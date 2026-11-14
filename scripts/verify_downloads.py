#!/usr/bin/env python3
"""Verify downloads/ files against data/downloads.json index."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PREFIXES = ("regulation/", "normative/", "specification/")
EXTRACT_TIME_RE = re.compile(r"^提取时间:\s*(.+)$", re.MULTILINE)


@dataclass
class VerifyReport:
    indexed_total: int = 0
    on_disk_total: int = 0
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    orphan: list[str] = field(default_factory=list)
    txt_stale_header: list[tuple[str, str]] = field(default_factory=list)
    txt_empty_body: list[str] = field(default_factory=list)
    repair_txt_to_pdf: list[str] = field(default_factory=list)
    repair_still_txt: list[str] = field(default_factory=list)
    by_ext_index: Counter = field(default_factory=Counter)
    by_ext_disk: Counter = field(default_factory=Counter)


def load_index(index_path: Path) -> dict[str, dict]:
    data = json.loads(index_path.read_text(encoding="utf-8"))
    records = data.get("records", data)
    filtered: dict[str, dict] = {}
    for url, rec in records.items():
        if not isinstance(rec, dict):
            continue
        rel = str(rec.get("relative_path", "")).strip()
        if any(rel.startswith(p) for p in PREFIXES):
            filtered[url] = {**rec, "relative_path": rel.replace("\\", "/")}
    return filtered


def scan_disk(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(p) for p in PREFIXES):
            files[rel] = path
    return files


def inspect_txt(path: Path) -> tuple[str | None, int]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, 0
    match = EXTRACT_TIME_RE.search(text)
    extract_time = match.group(1).strip() if match else None
    body = text.split("=" * 50, 1)
    body_chars = len(body[1].strip()) if len(body) > 1 else 0
    return extract_time, body_chars


def verify(
    index_path: Path,
    download_root: Path,
    since: str | None = None,
    repair_only: bool = False,
) -> VerifyReport:
    index = load_index(index_path)
    disk = scan_disk(download_root)
    report = VerifyReport(
        indexed_total=len(index),
        on_disk_total=len(disk),
    )

    indexed_paths = {rec["relative_path"] for rec in index.values()}
    report.matched = len(indexed_paths & set(disk))
    report.missing = sorted(indexed_paths - set(disk))
    report.orphan = sorted(set(disk) - indexed_paths)

    for rec in index.values():
        report.by_ext_index[Path(rec["relative_path"]).suffix.lower()] += 1

    for rel, path in disk.items():
        report.by_ext_disk[Path(rel).suffix.lower()] += 1
        if path.suffix.lower() == ".txt":
            extract_time, body_chars = inspect_txt(path)
            if body_chars < 80:
                report.txt_empty_body.append(rel)
            if since and extract_time and not extract_time.startswith(since):
                report.txt_stale_header.append((rel, extract_time or ""))

    if repair_only or since:
        for url, rec in index.items():
            rel = rec["relative_path"]
            updated = str(rec.get("updated_at", ""))
            if since and not updated.startswith(since):
                continue
            if rel.endswith(".pdf"):
                report.repair_txt_to_pdf.append(rel)
            elif rel.endswith(".txt"):
                report.repair_still_txt.append(rel)

    return report


def print_report(report: VerifyReport, since: str | None) -> None:
    print("=" * 60)
    print("downloads 校验报告")
    print("=" * 60)
    print(f"索引条目 (规章/规范/标准): {report.indexed_total}")
    print(f"磁盘文件:                  {report.on_disk_total}")
    print(f"路径完全匹配:              {report.matched}")
    print(f"索引有、磁盘无:            {len(report.missing)}")
    print(f"磁盘有、索引无 (orphan):   {len(report.orphan)}")
    print()
    print("索引扩展名:", dict(report.by_ext_index))
    print("磁盘扩展名:", dict(report.by_ext_disk))
    print()

    if since:
        print(f"--- Run 当天 ({since}) repair 结果 ---")
        print(f"  仍为 txt: {len(report.repair_still_txt)}")
        print(f"  已变 pdf: {len(report.repair_txt_to_pdf)}")
        for rel in report.repair_txt_to_pdf:
            print(f"    [PDF OK] {rel}")
        if report.repair_still_txt:
            print(f"  txt 样本 (前5):")
            for rel in report.repair_still_txt[:5]:
                print(f"    · {rel}")
        print()

    if report.txt_empty_body:
        print(f"[WARN] 正文过短 txt: {len(report.txt_empty_body)}")
        for rel in report.txt_empty_body[:5]:
            print(f"    {rel}")
        print()

    if report.missing:
        print(f"缺失文件样本 (前10 / 共{len(report.missing)}):")
        for rel in report.missing[:10]:
            print(f"    {rel}")
        print()

    if report.orphan:
        print(f"orphan 样本 (前5 / 共{len(report.orphan)}):")
        for rel in report.orphan[:5]:
            print(f"    {rel}")
        print()

    ok = (
        not report.missing
        and not report.orphan
        and not report.txt_empty_body
    )
    print("总体:", "[OK] 索引与磁盘一致" if ok else "[WARN] 存在差异（见上）")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify downloads against index")
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("data/downloads.json"),
        help="Path to downloads index JSON",
    )
    parser.add_argument(
        "--downloads",
        type=Path,
        default=Path("downloads"),
        help="Root directory containing regulation/normative/specification",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Report repair entries updated on/after this date",
    )
    parser.add_argument(
        "--repair-run",
        action="store_true",
        help="Shortcut: --since today for repair summary",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write machine-readable report JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    since = args.since
    if args.repair_run and not since:
        since = datetime.now().strftime("%Y-%m-%d")

    if not args.index.exists():
        print(f"[ERROR] Index not found: {args.index}", file=sys.stderr)
        return 1

    report = verify(args.index, args.downloads, since=since, repair_only=bool(since))
    print_report(report, since)

    if args.json_out:
        payload = {
            "indexed_total": report.indexed_total,
            "on_disk_total": report.on_disk_total,
            "matched": report.matched,
            "missing_count": len(report.missing),
            "orphan_count": len(report.orphan),
            "repair_txt_to_pdf": report.repair_txt_to_pdf,
            "repair_still_txt_count": len(report.repair_still_txt),
            "since": since,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"JSON 报告: {args.json_out}")

    return (
        0
        if not report.missing
        and not report.orphan
        and not report.txt_empty_body
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
