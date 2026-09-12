"""图谱渲染：Mermaid / Graphviz DOT / PNG / 自研 SVG。

为什么自带 SVG 渲染
-------------------
``dot`` 是系统级依赖（GitHub Actions 需 apt-get install graphviz，本地 macOS 需 brew）。
为保证**任何环境下都能出图**，这里实现零依赖的分层 SVG 渲染作为兜底：

- 有 ``dot`` 二进制 → 额外产出 PNG（方案文档要求的交付物）
- 无 ``dot`` → 产出 SVG + DOT + Mermaid，流水线不中断

SVG 节点承载三类信息（这是「图太简陋」的主要改进点）：
1. **主体名称**；2. **角色 + 注册地/城市**；3. **UBO 标记与累计持股比例**。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from redchip.graph.store import Graph
from redchip.models.schema import CompanyNode, PersonNode, UboResult

_NODE_W = 240
_NODE_H = 76
_GAP_X = 44
_GAP_Y = 104
_TOP_PAD = 96
_BOTTOM_PAD = 96

# 节点角色中文标签（图例与节点副标题共用）
KIND_LABELS: dict[str, str] = {
    "listed": "上市主体",
    "offshore": "离岸层",
    "hk": "香港层",
    "wfoe": "WFOE 外商独资",
    "opco": "境内运营实体",
    "gov": "国资主体",
    "person": "自然人",
    "unknown": "境内实体",
}

_NODE_COLORS: dict[str, str] = {
    "listed": "#d6e4ff",
    "offshore": "#e8e0f7",
    "hk": "#dceef5",
    "wfoe": "#dff5e1",
    "opco": "#fff3cd",
    "gov": "#ffe0e0",
    "person": "#fde8d7",
    "unknown": "#eef3fb",
}


def node_label(graph: Graph, node_id: str) -> str:
    """生成节点显示名。

    Args:
        graph: 图谱。
        node_id: 节点 id。

    Returns:
        str: 显示名，过长时截断。
    """
    node = graph.nodes.get(node_id)
    if node is None:
        return node_id
    return node.name[:18]


def _safe_label(node: CompanyNode | PersonNode) -> str:
    """生成用于渲染的安全标签：转义引号、替换半角括号，并限制长度。

    Args:
        node: 节点对象。

    Returns:
        str: 安全标签文本。
    """
    return node.name.replace('"', "").replace("(", "（").replace(")", "）")[:30]


def _kind_key(node: CompanyNode | PersonNode) -> str:
    """提取节点角色键。

    Pydantic 反序列化后 ``node.kind`` 是 ``EntityKind`` 枚举实例，直接 ``str()`` 会得到
    ``"EntityKind.LISTED"``（而非 ``"listed"``），导致查表全部回落到「境内实体」。
    这里统一取 ``.value``。

    Args:
        node: 节点对象。

    Returns:
        str: 角色键（如 "listed"）。
    """
    kind = getattr(node, "kind", "unknown")
    return getattr(kind, "value", str(kind))


def _kind_label(node: CompanyNode | PersonNode) -> str:
    """返回节点角色中文名。

    Args:
        node: 节点对象。

    Returns:
        str: 角色标签。
    """
    return KIND_LABELS.get(_kind_key(node), "境内实体")


def _node_subtitle(node: CompanyNode | PersonNode) -> str:
    """生成节点副标题：角色 + 注册地/城市。

    Args:
        node: 节点对象。

    Returns:
        str: 副标题文本。
    """
    if isinstance(node, PersonNode):
        return "自然人股东"
    jur = getattr(node, "jurisdiction", None)
    jur_text = getattr(jur, "value", None) or str(jur or "")
    city = getattr(node, "city", None) or ""
    province = getattr(node, "province", None) or ""
    place = city or province
    parts = [_kind_label(node)]
    if jur_text and jur_text != "其他":
        parts.append(jur_text)
    if place:
        parts.append(place)
    return " · ".join(parts)


def to_mermaid(graph: Graph, title: str = "红筹架构穿透图") -> str:
    """生成 Mermaid 代码（节点带角色与注册地）。

    Args:
        graph: 图谱。
        title: 图标题（写入注释）。

    Returns:
        str: Mermaid 文本。
    """
    lines = ["%% " + title, "graph TD"]
    alias = {nid: f"n{idx}" for idx, nid in enumerate(graph.nodes)}
    for nid, node in graph.nodes.items():
        label = f"{_safe_label(node)}<br/>{_node_subtitle(node)}"
        if isinstance(node, PersonNode):
            lines.append(f'    {alias[nid]}(("{label}"))')
        elif _kind_key(node) == "listed":
            lines.append(f"    {alias[nid]}{{{{ {label} }}}}")
        else:
            lines.append(f'    {alias[nid]}["{label}"]')
    for edge in graph.owns:
        if edge.from_id in alias and edge.to_id in alias:
            label = f"|{edge.share_pct:g}%|" if edge.share_pct else ""
            lines.append(f"    {alias[edge.from_id]} -->{label} {alias[edge.to_id]}")
    for edge in graph.controls:
        if edge.from_id in alias and edge.to_id in alias:
            lines.append(
                f"    {alias[edge.from_id]} -.->|VIE·{edge.contract_type}| {alias[edge.to_id]}"
            )
    return "\n".join(lines)


def to_dot(graph: Graph, ubos: list[UboResult] | None = None) -> str:
    """生成 Graphviz DOT 文本（节点两行：名称 / 角色·注册地）。

    Args:
        graph: 图谱。
        ubos: UBO 结果，用于给最终受益人加粗边框。

    Returns:
        str: DOT 文本。
    """
    ubo_ids = {u.person_id for u in (ubos or [])}
    lines = [
        "digraph redchip {",
        '  rankdir=TB; graph [fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica"];',
        '  edge [fontname="Helvetica", fontsize=10, color="#5b7290"];',
    ]
    alias = {nid: f"n{idx}" for idx, nid in enumerate(graph.nodes)}
    for nid, node in graph.nodes.items():
        kind = _kind_key(node)
        color = _NODE_COLORS.get(kind, "#eef3fb")
        penwidth = "3" if (nid in ubo_ids or kind == "listed") else "1"
        label = f"{_dot_escape(_safe_label(node))}\\n{_dot_escape(_node_subtitle(node))}"
        lines.append(
            f'  {alias[nid]} [label="{label}", fillcolor="{color}", penwidth={penwidth}];'
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


def _dot_escape(text: str) -> str:
    """转义 DOT 标签中的特殊字符。

    Args:
        text: 原文本。

    Returns:
        str: 转义后文本。
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


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
    result = subprocess.run(  # 固定参数，无外部输入拼接
        [dot_bin, "-Tpng", "-o", str(out_png)],
        input=dot_text,
        text=True,
        capture_output=True,
        check=False,
    )
    return out_png if result.returncode == 0 and out_png.exists() else None


