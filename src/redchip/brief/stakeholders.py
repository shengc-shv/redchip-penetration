"""干系人作战表：从穿透图谱推导可执行的营销干系人清单。

设计原则
--------
**纯规则推导，不依赖 LLM**：未来要批量跟进新企业，干系人分类必须可复现、可审计。
输入是穿透结果（图谱节点 + UBO + 披露股东 + VIE 协议当事人），输出是作战语言。

三条业务规则（决定这张表好不好用的关键）
1. **区分决策人与名义人**：VIE 登记股东常为代持（20-F 称为 designated individuals），
   必须在表中显式标注「不作营销对象」，否则客户经理照着名单打电话首战就废了。
2. **可触达性分层**：辖内主体可直接打、非辖内需联动、离岸层需总行/境外条线——
   这直接决定分行要不要投入资源。
3. **按"切入由头"而非"产品"组织**：客户经理上门带的是理由，不是产品清单。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from redchip.graph.store import Graph
from redchip.models.schema import CompanyReport, PersonNode

# 可触达性等级：3=属地可直接触达，2=需引荐/联动，1=需总行条线，0=暂不可触达
Reach = Literal[0, 1, 2, 3]
_REACH_LABEL: dict[int, str] = {
    3: "★★★ 属地可直接触达",
    2: "★★☆ 需引荐或行内联动",
    1: "★☆☆ 需总行/跨境条线",
    0: "— 暂不可触达",
}

# 主体类型 → 作战定位（角色 / 需求落点 / 切入由头 / 可触达性）
_TYPE_RULES: dict[str, dict[str, object]] = {
    "listed": {
        "role": "上市主体",
        "needs": "跨境结算、境外账户、再融资配套",
        "hook": "上市地位与再融资安排",
        "reach": 1,
    },
    "offshore": {
        "role": "离岸中间控股层",
        "needs": "境外账户开立与资金调拨",
        "hook": "跨境资金池调拨效率",
        "reach": 0,
    },
    "hk": {
        "role": "香港中间控股层",
        "needs": "境外账户、跨境结算与外汇避险",
        "hook": "跨境结算与外汇避险",
        "reach": 1,
    },
    "wfoe": {
        "role": "境内全资主体（WFOE）",
        "needs": "结算、代发、票据、现金管理",
        "hook": "跨境资金池、供应链金融、现金管理效率",
        "reach": 3,
    },
    "opco": {
        "role": "VIE 运营实体（实际业务承载）",
        "needs": "收单、结算、经营性贷款",
        "hook": "平台商户账期融资、收单与结算效率",
        "reach": 3,
    },
    "gov": {
        "role": "国资主体",
        "needs": "结算、代发",
        "hook": "国企采购与结算配套",
        "reach": 2,
    },
    "unknown": {
        "role": "境内关联主体",
        "needs": "结算、代发",
        "hook": "关联交易结算",
        "reach": 2,
    },
}


class Stakeholder(BaseModel):
    """一位营销干系人（主体或个人）。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    category: str = Field(default="", description="境外主体 / 境内主体 / 自然人 / 生态客群")
    role: str = ""
    needs: str = ""
    hook: str = ""
    reach: Reach = 2
    region: str = ""
    priority: Literal["A", "B", "C"] = "B"
    note: str = ""

    @property
    def reach_label(self) -> str:
        """可触达性中文标签。"""
        return _REACH_LABEL.get(int(self.reach), "")


