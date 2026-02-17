"""
Cloudflare R2 Upload Module

Uploads downloaded PDF files to R2 via S3-compatible API.
Uses botocore for SigV4 signing + httpx for HTTP (avoids boto3 urllib3 SSL issues).
Gracefully degrades when credentials are not configured.

Required env vars:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_DOMAIN
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from loguru import logger


class R2Uploader:
    """Cloudflare R2 file uploader using botocore SigV4 + httpx."""

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
        access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()

        required = {
            "R2_ACCOUNT_ID": self.account_id,
            "R2_BUCKET": self.bucket,
            "R2_DOMAIN": self.domain,
            "R2_ACCESS_KEY_ID": access_key,
            "R2_SECRET_ACCESS_KEY": secret_key,
        }
        missing = [name for name, val in required.items() if not val]

        if missing:
            logger.warning(f"R2 upload disabled, missing env vars: {missing}")
            self.enabled = False
            return

        try:
            from botocore.auth import SigV4Auth
            from botocore.credentials import Credentials

            self._credentials = Credentials(access_key, secret_key)
            self._signer_cls = SigV4Auth
        except ImportError:
            logger.warning("R2 upload disabled: botocore not installed")
            self.enabled = False
            return

        self._endpoint_url = (
            f"https://{self.account_id}.r2.cloudflarestorage.com"
        )
        self.enabled = True
        logger.info(f"R2 uploader initialized: bucket={self.bucket}, domain={self.domain}")
        self._diagnose_connectivity()

    def _diagnose_connectivity(self):
        """Log diagnostic info for R2 endpoint connectivity."""
        import socket
        import ssl
        import subprocess

        host = f"{self.account_id}.r2.cloudflarestorage.com"
        logger.info(f"R2 diagnostics: Python ssl.OPENSSL_VERSION={ssl.OPENSSL_VERSION}")

        # 1. DNS resolution
        try:
            ips = socket.getaddrinfo(host, 443, socket.AF_INET)
            logger.info(f"R2 diagnostics: DNS resolved {host} -> {ips[0][4][0]}")
        except Exception as e:
            logger.warning(f"R2 diagnostics: DNS failed for {host}: {e}")
            return

        # 2. TCP connectivity
        try:
            sock = socket.create_connection((host, 443), timeout=5)
            sock.close()
            logger.info("R2 diagnostics: TCP connection OK")
        except Exception as e:
            logger.warning(f"R2 diagnostics: TCP connection failed: {e}")
            return

        # 3. OpenSSL s_client test (force TLS 1.2)
        try:
            result = subprocess.run(
                ["openssl", "s_client", "-connect", f"{host}:443",
                 "-servername", host, "-brief", "-tls1_2"],
                capture_output=True, text=True, timeout=10,
                input=""
            )
            output = (result.stdout + result.stderr)[:500]
            logger.info(f"R2 diagnostics: openssl s_client -tls1_2: {output}")
        except Exception as e:
            logger.warning(f"R2 diagnostics: openssl s_client failed: {e}")

        # 4. Python ssl wrap test (force TLS 1.2)
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_default_certs()
            with socket.create_connection((host, 443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    logger.info(
                        f"R2 diagnostics: Python TLS 1.2 OK, version={ssock.version()}, "
                        f"cipher={ssock.cipher()}"
                    )
        except Exception as e:
            logger.warning(f"R2 diagnostics: Python TLS 1.2 wrap failed: {e}")

        # 5. Python ssl wrap test (default TLS)
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    logger.info(
                        f"R2 diagnostics: Python TLS default OK, version={ssock.version()}, "
                        f"cipher={ssock.cipher()}"
                    )
        except Exception as e:
            logger.warning(f"R2 diagnostics: Python TLS default failed: {e}")

    def _get_content_type(self, file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        return self.CONTENT_TYPES.get(ext, "application/octet-stream")

    def _build_public_url(self, r2_key: str) -> str:
        encoded_key = quote(r2_key, safe="/")
        return f"https://{self.domain}/{encoded_key}"

    def upload_file(self, local_path: str, r2_key: str) -> Optional[str]:
        """Upload a single file to R2. Returns public URL or None on failure."""
        if not self.enabled:
            return None
        try:
            from botocore.awsrequest import AWSRequest

            content_type = self._get_content_type(local_path)

            with open(local_path, "rb") as f:
                data = f.read()

            content_sha256 = hashlib.sha256(data).hexdigest()
            url = f"{self._endpoint_url}/{self.bucket}/{quote(r2_key, safe='/')}"

            headers = {
                "Content-Type": content_type,
                "x-amz-content-sha256": content_sha256,
                "Content-Length": str(len(data)),
            }

            aws_request = AWSRequest(method="PUT", url=url, headers=headers, data=data)
            self._signer_cls(self._credentials, "s3", "auto").add_auth(aws_request)

            # Force TLS 1.2 to work around OpenSSL 3.5.x handshake issues with R2
            import ssl

            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.load_default_certs()

            with httpx.Client(timeout=120, verify=ssl_context) as client:
                response = client.put(
                    url,
                    headers=dict(aws_request.headers),
                    content=data,
                )
                response.raise_for_status()

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
            f"R2 batch: uploaded={uploaded}, cached={cached}, skipped={skipped}, failed={failed}"
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