def to_svg(graph: Graph, title: str = "", ubos: list[UboResult] | None = None) -> str:
    """零依赖分层 SVG 渲染（dot 不可用时的兜底）。

    Args:
        graph: 图谱。
        title: 图标题。
        ubos: UBO 结果，用于在自然人节点上标注累计持股。

    Returns:
        str: SVG 文本。
    """
    ubo_by_id = {u.person_id: u for u in (ubos or [])}
    positions, width, height = _layout(graph)
    parts: list[str] = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'),
        ("<style>"
        '.t{font:700 16px "PingFang SC",Helvetica,sans-serif;fill:#0f172a;}'
        '.n{font:600 13px "PingFang SC",Helvetica,sans-serif;fill:#1f2937;}'
        '.s{font:11px "PingFang SC",Helvetica,sans-serif;fill:#64748b;}'
        ".e{stroke:#64748b;stroke-width:1.6;fill:none;}"
        ".v{stroke:#c0392b;stroke-width:1.8;stroke-dasharray:6,4;fill:none;}"
        ".lbl{font:11px Helvetica,sans-serif;fill:#334155;}"
        ".vlbl{font:11px Helvetica,sans-serif;fill:#c0392b;}"
        '.lg{font:11px "PingFang SC",Helvetica,sans-serif;fill:#475569;}'
        "</style>"),
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]

    if title:
        parts.append(f'<text class="t" x="28" y="42">{_xml_escape(title)}</text>')

    for edge in _visible_edges(graph, positions):
        x1, y1, x2, y2, label, is_vie = edge
        cls = "v" if is_vie else "e"
        parts.append(f'<path class="{cls}" d="M{x1},{y1} L{x2},{y2}" />')
        if label:
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            text_cls = "vlbl" if is_vie else "lbl"
            # 白色描边保证标签压在连线上仍可读
            parts.append(
                f'<text class="{text_cls}" x="{mx}" y="{my - 5}" text-anchor="middle" '
                f'stroke="#ffffff" stroke-width="3" paint-order="stroke">{_xml_escape(label)}</text>'
            )

    for nid, (cx, cy) in positions.items():
        node = graph.nodes[nid]
        x, y = cx - _NODE_W // 2, cy - _NODE_H // 2
        kind = _kind_key(node)
        color = _NODE_COLORS.get(kind, "#eef3fb")
        stroke = "#1d4ed8" if kind == "listed" else "#94a3b8"
        penwidth = 3 if (kind == "listed" or nid in ubo_by_id) else 1.2
        parts.append(
            f'<rect x="{x}" y="{y}" width="{_NODE_W}" height="{_NODE_H}" rx="10" '
            f'fill="{color}" stroke="{stroke}" stroke-width="{penwidth}"/>'
        )
        parts.append(
            f'<text class="n" x="{cx}" y="{cy - 4}" text-anchor="middle">'
            f"{_xml_escape(node.name[:15])}</text>"
        )
        subtitle = _node_subtitle(node)
        if nid in ubo_by_id:
            subtitle = f"UBO · 累计 {ubo_by_id[nid].cumulative_share * 100:.2f}%"
        parts.append(
            f'<text class="s" x="{cx}" y="{cy + 17}" text-anchor="middle">'
            f"{_xml_escape(subtitle)}</text>"
        )

    parts.extend(_legend(width, height))
    parts.append("</svg>")
    return "\n".join(parts)


