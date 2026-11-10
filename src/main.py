#!/usr/bin/env python3
"""
CAAC Document Update Monitor - Main Entry

Monitors all categories under "法定主动公开内容" for new PDF documents.

Flow:
1. Crawl CAAC website document list from all categories
2. Compare with historical state, detect new and updated documents
3. If changes: sync local files + send notification (grouped by category)
4. Update state file
"""

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from loguru import logger

from .crawler import (
    CaacCrawler,
    generate_filename,
    get_download_subdir,
    is_attachment_url,
    CATEGORIES,
    DETAIL_METADATA_CATEGORIES,
    DETAIL_METADATA_FIELDS,
    Document,
)
from .notifier import Notifier
from .r2_uploader import R2Uploader
from .storage import Storage, filter_by_days, JS_EXPORT_CONFIG


def _merge_documents(*documents_by_category: dict[str, list[Document]]) -> dict[str, list[Document]]:
    """Merge category-document mappings by URL"""
    merged: dict[str, list[Document]] = {}
    seen_urls_by_category: dict[str, set[str]] = {}

    for mapping in documents_by_category:
        for cat_id, docs in mapping.items():
            bucket = merged.setdefault(cat_id, [])
            seen = seen_urls_by_category.setdefault(cat_id, set())
            for doc in docs:
                if doc.url in seen:
                    continue
                bucket.append(doc)
                seen.add(doc.url)
    return merged


def _select_download_documents(
    new_documents: dict[str, list[Document]],
    updated_documents: dict[str, list[Document]],
    include_updated: bool = False,
) -> dict[str, list[Document]]:
    """Select documents that need file sync for the incremental run."""
    if include_updated:
        return _merge_documents(new_documents, updated_documents)
    return {cat_id: list(docs) for cat_id, docs in new_documents.items()}


def _find_missing_r2_urls(downloaded_pdf_urls: set[str], r2_url_map: dict[str, str]) -> list[str]:
    """Find downloaded PDF page URLs that did not receive a public R2 URL."""
    return sorted(url for url in downloaded_pdf_urls if url not in r2_url_map)


def _flatten_documents(documents_by_category: dict[str, list[Document]]) -> list[Document]:
    """Flatten grouped documents"""
    result: list[Document] = []
    for docs in documents_by_category.values():
        result.extend(docs)
    return result


def _parse_category_filter(raw: str | None) -> set[str]:
    """Parse a comma-separated category ID list into a set (empty = no filter)"""
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _has_detail_metadata(row: dict) -> bool:
    """True if a stored state row already carries metadata read from a detail page."""
    return any(str(row.get(field_name, "")).strip() for field_name in ("doc_number", "validity", "office_unit"))


def _carry_over_known_metadata(
    documents_by_category: dict[str, list[Document]],
    stored_documents: dict[str, list],
) -> int:
    """Reuse 13/14/15 metadata already recorded for known URLs.

    so-gov search hits never carry 文号/有效性, so without this every known
    document would look "updated" with its metadata wiped, and would be
    re-fetched from its detail page on every run.

    Returns the number of documents that received stored metadata.
    """
    carried_count = 0
    for category_id in DETAIL_METADATA_CATEGORIES:
        stored_row_by_url = {
            str(row.get("url", "")).strip(): row
            for row in stored_documents.get(category_id, [])
            if isinstance(row, dict) and row.get("url")
        }
        for document in documents_by_category.get(category_id, []):
            stored_row = stored_row_by_url.get(document.url)
            if not stored_row or not _has_detail_metadata(stored_row):
                continue
            for field_name in DETAIL_METADATA_FIELDS:
                stored_value = str(stored_row.get(field_name, "")).strip()
                if stored_value:
                    setattr(document, field_name, stored_value)
            carried_count += 1
    return carried_count


def _select_metadata_backfilled_documents(
    updated_documents: dict[str, list[Document]],
    stored_documents: dict[str, list],
) -> dict[str, list[Document]]:
    """Pick updated 13/14/15 documents whose 文号 was only just read from the detail page.

    Their earlier download was saved without 文号, so the file has to be
    re-synced under the {文号}{标题} name even without --download-updated.
    """
    backfilled_documents: dict[str, list[Document]] = {}
    for category_id, documents in updated_documents.items():
        if category_id not in DETAIL_METADATA_CATEGORIES:
            continue
        stored_doc_number_by_url = {
            str(row.get("url", "")).strip(): str(row.get("doc_number", "")).strip()
            for row in stored_documents.get(category_id, [])
            if isinstance(row, dict) and row.get("url")
        }
        newly_numbered = [
            document
            for document in documents
            if document.doc_number and not stored_doc_number_by_url.get(document.url)
        ]
        if newly_numbered:
            backfilled_documents[category_id] = newly_numbered
    return backfilled_documents


