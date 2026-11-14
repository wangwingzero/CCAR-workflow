#!/usr/bin/env python3
"""Merge CI artifact files into local downloads/ directory."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _files_equal(left: Path, right: Path, chunk_size: int = 1024 * 1024) -> bool:
    """Compare two files by size first, then byte content (not just size)."""
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as lf, right.open("rb") as rf:
        while True:
            left_chunk = lf.read(chunk_size)
            right_chunk = rf.read(chunk_size)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def sync_tree(source: Path, target: Path, dry_run: bool = False) -> tuple[int, int]:
    copied = 0
    skipped = 0
    if not source.exists():
        print(f"[ERROR] Source not found: {source}", file=sys.stderr)
        return copied, skipped

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        dest = target / rel
        if dest.exists() and _files_equal(dest, path):
            skipped += 1
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        copied += 1
    return copied, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync artifact files into downloads/")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("_artifact174"),
        help="Artifact root (default: _artifact174)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("downloads"),
        help="Local downloads root",
    )
    parser.add_argument("--dry-run", action="store_true", help="Count only, do not copy")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    copied, skipped = sync_tree(args.source, args.target, dry_run=args.dry_run)
    action = "Would copy" if args.dry_run else "Copied"
    print(f"{action} {copied} files, skipped {skipped} unchanged")
    return 0 if args.source.exists() else 1


if __name__ == "__main__":
    sys.exit(main())
