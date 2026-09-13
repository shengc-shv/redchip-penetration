"""证据等级与境内反推测试。"""

from redchip.domestic.onshore import detect_onshore_signals, has_offshore_holding
from redchip.graph.store import Graph
from redchip.models.evidence import EvidenceLevel, evaluate_evidence
from redchip.models.schema import (
    CompanyReport,
    EntityKind,
    Jurisdiction,
    OwnEdge,
    UboResult,
)


def _graph(city_province: str = "广东省", holder_jurisdiction: Jurisdiction = Jurisdiction.HK) -> Graph:
    """构造「境内主体 ← 境外股东」的最小图谱。

    Args:
        city_province: 境内主体所在省份。
        holder_jurisdiction: 股东注册地。

    Returns:
        Graph: 图谱。
    """
    g = Graph()
    wfoe = g.add_company(
        "某科技（深圳）有限公司",
        Jurisdiction.CN,
        EntityKind.WFOE,
        credit_code="C1",
        province=city_province,
        city="深圳市",
    )
    holder = g.add_company("Some Tech (Hong Kong) Limited", holder_jurisdiction, EntityKind.HK)
    g.add_own(OwnEdge(from_id=holder.id, to_id=wfoe.id, share_pct=100.0, source="cnbiz"))
    return g


# ---------- 境内反推：仅广东 ----------


def test_signal_detected_for_guangdong():
    """广东省内主体的境外股东应产出反推信号。"""
    signals = detect_onshore_signals(_graph(), target_province="广东省")
    assert len(signals) == 1
    assert signals[0].entity == "某科技（深圳）有限公司"
    assert "Some Tech (Hong Kong) Limited" in signals[0].offshore_holders
    assert signals[0].verdict == "疑似红筹架构"


def test_no_signal_for_other_provinces():
    """非目标省份主体不产出信号（只做广东）。"""
    assert detect_onshore_signals(_graph("浙江省"), target_province="广东省") == []


def test_no_signal_when_holder_is_domestic():
    """股东为境内主体时不产出信号。"""
    g = Graph()
    opco = g.add_company(
        "广州某公司", Jurisdiction.CN, EntityKind.OPCO, credit_code="C2", province="广东省"
    )
    holder = g.add_company("某投资有限公司", Jurisdiction.CN, EntityKind.UNKNOWN)
    g.add_own(OwnEdge(from_id=holder.id, to_id=opco.id, share_pct=100.0))
    assert detect_onshore_signals(g, target_province="广东省") == []


def test_has_offshore_holding_helper():
    """单主体判定helper 应返回 True。"""
    g = _graph()
    assert has_offshore_holding(g, "C1") is True


# ---------- 证据等级 ----------


def _report(**kwargs: object) -> CompanyReport:
    """构造测试用报告。

    Args:
        **kwargs: 字段覆盖。

    Returns:
        CompanyReport: 报告对象。
    """
    base: dict = {"code": "TEST", "name": "测试"}
    base.update(kwargs)
    return CompanyReport(**base)


def test_level_l1_full_evidence():
    """披露 + 工商 + UBO + 交叉核验齐备 → L1。"""
    report = _report(
        doc_kind="annual_report",
        doc_url="https://example.com/a.pdf",
        guangdong_entities=["腾讯科技（深圳）有限公司"],
        owns=[OwnEdge(from_id="p1", to_id="c1", share_pct=50.0, source="cnbiz")],
        ubos=[UboResult(person_id="p1", name="张三", cumulative_share=0.5, path=["c1", "p1"])],
        crosscheck={"passed": True, "disclosed": [{"name": "张三", "share_pct": 50.0}]},
    )
    assert evaluate_evidence(report).level is EvidenceLevel.L1


def test_level_l2_partial_evidence():
    """有官方披露但缺 UBO/核验 → L2。"""
    report = _report(doc_kind="20-F", doc_url="https://sec.gov/x.htm")
    assert evaluate_evidence(report).level is EvidenceLevel.L2


def test_level_l3_onshore_inference_only():
    """无官方披露但有境内反推信号 → L3。"""
    report = _report(
        onshore_signals=[{"entity": "某科技（深圳）有限公司", "offshore_holders": ["X Limited"]}]
    )
    assessment = evaluate_evidence(report)
    assert assessment.level is EvidenceLevel.L3
    assert "境内反推信号" in " ".join(assessment.basis)


def test_level_l4_lead_only():
    """无任何证据 → L4。"""
    assert evaluate_evidence(_report()).level is EvidenceLevel.L4


def test_basis_lists_each_dimension():
    """依据需逐项列出四个维度，便于人工判断。"""
    assessment = evaluate_evidence(_report())
    joined = " ".join(assessment.basis)
    for key in ("官方披露", "工商登记", "最终受益人", "交叉核验"):
        assert key in joined
