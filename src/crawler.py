#!/usr/bin/env python3
"""
CAAC Document Crawler Module

Uses Patchright (anti-detection Playwright) to crawl CAAC website documents.
Supports all categories under "法定主动公开内容".
"""

import random
import re
import time
from datetime import datetime
from dataclasses import dataclass, asdict
import os
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from patchright.sync_api import sync_playwright

from .search_api import SoGovSearch, classify_url, UNCLASSIFIED

# CAAC website configuration
BASE_URL = "https://www.caac.gov.cn"
WAS5_SEARCH_URL = "https://www.caac.gov.cn/was5/web/search"

# Channel ID for search
SEARCH_CHANNEL = "211383"
SPECIAL_SEARCH_CHANNELS = {
    "13": "269689",
    "14": "238066",
}
WAF_BLOCK_MARKERS = (
    "502 Bad Gateway",
    "云防护节点",
    "源站服务器",
    "Event ID:",
    "您的IP",
)

# Category definitions (fl parameter values and names)
# Based on the website's "法定主动公开内容 > 主题分类"
CATEGORIES = {
    "9": "通知公告",
    "10": "政策发布",
    "11": "政策解读",
    "12": "统计数据",
    "47": "法律法规",
    "13": "民航规章",
    "14": "规范性文件",
    "15": "标准规范",
    "16": "对外关系",
    "17": "港澳台合作",
    "18": "国际公约",
    "19": "人事信息",
    "20": "财政信息",
    "21": "发展规划",
    "22": "重大项目",
    "23": "行政权力",
    "24": "政府公文",
    "25": "机构职能",
    "26": "对外政策",
    "27": "执法典型案例",
    "28": "建议提案答复",
    "29": "政府网站年度报表",
}

DOWNLOAD_SUBDIRS = {
    "13": "regulation",
    "14": "normative",
    "15": "specification",
}

# 中文子目录名映射（用于 --cn-dirs 模式）
DOWNLOAD_SUBDIRS_CN = {
    "13": "CCAR规章",
    "14": "规范性文件",
    "15": "标准规范",
}

# so-gov 检索会把详情页的附件（P0xxxx.pdf 等）当成独立结果返回。
# 对规章/规范性文件/标准规范，附件只能经由详情页获取，否则同一文件会出现两条、
# 且附件条目没有文号/有效性，命名也就丢了文号。
ATTACHMENT_URL_PATTERN = re.compile(r"\.(pdf|docx?|xlsx?|wps|ofd|zip|rar)$", re.I)

# 这三类文件按 {文号}{标题}.pdf 命名，而 so-gov 检索结果不带文号/有效性等字段，
# 必须回详情页的 <li><b>标签：</b>值</li> 元数据里取。
DETAIL_METADATA_CATEGORIES = frozenset({"13", "14", "15"})
DETAIL_METADATA_FIELDS = ("doc_number", "validity", "office_unit", "sign_date", "publish_date")
DETAIL_METADATA_LABELS = frozenset({"文号", "有效性", "办文单位", "成文日期", "发文日期"})


def is_attachment_url(url: str) -> bool:
    """True if the URL points to a file attachment rather than a detail page."""
    if not url:
        return False
    return bool(ATTACHMENT_URL_PATTERN.search(urlparse(url).path))


