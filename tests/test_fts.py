"""FTS 中文检索测试。"""

import sqlite3

from redchip.overseas import fts as fts_mod
from redchip.overseas.hkex import PageText

PAGES = [
    PageText(page_no=1, text="本公司为于开曼群岛注册成立的获豁免有限公司，采用红筹架构。"),
    PageText(
        page_no=45,
        text="合约安排：本集团通过结构性合约控制境内运营实体深圳市腾讯计算机系统有限公司，"
        "包括独家服务协议、股权质押协议与投票权委托协议。",
    ),
    PageText(page_no=88, text="风险因素：倘中国监管机构认定结构性合约安排违反相关法律法规。"),
]


def test_trigram_matches_chinese_substring(tmp_path):
    """trigram 分词器应能命中任意中文子串。"""
    conn = fts_mod.build_index(PAGES, tmp_path / "t.sqlite")
    hits = fts_mod.search(conn, keywords=("合约安排",))
    assert any(h.page_no == 45 for h in hits)


def test_short_keyword_falls_back_to_like(tmp_path):
    """不足 3 字的关键词应回退到 LIKE 检索。"""
    conn = fts_mod.build_index(PAGES, tmp_path / "t2.sqlite")
    hits = fts_mod.search(conn, keywords=("VIE",))
    assert isinstance(hits, list)
    hits2 = fts_mod.search(conn, keywords=("红筹",))
    assert any(h.page_no == 1 for h in hits2)


def test_hits_carry_source_page_for_traceability(tmp_path):
    """命中结果必须带页码，供 LLM 溯源。"""
    conn = fts_mod.build_index(PAGES, tmp_path / "t3.sqlite")
    hits = fts_mod.search(conn, keywords=("结构性合约",))
    text = fts_mod.render_hits(hits)
    assert "[p.45]" in text or "[p.88]" in text


def test_vie_evidence_detection(tmp_path):
    """存在 VIE 关键词时应识别为有协议控制证据。"""
    conn = fts_mod.build_index(PAGES, tmp_path / "t4.sqlite")
    assert fts_mod.has_vie_evidence(conn) is True


def test_empty_pages_do_not_crash(tmp_path):
    """空文档不应导致索引或检索崩溃。"""
    conn = fts_mod.build_index([], tmp_path / "t5.sqlite")
    assert fts_mod.search(conn) == []
    assert isinstance(conn, sqlite3.Connection)
