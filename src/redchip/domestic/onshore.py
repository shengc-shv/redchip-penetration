"""境内工商反推红筹架构（**仅限广东省内主体**）。

适用场景
--------
企业尚未上市、没有任何官方披露文件时，境外抓取链路拿不到架构信息。
但只要它在境内有运营主体，工商登记的**股东**就能暴露外资持有事实：
WFOE 的股东通常是香港或离岸公司，这本身就是红筹架构的直接证据。

例如「腾讯科技（深圳）有限公司」的股东是 `Tencent Technology (Hong Kong) Limited`，
无需任何境外披露即可判断其外资持股属性。

边界与纪律
----------
- **只对广东省内主体执行**（项目聚焦广东）；其它省份主体不产出信号，避免越界判断。
- 结论一律表述为**疑似**：股东为境外公司是必要条件，不是红筹的充分条件
  （也可能是纯外资子公司，而非红筹架构的一环）。
- 信号只用于"是否值得人工跟进"，不替代合规认定。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from redchip.graph.store import Graph
from redchip.models.schema import CompanyNode, Jurisdiction

# 境外股东的名称特征（与 expand_upward 的判定保持一致，但这里用于"反推"而非"分类"）
_OFFSHORE_MARKERS = (
    "LIMITED",
    "LTD",
    "HOLDINGS",
    "HOLDING",
    "INTERNATIONAL",
    "INVESTMENT",
    "GROUP",
    "BVI",
    "CAYMAN",
    "HONG KONG",
    "（HK）",
    "(HK)",
    "香港",
    "开曼",
)

# 明确的离岸/香港注册地（比名称特征更硬的证据）
_OFFSHORE_JURISDICTIONS = {
    Jurisdiction.HK,
    Jurisdiction.BVI,
    Jurisdiction.CAYMAN,
    Jurisdiction.BERMUDA,
}


class OnshoreSignal(BaseModel):
    """一条境内反推信号。"""

    model_config = ConfigDict(extra="ignore")

    entity: str = Field(description="广东省内主体名称")
    entity_code: str = ""
    city: str = ""
    offshore_holders: list[str] = Field(default_factory=list)
    verdict: Literal["疑似红筹架构", "外资持股"] = "外资持股"
    basis: str = ""


def _looks_offshore_name(name: str) -> bool:
    """按名称特征判断是否境外主体。

    Args:
        name: 股东名称。

    Returns:
        bool: 含境外特征词返回 True。
    """
    upper = name.upper()
    return any(marker in upper for marker in _OFFSHORE_MARKERS)


def detect_onshore_signals(
    graph: Graph, target_province: str = "广东省"
) -> list[OnshoreSignal]:
    """从境内工商股权结构反推红筹特征（仅目标省份）。

    Args:
        graph: 股权图谱。
        target_province: 目标省份（默认广东）。

    Returns:
        list[OnshoreSignal]: 反推信号列表；无信号返回空列表。
    """
    signals: list[OnshoreSignal] = []

    for node in graph.companies():
        if not isinstance(node, CompanyNode):
            continue
        # 只处理目标省份主体：其它省份不产出信号
        if node.province != target_province:
            continue

        offshore_holders: list[str] = []
        for edge in graph.shareholders_of(node.id):
            holder = graph.nodes.get(edge.from_id)
            if not isinstance(holder, CompanyNode):
                continue
            if holder.jurisdiction in _OFFSHORE_JURISDICTIONS or _looks_offshore_name(holder.name):
                offshore_holders.append(holder.name)

        if not offshore_holders:
            continue

        signals.append(
            OnshoreSignal(
                entity=node.name,
                entity_code=node.credit_code or "",
                city=node.city or node.province or "",
                offshore_holders=sorted(set(offshore_holders)),
                verdict="疑似红筹架构" if len(offshore_holders) >= 1 else "外资持股",
                basis=(
                    f"工商登记显示其股东为境外主体（{'、'.join(sorted(set(offshore_holders)))}），"
                    "存在外资持股事实；是否为红筹架构需人工核实"
                ),
            )
        )

    signals.sort(key=lambda s: (-len(s.offshore_holders), s.entity))
    return signals


def has_offshore_holding(graph: Graph, company_id: str) -> bool:
    """判断单个主体是否存在境外股东。

    Args:
        graph: 股权图谱。
        company_id: 主体节点 id。

    Returns:
        bool: 存在境外股东返回 True。
    """
    for edge in graph.shareholders_of(company_id):
        holder = graph.nodes.get(edge.from_id)
        if isinstance(holder, CompanyNode) and (
            holder.jurisdiction in _OFFSHORE_JURISDICTIONS or _looks_offshore_name(holder.name)
        ):
            return True
    return False


def marker_pattern() -> re.Pattern[str]:
    """返回境外名称特征的匹配正则（供外部复用）。

    Returns:
        re.Pattern[str]: 编译后的正则。
    """
    return re.compile("|".join(re.escape(m) for m in _OFFSHORE_MARKERS), re.IGNORECASE)
