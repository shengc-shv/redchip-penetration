"""HTML 表格解析：从披露原文的 <table> 中结构化抽取。

为什么需要它
------------
纯文本正则啃 20-F 的 Item 7 股东表很脆弱（列序不固定、单元格被拆行、capacity 列干扰）。
但 20-F 本身是 HTML，股东表就是标准 ``<table>``：

.. code-block:: text

    ['Name', '', 'Beneficial ownership (Shares)', '', 'Percent']
    ['Joseph C. TSAI (2)', '', '', '272,492,074', '', '1.5%']

因此直接解析表格结构（行列）比在文本流上做模式匹配**准确得多**。
这里用标准库 ``html.parser`` 自研，避免为解析表格引入第三方依赖
（项目已有 edgartools 安装失败、graphviz 不可用的教训）。

覆盖范围：SEC 20-F / 6-K 等 HTML 披露；港股年报为 PDF，不适用此路径。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 单元格里的脚注标记、货币与千分位
_FOOTNOTE_RE = re.compile(r"\(\s*\d+\s*\)")
_PCT_IN_CELL_RE = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*%")
# 分组标题行（如 "Directors and Executive Officers:"）
_GROUP_ROW_RE = re.compile(r":\s*$")
# 合计行（如 "All directors and executive officers as a group"）：不是具体股东
_TOTAL_ROW_RE = re.compile(r"\bas a group\b|^all\s+directors|^total\b|^subtotal", re.IGNORECASE)
_NON_NAME_RE = re.compile(r"^[\d,.\s%()\-]+$")


class _TableCollector(HTMLParser):
    """收集 HTML 中的所有表格为「行 → 单元格文本」结构。

    用**栈**而非单层状态：SEC 披露里存在表格嵌套（外层布局表套内层数据表），
    单层状态会在内层 ``</table>`` 时把外层结构顶掉，导致后续表格全部错位。
    """

    def __init__(self, max_tables: int = 500) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._max = max_tables
        self._stack: list[dict[str, object] | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """处理开始标签。"""
        del attrs
        if tag == "table":
            # 超过上限时压入占位帧，保证开闭标签仍然配对（不影响已收集结果）
            if len(self.tables) < self._max:
                self._stack.append({"rows": [], "row": None, "cell": None})
            else:
                self._stack.append(None)
            return
        frame = self._stack[-1] if self._stack else None
        if frame is None:
            return
        if tag == "tr":
            frame["row"] = []
        elif tag in ("td", "th") and frame["row"] is not None:
            frame["cell"] = []
        elif tag == "br" and frame["cell"] is not None:
            cell = frame["cell"]
            assert isinstance(cell, list)
            cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        """处理结束标签，逐级回填行列。"""
        if not self._stack:
            return
        frame = self._stack[-1]
        if tag == "table":
            self._stack.pop()
            if frame is not None:
                rows = frame["rows"]
                assert isinstance(rows, list)
                self.tables.append(rows)
            return
        if frame is None:
            return
        if tag == "tr" and frame["row"] is not None:
            row = frame["row"]
            cell = frame["cell"]
            # 单元格未闭合时兜底落盘，避免整行丢失
            if cell is not None:
                assert isinstance(row, list) and isinstance(cell, list)
                row.append(" ".join("".join(cell).split()))
            assert isinstance(row, list)
            rows = frame["rows"]
            assert isinstance(rows, list)
            rows.append(row)
            frame["row"] = None
            frame["cell"] = None
        elif tag in ("td", "th") and frame["cell"] is not None:
            cell = frame["cell"]
            row = frame["row"]
            if isinstance(cell, list) and isinstance(row, list):
                row.append(" ".join("".join(cell).split()))
            frame["cell"] = None

    def handle_data(self, data: str) -> None:
        """收集单元格文本。"""
        if not self._stack:
            return
        frame = self._stack[-1]
        if frame is None:
            return
        cell = frame["cell"]
        if isinstance(cell, list):
            cell.append(data)


def parse_tables(html: str, max_tables: int = 500) -> list[list[list[str]]]:
    """把 HTML 解析为表格列表。

    Args:
        html: HTML 原文。
        max_tables: 最多解析的表格数量（20-F 可达上百张表）。

    Returns:
        list[list[list[str]]]: 表格 → 行 → 单元格文本。
    """
    collector = _TableCollector(max_tables=max_tables)
    collector.feed(html)
    return collector.tables


def _table_text(table: list[list[str]]) -> str:
    """表格的全部文本（小写，用于关键词判定）。

    Args:
        table: 表格结构。

    Returns:
        str: 拼接后的文本。
    """
    return " ".join(" ".join(row) for row in table).lower()


def is_holder_table(table: list[list[str]]) -> bool:
    """判断是否为「股东持股」表。

    判定依据（宽松但足够区分）：表头同时出现名称、持股语义与百分比。

    Args:
        table: 表格结构。

    Returns:
        bool: 是持股表返回 True。
    """
    text = _table_text(table)
    has_name = "name" in text
    has_holding = any(k in text for k in ("ownership", "shareholder", "shares held", "shareholding"))
    has_pct = "%" in text or "percent" in text
    return has_name and has_holding and has_pct


def find_holder_tables(tables: list[list[list[str]]]) -> list[list[list[str]]]:
    """从全部表格中筛出股东持股表。

    Args:
        tables: 全部表格。

    Returns:
        list[list[list[str]]]: 持股表。
    """
    return [t for t in tables if is_holder_table(t)]


def _clean_name(raw: str) -> str:
    """清洗股东名：去脚注标记与首尾标点。

    Args:
        raw: 原始单元格文本。

    Returns:
        str: 清洗后的名称。
    """
    name = _FOOTNOTE_RE.sub("", raw)
    return name.strip(" ,.;:•-*†‡")


def holders_from_table(table: list[list[str]]) -> list[tuple[str, float]]:
    """从持股表中提取「名称 + 百分比」。

    规则（不依赖固定列序）：
    1. 数据行的**第一个非空单元格**视为名称；
    2. 该行中**含 % 的单元格**视为持股比例；
    3. 跳过表头、分组标题（以 ``:`` 结尾）、注释行与纯数字行。

    Args:
        table: 表格结构。

    Returns:
        list[tuple[str, float]]: (名称, 百分比) 列表。
    """
    out: list[tuple[str, float]] = []
    for row in table:
        cells = [c for c in row if c and c.strip()]
        if len(cells) < 2:
            continue
        name_raw = cells[0]
        if _GROUP_ROW_RE.search(name_raw) or _TOTAL_ROW_RE.search(name_raw):
            continue
        if name_raw.lower() in {"name", "shareholder", "beneficial owner"}:
            continue
        if _NON_NAME_RE.match(name_raw) or len(name_raw) < 3 or len(name_raw) > 90:
            continue
        pct: float | None = None
        for cell in cells[1:]:
            match = _PCT_IN_CELL_RE.search(cell)
            if match:
                pct = float(match.group(1))
                break
        if pct is None or not (0 < pct <= 100):
            continue
        name = _clean_name(name_raw)
        if len(name) < 3:
            continue
        out.append((name, pct))
    return out


def extract_holders_from_html(html: str, max_tables: int = 500) -> list[tuple[str, float]]:
    """端到端：HTML → 持股表 → (名称, 百分比)。

    Args:
        html: HTML 原文。
        max_tables: 最多解析的表格数量。

    Returns:
        list[tuple[str, float]]: 去重后的持股记录。
    """
    tables = parse_tables(html, max_tables=max_tables)
    seen: set[tuple[str, float]] = set()
    out: list[tuple[str, float]] = []
    for table in find_holder_tables(tables):
        for item in holders_from_table(table):
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out
