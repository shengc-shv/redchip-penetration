"""境内穿透采集 + 广东省地域过滤。

过滤位置（方案文档 5.2）
------------------------
过滤器放在 **LLM-A 之后**：先用全量候选让 LLM 完成 WFOE 消歧，确认目标实体后再做地域过滤。
若提前过滤掉非广东候选，消歧时可能找不到正确实体。
"""

from __future__ import annotations

from dataclasses import dataclass

from redchip import config as config_mod
from redchip.domestic.cnbiz import CnbizClient, CompanyBasic, Shareholder
from redchip.graph.store import Graph
from redchip.models.schema import CandidateCompany, CompanyNode, EntityKind, Jurisdiction, OwnEdge

GUANGDONG_CITIES: frozenset[str] = frozenset(
    {
        "广州",
        "深圳",
        "珠海",
        "佛山",
        "惠州",
        "东莞",
        "中山",
        "江门",
        "肇庆",
        "汕头",
        "潮州",
        "揭阳",
        "汕尾",
        "湛江",
        "茂名",
        "阳江",
        "清远",
        "韶关",
        "梅州",
        "河源",
        "云浮",
    }
)


def is_guangdong(basic: CompanyBasic | dict[str, object] | None) -> bool:
    """判断是否属于广东省内主体。

    Args:
        basic: 企业基本信息（含 province / city）。

    Returns:
        bool: 广东省内返回 True。
    """
    if basic is None:
        return False
    if isinstance(basic, dict):
        province = str(basic.get("province") or "")
        city = str(basic.get("city") or "")
    else:
        province = basic.province or ""
        city = basic.city or ""
    return province == "广东省" or city in GUANGDONG_CITIES


@dataclass
class MatchedEntity:
    """通过地域过滤的境内实体。"""

    candidate: CandidateCompany
    basic: CompanyBasic
    credit_code: str


def collect_candidates(
    wfoe_names: list[str], client: CnbizClient, limit: int = 5
) -> list[CandidateCompany]:
    """按 LLM-A 给出的 WFOE 名称检索工商候选（免费接口）。

    Args:
        wfoe_names: WFOE 名称列表。
        client: 工商客户端。
        limit: 每个名称取多少条候选（压缩 token 的关键手段）。

    Returns:
        list[CandidateCompany]: 去重后的候选列表。
    """
    seen: set[str] = set()
    out: list[CandidateCompany] = []
    for name in wfoe_names:
        if not name:
            continue
        for cand in client.search_company(name, limit=limit):
            key = cand.credit_code or cand.name
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
    return out


def filter_guangdong(
    candidates: list[CandidateCompany], client: CnbizClient
) -> list[MatchedEntity]:
    """在 LLM-A 消歧之后执行地域过滤。

    Args:
        candidates: 全量候选。
        client: 工商客户端。

    Returns:
        list[MatchedEntity]: 广东省内的实体（保留全量候选用于审计）。
    """
    matched: list[MatchedEntity] = []
    for cand in candidates:
        if not cand.credit_code:
            continue
        basic = client.get_company_basic(cand.credit_code)
        if is_guangdong(basic):
            matched.append(
                MatchedEntity(candidate=cand, basic=basic, credit_code=cand.credit_code)
            )
    return matched


def expand_upward(
    graph: Graph,
    credit_code: str,
    client: CnbizClient,
    max_depth: int | None = None,
) -> int:
    """从境内实体向上逐层采集股东，写入图谱。

    采用 BFS 而非 DFS：层数可控，且能在超出预算时优雅停止。

    Args:
        graph: 目标图谱。
        credit_code: 起点企业的统一社会信用代码。
        client: 工商客户端。
        max_depth: 最大采集层数。

    Returns:
        int: 采集到的股东边数量。
    """
    settings = config_mod.get_settings()
    depth_limit = max_depth if max_depth is not None else settings.max_depth

    frontier: list[tuple[str, int]] = [(credit_code, 0)]
    visited: set[str] = set()
    edges_added = 0

    while frontier:
        current, depth = frontier.pop(0)
        if current in visited or depth > depth_limit:
            continue
        visited.add(current)

        for sh in client.get_shareholders(current):
            if not sh.name:
                continue
            if sh.kind == "person":
                person = graph.add_person(sh.name)
                graph.add_own(
                    OwnEdge(
                        from_id=person.id,
                        to_id=current,
                        share_pct=sh.share_pct,
                        share_raw=sh.share_raw,
                        source="cnbiz",
                    )
                )
            else:
                # 境外股东无统一社会信用代码，用「名称@注册地」作为稳定 id
                child: CompanyNode = graph.add_company(
                    name=sh.name,
                    jurisdiction=Jurisdiction.HK if _looks_offshore(sh.name) else Jurisdiction.CN,
                    kind=EntityKind.HK if _looks_offshore(sh.name) else EntityKind.UNKNOWN,
                    credit_code=sh.credit_code or None,
                )
                graph.add_own(
                    OwnEdge(
                        from_id=child.id,
                        to_id=current,
                        share_pct=sh.share_pct,
                        share_raw=sh.share_raw,
                        source="cnbiz",
                    )
                )
                if sh.credit_code and sh.credit_code not in visited:
                    frontier.append((sh.credit_code, depth + 1))
            edges_added += 1

    return edges_added


def _looks_offshore(name: str) -> bool:
    """按名称特征粗判是否为境外主体。

    Args:
        name: 企业名称。

    Returns:
        bool: 含境外特征词返回 True。
    """
    markers = ("LIMITED", "LTD", "INC", "HOLDINGS", "INTERNATIONAL", "CO.", "（HK）", "(HK)", "香港")
    upper = name.upper()
    return any(m in upper for m in markers)


def shareholders_to_text(shareholders: list[Shareholder]) -> str:
    """把股东列表渲染为可读文本（用于报告与 Prompt）。

    Args:
        shareholders: 股东列表。

    Returns:
        str: 格式化文本。
    """
    if not shareholders:
        return "（无股东数据）"
    return "\n".join(
        f"- {s.name}（{'自然人' if s.kind == 'person' else '企业'}）持股 {s.share_pct:g}%"
        for s in shareholders
    )
