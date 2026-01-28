#!/usr/bin/env python3
"""
通知模块

支持多种推送渠道：Email、PushPlus、Telegram。
"""

import os
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Literal, Optional

import httpx
from loguru import logger

from .crawler import RegulationDocument, generate_filename


class Notifier:
    """通知管理器"""

    def __init__(self):
        # Email 配置
        self.email_user = os.getenv("EMAIL_USER")
        self.email_pass = os.getenv("EMAIL_PASS")
        # EMAIL_TO 不填则默认发送给 EMAIL_USER
        self.email_to = os.getenv("EMAIL_TO") or self.email_user
        self.email_sender = os.getenv("EMAIL_SENDER", "CAAC 规章监控")
        
        # PushPlus 配置 (https://www.pushplus.plus/)
        self.pushplus_token = os.getenv("PUSHPLUS_TOKEN")
        
        # Telegram 配置
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        # HTTP 客户端
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        """获取 HTTP 客户端（懒加载）"""
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def close(self):
        """关闭资源"""
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
    ) -> dict[str, bool]:
        """发送通知到所有配置的渠道
        
        Args:
            title: 通知标题
            content: 纯文本内容
            html_content: HTML 内容（可选，用于邮件）
        
        Returns:
            各渠道发送结果
        """
        results: dict[str, bool] = {}
        
        # Email
        if self.email_user and self.email_pass and self.email_to:
            try:
                self._send_email(title, html_content or content, "html" if html_content else "text")
                logger.success(f"[Email] 推送成功 -> {self.email_to}")
                results["Email"] = True
            except Exception as e:
                logger.error(f"[Email] 推送失败: {e}")
                results["Email"] = False
        
        # PushPlus
        if self.pushplus_token:
            try:
                self._send_pushplus(title, html_content or content, "html" if html_content else "text")
                logger.success("[PushPlus] 推送成功")
                results["PushPlus"] = True
            except Exception as e:
                logger.error(f"[PushPlus] 推送失败: {e}")
                results["PushPlus"] = False
        
        # Telegram
        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                self._send_telegram(title, content)
                logger.success("[Telegram] 推送成功")
                results["Telegram"] = True
            except Exception as e:
                logger.error(f"[Telegram] 推送失败: {e}")
                results["Telegram"] = False
        
        if not results:
            logger.warning("未配置任何通知渠道")
        
        return results


    def _send_email(
        self,
        title: str,
        content: str,
        msg_type: Literal["text", "html"] = "text",
    ):
        """发送邮件通知"""
        if not self.email_user or not self.email_pass or not self.email_to:
            raise ValueError("Email 配置不完整")
        
        # 创建邮件
        msg = MIMEMultipart("alternative")
        
        if msg_type == "html":
            msg.attach(MIMEText("请使用支持 HTML 的邮件客户端查看此邮件。", "plain", "utf-8"))
            msg.attach(MIMEText(content, "html", "utf-8"))
        else:
            msg.attach(MIMEText(content, "plain", "utf-8"))
        
        # ⚠️ From 字段格式必须正确，否则 502 错误
        msg["From"] = formataddr((Header(self.email_sender, "utf-8").encode(), self.email_user))
        msg["To"] = self.email_to
        msg["Subject"] = Header(title, "utf-8")
        
        # 确定 SMTP 服务器
        domain = self.email_user.split("@")[1]
        smtp_server = f"smtp.{domain}"
        
        # 使用 SSL 465 端口
        with smtplib.SMTP_SSL(smtp_server, 465) as server:
            server.login(self.email_user, self.email_pass)
            server.sendmail(self.email_user, [self.email_to], msg.as_string())

    def _send_pushplus(
        self,
        title: str,
        content: str,
        msg_type: Literal["text", "html"] = "text",
    ):
        """发送 PushPlus 通知"""
        if not self.pushplus_token:
            raise ValueError("PushPlus 配置不完整")
        
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
        """发送 Telegram 通知"""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ValueError("Telegram 配置不完整")
        
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        
        # 转义 Markdown 特殊字符，避免解析错误
        def escape_markdown(text: str) -> str:
            """转义 Markdown 特殊字符"""
            special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
            for char in special_chars:
                text = text.replace(char, f'\\{char}')
            return text
        
        # 使用 MarkdownV2 格式，需要转义特殊字符
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
        new_regulations: list[RegulationDocument],
        new_normatives: list[RegulationDocument],
    ) -> tuple[str, str, str]:
        """格式化更新通知消息
        
        Returns:
            (标题, 纯文本内容, HTML内容)
        """
        total = len(new_regulations) + len(new_normatives)
        timestamp = datetime.now()
        
        # 标题
        title = f"📋 CAAC 规章更新通知 ({total} 条)"
        
        # 纯文本内容
        lines = [
            f"检测时间: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            f"新增规章: {len(new_regulations)} 条",
            f"新增规范性文件: {len(new_normatives)} 条",
            "",
        ]
        
        if new_regulations:
            lines.append("【新增规章】")
            for doc in new_regulations:
                lines.append(f"  • {doc.doc_number} {doc.title}")
                details = [f"状态: {doc.validity}"]
                if doc.publish_date:
                    details.append(f"发布: {doc.publish_date}")
                if doc.sign_date:
                    details.append(f"签发: {doc.sign_date}")
                if doc.office_unit:
                    details.append(f"单位: {doc.office_unit}")
                lines.append(f"    {' | '.join(details)}")
                lines.append(f"    详情: {doc.url}")
                if doc.pdf_url:
                    lines.append(f"    下载: {doc.pdf_url}")
            lines.append("")
        
        if new_normatives:
            lines.append("【新增规范性文件】")
            for doc in new_normatives:
                lines.append(f"  • {doc.doc_number} {doc.title}")
                details = [f"状态: {doc.validity}"]
                if doc.publish_date:
                    details.append(f"发布: {doc.publish_date}")
                if doc.sign_date:
                    details.append(f"签发: {doc.sign_date}")
                if doc.office_unit:
                    details.append(f"单位: {doc.office_unit}")
                lines.append(f"    {' | '.join(details)}")
                lines.append(f"    详情: {doc.url}")
                if doc.pdf_url:
                    lines.append(f"    下载: {doc.pdf_url}")
        
        text_content = "\n".join(lines)
        
        # HTML 内容
        html_content = self._generate_html_email(new_regulations, new_normatives, timestamp)
        
        return title, text_content, html_content


    def _generate_html_email(
        self,
        new_regulations: list[RegulationDocument],
        new_normatives: list[RegulationDocument],
        timestamp: datetime,
    ) -> str:
        """生成 HTML 邮件内容 - Apple 风格简洁设计"""
        total = len(new_regulations) + len(new_normatives)
        
        # 状态判断
        if total > 0:
            status_icon = "✓"
            status_bg = "#34C759"  # Apple Green
            status_text = "检测完成"
        else:
            status_icon = "−"
            status_bg = "#86868B"  # Apple Gray
            status_text = "暂无更新"
        
        def render_doc_item(doc: RegulationDocument, index: int) -> str:
            """渲染单个文档项"""
            filename = generate_filename(doc)
            
            # 状态颜色和图标
            if doc.validity == "有效":
                validity_color = "#34C759"
                validity_icon = "✓"
            else:
                validity_color = "#FF3B30"
                validity_icon = "✗"
            
            # 构建详情
            details = []
            if doc.publish_date:
                details.append(f"📅 {doc.publish_date}")
            if doc.sign_date:
                details.append(f"✍️ {doc.sign_date}")
            if doc.office_unit:
                details.append(f"🏢 {doc.office_unit}")
            details_html = " · ".join(details) if details else ""
            
            # 下载按钮
            download_btn = ""
            if doc.pdf_url:
                download_btn = f'''
                    <a href="{doc.pdf_url}" style="display: inline-block; background: #34C759; color: white; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-size: 13px; font-weight: 500; margin-right: 8px;">⬇️ 下载</a>'''
            
            # 分隔线（非第一项）
            separator = '<div style="height: 1px; background: #F5F5F7; margin: 16px 0;"></div>' if index > 0 else ""
            
            return f'''{separator}
                <div>
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                        <a href="{doc.url}" style="font-size: 15px; font-weight: 600; color: #1D1D1F; text-decoration: none; line-height: 1.4; flex: 1;">{doc.doc_number} {doc.title}</a>
                        <div style="width: 24px; height: 24px; background: {validity_color}; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin-left: 12px; flex-shrink: 0;">
                            <span style="color: white; font-size: 14px;">{validity_icon}</span>
                        </div>
                    </div>
                    <div style="font-size: 13px; color: #86868B; margin-bottom: 8px;">{details_html}</div>
                    <div style="font-size: 12px; color: #AEAEB2; margin-bottom: 12px;">📁 {filename}</div>
                    <div>
                        {download_btn}
                        <a href="{doc.url}" style="display: inline-block; background: #007AFF; color: white; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-size: 13px; font-weight: 500;">📄 详情</a>
                    </div>
                </div>'''
        
        # 生成规章卡片
        regulations_card = ""
        if new_regulations:
            items_html = ""
            for i, doc in enumerate(new_regulations):
                items_html += render_doc_item(doc, i)
            
            regulations_card = f'''
    <!-- 规章卡片 -->
    <div style="background: #FFFFFF; border-radius: 18px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.04);">
        <div style="display: flex; align-items: center; margin-bottom: 20px;">
            <span style="font-size: 24px; margin-right: 12px;">📜</span>
            <span style="font-size: 17px; font-weight: 600; color: #1D1D1F;">民航规章</span>
            <span style="background: #007AFF; color: white; font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 10px; margin-left: 8px;">{len(new_regulations)}</span>
        </div>
        {items_html}
    </div>'''
        
        # 生成规范性文件卡片
        normatives_card = ""
        if new_normatives:
            items_html = ""
            for i, doc in enumerate(new_normatives):
                items_html += render_doc_item(doc, i)
            
            normatives_card = f'''
    <!-- 规范性文件卡片 -->
    <div style="background: #FFFFFF; border-radius: 18px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.04);">
        <div style="display: flex; align-items: center; margin-bottom: 20px;">
            <span style="font-size: 24px; margin-right: 12px;">📋</span>
            <span style="font-size: 17px; font-weight: 600; color: #1D1D1F;">规范性文件</span>
            <span style="background: #FF9500; color: white; font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 10px; margin-left: 8px;">{len(new_normatives)}</span>
        </div>
        {items_html}
    </div>'''
        
        html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; background-color: #F5F5F7;">
