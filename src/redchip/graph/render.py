"""图谱渲染：Mermaid / Graphviz DOT / PNG / 自研 SVG。

为什么自带 SVG 渲染
-------------------
``dot`` 是系统级依赖（GitHub Actions 需 apt-get install graphviz，本地 macOS 需 brew）。
为了保证**任何环境下都能出图**，这里实现零依赖的分层 SVG 渲染作为兜底：

- 有 ``dot`` 二进制 → 额外产出 PNG（方案文档要求的交付物）
- 无 ``dot`` → 产出 SVG + DOT + Mermaid，流水线不中断
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from redchip.graph.store import Graph
from redchip.models.schema import CompanyNode, PersonNode

_NODE_W = 220
_NODE_H = 64
_GAP_X = 40
_GAP_Y = 96


def node_label(graph: Graph, node_id: str) -> str:
    """生成节点显示名（含注册地/持股比例无关信息）。

    Args:
        graph: 图谱。
        node_id: 节点 id。

    Returns:
        str: 显示名，过长时截断。
    """
    node = graph.nodes.get(node_id)
    if node is None:
        return node_id
    name = node.name[:18]
    return name


def to_mermaid(graph: Graph, title: str = "红筹架构穿透图") -> str:
    """生成 Mermaid 代码。

    Args:
        graph: 图谱。
        title: 图标题（写入注释）。

    Returns:
        str: Mermaid 文本。
    """
    lines = ["%% " + title, "graph TD"]
    alias = {nid: f"n{idx}" for idx, nid in enumerate(graph.nodes)}
    for nid, node in graph.nodes.items():
        shape = _mermaid_shape(node)
        lines.append(f"    {alias[nid]}{shape}")
    for edge in graph.owns:
        if edge.from_id in alias and edge.to_id in alias:
            label = f"|{edge.share_pct:g}%|" if edge.share_pct else ""
            lines.append(f"    {alias[edge.from_id]} -->{label} {alias[edge.to_id]}")
    for edge in graph.controls:
        if edge.from_id in alias and edge.to_id in alias:
            lines.append(f"    {alias[edge.from_id]} -.->|VIE:{edge.contract_type}| {alias[edge.to_id]}")
    return "\n".join(lines)


def _mermaid_shape(node: CompanyNode | PersonNode) -> str:
    """按节点类型返回 Mermaid 形状语法。

    Args:
        node: 节点对象。

    Returns:
        str: Mermaid 节点定义串。
    """
    label = _safe_label(node)
    if isinstance(node, PersonNode):
        return f'(["{label}"])'
    kind = getattr(node, "kind", "")
    if kind == "listed":
        return f'{{{{"{label}"}}}}'
    return f'["{label}"]'


def to_dot(graph: Graph) -> str:
    """生成 Graphviz DOT 文本。

    Args:
        graph: 图谱。

    Returns:
        str: DOT 文本。
    """
    lines = [
        "digraph redchip {",
        '  rankdir=TB; graph [fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#eef3fb", fontname="Helvetica"];',
        '  edge [fontname="Helvetica", fontsize=10, color="#5b7290"];',
    ]
    alias = {nid: f"n{idx}" for idx, nid in enumerate(graph.nodes)}
    for nid, node in graph.nodes.items():
        color = _node_color(node)
        lines.append(
            f'  {alias[nid]} [label="{_dot_escape(_safe_label(node))}", fillcolor="{color}"];'
        )
    for edge in graph.owns:
        if edge.from_id in alias and edge.to_id in alias:
            label = f"{edge.share_pct:g}%" if edge.share_pct else ""
            lines.append(f'  {alias[edge.from_id]} -> {alias[edge.to_id]} [label="{label}"];')
    for edge in graph.controls:
        if edge.from_id in alias and edge.to_id in alias:
            lines.append(
                f'  {alias[edge.from_id]} -> {alias[edge.to_id]} '
                f'[style=dashed, color="#c0392b", label="VIE:{edge.contract_type}"];'
            )
    lines.append("}")
    return "\n".join(lines)


def _safe_label(node: CompanyNode | PersonNode) -> str:
    """生成用于渲染的安全标签：转义引号、替换半角括号，并限制长度。

    Args:
        node: 节点对象。

    Returns:
        str: 安全标签文本。
    """
    return (
        node.name.replace('"', "")
        .replace("(", "（")
        .replace(")", "）")[:30]
    )


def _dot_escape(text: str) -> str:
    """转义 DOT 标签中的特殊字符。

    Args:
        text: 原文本。

    Returns:
        str: 转义后文本。
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _node_color(node: CompanyNode | PersonNode) -> str:
    """按节点类型返回填充色。

    Args:
        node: 节点对象。

    Returns:
        str: 十六进制颜色。
    """
    if isinstance(node, PersonNode):
        return "#fde8d7"
    kind = str(getattr(node, "kind", ""))
    return {
        "listed": "#d6e4ff",
        "offshore": "#e8e0f7",
        "hk": "#e0f0f5",
        "wfoe": "#dff5e1",
        "opco": "#fff3cd",
        "gov": "#ffe0e0",
    }.get(kind, "#eef3fb")