def build_stakeholders(
    report: CompanyReport,
    target_province: str = "广东省",
    disclosed_persons: list[dict[str, str]] | None = None,
) -> list[Stakeholder]:
    """从穿透结果推导干系人作战表。

    Args:
        report: 穿透结果（含节点、UBO、VIE 协议当事人、披露股东）。
        target_province: 本行属地省份，用于判定「辖内 / 非辖内」。
        disclosed_persons: **披露口径确认的自然人干系人**（如年报第XV部注释指明的
            持股平台实控人）。工商穿透不可用时（数据源故障/仅凭披露文件分析），
            UBO 穿透会空缺，此时若无本参数，披露已确认的决策人会被错误归类为
            「VIE 名义持股人」。每项至少含 ``name`` 与 ``basis``（溯源说明）。

    Returns:
        list[Stakeholder]: 按优先级与可触达性排序的干系人列表。
    """
    graph = Graph()
    for node in report.nodes:
        graph.nodes[node.id] = node
    for edge in report.owns:
        graph.add_own(edge)
    for edge in report.controls:
        graph.add_control(edge)

    ubo_ids = {u.person_id for u in report.ubos}
    # VIE 协议里的登记股东：常态为代持，需与真实受益人区分
    vie_nominees = {
        name
        for contract in report.llm_a.vie_contracts
        for name in contract.shareholders
        if name
    }
    disclosed_names = {h.get("name", "") for h in report.crosscheck.get("disclosed", [])}

    # 披露口径确认的决策人：优先级高于图谱推导（它们有法定披露文件背书）
    disclosed_map = {
        str(item.get("name", "")): item
        for item in (disclosed_persons or [])
        if item.get("name")
    }

    rows: list[Stakeholder] = []

    def _disclosed_row(name: str, item: dict[str, str]) -> Stakeholder:
        """构造披露口径决策人行。"""
        return Stakeholder(
            name=name,
            category="自然人",
            role="最终决策人（披露口径确认）",
            needs="私人银行、家族财富安排",
            hook="上市主体权益安排与个人财富管理",
            reach=2,
            priority="B",
            note=(
                f"依据：{item.get('basis', '公开披露文件')}；"
                "境内工商登记口径待行内渠道核验"
            ),
        )

    for node in report.nodes:
        if isinstance(node, PersonNode):
            disclosed = disclosed_map.get(node.name)
            if disclosed is not None:
                # 有披露文件背书的决策人：按披露口径出行，不做穿透推断
                rows.append(_disclosed_row(node.name, disclosed))
                continue
            rows.append(
                _person_row(
                    node,
                    node.id in ubo_ids,
                    node.name in vie_nominees,
                    disclosed_names,
                    is_us=report.market.value == "us",
                )
            )
            continue

        kind = getattr(node.kind, "value", str(node.kind))
        rule = _TYPE_RULES.get(kind)
        if rule is None:
            continue
        region = node.city or node.province or ""
        # 属地三态：True=辖内 / False=明确非辖内 / None=未知（工商口径缺失时常见）。
        # 未知不能按「非辖内」处理——那会把辖内主体错误降档，误导作战优先级
        if node.province:
            in_region: bool | None = node.province == target_province
        elif kind in ("wfoe", "opco", "gov", "unknown"):
            in_region = None
        else:
            in_region = False
        reach = int(rule["reach"])  # type: ignore[arg-type]
        if reach == 3 and in_region is False:
            # 明确的境内非辖内主体：可触达性降一档
            reach = 2
        if kind in ("listed", "offshore", "hk"):
            note = ""
        elif in_region is True:
            note = "辖内，可独立承接"
        elif in_region is False:
            note = "非辖内，须属地联动"
        else:
            note = "属地待核验（工商登记口径缺失，建议行内核验属地后定级）"
        rows.append(
            Stakeholder(
                name=node.name,
                category="境外主体" if kind in ("listed", "offshore", "hk") else "境内主体",
                role=str(rule["role"]),
                needs=str(rule["needs"]),
                hook=str(rule["hook"]),
                reach=reach,  # type: ignore[arg-type]
                region=region or ("境外" if kind in ("listed", "offshore", "hk") else ""),
                priority=_priority_for(kind, reach, in_region),
                note=note,
            )
        )

    # 生态客群：不来自披露文件，而是基于属地客群的通用开发方向
    rows.append(
        Stakeholder(
            name="辖内生态商户 / 上下游服务商（待摸排）",
            category="生态客群",
            role="平台生态内的本地中小主体",
            needs="收单、结算、经营性贷款、票据",
            hook="平台账期融资、收单与结算效率",
            reach=3,
            region=target_province,
            priority="A",
            note="不依赖集团主体配合，客户经理可独立开发",
        )
    )
    rows.append(
        Stakeholder(
            name="员工持股平台（有限合伙，待识别）",
            category="境内主体",
            role="股权激励载体：独立法人、决策链最短",
            needs="结算、股权质押融资、行权个税代扣",
            hook="激励行权的结算与融资便利",
            reach=2,
            region="",
            priority="B",
            note="工商穿透若发现「有限合伙」类股东，应升级为 A 级首触点",
        )
    )

    # 披露口径确认、但图谱中无人节点的决策人（如工商穿透空缺时）也要出行
    seen_names = {row.name for row in rows}
    for name, item in disclosed_map.items():
        if name not in seen_names:
            rows.append(_disclosed_row(name, item))

    order = {"A": 0, "B": 1, "C": 2}
    rows.sort(key=lambda r: (order[r.priority], -int(r.reach), r.name))
    return rows


