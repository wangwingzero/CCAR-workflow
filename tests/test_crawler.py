import unittest

from src.crawler import (
    CaacCrawler,
    Document,
    _looks_like_waf_block,
    find_detail_hit_for_attachment,
    generate_filename,
    is_attachment_url,
    parse_detail_metadata,
    strip_attachment_extension,
)
from src.search_api import SearchHit


STANDARD_DETAIL_HTML = """
<div class="content_nav">
  <ul id="li_ztfl"><li class="content_nav_left special"><b>主题分类：</b>行业标准</li></ul>
  <ul>
    <li class="content_nav_left" id="libwdw"><b>办文单位：</b>航空器适航审定司</li>
    <li id="lifwrq"><b>发文日期：</b>2026-10-23</li>
    <li id="lichengwen" style="display:none"><b>成文日期：</b>2026-10-23</li>
  </ul>
  <ul>
    <li id="lifwrq2" style="display:none"><b>发文日期：</b>2026-10-23</li>
    <li id="liyouxiao2" style="display:none"><b>有效性：</b>有效</li>
  </ul>
  <ul>
    <li class="content_nav_left"><b>名称：</b></li><li>航班唯一飞行标识符数据规范</li>
  </ul>
  <ul><li id="normWenHao" style="display:none"><b>字号：</b></li></ul>
  <ul>
    <li class="content_nav_left" id="liwenhao"><b>文号：</b>MH/T 4067-2026</li>
    <li id="liyouxiao"><b>有效性：</b>有效</li>
  </ul>
  <ul id="ulBuhao" style="display:none">
    <li class="content_nav_left"><b><span id="CCAR"></span>部号：</b></li>
  </ul>
</div>
"""

REGULATION_DETAIL_HTML = """
<div class="other_wrapper">
  <ul>
    <li class="content_nav_left" id="libwdw"><b>办文单位：</b>政策法规司</li>
    <li id="lifwrq"><b>发文日期：</b>2026-07-01</li>
  </ul>
  <ul>
    <li class="content_nav_left" id="liwenhao" style="display: none;"><b>文号：</b>交通运输部令2026年第14号</li>
    <li class="content_nav_left" id="liyouxiao"><b>有效性：</b>有效</li>
  </ul>
  <ul id="ulBuhao">
    <li class="content_nav_left"><b><span id="CCAR">CCAR</span>部号：</b>CCAR-290-R4</li>
  </ul>
</div>
"""


class DetailMetadataParsingTests(unittest.TestCase):
    def test_standard_page_yields_doc_number_and_validity(self):
        metadata = parse_detail_metadata(STANDARD_DETAIL_HTML, "15")

        self.assertEqual("MH/T 4067-2026", metadata["doc_number"])
        self.assertEqual("有效", metadata["validity"])
        self.assertEqual("航空器适航审定司", metadata["office_unit"])
        self.assertEqual("2026-10-23", metadata["publish_date"])
        self.assertEqual("2026-10-23", metadata["sign_date"])

    def test_regulation_page_prefers_ccar_part_number(self):
        metadata = parse_detail_metadata(REGULATION_DETAIL_HTML, "13")

        self.assertEqual("CCAR-290-R4", metadata["doc_number"])
        self.assertEqual("有效", metadata["validity"])
        self.assertEqual("政策法规司", metadata["office_unit"])

    def test_page_without_metadata_returns_empty(self):
        self.assertEqual({}, parse_detail_metadata("<html><body>无</body></html>", "15"))
        self.assertEqual({}, parse_detail_metadata("", "15"))

    def test_filename_uses_parsed_doc_number(self):
        document = Document(
            title="航班唯一飞行标识符数据规范",
            url="http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/t20261109_232033.html",
            category="标准规范",
            category_id="15",
            doc_number="",
            office_unit="",
        )
        for field_name, value in parse_detail_metadata(STANDARD_DETAIL_HTML, "15").items():
            setattr(document, field_name, value)

        self.assertEqual("MH_T 4067-2026航班唯一飞行标识符数据规范.pdf", generate_filename(document))


