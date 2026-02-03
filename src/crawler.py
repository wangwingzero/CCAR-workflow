#!/usr/bin/env python3
"""
CAAC Document Crawler Module

Uses Patchright (anti-detection Playwright) to crawl CAAC website documents.
Supports all categories under "法定主动公开内容".
"""

import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin, quote

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from patchright.sync_api import sync_playwright

# CAAC website configuration
BASE_URL = "https://www.caac.gov.cn"
WAS5_SEARCH_URL = "https://www.caac.gov.cn/was5/web/search"

# Channel ID for search
SEARCH_CHANNEL = "211383"

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


def generate_filename(document: Document) -> str:
    """Generate PDF filename
    
    Format: [{category}]{doc_number}{title}.pdf
    Invalid documents get "失效!" prefix
    """
    def sanitize(text: str) -> str:
        """Replace illegal filename characters"""
        return re.sub(r'[<>:"/\\|?*]', '_', text)

    parts = []

    validity = document.validity.strip()
    if validity in ("失效", "废止"):
        parts.append("失效!")

    # Add category prefix
    category = sanitize(document.category.strip())
    if category:
        parts.append(f"[{category}]")

    doc_number = sanitize(document.doc_number.strip())
    if doc_number:
        parts.append(doc_number)

    title = sanitize(document.title.strip())
    parts.append(title)

    filename = "".join(parts) + ".pdf"

    if len(filename) > 200:
        filename = filename[:197] + "....pdf"

    return filename


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


class CaacCrawler:
    """CAAC Document Crawler"""

    def __init__(self):
        self._http_client: Optional[httpx.Client] = None
        self._playwright = None
        self._browser = None

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
            self._browser = self._playwright.chromium.launch(headless=True)
            logger.info("Browser instance started")
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

    def fetch_category(self, category_id: str, perpage: int = 50) -> list[Document]:
        """Fetch documents from a specific category
        
        Args:
            category_id: Category ID (fl parameter)
            perpage: Number of results per page
        
        Returns:
            List of documents
        """
        category_name = CATEGORIES.get(category_id, f"未知分类({category_id})")
        logger.info(f"Fetching category: {category_name} (ID: {category_id})")
        
        # Build search URL
        search_url = (
            f"{WAS5_SEARCH_URL}?"
            f"channelid={SEARCH_CHANNEL}&"
            f"was_custom_expr=+PARENTID%3D%27{category_id}%27+or+CLASSINFOID%3D%27{category_id}%27+&"
            f"perpage={perpage}&"
            f"orderby=-fabuDate&"
            f"fl={category_id}"
        )
        
        logger.debug(f"Search URL: {search_url}")
        
        try:
            html_content = self._fetch_with_browser(search_url)
            if html_content:
                documents = self._parse_list_page(html_content, category_id, category_name)
                logger.info(f"Category {category_name}: {len(documents)} documents")
                return documents
        except Exception as e:
            logger.error(f"Failed to fetch category {category_name}: {e}")
        
        return []

    def fetch_all_categories(self, category_ids: list[str] = None, perpage: int = 50) -> dict[str, list[Document]]:
        """Fetch documents from all or specified categories
        
        Args:
            category_ids: List of category IDs to fetch. If None, fetch all.
            perpage: Number of results per page
        
        Returns:
            Dictionary mapping category_id to list of documents
        """
        if category_ids is None:
            category_ids = list(CATEGORIES.keys())
        
        results = {}
        total_docs = 0
        
        for i, cat_id in enumerate(category_ids):
            if i > 0:
                self._random_delay()
            
            docs = self.fetch_category(cat_id, perpage)
            results[cat_id] = docs
            total_docs += len(docs)
        
        logger.info(f"Total: {total_docs} documents from {len(category_ids)} categories")
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
        """Check if document has PDF and download it
        
        Args:
            document: Document to check
            save_path: Path to save PDF
        
        Returns:
            True if PDF was downloaded successfully
        """
        logger.info(f"Checking PDF: [{document.category}] {document.doc_number} {document.title}")
        
        try:
            self._random_delay(0.5, 1.5)
            html_content = self._fetch_with_browser(document.url)
            
            if not html_content:
                logger.warning("Failed to access detail page")
                return False
            
            soup = BeautifulSoup(html_content, "lxml")
            pdf_link = self._find_pdf_link(soup, document.url)
            
            if not pdf_link:
                logger.debug(f"No PDF found: {document.url}")
                return False
            
            document.pdf_url = pdf_link
            document.has_pdf = True
            
            logger.info(f"Downloading PDF: {pdf_link}")
            
            import os
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            
            with self._get_http_client().stream("GET", pdf_link) as response:
                if response.status_code != 200:
                    logger.warning(f"Download failed: HTTP {response.status_code}")
                    return False
                
                content_length = response.headers.get("content-length")
                if content_length:
                    logger.info(f"File size: {int(content_length) / 1024:.1f} KB")
                
                total_size = 0
                with open(save_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        total_size += len(chunk)
            
            if total_size < 1024:
                logger.warning(f"Downloaded file too small ({total_size} bytes)")
                os.remove(save_path)
                return False
            
            logger.info(f"PDF saved: {save_path} ({total_size / 1024:.1f} KB)")
            return True
            
        except Exception as e:
            logger.error(f"Failed to download PDF: {e}")
            return False

    def _find_pdf_link(self, soup: BeautifulSoup, doc_url: str) -> Optional[str]:
        """Find PDF download link in detail page"""
        
        # Pattern 1: Find attachment area
        attachment_texts = soup.find_all(string=re.compile(r'附件[：:]?', re.I))
        for text in attachment_texts:
            parent = text.parent
            if parent:
                container = parent.parent or parent
                pdf_pattern = re.compile(r'\.pdf$', re.I)
                links = container.find_all('a', href=pdf_pattern)
                if links:
                    return self._build_full_url(links[0].get('href'), doc_url)
        
        # Pattern 2: Find all PDF links directly
        pdf_pattern = re.compile(r'\.pdf$', re.I)
        links = soup.find_all('a', href=pdf_pattern)
        if links:
            return self._build_full_url(links[0].get('href'), doc_url)
        
        return None

    def _build_full_url(self, link: str, doc_url: str) -> str:
        """Build full URL"""
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
