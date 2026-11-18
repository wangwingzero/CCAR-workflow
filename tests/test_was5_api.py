"""was5 栏目列表解析与抓取的单元测试。"""

import unittest
from datetime import date as _DATE
from unittest import mock

from src.was5_api import (
    Was5Item,
    Was5ListFetcher,
    normalize_date,
    normalize_doc_url,
    parse_list_rows,
)


def _row_13(title, href, date_text, doc_number, validity, office):
    return f"""
    <tr>
      <td>1</td>
      <td style="width:350px" class="t_l">
        <a href="{href}" target="_blank" name="{title}">{title}</a>
        <div class="t_l_content">
          <ul>
            <li class="t_l_content_left"><b>办文单位：</b>{office}</li>
            <li><b>发文日期：</b>{date_text}</li>
          </ul>
          <ul>
            <li class="t_l_content_left"><b>CCAR部号：</b>{doc_number}</li>
            <li><b>有&nbsp;&nbsp;效&nbsp;&nbsp;性：</b>{validity}</li>
          </ul>
        </div>
      </td>
      <td>{doc_number}</td>
      <td><span class="">{validity}</span></td>
    </tr>
    """


def _row_14(title, href, date_text, doc_number, validity, office):
    return f"""
    <tr>
      <td>1</td>
      <td style="width:30%" class="t_l tdMC">
        <a href="{href}" target="_blank" name="{title}">{title}</a>
        <div class="t_l_content">
          <ul>
            <li class="t_l_content_left"><b>办文单位：</b>{office}</li>
            <li><b>发文日期：</b>{date_text}</li>
          </ul>
          <ul>
            <li class="t_l_content_left"><b>文&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;号：</b>{doc_number}</li>
            <li><b>有&nbsp;&nbsp;效&nbsp;&nbsp;性：</b>{validity}</li>
          </ul>
        </div>
      </td>
      <td class="tdRQ">{date_text}</td>
      <td class="strFL">{doc_number}</td>
      <td class="strGF"><span class="">{validity}</span></td>
    </tr>
    """


def _page(*rows):
    return (
        "<html><body><table class='t_table'><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )


class NormalizeTests(unittest.TestCase):
    def test_normalize_date_variants(self):
        self.assertEqual("2026-10-23", normalize_date("2026年10月23日"))
        self.assertEqual("2026-10-23", normalize_date("2026-10-23"))
        self.assertEqual("2026-01-05", normalize_date("2026/1/5"))
        self.assertEqual("", normalize_date(""))
        self.assertEqual("", normalize_date("2026年13月40日"))

    def test_normalize_doc_url_keeps_http_scheme_for_state_dedupe(self):
        # The state file stores http:// URLs, so the scheme must not be upgraded
        # (a rewrite would make every was5 row look like a new document).
        expected = "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202607/t20260717_231321.html"
        self.assertEqual(expected, normalize_doc_url(expected))
        self.assertEqual(
            expected,
            normalize_doc_url(
                "https://www.caac.gov.cn/XXGK/XXGK/MHGZ/202607/t20260717_231321.html"
            ),
        )
        self.assertEqual(
            expected,
            normalize_doc_url("http://caac.gov.cn/XXGK/XXGK/MHGZ/202607/t20260717_231321.html"),
        )
        self.assertEqual("", normalize_doc_url(""))
        self.assertEqual("https://other.test/a", normalize_doc_url("https://other.test/a"))


class ParseListRowsTests(unittest.TestCase):
    def test_parses_13_rows_with_ccar_number(self):
        html = _page(
            _row_13(
                "通用航空经营管理规定",
                "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202607/t20260717_231321.html",
                "2026年07月01日",
                "CCAR-290-R4",
                "有效",
                "政策法规司",
            )
        )

        items = parse_list_rows(html, "13")

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("通用航空经营管理规定", item.title)
        self.assertEqual("2026-07-01", item.publish_date)
        self.assertEqual("CCAR-290-R4", item.doc_number)
        self.assertEqual("有效", item.validity)
        self.assertEqual("政策法规司", item.office_unit)
        self.assertEqual("13", item.category_id)
        self.assertTrue(item.url.startswith("http://www.caac.gov.cn/"))

    def test_parses_14_rows_with_document_number(self):
        html = _page(
            _row_14(
                "关于加强民航专业工程施工工期管理的通知",
                "http://www.caac.gov.cn/XXGK/XXGK/GFXWJ/202610/t20261027_231948.html",
                "2026年10月23日",
                "民航规〔2026〕24 号",
                "有效",
                "机场司",
            )
        )

        items = parse_list_rows(html, "14")

        self.assertEqual(1, len(items))
        self.assertEqual("民航规〔2026〕24 号", items[0].doc_number)
        self.assertEqual("机场司", items[0].office_unit)
        self.assertEqual("2026-10-23", items[0].publish_date)
        self.assertEqual("14", items[0].category_id)

    def test_unknown_category_or_empty_html_returns_nothing(self):
        html = _page(_row_13("t", "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/t1.html", "2026年07月01日", "", "", ""))

        self.assertEqual([], parse_list_rows(html, "9"))
        self.assertEqual([], parse_list_rows("", "13"))
        self.assertEqual([], parse_list_rows("<html><body>502 Bad Gateway</body></html>", "13"))

    def test_duplicate_urls_are_dropped(self):
        row = _row_13(
            "同一篇",
            "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202607/t20260717_231321.html",
            "2026年07月01日",
            "",
            "有效",
            "",
        )

        self.assertEqual(1, len(parse_list_rows(_page(row, row), "13")))


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url, params=None):
        self.requests.append(dict(params or {}))
        page = int((params or {}).get("page", 1))
        return FakeResponse(self.pages.get(page, "<html></html>"))

    def close(self):
        pass


