#!/usr/bin/env python3
"""CAAC was5 list service client.

so-gov keyword search is recall-oriented: it can miss documents whose title
does not contain any of the configured keywords.  The CAAC site still exposes
the classic ``was5`` list service, which serves the complete, date-sorted
listing of the 民航规章 / 规范性文件 / 标准规范 columns.  We use it as a
second discovery source and merge its rows with the so-gov results by URL.

The list rows already carry 文号 / 有效性 / 办文单位 / 发文日期, so documents
discovered here need fewer detail-page visits.
"""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

import httpx
from bs4 import BeautifulSoup
from loguru import logger

WAS5_SEARCH_URL = "http://www.caac.gov.cn/was5/web/search"

# Only these categories have a clean, date-sorted channel list.  The site's
# goto() JavaScript maps fl=13 to a dedicated 民航规章 channel, while the
# other two reuse the general channel with an ``fl`` filter.
LIST_CHANNELS: dict[str, dict[str, str]] = {
    "13": {"channelid": "269689"},
    "14": {"channelid": "211383", "fl": "14"},
    "15": {"channelid": "211383", "fl": "15"},
}

DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAX_PAGES = 10
REQUEST_TIMEOUT = 25.0
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ISO_DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_LI_LABEL_RE = re.compile(r"^([^：:]{1,16})[：:]\s*(.*)$")


@dataclass
class Was5Item:
    """One row of a was5 category list."""

    title: str
    url: str
    publish_date: str = ""
    doc_number: str = ""
    validity: str = ""
    office_unit: str = ""
    category_id: str = ""


def normalize_doc_url(url: str) -> str:
    """Canonicalise a CAAC document URL for dedupe against the state file.

    The state file and so-gov both store ``http://www.caac.gov.cn/...``; was5
    returns the same scheme, but host variants (``caac.gov.cn``) do occur.  The
    scheme is deliberately *not* upgraded: dedupe is by exact URL, so a scheme
    rewrite would make every was5 row look like a brand-new document.
    """
    url = (url or "").strip()
    for prefix in ("https://caac.gov.cn", "http://caac.gov.cn"):
        if url.startswith(prefix):
            return "http://www.caac.gov.cn" + url[len(prefix) :]
    if url.startswith("https://www.caac.gov.cn"):
        return "http://" + url[len("https://") :]
    return url


def normalize_date(value: str) -> str:
    """Return an ISO date for 2026年10月23日 / 2026-10-23 / 2026/10/23."""
    text = (value or "").strip()
    match = _CN_DATE_RE.search(text) or _ISO_DATE_RE.search(text)
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def _today() -> date:
    """Today's date; separated so tests can pin the clock."""
    return date.today()


def _cell_text(row, css_class: str) -> str:
    cell = row.find("td", class_=css_class)
    if cell is None:
        return ""
    return _clean(cell.get_text(" ", strip=True))


def _extract_li_fields(row) -> dict[str, str]:
    """Extract 办文单位/发文日期/文号/CCAR部号/有效性 from the row's <li> list."""
    fields: dict[str, str] = {}
    for item in row.find_all("li"):
        text = _clean(item.get_text(" ", strip=True))
        match = _LI_LABEL_RE.match(text)
        if not match:
            continue
        label = re.sub(r"\s+", "", match.group(1))
        value = match.group(2).strip()
        if value:
            fields.setdefault(label, value)
    return fields


def parse_list_rows(html: str, category_id: str) -> list[Was5Item]:
    """Parse a was5 list page into items. Returns [] for unknown categories."""
    if category_id not in LIST_CHANNELS or not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="t_table")
    if table is None:
        return []

    items: list[Was5Item] = []
    seen_urls: set[str] = set()
    for row in table.find_all("tr"):
        link = row.find("a", href=True)
        if link is None:
            continue
        href = normalize_doc_url(link.get("href") or "")
        if not href.startswith(("http://", "https://")):
            continue
        title = _clean(link.get("name") or link.get_text(" ", strip=True))
        if not title or href in seen_urls:
            continue
        seen_urls.add(href)

        fields = _extract_li_fields(row)
        publish_date = normalize_date(fields.get("发文日期", "")) or normalize_date(
            _cell_text(row, "tdRQ")
        )
        doc_number = (
            fields.get("CCAR部号", "")
            or fields.get("文号", "")
            or _cell_text(row, "strFL")
        )
        validity = fields.get("有效性", "") or _cell_text(row, "strGF")
        office_unit = fields.get("办文单位", "")

        items.append(
            Was5Item(
                title=title,
                url=href,
                publish_date=publish_date,
                doc_number=_clean(doc_number),
                validity=_clean(validity),
                office_unit=_clean(office_unit),
                category_id=category_id,
            )
        )
    return items


class Was5ListFetcher:
    """Fetch date-sorted was5 category lists for categories 13/14/15."""

    def __init__(
        self,
        timeout: float = REQUEST_TIMEOUT,
        min_delay: float = 0.8,
        max_delay: float = 1.5,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        # The HTTP client is created on first use so constructing a fetcher
        # never touches the network stack (and stays cheap to unit test).
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": UA, "Referer": "https://www.caac.gov.cn/XXGK/XXGK/"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _pace(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _fetch_page(self, category_id: str, page: int) -> str:
        params = dict(LIST_CHANNELS[category_id])
        params["page"] = str(page)
        response = self.client.get(WAS5_SEARCH_URL, params=params)
        if response.status_code != 200:
            raise RuntimeError(
                f"was5 list HTTP {response.status_code} for category {category_id} page {page}"
            )
        response.encoding = "utf-8"
        return response.text

    def fetch_recent(self, category_id: str, days: Optional[int] = None) -> list[Was5Item]:
        """Return items published within the window, newest first."""
        if category_id not in LIST_CHANNELS:
            return []
        window = int(os.getenv("WAS5_DAYS", str(DEFAULT_WINDOW_DAYS)) or DEFAULT_WINDOW_DAYS)
        if days is not None and days > 0:
            window = days
        cutoff = (_today() - timedelta(days=window)).isoformat()
        max_pages = int(os.getenv("WAS5_MAX_PAGES", str(DEFAULT_MAX_PAGES)) or DEFAULT_MAX_PAGES)

        collected: dict[str, Was5Item] = {}
        for page in range(1, max_pages + 1):
            html = self._fetch_page(category_id, page)
            rows = parse_list_rows(html, category_id)
            if not rows:
                break
            page_fresh = 0
            for row in rows:
                if not row.publish_date:
                    # Keep undated rows from the first couple of pages; the
                    # state-based dedupe prevents historical rewrites.
                    if page <= 2 and row.url not in collected:
                        collected[row.url] = row
                        page_fresh += 1
                    continue
                if row.publish_date < cutoff:
                    continue
                collected.setdefault(row.url, row)
                page_fresh += 1
            dated = [row.publish_date for row in rows if row.publish_date]
            logger.debug(
                f"[was5] category {category_id} page {page}: "
                f"{len(rows)} rows, {page_fresh} in window"
            )
            # The listing is newest-first: once a whole page is older than the
            # window there is nothing left to collect.
            if idx_last_page(rows) or not dated or max(dated) < cutoff:
                break
            self._pace()

        items = list(collected.values())
        items.sort(key=lambda item: item.publish_date, reverse=True)
        return items


def idx_last_page(rows: Iterable[Was5Item]) -> bool:
    """True when a page clearly contains fewer rows than a full list page."""
    return len(list(rows)) < 10
