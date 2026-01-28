#!/usr/bin/env python3
"""
CAAC 规章更新监控 - 主入口

流程:
1. 爬取 CAAC 官网规章列表
2. 对比历史状态，检测变更
3. 如有变更：下载 PDF + 发送通知
4. 更新状态文件
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
    """配置日志"""
    logger.remove()
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
        level="INFO",
    )


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="CAAC 规章更新监控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="只发送最近 N 天发布的规章（默认：检测新增，N 必须 > 0）",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="跳过 PDF 下载",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="跳过发送通知",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行，不更新状态文件",
    )
    return parser.parse_args()


def main() -> int:
    """主函数
    
    Returns:
        退出码：0 成功，1 失败
    """
    setup_logging()
    args = parse_args()
    
    # 参数验证
    if args.days is not None and args.days <= 0:
        logger.error("--days 参数必须大于 0")
        return 1
    
    logger.info("=" * 50)
    logger.info("CAAC 规章更新监控 - 开始运行")
    if args.days:
        logger.info(f"模式: 发送最近 {args.days} 天的规章")
    else:
        logger.info("模式: 检测新增规章")
    logger.info("=" * 50)
    
    exit_code = 0
    
    try:
        # 初始化组件
        storage = Storage("data/regulations.json")
        
        with CaacCrawler() as crawler, Notifier() as notifier:
            # 1. 爬取最新规章列表
            logger.info("步骤 1/4: 爬取规章列表...")
            regulations = crawler.fetch_regulations()
            normatives = crawler.fetch_normatives()
            
            if not regulations and not normatives:
                logger.error("未获取到任何规章数据，可能被反爬拦截")
                return 1
            
            logger.info(f"获取完成: {len(regulations)} 条规章, {len(normatives)} 条规范性文件")
            
            # 2. 检测变更或按天数过滤
            logger.info("步骤 2/4: 筛选规章...")
            
            if args.days:
                # 按天数过滤模式
                filtered_regulations = filter_by_days(regulations, args.days)
                filtered_normatives = filter_by_days(normatives, args.days)
                
                if not filtered_regulations and not filtered_normatives:
                    logger.info(f"最近 {args.days} 天没有发布新规章")
                    return 0
                
                logger.info(f"最近 {args.days} 天: {len(filtered_regulations)} 条规章, {len(filtered_normatives)} 条规范性文件")
                target_regulations = filtered_regulations
                target_normatives = filtered_normatives
            else:
                # 检测新增模式
                changes = storage.detect_changes(regulations, normatives)
                
                if not changes.has_changes:
                    logger.info("未检测到新增规章，本次运行结束")
                    if not args.dry_run:
                        storage.update_state(regulations, normatives)
                    return 0
                
                logger.info(f"检测到 {changes.total_count} 条新增规章/规范性文件")
                target_regulations = changes.new_regulations
                target_normatives = changes.new_normatives
            
            # 3. 下载 PDF（可选）
            if not args.no_download:
                logger.info("步骤 3/4: 下载 PDF...")
                download_dir = "downloads"
                os.makedirs(download_dir, exist_ok=True)
                
                downloaded_count = 0
                for doc in target_regulations + target_normatives:
                    filename = generate_filename(doc)
                    save_path = os.path.join(download_dir, filename)
                    
                    if crawler.download_pdf(doc, save_path):
                        downloaded_count += 1
                    else:
                        logger.warning(f"下载失败: {doc.doc_number} {doc.title}")
                
                logger.info(f"下载完成: {downloaded_count}/{len(target_regulations) + len(target_normatives)} 个文件")
            else:
                logger.info("步骤 3/4: 跳过 PDF 下载")
            
            # 4. 发送通知
            if not args.no_notify:
                logger.info("步骤 4/4: 发送通知...")
                title, text_content, html_content = notifier.format_update_message(
                    target_regulations,
                    target_normatives,
                )
                
                results = notifier.send_all(title, text_content, html_content)
                
                if results:
                    success_count = sum(1 for v in results.values() if v)
                    failed_count = len(results) - success_count
                    logger.info(f"通知发送完成: {success_count}/{len(results)} 个渠道成功")
                    
                    if success_count == 0 and failed_count > 0:
                        logger.warning("所有通知渠道都失败了")
                        exit_code = 1
            else:
                logger.info("步骤 4/4: 跳过发送通知")
            
            # 5. 更新状态（仅在非 --days 模式且非 dry-run 时）
            if not args.days and not args.dry_run:
                storage.update_state(regulations, normatives)
            
            logger.info("=" * 50)
            logger.info("CAAC 规章更新监控 - 运行完成")
            logger.info(f"规章: {len(target_regulations)} 条")
            logger.info(f"规范性文件: {len(target_normatives)} 条")
            logger.info("=" * 50)
    
    except KeyboardInterrupt:
        logger.warning("用户中断运行")
        exit_code = 130
    except Exception as e:
        logger.error(f"运行出错: {e}")
        logger.error(traceback.format_exc())
        exit_code = 1
    
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