def _collect_metadata_repair_targets(
    stored_documents: dict[str, list],
) -> tuple[dict[str, list[Document]], set[str]]:
    """Find 13/14/15 state rows damaged by raw so-gov search hits.

    Returns:
        (detail-page documents still missing 文号/有效性, attachment-only URLs to purge)
    """
    documents_missing_metadata: dict[str, list[Document]] = {}
    attachment_only_urls: set[str] = set()
    for category_id in DETAIL_METADATA_CATEGORIES:
        for row in stored_documents.get(category_id, []):
            if not isinstance(row, dict):
                continue
            document = Document.from_dict(row)
            if is_attachment_url(document.url):
                attachment_only_urls.add(document.url)
            elif CaacCrawler.needs_detail_metadata(document):
                documents_missing_metadata.setdefault(category_id, []).append(document)
    return documents_missing_metadata, attachment_only_urls


def _filter_documents_by_category(
    documents_by_category: dict[str, list[Document]],
    category_ids: set[str],
) -> dict[str, list[Document]]:
    """Keep only the selected categories; an empty set means "keep everything"."""
    if not category_ids:
        return documents_by_category
    return {
        cat_id: docs
        for cat_id, docs in documents_by_category.items()
        if cat_id in category_ids
    }


def _select_notify_attachments(
    downloaded_files: list[str],
    downloaded_files_by_category: dict[str, list[str]],
    category_ids: set[str],
) -> list[str]:
    """Pick email attachments; with a filter only the notified categories count."""
    if not category_ids:
        return downloaded_files
    return [
        path
        for cat_id, paths in downloaded_files_by_category.items()
        if cat_id in category_ids
        for path in paths
    ]


def _load_txt_repair_documents(storage: Storage) -> list[Document]:
    """Load 13/14/15 documents whose download index entry is a .txt fallback."""
    download_index = storage.load_download_index()
    state = storage.load()

    url_to_doc: dict[str, Document] = {}
    for cat_id in JS_EXPORT_CONFIG:
        for item in state.documents.get(cat_id, []):
            if isinstance(item, dict):
                doc = Document.from_dict(item)
            else:
                continue
            if doc.url:
                url_to_doc[doc.url] = doc

    repair_docs: list[Document] = []
    for url, record in download_index.items():
        rel_path = str(record.get("relative_path", "")).strip()
        if not rel_path.lower().endswith(".txt"):
            continue
        doc = url_to_doc.get(url)
        if doc:
            repair_docs.append(doc)

    return repair_docs


