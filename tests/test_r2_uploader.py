import json
import tempfile
import unittest
from pathlib import Path

from src.r2_uploader import R2Uploader


class _FailingClient:
    def put(self, *args, **kwargs):
        raise RuntimeError("worker unavailable")


class _OkResponse:
    status_code = 200
    text = ""


class _OkClient:
    def put(self, *args, **kwargs):
        return _OkResponse()


class UploadDownloadsTests(unittest.TestCase):
    def _uploader(self, client=None) -> R2Uploader:
        uploader = object.__new__(R2Uploader)
        uploader.enabled = True
        uploader._client = client
        uploader.worker_url = "https://worker.test"
        uploader._secret = "secret"
        uploader.domain = "files.test"
        return uploader

    def _local_pdf(self, root: Path, relative_path: str) -> Path:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7" + b"\x00" * 2000)
        return path

    def test_cached_record_without_r2_url_is_skipped_when_file_missing(self):
        uploader = self._uploader()
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "r2_uploads.json"
            index_path.write_text(
                json.dumps({"records": {"regulation/missing.pdf": {"file_size": 1}}}),
                encoding="utf-8",
            )

            result = uploader.upload_downloads(
                {"http://caac.test/doc": {"relative_path": "regulation/missing.pdf"}},
                download_dir=tmp,
                r2_index_path=str(index_path),
            )

        self.assertEqual({}, result)

    def test_failed_upload_without_cached_url_counts_as_failure(self):
        uploader = self._uploader(_FailingClient())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._local_pdf(root, "regulation/doc.pdf")
            index_path = root / "r2_uploads.json"
            index_path.write_text(
                json.dumps({"records": {"regulation/doc.pdf": {"file_size": 1}}}),
                encoding="utf-8",
            )

            result = uploader.upload_downloads(
                {"http://caac.test/doc": {"relative_path": "regulation/doc.pdf"}},
                download_dir=tmp,
                r2_index_path=str(index_path),
            )

            stored = json.loads(index_path.read_text(encoding="utf-8"))["records"]

        self.assertEqual({}, result)
        # Nothing was uploaded, so the index must not be rewritten.
        self.assertNotIn("r2_url", stored["regulation/doc.pdf"])

    def test_successful_upload_returns_public_url_and_updates_index(self):
        uploader = self._uploader(_OkClient())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = self._local_pdf(root, "regulation/doc.pdf")
            index_path = root / "r2_uploads.json"

            result = uploader.upload_downloads(
                {"http://caac.test/doc": {"relative_path": "regulation/doc.pdf"}},
                download_dir=tmp,
                r2_index_path=str(index_path),
            )

            stored = json.loads(index_path.read_text(encoding="utf-8"))["records"]
            local_size = local.stat().st_size

        self.assertEqual(
            {"http://caac.test/doc": "https://files.test/regulation/doc.pdf"}, result
        )
        self.assertEqual(
            "https://files.test/regulation/doc.pdf",
            stored["regulation/doc.pdf"]["r2_url"],
        )
        self.assertEqual(local_size, stored["regulation/doc.pdf"]["file_size"])


if __name__ == "__main__":
    unittest.main()
