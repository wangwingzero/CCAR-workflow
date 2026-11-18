import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import main as src_main
from src.crawler import Document
from src.main import (
    MAX_DOWNLOAD_ATTEMPTS,
    _carry_over_known_metadata,
    _collect_metadata_repair_targets,
    _filter_documents_by_category,
    _find_missing_r2_urls,
    _load_download_retries,
    _parse_category_filter,
    _retry_documents_by_category,
    _save_download_retries,
    _select_download_documents,
    _select_metadata_backfilled_documents,
    _select_notify_attachments,
    _update_download_retries,
)
from src.storage import ChangeResult, StorageState


def make_doc(url: str, category_id: str = "13") -> Document:
    return Document(
        title=f"Doc {url}",
        url=url,
        category="民航规章",
        category_id=category_id,
        doc_number="",
        office_unit="",
    )


class DownloadSelectionTests(unittest.TestCase):
    def test_updated_documents_are_skipped_by_default(self):
        new_doc = make_doc("https://example.test/new")
        updated_doc = make_doc("https://example.test/updated")

        selected = _select_download_documents(
            {"13": [new_doc]},
            {"13": [updated_doc]},
        )

        self.assertEqual({"13": [new_doc]}, selected)

    def test_updated_documents_can_be_included_explicitly(self):
        new_doc = make_doc("https://example.test/new")
        updated_doc = make_doc("https://example.test/updated")

        selected = _select_download_documents(
            {"13": [new_doc]},
            {"13": [updated_doc]},
            include_updated=True,
        )

        self.assertEqual({"13": [new_doc, updated_doc]}, selected)


class R2UrlValidationTests(unittest.TestCase):
    def test_reports_downloaded_pdf_urls_missing_from_r2_map(self):
        missing = _find_missing_r2_urls(
            {
                "https://example.test/has-url",
                "https://example.test/missing-url",
            },
            {
                "https://example.test/has-url": "https://files.example.test/doc.pdf",
            },
        )

        self.assertEqual(["https://example.test/missing-url"], missing)


class NotifyCategoryFilterTests(unittest.TestCase):
    def test_parse_category_filter_ignores_blank_items(self):
        self.assertEqual({"13", "14", "15"}, _parse_category_filter(" 13,14 , 15 , "))

    def test_parse_category_filter_empty_means_no_filter(self):
        self.assertEqual(set(), _parse_category_filter(None))
        self.assertEqual(set(), _parse_category_filter(""))

    def test_filter_keeps_only_selected_categories(self):
        documents = {
            "9": [make_doc("https://example.test/notice", "9")],
            "13": [make_doc("https://example.test/ccar", "13")],
            "15": [make_doc("https://example.test/standard", "15")],
        }

        filtered = _filter_documents_by_category(documents, {"13", "15"})

        self.assertEqual(["13", "15"], sorted(filtered))
        self.assertEqual(documents["13"], filtered["13"])

    def test_empty_filter_keeps_every_category(self):
        documents = {
            "9": [make_doc("https://example.test/notice", "9")],
            "13": [make_doc("https://example.test/ccar", "13")],
        }

        self.assertEqual(documents, _filter_documents_by_category(documents, set()))

    def test_filter_without_matches_returns_empty_mapping(self):
        documents = {"9": [make_doc("https://example.test/notice", "9")]}

        self.assertEqual({}, _filter_documents_by_category(documents, {"13", "14", "15"}))

    def test_attachments_without_filter_keep_everything(self):
        by_category = {
            "9": ["downloads/other/notice.pdf"],
            "13": ["downloads/regulation/ccar.pdf"],
        }

        selected = _select_notify_attachments(
            ["downloads/other/notice.pdf", "downloads/regulation/ccar.pdf"],
            by_category,
            set(),
        )

        self.assertEqual(
            ["downloads/other/notice.pdf", "downloads/regulation/ccar.pdf"], selected
        )

    def test_attachments_are_limited_to_notified_categories(self):
        selected = _select_notify_attachments(
            [
                "downloads/other/notice.pdf",
                "downloads/regulation/ccar.pdf",
                "downloads/specification/standard.pdf",
            ],
            {
                "9": ["downloads/other/notice.pdf"],
                "13": ["downloads/regulation/ccar.pdf"],
                "15": ["downloads/specification/standard.pdf"],
            },
            {"13", "14", "15"},
        )

        self.assertEqual(
            ["downloads/regulation/ccar.pdf", "downloads/specification/standard.pdf"],
            selected,
        )