<div style="font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', 'Helvetica Neue', Helvetica, Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 40px 20px; background-color: #F5F5F7; -webkit-font-smoothing: antialiased;">
    
    <!-- 状态指示器 -->
    <div style="text-align: center; margin-bottom: 32px;">
        <div style="display: inline-block; width: 64px; height: 64px; background: {status_bg}; border-radius: 50%; line-height: 64px; margin-bottom: 16px;">
            <span style="color: white; font-size: 32px; font-weight: 300;">{status_icon}</span>
        </div>
        <h1 style="margin: 0; font-size: 28px; font-weight: 600; color: #1D1D1F; letter-spacing: -0.5px;">{status_text}</h1>
        <p style="margin: 8px 0 0 0; font-size: 15px; color: #86868B;">{timestamp.strftime('%Y年%m月%d日 %H:%M')}</p>
    </div>
    
    <!-- 统计卡片 -->
    <div style="background: #FFFFFF; border-radius: 18px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.04);">
        <div style="display: flex; justify-content: space-around; text-align: center;">
            <div>
                <div style="font-size: 34px; font-weight: 600; color: #007AFF; letter-spacing: -1px;">{len(new_regulations)}</div>
                <div style="font-size: 13px; color: #86868B; margin-top: 4px;">民航规章</div>
            </div>
            <div style="width: 1px; background: #F5F5F7;"></div>
            <div>
                <div style="font-size: 34px; font-weight: 600; color: #FF9500; letter-spacing: -1px;">{len(new_normatives)}</div>
                <div style="font-size: 13px; color: #86868B; margin-top: 4px;">规范性文件</div>
            </div>
        </div>
    </div>
    
    {regulations_card}
    {normatives_card}
    
    <!-- 页脚 -->
    <div style="text-align: center; padding: 20px 0;">
        <p style="font-size: 12px; color: #AEAEB2; margin: 0;">CAAC 规章监控系统 · 自动发送</p>
    </div>
    
</div>
</body>
</html>'''
        
        return html
