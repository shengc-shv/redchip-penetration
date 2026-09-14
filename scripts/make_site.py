#!/usr/bin/env python3
"""把 Markdown 报告与结构化「会前简报」渲染成 gh-pages 站点（复用腾讯简报同一套 CSS / 结构）。

站点布局（按日期批次分目录）
----------------------------
::

    index.html                    站点首页：批次导航 + **可点击企业名** → 报告页
    brief.html                    兼容入口（内容同 index.html）
    icon.png
    sample/                       腾讯控股样例（**打标「样例」**，非本项目产出）
        tencent_deck.html         会前简报
        tencent_brief.html        一页纸速览
    <YYYY-MM-DD>/                 按日期的批次目录
        index.html                当日批次索引
        <slug>.html               企业会前简报（4 板块，版式同腾讯）
        <slug>_detail.html        该企业的穿透详情（原始章节）
        criteria.html ...         全量摸底系列文档

为什么单独一个脚本
------------------
``make_brief.py`` 的数据源是穿透流水线的 ``CompanyReport``（节点/股权边/UBO），
而全量摸底系列的数据源是港交所申请版本分册抽取结果，二者模型不同。
本脚本只复用 **视觉与结构**（``make_brief.CSS`` / ``page_head`` / ``thumb_img`` /
``NOTICE_HTML``），简报正文由 ``brief_data.BRIEFS`` 提供数据、本脚本按同款版式渲染。

用法::

    PYTHONPATH=src python scripts/make_site.py [--batch 2026-09-14]
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redchip import config as config_mod  # noqa: E402

import make_brief as MB  # noqa: E402  同目录：复用其 CSS / head / notice
from brief_data import BRIEFS, SITE_CONCL, SITE_DESC, SITE_TITLE  # noqa: E402

TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
MD_H = re.compile(r"^#{1,6}\s+")

BACK_LINK = '<div class="meta" style="margin-top:10px"><a href="{href}">← 返回站点首页</a></div>'


# ── 极简 Markdown → HTML（只覆盖本项目报告用到的语法）─────────────────────────
def _inline(text: str) -> str:
    """行内：转义 → 粗体 / 行内代码 / 链接。"""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)
    return out


def _table(rows: list[str]) -> str:
    """把 Markdown 表格行渲染成 table（首行为表头）。"""
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def md_to_blocks(md: str) -> tuple[str, str]:
    """把 Markdown 渲染为 (页头补充区 HTML, 正文 cards HTML)。

    页头补充区 = 文档开头（第一个 ``##`` 之前）的引用块，用作声明条。
    """
    lines = md.splitlines()
    # 丢掉首个 H1（标题已进 <head> 与 .head）
    while lines and (not lines[0].strip() or lines[0].startswith("# ")):
        if lines[0].startswith("# "):
            lines.pop(0)
            break
        lines.pop(0)

    def render_seq(seq: list[str]) -> str:
        out: list[str] = []
        i = 0
        while i < len(seq):
            ln = seq[i]
            s = ln.strip()
            if not s:
                i += 1
                continue
            # 代码块
            if s.startswith("```"):
                lang = s[3:].strip()
                i += 1
                buf: list[str] = []
                while i < len(seq) and not seq[i].strip().startswith("```"):
                    buf.append(seq[i])
                    i += 1
                i += 1
                code = "\n".join(buf)
                if lang == "mermaid":  # 保持原样，交给 mermaid.js 渲染
                    out.append(f'<pre class="mermaid">{code}</pre>')
                else:
                    out.append(f"<pre><code>{html.escape(code, quote=False)}</code></pre>")
                continue
            # 表格
            if s.startswith("|") and i + 1 < len(seq) and TABLE_SEP.match(seq[i + 1].strip()):
                tbl: list[str] = []
                while i < len(seq) and seq[i].strip().startswith("|"):
                    tbl.append(seq[i])
                    i += 1
                out.append(_table(tbl))
                continue
            # 引用块
            if s.startswith(">"):
                buf = []
                while i < len(seq) and seq[i].strip().startswith(">"):
                    buf.append(seq[i].strip().lstrip(">").strip())
                    i += 1
                body = "<br>".join(_inline(x) for x in buf)
                out.append(
                    '<div style="background:#fafafa;border-left:3px solid #d9d9d9;'
                    f'border-radius:2px;padding:10px 13px;margin:10px 0;font-size:13px;color:#555">{body}</div>'
                )
                continue
            # 列表
            if re.match(r"^([-*]|\d+\.)\s+", s):
                ordered = bool(re.match(r"^\d+\.\s+", s))
                tag = "ol" if ordered else "ul"
                items = []
                while i < len(seq) and re.match(r"^([-*]|\d+\.)\s+", seq[i].strip()):
                    items.append(re.sub(r"^([-*]|\d+\.)\s+", "", seq[i].strip()))
                    i += 1
                lis = "".join(f"<li>{_inline(x)}</li>" for x in items)
                out.append(f'<{tag} style="margin:6px 0;padding-left:20px">{lis}</{tag}>')
                continue
            if s == "---":
                out.append('<hr style="border:0;border-top:1px solid #eee;margin:16px 0">')
                i += 1
                continue
            if s.startswith("### "):
                out.append(f"<h3 style='font-size:13.5px;margin:14px 0 6px'>{_inline(s[4:])}</h3>")
                i += 1
                continue
            # 普通段落（连续行合并）
            buf = []
            while i < len(seq) and seq[i].strip() and not MD_H.match(seq[i].strip()) \
                    and not seq[i].strip().startswith(("|", ">", "```")) \
                    and not re.match(r"^([-*]|\d+\.)\s+", seq[i].strip()):
                buf.append(seq[i].strip())
                i += 1
            out.append(f"<p style='margin:8px 0'>{_inline(' '.join(buf))}</p>")
        return "\n".join(out)

    # 按二级标题切分卡片
    head_part: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur_title: str | None = None
    cur: list[str] = []
    for ln in lines:
        if ln.startswith("## "):
            if cur_title is not None:
                sections.append((cur_title, cur))
            elif cur:
                head_part = cur
            cur_title = ln[3:].strip()
            cur = []
        else:
            cur.append(ln)
    if cur_title is not None:
        sections.append((cur_title, cur))
    elif cur:
        head_part = cur

    cards = []
    for title, body in sections:
        cards.append(
            '<div class="card">\n'
            f'  <h2 style="margin-top:0"><span class="bar"></span>{_inline(title)}</h2>\n'
            f'  {render_seq(body)}\n'
            "</div>"
        )
    return render_seq(head_part), "\n".join(cards)


# ── 会前简报（4 板块，版式对齐腾讯简报）──────────────────────────────────────
_TAG_LABEL = {
    "a": "A 级 · 可独立承接",
    "b": "B 级 · 需联动",
    "c": "C 级 · 需总行条线",
    "n": "待核验 · 需先落主体",
}


def _tag(level: str) -> str:
    """商机优先级标签（a/b/c/n）。"""
    return f'<span class="tag {level}">{_TAG_LABEL[level]}</span>'


def _p_tag(level: str) -> str:
    """干系人优先级标签（A/B/C）。"""
    return f'<span class="tag {level.lower()}">{level} 级</span>'


def _profile_table(rows: list) -> str:
    """① 一页画像：键值表（``pair`` 两列键值 / ``full`` 整行跨列）。"""
    out: list[str] = []
    for row in rows:
        if row[0] == "full":
            _, key, val = row
            if isinstance(val, list):
                items = "".join(f"<li>{v}</li>" for v in val)
                body = f'<ul style="margin:0;padding-left:16px">{items}</ul>'
            else:
                body = val
            out.append(f'<tr><td class="k">{key}</td><td colspan="3">{body}</td></tr>')
        else:
            _, k1, v1, k2, v2 = row
            out.append(
                f'<tr><td class="k">{k1}</td><td>{v1}</td>'
                f'<td class="k">{k2}</td><td>{v2}</td></tr>'
            )
    return f'<table>{"".join(out)}</table>'


def render_brief(spec: dict, batch: str, back_href: str = "../index.html") -> str:
    """渲染一页「会前简报」（4 板块），版式与腾讯简报一致。"""
    e = html.escape
    ev = spec.get("evidence") or {}
    ev_badge = (
        f'<span class="lv lv-{ev.get("level", "L4")}">证据等级 {ev.get("level", "—")} '
        f'{ev.get("label", "")}</span>'
        if ev else ""
    )
    ev_basis = "".join(f"<li>{b}</li>" for b in ev.get("basis", []))

    profile_rows = list(spec.get("profile", []))
    profile = _profile_table(profile_rows)
    if ev:
        # 证据等级行需带 <ul> 列表，与键值行的结构不同 → 单独拼一行再插到表尾
        ev_row = (
            '<tr><td class="k">证据等级</td><td colspan="3">'
            f'<b>{ev.get("level", "—")} {ev.get("label", "")}</b>——{ev.get("note", "")}'
            f'<ul class="muted" style="margin:6px 0 0;padding-left:18px">{ev_basis}</ul>'
            "</td></tr>"
        )
        profile = profile[: -len("</table>")] + ev_row + "</table>"

    touch_rows = "".join(
        f'<tr><td>{layer}</td><td>{who}</td><td>{needs}</td>'
        f'<td><span class="pill">{pill}</span></td></tr>'
        for layer, who, needs, pill in spec.get("touchpoints", [])
    )
    matrix_rows = "".join(
        f"<tr><td><b>{t}</b></td><td>{who}</td><td>{_tag(level)}</td><td>{why}</td></tr>"
        for level, t, who, why in spec.get("matrix", [])
    )
    stake_rows = "".join(
        f"<tr><td>{_p_tag(p)}</td><td><b>{name}</b></td><td>{role}</td>"
        f"<td>{needs}</td><td>{hook}</td><td>{reach}</td>"
        f"<td class='muted'>{note}</td></tr>"
        for p, name, role, needs, hook, reach, note in spec.get("stakeholders", [])
    )

    title = spec["head_title"]
    desc = re.sub(r"<[^>]+>", "", spec["concl"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">{MB.page_head(title, desc, f"{batch}/{spec['slug']}.html")}
<body>{MB.thumb_img()}<div class="wrap" style="--w:1060px">
  {MB.NOTICE_HTML}

  <div class="head">
    <div class="brand">
      <img src="icon.png" alt="" width="44" height="44">
      <div>
        <div class="kicker">{e(spec['kicker'])}</div>
        <h1>{e(spec['name'])} <span class="code">{e(spec['code'])}</span></h1>
      </div>
    </div>
    {BACK_LINK.format(href=back_href)}
    <div class="meta" style="margin-top:10px">{spec['meta']}{ev_badge}</div>
    <div class="concl">{spec['concl']}</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>① 一页画像</h2>
    {profile}
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>② 触点地图：钱在哪里、谁做决策</h2>
    <table>
      <thead><tr><th>层面</th><th>主体</th><th>金融需求落点</th><th>可否独立承接</th></tr></thead>
      <tbody>{touch_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>③ 商机矩阵</h2>
    <table>
      <thead><tr><th style="width:26%">商机</th><th style="width:24%">目标对象</th>
      <th style="width:18%">优先级</th><th>判断依据</th></tr></thead>
      <tbody>{matrix_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>④ 干系人作战表（首战切入点）</h2>
    <table>
      <thead><tr><th style="width:56px">优先级</th><th style="width:17%">干系人</th>
      <th style="width:17%">角色定位</th><th style="width:14%">需求落点</th>
      <th style="width:16%">切入由头</th><th style="width:11%">可触达性</th><th>备注</th></tr></thead>
      <tbody>{stake_rows}</tbody>
    </table>
    <div class="muted" style="margin-top:10px">{spec.get('stakeholder_note', '')}</div>
  </div>

  <div class="foot">
    {spec.get('foot', '')}
  </div>
  <button class="printbtn" onclick="window.print()">打印 / 存为 PDF</button>
</div></body></html>"""