class DetailMetadataHelperTests(unittest.TestCase):
    def test_carry_over_reuses_stored_doc_number_for_known_url(self):
        crawled = make_doc("https://example.test/15/known", "15")
        stored_rows = {
            "15": [{
                "url": "https://example.test/15/known",
                "doc_number": "MH/T 4067-2026",
                "validity": "有效",
                "office_unit": "航空器适航审定司",
                "publish_date": "2026-10-23",
            }],
        }

        carried_count = _carry_over_known_metadata({"15": [crawled]}, stored_rows)

        self.assertEqual(1, carried_count)
        self.assertEqual("MH/T 4067-2026", crawled.doc_number)
        self.assertEqual("有效", crawled.validity)
        self.assertEqual("2026-10-23", crawled.publish_date)

    def test_carry_over_ignores_rows_that_never_had_metadata(self):
        crawled = make_doc("https://example.test/15/known", "15")
        stored_rows = {"15": [{"url": "https://example.test/15/known", "doc_number": ""}]}

        self.assertEqual(0, _carry_over_known_metadata({"15": [crawled]}, stored_rows))
        self.assertEqual("", crawled.doc_number)

    def test_backfilled_selection_only_picks_newly_numbered_rows(self):
        newly_numbered = make_doc("https://example.test/15/a", "15")
        newly_numbered.doc_number = "MH/T 4067-2026"
        already_numbered = make_doc("https://example.test/15/b", "15")
        already_numbered.doc_number = "MH/T 4068-2026"
        stored_rows = {
            "15": [
                {"url": "https://example.test/15/a", "doc_number": ""},
                {"url": "https://example.test/15/b", "doc_number": "MH/T 4068-2026"},
            ],
        }

        selected = _select_metadata_backfilled_documents(
            {"15": [newly_numbered, already_numbered]}, stored_rows
        )

        self.assertEqual({"15": [newly_numbered]}, selected)

    def test_repair_targets_split_attachments_from_rows_missing_metadata(self):
        stored_rows = {
            "15": [
                {"url": "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/P020261109628802830918.pdf",
                 "category_id": "15", "title": "航班唯一飞行标识符数据规范.pdf"},
                {"url": "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/t20261109_232033.html",
                 "category_id": "15", "title": "航班唯一飞行标识符数据规范"},
                {"url": "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202610/t20261015_231865.html",
                 "category_id": "15", "title": "已有文号", "doc_number": "MH/T 4066-2026"},
            ],
            "9": [
                {"url": "http://www.caac.gov.cn/XXGK/XXGK/TZTG/202611/P020261109449420328523.pdf",
                 "category_id": "9", "title": "通知公告附件.pdf"},
            ],
        }

        missing_metadata, attachment_urls = _collect_metadata_repair_targets(stored_rows)

        self.assertEqual(
            {"http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/P020261109628802830918.pdf"},
            attachment_urls,
        )
        self.assertEqual(
            ["http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/t20261109_232033.html"],
            [document.url for document in missing_metadata["15"]],
        )


class _FakeStorage:
    """Storage stub: one known doc per stored category, no download index."""

    def __init__(self, path, stored_categories=("9", "13", "15")):
        self.data_path = path
        self.stored_categories = tuple(stored_categories)
        self.updated_state = None
        self.new_documents = {}
        self.updated_documents = {}

    def load(self):
        state = StorageState()
        state.documents = {
            cat: [make_doc(f"https://example.test/{cat}/known", cat)]
            for cat in self.stored_categories
        }
        return state

    def detect_changes(self, current_documents):
        return ChangeResult(
            new_documents=self.new_documents,
            updated_documents=self.updated_documents,
        )

    def load_download_index(self):
        return {}

    def save_download_index(self, index):
        pass

    def sync_js_files(self, documents, js_dir, r2_url_map=None):
        return {}

    def update_state(self, documents):
        self.updated_state = documents


