"""渲染与数据契约测试。"""

from redchip.graph import render as render_mod
from redchip.graph.store import Graph
from redchip.models.schema import (
    ControlEdge,
    EntityKind,
    Jurisdiction,
    LlmAOutput,
    OwnEdge,
)


def _graph() -> Graph:
    """构造一张含 VIE 的最小图谱。"""
    g = Graph()
    listed = g.add_company("开曼控股", Jurisdiction.CAYMAN, EntityKind.LISTED)
    wfoe = g.add_company("境内WFOE", Jurisdiction.CN, EntityKind.WFOE, credit_code="C1")
    opco = g.add_company("境内OPCO", Jurisdiction.CN, EntityKind.OPCO, credit_code="C2")
    person = g.add_person("王某")
    g.add_own(OwnEdge(from_id=listed.id, to_id=wfoe.id, share_pct=100.0))
    g.add_own(OwnEdge(from_id=person.id, to_id=opco.id, share_pct=55.0))
    g.add_control(ControlEdge(from_id=wfoe.id, to_id=opco.id, contract_type="独家服务协议"))
    return g


def test_mermaid_output_shape():
    """Mermaid 应含 graph TD、股权实线与 VIE 虚线。"""
    text = render_mod.to_mermaid(_graph())
    assert text.startswith("%%")
    assert "graph TD" in text
    assert "-->" in text
    assert "-.->|VIE" in text


def test_dot_output_is_wellformed():
    """DOT 应包含 digraph 声明与闭合括号。"""
    text = render_mod.to_dot(_graph())
    assert text.startswith("digraph")
    assert text.strip().endswith("}")
    assert "dashed" in text


def test_svg_is_rendered_without_external_deps():
    """零依赖 SVG 渲染应产出所有节点。"""
    svg = render_mod.to_svg(_graph())
    assert svg.startswith("<svg")
    for label in ("开曼控股", "境内WFOE", "境内OPCO", "王某"):
        assert label in svg


def test_same_name_same_jurisdiction_is_merged():
    """同名同注册地应复用同一节点，避免重复。"""
    g = Graph()
    a = g.add_company("同名公司", Jurisdiction.CN, EntityKind.WFOE)
    b = g.add_company("同名公司", Jurisdiction.CN, EntityKind.OPCO)
    assert a.id == b.id
    assert len(g.nodes) == 1


def test_credit_code_takes_priority_as_id():
    """有统一社会信用代码时应以其作为稳定 id。"""
    g = Graph()
    node = g.add_company("某公司", Jurisdiction.CN, EntityKind.WFOE, credit_code="911100001")
    assert node.id == "911100001"


def test_llm_a_accepts_raw_dict_items():
    """LLM 返回中嵌套 dict 应被自动归一化为模型（防 list.append 绕过校验）。"""
    payload = {
        "listed_entity": {"name": "X", "jurisdiction": "Cayman"},
        "wfoe": [{"name": "W", "credit_code": "C", "matched_candidate_index": 0}],
        "vie_contracts": [{"type": "股权质押", "party_a": "W", "party_b": "O"}],
        "confidence": 0.8,
    }
    out = LlmAOutput.model_validate(payload)
    assert out.listed_entity.name == "X"
    assert out.wfoe[0].name == "W"
    assert out.vie_contracts[0].type == "股权质押"


def test_llm_a_confidence_is_clamped():
    """confidence 超出 0~1 应被拒绝。"""
    try:
        LlmAOutput.model_validate({"confidence": 5})
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        assert "confidence" in str(exc)
    else:
        raise AssertionError("confidence=5 应校验失败")


def test_render_all_writes_files(tmp_path):
    """render_all 应写出 dot / mermaid / svg。"""
    produced = render_mod.render_all(_graph(), tmp_path)
    assert {"dot", "mermaid", "svg"} <= set(produced)
    assert (tmp_path / "penetration_graph.svg").exists()
    # 无 dot 二进制时不应报错，只是没有 png
    assert "png" not in produced or (tmp_path / "penetration_graph.png").exists()


def test_node_kinds_are_preserved():
    """节点角色应正确写入，供后续按角色筛选。"""
    g = _graph()
    kinds = {n.kind for n in g.companies()}
    assert EntityKind.LISTED in kinds
    assert EntityKind.WFOE in kinds
    assert EntityKind.OPCO in kinds
    assert isinstance(g.nodes, dict)
