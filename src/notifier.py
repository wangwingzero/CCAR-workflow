#!/usr/bin/env python3
"""
Notification Module

Supports multiple push channels: Email, PushPlus, Telegram.
"""

import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Literal, Optional

import httpx
from loguru import logger

from .crawler import Document, generate_filename, CATEGORIES


class Notifier:
    """Notification manager"""

    def __init__(self):
        # Email config
        self.email_user = os.getenv("EMAIL_USER")
        self.email_pass = os.getenv("EMAIL_PASS")
        self.email_to = os.getenv("EMAIL_TO") or self.email_user
        self.email_sender = os.getenv("EMAIL_SENDER", "CAAC 文件监控")
        
        # PushPlus config
        self.pushplus_token = os.getenv("PUSHPLUS_TOKEN")
        
        # Telegram config
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        """Get HTTP client (lazy loading)"""
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def close(self):
        """Close resources"""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def send_all(
        self,
        title: str,
        content: str,
        html_content: Optional[str] = None,
        attachments: Optional[list[str]] = None,
    ) -> dict[str, bool]:
        """Send notification to all configured channels"""
        results: dict[str, bool] = {}
        
        # Email
        if self.email_user and self.email_pass and self.email_to:
            try:
                self._send_email(
                    title, 
                    html_content or content, 
                    "html" if html_content else "text",
                    attachments=attachments,
                )
                logger.success(f"[Email] Push succeeded -> {self.email_to}")
                results["Email"] = True
            except Exception as e:
                logger.error(f"[Email] Push failed: {e}")
                results["Email"] = False
        
        # PushPlus
        if self.pushplus_token:
            try:
                self._send_pushplus(title, html_content or content, "html" if html_content else "text")
                logger.success("[PushPlus] Push succeeded")
                results["PushPlus"] = True
            except Exception as e:
                logger.error(f"[PushPlus] Push failed: {e}")
                results["PushPlus"] = False
        
        # Telegram
        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                self._send_telegram(title, content)
                logger.success("[Telegram] Push succeeded")
                results["Telegram"] = True
            except Exception as e:
                logger.error(f"[Telegram] Push failed: {e}")
                results["Telegram"] = False
        
        if not results:
            logger.warning("No notification channels configured")
        
        return results

    def _send_email(
        self,
        title: str,
        content: str,
        msg_type: Literal["text", "html"] = "text",
        attachments: Optional[list[str]] = None,
    ):
        """Send email notification"""
        if not self.email_user or not self.email_pass or not self.email_to:
            raise ValueError("Email configuration incomplete")
        
        msg = MIMEMultipart("mixed")
        
        body_part = MIMEMultipart("alternative")
        if msg_type == "html":
            body_part.attach(MIMEText("请使用支持 HTML 的邮件客户端查看此邮件。", "plain", "utf-8"))
            body_part.attach(MIMEText(content, "html", "utf-8"))
        else:
            body_part.attach(MIMEText(content, "plain", "utf-8"))
        msg.attach(body_part)
        
        if attachments:
            for file_path in attachments:
                path = Path(file_path)
                if not path.exists():
                    logger.warning(f"Attachment not found, skipping: {file_path}")
                    continue
                
                try:
                    with open(path, "rb") as f:
                        attachment = MIMEApplication(f.read(), _subtype="pdf")
                    
                    filename = path.name
                    attachment.add_header(
                        "Content-Disposition",
                        "attachment",
                        filename=("utf-8", "", filename),
                    )
                    msg.attach(attachment)
                    logger.info(f"Added attachment: {filename}")
                except Exception as e:
                    logger.warning(f"Failed to add attachment {file_path}: {e}")
        
        msg["From"] = formataddr((Header(self.email_sender, "utf-8").encode(), self.email_user))
        msg["To"] = self.email_to
        msg["Subject"] = Header(title, "utf-8")
        
        domain = self.email_user.split("@")[1]
        smtp_server = f"smtp.{domain}"
        
        with smtplib.SMTP_SSL(smtp_server, 465) as server:
            server.login(self.email_user, self.email_pass)
            server.sendmail(self.email_user, [self.email_to], msg.as_string())

    def _send_pushplus(
        self,
        title: str,
        content: str,
        msg_type: Literal["text", "html"] = "text",
    ):
        """Send PushPlus notification"""
        if not self.pushplus_token:
            raise ValueError("PushPlus configuration incomplete")
        
        url = "https://www.pushplus.plus/send"
        payload = {
            "token": self.pushplus_token,
            "title": title,
            "content": content,
            "template": "html" if msg_type == "html" else "txt",
        }
        
        response = self.client.post(url, json=payload)
        response.raise_for_status()

    def _send_telegram(self, title: str, content: str):
        """Send Telegram notification"""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ValueError("Telegram configuration incomplete")
        
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        
        def escape_markdown(text: str) -> str:
            """Escape Markdown special characters"""
            special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
            for char in special_chars:
                text = text.replace(char, f'\\{char}')
            return text
        
        escaped_title = escape_markdown(title)
        escaped_content = escape_markdown(content)
        text = f"*{escaped_title}*\n\n{escaped_content}"
        
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
        }
        
        response = self.client.post(url, json=payload)
        response.raise_for_status()

    def format_update_message(
        self,
        documents_by_category: dict,
    ) -> tuple[str, str, str]:
        """Format update notification message
        
        Args:
            documents_by_category: Dict mapping category name to list of documents
        
        Returns:
            (title, plain text content, HTML content)
        """
        total = sum(len(docs) for docs in documents_by_category.values())
        beijing_tz = timezone(timedelta(hours=8))
        timestamp = datetime.now(beijing_tz)
        
        # Title
        title = f"📋 CAAC 文件更新通知 ({total} 条)"
        
        # Plain text content
        lines = [
            f"检测时间: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            f"新增文件: {total} 条",
            "",
        ]
        
        for cat_name, docs in documents_by_category.items():
            if not docs:
                continue
            lines.append(f"【{cat_name}】({len(docs)} 条)")
            for doc in docs:
                lines.append(f"  • {doc.doc_number} {doc.title}" if doc.doc_number else f"  • {doc.title}")
                details = []
                if doc.validity:
                    details.append(f"状态: {doc.validity}")
                if doc.publish_date:
                    details.append(f"发布: {doc.publish_date}")
                if doc.office_unit:
                    details.append(f"单位: {doc.office_unit}")
                if details:
                    lines.append(f"    {' | '.join(details)}")
                lines.append(f"    详情: {doc.url}")
            lines.append("")
        
        text_content = "\n".join(lines)
        html_content = self._generate_html_email(documents_by_category, timestamp)
        
        return title, text_content, html_content

    def _generate_html_email(
        self,
        documents_by_category: dict,
        timestamp: datetime,
    ) -> str:
        """Generate HTML email content - Apple style clean design"""
        total = sum(len(docs) for docs in documents_by_category.values())
        
        if total > 0:
            status_icon = "✓"
            status_bg = "#34C759"
            status_text = "检测完成"
        else:
            status_icon = "−"
            status_bg = "#86868B"
            status_text = "暂无更新"
        
        # Category colors
        category_colors = {
            "通知公告": "#007AFF",
            "政策发布": "#FF9500",
            "政策解读": "#5856D6",
            "统计数据": "#00C7BE",
            "法律法规": "#FF2D55",
            "民航规章": "#007AFF",
            "规范性文件": "#FF9500",
            "标准规范": "#5856D6",
            "对外关系": "#34C759",
            "港澳台合作": "#FF3B30",
            "国际公约": "#AF52DE",
            "人事信息": "#FF9500",
            "财政信息": "#00C7BE",
            "发展规划": "#007AFF",
            "重大项目": "#FF2D55",
            "行政权力": "#5856D6",
            "政府公文": "#34C759",
            "机构职能": "#FF9500",
            "对外政策": "#007AFF",
            "执法典型案例": "#FF3B30",
            "建议提案答复": "#AF52DE",
            "政府网站年度报表": "#00C7BE",
        }
        
        # Category icons
        category_icons = {
            "通知公告": "📢",
            "政策发布": "📜",
            "政策解读": "📖",
            "统计数据": "📊",
            "法律法规": "⚖️",
            "民航规章": "✈️",
            "规范性文件": "📋",
            "标准规范": "📐",
            "对外关系": "🌍",
            "港澳台合作": "🤝",
            "国际公约": "🌐",
            "人事信息": "👤",
            "财政信息": "💰",
            "发展规划": "📈",
            "重大项目": "🏗️",
            "行政权力": "🏛️",
            "政府公文": "📄",
            "机构职能": "🏢",
            "对外政策": "🌏",
            "执法典型案例": "⚖️",
            "建议提案答复": "💬",
            "政府网站年度报表": "📑",
        }
        
        def render_doc_item(doc: Document, index: int) -> str:
            """Render single document item"""
            if doc.validity == "有效":
                validity_color = "#34C759"
                validity_icon = "✓"
            elif doc.validity in ("失效", "废止"):
                validity_color = "#FF3B30"
                validity_icon = "✗"
            else:
                validity_color = "#86868B"
                validity_icon = "−"
            
            details = []
            if doc.publish_date:
                details.append(f"📅 {doc.publish_date}")
            if doc.office_unit:
                details.append(f"🏢 {doc.office_unit}")
            details_html = " · ".join(details) if details else ""
            
            doc_title = f"{doc.doc_number} {doc.title}" if doc.doc_number else doc.title
            
            separator = '<div style="height: 1px; background: #E5E5EA; margin: 16px 0;"></div>' if index > 0 else ""
            
            validity_badge = ""
            if doc.validity:
                validity_badge = f'''
                    <div style="width: 20px; height: 20px; background: {validity_color}; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; margin-left: 8px; flex-shrink: 0;">
                        <span style="color: white; font-size: 12px;">{validity_icon}</span>
                    </div>'''
            
            return f'''{separator}
                <div style="padding: 4px 0;">
                    <div style="display: flex; align-items: flex-start; margin-bottom: 6px;">
                        <a href="{doc.url}" style="font-size: 14px; font-weight: 500; color: #1D1D1F; text-decoration: none; line-height: 1.4; flex: 1;">{doc_title}</a>
                        {validity_badge}
                    </div>
                    <div style="font-size: 12px; color: #86868B;">{details_html}</div>
                </div>'''
        
        # Build category cards
        category_cards = ""
        for cat_name, docs in documents_by_category.items():
            if not docs:
                continue
            
            color = category_colors.get(cat_name, "#007AFF")
            icon = category_icons.get(cat_name, "📄")
            
            items_html = ""
            for i, doc in enumerate(docs):
                items_html += render_doc_item(doc, i)
            
            category_cards += f'''
    <!-- {cat_name} Card -->
    <div style="background: #FFFFFF; border-radius: 16px; padding: 20px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
        <div style="display: flex; align-items: center; margin-bottom: 16px;">
            <span style="font-size: 20px; margin-right: 10px;">{icon}</span>
            <span style="font-size: 15px; font-weight: 600; color: #1D1D1F;">{cat_name}</span>
            <span style="background: {color}; color: white; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; margin-left: 8px;">{len(docs)}</span>
        </div>
        {items_html}
    </div>'''
        
        # Build statistics
        stats_items = ""
        for cat_name, docs in documents_by_category.items():
            if not docs:
                continue
            color = category_colors.get(cat_name, "#007AFF")
            stats_items += f'''
            <div style="text-align: center; padding: 0 8px;">
                <div style="font-size: 24px; font-weight: 600; color: {color};">{len(docs)}</div>
                <div style="font-size: 11px; color: #86868B; margin-top: 2px;">{cat_name}</div>
            </div>'''
        
        html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; background-color: #F5F5F7;">