class _FakeCrawler:
    def __init__(self, storage):
        self.storage = storage
        self.downloaded: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetch_all_categories(self, category_ids, perpage):
        return {cat: list(docs) for cat, docs in self.storage.new_documents.items()}

    def enrich_documents_with_detail_metadata(self, documents_by_category):
        return 0

    def download_document_file(self, doc, save_base_path):
        self.downloaded.append(doc.url)
        return f"{save_base_path}.pdf"


class _FakeNotifier:
    sent = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def format_update_message(
        self, docs_by_category, updated_urls=None, other_updates=None
    ):
        self.sent = {
            "categories": sorted(docs_by_category),
            "attachments": [],
            "document_urls": sorted(
                doc.url for docs in docs_by_category.values() for doc in docs
            ),
            "updated_urls": set(updated_urls or ()),
            "other_updates": dict(other_updates or {}),
        }
        return ("title", "text", "html")

    def send_all(self, title, text, html, attachments=None):
        self.sent["attachments"] = attachments or []
        return {"Email": True}


class _FakeR2:
    enabled = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NotifyCategoryFlowTests(unittest.TestCase):
    """Run main() with stubbed collaborators to check the end-to-end filtering."""

    def _run(self, new_documents, argv, stored_categories=("9", "13", "15")):
        storage = _FakeStorage("data/regulations.json", stored_categories)
        storage.new_documents = new_documents
        crawler = _FakeCrawler(storage)
        self.crawler = crawler
        notifier = _FakeNotifier()
        notifier.sent = None

        with mock.patch.object(src_main, "Storage", return_value=storage), \
             mock.patch.object(src_main, "CaacCrawler", lambda: crawler), \
             mock.patch.object(src_main, "Notifier", lambda: notifier), \
             mock.patch.object(src_main, "R2Uploader", lambda: _FakeR2()), \
             mock.patch("sys.argv", ["main", *argv, "--notify", "1"]), \
             mock.patch.dict("os.environ", {"NOTIFY_CATEGORIES": ""}):
            exit_code = src_main.main()

        self.assertEqual(0, exit_code)
        return notifier.sent, storage.updated_state

    def test_notification_is_limited_to_selected_categories(self):
        new_documents = {
            "9": [make_doc("https://example.test/9/notice", "9")],
            "13": [make_doc("https://example.test/13/ccar", "13")],
            "15": [make_doc("https://example.test/15/standard", "15")],
        }

        sent, updated_state = self._run(new_documents, ["--notify-categories", "13,14,15"])

        self.assertEqual(["标准规范", "民航规章"], sent["categories"])
        self.assertEqual(2, len(sent["attachments"]))
        self.assertTrue(all("notice" not in path for path in sent["attachments"]))
        self.assertEqual({"通知公告": 1}, sent["other_updates"])
        # Crawling/state must stay complete even though only 13/15 were emailed.
        self.assertEqual(["13", "15", "9"], sorted(updated_state))

    def test_sends_heartbeat_when_only_other_categories_changed(self):
        sent, updated_state = self._run(
            {"9": [make_doc("https://example.test/9/notice", "9")]},
            ["--notify-categories", "13,14,15"],
        )

        # --notify 1 means a daily "暂无更新" email even when nothing in
        # the notified categories changed; the summary line still reports the
        # documents that were detected outside the notified categories.
        self.assertIsNotNone(sent)
        self.assertEqual([], sent["categories"])
        self.assertEqual([], sent["attachments"])
        self.assertEqual({"通知公告": 1}, sent["other_updates"])
        self.assertEqual(["9"], sorted(updated_state))

    def test_stays_quiet_with_notify_zero_when_nothing_relevant_changed(self):
        storage = _FakeStorage("data/regulations.json")
        storage.new_documents = {"9": [make_doc("https://example.test/9/notice", "9")]}
        notifier = _FakeNotifier()
        notifier.sent = None

        with mock.patch.object(src_main, "Storage", return_value=storage), \
             mock.patch.object(src_main, "CaacCrawler", lambda: _FakeCrawler(storage)), \
             mock.patch.object(src_main, "Notifier", lambda: notifier), \
             mock.patch.object(src_main, "R2Uploader", lambda: _FakeR2()), \
             mock.patch("sys.argv", ["main", "--notify-categories", "13,14,15", "--notify", "0"]), \
             mock.patch.dict("os.environ", {"NOTIFY_CATEGORIES": ""}):
            self.assertEqual(0, src_main.main())

        self.assertIsNone(notifier.sent)

    def test_without_filter_every_category_is_notified(self):
        new_documents = {
            "9": [make_doc("https://example.test/9/notice", "9")],
            "13": [make_doc("https://example.test/13/ccar", "13")],
        }

        sent, _ = self._run(new_documents, [])

        self.assertEqual(["民航规章", "通知公告"], sent["categories"])


class DownloadRetryQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "download_retry.json")

    def test_load_missing_file_returns_empty(self):
        self.assertEqual({}, _load_download_retries(self.path))

    def test_succeeded_url_is_removed(self):
        doc_a = make_doc("https://example.test/a", "13")
        doc_b = make_doc("https://example.test/b", "13")
        _save_download_retries(self.path, {
            doc_a.url: {"attempts": 1, "document": doc_a.to_dict()},
            doc_b.url: {"attempts": 2, "document": doc_b.to_dict()},
        })

        _update_download_retries(
            self.path, _load_download_retries(self.path), {doc_a.url}, []
        )

        records = _load_download_retries(self.path)
        self.assertEqual([doc_b.url], list(records))
        self.assertEqual(2, records[doc_b.url]["attempts"])

    def test_failed_attempts_are_counted_then_given_up(self):
        doc = make_doc("https://example.test/fail", "13")

        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS):
            _update_download_retries(
                self.path, _load_download_retries(self.path), set(), [doc]
            )
            records = _load_download_retries(self.path)
            self.assertEqual(attempt, records[doc.url]["attempts"])

        _update_download_retries(
            self.path, _load_download_retries(self.path), set(), [doc]
        )
        self.assertEqual({}, _load_download_retries(self.path))

    def test_retry_documents_are_limited_to_crawled_categories(self):
        doc_13 = make_doc("https://example.test/13/retry", "13")
        doc_14 = make_doc("https://example.test/14/retry", "14")
        retries = {
            doc_13.url: {"attempts": 1, "document": doc_13.to_dict()},
            doc_14.url: {"attempts": 1, "document": doc_14.to_dict()},
        }

        result = _retry_documents_by_category(retries, {"13": []})

        self.assertEqual(
            [doc_13.url], [doc.url for docs in result.values() for doc in docs]
        )


class MetadataValidityResyncTests(unittest.TestCase):
    def test_validity_change_affecting_filename_prefix_is_resynced(self):
        doc = make_doc("https://example.test/15/a", "15")
        doc.validity = "失效"
        stored = {"15": [{"url": doc.url, "doc_number": "", "validity": "有效"}]}

        selected = _select_metadata_backfilled_documents({"15": [doc]}, stored)

        self.assertEqual([doc], selected["15"])

    def test_empty_to_effective_validity_does_not_need_resync(self):
        doc = make_doc("https://example.test/15/b", "15")
        doc.validity = "有效"
        stored = {"15": [{"url": doc.url, "doc_number": "", "validity": ""}]}

        self.assertEqual({}, _select_metadata_backfilled_documents({"15": [doc]}, stored))


class BaselineCategoryFlowTests(unittest.TestCase):
    def test_new_category_is_recorded_without_notifying_or_downloading(self):
        storage = _FakeStorage("data/regulations.json", stored_categories=("13",))
        existing_doc = make_doc("https://example.test/13/new", "13")
        new_category_doc = make_doc("https://example.test/14/first", "14")
        storage.new_documents = {"13": [existing_doc], "14": [new_category_doc]}
        crawler = _FakeCrawler(storage)
        notifier = _FakeNotifier()
        notifier.sent = None

        with mock.patch.object(src_main, "Storage", return_value=storage), \
             mock.patch.object(src_main, "CaacCrawler", lambda: crawler), \
             mock.patch.object(src_main, "Notifier", lambda: notifier), \
             mock.patch.object(src_main, "R2Uploader", lambda: _FakeR2()), \
             mock.patch("sys.argv", ["main", "--notify", "1"]), \
             mock.patch.dict("os.environ", {"NOTIFY_CATEGORIES": ""}):
            self.assertEqual(0, src_main.main())

        # Only the genuinely new document in the already-known category is
        # notified/downloaded; the new category is baseline-only.
        self.assertEqual(["民航规章"], notifier.sent["categories"])
        self.assertEqual([existing_doc.url], crawler.downloaded)
        self.assertEqual(["13", "14"], sorted(storage.updated_state))