def _legend(width: int, height: int) -> list[str]:
    """生成图例。

    Args:
        width: 画布宽（未使用，保留以便后续排版扩展）。
        height: 画布高。

    Returns:
        list[str]: SVG 片段列表。
    """
    del width  # 图例固定贴左下，暂不依赖画布宽度
    parts = [f'<text class="lg" x="28" y="{height - 26}">图例</text>']
    x = 74
    for kind, label in (
        ("listed", "上市主体"),
        ("offshore", "离岸层"),
        ("hk", "香港层"),
        ("wfoe", "WFOE"),
        ("opco", "运营实体"),
        ("person", "自然人/UBO"),
    ):
        parts.append(
            f'<rect x="{x}" y="{height - 38}" width="16" height="12" rx="3" '
            f'fill="{_NODE_COLORS[kind]}" stroke="#94a3b8"/>'
        )
        parts.append(f'<text class="lg" x="{x + 22}" y="{height - 27}">{label}</text>')
        x += 22 + len(label) * 13 + 20
    parts.append(
        
            f'<path class="e" d="M{x},{height - 32} L{x + 26},{height - 32}" />'
            f'<text class="lg" x="{x + 32}" y="{height - 27}">股权</text>'
        
    )
    parts.append(
        
            f'<path class="v" d="M{x + 82},{height - 32} L{x + 108},{height - 32}" />'
            f'<text class="lg" x="{x + 114}" y="{height - 27}">VIE 协议控制</text>'
        
    )
    return parts


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
    width = max(1040, max_row * (_NODE_W + _GAP_X) + _GAP_X)
    height = max(430, _TOP_PAD + len(levels) * (_NODE_H + _GAP_Y) + _BOTTOM_PAD)

    positions: dict[str, tuple[int, int]] = {}
    for d, nids in levels.items():
        nids.sort()
        row_w = len(nids) * _NODE_W + (len(nids) - 1) * _GAP_X
        start_x = max(_GAP_X + _NODE_W // 2, (width - row_w) // 2 + _NODE_W // 2)
        for idx, nid in enumerate(nids):
            positions[nid] = (
                start_x + idx * (_NODE_W + _GAP_X),
                _TOP_PAD + d * (_NODE_H + _GAP_Y),
            )
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


def render_all(
    graph: Graph,
    out_dir: Path,
    stem: str = "penetration_graph",
    ubos: list[UboResult] | None = None,
    title: str = "",
) -> dict[str, Path]:
    """一次性产出所有图格式。

    Args:
        graph: 图谱。
        out_dir: 输出目录。
        stem: 文件名前缀。
        ubos: UBO 结果（用于标注最终受益人）。
        title: 图标题。

    Returns:
        dict[str, Path]: 格式 → 文件路径（PNG 可能缺失）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Path] = {}

    dot_text = to_dot(graph, ubos=ubos)
    dot_path = out_dir / f"{stem}.dot"
    dot_path.write_text(dot_text, encoding="utf-8")
    produced["dot"] = dot_path

    mmd = out_dir / f"{stem}.mermaid"
    mmd.write_text(to_mermaid(graph, title=title or stem), encoding="utf-8")
    produced["mermaid"] = mmd

    svg = out_dir / f"{stem}.svg"
    svg.write_text(to_svg(graph, title=title, ubos=ubos), encoding="utf-8")
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