class AttachmentUrlTests(unittest.TestCase):
    def test_detects_attachment_urls(self):
        self.assertTrue(is_attachment_url(
            "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/P020261109628802830918.pdf"
        ))
        self.assertTrue(is_attachment_url("http://www.caac.gov.cn/a/b/file.DOCX?x=1"))

    def test_detail_pages_are_not_attachments(self):
        self.assertFalse(is_attachment_url(
            "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/t20261109_232033.html"
        ))
        self.assertFalse(is_attachment_url(""))

    def test_needs_detail_metadata_only_for_bare_13_14_15_pages(self):
        bare_standard = Document(
            title="t", url="http://x/XXGK/XXGK/BZGF/t1_1.html", category="标准规范",
            category_id="15", doc_number="", office_unit="",
        )
        numbered_standard = Document(
            title="t", url="http://x/XXGK/XXGK/BZGF/t1_2.html", category="标准规范",
            category_id="15", doc_number="MH/T 1-2026", office_unit="",
        )
        notice = Document(
            title="t", url="http://x/XXGK/XXGK/TZTG/t1_3.html", category="通知公告",
            category_id="9", doc_number="", office_unit="",
        )

        self.assertTrue(CaacCrawler.needs_detail_metadata(bare_standard))
        self.assertFalse(CaacCrawler.needs_detail_metadata(numbered_standard))
        self.assertFalse(CaacCrawler.needs_detail_metadata(notice))


HYBZ_DIRECTORY = "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611"


def make_hit(title, url):
    return SearchHit(title=title, url=url, doc_date="2026-11-09", site_name="", file_type="")


class FakeTitleSearcher:
    def __init__(self, hits_by_title):
        self.hits_by_title = hits_by_title
        self.searched_titles = []

    def search_title(self, title):
        self.searched_titles.append(title)
        return self.hits_by_title.get(title, [])


class OrphanAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.attachment_hit = make_hit(
            "航空5G AeroMACS地面终端技术要求.pdf", f"{HYBZ_DIRECTORY}/P020261109629772719885.pdf"
        )
        self.detail_hit = make_hit(
            "航空5G AeroMACS地面终端技术要求", f"{HYBZ_DIRECTORY}/t20261109_232034.html"
        )

    def test_strip_attachment_extension(self):
        self.assertEqual("标准A", strip_attachment_extension("标准A.pdf"))
        self.assertEqual("标准A", strip_attachment_extension("标准A"))

    def test_matches_detail_page_in_same_directory(self):
        comment_attachment = make_hit(
            "附件10：航空5G AeroMACS地面终端技术要求（征求意见稿）.pdf",
            "http://www.caac.gov.cn/HDJL/YJZJ/202511/P020251104612414641962.pdf",
        )
        candidates = [comment_attachment, self.attachment_hit, self.detail_hit]

        self.assertIs(self.detail_hit, find_detail_hit_for_attachment(self.attachment_hit, candidates))

    def test_ignores_detail_page_in_other_directory(self):
        other_directory_hit = make_hit(
            "航空5G AeroMACS地面终端技术要求",
            "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202610/t20261009_1.html",
        )
        self.assertIsNone(find_detail_hit_for_attachment(self.attachment_hit, [other_directory_hit]))

    def test_detail_already_in_hits_needs_no_lookup(self):
        searcher = FakeTitleSearcher({})
        resolved, outcomes = CaacCrawler()._resolve_orphan_attachments(
            [self.attachment_hit], [self.detail_hit], searcher
        )

        self.assertEqual([], resolved)
        self.assertEqual(1, outcomes["already_listed"])
        self.assertEqual([], searcher.searched_titles)

    def test_title_lookup_recovers_missing_detail_page(self):
        searcher = FakeTitleSearcher({
            "航空5G AeroMACS地面终端技术要求": [self.attachment_hit, self.detail_hit],
        })
        resolved, outcomes = CaacCrawler()._resolve_orphan_attachments(
            [self.attachment_hit], [], searcher
        )

        self.assertEqual([self.detail_hit], resolved)
        self.assertEqual(1, outcomes["detail_found"])

    def test_unresolved_attachment_is_kept_without_extension(self):
        resolved, outcomes = CaacCrawler()._resolve_orphan_attachments(
            [self.attachment_hit], [], FakeTitleSearcher({})
        )

        self.assertEqual(1, outcomes["kept_attachment"])
        self.assertEqual("航空5G AeroMACS地面终端技术要求", resolved[0].title)
        self.assertTrue(resolved[0].url.endswith(".pdf"))

    def test_supporting_file_attachments_are_skipped(self):
        supporting_file = make_hit("附件1：意见反馈表.pdf", f"{HYBZ_DIRECTORY}/P0202611090001.pdf")
        searcher = FakeTitleSearcher({})
        resolved, outcomes = CaacCrawler()._resolve_orphan_attachments([supporting_file], [], searcher)

        self.assertEqual([], resolved)
        self.assertEqual(1, outcomes["skipped"])
        self.assertEqual([], searcher.searched_titles)