class BackfillGuardTests(unittest.TestCase):
    def _run(self, argv, r2_enabled):
        storage = _FakeStorage("data/regulations.json")
        storage.new_documents = {"13": [make_doc("https://example.test/13/known", "13")]}
        crawler = _FakeCrawler(storage)
        notifier = _FakeNotifier()
        notifier.sent = None
        r2 = _FakeR2()
        r2.enabled = r2_enabled

        with mock.patch.object(src_main, "Storage", return_value=storage), \
             mock.patch.object(src_main, "CaacCrawler", lambda: crawler), \
             mock.patch.object(src_main, "Notifier", lambda: notifier), \
             mock.patch.object(src_main, "R2Uploader", lambda: r2), \
             mock.patch("sys.argv", ["main", *argv]), \
             mock.patch.dict("os.environ", {"NOTIFY_CATEGORIES": ""}):
            exit_code = src_main.main()

        self.assertIsNone(notifier.sent)
        return exit_code

    def test_backfill_requires_configured_r2(self):
        self.assertEqual(1, self._run(["--backfill-r2"], r2_enabled=False))

    def test_backfill_rejects_no_download(self):
        self.assertEqual(1, self._run(["--backfill-r2", "--no-download"], r2_enabled=True))


class NotifyUpdatedFlowTests(unittest.TestCase):
    """--notify-updated surfaces metadata-only changes in the notification."""

    def _run(self, argv):
        storage = _FakeStorage("data/regulations.json", stored_categories=("13",))
        new_doc = make_doc("https://example.test/13/new", "13")
        updated_doc = make_doc("https://example.test/13/updated", "13")
        storage.new_documents = {"13": [new_doc]}
        storage.updated_documents = {"13": [updated_doc]}
        crawler = _FakeCrawler(storage)
        notifier = _FakeNotifier()
        notifier.sent = None

        with mock.patch.object(src_main, "Storage", return_value=storage), \
             mock.patch.object(src_main, "CaacCrawler", lambda: crawler), \
             mock.patch.object(src_main, "Notifier", lambda: notifier), \
             mock.patch.object(src_main, "R2Uploader", lambda: _FakeR2()), \
             mock.patch("sys.argv", ["main", *argv]), \
             mock.patch.dict("os.environ", {"NOTIFY_CATEGORIES": ""}):
            self.assertEqual(0, src_main.main())

        return notifier.sent

    def test_updated_documents_are_not_notified_by_default(self):
        sent = self._run([])

        self.assertEqual(["https://example.test/13/new"], sent["document_urls"])
        self.assertEqual(set(), sent["updated_urls"])

    def test_notify_updated_merges_updated_documents_into_notification(self):
        sent = self._run(["--notify-updated"])

        self.assertEqual(
            {
                "https://example.test/13/new",
                "https://example.test/13/updated",
            },
            set(sent["document_urls"]),
        )
        self.assertEqual({"https://example.test/13/updated"}, sent["updated_urls"])


class RepoPathDefaultsTests(unittest.TestCase):
    """Paths default to the repository root so the CLI works from any cwd."""

    def test_defaults_are_absolute_repo_paths(self):
        with mock.patch("sys.argv", ["main"]):
            args = src_main.parse_args()

        self.assertEqual(str(src_main.DEFAULT_DOWNLOAD_DIR), args.download_dir)
        self.assertTrue(Path(args.download_dir).is_absolute())
        self.assertEqual(src_main.PROJECT_ROOT, src_main.DATA_DIR.parent)
        self.assertEqual(src_main.PROJECT_ROOT, src_main.JS_DIR.parent)
        self.assertEqual(src_main.PROJECT_ROOT, src_main.DEFAULT_DOWNLOAD_DIR.parent)
        self.assertEqual(src_main.DATA_DIR, src_main.PROJECT_ROOT / "data")


if __name__ == "__main__":
    unittest.main()