def parse_detail_metadata(html_content: str, category_id: str) -> dict[str, str]:
    """Extract 文号/有效性/办文单位/日期 from a CAAC detail page.

    Metadata is rendered as ``<li><b>标签：</b>值</li>`` items. The container
    differs between templates (``div.content_nav`` on 规范性文件/标准规范, a
    different wrapper on 民航规章), so items are matched by label instead.
    Some items are hidden duplicates (发文日期/有效性 appear twice with the
    same value), so the first non-empty value per label wins.
    Regulations carry the CCAR part number in a separate "CCAR部号" item,
    which is what historical filenames use (e.g. ``CCAR-290-R4``).

    Returns only the fields that were found, keyed by Document attribute.
    """
    if not html_content:
        return {}

    soup = BeautifulSoup(html_content, "lxml")
    value_by_label: dict[str, str] = {}
    for label_tag in soup.select("li > b"):
        label_match = re.match(r"^(.*?)[：:]\s*$", label_tag.get_text("", strip=True))
        if not label_match:
            continue
        label = re.sub(r"\s+", "", label_match.group(1))
        if label not in DETAIL_METADATA_LABELS and not label.endswith("部号"):
            continue
        item_text = label_tag.parent.get_text("", strip=True)
        value = re.sub(r"^.*?[：:]", "", item_text, count=1).strip()
        if value and label not in value_by_label:
            value_by_label[label] = value

    regulation_part_number = next(
        (value for label, value in value_by_label.items() if label.endswith("部号")),
        "",
    )
    document_number = value_by_label.get("文号", "")
    if category_id == "13" and regulation_part_number:
        document_number = regulation_part_number

    metadata = {
        "doc_number": document_number,
        "validity": value_by_label.get("有效性", ""),
        "office_unit": value_by_label.get("办文单位", ""),
        "sign_date": normalize_date(value_by_label.get("成文日期", "")),
        "publish_date": normalize_date(value_by_label.get("发文日期", "")),
    }
    return {field_name: value for field_name, value in metadata.items() if value}


@dataclass
class Document:
    """Document data model"""
    title: str
    url: str
    category: str  # Category name (e.g., "通知公告", "民航规章")
    category_id: str  # Category ID (fl parameter)
    doc_number: str  # Document number (文号)
    office_unit: str  # Publishing unit (办文单位)
    sign_date: str = ""  # Signing date (成文日期)
    publish_date: str = ""  # Publishing date (发文日期)
    validity: str = ""  # Validity status (有效性)
    pdf_url: str = ""  # PDF attachment URL
    has_pdf: bool = False  # Whether document has PDF attachment

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        """Create from dictionary"""
        return cls(
            title=data.get("title", ""),
            url=data.get("url", ""),
            category=data.get("category", ""),
            category_id=data.get("category_id", ""),
            doc_number=data.get("doc_number", ""),
            office_unit=data.get("office_unit", ""),
            sign_date=data.get("sign_date", ""),
            publish_date=data.get("publish_date", ""),
            validity=data.get("validity", ""),
            pdf_url=data.get("pdf_url", ""),
            has_pdf=data.get("has_pdf", False),
        )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate a string to at most max_bytes UTF-8 bytes without splitting a char."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def generate_filename(document: Document, extension: str = ".pdf") -> str:
    """Generate local filename
    
    Format: {validity!}{doc_number}{title}{ext}
    Validity prefix is only added when validity is not "有效".
    """
    def sanitize(text: str) -> str:
        """Replace illegal filename characters"""
        return re.sub(r'[<>:"/\\|?*]', '_', text)

    parts = []

    validity = document.validity.strip()
    if validity and validity != "有效":
        parts.append(f"{sanitize(validity)}!")

    doc_number = sanitize(document.doc_number.strip())
    if doc_number:
        parts.append(doc_number)

    title = sanitize(document.title.strip())
    parts.append(title or "未命名文件")

    ext = extension.strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    ext_tail = ext.lstrip(".")

    filename = "".join(parts)
    if ext_tail:
        filename = f"{filename}.{ext_tail}"

    # Filesystem limit is 255 *bytes* on ext4 (CI runner); Chinese chars are
    # 3 bytes in UTF-8, so a 200-character cap could still produce a ~600-byte
    # name. Truncate the base on a byte budget, preserving the extension.
    max_bytes = 200
    if len(filename.encode("utf-8")) > max_bytes:
        base = "".join(parts)
        if ext_tail:
            budget = max(1, max_bytes - len(ext_tail.encode("utf-8")) - 1)
            filename = f"{_truncate_utf8(base, budget)}.{ext_tail}"
        else:
            filename = _truncate_utf8(base, max_bytes)

    return filename


