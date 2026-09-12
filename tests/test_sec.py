"""美股模块离线测试（不发起 SEC 请求）。"""

from redchip.overseas import sec


def _submissions() -> dict:
    """构造一份精简的 submissions 响应。"""
    return {
        "filings": {
            "recent": {
                "form": ["20-F", "6-K", "20-F"],
                "filingDate": ["2025-05-20", "2026-02-10", "2026-05-20"],
                "accessionNumber": [
                    "0001193125-25-111111",
                    "0001193125-26-222222",
                    "0001193125-26-231755",
                ],
                "primaryDocument": ["a.htm", "b.htm", "baba-20260331.htm"],
            }
        }
    }


def test_pick_latest_20f():
    """应挑出最新一份 20-F，忽略 6-K。"""
    filing = sec.pick_filing(_submissions(), "0001577552", ticker="BABA", company="Alibaba")
    assert filing is not None
    assert filing.form == "20-F"
    assert filing.filing_date == "2026-05-20"
    assert filing.accession == "0001193125-26-231755"


def test_filing_url_shape():
    """申报原文 URL 应符合 SEC Archives 规范（accession 去横线、CIK 去前导零）。"""
    filing = sec.pick_filing(_submissions(), "0001577552", ticker="BABA")
    assert filing is not None
    assert filing.url == (
        "https://www.sec.gov/Archives/edgar/data/1577552/000119312526231755/baba-20260331.htm"
    )


def test_pick_filing_returns_none_when_missing():
    """没有目标表单时应返回 None 而非抛异常。"""
    assert sec.pick_filing(_submissions(), "0001577552", form="10-K") is None


def test_extract_html_pages_strips_tags_and_chunks(tmp_path):
    """HTML 应被去标签、去脚本，并按字符数分块。"""
    html = (
        "<html><head><style>body{}</style></head><body>"
        "<script>var a=1;</script>"
        "<p>Zhejiang Taobao Network Co., Ltd. is a representative VIE.</p>"
        "<p>" + ("contractual arrangements " * 400) + "</p>"
        "</body></html>"
    )
    path = tmp_path / "baba.htm"
    path.write_text(html, encoding="utf-8")

    pages = sec.extract_html_pages(path, chunk_chars=1000, max_chunks=50)
    assert pages, "应产���至少一个文本块"
    assert all("script" not in p.text.lower() for p in pages)
    assert any("Zhejiang Taobao Network" in p.text for p in pages)
    # page_no 从 1 开始递增，用于溯源
    assert [p.page_no for p in pages] == list(range(1, len(pages) + 1))


def test_identity_header_uses_configured_email():
    """User-Agent 必须携带配置中的身份串（SEC 硬性要求）。"""
    from redchip.config import get_settings

    settings = get_settings()
    assert "@" in sec._identity(settings) or "example.com" in sec._identity(settings)
