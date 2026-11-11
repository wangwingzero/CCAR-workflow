#!/usr/bin/env python3
"""
CAAC 统一搜索（so-gov）客户端

背景：民航局原站内检索 `/was5/web/search` 已于 2026-10 停止响应（TCP/TLS 正常、首字节恒为 0）。
栏目页 `search.js` 的 `config.searchPageURL` 已改为政府网站统一检索平台：

    POST https://api.so-gov.cn/query/s       (form-urlencoded)
    siteCode=bm70000001 & tab=all & qt=<keyword> & page=N & pageSize=20
    可选：timeOption=1 & days=N   → 只返回最近 N 天发布的文档

已知契约（实测）：
  * `qt` 不能为空（否则 code=201 无搜索词）
  * `pageSize` 上限 20
  * `page` 上限由站点 tabs 配置写死 `maxPage=10` → 单次查询最多 200 条
  * 分类过滤参数（channelid/fl/column/L4/DOCNF…）全部无效，只能按返回的 URL 路径自行分类
  * 返回 `resultDocs[].data`：titleO / url / docDate / siteLabel.value / fileType
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlencode

import httpx
from loguru import logger

SEARCH_API = "https://api.so-gov.cn/query/s"
SITE_CODE = "bm70000001"
REFERER = "http://www.caac.gov.cn/so/s?tab=all&siteCode=bm70000001"

PAGE_SIZE = 20
MAX_PAGE = 10  # 站点 tabs 配置写死，超出会回到第 1 页


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default

# 栏目代码 → 分类 ID（URL 里的 /XXGK/XXGK/<CODE>/）
COLUMN_CODE_TO_CATEGORY = {
    "MHGZ": "13",     # 民航规章
    "GFXWJ": "14",    # 规范性文件
    "BZGF": "15",     # 标准规范
    "TZTG": "9",      # 通知公告
    "ZCFB": "10",     # 政策发布
    "ZCFBJD": "10",
    "ZCJD": "11",     # 政策解读
    "TJSJ": "12",     # 统计数据
    "SBGX": "16",     # 对外关系（双边关系）
    "RSXX": "19",     # 人事信息
    "CZXX": "20",     # 财政信息
    "FZGH": "21",     # 发展规划
    "ZDXM": "22",     # 重大项目
    "XZQL": "23",     # 行政权力
    "ZFGW": "24",     # 政府公文
    "JGZN": "25",     # 机构职能
    "DWZC": "26",     # 对外政策
    "ZFDXAL": "27",   # 执法典型案例
    "FLFG": "47",     # 法律法规
}

UNCLASSIFIED = "0"

# 用于"拉全量"的宽关键词。检索 API 必须带 qt，没有"列出全部"的能力，
# 所以靠多关键词并集 + timeOption/days 窗口来覆盖近 N 天的更新。
# 召回测试（30 天窗口，对照 data/regulations.json 已知 17 篇）漏了
# 「国际航线审批结果公开（C2026008）」——原词表没有"航线/审批"。
# 补词后该篇可召回。请求量随词数线性增长，新增词前请先跑召回测试。
DEFAULT_KEYWORDS = [
    "民航", "航空", "机场", "飞行", "运输", "空管", "航路", "空域",
    "通知", "规定", "办法", "标准", "规章", "通告", "公告", "意见",
    "安全", "运行", "管理", "技术", "维修", "执照", "训练", "审定",
    "航线", "审批", "许可", "气象", "通信", "导航", "危险品", "应急",
]


@dataclass
class SearchHit:
    title: str
    url: str
    doc_date: str
    site_name: str
    file_type: str


def classify_url(url: str) -> str:
    """按 URL 路径把文档归到分类 ID；无法识别返回 UNCLASSIFIED。"""
    if not url:
        return UNCLASSIFIED
    marker = "/XXGK/XXGK/"
    if marker in url:
        tail = url.split(marker, 1)[1]
        code = tail.split("/", 1)[0].upper()
        return COLUMN_CODE_TO_CATEGORY.get(code, UNCLASSIFIED)
    return UNCLASSIFIED


_URL_DATE_RE = re.compile(r"/(\d{4})(\d{2})/(?:t(\d{4})(\d{2})(\d{2})_|P0\d+(\d{4})(\d{2})(\d{2}))")


def date_from_url(url: str) -> str:
    """从民航局 URL 里取发布日期。

    两种形态：
      /202610/t20261009_231819.html   -> 2026-10-09
      /202610/P020261023378688959993  -> 2026-10-23（P 后是 yyyymmddHHMMSS...）

    必须优先用它，而不是 API 返回的 docDate：实测「国际航线审批结果公开
    （C2026008）」API 的 docDate=2026-09-16（成文日期），而网站实际发文
    日期是 2026-10-09，URL 里写的是 t20261009_231819。用 docDate 卡时间
    窗口会把这类文档整批漏掉。
    """
    if not url:
        return ""
    m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", url)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    m = re.search(r"/P0(\d{4})(\d{2})(\d{2})", url)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    return ""


def _valid_date(value: str) -> bool:
    """过滤 API 偶发返回的 1970-01-01 等占位日期。"""
    return bool(value) and value[:4].isdigit() and int(value[:4]) >= 2000


class SoGovSearch:
    """统一检索客户端：多关键词 + 时间窗口 → 去重后的 SearchHit 列表。"""

    def __init__(
        self,
        keywords: Optional[Iterable[str]] = None,
        timeout: float = 25.0,
        min_delay: float = 1.2,
        max_delay: float = 2.0,
    ) -> None:
        self.keywords = list(keywords or DEFAULT_KEYWORDS)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=15.0, pool=10.0),
            headers={
                "Referer": REFERER,
                "Origin": "http://www.caac.gov.cn",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            follow_redirects=True,
        )
        self._last_request = 0.0
        self.request_count = 0

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ---- 底层请求 -------------------------------------------------------

    def _pace(self) -> None:
        """礼貌间隔，避免被统一检索平台限流/封禁。"""
        import random

        gap = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.time() - self._last_request
        if elapsed < gap:
            time.sleep(gap - elapsed)

    def _query(self, keyword: str, page: int, days: int) -> dict:
        self._pace()
        payload = {
            "siteCode": SITE_CODE,
            "tab": "all",
            "qt": keyword,
            "page": str(page),
            "pageSize": str(PAGE_SIZE),
        }
        if days and days > 0:
            payload["timeOption"] = "1"
            payload["days"] = str(days)

        # 必须用 form 编码并显式声明 Content-Type：httpx 的 content= 不会自动加
        # application/x-www-form-urlencoded，服务端会把 siteCode 读成 null 并报
        # "不存在的站点[null]"。用 data= 让它按表单编码发送。
        resp = self._client.post(
            SEARCH_API,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._last_request = time.time()
        self.request_count += 1

        if resp.status_code != 200:
            raise RuntimeError(f"search HTTP {resp.status_code}")

        data = resp.json()
        if not data.get("ok"):
            code = data.get("code")
            msg = data.get("msg") or ""
            if code == -101:
                raise RuntimeError(f"IP banned by so-gov: {msg}")
            raise RuntimeError(f"search rejected: code={code} msg={msg}")
        return data

    # ---- 对外接口 -------------------------------------------------------

    def fetch_keyword(self, keyword: str, days: int, max_pages: int = 0) -> list[SearchHit]:
        max_pages = max_pages or _env_int("SO_GOV_MAX_PAGES", MAX_PAGE)
        """按关键词翻页抓取，遇到空页/重复页即停。"""
        hits: list[SearchHit] = []
        seen_first: set[str] = set()

        for page in range(1, max_pages + 1):
            try:
                data = self._query(keyword, page, days)
            except Exception as exc:  # noqa: BLE001 —— 单关键词失败不拖垮整轮
                logger.warning(f"[so-gov] keyword={keyword} page={page} failed: {exc}")
                break

            docs = data.get("resultDocs") or []
            if not docs:
                break

            first_url = ((docs[0].get("data") or {}).get("url") or "")
            if page > 1 and first_url in seen_first:
                # 超过 maxPage 后服务端会回到第 1 页，说明已到上限
                logger.info(f"[so-gov] keyword={keyword} hit maxPage at page={page}, stop")
                break
            if first_url:
                seen_first.add(first_url)

            for item in docs:
                raw = item.get("data") or {}
                url = (raw.get("url") or "").strip()
                if not url:
                    continue
                api_date = (raw.get("docDate") or "").strip()
                doc_date = date_from_url(url) or api_date
                if not _valid_date(doc_date):
                    continue
                hits.append(
                    SearchHit(
                        title=(raw.get("titleO") or "").strip(),
                        url=url,
                        doc_date=doc_date,
                        site_name=((raw.get("siteLabel") or {}).get("value") or "").strip(),
                        file_type=(raw.get("fileType") or "").strip(),
                    )
                )

            if len(docs) < PAGE_SIZE:
                break

        return hits

    def fetch_keyword_wide(self, keyword: str, cutoff: str, max_pages: int = 0) -> list[SearchHit]:
        max_pages = max_pages or _env_int("SO_GOV_WIDE_PAGES", 2)
        """不带时间过滤的补充抓取，本地按 docDate 卡 cutoff。

        为什么需要这一趟：`timeOption=1&days=N` 过滤用的不是返回里的 docDate。
        实测「国际航线审批结果公开（C2026008）」docDate=2026-10-09（在窗口内），
        但 days=30 查不到；去掉时间过滤后第 1 页就能找到。说明平台用的是另一个
        日期字段（成文日期之类），部分文档因此被窗口漏掉。补一趟宽查询兜住。
        """
        hits: list[SearchHit] = []
        stale_pages = 0

        for page in range(1, max_pages + 1):
            try:
                data = self._query(keyword, page, days=0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[so-gov] wide kw={keyword} page={page} failed: {exc}")
                break

            docs = data.get("resultDocs") or []
            if not docs:
                break

            fresh = 0
            for item in docs:
                raw = item.get("data") or {}
                url = (raw.get("url") or "").strip()
                if not url:
                    continue
                api_date = (raw.get("docDate") or "").strip()
                doc_date = date_from_url(url) or api_date
                if not _valid_date(doc_date):
                    continue
                if doc_date < cutoff:
                    continue
                fresh += 1
                hits.append(
                    SearchHit(
                        title=(raw.get("titleO") or "").strip(),
                        url=url,
                        doc_date=doc_date,
                        site_name=((raw.get("siteLabel") or {}).get("value") or "").strip(),
                        file_type=(raw.get("fileType") or "").strip(),
                    )
                )

            # 连续两页都没有窗口内的文档，说明相关度排序已经跑到旧文档，停
            stale_pages = 0 if fresh else stale_pages + 1
            if stale_pages >= 2:
                break
            if len(docs) < PAGE_SIZE:
                break

        return hits

    def search_title(self, title: str) -> list[SearchHit]:
        """不带时间过滤地按标题检索第 1 页，用于给孤立附件找回所属详情页。"""
        if not title:
            return []
        data = self._query(title, page=1, days=0)
        hits: list[SearchHit] = []
        for item in data.get("resultDocs") or []:
            raw = item.get("data") or {}
            url = (raw.get("url") or "").strip()
            if not url:
                continue
            api_date = (raw.get("docDate") or "").strip()
            hits.append(
                SearchHit(
                    title=(raw.get("titleO") or "").strip(),
                    url=url,
                    doc_date=date_from_url(url) or api_date,
                    site_name=((raw.get("siteLabel") or {}).get("value") or "").strip(),
                    file_type=(raw.get("fileType") or "").strip(),
                )
            )
        return hits

    def fetch_recent(self, days: int, keywords: Optional[Iterable[str]] = None) -> list[SearchHit]:
        """多关键词并集，按 URL 去重。"""
        kws = list(keywords or self.keywords)
        merged: dict[str, SearchHit] = {}

        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

        for idx, kw in enumerate(kws, 1):
            got = self.fetch_keyword(kw, days=days)
            narrow = len(got)
            # 补充一趟不带时间过滤的宽查询，兜住被平台日期字段漏掉的文档
            got += self.fetch_keyword_wide(kw, cutoff=cutoff)
            new = 0
            for hit in got:
                if hit.url not in merged:
                    merged[hit.url] = hit
                    new += 1
            logger.info(
                f"[so-gov] {idx}/{len(kws)} kw={kw}: narrow={narrow} "
                f"with_wide={len(got)} new={new} total={len(merged)}"
            )

        logger.info(
            f"[so-gov] done: {len(merged)} unique documents, {self.request_count} requests"
        )
        return list(merged.values())