class WafDetectionTests(unittest.TestCase):
    def test_detects_caac_cloud_protection_page(self):
        html = """
        <html>
          <head><title>502 Bad Gateway</title></head>
          <body>
            <p>您的IP: 62.243.189.100</p>
            <p>网站访问者 云防护节点 源站服务器</p>
            <p>Event ID: abcdef.waf</p>
          </body>
        </html>
        """

        self.assertTrue(_looks_like_waf_block(html))

    def test_normal_search_page_is_not_waf_block(self):
        html = """
        <html>
          <body>
            <table class="t_table">
              <tr><td><a href="/XXGK/XXGK/MHGZ/202606/t20260618.html">公共航空运输企业经营许可规定</a></td></tr>
            </table>
          </body>
        </html>
        """

        self.assertFalse(_looks_like_waf_block(html))


class UrlResolutionTests(unittest.TestCase):
    DETAIL_URL = "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/t20261109_232033.html"

    def setUp(self):
        self.crawler = CaacCrawler()

    def test_protocol_relative_link_uses_page_scheme(self):
        self.assertEqual(
            "http://www.caac.gov.cn/a.pdf",
            self.crawler._build_full_url("//www.caac.gov.cn/a.pdf", self.DETAIL_URL),
        )

    def test_query_only_link_keeps_detail_path(self):
        self.assertEqual(
            f"{self.DETAIL_URL}?download=1",
            self.crawler._build_full_url("?download=1", self.DETAIL_URL),
        )

    def test_parent_directory_link_is_resolved(self):
        self.assertEqual(
            "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202610/P1.pdf",
            self.crawler._build_full_url("../202610/P1.pdf", self.DETAIL_URL),
        )

    def test_non_http_scheme_is_rejected(self):
        self.assertEqual("", self.crawler._build_full_url("javascript:void(0)", self.DETAIL_URL))
        self.assertEqual("", self.crawler._build_full_url("#top", self.DETAIL_URL))


class BinaryValidationTests(unittest.TestCase):
    def setUp(self):
        self.crawler = CaacCrawler()

    def test_valid_pdf_is_accepted(self):
        self.assertTrue(self.crawler._is_valid_binary(b"%PDF-1.7" + b"\x00" * 2000, ".pdf"))

    def test_html_disguised_as_pdf_is_rejected(self):
        body = b"<html><body>WAF challenge</body></html>" + b" " * 2000
        self.assertFalse(self.crawler._is_valid_binary(body, ".pdf"))

    def test_html_disguised_as_zip_is_rejected(self):
        body = b"<!DOCTYPE html><html><body>error</body></html>" + b" " * 2000
        self.assertFalse(self.crawler._is_valid_binary(body, ".zip"))

    def test_valid_docx_is_accepted(self):
        body = b"PK\x03\x04" + b"\x00" * 2000
        self.assertTrue(self.crawler._is_valid_binary(body, ".docx"))

    def test_tiny_response_is_rejected(self):
        self.assertFalse(self.crawler._is_valid_binary(b"%PDF-1.7", ".pdf"))


class AttachmentLinkScoringTests(unittest.TestCase):
    DETAIL_URL = "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/t20261109_232033.html"

    def setUp(self):
        self.crawler = CaacCrawler()

    def test_prefers_same_directory_attachment_over_related_pdf(self):
        from bs4 import BeautifulSoup

        html = """
        <div class="related">
          <a href="/XXGK/XXGK/BZGF/HYBZ/202501/P0202501010001.pdf">相关链接：旧标准</a>
        </div>
        <div class="article">
          <p>附件：<a href="P020261109628802830918.pdf">点击下载</a></p>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")

        link = self.crawler._find_attachment_link(soup, self.DETAIL_URL)

        self.assertEqual(
            "http://www.caac.gov.cn/XXGK/XXGK/BZGF/HYBZ/202611/P020261109628802830918.pdf",
            link,
        )

    def test_returns_empty_without_attachment_links(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<p>无附件</p>", "lxml")
        self.assertEqual("", self.crawler._find_attachment_link(soup, self.DETAIL_URL))


if __name__ == "__main__":
    unittest.main()
