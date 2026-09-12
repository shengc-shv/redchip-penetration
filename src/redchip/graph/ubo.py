"""UBO 穿透算法。

算法（对应方案文档第 6.2 节）
----------------------------
自下而上累乘：从起点实体出发，逐层向上取股东，``累计持股 = 上层累计 × 本层直接持股``，
遇到自然人且累计持股 ≥ 阈值即判定为 UBO。

工程上的三处加固
----------------
1. **环路保护**：交叉持股在境内非常常见，``visited`` 防止无限递归。
2. **路径可达性**：``visited`` 按「当前路径」而非全局去重，否则 A→B→A 之外的合法菱形路径会被误剪。
   这里采用路径内去重，保证多路径都能被枚举。
3. **VIE 段标记**：路径中经过协议控制边时置 ``reached_via_vie=True``，供合规判断
   （协议控制不等于股权，UBO 结论需提示）。
"""

from __future__ import annotations

from redchip import config as config_mod
from redchip.graph.store import Graph
from redchip.models.schema import PersonNode, UboResult


def penetrate_ubo(
    graph: Graph,
    entity_id: str,
    max_depth: int | None = None,
    min_share: float | None = None,
) -> list[UboResult]:
    """从指定实体向上穿透，找出最终受益人。

    Args:
        graph: 股权图谱。
        entity_id: 起点节点 id（通常是 WFOE 或 VIE 运营实体）。
        max_depth: 最大穿透层数；缺省取配置。
        min_share: UBO 判定阈值（0~1）；缺省取配置。

    Returns:
        list[UboResult]: 按累计持股比例降序的结果列表。
    """
    settings = config_mod.get_settings()
    depth_limit = max_depth if max_depth is not None else settings.max_depth
    threshold = min_share if min_share is not None else settings.min_share

    results: list[UboResult] = []

    def dfs(
        current_id: str,
        path: list[str],
        cumulative_share: float,
        depth: int,
        via_vie: bool,
    ) -> None:
        if depth > depth_limit:
            return
        for edge in graph.shareholders_of(current_id):
            if edge.from_id in path:  # 路径内环路：交叉持股保护
                continue
            new_share = cumulative_share * (edge.share_pct / 100.0)
            new_path = path + [edge.from_id]
            node = graph.nodes.get(edge.from_id)

            if isinstance(node, PersonNode):
                if new_share >= threshold:
                    results.append(
                        UboResult(
                            person_id=node.id,
                            name=node.name,
                            cumulative_share=round(new_share, 6),
                            path=new_path,
                            path_names=[_name_of(graph, nid) for nid in new_path],
                            depth=len(new_path) - 1,
                            reached_via_vie=via_vie,
                        )
                    )
            else:
                dfs(edge.from_id, new_path, new_share, depth + 1, via_vie)

    # 先处理协议控制：若该实体被 WFOE 协议控制，则从控制方继续向上穿透，经济权益视同 100%
    for control in graph.controlled_by(entity_id):
        dfs(control.from_id, [control.from_id], 1.0, 1, via_vie=True)

    dfs(entity_id, [entity_id], 1.0, 0, via_vie=False)

    results.sort(key=lambda r: r.cumulative_share, reverse=True)
    return _dedupe(results)


def _name_of(graph: Graph, node_id: str) -> str:
    """取节点显示名。

    Args:
        graph: 图谱。
        node_id: 节点 id。

    Returns:
        str: 节点名称；不存在时回退为 id。
    """
    node = graph.nodes.get(node_id)
    return node.name if node is not None else node_id


def _dedupe(results: list[UboResult]) -> list[UboResult]:
    """同一自然人保留持股比例最高的一条路径。

    Args:
        results: 原始结果。

    Returns:
        list[UboResult]: 去重后的结果。
    """
    best: dict[str, UboResult] = {}
    for r in results:
        current = best.get(r.person_id)
        if current is None or r.cumulative_share > current.cumulative_share:
            best[r.person_id] = r
    return list(best.values())