# ── Markdown 页（全量摸底系列 + 企业穿透详情）────────────────────────────────
def render_md_page(spec: dict, md_path: Path, batch: str, back_href: str = "../index.html") -> str:
    """把一份 Markdown 渲染成整页 HTML（结构对齐会前简报）。"""
    e = html.escape
    md = md_path.read_text(encoding="utf-8")
    head_extra, cards = md_to_blocks(md)
    # 无 ``##`` 分节的文档（如 guangdong / unverified）→ 整体包一张卡片，避免内容落在卡片外
    body = f"{head_extra}\n{cards}" if cards else f'<div class="card">{head_extra}</div>'
    name = spec.get("name", "")
    code = spec.get("code", "")
    title = f"{name} {code}".strip() or spec.get("kicker", "报告")
    has_mermaid = 'class="mermaid"' in cards
    mermaid_js = (
        '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n'
        "<script>window.addEventListener('DOMContentLoaded',function(){"
        "if(window.mermaid){mermaid.initialize({startOnLoad:true,theme:'neutral'});}});</script>"
        if has_mermaid else ""
    )
    concl = spec.get("concl", "")
    concl_html = f'<div class="concl">{_inline(concl)}</div>' if concl else ""
    brand = (
        '<div class="brand">\n'
        '      <img src="icon.png" alt="" width="44" height="44">\n'
        "      <div>\n"
        f'        <div class="kicker">{e(spec.get("kicker", ""))}</div>\n'
        f'        <h1>{e(name)} <span class="code">{e(code)}</span></h1>\n'
        "      </div>\n"
        "    </div>"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">{MB.page_head(title, spec.get("desc", ""), f"{batch}/{spec['slug']}.html")}
<body>{MB.thumb_img()}<div class="wrap" style="--w:980px">
  {MB.NOTICE_HTML}

  <div class="head">
    {brand}
    {BACK_LINK.format(href=back_href)}
    {concl_html}
  </div>

  {body}

  <div class="foot">
    数据来源：港交所披露易公开申请版本（Multi-Files 分册）· 由红筹架构穿透系统自动抽取<br>
    事实性陈述均标注来源分册，可逐条回溯原文；具体数字需业务部门另行核实
  </div>
  <button class="printbtn" onclick="window.print()">打印 / 存为 PDF</button>
</div>
{mermaid_js}
</body></html>"""


# ── 索引页 ───────────────────────────────────────────────────────────────────
def _link_row(name: str, href: str, cells: list[str]) -> str:
    td = "".join(f'<td class="muted">{c}</td>' for c in cells)
    return f'<tr><td style="width:26%"><b><a href="{href}">{html.escape(name)}</a></b></td>{td}</tr>'


def render_batch_index(briefs: list[dict], docs: list[dict], batch: str) -> str:
    """当日批次索引页。"""
    e = html.escape
    brief_rows = "".join(
        _link_row(
            b["name"], f"{b['slug']}.html",
            [f"申请编号 {b['code']}",
             re.sub(r"<[^>]+>", "", b["meta"]).split("·")[1].strip() if "·" in b["meta"] else "",
             f'<span class="pill">会前简报</span>'
             + (f' <a href="{b["slug"]}_detail.html">穿透详情</a>' if b.get("detail_md") else "")],
        )
        for b in briefs
    )
    doc_rows = "".join(
        _link_row(d["name"], f"{d['slug']}.html", [d.get("kicker", ""), d.get("desc", "")])
        for d in docs
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">{MB.page_head(f"批次 {batch} · 红筹架构与广东商机", SITE_DESC, f"{batch}/index.html")}
<body>{MB.thumb_img()}<div class="wrap" style="--w:980px">
  {MB.NOTICE_HTML}

  <div class="head">
    <div class="brand">
      <img src="icon.png" alt="" width="44" height="44">
      <div>
        <div class="kicker">批次目录</div>
        <h1>红筹架构与广东商机 <span class="code">{e(batch)}</span></h1>
      </div>
    </div>
    <div class="meta" style="margin-top:10px"><a href="../index.html">← 返回站点首页</a></div>
    <div class="concl">{SITE_CONCL}</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>① 企业红筹架构分析报告</h2>
    <table><tbody>{brief_rows}</tbody></table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>② 全量摸底：口径 · 视图 · 证据卡</h2>
    <table><tbody>{doc_rows}</tbody></table>
  </div>

  <div class="foot">
    数据来源：港交所披露易「新上市申請版本及相關資料」官方静态 JSON 与申请版本 PDF<br>
    ⚠️ 行外公开信息初筛，须经行内渠道复核；关系状态（是否已开户 / 他行锁定）公开不可得
  </div>
  <button class="printbtn" onclick="window.print()">打印 / 存为 PDF</button>
</div></body></html>"""


def render_site_index(briefs: list[dict], docs: list[dict], batch: str, samples: list[dict]) -> str:
    """站点首页：**只列企业报告**（腾讯控股样例打头并打标「样例」）。

    刻意保持极简（用户 2026-09-14 要求）：首页只有一张「企业红筹架构分析报告」表格，
    全量摸底的口径 / 视图 / 证据卡文档不在此处展开，仅在页脚留一个批次目录入口。
    """
    def row(name: str, href: str, cells: list[str], sample: bool = False) -> str:
        tag = ' <span class="tag c">样例</span>' if sample else ""
        tds = "".join(f'<td class="muted">{c}</td>' for c in cells)
        return (f'<tr><td style="width:26%">'
                f'<b><a href="{href}">{html.escape(name)}</a></b>{tag}</td>{tds}</tr>')

    # 腾讯控股「会前简报」置顶，打标「样例」（非本项目产出，仅作版式参照）
    tencent = row("腾讯控股", "sample/tencent_deck.html",
                  ["00700.HK", "已上市", "开曼群岛", "总部在粤 · 深圳市"],
                  sample=True)
    brief_rows = tencent + "".join(
        row(b["name"], f'{batch}/{b["slug"]}.html',
            [f'<code>{html.escape(str(b.get("code", "")))}</code>',
             html.escape(str(b.get("submit_date", ""))),
             html.escape(str(b.get("domicile_cn", ""))),
             html.escape(str(b.get("gd_note", "")))])
        for b in briefs
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">{MB.page_head(SITE_TITLE, SITE_DESC, "")}
<body>{MB.thumb_img()}<div class="wrap" style="--w:980px">
  {MB.NOTICE_HTML}

  <div class="head">
    <div class="brand">
      <img src="icon.png" alt="" width="44" height="44">
      <div>
        <div class="kicker">港交所递表企业</div>
        <h1>红筹架构与广东商机 <span class="code">2026 全量摸底</span></h1>
      </div>
    </div>
    <div class="meta" style="margin-top:10px">口径：递交日 ≥ 2026-01-01 且状态 ∈ 处理中 / 含PHIP / 已上市（已剔除失效·撤回·被拒·发回）</div>
    <div class="concl">{SITE_CONCL}</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>① 企业红筹架构分析报告</h2>
    <div class="muted" style="margin:-4px 0 8px">点击<b>企业名称</b>进入报告页</div>
    <table>
      <thead><tr><th>企业名称</th><th>代码 / 编号</th><th>递表日</th><th>上市主体注册地</th><th>广东连接</th></tr></thead>
      <tbody>{brief_rows}</tbody>
    </table>
  </div>

  <div class="foot">
    数据来源：港交所披露易「新上市申請版本及相關資料」官方静态 JSON 与申请版本 PDF<br>
    ⚠️ 行外公开信息初筛，须经行内渠道复核；关系状态（是否已开户 / 他行锁定）公开不可得<br>
    口径文档 · 视图 · 证据卡见批次目录 <a href="{batch}/index.html"><code>{batch}/</code></a>
  </div>
  <button class="printbtn" onclick="window.print()">打印 / 存为 PDF</button>
</div></body></html>"""


# ── 腾讯样例页：注入「样例」标识 ─────────────────────────────────────────────
SAMPLE_BANNER = (
    '<div style="max-width:1060px;margin:0 auto 10px;background:#faf5ec;border:1px solid #f0e6d2;'
    'border-left:3px solid #b8944d;border-radius:3px;padding:9px 12px;font-size:12px;color:#8a6c2f;'
    'line-height:1.7"><b>样例</b> · 本页为「腾讯控股」示例报告，仅作<b>版式参照</b>，'
    '不属于本次全量摸底（2026 年以来递表企业）的产出。'
    '<a href="../index.html" style="color:#8a6c2f">← 返回站点首页</a></div>'
)


def _mark_sample(src: Path, dst: Path) -> None:
    """复制腾讯示例页并注入「样例」横幅 + <title> 前缀。"""
    t = src.read_text(encoding="utf-8")
    t = t.replace("<title>", "<title>【样例】", 1)
    t = re.sub(r"(<body[^>]*>)", r"\1" + SAMPLE_BANNER, t, count=1)
    dst.write_text(t, encoding="utf-8")


# ── 主流程 ───────────────────────────────────────────────────────────────────
# 全量摸底系列文档（Markdown → 页面），随批次发布
DOCS: list[dict[str, str]] = [
    {"slug": "criteria", "md": "筛选条件_广东红筹.md", "kicker": "口径文档",
     "name": "广东红筹筛选条件", "desc": "5 个 AND 条件 + 核心/补充分层 + 阈值敏感性 + 词库审计 + 已知缺口"},
    {"slug": "fields", "md": "字段说明_全量摸底表.md", "kicker": "口径文档",
     "name": "字段中文说明", "desc": "字段的中文含义、取值口径与可信度分级"},
    {"slug": "conclusion", "md": "full_listing_conclusion.md", "kicker": "全量摸底",
     "name": "结论与方法论修正", "desc": "人口 474 家 · 注册地分布 · 旧口径 19 条假阳性翻案"},
    {"slug": "bd_ledger", "md": "gd_hk_ipo_2026_bd_ledger.md", "kicker": "全量摸底",
     "name": "广东商机 BD 台账", "desc": "广东红筹核心层 + 补充层 + 广东 H 股 + 其余人口"},
    {"slug": "redchip", "md": "full_listing_redchip.md", "kicker": "全量摸底",
     "name": "红筹视图（离岸 59 家）", "desc": "封面页确认离岸注册的全部申请人，含广东线索分层"},
    {"slug": "guangdong", "md": "full_listing_guangdong.md", "kicker": "全量摸底",
     "name": "广东视图", "desc": "名称通道（境内 H 股）+ 正文通道（离岸壳名）的并集"},
    {"slug": "gd_cards", "md": "redchip_gd_evidence_cards.md", "kicker": "证据卡",
     "name": "广东红筹候选 · 原文证据卡", "desc": "逐家原文摘句，可直接复核招股书"},
    {"slug": "vie_cards", "md": "vie_evidence_cards.md", "kicker": "证据卡",
     "name": "VIE 判定 · 原文证据卡", "desc": "是 4 / 否(仅历史安排) 19 / 待核验 11，逐条可溯源"},
    {"slug": "unverified", "md": "full_listing_unverified.md", "kicker": "全量摸底",
     "name": "待核验视图", "desc": "注册地未判定的 17 家及其原因"},
]

# 首页表格用到的派生展示字段（事实来自 output/*.md 底稿）
BRIEF_INDEX_META = {
    "qdama": {"submit_date": "2026-08-21", "domicile_cn": "开曼群岛",
              "gd_note": "总部在粤 · 广州市海珠区（法人级）"},
    "exegenesis": {"submit_date": "2026-08-28", "domicile_cn": "开曼群岛",
                   "gd_note": "集团主要 PRC 运营实体在粤 · 广州嘉因（法人级）"},
}

SAMPLES = [
    {"slug": "tencent_deck", "name": "腾讯控股 · 会前简报（00700.HK）",
     "desc": "4 板块：一页画像 / 触点地图 / 商机矩阵 / 干系人作战表 —— 新报告页的版式来源"},
    {"slug": "tencent_brief", "name": "腾讯控股 · 一页纸速览（00700.HK）",
     "desc": "单页版：关键指标 + 商机分档 A/B/C"},
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None,
                    help="批次目录名（默认取北京时间当日 YYYY-MM-DD）")
    args = ap.parse_args()

    settings = config_mod.get_settings()
    out_dir = settings.redchip_output_dir
    site = out_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    # gh-pages 站点为纯静态 HTML，写 .nojekyll 阻止 GitHub Pages 走 Jekyll 处理
    (site / ".nojekyll").write_text("", encoding="utf-8")

    batch = args.batch or dt.datetime.now(config_mod.CST).strftime("%Y-%m-%d")
    batch_dir = site / batch
    sample_dir = site / "sample"
    batch_dir.mkdir(parents=True, exist_ok=True)
    sample_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    # 图标：根 + 各子目录（页内 <img src="icon.png"> 是相对路径）
    icon_src = out_dir / "briefs" / "icon.png"
    if not icon_src.exists():
        icon_src = site / "icon.png"
    if icon_src.exists():
        for d in (site, batch_dir, sample_dir):
            shutil.copy2(icon_src, d / "icon.png")

    # ── 企业会前简报（4 板块）+ 穿透详情 ─────────────────────────────────
    briefs: list[dict] = []
    for spec in BRIEFS:
        meta = BRIEF_INDEX_META.get(spec["slug"], {})
        briefs.append({**spec, **meta})
        (batch_dir / f"{spec['slug']}.html").write_text(
            render_brief(spec, batch), encoding="utf-8")
        written.append(f"{batch}/{spec['slug']}.html")
        detail = spec.get("detail_md", "")
        if detail and (out_dir / detail).exists():
            dspec = {
                "slug": f"{spec['slug']}_detail", "name": f"{spec['name']} 穿透详情",
                "code": f"申请编号 {spec['code']}",
                "kicker": "红筹架构分析 · 原始章节（附录）",
                "desc": re.sub(r"<[^>]+>", "", spec["concl"])[:120],
            }
            (batch_dir / f"{spec['slug']}_detail.html").write_text(
                render_md_page(dspec, out_dir / detail, batch), encoding="utf-8")
            written.append(f"{batch}/{spec['slug']}_detail.html")

    # ── 全量摸底系列文档 ──────────────────────────────────────────────
    docs_html: list[dict] = []
    for d in DOCS:
        md_path = out_dir / d["md"]
        if not md_path.exists():
            print(f"  ⚠️ 跳过（缺文件）：{md_path.name}")
            continue
        docs_html.append(d)
        (batch_dir / f"{d['slug']}.html").write_text(
            render_md_page(d, md_path, batch), encoding="utf-8")
        written.append(f"{batch}/{d['slug']}.html")

    # ── 腾讯样例页（打标「样例」）────────────────────────────────────────
    for src, slug in ((out_dir / "briefs" / "index.html", "tencent_deck"),
                      (out_dir / "briefs" / "brief.html", "tencent_brief")):
        if src.exists():
            _mark_sample(src, sample_dir / f"{slug}.html")
            written.append(f"sample/{slug}.html")

    # ── 索引页 ────────────────────────────────────────────────────────
    (batch_dir / "index.html").write_text(
        render_batch_index(briefs, docs_html, batch), encoding="utf-8")
    index_html = render_site_index(briefs, docs_html, batch, SAMPLES)
    (site / "index.html").write_text(index_html, encoding="utf-8")
    (site / "brief.html").write_text(index_html, encoding="utf-8")

    print(f"站点已生成：{site}")
    print(f"  批次目录：{batch}/（{len(briefs)} 份会前简报 + {len(docs_html)} 份摸底文档）")
    print(f"  样例目录：sample/（腾讯控股 {len(SAMPLES)} 页，已打标）")
    print(f"  页面 {len(written) + 3} 个：index.html、brief.html、" + "、".join(written[:4]) + " …")
    print(f"  首页：{site / 'index.html'}")


if __name__ == "__main__":
    main()
