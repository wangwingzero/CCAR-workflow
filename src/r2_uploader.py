"""
Cloudflare R2 Upload Module

Uploads downloaded PDF files to R2 via Node.js AWS S3 SDK.
Node.js uses BoringSSL which bypasses OpenSSL 3.5.x TLS issues on CI.
Gracefully degrades when credentials are not configured.

Required env vars:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_DOMAIN
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from loguru import logger


class R2Uploader:
    """Cloudflare R2 file uploader using Node.js S3 SDK."""

    CONTENT_TYPES = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain; charset=utf-8",
    }

    def __init__(self):
        self.account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
        self.bucket = os.environ.get("R2_BUCKET", "").strip()
        self.domain = os.environ.get("R2_DOMAIN", "").strip().rstrip("/")
        self._access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        self._secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()

        required = {
            "R2_ACCOUNT_ID": self.account_id,
            "R2_BUCKET": self.bucket,
            "R2_DOMAIN": self.domain,
            "R2_ACCESS_KEY_ID": self._access_key,
            "R2_SECRET_ACCESS_KEY": self._secret_key,
        }
        missing = [name for name, val in required.items() if not val]

        if missing:
            logger.warning(f"R2 upload disabled, missing env vars: {missing}")
            self.enabled = False
            return

        # Find the upload script
        self._script_path = str(
            Path(__file__).parent.parent / "scripts" / "r2-upload.mjs"
        )
        if not os.path.exists(self._script_path):
            logger.warning(f"R2 upload disabled: script not found at {self._script_path}")
            self.enabled = False
            return

        # Verify node is available
        try:
            result = subprocess.run(
                ["node", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(f"node failed: {result.stderr}")
            self.enabled = True
            logger.info(
                f"R2 uploader initialized: bucket={self.bucket}, "
                f"domain={self.domain}, node={result.stdout.strip()}"
            )
        except FileNotFoundError:
            logger.warning("R2 upload disabled: node not found")
            self.enabled = False
        except Exception as e:
            logger.warning(f"R2 upload disabled: node check failed: {e}")
            self.enabled = False

    def _get_content_type(self, file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        return self.CONTENT_TYPES.get(ext, "application/octet-stream")

    def _build_public_url(self, r2_key: str) -> str:
        encoded_key = quote(r2_key, safe="/")
        return f"https://{self.domain}/{encoded_key}"

    def upload_file(self, local_path: str, r2_key: str) -> Optional[str]:
        """Upload a single file to R2 via Node.js script. Returns public URL or None."""
        if not self.enabled:
            return None
        try:
            content_type = self._get_content_type(local_path)
            env = {
                **os.environ,
                "R2_ACCOUNT_ID": self.account_id,
                "R2_ACCESS_KEY_ID": self._access_key,
                "R2_SECRET_ACCESS_KEY": self._secret_key,
                "R2_BUCKET": self.bucket,
            }

            result = subprocess.run(
                ["node", self._script_path, local_path, r2_key, content_type],
                capture_output=True, text=True, timeout=120, env=env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(error_msg[:500])

            public_url = self._build_public_url(r2_key)
            logger.debug(f"Uploaded to R2: {r2_key}")
            return public_url
        except Exception as e:
            logger.warning(f"R2 upload failed for {r2_key}: {e}")
            return None

    def upload_downloads(
        self,
        download_index: dict,
        download_dir: str = "downloads",
        r2_index_path: str = "data/r2_uploads.json",
    ) -> dict[str, str]:
        """Batch upload PDFs from download_index to R2.

        Args:
            download_index: {caac_url: {relative_path, updated_at}}
            download_dir: base directory for local files
            r2_index_path: path to R2 upload tracking index

        Returns:
            {caac_url: r2_public_url} for all available files (cached + newly uploaded)
        """
        if not self.enabled:
            return {}

        r2_index = self._load_r2_index(r2_index_path)
        r2_url_map: dict[str, str] = {}
        uploaded = 0
        cached = 0
        skipped = 0
        failed = 0

        for caac_url, record in download_index.items():
            rel_path = str(record.get("relative_path", "")).strip()
            if not rel_path:
                continue

            if not rel_path.lower().endswith(".pdf"):
                skipped += 1
                continue

            local_path = os.path.join(download_dir, rel_path)
            if not os.path.exists(local_path):
                skipped += 1
                continue

            # Check cache: skip upload if file size matches
            file_size = os.path.getsize(local_path)
            cached_record = r2_index.get(rel_path)
            if cached_record and cached_record.get("file_size") == file_size:
                r2_url_map[caac_url] = cached_record["r2_url"]
                cached += 1
                continue

            r2_url = self.upload_file(local_path, rel_path)
            if r2_url:
                r2_url_map[caac_url] = r2_url
                r2_index[rel_path] = {
                    "r2_url": r2_url,
                    "file_size": file_size,
                }
                uploaded += 1
            else:
                # Still use cached URL if upload fails
                if cached_record:
                    r2_url_map[caac_url] = cached_record["r2_url"]
                    cached += 1
                else:
                    failed += 1

        logger.info(
            f"R2 batch: uploaded={uploaded}, cached={cached}, "
            f"skipped={skipped}, failed={failed}"
        )

        if uploaded > 0:
            self._save_r2_index(r2_index_path, r2_index)

        return r2_url_map

    @staticmethod
    def _load_r2_index(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("records", {})
        except Exception:
            return {}

    @staticmethod
    def _save_r2_index(path: str, records: dict) -> None:
        from datetime import datetime

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_update": datetime.now().isoformat(),
            "records": records,
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
