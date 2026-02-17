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

from .crawler import CaacCrawler, generate_filename, get_download_subdir, CATEGORIES, Document
from .notifier import Notifier
from .r2_uploader import R2Uploader
from .storage import Storage, filter_by_days


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


def _flatten_documents(documents_by_category: dict[str, list[Document]]) -> list[Document]:
    """Flatten grouped documents"""
    result: list[Document] = []
    for docs in documents_by_category.values():
        result.extend(docs)
    return result


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
        help="Send documents from last N days; 0 or unset = incremental (detect new, 30-day cap)",
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
    
    # Parse category IDs
    category_ids = None
    if args.categories:
        category_ids = [c.strip() for c in args.categories.split(",")]
        invalid_ids = [c for c in category_ids if c not in CATEGORIES]
        if invalid_ids:
            logger.error(f"Invalid category IDs: {invalid_ids}. Use --list-categories to see available IDs.")
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
    logger.info("=" * 50)
    
    exit_code = 0
    
    try:
        storage = Storage("data/regulations.json")
        
        with CaacCrawler() as crawler, Notifier() as notifier:
            # 1. Crawl document list from all categories
            logger.info("Step 1/7: Crawling document list...")
            all_documents = crawler.fetch_all_categories(category_ids, args.perpage)

            total_docs = sum(len(docs) for docs in all_documents.values())
            if total_docs == 0:
                logger.error("No documents fetched, may be blocked by anti-crawler")
                return 1

            logger.info(f"Fetch complete: {total_docs} documents from {len(all_documents)} categories")

            # 2. Detect changes or filter by days
            logger.info("Step 2/7: Filtering documents...")
            
            DEFAULT_MAX_DAYS = 30
            download_documents: dict[str, list[Document]] = {}
            
            if args.days:
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
                target_documents = filtered_documents
                download_documents = filtered_documents
            else:
                # Detect new and updated documents
                changes = storage.detect_changes(all_documents)
                
                if not changes.has_changes:
                    logger.info("No new or updated documents detected")
                    if args.notify == 1:
                        logger.info("Force notify enabled, will send notification with empty results")
                        target_documents = {}
                        download_documents = {}
                    else:
                        logger.info("Run complete")
                        if not args.dry_run:
                            storage.update_state(all_documents)
                        return 0
                else:
                    # New documents are notification targets (with 30-day cap)
                    target_documents = {}
                    for cat_id, docs in changes.new_documents.items():
                        filtered = filter_by_days(docs, DEFAULT_MAX_DAYS)
                        if filtered:
                            target_documents[cat_id] = filtered
                    
                    new_original_count = changes.new_count
                    new_filtered_count = sum(len(docs) for docs in target_documents.values())
                    updated_count = changes.updated_count
                    
                    if new_filtered_count < new_original_count:
                        logger.info(
                            f"Detected {new_original_count} new, limited to last {DEFAULT_MAX_DAYS} days: "
                            f"{new_filtered_count}"
                        )
                    else:
                        logger.info(f"Detected {new_filtered_count} new documents")

                    if updated_count > 0:
                        logger.info(f"Detected {updated_count} updated documents (status/title/doc_number etc.)")

                    download_documents = _merge_documents(target_documents, changes.updated_documents)
                    
                    if new_filtered_count == 0 and updated_count == 0:
                        logger.info(f"No new documents in last {DEFAULT_MAX_DAYS} days")
                        if args.notify == 1:
                            logger.info("Force notify enabled, will send notification with empty results")
                            target_documents = {}
                        else:
                            if not args.dry_run:
                                storage.update_state(all_documents)
                            return 0
            
            # 3. Download/rename files (optional)
            downloaded_files: list[str] = []
            if not args.no_download:
                logger.info("Step 3/7: Syncing download files...")
                download_dir = "downloads"
                os.makedirs(download_dir, exist_ok=True)
                docs_to_sync = _flatten_documents(download_documents)
                if not docs_to_sync:
                    logger.info("No files need download/rename")
                else:
                    download_index = storage.load_download_index()
                    synced_count = 0
                    renamed_count = 0
                    failed_count = 0

                    for doc in docs_to_sync:
                        subdir = get_download_subdir(doc.category_id)
                        base_dir = os.path.join(download_dir, subdir)
                        base_name = generate_filename(doc, extension="")
                        save_base_path = os.path.join(base_dir, base_name)

                        record = download_index.get(doc.url, {})
                        old_relative_path = str(record.get("relative_path", "")).strip()
                        old_path = os.path.join(download_dir, old_relative_path) if old_relative_path else ""

                        if old_path and os.path.exists(old_path):
                            ext = os.path.splitext(old_path)[1].lower() or ".pdf"
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

                        if existing_local:
                            download_index[doc.url] = {
                                "relative_path": os.path.relpath(existing_local, download_dir),
                                "updated_at": datetime.now().isoformat(),
                            }
                            synced_count += 1
                            continue

                        saved_path = crawler.download_document_file(doc, save_base_path)
                        if saved_path:
                            download_index[doc.url] = {
                                "relative_path": os.path.relpath(saved_path, download_dir),
                                "updated_at": datetime.now().isoformat(),
                            }
                            synced_count += 1
                            if saved_path.lower().endswith(".pdf"):
                                downloaded_files.append(saved_path)
                        else:
                            failed_count += 1
                            logger.debug(f"Download failed: [{doc.category}] {doc.title}")

                    storage.save_download_index(download_index)
                    logger.info(
                        f"Download sync complete: total={len(docs_to_sync)}, synced={synced_count}, "
                        f"renamed={renamed_count}, failed={failed_count}"
                    )
            else:
                logger.info("Step 3/7: Skipping file download")

            # 4. Upload to R2 (optional)
            r2_url_map: dict[str, str] = {}
            r2 = R2Uploader()
            if r2.enabled and not args.no_download:
                logger.info("Step 4/7: Uploading files to R2...")
                try:
                    download_index = storage.load_download_index()
                    r2_index_path = str(Path(storage.data_path).parent / "r2_uploads.json")
                    r2_url_map = r2.upload_downloads(download_index, "downloads", r2_index_path)
                    logger.info(f"R2 upload complete: {len(r2_url_map)} URLs mapped")
                except Exception as e:
                    logger.warning(f"R2 upload failed (non-fatal): {e}")
            else:
                if not r2.enabled:
                    logger.info("Step 4/7: R2 not configured, skipping upload")
                else:
                    logger.info("Step 4/7: Skipping R2 upload (no-download mode)")

            # 5. Sync JS files for categories 13/14/15
            logger.info("Step 5/7: Syncing JS data files...")
            js_summary = storage.sync_js_files(all_documents, "JS", r2_url_map=r2_url_map or None)
            if js_summary:
                summary_text = ", ".join(f"{name}={count}" for name, count in js_summary.items())
                logger.info(f"JS sync complete: {summary_text}")

            # 6. Send notification
            if not args.no_notify:
                notify_total = sum(len(docs) for docs in target_documents.values())
                if args.notify == 0 and notify_total == 0:
                    logger.info("Step 6/7: Skipping notifications (no new documents)")
                else:
                    logger.info("Step 6/7: Sending notifications...")

                    # Group by category name for notification
                    docs_by_category = {}
                    for cat_id, docs in target_documents.items():
                        cat_name = CATEGORIES.get(cat_id, f"未知分类({cat_id})")
                        docs_by_category[cat_name] = docs

                    title, text_content, html_content = notifier.format_update_message(docs_by_category)

                    results = notifier.send_all(
                        title,
                        text_content,
                        html_content,
                        attachments=downloaded_files if downloaded_files else None,
                    )

                    if results:
                        success_count = sum(1 for v in results.values() if v)
                        failed_count = len(results) - success_count
                        logger.info(f"Notification complete: {success_count}/{len(results)} channels succeeded")

                        if success_count == 0 and failed_count > 0:
                            logger.warning("All notification channels failed")
                            exit_code = 1
            else:
                logger.info("Step 6/7: Skipping notifications")

            # 7. Update state
            if not args.days and not args.dry_run:
                logger.info("Step 7/7: Updating state file...")
                storage.update_state(all_documents)
            
            total_target = sum(len(docs) for docs in target_documents.values())
            logger.info("=" * 50)
            logger.info("CAAC Document Update Monitor - Complete")
            logger.info(f"Notification documents: {total_target}")
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
