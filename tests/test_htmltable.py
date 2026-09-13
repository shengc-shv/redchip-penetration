"""HTML 表格解析测试（20-F Item 7 股东表）。"""

from redchip.overseas.htmltable import (
    extract_holders_from_html,
    find_holder_tables,
    holders_from_table,
    is_holder_table,
    parse_tables,
)

ITEM7_HTML = """
<table>
  <tr><td></td><td></td><td></td></tr>
  <tr><td>Name</td><td></td><td>Beneficial ownership (Shares)</td><td></td><td>Percent</td></tr>
  <tr><td>Directors and Executive Officers:</td><td></td><td></td><td></td><td></td></tr>
  <tr><td>Joseph C. TSAI (2)</td><td></td><td>272,492,074</td><td></td><td>1.5%</td></tr>
  <tr><td>Eddie Yongming WU</td><td></td><td>19,706,418</td><td></td><td>0.1%</td></tr>
  <tr><td>Jerry YANG</td><td></td><td>485,072</td><td></td><td>0.0%</td></tr>
  <tr><td>All directors and executive officers as a group</td><td></td><td>349,543,033</td><td></td><td>1.9%</td></tr>
</table>
<table>
  <tr><td>Year ended March 31,</td><td>2026</td><td>%</td></tr>
  <tr><td>Revenue</td><td>1,000</td><td>100%</td></tr>
</table>
"""


def test_parse_tables_shape():
    """应解析出全部表格并按「行 → 单元格」组织。"""
    tables = parse_tables(ITEM7_HTML)
    assert len(tables) == 2
    assert len(tables[0]) == 7
    assert tables[0][1][0] == "Name"


def test_is_holder_table_discriminates():
    """股东表应被识别，财务表不应被误判。"""
    tables = parse_tables(ITEM7_HTML)
    assert is_holder_table(tables[0]) is True
    assert is_holder_table(tables[1]) is False
    assert len(find_holder_tables(tables)) == 1


def test_holders_from_table_filters_noise():
    """应跳过表头、分组行、合计行与 0.0% 记录。"""
    table = parse_tables(ITEM7_HTML)[0]
    holders = dict(holders_from_table(table))
    assert holders == {"Joseph C. TSAI": 1.5, "Eddie Yongming WU": 0.1}
    assert "All directors and executive officers as a group" not in holders
    assert "Name" not in holders


def test_footnote_markers_are_stripped():
    """名称中的脚注标记 (2) 应被剥离。"""
    table = parse_tables(ITEM7_HTML)[0]
    assert "Joseph C. TSAI" in dict(holders_from_table(table))


def test_end_to_end_extraction():
    """端到端抽取应返回 (名称, 百分比) 列表。"""
    out = extract_holders_from_html(ITEM7_HTML)
    assert ("Joseph C. TSAI", 1.5) in out
    assert all(0 < pct <= 100 for _, pct in out)


def test_nested_tables_do_not_break_parsing():
    """嵌套表格不应导致外层结构错乱（栈式解析）。"""
    nested = """
    <table>
      <tr><td>Name</td><td>Shares</td><td>Percent</td></tr>
      <tr><td>Outer A</td><td>100</td><td>5.0%</td>
          <td><table><tr><td>inner</td></tr></table></td></tr>
      <tr><td>Outer B</td><td>200</td><td>7.5%</td></tr>
    </table>
    """
    tables = parse_tables(nested)
    assert tables, "应至少解析出一张表"
    outer = max(tables, key=len)
    names = dict(holders_from_table(outer))
    assert names.get("Outer A") == 5.0
    assert names.get("Outer B") == 7.5


def test_empty_html_is_safe():
    """空输入不应抛异常。"""
    assert parse_tables("") == []
    assert extract_holders_from_html("") == []
