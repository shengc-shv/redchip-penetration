"""交叉验证器测试：披露口径 ↔ 工商登记口径。"""

import pytest

from redchip.config import reload_settings
from redchip.graph.store import Graph
from redchip.models.schema import CompanyReport, EntityKind, Jurisdiction, OwnEdge
from redchip.pipeline import run as pipeline
from redchip.verify.crosscheck import crosscheck
from redchip.verify.shareholders import (
    DisclosedHolder,
    extract_disclosed_holders,
    is_complete_sum,
    sum_check,
)

SAMPLE = (
    "主要股东权益。根据《证券及期货条例》第XV部，以下人士于本公司股份中拥有须申报的权益或淡仓："
    "马化腾先生（54.29%）；张志东先生（22.86%）；陈一丹先生（11.43%）；许晨晔先生（11.42%）。"
    "合计 100.00%。上述权益包括透过受控法团及离岸控股平台持有的权益。"
)


def test_extract_filters_summary_rows():
    """「合计/total」等汇总行不应被当成具体股东。"""
    holders = extract_disclosed_holders(SAMPLE, source_page="89")
    names = [h.name for h in holders]
    assert "马化腾" in names
    assert all("合计" not in n and "Total" not in n for n in names)
    assert len(holders) == 4


def test_extract_keeps_source_page():
    """提取结果应保留页码溯源。"""
    holders = extract_disclosed_holders(SAMPLE, source_page="89")
    assert all(h.source_page == "89" for h in holders)


def test_sum_check_passes_and_flags():
    """合计 100% 通过，95% 应判不完整。"""
    holders = [DisclosedHolder(name=f"p{i}", share_pct=p) for i, p in enumerate((54.29, 22.86, 11.43, 11.42))]
    assert is_complete_sum(sum_check(holders)) is True
    assert is_complete_sum(sum_check(holders[:2])) is False


def _sample_hits() -> list[dict]:
    """构造股东权益章节的 FTS 命中（模拟 stage_crosscheck 的定向召回）。"""
    return [
        {
            "page_no": 89,
            "score": 3,
            "keywords": ["主要股东权益"],
            "snippet": SAMPLE,
        }
    ]


def _graph_with_registry() -> Graph:
    """构造含工商登记口径股东的图谱。"""
    g = Graph()
    opco = g.add_company("深圳市测试运营有限公司", Jurisdiction.CN, EntityKind.OPCO, credit_code="R1")
    for name, pct in (("马化腾", 54.29), ("张志东", 22.86), ("陈一丹", 11.43), ("许晨晔", 11.42)):
        person = g.add_person(name)
        g.add_own(OwnEdge(from_id=person.id, to_id=opco.id, share_pct=pct))
    return g


def test_crosscheck_passes_when_consistent():
    """披露与登记一致时应通过，不改写复核状态。"""
    report = CompanyReport(code="00700", name="测试")
    cc = crosscheck(report, _graph_with_registry(), _sample_hits(), None)
    assert cc.passed is True
    assert cc.issues == []
    assert len(cc.disclosed) == 4
    assert report.needs_review is False


def test_crosscheck_flags_pct_mismatch():
    """披露与登记比例偏差超容差应记 error 并拉低一致性得分。"""
    g = _graph_with_registry()
    # 把登记口径的某个比例改小，模拟不一致
    for edge in g.owns:
        if edge.share_pct == 54.29:
            edge.share_pct = 40.0
    report = CompanyReport(code="00700", name="测试")
    cc = crosscheck(report, g, _sample_hits(), None)
    assert cc.passed is False
    assert any(i.type == "pct_mismatch" for i in cc.issues)
    assert report.needs_review is True
    assert any("交叉验证" in r for r in report.review_reasons)


def test_crosscheck_offshore_holder_is_info_only():
    """披露中有而登记无（离岸层股东）应为 info 级，不影响通过状态。"""
    g = _graph_with_registry()
    report = CompanyReport(code="00700", name="测试")
    hits = [
        {
            "page_no": 89,
            "score": 3,
            "keywords": ["主要股东权益"],
            "snippet": "主要股东权益：王离岸（30.00%）为透过受控法团持有之权益；马化腾先生（54.29%）。",
        }
    ]
    cc = crosscheck(report, g, hits, None)
    assert cc.passed is True
    assert any(i.type == "holder_only_in_disclosure" for i in cc.issues)
    assert cc.disclosed and cc.disclosed[0].source_page == "89"


def test_sum_mismatch_flags_registry_incompleteness():
    """登记股东合计不足 100% 应记 error。"""
    g = Graph()
    opco = g.add_company("X公司", Jurisdiction.CN, EntityKind.OPCO, credit_code="X1")
    person = g.add_person("甲")
    g.add_own(OwnEdge(from_id=person.id, to_id=opco.id, share_pct=70.0))
    report = CompanyReport(code="00700", name="测试")
    cc = crosscheck(report, g, [], None)
    assert cc.passed is False
    assert any(i.type == "sum_mismatch" for i in cc.issues)


def test_pipeline_crosscheck_end_to_end(monkeypatch, tmp_path):
    """离线流水线应产出交叉验证结论（披露 4 条，与登记一致）。"""
    monkeypatch.setenv("REDCHIP_MOCK", "true")
    monkeypatch.setenv("REDCHIP_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("REDCHIP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    reload_settings()
    reports = pipeline.run_batch(["00700"], names={"00700": "腾讯控股"}, keywords={"00700": ["腾讯"]})
    report = reports[0]
    assert report.crosscheck, "应写入交叉验证结果"
    assert len(report.crosscheck.get("disclosed", [])) == 4
    assert report.crosscheck.get("passed") is True


@pytest.mark.parametrize("text", ["无股东信息的普通段落，不含比例。"])
def test_extract_no_hit(text: str):
    """无比例的文本不应产生误报。"""
    assert extract_disclosed_holders(text) == []
