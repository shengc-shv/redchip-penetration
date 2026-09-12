"""内存股权图谱（可选同步到 Neo4j）。

设计要点
--------
- 图是**唯一的中间表示**：境外披露、工商 API、LLM 提取都往这里写，穿透与渲染都从这里读。
- Neo4j 是可选落地点：配了 ``NEO4J_URI`` 就同步，没配就只落 JSON，保证 GitHub Actions 之外
  （以及本地无图库环境）也能完整跑通。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from redchip import config as config_mod
from redchip.models.schema import (
    CompanyNode,
    ControlEdge,
    EntityKind,
    Jurisdiction,
    OwnEdge,
    PersonNode,
)


class Graph:
    """股权关系图。"""

    def __init__(self) -> None:
        self.nodes: dict[str, CompanyNode | PersonNode] = {}
        self.owns: list[OwnEdge] = []
        self.controls: list[ControlEdge] = []

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def add_company(
        self,
        name: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.OTHER,
        kind: EntityKind = EntityKind.UNKNOWN,
        credit_code: str | None = None,
        province: str | None = None,
        city: str | None = None,
        **extra: Any,
    ) -> CompanyNode:
        """新增（或复用）公司节点。

        优先用统一社会信用代码作 id；离岸实体无信用代码，退化为 ``名称@注册地``。

        Args:
            name: 企业名称。
            jurisdiction: 注册地。
            kind: 实体角色。
            credit_code: 统一社会信用代码。
            province: 省份。
            city: 城市。
            **extra: 其它字段（reg_status / business_scope 等）。

        Returns:
            CompanyNode: 节点对象。
        """
        jur = Jurisdiction(jurisdiction) if _valid_jurisdiction(jurisdiction) else Jurisdiction.OTHER
        node_id = credit_code or f"{name}@{jur.value}"

        # 与已存在节点保持同一 key，避免同一实体被拆成多个节点：
        # 先按信用代码命中，再按「名称 + 注册地」命中并补全信用代码
        if credit_code and credit_code in self.nodes:
            existing = self.nodes[credit_code]
            if isinstance(existing, CompanyNode):
                return existing
        for candidate in self.nodes.values():
            if (
                isinstance(candidate, CompanyNode)
                and candidate.name == name
                and candidate.jurisdiction == jur
            ):
                if credit_code and not candidate.credit_code:
                    candidate.credit_code = credit_code
                return candidate

        existing = self.nodes.get(node_id)
        if isinstance(existing, CompanyNode):
            # 已存在则只补全空字段，避免后写入的粗糙数据覆盖精确数据
            for field, value in {
                "province": province,
                "city": city,
                "credit_code": credit_code,
            }.items():
                if value and not getattr(existing, field):
                    setattr(existing, field, value)
            return existing

        node = CompanyNode(
            id=node_id,
            name=name,
            jurisdiction=jur,
            kind=kind,
            credit_code=credit_code,
            province=province,
            city=city,
            **extra,
        )
        self.nodes[node_id] = node
        return node

    def add_person(self, name: str) -> PersonNode:
        """新增（或复用）自然人节点。

        Args:
            name: 姓名。

        Returns:
            PersonNode: 节点对象。
        """
        node_id = f"person:{name}"
        existing = self.nodes.get(node_id)
        if isinstance(existing, PersonNode):
            return existing
        node = PersonNode(id=node_id, name=name)
        self.nodes[node_id] = node
        return node

    def add_own(self, edge: OwnEdge) -> None:
        """追加一条股权边（去重）。"""
        if not any(
            e.from_id == edge.from_id and e.to_id == edge.to_id and e.share_pct == edge.share_pct
            for e in self.owns
        ):
            self.owns.append(edge)

    def remove_placeholder_own(self, from_id: str, to_id: str) -> int:
        """删除 0% 占位股权边（VIE 登记股东在未取得工商数据前的占位）。

        工商数据到位后，占位边会被真实持股比例的边替换，避免出现同一对节点的
        「0% 与 54.29%」双线噪声。

        Args:
            from_id: 股东 id。
            to_id: 被持股公司 id。

        Returns:
            int: 删除的边的数量。
        """
        before = len(self.owns)
        self.owns = [
            e
            for e in self.owns
            if not (e.from_id == from_id and e.to_id == to_id and e.share_pct == 0.0)
        ]
        return before - len(self.owns)

    def add_control(self, edge: ControlEdge) -> None:
        """追加一条协议控制边（去重）。"""
        if not any(
            e.from_id == edge.from_id and e.to_id == edge.to_id and e.contract_type == edge.contract_type
            for e in self.controls
        ):
            self.controls.append(edge)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def shareholders_of(self, node_id: str) -> list[OwnEdge]:
        """返回直接股东边（股东 → 该公司）。

        Args:
            node_id: 目标公司 id。

        Returns:
            list[OwnEdge]: 股东边列表，按持股比例降序。
        """
        edges = [e for e in self.owns if e.to_id == node_id]
        return sorted(edges, key=lambda e: e.share_pct, reverse=True)

    def controlled_by(self, node_id: str) -> list[ControlEdge]:
        """返回指向该节点的协议控制边。

        Args:
            node_id: 目标公司 id。

        Returns:
            list[ControlEdge]: 控制边列表。
        """
        return [e for e in self.controls if e.to_id == node_id]

    def companies(self) -> list[CompanyNode]:
        """返回所有公司节点。"""
        return [n for n in self.nodes.values() if isinstance(n, CompanyNode)]

    def persons(self) -> list[PersonNode]:
        """返回所有自然人节点。"""
        return [n for n in self.nodes.values() if isinstance(n, PersonNode)]

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "nodes": [n.model_dump(mode="json") for n in self.nodes.values()],
            "owns": [e.model_dump(mode="json") for e in self.owns],
            "controls": [e.model_dump(mode="json") for e in self.controls],
        }

    @classmethod
    def load(cls, path: Path) -> Graph:
        """从 JSON 还原图谱（供分步脚本之间传递状态）。

        Args:
            path: graph.json 路径。

        Returns:
            Graph: 图对象；文件不存在时返回空图。
        """
        graph = cls()
        if not path.exists():
            return graph
        data = json.loads(path.read_text(encoding="utf-8"))
        from redchip.models.schema import OwnEdge  # 局部导入避免循环依赖

        for raw in data.get("nodes", []):
            if raw.get("kind") == "person":
                node = PersonNode.model_validate(raw)
            else:
                node = CompanyNode.model_validate(raw)
            graph.nodes[node.id] = node
        for raw in data.get("owns", []):
            graph.owns.append(OwnEdge.model_validate(raw))
        for raw in data.get("controls", []):
            graph.controls.append(ControlEdge.model_validate(raw))
        return graph

    def save(self, path: Path) -> Path:
        """落盘为 JSON。

        Args:
            path: 目标文件。

        Returns:
            Path: 写入的文件路径。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def sync_neo4j(self) -> int:
        """同步到 Neo4j（可选）。

        Returns:
            int: 成功写入的关系数；未配置图库或驱动缺失时返回 0。
        """
        settings = config_mod.get_settings()
        if not settings.neo4j_uri:
            return 0
        try:
            from neo4j import GraphDatabase  # 局部导入：可选依赖
        except ImportError:
            return 0

        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        written = 0
        try:
            with driver.session() as session:
                for node in self.nodes.values():
                    if isinstance(node, CompanyNode):
                        session.run(
                            "MERGE (c:Company {id: $id}) SET c.name=$name, c.jurisdiction=$jur, "
                            "c.kind=$kind, c.province=$province, c.city=$city, c.credit_code=$code",
                            id=node.id,
                            name=node.name,
                            jur=node.jurisdiction.value,
                            kind=node.kind.value,
                            province=node.province or "",
                            city=node.city or "",
                            code=node.credit_code or "",
                        )
                    else:
                        session.run(
                            "MERGE (p:Person {id: $id}) SET p.name=$name",
                            id=node.id,
                            name=node.name,
                        )
                for edge in self.owns:
                    session.run(
                        "MATCH (a {id: $from}), (b {id: $to}) "
                        "MERGE (a)-[r:SHAREHOLDER_OF {share_pct: $pct}]->(b)",
                        **{"from": edge.from_id, "to": edge.to_id, "pct": edge.share_pct},
                    )
                    written += 1
                for edge in self.controls:
                    session.run(
                        "MATCH (a {id: $from}), (b {id: $to}) "
                        "MERGE (a)-[r:CONTROLS {contract_type: $type}]->(b)",
                        **{"from": edge.from_id, "to": edge.to_id, "type": edge.contract_type},
                    )
                    written += 1
        finally:
            driver.close()
        return written


def _valid_jurisdiction(value: Jurisdiction | str) -> bool:
    """判断注册地取值是否合法。

    Args:
        value: 待校验值。

    Returns:
        bool: 合法返回 True。
    """
    try:
        Jurisdiction(value)
        return True
    except ValueError:
        return False


def iter_edges(graph: Graph) -> Iterable[tuple[str, str, str]]:
    """遍历所有边，返回 ``(起点, 终点, 标签)`` 三元组，供渲染层使用。

    Args:
        graph: 图对象。

    Yields:
        tuple[str, str, str]: 起点 id、终点 id、边标签。
    """
    for edge in graph.owns:
        yield edge.from_id, edge.to_id, f"{edge.share_pct:g}%"
    for edge in graph.controls:
        yield edge.from_id, edge.to_id, f"VIE:{edge.contract_type}"
