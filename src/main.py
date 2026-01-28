#!/usr/bin/env python3
"""
CAAC Regulation Update Monitor - Main Entry

Flow:
1. Crawl CAAC website regulation list
2. Compare with historical state, detect changes
3. If changes: download PDF + send notification
4. Update state file
"""

import argparse
import os
import sys
import traceback

from loguru import logger

from .crawler import CaacCrawler, generate_filename
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
        description="CAAC Regulation Update Monitor",
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
        help="Only send regulations from last N days (default: detect new with 30-day limit, use --days to override, can set via DAYS env var)",
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
    return parser.parse_args()


def main() -> int:
    """Main function
    
    Returns:
        Exit code: 0 success, 1 failure
    """
    setup_logging()
    args = parse_args()
    
    if args.days is not None and args.days <= 0:
        logger.error("--days must be greater than 0")
        return 1
    
    logger.info("=" * 50)
    logger.info("CAAC Regulation Update Monitor - Starting")
    if args.days:
        logger.info(f"Mode: Send regulations from last {args.days} days")
    else:
        logger.info("Mode: Detect new regulations")
    logger.info("=" * 50)
    
    exit_code = 0
    
    try:
        storage = Storage("data/regulations.json")
        
        with CaacCrawler() as crawler, Notifier() as notifier:
            # 1. Crawl latest regulation list
            logger.info("Step 1/4: Crawling regulation list...")
            regulations = crawler.fetch_regulations()
            normatives = crawler.fetch_normatives()
            
            if not regulations and not normatives:
                logger.error("No regulation data fetched, may be blocked by anti-crawler")
                return 1
            
            logger.info(f"Fetch complete: {len(regulations)} regulations, {len(normatives)} normative documents")
            
            # 2. Detect changes or filter by days
            logger.info("Step 2/4: Filtering regulations...")
            
            # Default max days limit (unless user explicitly specifies --days)
            DEFAULT_MAX_DAYS = 30
            
            if args.days:
                # User explicitly specified --days, use their value
                filtered_regulations = filter_by_days(regulations, args.days)
                filtered_normatives = filter_by_days(normatives, args.days)
                
                if not filtered_regulations and not filtered_normatives:
                    logger.info(f"No regulations published in last {args.days} days")
                    return 0
                
                logger.info(f"Last {args.days} days: {len(filtered_regulations)} regulations, {len(filtered_normatives)} normatives")
                target_regulations = filtered_regulations
                target_normatives = filtered_normatives
            else:
                # Default mode: detect new, but limit to last 30 days max
                changes = storage.detect_changes(regulations, normatives)
                
                if not changes.has_changes:
                    logger.info("No new regulations detected, run complete")
                    if not args.dry_run:
                        storage.update_state(regulations, normatives)
                    return 0
                
                # Apply 30-day limit to prevent email flood on first run
                target_regulations = filter_by_days(changes.new_regulations, DEFAULT_MAX_DAYS)
                target_normatives = filter_by_days(changes.new_normatives, DEFAULT_MAX_DAYS)
                
                original_count = changes.total_count
                filtered_count = len(target_regulations) + len(target_normatives)
                
                if filtered_count < original_count:
                    logger.info(f"Detected {original_count} new, limited to last {DEFAULT_MAX_DAYS} days: {filtered_count}")
                else:
                    logger.info(f"Detected {filtered_count} new regulations/normatives")
                
                if filtered_count == 0:
                    logger.info(f"No new regulations in last {DEFAULT_MAX_DAYS} days")
                    if not args.dry_run:
                        storage.update_state(regulations, normatives)
                    return 0
            
            # 3. Download PDF (optional)
            downloaded_files: list[str] = []
            if not args.no_download:
                logger.info("Step 3/4: Downloading PDFs...")
                download_dir = "downloads"
                os.makedirs(download_dir, exist_ok=True)
                
                downloaded_count = 0
                for doc in target_regulations + target_normatives:
                    filename = generate_filename(doc)
                    save_path = os.path.join(download_dir, filename)
                    
                    if crawler.download_pdf(doc, save_path):
                        downloaded_count += 1
                        downloaded_files.append(save_path)
                    else:
                        logger.warning(f"Download failed: {doc.doc_number} {doc.title}")
                
                logger.info(f"Download complete: {downloaded_count}/{len(target_regulations) + len(target_normatives)} files")
            else:
                logger.info("Step 3/4: Skipping PDF download")
            
            # 4. Send notification
            if not args.no_notify:
                logger.info("Step 4/4: Sending notifications...")
                title, text_content, html_content = notifier.format_update_message(
                    target_regulations,
                    target_normatives,
                )
                
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
                logger.info("Step 4/4: Skipping notifications")
            
            # 5. Update state (only in non-days mode and non-dry-run)
            if not args.days and not args.dry_run:
                storage.update_state(regulations, normatives)
            
            logger.info("=" * 50)
            logger.info("CAAC Regulation Update Monitor - Complete")
            logger.info(f"Regulations: {len(target_regulations)}")
            logger.info(f"Normatives: {len(target_normatives)}")
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
