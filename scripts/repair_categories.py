"""Repair ``data/regulations.json``: re-categorise rows and drop duplicates.

Rows in the state file were historically grouped by the old was5 search channel
instead of the XXGK column code embedded in each URL, so categories such as
17/18/28/29 hold rows that belong to other columns. This script uses
:func:`src.search_api.classify_url` to detect such rows and move them to the
category their URL actually belongs to. It also drops byte-identical duplicate
rows (same URL and same payload) that state merges left behind.

It runs as a dry-run by default; pass ``--apply`` to write the state. JS exports
are not touched because only categories 13/14/15 are exported and they are
never re-categorised.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.crawler import CATEGORIES  # noqa: E402
from src.search_api import UNCLASSIFIED, classify_url  # noqa: E402
from src.storage import Storage  # noqa: E402

DEFAULT_STATE = PROJECT_ROOT / "data" / "regulations.json"
DEFAULT_CATEGORIES = "17,18,28,29"


def dedupe_documents(documents: dict):
    """Drop exact duplicate rows inside each category.

    Returns ``(new_documents, dropped, conflicts)``. Rows that share a URL but
    carry different metadata are all kept and reported in ``conflicts`` as
    ``(category_id, url)`` tuples, so nothing is silently lost.
    """
    new_documents = {}
    dropped = 0
    conflicts = []
    for cat_id, rows in documents.items():
        if not isinstance(rows, list):
            new_documents[cat_id] = rows
            continue
        seen = {}
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            url = row.get("url")
            if url in seen:
                if seen[url] == row:
                    dropped += 1
                else:
                    conflicts.append((cat_id, url))
                    kept.append(row)
                continue
            seen[url] = row
            kept.append(row)
        new_documents[cat_id] = kept
    return new_documents, dropped, conflicts


def plan_moves(documents: dict, categories: list[str]):
    """Return ``(new_documents, moves)`` without mutating ``documents``.

    ``moves`` is a list of ``(source_category, target_category, row)`` tuples for
    rows whose URL column code maps to a different known category.
    """
    new_documents = {
        cat_id: list(rows) for cat_id, rows in documents.items() if isinstance(rows, list)
    }
    moves = []
    for cat_id in categories:
        remaining = []
        for row in new_documents.get(cat_id, []):
            if not isinstance(row, dict):
                remaining.append(row)
                continue
            target = classify_url(str(row.get("url") or ""))
            if target == UNCLASSIFIED or target == cat_id:
                remaining.append(row)
                continue
            moves.append((cat_id, target, row))
        new_documents[cat_id] = remaining
    return new_documents, moves


def apply_moves(new_documents: dict, moves: list[tuple[str, str, dict]]):
    """Move rows into their target category; returns ``(moved, deduped)``.

    A row is dropped instead of moved when the target category already holds a
    document with the same URL.
    """
    moved = 0
    deduped = 0
    for _source, target, row in moves:
        rows = new_documents.setdefault(target, [])
        url = row.get("url")
        if any(
            isinstance(existing, dict) and existing.get("url") == url for existing in rows
        ):
            deduped += 1
            continue
        moved_row = dict(row)
        moved_row["category_id"] = target
        moved_row["category"] = CATEGORIES.get(target, target)
        rows.append(moved_row)
        moved += 1
    return moved, deduped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move state rows to the category implied by their XXGK column code and "
            "drop exact duplicate rows. Dry-run unless --apply is given."
        )
    )
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="state JSON path")
    parser.add_argument(
        "--categories",
        default=DEFAULT_CATEGORIES,
        help="comma separated category ids to inspect (default: 17,18,28,29)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the repaired state (default: dry-run)"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    categories = [item.strip() for item in args.categories.split(",") if item.strip()]
    unknown = [cat for cat in categories if cat not in CATEGORIES]
    if unknown:
        print(f"Unknown category ids: {', '.join(unknown)}", file=sys.stderr)
        return 1
    if not categories:
        print("No category ids to inspect", file=sys.stderr)
        return 1

    storage = Storage(args.state)
    state = storage.load()

    documents, dropped, conflicts = dedupe_documents(state.documents)
    new_documents, moves = plan_moves(documents, categories)

    print(f"State: {args.state}")
    print(f"Categories inspected: {', '.join(categories)}")
    print(f"Duplicate rows dropped: {dropped}")
    for cat_id, url in conflicts:
        print(f"  duplicate URL with differing metadata in {cat_id}: {url}")
    print(f"Rows to move: {len(moves)}")
    for source, target, row in moves:
        title = str(row.get("title") or "")[:40]
        print(
            f"  {source} -> {target} ({CATEGORIES.get(target, target)})  "
            f"{row.get('url')}  {title}"
        )

    if not moves and not dropped:
        print("Nothing to repair.")
        return 0

    moved, deduped = apply_moves(new_documents, moves)
    print(f"Rows moved: {moved}, duplicates dropped on move: {deduped}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply to write the changes.")
        return 0

    state.documents = new_documents
    storage.save(state)
    print(f"State written: {args.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
