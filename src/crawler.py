#!/usr/bin/env python3
"""
CAAC Regulation Crawler Module

Uses Patchright (anti-detection Playwright) to crawl CAAC website regulation list.
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

# Channel IDs
REGULATION_CHANNEL = "269689"  # Regulations channel
NORMATIVE_CHANNEL = "238066"   # Normative documents channel

# Category IDs (fl parameter)
REGULATION_FL = "13"   # Regulations category
NORMATIVE_FL = "14"    # Normative documents category


@dataclass
class RegulationDocument:
    """Regulation document data model"""
    title: str
    url: str
    validity: str  # "有效", "失效", "废止"
    doc_number: str  # Document number
    office_unit: str  # Publishing unit
    doc_type: str  # "regulation" or "normative"
    sign_date: str = ""  # Signing date
    publish_date: str = ""  # Publishing date
    pdf_url: str = ""  # PDF attachment URL

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RegulationDocument":
        """Create from dictionary"""
        return cls(
            title=data.get("title", ""),
            url=data.get("url", ""),
            validity=data.get("validity", ""),
            doc_number=data.get("doc_number", ""),
            office_unit=data.get("office_unit", ""),
            doc_type=data.get("doc_type", "regulation"),
            sign_date=data.get("sign_date", ""),
            publish_date=data.get("publish_date", ""),
            pdf_url=data.get("pdf_url", ""),
        )


def generate_filename(document: RegulationDocument) -> str:
    """Generate PDF filename
    
    Format: {doc_number}{title}.pdf
    Invalid regulations get "失效!" prefix
    """
    def sanitize(text: str) -> str:
        """Replace illegal filename characters"""
        return re.sub(r'[<>:"/\\|?*]', '_', text)

    parts = []

    validity = document.validity.strip()
    if validity in ("失效", "废止"):
        parts.append("失效!")

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
    """CAAC Regulation Crawler"""

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

    def _random_delay(self, min_sec: float = 2.0, max_sec: float = 5.0):
        """Random delay to avoid rate limiting"""
        delay = random.uniform(min_sec, max_sec)
        logger.debug(f"Random delay {delay:.1f} seconds")
        time.sleep(delay)

    def fetch_regulations(self, keyword: str = "") -> list[RegulationDocument]:
        """Fetch regulation list"""
        logger.info("Starting to fetch regulation list...")
        
        if keyword:
            search_url = f"{WAS5_SEARCH_URL}?channelid={REGULATION_CHANNEL}&sw={quote(keyword)}&perpage=100&orderby=-fabuDate&fl={REGULATION_FL}"
        else:
            search_url = f"{WAS5_SEARCH_URL}?channelid={REGULATION_CHANNEL}&perpage=100&orderby=-fabuDate&fl={REGULATION_FL}"

        logger.info(f"Regulation search URL: {search_url}")
        
        try:
            html_content = self._fetch_with_browser(search_url)
            if html_content:
                documents = self._parse_regulation_page(html_content)
                logger.info(f"Regulation list fetched: {len(documents)} items")
                return documents
        except Exception as e:
            logger.error(f"Failed to fetch regulation list: {e}")
        
        return []

    def fetch_normatives(self, keyword: str = "") -> list[RegulationDocument]:
        """Fetch normative document list"""
        logger.info("Starting to fetch normative document list...")
        
        if keyword:
            search_url = f"{WAS5_SEARCH_URL}?channelid={NORMATIVE_CHANNEL}&sw={quote(keyword)}&perpage=100&orderby=-fabuDate&fl={NORMATIVE_FL}"
        else:
            search_url = f"{WAS5_SEARCH_URL}?channelid={NORMATIVE_CHANNEL}&perpage=100&orderby=-fabuDate&fl={NORMATIVE_FL}"

        logger.info(f"Normative document search URL: {search_url}")
        
        try:
            self._random_delay()
            html_content = self._fetch_with_browser(search_url)
            if html_content:
                documents = self._parse_normative_page(html_content)
                logger.info(f"Normative document list fetched: {len(documents)} items")
                return documents
        except Exception as e:
            logger.error(f"Failed to fetch normative document list: {e}")
        
        return []

    def _parse_regulation_page(self, html_content: str) -> list[RegulationDocument]:
        """Parse regulation search result page"""
        documents = []

        try:
            soup = BeautifulSoup(html_content, "lxml")
            table = soup.find("table", class_="t_table")

            if not table:
                tables = soup.find_all("table")
                table = tables[0] if tables else None

            if not table:
                logger.warning("Regulation table not found")
                return documents

            tbody = table.find("tbody")
            rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue

                try:
                    title_cell = row.find("td", class_="t_l")
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

                    doc_number = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    validity = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                    publish_date = extract_date_from_url(full_url)

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
                        documents.append(RegulationDocument(
                            title=title,
                            url=full_url,
                            validity=validity,
                            doc_number=doc_number,
                            office_unit=office_unit,
                            doc_type="regulation",
                            publish_date=publish_date,
                        ))
                except Exception as e:
                    logger.warning(f"Failed to parse regulation row: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to parse regulation page: {e}")

        return documents

    def _parse_normative_page(self, html_content: str) -> list[RegulationDocument]:
        """Parse normative document search result page"""
        documents = []

        try:
            soup = BeautifulSoup(html_content, "lxml")
            table = soup.find("table", class_="t_table")

            if not table:
                tables = soup.find_all("table")
                table = tables[0] if tables else None

            if not table:
                logger.warning("Normative document table not found")
                return documents

            tbody = table.find("tbody")
            rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue

                try:
                    title_cell = row.find("td", class_="tdMC")
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

                    doc_number = ""
                    doc_number_cell = row.find("td", class_="strFL")
                    if doc_number_cell:
                        doc_number = doc_number_cell.get_text(strip=True)

                    validity = ""
                    validity_cell = row.find("td", class_="strGF")
                    if validity_cell:
                        validity = validity_cell.get_text(strip=True)

                    sign_date = ""
                    publish_date = ""
                    date_cells = row.find_all("td", class_="tdRQ")
                    if len(date_cells) >= 1:
                        sign_date = normalize_date(date_cells[0].get_text(strip=True))
                    if len(date_cells) >= 2:
                        publish_date = normalize_date(date_cells[1].get_text(strip=True))

                    office_unit = ""
                    detail_div = title_cell.find("div", class_="t_l_content")
                    if detail_div:
                        unit_li = detail_div.find("li", class_="t_l_content_left")
                        if unit_li:
                            unit_text = unit_li.get_text(strip=True)
                            if "办文单位：" in unit_text:
                                office_unit = unit_text.replace("办文单位：", "").strip()

                    if title:
                        documents.append(RegulationDocument(
                            title=title,
                            url=full_url,
                            validity=validity,
                            doc_number=doc_number,
                            office_unit=office_unit,
                            doc_type="normative",
                            sign_date=sign_date,
                            publish_date=publish_date,
                        ))
                except Exception as e:
                    logger.warning(f"Failed to parse normative document row: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to parse normative document page: {e}")

        return documents

    def download_pdf(self, document: RegulationDocument, save_path: str) -> bool:
        """Download PDF file (streaming to avoid memory overflow)"""
        logger.info(f"Downloading PDF: {document.doc_number} {document.title}")
        
        try:
            self._random_delay(1.0, 3.0)
            html_content = self._fetch_with_browser(document.url)
            
            if not html_content:
                logger.warning("Failed to access detail page")
                return False
            
            soup = BeautifulSoup(html_content, "lxml")
            pdf_link = self._find_pdf_link(soup, document.url)
            
            if not pdf_link:
                logger.warning(f"PDF link not found: {document.url}")
                return False
            
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
