import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.main import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import repair_categories  # noqa: E402

BASE = "http://www.caac.gov.cn/XXGK/XXGK"


def make_row(url: str, category_id: str, title: str = "doc") -> dict:
    return {"url": url, "category_id": category_id, "category": "old", "title": title}


class PlanMovesTests(unittest.TestCase):
    def test_rows_are_moved_to_the_category_implied_by_their_url(self):
        documents = {
            "28": [
                make_row(f"{BASE}/ZFGW/202610/t20261009_1.html", "28"),
                make_row(f"{BASE}/TZTG/202610/t20261009_2.html", "28"),
            ],
            "18": [make_row(f"{BASE}/GJGY/202610/t20261009_3.html", "18")],
            "17": [make_row(f"{BASE}/UNKNOWNCODE/202610/t20261009_4.html", "17")],
        }

        new_documents, moves = repair_categories.plan_moves(documents, ["28", "18", "17"])

        self.assertEqual(
            [("28", "24"), ("28", "9")], [(source, target) for source, target, _ in moves]
        )
        self.assertEqual(1, len(new_documents["18"]))
        self.assertEqual(1, len(new_documents["17"]))
        self.assertEqual(0, len(new_documents["28"]))
        # original mapping untouched
        self.assertEqual(2, len(documents["28"]))

    def test_unclassified_and_matching_rows_are_kept(self):
        documents = {
            "28": [
                make_row(f"{BASE}/JYTNDF/202610/t20261009_5.html", "28"),
                make_row("https://example.test/not-a-column-page.html", "28"),
            ]
        }

        new_documents, moves = repair_categories.plan_moves(documents, ["28"])

        self.assertEqual([], moves)
        self.assertEqual(2, len(new_documents["28"]))


class ApplyMovesTests(unittest.TestCase):
    def test_moved_rows_get_new_category_fields(self):
        documents = {"28": [make_row(f"{BASE}/ZFGW/202610/t20261009_1.html", "28")]}
        new_documents, moves = repair_categories.plan_moves(documents, ["28"])

        moved, deduped = repair_categories.apply_moves(new_documents, moves)

        self.assertEqual((1, 0), (moved, deduped))
        self.assertEqual([], new_documents["28"])
        self.assertEqual("24", new_documents["24"][0]["category_id"])
        self.assertEqual("政府公文", new_documents["24"][0]["category"])

    def test_rows_already_present_in_the_target_are_dropped(self):
        url = f"{BASE}/ZFGW/202610/t20261009_1.html"
        documents = {
            "28": [make_row(url, "28")],
            "24": [make_row(url, "24")],
        }
        new_documents, moves = repair_categories.plan_moves(documents, ["28"])

        moved, deduped = repair_categories.apply_moves(new_documents, moves)

        self.assertEqual((0, 1), (moved, deduped))
        self.assertEqual(1, len(new_documents["24"]))


class DedupeTests(unittest.TestCase):
    def test_exact_duplicate_rows_are_dropped(self):
        row = make_row(f"{BASE}/ZFGW/202610/t20261009_1.html", "24")
        documents = {"24": [row, dict(row)]}

        new_documents, dropped, conflicts = repair_categories.dedupe_documents(documents)

        self.assertEqual(1, dropped)
        self.assertEqual([], conflicts)
        self.assertEqual(1, len(new_documents["24"]))

    def test_same_url_with_different_metadata_is_kept_and_reported(self):
        first = make_row(f"{BASE}/ZFGW/202610/t20261009_1.html", "24", title="old")
        second = make_row(f"{BASE}/ZFGW/202610/t20261009_1.html", "24", title="new")
        documents = {"24": [first, second]}

        new_documents, dropped, conflicts = repair_categories.dedupe_documents(documents)

        self.assertEqual(0, dropped)
        self.assertEqual(
            [("24", f"{BASE}/ZFGW/202610/t20261009_1.html")], conflicts
        )
        self.assertEqual(2, len(new_documents["24"]))


class MainTests(unittest.TestCase):
    def _write_state(self, directory: Path) -> Path:
        state_path = directory / "regulations.json"
        state_path.write_text(
            json.dumps(
                {
                    "last_check": "2026-11-14T00:00:00",
                    "documents": {
                        "28": [make_row(f"{BASE}/ZFGW/202610/t20261009_1.html", "28")]
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return state_path

    def test_dry_run_does_not_touch_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self._write_state(Path(tmp))
            before = state_path.read_text(encoding="utf-8")

            exit_code = repair_categories.main(
                ["--state", str(state_path), "--categories", "28"]
            )

            self.assertEqual(0, exit_code)
            self.assertEqual(before, state_path.read_text(encoding="utf-8"))

    def test_apply_rewrites_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self._write_state(Path(tmp))

            exit_code = repair_categories.main(
                ["--state", str(state_path), "--categories", "28", "--apply"]
            )

            self.assertEqual(0, exit_code)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual([], payload["documents"].get("28", []))
            self.assertEqual(1, len(payload["documents"]["24"]))
            self.assertEqual("24", payload["documents"]["24"][0]["category_id"])

    def test_unknown_category_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self._write_state(Path(tmp))

            exit_code = repair_categories.main(
                ["--state", str(state_path), "--categories", "28,999"]
            )

            self.assertEqual(1, exit_code)


if __name__ == "__main__":
    unittest.main()
