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


if __name__ == "__main__":
    unittest.main()
