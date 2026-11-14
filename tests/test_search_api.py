import unittest

from src.search_api import SearchBannedError, SoGovSearch, classify_url


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self.response


class ClassifyUrlTests(unittest.TestCase):
    def test_new_column_codes_map_to_their_categories(self):
        cases = {
            "http://www.caac.gov.cn/XXGK/XXGK/DWGX/202611/t1.html": "16",
            "http://www.caac.gov.cn/XXGK/XXGK/GATSHHZ/202611/t1.html": "17",
            "http://www.caac.gov.cn/XXGK/XXGK/GJGY/202611/t1.html": "18",
            "http://www.caac.gov.cn/XXGK/XXGK/JYTNDF/202611/t1.html": "28",
            "http://www.caac.gov.cn/XXGK/XXGK/ZFWZNDBB/202611/t1.html": "29",
        }

        for url, expected in cases.items():
            self.assertEqual(expected, classify_url(url))

    def test_unknown_column_is_unclassified(self):
        self.assertEqual(
            "0",
            classify_url("http://www.caac.gov.cn/XXGK/XXGK/UNKNOWN/202611/t1.html"),
        )


class QueryBanDetectionTests(unittest.TestCase):
    def _search(self, response: _FakeResponse) -> SoGovSearch:
        searcher = object.__new__(SoGovSearch)
        searcher._client = _FakeClient(response)
        searcher.request_count = 0
        searcher._last_request = 0.0
        searcher.min_delay = 0
        searcher.max_delay = 0
        return searcher

    def test_http_403_raises_search_banned(self):
        with self.assertRaises(SearchBannedError):
            self._search(_FakeResponse(403))._query("kw", 1, 7)

    def test_http_429_raises_search_banned(self):
        with self.assertRaises(SearchBannedError):
            self._search(_FakeResponse(429))._query("kw", 1, 7)

    def test_ip_ban_code_raises_search_banned(self):
        response = _FakeResponse(200, {"ok": False, "code": -101, "msg": "banned"})
        with self.assertRaises(SearchBannedError):
            self._search(response)._query("kw", 1, 7)

    def test_other_api_error_is_not_treated_as_ban(self):
        response = _FakeResponse(200, {"ok": False, "code": 500, "msg": "oops"})
        with self.assertRaises(RuntimeError) as raised:
            self._search(response)._query("kw", 1, 7)
        self.assertNotIsInstance(raised.exception, SearchBannedError)

    def test_fetch_keyword_propagates_ban_instead_of_stopping_quietly(self):
        with self.assertRaises(SearchBannedError):
            self._search(_FakeResponse(403)).fetch_keyword("kw", 7, max_pages=1)


if __name__ == "__main__":
    unittest.main()