def _person_row(
    node: PersonNode,
    is_ubo: bool,
    is_vie_nominee: bool,
    disclosed_names: set[str],
    is_us: bool = False,
) -> Stakeholder:
    """构造自然人干系人。

    关键业务区分（直接决定名单质量）：
    - 只出现在 VIE 协议里、未满足 UBO 阈值 → 大概率代持，**不作营销对象**；
    - 既满足 UBO 阈值又出现在 VIE 协议里 → 是架构安排下的持股人（美股 20-F 称
      designated individuals），须提示营销前核实实际决策角色。

    Args:
        node: 自然人节点。
        is_ubo: 是否满足 UBO 阈值。
        is_vie_nominee: 是否为 VIE 协议中的登记股东。
        disclosed_names: 披露口径出现的自然人/主体名。
        is_us: 是否美股披露（措辞差异）。

    Returns:
        Stakeholder: 干系人记录。
    """
    if is_vie_nominee and not is_ubo:
        return Stakeholder(
            name=node.name,
            category="自然人",
            role="VIE 登记股东（名义持股人）",
            needs="—",
            hook="—",
            reach=0,
            priority="C",
            note="⚠️ 代持性质，不作营销对象；仅用于识别真实决策人",
        )
    if is_ubo and is_vie_nominee:
        label = "20-F 披露为 designated individual" if is_us else "年报登记口径"
        return Stakeholder(
            name=node.name,
            category="自然人",
            role="VIE 登记持股人（登记口径满足 UBO 阈值）",
            needs="私人银行、家族财富安排",
            hook="股权安排与个人财富管理",
            reach=2,
            priority="B",
            note=f"⚠️ {label}，系架构安排下的持股人；营销前须核实其实际决策角色，勿直接视为实控人",
        )
    if is_ubo:
        return Stakeholder(
            name=node.name,
            category="自然人",
            role="最终受益人 / 实际控制人",
            needs="私人银行、家族信托、跨境资产配置",
            hook="减持与分红后的资金承接、家族财富安排",
            reach=2,
            priority="B",
            note="首战靠引荐，建议名单上报并由高层/私行前置接触",
        )
    matched = next((d for d in disclosed_names if node.name in d or d in node.name), "")
    return Stakeholder(
        name=node.name,
        category="自然人",
        role="登记股东 / 董监高",
        needs="私人银行、消费金融、股权质押",
        hook="股权激励行权与个人财富管理",
        reach=2,
        priority="B",
        note=f"披露口径亦出现（{matched}）" if matched else "",
    )


def _priority_for(kind: str, reach: int, in_region: bool | None) -> Literal["A", "B", "C"]:
    """按主体类型与可触达性判定优先级。

    Args:
        kind: 节点角色。
        reach: 可触达性等级。
        in_region: 是否辖内；None 表示属地未知（按待核验处理，不给 A 级）。

    Returns:
        Literal["A", "B", "C"]: 优先级。
    """
    if kind in ("wfoe", "opco") and in_region is True:
        return "A"
    if kind in ("wfoe", "opco"):
        return "B"
    if kind in ("listed", "offshore"):
        return "C"
    return "B"


def to_csv(rows: list[Stakeholder], path: Path) -> Path:
    """导出干系人线索清单（可直接进客户经理工作台）。

    Args:
        rows: 干系人列表。
        path: 目标 CSV 路径。

    Returns:
        Path: 写入的文件路径。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["优先级", "干系人", "类别", "角色定位", "需求落点", "切入由头", "可触达性", "属地", "备注"]
        )
        for r in rows:
            writer.writerow(
                [
                    r.priority,
                    r.name,
                    r.category,
                    r.role,
                    r.needs,
                    r.hook,
                    r.reach_label,
                    r.region,
                    r.note,
                ]
            )
    return path


def build_paths(rows: list[Stakeholder]) -> list[tuple[str, str]]:
    """生成接触路径（从谁撬动谁），供首战使用。

    Args:
        rows: 干系人列表。

    Returns:
        list[tuple[str, str]]: (路径名, 说明) 列表。
    """
    domestic = [r for r in rows if r.category == "境内主体" and r.priority == "A"]
    people = [r for r in rows if r.category == "自然人" and r.priority != "C"]
    offshore = [r for r in rows if r.category == "境外主体"]
    anchoring = domestic[0].name if domestic else "境内主体"

    paths = [
        (
            "由侧向内：生态客群先行",
            ("从辖内生态商户/上下游服务商切入（门槛最低、可独立开发），"
            f"在结算与融资往来中观察集团资金流，反向定位{anchoring}的财务决策人。"
            "此路径不依赖任何人引荐，客户经理今天就能启动。"),
        ),
        (
            "由下向上：代发换口碑",
            (f"以{anchoring}的员工代发为切入点，先服务「人」再谈「企业」；"
            "代发落地后可带动信用卡、消费贷与房贷，同时积累内部推荐线索，"
            "为后续接触资金与财务条线创造由头。"),
        ),
        (
            "平台捷径：持股平台与关键人",
            "员工持股平台（有限合伙）是决策链最短的独立法人，"
            "股权激励行权、结算与质押融资需求明确；"
            + (f"同时将 {('、'.join(r.name for r in people[:3]))} 等关键人报送私行前置接触，先建立个人信任。" if people else "")
            + "两个动作都不涉及集团层面的商务谈判，适合作为首战突破口。",
        ),
    ]
    if offshore:
        paths.append(
            (
                "上层联动：跨境与总行协同",
                (f"{('、'.join(r.name for r in offshore[:2]))} 等境外主体需总行跨境条线或境外分行承接，"
                "分行侧做信息报送与本地配合，不作为首战投入方向。"),
            )
        )
    return paths