def get_download_subdir(category_id: str, use_cn: bool = False) -> str:
    """Map category ID to local download subdirectory
    
    Args:
        category_id: 分类 ID
        use_cn: 是否使用中文目录名
    """
    mapping = DOWNLOAD_SUBDIRS_CN if use_cn else DOWNLOAD_SUBDIRS
    return mapping.get(category_id, "other")


def normalize_date(date_str: str) -> str:
    """Normalize date format: 2024年01月15日 -> 2024-01-15"""
    if not date_str:
        return ""
    
    match = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    
    if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        return date_str
    
    return date_str


def extract_date_from_url(url: str) -> str:
    """Extract date from URL"""
    match = re.search(r'/t(\d{4})(\d{2})(\d{2})_', url)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    return ""


def _looks_like_waf_block(html_content: str) -> bool:
    """Detect CAAC cloud-protection error pages."""
    if not html_content:
        return False
    marker_count = sum(1 for marker in WAF_BLOCK_MARKERS if marker in html_content)
    return marker_count >= 2


class CaacCrawler:
    """CAAC Document Crawler"""

    def __init__(self):
        self._http_client: Optional[httpx.Client] = None
        self._playwright = None
        self._browser = None
        # Detail pages fetched for metadata are reused by the download step,
        # so each new document costs one page load instead of two.
        self._detail_html_by_url: dict[str, str] = {}

    def _get_http_client(self) -> httpx.Client:
        """Get HTTP client (lazy loading)"""
        if self._http_client is None:
            timeout = httpx.Timeout(
                connect=10.0,
                read=60.0,
                write=30.0,
                pool=10.0,
            )
            self._http_client = httpx.Client(
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                },
                follow_redirects=True,
            )
        return self._http_client

    def _get_browser(self):
        """Get browser instance (reuse)"""
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        if self._browser is None:
            try:
                self._browser = self._playwright.chromium.launch(headless=True)
                logger.info("Browser instance started")
            except Exception:
                if self._playwright is not None:
                    self._playwright.stop()
                    self._playwright = None
                self._browser = None
                raise
        return self._browser

    def close(self):
        """Close resources"""
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None
        
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
            logger.info("Browser instance closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _fetch_with_browser(self, url: str, retry_count: int = 3) -> str:
        """Fetch page content using Patchright (anti-detection)"""
        last_error = None
        
        for attempt in range(retry_count):
            try:
                logger.info(f"Fetching page (attempt {attempt + 1}/{retry_count}): {url}")
                
                browser = self._get_browser()
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    
                    try:
                        page.wait_for_selector(
                            "table.t_table, .article-content, .TRS_Editor, .content",
                            timeout=10000
                        )
                    except Exception:
                        page.wait_for_timeout(2000)
                    
                    content = page.content()
                    if _looks_like_waf_block(content):
                        last_error = RuntimeError("CAAC WAF/cloud protection page returned")
                        logger.warning(
                            f"CAAC WAF/cloud protection page returned "
                            f"(attempt {attempt + 1}/{retry_count}): {len(content)} characters"
                        )
                        if attempt < retry_count - 1:
                            time.sleep(2 ** attempt)
                            continue
                        return ""
                    logger.info(f"Page fetched successfully: {len(content)} characters")
                    return content
                finally:
                    context.close()
                    
            except Exception as e:
                last_error = e
                logger.warning(f"Failed to fetch page (attempt {attempt + 1}/{retry_count}): {e}")
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)
        
        logger.error(f"Failed to fetch page after all retries: {last_error}")
        return ""

    def _random_delay(self, min_sec: float = 1.0, max_sec: float = 3.0):
        """Random delay to avoid rate limiting"""
        delay = random.uniform(min_sec, max_sec)
        logger.debug(f"Random delay {delay:.1f} seconds")
        time.sleep(delay)

    # ------------------------------------------------------------------
    # 取数层：so-gov 统一检索（was5 已于 2026-10 停止响应）
    # ------------------------------------------------------------------

    def _hit_to_document(self, hit, category_id: str) -> Document:
        """把检索结果转换成 Document。"""
        category_name = CATEGORIES.get(category_id, f"未知分类({category_id})")
        url = hit.url or ""
        is_pdf = url.lower().split("?")[0].endswith(".pdf") or hit.file_type == "pdf"
        return Document(
            title=hit.title or "",
            url=url,
            category=category_name,
            category_id=category_id,
            doc_number="",
            office_unit="",
            sign_date="",
            publish_date=hit.doc_date or "",
            validity="",
            pdf_url=url if is_pdf else "",
            has_pdf=is_pdf,
        )

    def fetch_category(self, category_id: str, perpage: int = 50, days: Optional[int] = None) -> list[Document]:
        """抓取单个分类（内部委托给 fetch_all_categories）。"""
        return self.fetch_all_categories([category_id], perpage=perpage, days=days).get(category_id, [])

    @staticmethod
    def needs_detail_metadata(document: Document) -> bool:
        """True for 13/14/15 detail pages that have never had their metadata read."""
        if document.category_id not in DETAIL_METADATA_CATEGORIES:
            return False
        if is_attachment_url(document.url):
            return False
        return not (document.doc_number or document.validity or document.office_unit)

    def apply_detail_metadata(self, document: Document) -> bool:
        """Read 文号/有效性/办文单位/日期 from the detail page into the document."""
        self._random_delay(2.0, 5.0)
        html_content = self._fetch_with_browser(document.url)
        if not html_content:
            logger.warning(f"Detail page unavailable, metadata not filled: {document.url}")
            return False

        self._detail_html_by_url[document.url] = html_content
        metadata = parse_detail_metadata(html_content, document.category_id)
        if not metadata:
            logger.warning(f"No metadata block on detail page: {document.url}")
            return False

        for field_name, value in metadata.items():
            setattr(document, field_name, value)
        logger.info(
            f"Detail metadata: [{document.category}] {document.doc_number or '(无文号)'} "
            f"{document.title} | 有效性={document.validity or '-'}"
        )
        return True

    def enrich_documents_with_detail_metadata(
        self,
        documents_by_category: dict[str, list[Document]],
    ) -> int:
        """Fill detail-page metadata for every 13/14/15 document still missing it.

        Returns the number of documents successfully enriched.
        """
        pending_documents = [
            document
            for category_id in DETAIL_METADATA_CATEGORIES
            for document in documents_by_category.get(category_id, [])
            if self.needs_detail_metadata(document)
        ]
        if not pending_documents:
            return 0

        logger.info(f"Reading detail-page metadata for {len(pending_documents)} documents...")
        enriched_count = sum(1 for document in pending_documents if self.apply_detail_metadata(document))
        logger.info(f"Detail metadata filled: {enriched_count}/{len(pending_documents)}")
        return enriched_count

    def fetch_all_categories(
        self,
        category_ids: list[str] = None,
        perpage: int = 50,
        days: Optional[int] = None,
    ) -> dict[str, list[Document]]:
        """用 so-gov 统一检索拉取近 N 天文档，再按 URL 路径分流到各分类。

        说明：新检索 API 不支持按栏目过滤（channelid/fl/column 全部无效），
        只能"多关键词 + 时间窗口"取回一批，再按 URL 里的 /XXGK/XXGK/<栏目代码>/
        自行归类。因此：
          * 时间窗口默认 7 天，可用环境变量 SO_GOV_DAYS 覆盖；
          * 单关键词最多 10 页 × 20 条 = 200 条（站点 maxPage 限制），
            靠多关键词并集提升召回。

        Args:
            category_ids: 需要返回的分类 ID 列表；None = 全部已定义分类
            perpage: 兼容旧签名，新 API 固定每页 20，此参数不再生效
            days: 时间窗口天数；None 时读 SO_GOV_DAYS，再不行默认 7

        Returns:
            {category_id: [Document, ...]}
        """
        if category_ids is None:
            category_ids = list(CATEGORIES.keys())
        wanted = set(category_ids)

        if days is None:
            try:
                days = int(os.getenv("SO_GOV_DAYS", "7") or "7")
            except ValueError:
                days = 7
        if days <= 0:
            days = 7

        logger.info(f"[so-gov] Fetching last {days} day(s) via unified search...")

        searcher = SoGovSearch()
        try:
            hits = searcher.fetch_recent(days=days)
        finally:
            searcher.close()

        results: dict[str, list[Document]] = {cid: [] for cid in category_ids}
        counters: dict[str, int] = {}
        skipped_unknown = 0
        skipped_other = 0
        skipped_attachments = 0

        for hit in hits:
            cid = classify_url(hit.url)
            if cid == UNCLASSIFIED or cid not in CATEGORIES:
                skipped_unknown += 1
                if UNCLASSIFIED in wanted:
                    results.setdefault(UNCLASSIFIED, []).append(
                        self._hit_to_document(hit, UNCLASSIFIED)
                    )
                continue
            if cid not in wanted:
                skipped_other += 1
                continue
            if cid in DETAIL_METADATA_CATEGORIES and is_attachment_url(hit.url):
                skipped_attachments += 1
                continue
            results[cid].append(self._hit_to_document(hit, cid))
            counters[cid] = counters.get(cid, 0) + 1

        for cid, docs in sorted(results.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999):
            if docs:
                logger.info(
                    f"Category {CATEGORIES.get(cid, cid)} (ID: {cid}): {len(docs)} documents"
                )

        total = sum(len(v) for v in results.values())
        logger.info(
            f"Total: {total} documents from {len([k for k, v in results.items() if v])} "
            f"categories (hits={len(hits)}, unmapped_url={skipped_unknown}, "
            f"other_category={skipped_other}, attachment_only={skipped_attachments})"
        )

        if total == 0:
            logger.error(
                "No documents matched any monitored category. "
                "Either the search API is blocked, or no updates in the window. "
                "Check the [so-gov] log lines above."
            )

        return results

    def _parse_list_page(self, html_content: str, category_id: str, category_name: str) -> list[Document]:
        """Parse document list page"""
        documents = []

        try:
            soup = BeautifulSoup(html_content, "lxml")
            
            # Find the main table
            table = soup.find("table", class_="t_table")
            if not table:
                tables = soup.find_all("table")
                for t in tables:
                    if t.find("th") or t.find("td", class_="tdMC"):
                        table = t
                        break

            if not table:
                logger.warning(f"Table not found for category {category_name}")
                return documents

            tbody = table.find("tbody")
            rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                try:
                    # Find title cell (may have different class names)
                    title_cell = row.find("td", class_="tdMC") or row.find("td", class_="t_l")
                    if not title_cell and len(cells) > 1:
                        title_cell = cells[1]
                    if not title_cell:
                        continue

                    link = title_cell.find("a", href=True)
                    if not link:
                        continue

                    title = link.get_text(strip=True)
                    href = link.get("href", "")
                    full_url = urljoin(BASE_URL, href)

                    # Get document number
                    doc_number = ""
                    doc_number_cell = row.find("td", class_="strFL")
                    if doc_number_cell:
                        doc_number = doc_number_cell.get_text(strip=True)
                    elif len(cells) > 2:
                        doc_number = cells[2].get_text(strip=True)

                    # Get validity
                    validity = ""
                    validity_cell = row.find("td", class_="strGF")
                    if validity_cell:
                        validity = validity_cell.get_text(strip=True)
                    elif len(cells) > 3:
                        validity = cells[3].get_text(strip=True)

                    # Get dates
                    sign_date = ""
                    publish_date = ""
                    date_cells = row.find_all("td", class_="tdRQ")
                    if len(date_cells) >= 1:
                        sign_date = normalize_date(date_cells[0].get_text(strip=True))
                    if len(date_cells) >= 2:
                        publish_date = normalize_date(date_cells[1].get_text(strip=True))
                    
                    # Fallback: extract date from URL
                    if not publish_date:
                        publish_date = extract_date_from_url(full_url)

                    # Get office unit from detail div
                    office_unit = ""
                    detail_div = title_cell.find("div", class_="t_l_content")
                    if detail_div:
                        for li in detail_div.find_all("li"):
                            li_text = li.get_text(strip=True)
                            if "办文单位：" in li_text:
                                office_unit = li_text.replace("办文单位：", "").strip()
                            elif "发文日期：" in li_text or "发文日期:" in li_text:
                                date_text = re.sub(r"发文日期[：:]", "", li_text).strip()
                                publish_date = normalize_date(date_text) or publish_date
                            elif "有效性" in li_text and not validity:
                                validity = re.sub(r"有\s*效\s*性\s*[：:]", "", li_text).strip()

                    if title:
                        documents.append(Document(
                            title=title,
                            url=full_url,
                            category=category_name,
                            category_id=category_id,
                            doc_number=doc_number,
                            office_unit=office_unit,
                            sign_date=sign_date,
                            publish_date=publish_date,
                            validity=validity,
                        ))
                except Exception as e:
                    logger.warning(f"Failed to parse row: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to parse list page: {e}")

        return documents

    def check_pdf_and_download(self, document: Document, save_path: str) -> bool:
        """Backward-compatible helper: download file and report PDF-only success"""
        base_path, _ = os.path.splitext(save_path)
        saved_path = self.download_document_file(document, base_path, preferred_ext=".pdf")
        return bool(saved_path and saved_path.lower().endswith(".pdf"))

    def download_document_file(
        self,
        document: Document,
        save_base_path: str,
        preferred_ext: str = ".pdf",
    ) -> Optional[str]:
        """Download attachment or fallback to text extraction.

        Returns:
            Saved local path if success, otherwise None.
        """
        logger.info(f"Checking attachment: [{document.category}] {document.doc_number} {document.title}")

        try:
            # 检索结果本身就是 .pdf 直链时，跳过详情页直接下载
            if document.url and document.url.lower().split("?")[0].endswith(".pdf"):
                ext = self._guess_file_extension(document.url, preferred_ext)
                direct_path = f"{save_base_path}{ext}"
                if self._download_binary_file(document.url, direct_path):
                    document.pdf_url = document.url
                    document.has_pdf = True
                    return direct_path
                logger.warning(f"Direct PDF download failed: {document.url}")

            html_content = self._detail_html_by_url.get(document.url, "")
            if not html_content:
                self._random_delay(0.5, 1.5)
                html_content = self._fetch_with_browser(document.url)

            if not html_content:
                logger.warning("Failed to access detail page")
                return None

            soup = BeautifulSoup(html_content, "lxml")
            attachment_link = self._find_attachment_link(soup, document.url)

            if attachment_link:
                file_ext = self._guess_file_extension(attachment_link, preferred_ext)
                save_path = f"{save_base_path}{file_ext}"

                if self._download_binary_file(attachment_link, save_path, referer=document.url):
                    if file_ext.lower() == ".pdf":
                        document.pdf_url = attachment_link
                        document.has_pdf = True
                    else:
                        document.pdf_url = ""
                        document.has_pdf = False
                    return save_path

                logger.warning(f"Attachment download failed, fallback to text: {attachment_link}")
            else:
                logger.debug(f"No attachment link found: {document.url}")

            txt_path = f"{save_base_path}.txt"
            if self._extract_and_save_text_content(document, soup, txt_path):
                document.pdf_url = ""
                document.has_pdf = False
                return txt_path

            return None
        except Exception as e:
            logger.error(f"Failed to download document file: {e}")
            return None

    def _is_valid_binary(self, body: bytes, ext: str) -> bool:
        """Validate downloaded binary content (reject WAF/HTML responses)."""
        if len(body) < 1024:
            return False

        ext = ext.lower()
        if ext == ".pdf":
            return body[:4] == b"%PDF"
        if ext == ".docx":
            return body[:2] == b"PK"
        if ext == ".doc":
            return body[:4] == b"\xd0\xcf\x11\xe0"
        return True

    def _save_binary_body(self, body: bytes, save_path: str) -> bool:
        """Write and validate binary content to disk."""
        ext = os.path.splitext(save_path)[1]
        if not self._is_valid_binary(body, ext):
            logger.warning(f"Downloaded content invalid ({len(body)} bytes): {save_path}")
            return False

        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(body)
        logger.info(f"File saved: {save_path} ({len(body) / 1024:.1f} KB)")
        return True

    def _download_via_browser_context(
        self,
        context,
        file_url: str,
        save_path: str,
        referer: str = "",
    ) -> bool:
        """Download binary file using browser request context (WAF-safe)."""
        logger.info(f"Downloading via browser: {file_url}")
        try:
            headers = {"Referer": referer} if referer else None
            response = context.request.get(file_url, timeout=60000, headers=headers)
            if response.status != 200:
                logger.warning(f"Browser download failed: HTTP {response.status}")
                return False
            return self._save_binary_body(response.body(), save_path)
        except Exception as e:
            logger.warning(f"Browser download exception: {e}")
            if os.path.exists(save_path):
                os.remove(save_path)
            return False

    def _download_binary_file(
        self,
        file_url: str,
        save_path: str,
        referer: str = "",
    ) -> bool:
        """Download binary file: browser session first, httpx as fallback."""
        if referer:
            browser = self._get_browser()
            context = browser.new_context()
            try:
                page = context.new_page()
                page.goto(referer, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                if self._download_via_browser_context(context, file_url, save_path, referer=referer):
                    return True
            except Exception as e:
                logger.warning(f"Browser download setup failed: {e}")
            finally:
                context.close()

        logger.info(f"Downloading via HTTP: {file_url}")
        try:
            headers = {"Referer": referer} if referer else None
            timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
            with self._get_http_client().stream(
                "GET", file_url, headers=headers, timeout=timeout
            ) as response:
                if response.status_code != 200:
                    logger.warning(f"Download failed: HTTP {response.status_code}")
                    return False

                content_length = response.headers.get("content-length")
                if content_length:
                    logger.info(f"File size: {int(content_length) / 1024:.1f} KB")

                chunks = []
                for chunk in response.iter_bytes(chunk_size=8192):
                    chunks.append(chunk)

            body = b"".join(chunks)
            if not self._save_binary_body(body, save_path):
                if os.path.exists(save_path):
                    os.remove(save_path)
                return False
            return True
        except Exception as e:
            logger.warning(f"Download exception: {e}")
            if os.path.exists(save_path):
                os.remove(save_path)
            return False

    def _guess_file_extension(self, file_url: str, default_ext: str = ".pdf") -> str:
        """Guess extension from URL, fallback to default"""
        match = re.search(r"\.(pdf|docx?)($|[?#])", file_url, re.I)
        if match:
            return f".{match.group(1).lower()}"

        ext = default_ext.strip() if default_ext else ".pdf"
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        return ext or ".pdf"

    def _find_attachment_link(self, soup: BeautifulSoup, doc_url: str) -> Optional[str]:
        """Find downloadable attachment link in detail page"""
        file_pattern = re.compile(r"\.(pdf|doc|docx)$", re.I)

        # Pattern 1: links near "附件"
        attachment_texts = soup.find_all(string=re.compile(r"附件[：:]?", re.I))
        for text in attachment_texts:
            parent = text.parent
            if not parent:
                continue
            container = parent.parent or parent
            links = container.find_all("a", href=file_pattern)
            if links:
                full_url = self._build_full_url(links[0].get("href"), doc_url)
                if full_url:
                    return full_url

        # Pattern 2: all file links, prefer official XXGK path or absolute URL
        file_links = soup.find_all("a", href=file_pattern)
        for link in file_links:
            href = link.get("href", "")
            if href and ("/XXGK/" in href or href.startswith("http")):
                full_url = self._build_full_url(href, doc_url)
                if full_url:
                    return full_url
        if file_links:
            full_url = self._build_full_url(file_links[0].get("href"), doc_url)
            if full_url:
                return full_url

        # Pattern 3: keyword links (download / attachment)
        keyword_links = soup.find_all("a")
        for link in keyword_links:
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if not href:
                continue
            href_lower = href.lower()
            if (
                "download" in href_lower
                or "attachment" in href_lower
                or ".pdf" in href_lower
                or ".doc" in href_lower
                or "附件" in text
                or "下载" in text
            ):
                full_url = self._build_full_url(href, doc_url)
                if full_url:
                    return full_url

        # Pattern 4: onclick embedded file URL
        onclick_links = soup.find_all("a", onclick=True)
        for link in onclick_links:
            onclick = link.get("onclick", "")
            match = re.search(r"['\"]([^'\"]*(?:\.pdf|\.doc|\.docx|download|attachment)[^'\"]*)['\"]", onclick, re.I)
            if match:
                full_url = self._build_full_url(match.group(1), doc_url)
                if full_url:
                    return full_url

        return None

    def _extract_and_save_text_content(self, document: Document, soup: BeautifulSoup, txt_path: str) -> bool:
        """Extract main text from detail page and save as .txt"""
        try:
            content_text = ""

            main_content = soup.find("div", class_="content") or soup.find("div", class_="main-content")
            if main_content:
                content_text = main_content.get_text(separator="\n", strip=True)

            if not content_text:
                article_content = soup.find("div", class_="article") or soup.find("article")
                if article_content:
                    content_text = article_content.get_text(separator="\n", strip=True)

            if not content_text:
                for div in soup.find_all("div"):
                    div_text = div.get_text(strip=True)
                    if len(div_text) > 500 and ("第一条" in div_text or "总则" in div_text or "第一章" in div_text):
                        content_text = div.get_text(separator="\n", strip=True)
                        break

            if not content_text:
                for element in soup.find_all(["nav", "header", "footer", "aside"]):
                    element.decompose()
                body_content = soup.find("body")
                if body_content:
                    full_text = body_content.get_text(separator="\n", strip=True)
                    title = document.title or ""
                    if title and title in full_text:
                        content_text = full_text[full_text.find(title):]
                    else:
                        content_text = full_text

            if not content_text:
                logger.warning(f"No text content extracted: {document.url}")
                return False

            lines = [line.strip() for line in content_text.split("\n") if line.strip()]
            normalized = "\n".join(lines)
            if len(normalized) > 50000:
                normalized = normalized[:50000] + "\n\n[内容过长，已截取前50KB]"

            os.makedirs(os.path.dirname(txt_path) or ".", exist_ok=True)
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"文档标题: {document.title}\n")
                f.write(f"文档链接: {document.url}\n")
                f.write(f"提取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 50 + "\n\n")
                f.write(normalized)

            logger.info(f"Text saved: {txt_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to extract text content: {e}")
            return False

    def _build_full_url(self, link: str, doc_url: str) -> str:
        """Build full URL"""
        if not link:
            return ""

        link = link.strip()
        if link.lower().startswith("javascript:"):
            return ""

        if link.startswith('http'):
            return link
        
        if link.startswith('/'):
            from urllib.parse import urlparse
            parsed = urlparse(doc_url)
            return f"{parsed.scheme}://{parsed.netloc}{link}"
        
        doc_dir = '/'.join(doc_url.split('/')[:-1])
        
        while link.startswith('../'):
            link = link[3:]
            doc_dir = '/'.join(doc_dir.split('/')[:-1])
        
        if link.startswith('./'):
            link = link[2:]
        
        return f"{doc_dir}/{link}"


# Legacy compatibility - map old types to new categories
def get_legacy_doc_type(category_id: str) -> str:
    """Map category ID to legacy doc_type for backward compatibility"""
    mapping = {
        "13": "regulation",
        "14": "normative",
        "15": "standard",
    }
    return mapping.get(category_id, "document")