def setup_logging():
    """Configure logging"""
    logger.remove()
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
        level="INFO",
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="CAAC Document Update Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    default_days = os.getenv("DAYS")
    if default_days:
        try:
            default_days = int(default_days)
        except ValueError:
            default_days = None
    
    parser.add_argument(
        "--days",
        type=int,
        default=default_days,
        metavar="N",
        help="Send documents from last N days; 0 or unset = incremental (detect new docs by URL)",
    )
    parser.add_argument(
        "--categories",
        type=str,
        default=None,
        metavar="IDS",
        help="Comma-separated category IDs to monitor (default: all). Use --list-categories to see available IDs.",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List all available category IDs and exit",
    )
    parser.add_argument(
        "--notify-categories",
        type=str,
        default=os.getenv("NOTIFY_CATEGORIES"),
        metavar="IDS",
        help=(
            "Comma-separated category IDs to include in notifications "
            "(default: every crawled category). Crawl scope, state file and file "
            "sync are NOT affected — only the notification content/attachments."
        ),
    )
    parser.add_argument(
        "--download-dir",
        type=str,
        default="downloads",
        metavar="DIR",
        help="Base directory for file downloads (default: downloads)",
    )
    parser.add_argument(
        "--cn-dirs",
        action="store_true",
        help="Use Chinese subdirectory names (CCAR规章/规范性文件/标准规范) instead of English",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Skip file download",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip sending notifications",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run, don't update state file",
    )
    parser.add_argument(
        "--notify",
        type=int,
        choices=[0, 1],
        default=1,
        metavar="0|1",
        help="Force send notification: 0=only when new, 1=send even if no new (default)",
    )
    parser.add_argument(
        "--perpage",
        type=int,
        default=50,
        metavar="N",
        help="Number of documents to fetch per category (default: 50)",
    )
    parser.add_argument(
        "--repair-txt",
        action="store_true",
        help="Re-download documents previously saved as .txt fallback (categories 13/14/15)",
    )
    parser.add_argument(
        "--download-updated",
        action="store_true",
        help="Also download documents whose metadata changed; intended for explicit manual repair runs.",
    )
    parser.add_argument(
        "--backfill-r2",
        action="store_true",
        help="Re-upload exported-category (13/14/15) PDFs missing from the R2 index "
             "(e.g. after an upload outage). No notification, state untouched.",
    )
    parser.add_argument(
        "--repair-metadata",
        action="store_true",
        help="One-off repair for 13/14/15: drop attachment-only rows (duplicate *.pdf entries) "
             "and re-read 文号/有效性 for rows missing them, re-syncing files under "
             "{文号}{标题}.pdf. Skips the crawl and never notifies.",
    )
    return parser.parse_args()