class FetchRecentTests(unittest.TestCase):
    def _fetcher(self, pages):
        fetcher = Was5ListFetcher(min_delay=0, max_delay=0)
        fetcher.close()
        fetcher._client = FakeClient(pages)
        return fetcher

    def test_filters_old_rows_and_stops_on_older_page(self):
        fresh = _page(
            *[
                _row_13(
                    f"新规定{i}",
                    f"http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202611/t2026111{i}_1.html",
                    "2026年11月14日",
                    "CCAR-1",
                    "有效",
                    "政策法规司",
                )
                for i in range(10)
            ]
        )
        older = _page(
            _row_13(
                "旧规定",
                "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202501/t20250110_2.html",
                "2025年01月10日",
                "",
                "有效",
                "",
            )
        )
        fetcher = self._fetcher({1: fresh, 2: older})

        with mock.patch("src.was5_api._today", return_value=_DATE(2026, 11, 18)):
            items = fetcher.fetch_recent("13", days=7)

        self.assertEqual(10, len(items))
        self.assertTrue(all(item.publish_date == "2026-11-14" for item in items))
        # The second page is entirely outside the window, so paging stops there.
        self.assertEqual(["1", "2"], [request["page"] for request in fetcher._client.requests])

    def test_paginates_while_pages_are_fresh(self):
        full_first = _page(
            *[
                _row_13(
                    f"规定{i}",
                    f"http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202611/t2026111{i}_1.html",
                    "2026年11月11日",
                    "",
                    "有效",
                    "",
                )
                for i in range(10)
            ]
        )
        short_second = _page(
            _row_13(
                "规定X",
                "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/202611/t20261112_2.html",
                "2026年11月12日",
                "",
                "有效",
                "",
            )
        )
        fetcher = self._fetcher({1: full_first, 2: short_second})

        with mock.patch("src.was5_api._today", return_value=_DATE(2026, 11, 18)):
            items = fetcher.fetch_recent("13", days=30)

        self.assertEqual(11, len(items))
        self.assertEqual(2, len(fetcher._client.requests))

    def test_unsupported_category_makes_no_request(self):
        fetcher = self._fetcher({})

        self.assertEqual([], fetcher.fetch_recent("9", days=7))
        self.assertEqual([], fetcher._client.requests)

    def test_http_error_is_reported(self):
        class ErrorClient(FakeClient):
            def get(self, url, params=None):
                return FakeResponse("", status_code=502)

        fetcher = self._fetcher({})
        fetcher._client = ErrorClient({})

        with self.assertRaises(RuntimeError):
            fetcher.fetch_recent("13", days=7)


class Was5ItemTests(unittest.TestCase):
    def test_defaults(self):
        item = Was5Item(title="t", url="u")

        self.assertEqual("", item.publish_date)
        self.assertEqual("", item.doc_number)


if __name__ == "__main__":
    unittest.main()
