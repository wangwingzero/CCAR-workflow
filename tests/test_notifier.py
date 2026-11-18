import unittest

from src.crawler import Document
from src.notifier import Notifier


def make_doc(url: str, title: str = "", doc_number: str = "") -> Document:
    return Document(
        title=title or f"Doc {url}",
        url=url,
        category="民航规章",
        category_id="13",
        doc_number=doc_number,
        office_unit="",
    )


class UpdateMarkingTests(unittest.TestCase):
    """Documents whose metadata changed are visibly marked as updates."""

    def setUp(self):
        self.new_doc = make_doc("https://example.test/13/new", "新增规章")
        self.updated_doc = make_doc(
            "https://example.test/13/updated", "更新规章", "CCAR-91-R5"
        )
        self.documents = {"民航规章": [self.new_doc, self.updated_doc]}

    def test_plain_text_marks_only_updated_documents(self):
        notifier = Notifier()
        _, plain, _ = notifier.format_update_message(
            self.documents, updated_urls={self.updated_doc.url}
        )

        self.assertIn("其中更新 1 条", plain)
        updated_lines = [line for line in plain.splitlines() if "🔄 [更新]" in line]
        self.assertEqual(1, len(updated_lines))
        self.assertIn("更新规章", updated_lines[0])
        self.assertNotIn(f"🔄 [更新] {self.new_doc.title}", plain)

    def test_html_shows_update_badge_only_for_updated_documents(self):
        notifier = Notifier()
        _, _, html = notifier.format_update_message(
            self.documents, updated_urls={self.updated_doc.url}
        )

        self.assertEqual(1, html.count(">更新</span>"))

    def test_no_marking_without_updated_urls(self):
        notifier = Notifier()
        _, plain, html = notifier.format_update_message(self.documents)

        self.assertNotIn("🔄 [更新]", plain)
        self.assertNotIn("其中更新", plain)
        self.assertNotIn(">更新</span>", html)


class OtherCategorySummaryTests(unittest.TestCase):
    """A heartbeat still tells the reader how many other-category docs were seen."""

    def test_plain_and_html_include_other_category_summary(self):
        notifier = Notifier()
        title, plain, html = notifier.format_update_message(
            {},
            other_updates={"通知公告": 2, "政策发布": 1},
        )

        self.assertIn("0 条", title)
        self.assertIn("其他分类新增: 3 条", plain)
        self.assertIn("通知公告 2", plain)
        self.assertIn("政策发布 1", plain)
        self.assertIn("未展开", plain)
        self.assertIn("其他分类新增 3 条", html)
        self.assertIn("未展开", html)

    def test_other_category_summary_appears_next_to_notified_documents(self):
        notifier = Notifier()
        _, plain, html = notifier.format_update_message(
            {"民航规章": [make_doc("https://example.test/13/new", "新增规章")]},
            other_updates={"通知公告": 1},
        )

        self.assertIn("新增文件: 1 条", plain)
        self.assertIn("其他分类新增: 1 条", plain)
        self.assertIn("其他分类新增 1 条", html)

    def test_no_other_category_summary_without_other_updates(self):
        notifier = Notifier()
        _, plain, html = notifier.format_update_message(
            {"民航规章": [make_doc("https://example.test/13/new", "新增规章")]}
        )

        self.assertNotIn("其他分类新增", plain)
        self.assertNotIn("其他分类新增", html)


if __name__ == "__main__":
    unittest.main()
