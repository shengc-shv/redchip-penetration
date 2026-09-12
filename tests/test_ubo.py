"""UBO 穿透算法测试。"""

from redchip.graph.store import Graph
from redchip.graph.ubo import penetrate_ubo
from redchip.models.schema import (
    ControlEdge,
    EntityKind,
    Jurisdiction,
    OwnEdge,
    PersonNode,
)


def _build_chain() -> Graph:
    """构造「自然人 → 开曼上市主体 → 香港公司 → 境内 WFOE」的典型红筹链条。"""
    g = Graph()
    listed = g.add_company("Holdings Ltd", Jurisdiction.CAYMAN, EntityKind.LISTED)
    hk = g.add_company("HK Sub Ltd", Jurisdiction.HK, EntityKind.HK)
    wfoe = g.add_company("境内WFOE有限公司", Jurisdiction.CN, EntityKind.WFOE, credit_code="CODE1")
    g.add_own(OwnEdge(from_id=listed.id, to_id=hk.id, share_pct=100.0))
    g.add_own(OwnEdge(from_id=hk.id, to_id=wfoe.id, share_pct=80.0))
    # 自然人位于离岸层之上，需经三级链条累乘
    for name, pct in (("甲", 60.0), ("乙", 40.0)):
        person = g.add_person(name)
        g.add_own(OwnEdge(from_id=person.id, to_id=listed.id, share_pct=pct))
    return g


def test_cumulative_share_is_multiplied():
    """累计持股应为逐层相乘：100% × 80% × 60% = 48%。"""
    g = _build_chain()
    ubos = penetrate_ubo(g, "CODE1", min_share=0.25)
    jia = next(u for u in ubos if u.name == "甲")
    assert abs(jia.cumulative_share - 0.48) < 1e-6


def test_threshold_filters_small_holders():
    """低于阈值的自然人不应判定为 UBO。"""
    g = _build_chain()
    ubos = penetrate_ubo(g, "CODE1", min_share=0.25)
    yi = next(u for u in ubos if u.name == "乙")
    assert abs(yi.cumulative_share - 0.32) < 1e-6
    ubos_high = penetrate_ubo(g, "CODE1", min_share=0.40)
    assert [u.name for u in ubos_high] == ["甲"]


def test_cross_holding_does_not_loop_forever():
    """交叉持股必须被路径内环路保护拦截。"""
    g = Graph()
    a = g.add_company("A公司", Jurisdiction.CN, EntityKind.OPCO, credit_code="A")
    b = g.add_company("B公司", Jurisdiction.CN, EntityKind.OPCO, credit_code="B")
    g.add_own(OwnEdge(from_id=a.id, to_id=b.id, share_pct=50.0))
    g.add_own(OwnEdge(from_id=b.id, to_id=a.id, share_pct=50.0))
    assert penetrate_ubo(g, "A", max_depth=20) == []


def test_vie_path_is_flagged():
    """经过协议控制边的路径应标记 reached_via_vie。"""
    g = Graph()
    wfoe = g.add_company("WFOE", Jurisdiction.CN, EntityKind.WFOE, credit_code="W")
    opco = g.add_company("OPCO", Jurisdiction.CN, EntityKind.OPCO, credit_code="O")
    g.add_control(
        ControlEdge(from_id=wfoe.id, to_id=opco.id, contract_type="独家服务协议")
    )
    # 自然人持有 WFOE 股权，从 OPCO 出发须先经协议控制边才能到达该自然人
    person = g.add_person("丙")
    g.add_own(OwnEdge(from_id=person.id, to_id=wfoe.id, share_pct=70.0))
    ubos = penetrate_ubo(g, "O", min_share=0.25)
    assert ubos and ubos[0].name == "丙"
    # 协议控制视同 100% 经济权益，故丙为 70%
    assert abs(ubos[0].cumulative_share - 0.70) < 1e-6
    assert ubos[0].reached_via_vie is True


def test_diamond_path_is_not_pruned():
    """菱形持股（同一自然人经两条路径）应保留持股比例最高的一条。"""
    g = Graph()
    top = g.add_company("TOP", Jurisdiction.CN, EntityKind.OPCO, credit_code="T")
    m1 = g.add_company("M1", Jurisdiction.CN, EntityKind.OPCO, credit_code="M1")
    m2 = g.add_company("M2", Jurisdiction.CN, EntityKind.OPCO, credit_code="M2")
    person = g.add_person("丁")
    g.add_own(OwnEdge(from_id=m1.id, to_id=top.id, share_pct=60.0))
    g.add_own(OwnEdge(from_id=m2.id, to_id=top.id, share_pct=40.0))
    g.add_own(OwnEdge(from_id=person.id, to_id=m1.id, share_pct=50.0))
    g.add_own(OwnEdge(from_id=person.id, to_id=m2.id, share_pct=90.0))
    ubos = penetrate_ubo(g, "T", min_share=0.1)
    assert len(ubos) == 1
    assert abs(ubos[0].cumulative_share - 0.36) < 1e-6  # max(60%×50%, 40%×90%)


def test_person_node_kind():
    """自然人节点类型应固定为 person。"""
    g = Graph()
    p = g.add_person("张三")
    assert isinstance(p, PersonNode)
    assert p.kind == "person"