def main() -> int:
    """Main function
    
    Returns:
        Exit code: 0 success, 1 failure
    """
    setup_logging()
    args = parse_args()
    
    # List categories and exit
    if args.list_categories:
        print("\nAvailable categories:")
        print("-" * 50)
        for cat_id, cat_name in sorted(CATEGORIES.items(), key=lambda x: int(x[0])):
            print(f"  {cat_id:>3}: {cat_name}")
        print("-" * 50)
        print(f"Total: {len(CATEGORIES)} categories")
        print("\nUsage: --categories 9,13,14,15")
        return 0
    
    if args.days is not None and args.days < 0:
        logger.error("--days must be >= 0")
        return 1
    
    # days=0 means incremental detection mode
    if args.days == 0:
        args.days = None

    # Backfill is a pure R2-repair pass: never notify, and (handled at step 7)
    # never rewrite state. Force --no-notify so a stray --notify can't flood.
    if args.backfill_r2:
        args.no_notify = True
    if args.repair_metadata:
        args.no_notify = True
        args.days = None
    
    # Parse category IDs
    category_ids = None
    if args.categories:
        category_ids = [c.strip() for c in args.categories.split(",")]
        invalid_ids = [c for c in category_ids if c not in CATEGORIES]
        if invalid_ids:
            logger.error(f"Invalid category IDs: {invalid_ids}. Use --list-categories to see available IDs.")
            return 1

    # Parse the notification filter. This only trims what gets emailed; every
    # crawled category is still stored, downloaded and exported as before.
    notify_category_ids = _parse_category_filter(args.notify_categories)
    invalid_notify_ids = sorted(c for c in notify_category_ids if c not in CATEGORIES)
    if invalid_notify_ids:
        logger.error(
            f"Invalid --notify-categories IDs: {invalid_notify_ids}. "
            "Use --list-categories to see available IDs."
        )
        return 1
    
    logger.info("=" * 50)
    logger.info("CAAC Document Update Monitor - Starting")
    if args.days:
        logger.info(f"Mode: Send documents from last {args.days} days")
    else:
        logger.info("Mode: Detect new and updated documents")
    if category_ids:
        logger.info(f"Categories: {', '.join(CATEGORIES[c] for c in category_ids)}")
    else:
        logger.info(f"Categories: All ({len(CATEGORIES)} categories)")
    if notify_category_ids:
        notify_names = ", ".join(
            CATEGORIES.get(c, f"未知分类({c})") for c in sorted(notify_category_ids)
        )
        logger.info(f"Notify only: {notify_names} (all categories are still crawled)")
    if args.download_dir != "downloads":
        logger.info(f"Download dir: {args.download_dir}")
    if args.cn_dirs:
        logger.info("Using Chinese subdirectory names")
    logger.info("=" * 50)
    
    exit_code = 0
    
    try:
        storage = Storage("data/regulations.json")
        
        with CaacCrawler() as crawler, Notifier() as notifier, R2Uploader() as r2:
            # 1. Crawl document list from all categories
            stored_documents = storage.load().documents
            if args.repair_metadata:
                logger.info("Step 1/7: Repair-metadata mode, collecting damaged 13/14/15 rows (no crawl)...")
                all_documents, attachment_only_urls = _collect_metadata_repair_targets(stored_documents)
                repair_total = sum(len(docs) for docs in all_documents.values())
                logger.info(
                    f"Repair-metadata: purging {len(attachment_only_urls)} attachment-only rows, "
                    f"re-reading {repair_total} rows missing 文号/有效性"
                )
                if attachment_only_urls and not args.dry_run:
                    storage.purge_documents(attachment_only_urls)
                    stored_documents = storage.load().documents
                if repair_total == 0:
                    logger.info("Repair-metadata: no rows left to re-read, done")
                    return 0
            else:
                logger.info("Step 1/7: Crawling document list...")
                all_documents = crawler.fetch_all_categories(category_ids, args.perpage)

                total_docs = sum(len(docs) for docs in all_documents.values())
                if total_docs == 0:
                    logger.error("No documents fetched, may be blocked by anti-crawler")
                    return 1

                logger.info(f"Fetch complete: {total_docs} documents from {len(all_documents)} categories")
                carried_count = _carry_over_known_metadata(all_documents, stored_documents)
                if carried_count:
                    logger.info(f"Reused stored 文号/有效性 for {carried_count} known documents")

            # --days filters on publish_date, which the detail page rewrites to
            # the official 发文日期; that branch enriches after filtering instead.
            if not args.days and not args.backfill_r2:
                crawler.enrich_documents_with_detail_metadata(all_documents)

            # 2. Detect changes or filter by days
            logger.info("Step 2/7: Filtering documents...")
            
            download_documents: dict[str, list[Document]] = {}
            
            if args.backfill_r2:
                # Re-upload only exported-category (13/14/15) PDFs missing from
                # the R2 index — repair after an upload outage. CAAC retroactive
                # docs carry old publish_date, so a date filter (--days) would
                # miss them; selecting by "absent from R2 index" catches every gap.
                url_to_doc: dict[str, Document] = {}
                for cat_docs in all_documents.values():
                    for doc in cat_docs:
                        url_to_doc[doc.url] = doc
                dl_index = storage.load_download_index()
                r2_index_path = str(Path(storage.data_path).parent / "r2_uploads.json")
                r2_keys = set(R2Uploader._load_r2_index(r2_index_path).keys())
                backfill_documents: dict[str, list[Document]] = {}
                for url, record in dl_index.items():
                    rel_path = str(record.get("relative_path", "")).strip()
                    if not rel_path.lower().endswith(".pdf") or rel_path in r2_keys:
                        continue
                    doc = url_to_doc.get(url)
                    if doc and doc.category_id in JS_EXPORT_CONFIG:
                        backfill_documents.setdefault(doc.category_id, []).append(doc)
                total_backfill = sum(len(docs) for docs in backfill_documents.values())
                if total_backfill == 0:
                    logger.info("Backfill-R2: no exported-category PDFs missing from R2, nothing to do")
                    return 0
                logger.info(f"Backfill-R2: {total_backfill} exported-category PDFs missing from R2, re-fetching")
                target_documents = {}
                download_documents = backfill_documents
            elif args.days:
                # Filter by days
                filtered_documents = {}
                for cat_id, docs in all_documents.items():
                    filtered = filter_by_days(docs, args.days)
                    if filtered:
                        filtered_documents[cat_id] = filtered
                
                total_filtered = sum(len(docs) for docs in filtered_documents.values())
                if total_filtered == 0:
                    logger.info(f"No documents published in last {args.days} days")
                    return 0
                
                logger.info(f"Last {args.days} days: {total_filtered} documents")
                crawler.enrich_documents_with_detail_metadata(filtered_documents)
                target_documents = filtered_documents
                download_documents = filtered_documents
            else:
                # Detect new and updated documents
                # Scope the "empty prior state" check to the categories crawled
                # this run, so first-time monitoring of a NEW category against an
                # already-populated state still hits the baseline-only guard below
                # instead of flooding with every document in that category.
                stored_docs = stored_documents
                known_total = sum(len(stored_docs.get(cat_id, [])) for cat_id in all_documents)
                changes = storage.detect_changes(all_documents)

                # Drop CAAC re-posts (same document re-published under a new
                # URL) so they never reach state, JS export, or notifications.
                reposted_urls = {
                    doc.url
                    for docs in changes.reposted_documents.values()
                    for doc in docs
                    if doc.url
                }
                if reposted_urls:
                    all_documents = {
                        cat_id: [d for d in docs if d.url not in reposted_urls]
                        for cat_id, docs in all_documents.items()
                    }
                    logger.info(
                        f"Suppressed {changes.reposted_count} reposted "
                        f"documents (re-published under new URLs)"
                    )

                if not changes.has_changes:
                    logger.info("No new or updated documents detected")
                    if args.notify == 1 and not args.no_notify:
                        logger.info("Force notify enabled, will send notification with empty results")
                        target_documents = {}
                        download_documents = {}
                    elif args.repair_txt and not args.no_download:
                        logger.info("Repair-txt enabled, continuing for txt fallback re-download")
                        target_documents = {}
                        download_documents = {}
                    else:
                        logger.info("Run complete")
                        if not args.dry_run:
                            storage.update_state(all_documents)
                        return 0
                else:
                    # New documents are notification targets.
                    # No date filtering here — documents detected as new via URL
                    # comparison are genuinely new to the website, even if their
                    # editorial publish_date is old (CAAC retroactive additions).
                    target_documents = dict(changes.new_documents)

                    new_count = changes.new_count
                    updated_count = changes.updated_count

                    logger.info(f"Detected {new_count} new documents")

                    if updated_count > 0:
                        logger.info(f"Detected {updated_count} updated documents (status/title/doc_number etc.)")

                    download_documents = _select_download_documents(
                        target_documents,
                        changes.updated_documents,
                        include_updated=args.download_updated,
                    )
                    metadata_backfilled_documents = _select_metadata_backfilled_documents(
                        changes.updated_documents, stored_documents
                    )
                    if metadata_backfilled_documents:
                        backfilled_count = sum(len(docs) for docs in metadata_backfilled_documents.values())
                        logger.info(f"Re-syncing {backfilled_count} files whose 文号 was just filled in")
                        download_documents = _merge_documents(download_documents, metadata_backfilled_documents)

                    if known_total == 0:
                        logger.warning(
                            "Empty prior state (first run / recovered from corruption): "
                            "recording baseline only — skipping notification AND bulk download "
                            "to avoid a flood. Use --days N for an explicit initial backfill."
                        )
                        target_documents = {}
                        download_documents = {}

            # 3. Download/rename files (optional)
            downloaded_files: list[str] = []
            downloaded_files_by_category: dict[str, list[str]] = {}
            downloaded_pdf_urls: set[str] = set()
            if args.dry_run:
                logger.info("Step 3/7: Dry-run, skipping file download")
            elif not args.no_download:
                logger.info("Step 3/7: Syncing download files...")
                download_dir = args.download_dir
                os.makedirs(download_dir, exist_ok=True)
                docs_to_sync = _flatten_documents(download_documents)
                repair_urls: set[str] = set()
                if args.repair_txt:
                    repair_docs = _load_txt_repair_documents(storage)
                    repair_urls = {doc.url for doc in repair_docs}
                    seen_urls = {doc.url for doc in docs_to_sync}
                    added = [doc for doc in repair_docs if doc.url not in seen_urls]
                    if added:
                        logger.info(f"Repair-txt: {len(added)} documents queued for PDF re-download")
                        docs_to_sync.extend(added)
                if not docs_to_sync:
                    logger.info("No files need download/rename")
                else:
                    download_index = storage.load_download_index()
                    synced_count = 0
                    renamed_count = 0
                    failed_count = 0
                    repair_pdf_count = 0
                    repair_txt_count = 0
                    repair_fail_count = 0

                    for doc in docs_to_sync:
                        subdir = get_download_subdir(doc.category_id, use_cn=args.cn_dirs)
                        base_dir = os.path.join(download_dir, subdir)
                        base_name = generate_filename(doc, extension="")
                        save_base_path = os.path.join(base_dir, base_name)

                        record = download_index.get(doc.url, {})
                        old_relative_path = str(record.get("relative_path", "")).strip()
                        old_path = os.path.join(download_dir, old_relative_path) if old_relative_path else ""

                        if old_path and os.path.exists(old_path):
                            ext = os.path.splitext(old_path)[1].lower() or ".pdf"
                            if ext != ".txt":
                                new_filename = generate_filename(doc, extension=ext)
                                new_path = os.path.join(base_dir, new_filename)
                                os.makedirs(os.path.dirname(new_path), exist_ok=True)

                                old_norm = os.path.normcase(os.path.normpath(old_path))
                                new_norm = os.path.normcase(os.path.normpath(new_path))
                                final_path = old_path

                                if old_norm != new_norm:
                                    if os.path.exists(new_path):
                                        os.remove(old_path)
                                        final_path = new_path
                                        logger.info(f"Removed stale duplicate file: {old_path}")
                                    else:
                                        os.replace(old_path, new_path)
                                        final_path = new_path
                                    renamed_count += 1
                                    logger.info(f"Renamed file: {final_path}")

                                download_index[doc.url] = {
                                    "relative_path": os.path.relpath(final_path, download_dir),
                                    "updated_at": datetime.now().isoformat(),
                                }
                                synced_count += 1
                                continue

                        existing_local = None
                        for ext in (".pdf", ".doc", ".docx", ".txt"):
                            candidate = f"{save_base_path}{ext}"
                            if os.path.exists(candidate):
                                existing_local = candidate
                                break

                        if existing_local and not existing_local.lower().endswith(".txt"):
                            download_index[doc.url] = {
                                "relative_path": os.path.relpath(existing_local, download_dir),
                                "updated_at": datetime.now().isoformat(),
                            }
                            synced_count += 1
                            continue

                        old_was_txt = (
                            doc.url in repair_urls
                            and old_relative_path.lower().endswith(".txt")
                        )
                        saved_path = crawler.download_document_file(doc, save_base_path)
                        if saved_path:
                            if (
                                old_path
                                and os.path.exists(old_path)
                                and old_path.lower().endswith(".txt")
                                and saved_path.lower().endswith(".pdf")
                            ):
                                os.remove(old_path)
                                logger.info(f"Removed txt fallback after PDF download: {old_path}")
                            download_index[doc.url] = {
                                "relative_path": os.path.relpath(saved_path, download_dir),
                                "updated_at": datetime.now().isoformat(),
                            }
                            synced_count += 1
                            if saved_path.lower().endswith(".pdf"):
                                downloaded_files.append(saved_path)
                                downloaded_files_by_category.setdefault(
                                    doc.category_id, []
                                ).append(saved_path)
                                downloaded_pdf_urls.add(doc.url)
                            if old_was_txt:
                                if saved_path.lower().endswith(".pdf"):
                                    repair_pdf_count += 1
                                elif saved_path.lower().endswith(".txt"):
                                    repair_txt_count += 1
                        else:
                            failed_count += 1
                            if old_was_txt:
                                repair_fail_count += 1
                            logger.debug(f"Download failed: [{doc.category}] {doc.title}")

                    storage.save_download_index(download_index)
                    logger.info(
                        f"Download sync complete: total={len(docs_to_sync)}, synced={synced_count}, "
                        f"renamed={renamed_count}, failed={failed_count}"
                    )
                    if args.repair_txt and repair_urls:
                        logger.info(
                            "Repair-txt summary: "
                            f"queued={len(repair_urls)}, pdf_ok={repair_pdf_count}, "
                            f"still_txt={repair_txt_count}, failed={repair_fail_count}"
                        )
            else:
                logger.info("Step 3/7: Skipping file download")

            # 4. Upload to R2 (optional)
            r2_url_map: dict[str, str] = {}
            if args.dry_run:
                logger.info("Step 4/7: Dry-run, skipping R2 upload")
            elif r2.enabled and not args.no_download:
                logger.info("Step 4/7: Uploading files to R2...")
                try:
                    download_index = storage.load_download_index()
                    r2_index_path = str(Path(storage.data_path).parent / "r2_uploads.json")
                    r2_url_map = r2.upload_downloads(download_index, args.download_dir, r2_index_path)
                    missing_r2_urls = _find_missing_r2_urls(downloaded_pdf_urls, r2_url_map)
                    if missing_r2_urls:
                        raise RuntimeError(
                            "R2 upload missing public URLs for newly downloaded PDFs: "
                            f"{len(missing_r2_urls)}"
                        )
                    logger.info(f"R2 upload complete: {len(r2_url_map)} URLs mapped")
                except Exception as e:
                    logger.error(f"R2 upload failed: {e}")
                    raise
            else:
                if not r2.enabled:
                    logger.info("Step 4/7: R2 not configured, skipping upload")
                else:
                    logger.info("Step 4/7: Skipping R2 upload (no-download mode)")

            # 5. Sync JS files for categories 13/14/15
            if args.dry_run:
                logger.info("Step 5/7: Dry-run, skipping JS data sync")
            else:
                logger.info("Step 5/7: Syncing JS data files...")
                js_summary = storage.sync_js_files(all_documents, "JS", r2_url_map=r2_url_map or None)
                if js_summary:
                    summary_text = ", ".join(f"{name}={count}" for name, count in js_summary.items())
                    logger.info(f"JS sync complete: {summary_text}")

            # 5b. Upload JSON data to R2 for mini program hot-update
            if r2.enabled and not args.dry_run:
                logger.info("Step 5b: Uploading JSON data to R2...")
                json_uploaded = 0
                for cat_id, config in JS_EXPORT_CONFIG.items():
                    json_filename = config["filename"].replace(".js", ".json")
                    json_local = os.path.join("JS", json_filename)
                    if os.path.exists(json_local):
                        r2_key = f"data/v1/{json_filename}"
                        url = r2.upload_file(json_local, r2_key)
                        if url:
                            json_uploaded += 1
                            logger.info(f"JSON uploaded to R2: {r2_key}")
                        else:
                            logger.warning(f"JSON upload failed: {r2_key}")
                logger.info(f"JSON data upload complete: {json_uploaded} files")

            # 6. Send notification
            # --notify-categories trims the digest only; crawling, state and R2
            # exports above already ran for every category.
            notify_target_documents = _filter_documents_by_category(
                target_documents, notify_category_ids
            )
            if args.dry_run:
                logger.info("Step 6/7: Dry-run, skipping notifications")
            elif not args.no_notify:
                notify_total = sum(len(docs) for docs in notify_target_documents.values())
                # --notify 1 means a daily heartbeat email even with nothing new,
                # including when the only changes were outside --notify-categories.
                if args.notify == 0 and notify_total == 0:
                    logger.info("Step 6/7: Skipping notifications (no new documents)")
                else:
                    logger.info("Step 6/7: Sending notifications...")

                    # Group by category name for notification. An empty mapping
                    # renders as the "暂无更新" heartbeat email.
                    docs_by_category = {}
                    for cat_id, docs in notify_target_documents.items():
                        cat_name = CATEGORIES.get(cat_id, f"未知分类({cat_id})")
                        docs_by_category[cat_name] = docs

                    title, text_content, html_content = notifier.format_update_message(docs_by_category)

                    notify_attachments = _select_notify_attachments(
                        downloaded_files,
                        downloaded_files_by_category,
                        notify_category_ids,
                    )

                    results = notifier.send_all(
                        title,
                        text_content,
                        html_content,
                        attachments=notify_attachments[:10] if notify_attachments else None,
                    )

                    if results:
                        success_count = sum(1 for v in results.values() if v)
                        failed_count = len(results) - success_count
                        logger.info(f"Notification complete: {success_count}/{len(results)} channels succeeded")

                        if success_count == 0 and failed_count > 0:
                            logger.warning("All notification channels failed (non-fatal)")
            else:
                logger.info("Step 6/7: Skipping notifications")

            # 7. Update state
            if not args.days and not args.backfill_r2 and not args.dry_run:
                logger.info("Step 7/7: Updating state file...")
                storage.update_state(all_documents)
            
            total_target = sum(len(docs) for docs in notify_target_documents.values())
            logger.info("=" * 50)
            logger.info("CAAC Document Update Monitor - Complete" + (" (DRY RUN)" if args.dry_run else ""))
            logger.info(f"Notification documents: {total_target}")
            if not args.dry_run:
                logger.info(f"PDF files synced: {len(downloaded_files)}")
            logger.info("=" * 50)
    
    except KeyboardInterrupt:
        logger.warning("User interrupted")
        exit_code = 130
    except Exception as e:
        logger.error(f"Error: {e}")
        logger.error(traceback.format_exc())
        exit_code = 1
    
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