<div style="font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 32px 16px; background-color: #F5F5F7;">
    
    <!-- Status Indicator -->
    <div style="text-align: center; margin-bottom: 24px;">
        <div style="display: inline-block; width: 56px; height: 56px; background: {status_bg}; border-radius: 50%; line-height: 56px; margin-bottom: 12px;">
            <span style="color: white; font-size: 28px; font-weight: 300;">{status_icon}</span>
        </div>
        <h1 style="margin: 0; font-size: 24px; font-weight: 600; color: #1D1D1F;">{status_text}</h1>
        <p style="margin: 6px 0 0 0; font-size: 13px; color: #86868B;">{timestamp.strftime('%Y年%m月%d日 %H:%M')}</p>
    </div>
    
    <!-- Statistics Card -->
    <div style="background: #FFFFFF; border-radius: 16px; padding: 20px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
        <div style="display: flex; justify-content: center; flex-wrap: wrap; gap: 16px;">
            {stats_items}
        </div>
    </div>
    
    {category_cards}
    
    <!-- Footer -->
    <div style="text-align: center; padding: 16px 0;">
        <p style="font-size: 11px; color: #AEAEB2; margin: 0;">CAAC 文件监控系统 · 自动发送</p>
    </div>
    
</div>
</body>
</html>'''
        
        return html