def render_png(dot_text: str, out_png: Path) -> Path | None:
    """调用 graphviz 渲染 PNG。

    Args:
        dot_text: DOT 文本。
        out_png: 输出 PNG 路径。

    Returns:
        Path | None: 成功返回路径；系统无 dot 二进制返回 None。
    """
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return None
    out_png.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [dot_bin, "-Tpng", "-o", str(out_png)],
        input=dot_text,
        text=True,
        capture_output=True,
        check=False,
    )
    return out_png if result.returncode == 0 and out_png.exists() else None


def to_svg(graph: Graph) -> str:
    """零依赖分层 SVG 渲染（dot 不可用时的兜底）。

    布局算法：按「最大路径深度」分层，层内水平均分，自顶向下连线。

    Args:
        graph: 图谱。

    Returns:
        str: SVG 文本。
    """
    positions, width, height = _layout(graph)
    parts: list[str] = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'),
        ('<style>.n{font:13px "PingFang SC",Helvetica,sans-serif;} '
        ".e{stroke:#5b7290;stroke-width:1.4;fill:none;} "
        ".v{stroke:#c0392b;stroke-width:1.4;stroke-dasharray:5,4;fill:none;} "
        ".lbl{font:11px Helvetica;fill:#5b7290;}</style>"),
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]

    for edge in _visible_edges(graph, positions):
        x1, y1, x2, y2, label, is_vie = edge
        cls = "v" if is_vie else "e"
        parts.append(f'<path class="{cls}" d="M{x1},{y1} L{x2},{y2}" />')
        if label:
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            parts.append(f'<text class="lbl" x="{mx}" y="{my - 4}" text-anchor="middle">{label}</text>')

    for nid, (cx, cy) in positions.items():
        node = graph.nodes[nid]
        x, y = cx - _NODE_W // 2, cy - _NODE_H // 2
        color = _node_color(node)
        parts.append(
            f'<rect x="{x}" y="{y}" width="{_NODE_W}" height="{_NODE_H}" rx="8" '
            f'fill="{color}" stroke="#8fa6c4"/>'
        )
        parts.append(
            f'<text class="n" x="{cx}" y="{cy + 5}" text-anchor="middle" fill="#1f2937">'
            f"{_xml_escape(node.name[:16])}</text>"
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _xml_escape(text: str) -> str:
    """XML 转义。

    Args:
        text: 原文本。

    Returns:
        str: 转义后文本。
    """
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _layout(graph: Graph) -> tuple[dict[str, tuple[int, int]], int, int]:
    """计算分层坐标。

    Args:
        graph: 图谱。

    Returns:
        tuple[dict[str, tuple[int, int]], int, int]: 节点坐标、画布宽、画布高。
    """
    parents: dict[str, set[str]] = {nid: set() for nid in graph.nodes}
    for edge in list(graph.owns) + list(graph.controls):  # type: ignore[operator]
        if edge.to_id in parents:
            parents[edge.to_id].add(edge.from_id)

    depth: dict[str, int] = {}

    def depth_of(nid: str, guard: set[str]) -> int:
        if nid in depth:
            return depth[nid]
        if nid in guard or not parents.get(nid):
            depth[nid] = 0
            return 0
        guard.add(nid)
        depth[nid] = max(depth_of(p, guard) + 1 for p in parents[nid])
        return depth[nid]

    for nid in list(graph.nodes):
        depth_of(nid, set())

    levels: dict[int, list[str]] = {}
    for nid, d in depth.items():
        levels.setdefault(d, []).append(nid)

    max_row = max(len(v) for v in levels.values()) if levels else 1
    width = max(900, max_row * (_NODE_W + _GAP_X) + _GAP_X)
    height = max(300, len(levels) * (_NODE_H + _GAP_Y) + _GAP_Y)

    positions: dict[str, tuple[int, int]] = {}
    for d, nids in levels.items():
        nids.sort()
        row_w = len(nids) * _NODE_W + (len(nids) - 1) * _GAP_X
        start_x = max(_GAP_X + _NODE_W // 2, (width - row_w) // 2 + _NODE_W // 2)
        for idx, nid in enumerate(nids):
            positions[nid] = (start_x + idx * (_NODE_W + _GAP_X), _GAP_Y + d * (_NODE_H + _GAP_Y))
    return positions, width, height


def _visible_edges(
    graph: Graph, positions: dict[str, tuple[int, int]]
) -> Iterable[tuple[int, int, int, int, str, bool]]:
    """生成可见边的线段坐标（父节点底部 → 子节点顶部）。

    Args:
        graph: 图谱。
        positions: 节点坐标。

    Yields:
        tuple[int, int, int, int, str, bool]: x1, y1, x2, y2, 标签, 是否 VIE 边。
    """
    for edge in graph.owns:
        if edge.from_id in positions and edge.to_id in positions:
            x1, y1 = positions[edge.from_id]
            x2, y2 = positions[edge.to_id]
            yield x1, y1 + _NODE_H // 2, x2, y2 - _NODE_H // 2, (
                f"{edge.share_pct:g}%" if edge.share_pct else ""
            ), False
    for edge in graph.controls:
        if edge.from_id in positions and edge.to_id in positions:
            x1, y1 = positions[edge.from_id]
            x2, y2 = positions[edge.to_id]
            yield x1, y1 + _NODE_H // 2, x2, y2 - _NODE_H // 2, edge.contract_type, True


def render_all(graph: Graph, out_dir: Path, stem: str = "penetration_graph") -> dict[str, Path]:
    """一次性产出所有图格式。

    Args:
        graph: 图谱。
        out_dir: 输出目录。
        stem: 文件名前缀。

    Returns:
        dict[str, Path]: 格式 → 文件路径（PNG 可能缺失）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Path] = {}

    dot_text = to_dot(graph)
    dot_path = out_dir / f"{stem}.dot"
    dot_path.write_text(dot_text, encoding="utf-8")
    produced["dot"] = dot_path

    mmd = out_dir / f"{stem}.mermaid"
    mmd.write_text(to_mermaid(graph), encoding="utf-8")
    produced["mermaid"] = mmd

    svg = out_dir / f"{stem}.svg"
    svg.write_text(to_svg(graph), encoding="utf-8")
    produced["svg"] = svg

    png = render_png(dot_text, out_dir / f"{stem}.png")
    if png:
        produced["png"] = png
    return produced


def edge_count(graph: Graph) -> tuple[int, int]:
    """统计边数量。

    Args:
        graph: 图谱。

    Returns:
        tuple[int, int]: (股权边数, 协议控制边数)。
    """
    return len(graph.owns), len(graph.controls)
