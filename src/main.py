#!/usr/bin/env python3
"""
CAAC Document Update Monitor - Main Entry

Monitors all categories under "法定主动公开内容" for new PDF documents.

Flow:
1. Crawl CAAC website document list from all categories
2. Compare with historical state, detect changes
3. If changes: download PDF + send notification (grouped by category)
4. Update state file
"""

import argparse
import os
import sys
import traceback

from loguru import logger

from .crawler import CaacCrawler, generate_filename, CATEGORIES, Document
from .notifier import Notifier
from .storage import Storage, filter_by_days


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
        help="Only send documents from last N days (default: detect new with 30-day limit)",
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
        help="Skip PDF download",
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
        default=0,
        metavar="0|1",
        help="Force send notification: 0=normal (default), 1=force send even if no new documents",
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
        logger.info("Mode: Detect new documents")
    if category_ids:
        logger.info(f"Categories: {', '.join(CATEGORIES[c] for c in category_ids)}")
    else:
        logger.info(f"Categories: All ({len(CATEGORIES)} categories)")
    logger.info("=" * 50)
    
    exit_code = 0
    
    try:
        storage = Storage("data/documents.json")
        
        with CaacCrawler() as crawler, Notifier() as notifier:
            # 1. Crawl document list from all categories
            logger.info("Step 1/5: Crawling document list...")
            all_documents = crawler.fetch_all_categories(category_ids, args.perpage)
            
            total_docs = sum(len(docs) for docs in all_documents.values())
            if total_docs == 0:
                logger.error("No documents fetched, may be blocked by anti-crawler")
                return 1
            
            logger.info(f"Fetch complete: {total_docs} documents from {len(all_documents)} categories")
            
            # 2. Detect changes or filter by days
            logger.info("Step 2/5: Filtering documents...")
            
            DEFAULT_MAX_DAYS = 30
            
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
            else:
                # Detect new documents
                changes = storage.detect_changes(all_documents)
                
                if not changes.has_changes:
                    logger.info("No new documents detected")
                    if args.notify == 1:
                        logger.info("Force notify enabled, will send notification with empty results")
                        target_documents = {}
                    else:
                        logger.info("Run complete")
                        if not args.dry_run:
                            storage.update_state(all_documents)
                        return 0
                else:
                    # Apply 30-day limit
                    target_documents = {}
                    for cat_id, docs in changes.new_documents.items():
                        filtered = filter_by_days(docs, DEFAULT_MAX_DAYS)
                        if filtered:
                            target_documents[cat_id] = filtered
                    
                    original_count = changes.total_count
                    filtered_count = sum(len(docs) for docs in target_documents.values())
                    
                    if filtered_count < original_count:
                        logger.info(f"Detected {original_count} new, limited to last {DEFAULT_MAX_DAYS} days: {filtered_count}")
                    else:
                        logger.info(f"Detected {filtered_count} new documents")
                    
                    if filtered_count == 0:
                        logger.info(f"No new documents in last {DEFAULT_MAX_DAYS} days")
                        if args.notify == 1:
                            logger.info("Force notify enabled, will send notification with empty results")
                            target_documents = {}
                        else:
                            if not args.dry_run:
                                storage.update_state(all_documents)
                            return 0
            
            # 3. Download PDFs (optional)
            downloaded_files: list[str] = []
            if not args.no_download:
                logger.info("Step 3/5: Downloading PDFs...")
                download_dir = "downloads"
                os.makedirs(download_dir, exist_ok=True)
                
                all_target_docs = []
                for docs in target_documents.values():
                    all_target_docs.extend(docs)
                
                downloaded_count = 0
                for doc in all_target_docs:
                    filename = generate_filename(doc)
                    save_path = os.path.join(download_dir, filename)
                    
                    if crawler.check_pdf_and_download(doc, save_path):
                        downloaded_count += 1
                        downloaded_files.append(save_path)
                    else:
                        logger.debug(f"No PDF or download failed: [{doc.category}] {doc.title}")
                
                logger.info(f"Download complete: {downloaded_count}/{len(all_target_docs)} files with PDF")
            else:
                logger.info("Step 3/5: Skipping PDF download")
            
            # 4. Send notification
            if not args.no_notify:
                logger.info("Step 4/5: Sending notifications...")
                
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
                logger.info("Step 4/5: Skipping notifications")
            
            # 5. Update state
            if not args.days and not args.dry_run:
                storage.update_state(all_documents)
            
            total_target = sum(len(docs) for docs in target_documents.values())
            logger.info("=" * 50)
            logger.info("CAAC Document Update Monitor - Complete")
            logger.info(f"New documents: {total_target}")
            logger.info(f"PDFs downloaded: {len(downloaded_files)}")
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
