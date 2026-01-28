#!/usr/bin/env python3
"""
CAAC 规章爬虫模块

使用 Patchright (反检测 Playwright) 爬取中国民航局官网规章列表。
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

# CAAC 官网配置
BASE_URL = "https://www.caac.gov.cn"
WAS5_SEARCH_URL = "https://www.caac.gov.cn/was5/web/search"

# 频道 ID
REGULATION_CHANNEL = "269689"  # 民航规章频道
NORMATIVE_CHANNEL = "238066"   # 规范性文件频道

# 分类 ID (fl 参数)
REGULATION_FL = "13"   # 民航规章分类
NORMATIVE_FL = "14"    # 规范性文件分类


@dataclass
class RegulationDocument:
    """规章文档数据模型"""
    title: str
    url: str
    validity: str  # "有效", "失效", "废止"
    doc_number: str  # 文号
    office_unit: str  # 发布单位
    doc_type: str  # "regulation" 规章, "normative" 规范性文件
    sign_date: str = ""  # 签发日期
    publish_date: str = ""  # 发布日期
    pdf_url: str = ""  # PDF 附件链接

    def to_dict(self) -> dict:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RegulationDocument":
        """从字典创建"""
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
    """生成 PDF 文件名
    
    格式: {文号}{标题}.pdf
    失效的规章加 "失效!" 前缀
    
    示例:
    - CCAR-91-R4一般运行和飞行规则.pdf
    - AC-91-FS-041航空器运行-航空器操作程序.pdf
    - 失效!CCAR-121-R6大型飞机公共航空运输承运人运行合格审定规则.pdf
    """
    def sanitize(text: str) -> str:
        """替换文件名中的非法字符"""
        return re.sub(r'[<>:"/\\|?*]', '_', text)

    parts = []

    # 有效性前缀（失效的加前缀）
    validity = document.validity.strip()
    if validity in ("失效", "废止"):
        parts.append("失效!")

    # 文号
    doc_number = sanitize(document.doc_number.strip())
    if doc_number:
        parts.append(doc_number)

    # 标题
    title = sanitize(document.title.strip())
    parts.append(title)

    filename = "".join(parts) + ".pdf"

    # 限制文件名长度
    if len(filename) > 200:
        filename = filename[:197] + "....pdf"

    return filename


def normalize_date(date_str: str) -> str:
    """标准化日期格式：2024年01月15日 -> 2024-01-15"""
    if not date_str:
        return ""
    
    match = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    
    # 已经是标准格式
    if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        return date_str
    
    return date_str


def extract_date_from_url(url: str) -> str:
    """从 URL 提取日期"""
    match = re.search(r'/t(\d{4})(\d{2})(\d{2})_', url)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    return ""


class CaacCrawler:
    """CAAC 规章爬虫"""

    def __init__(self):
        self._http_client: Optional[httpx.Client] = None
        self._playwright = None
        self._browser = None

    def _get_http_client(self) -> httpx.Client:
        """获取 HTTP 客户端（懒加载）
        
        配置说明：
        - connect: 连接超时 10 秒
        - read: 读取超时 60 秒（大文件需要更长时间）
        - write: 写入超时 30 秒
        - pool: 连接池超时 10 秒
        """
        if self._http_client is None:
            # 精细化超时配置，避免大文件下载超时
            timeout = httpx.Timeout(
                connect=10.0,
                read=60.0,
                write=30.0,
                pool=10.0,
            )
            self._http_client = httpx.Client(
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                },
                follow_redirects=True,
            )
        return self._http_client

    def _get_browser(self):
        """获取浏览器实例（复用，避免每次请求都启动新浏览器）"""
        if self._playwright is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            logger.info("浏览器实例已启动")
        return self._browser

    def close(self):
        """关闭资源"""
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None
        
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
            logger.info("浏览器实例已关闭")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _fetch_with_browser(self, url: str, retry_count: int = 3) -> str:
        """使用 Patchright 获取页面内容（反检测）
        
        Args:
            url: 目标 URL
            retry_count: 重试次数
        
        Returns:
            页面 HTML 内容
            
        Note:
            使用 domcontentloaded 而非 networkidle，因为政府网站常有
            统计脚本/地图服务等导致网络永远不"空闲"，容易超时。
        """
        last_error = None
        
        for attempt in range(retry_count):
            try:
                logger.info(f"获取页面 (尝试 {attempt + 1}/{retry_count}): {url}")
                
                browser = self._get_browser()
                # 使用新的 context 隔离，但复用 browser
                context = browser.new_context()
                try:
                    page = context.new_page()
                    # 使用 domcontentloaded 避免政府网站统计脚本导致的超时
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    
                    # 等待页面核心内容加载（表格或文章内容）
                    try:
                        page.wait_for_selector(
                            "table.t_table, .article-content, .TRS_Editor, .content",
                            timeout=10000
                        )
                    except Exception:
                        # 如果找不到特定元素，等待一小段时间让 JS 执行
                        page.wait_for_timeout(2000)
                    
                    content = page.content()
                    logger.info(f"页面获取成功: {len(content)} 字符")
                    return content
                finally:
                    context.close()
                    
            except Exception as e:
                last_error = e
                logger.warning(f"获取页面失败 (尝试 {attempt + 1}/{retry_count}): {e}")
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)  # 指数退避
        
        logger.error(f"获取页面最终失败: {last_error}")
        return ""

    def _random_delay(self, min_sec: float = 2.0, max_sec: float = 5.0):
        """随机延迟，避免触发限流"""
        delay = random.uniform(min_sec, max_sec)
        logger.debug(f"随机延迟 {delay:.1f} 秒")
        time.sleep(delay)


    def fetch_regulations(self, keyword: str = "") -> list[RegulationDocument]:
        """获取规章列表"""
        logger.info("开始获取规章列表...")
        
        if keyword:
            search_url = f"{WAS5_SEARCH_URL}?channelid={REGULATION_CHANNEL}&sw={quote(keyword)}&perpage=100&orderby=-fabuDate&fl={REGULATION_FL}"
        else:
            search_url = f"{WAS5_SEARCH_URL}?channelid={REGULATION_CHANNEL}&perpage=100&orderby=-fabuDate&fl={REGULATION_FL}"

        logger.info(f"规章搜索 URL: {search_url}")
        
        try:
            html_content = self._fetch_with_browser(search_url)
            if html_content:
                documents = self._parse_regulation_page(html_content)
                logger.info(f"规章列表获取完成，共 {len(documents)} 条")
                return documents
        except Exception as e:
            logger.error(f"获取规章列表失败: {e}")
        
        return []

    def fetch_normatives(self, keyword: str = "") -> list[RegulationDocument]:
        """获取规范性文件列表"""
        logger.info("开始获取规范性文件列表...")
        
        if keyword:
            search_url = f"{WAS5_SEARCH_URL}?channelid={NORMATIVE_CHANNEL}&sw={quote(keyword)}&perpage=100&orderby=-fabuDate&fl={NORMATIVE_FL}"
        else:
            search_url = f"{WAS5_SEARCH_URL}?channelid={NORMATIVE_CHANNEL}&perpage=100&orderby=-fabuDate&fl={NORMATIVE_FL}"

        logger.info(f"规范性文件搜索 URL: {search_url}")
        
        try:
            self._random_delay()  # 两次请求之间加延迟
            html_content = self._fetch_with_browser(search_url)
            if html_content:
                documents = self._parse_normative_page(html_content)
                logger.info(f"规范性文件列表获取完成，共 {len(documents)} 条")
                return documents
        except Exception as e:
            logger.error(f"获取规范性文件列表失败: {e}")
        
        return []

    def _parse_regulation_page(self, html_content: str) -> list[RegulationDocument]:
        """解析规章搜索结果页面"""
        documents = []

        try:
            soup = BeautifulSoup(html_content, "lxml")
            table = soup.find("table", class_="t_table")

            if not table:
                tables = soup.find_all("table")
                table = tables[0] if tables else None

            if not table:
                logger.warning("未找到规章表格")
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
                    logger.warning(f"解析规章行失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"解析规章页面失败: {e}")

        return documents


    def _parse_normative_page(self, html_content: str) -> list[RegulationDocument]:
        """解析规范性文件搜索结果页面"""
        documents = []

        try:
            soup = BeautifulSoup(html_content, "lxml")
            table = soup.find("table", class_="t_table")

            if not table:
                tables = soup.find_all("table")
                table = tables[0] if tables else None

            if not table:
                logger.warning("未找到规范性文件表格")
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
                    logger.warning(f"解析规范性文件行失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"解析规范性文件页面失败: {e}")

        return documents

    def download_pdf(self, document: RegulationDocument, save_path: str) -> bool:
        """下载 PDF 文件（使用流式传输，避免内存溢出）
        
        Args:
            document: 规章文档
            save_path: 保存路径
        
        Returns:
            是否下载成功
        """
        logger.info(f"下载 PDF: {document.doc_number} {document.title}")
        
        try:
            # 先访问详情页获取 PDF 链接
            self._random_delay(1.0, 3.0)
            html_content = self._fetch_with_browser(document.url)
            
            if not html_content:
                logger.warning("访问详情页失败")
                return False
            
            soup = BeautifulSoup(html_content, "lxml")
            pdf_link = self._find_pdf_link(soup, document.url)
            
            if not pdf_link:
                logger.warning(f"未找到 PDF 链接: {document.url}")
                return False
            
            # 使用流式下载，避免大文件内存溢出
            logger.info(f"下载 PDF: {pdf_link}")
            
            import os
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            
            # 流式下载
            with self._get_http_client().stream("GET", pdf_link) as response:
                if response.status_code != 200:
                    logger.warning(f"下载失败: HTTP {response.status_code}")
                    return False
                
                # 获取文件大小（如果有）
                content_length = response.headers.get("content-length")
                if content_length:
                    logger.info(f"文件大小: {int(content_length) / 1024:.1f} KB")
                
                # 写入文件
                total_size = 0
                with open(save_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        total_size += len(chunk)
            
            # 检查文件大小
            if total_size < 1024:
                logger.warning(f"下载的文件太小 ({total_size} bytes)，可能不是有效的 PDF")
                os.remove(save_path)
                return False
            
            logger.info(f"PDF 保存成功: {save_path} ({total_size / 1024:.1f} KB)")
            return True
            
        except Exception as e:
            logger.error(f"下载 PDF 失败: {e}")
            return False

    def _find_pdf_link(self, soup: BeautifulSoup, doc_url: str) -> Optional[str]:
        """在详情页查找 PDF 下载链接"""
        
        # 模式1: 查找附件区域
        attachment_texts = soup.find_all(string=re.compile(r'附件[：:]?', re.I))
        for text in attachment_texts:
            parent = text.parent
            if parent:
                container = parent.parent or parent
                links = container.find_all('a', href=re.compile(r'\.pdf$', re.I))
                if links:
                    return self._build_full_url(links[0].get('href'), doc_url)
        
        # 模式2: 直接查找所有 PDF 链接
        links = soup.find_all('a', href=re.compile(r'\.pdf$', re.I))
        if links:
            return self._build_full_url(links[0].get('href'), doc_url)
        
        return None

    def _build_full_url(self, link: str, doc_url: str) -> str:
        """构建完整 URL"""
        if link.startswith('http'):
            return link
        
        if link.startswith('/'):
            from urllib.parse import urlparse
            parsed = urlparse(doc_url)
            return f"{parsed.scheme}://{parsed.netloc}{link}"
        
        # 相对路径
        doc_dir = '/'.join(doc_url.split('/')[:-1])
        
        # 处理 ../ 和 ./
        while link.startswith('../'):
            link = link[3:]
            doc_dir = '/'.join(doc_dir.split('/')[:-1])
        
        if link.startswith('./'):
            link = link[2:]
        
        return f"{doc_dir}/{link}"
