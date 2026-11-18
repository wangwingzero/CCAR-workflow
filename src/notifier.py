#!/usr/bin/env python3
"""
Notification Module

Supports multiple push channels: Email, PushPlus, Telegram.
"""

import html
import mimetypes
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email import encoders
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
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
        # SMTP host/port: explicit override falls back to smtp.<domain>:465 (SSL)
        self.email_smtp_host = os.getenv("EMAIL_SMTP_HOST")
        try:
            self.email_smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        except ValueError:
            logger.warning(
                "Invalid EMAIL_SMTP_PORT=%r, falling back to 465",
                os.getenv("EMAIL_SMTP_PORT"),
            )
            self.email_smtp_port = 465
        
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
                recipients = [addr.strip() for addr in self.email_to.split(",") if addr.strip()]
                self._send_email(
                    title, 
                    html_content or content, 
                    "html" if html_content else "text",
                    attachments=attachments,
                )
                logger.success(f"[Email] Push succeeded -> {', '.join(recipients)}")
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
                    mime_type, _ = mimetypes.guess_type(path.name)
                    maintype, subtype = (
                        mime_type.split("/", 1)
                        if mime_type
                        else ("application", "octet-stream")
                    )
                    with open(path, "rb") as f:
                        data = f.read()
                    if maintype == "application":
                        attachment = MIMEApplication(data, _subtype=subtype)
                    else:
                        attachment = MIMEBase(maintype, subtype)
                        attachment.set_payload(data)
                        encoders.encode_base64(attachment)

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
        
        # Support multiple recipients (comma-separated)
        recipients = [addr.strip() for addr in self.email_to.split(",") if addr.strip()]
        
        msg["From"] = formataddr((Header(self.email_sender, "utf-8").encode(), self.email_user))
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = Header(title, "utf-8")
        
        if self.email_smtp_host:
            smtp_server = self.email_smtp_host
        elif "@" in self.email_user:
            smtp_server = f"smtp.{self.email_user.split('@', 1)[1]}"
        else:
            raise ValueError(
                f"Cannot infer SMTP host from EMAIL_USER '{self.email_user}' "
                f"(no '@'); set EMAIL_SMTP_HOST explicitly"
            )

        with smtplib.SMTP_SSL(smtp_server, self.email_smtp_port, timeout=30) as server:
            server.login(self.email_user, self.email_pass)
            server.sendmail(self.email_user, recipients, msg.as_string())

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
        """Send Telegram notification.

        Telegram caps a message at 4096 characters, so the body is truncated
        before formatting. If MarkdownV2 escaping hits an edge case and the API
        rejects the message, fall back to plain text without parse_mode.
        """
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ValueError("Telegram configuration incomplete")

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"

        def escape_markdown(text: str) -> str:
            """Escape Markdown special characters (backslash itself first)."""
            text = text.replace("\\", "\\\\")
            special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
            for char in special_chars:
                text = text.replace(char, f'\\{char}')
            return text

        # Truncate the body below the 4096-char hard limit, leaving headroom
        # for the title and MarkdownV2 escape backslashes.
        max_content = 3500
        body = content
        if len(body) > max_content:
            body = body[:max_content].rstrip() + "\n\n…（内容过长已截断，详见邮件/PushPlus）"

        plain = f"{title}\n\n{body}"[:4096]

        # MarkdownV2 escaping can nearly double the length (a backslash before
        # every special char). If the escaped text would blow past Telegram's
        # 4096-char hard limit, skip the doomed formatted request and send
        # plain text directly.
        text = f"*{escape_markdown(title)}*\n\n{escape_markdown(body)}"
        if len(text) > 4096:
            response = self.client.post(
                url, json={"chat_id": self.telegram_chat_id, "text": plain}
            )
            response.raise_for_status()
            return

        response = self.client.post(
            url,
            json={"chat_id": self.telegram_chat_id, "text": text, "parse_mode": "MarkdownV2"},
        )
        if response.status_code >= 400:
            # MarkdownV2 rejected — log the real reason (could be 401/403/429,
            # not just an escaping edge case) before retrying as plain text.
            logger.warning(
                f"[Telegram] MarkdownV2 send failed (HTTP {response.status_code}): "
                f"{response.text[:200]}; retrying as plain text"
            )
            response = self.client.post(
                url, json={"chat_id": self.telegram_chat_id, "text": plain}
            )
        response.raise_for_status()

    def format_update_message(
        self,
        documents_by_category: dict,
        max_docs_per_category: int = 50,
        updated_urls: Optional[set[str]] = None,
        other_updates: Optional[dict[str, int]] = None,
    ) -> tuple[str, str, str]:
        """Format update notification message

        Args:
            documents_by_category: Dict mapping category name to list of documents
            max_docs_per_category: Max documents to show per category (rest summarized)
            updated_urls: URLs of documents whose metadata changed (rendered with
                an "更新" marker). None means every document is new.
            other_updates: Optional mapping of category name to the number of new
                documents detected outside --notify-categories. Rendered as one
                summary line so a "0 条" heartbeat still proves the job ran.

        Returns:
            (title, plain text content, HTML content)
        """
        updated_urls = updated_urls or set()
        other_updates = other_updates or {}
        total = sum(len(docs) for docs in documents_by_category.values())
        updated_total = sum(
            1 for docs in documents_by_category.values() for doc in docs
            if doc.url in updated_urls
        )
        beijing_tz = timezone(timedelta(hours=8))
        timestamp = datetime.now(beijing_tz)

        # Title
        title = f"📋 CAAC 文件更新通知 ({total} 条)"

        # Truncate for display
        truncated = {}
        for cat_name, docs in documents_by_category.items():
            truncated[cat_name] = docs[:max_docs_per_category]

        # Plain text content
        lines = [
            f"检测时间: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            (
                f"新增文件: {total} 条（其中更新 {updated_total} 条）"
                if updated_total
                else f"新增文件: {total} 条"
            ),
            "",
        ]
        other_total = sum(other_updates.values())
        if other_total:
            breakdown = "、".join(
                f"{name} {count}"
                for name, count in sorted(other_updates.items())
                if count
            )
            lines.append(f"其他分类新增: {other_total} 条（{breakdown}，未展开）")
            lines.append("")

        for cat_name, docs in documents_by_category.items():
            if not docs:
                continue
            lines.append(f"【{cat_name}】({len(docs)} 条)")
            display_docs = truncated[cat_name]
            for doc in display_docs:
                marker = "🔄 [更新] " if doc.url in updated_urls else ""
                if doc.doc_number:
                    lines.append(f"  • {marker}{doc.doc_number} {doc.title}")
                else:
                    lines.append(f"  • {marker}{doc.title}")
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
            remaining = len(docs) - len(display_docs)
            if remaining > 0:
                lines.append(f"  ... 还有 {remaining} 条，已省略")
            lines.append("")

        text_content = "\n".join(lines)
        html_content = self._generate_html_email(
            truncated,
            timestamp,
            documents_by_category,
            updated_urls=updated_urls,
            other_updates=other_updates,
        )

        return title, text_content, html_content

    def _generate_html_email(
        self,
        documents_by_category: dict,
        timestamp: datetime,
        full_counts: Optional[dict] = None,
        updated_urls: Optional[set[str]] = None,
        other_updates: Optional[dict[str, int]] = None,
    ) -> str:
        """Generate HTML email content - Apple style clean design

        Args:
            documents_by_category: Possibly truncated dict of docs to render
            timestamp: Current timestamp
            full_counts: Original full dict for accurate counts (optional)
            updated_urls: URLs of documents whose metadata changed (marked "更新")
            other_updates: New documents outside --notify-categories (summary only)
        """
        updated_urls = updated_urls or set()
        other_updates = other_updates or {}
        if full_counts is None:
            full_counts = documents_by_category
        total = sum(len(docs) for docs in full_counts.values())
        
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
                details.append(f"📅 {html.escape(doc.publish_date)}")
            if doc.office_unit:
                details.append(f"🏢 {html.escape(doc.office_unit)}")
            details_html = " · ".join(details) if details else ""
            
            raw_title = f"{doc.doc_number} {doc.title}" if doc.doc_number else doc.title
            doc_title = html.escape(raw_title or "")
            doc_href = html.escape(doc.url or "", quote=True)
            update_badge = (
                '<span style="font-size: 11px; color: #FF9500; border: 1px solid #FF9500;'
                ' border-radius: 4px; padding: 1px 5px; margin-left: 8px;'
                ' white-space: nowrap;">更新</span>'
                if doc.url in updated_urls
                else ""
            )
            
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
                        <a href="{doc_href}" style="font-size: 14px; font-weight: 500; color: #1D1D1F; text-decoration: none; line-height: 1.4; flex: 1;">{doc_title}</a>{update_badge}
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
            full_count = len(full_counts.get(cat_name, docs))

            items_html = ""
            for i, doc in enumerate(docs):
                items_html += render_doc_item(doc, i)

            remaining = full_count - len(docs)
            if remaining > 0:
                items_html += f'''
                <div style="height: 1px; background: #E5E5EA; margin: 16px 0;"></div>
                <div style="text-align: center; padding: 8px 0; font-size: 13px; color: #86868B;">
                    ... 还有 {remaining} 条，已省略
                </div>'''

            category_cards += f'''
    <!-- {cat_name} Card -->
    <div style="background: #FFFFFF; border-radius: 16px; padding: 20px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
        <div style="display: flex; align-items: center; margin-bottom: 16px;">
            <span style="font-size: 20px; margin-right: 10px;">{icon}</span>
            <span style="font-size: 15px; font-weight: 600; color: #1D1D1F;">{cat_name}</span>
            <span style="background: {color}; color: white; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; margin-left: 8px;">{full_count}</span>
        </div>
        {items_html}
    </div>'''
        
        # Build statistics
        stats_items = ""
        for cat_name, docs in full_counts.items():
            if not docs:
                continue
            color = category_colors.get(cat_name, "#007AFF")
            stats_items += f'''
            <div style="text-align: center; padding: 0 8px;">
                <div style="font-size: 24px; font-weight: 600; color: {color};">{len(docs)}</div>
                <div style="font-size: 11px; color: #86868B; margin-top: 2px;">{cat_name}</div>
            </div>'''
        
        # Summary card for new documents outside --notify-categories. This keeps
        # the "0 条" heartbeat informative without listing unrelated documents.
        other_updates_html = ""
        other_total = sum(other_updates.values())
        if other_total:
            breakdown = "、".join(
                f"{name} {count}"
                for name, count in sorted(other_updates.items())
                if count
            )
            other_updates_html = f'''
    <!-- Other categories summary -->
    <div style="background: #FFF8E6; border: 1px solid #FFE0A3; border-radius: 12px; padding: 14px 18px; margin-bottom: 12px;">
        <div style="font-size: 13px; color: #8A6D1F; line-height: 1.5;">
            其他分类新增 {other_total} 条（{breakdown}），未展开。
        </div>
    </div>'''

        html_doc = f'''<!DOCTYPE html>
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

    {other_updates_html}

    {category_cards}
    
    <!-- Footer -->
    <div style="text-align: center; padding: 16px 0;">
        <p style="font-size: 11px; color: #AEAEB2; margin: 0;">CAAC 文件监控系统 · 自动发送</p>
    </div>
    
</div>
</body>
</html>'''

        return html_doc
