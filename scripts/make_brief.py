#!/usr/bin/env python3
"""生成面向银行高管的红筹企业商机报告（两种风格）。

风格 A —— 一页纸速览（Executive Brief）
    单页、结论先行、分档商机，适合手机上快速读完、邮件转发。

风格 B —— 会前简报（Briefing Deck）
    结构化深度版：画像 / 触点地图 / 商机矩阵 / 干系人作战表 / 接触路径 /
    推进方向 / 合规边界，适合上会前阅读或作为首战指挥材料。

输出：``output/briefs/``；其中会前简报会同时写出 ``index.html``，用于 gh-pages 手机访问。

用法::

    python scripts/make_brief.py
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from redchip import config as config_mod
from redchip.brief.stakeholders import build_paths, build_stakeholders
from redchip.models.schema import CompanyReport

# 视觉风格：深红主色 + 大量留白 + 细线分隔 + 金色点缀
# 用色克制、不加大圆角与重阴影，贴近正式报告的观感
CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 14px 56px; background: #f4f4f5;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, Helvetica, sans-serif;
  color: #2b2b2b; -webkit-font-smoothing: antialiased; line-height: 1.65; }
.wrap { max-width: var(--w, 760px); margin: 0 auto; }
.card { background: #fff; border: 1px solid #e6e6e8; border-radius: 4px;
  padding: 18px 20px; margin-bottom: 12px; }
.head { background: #c7000b; color: #fff; border: none; border-radius: 4px;
  padding: 20px 22px; margin-bottom: 12px; }
.head h1 { margin: 0 0 6px; font-size: 21px; font-weight: 600; letter-spacing: .5px; }
.head .meta { font-size: 12.5px; opacity: .85; }
.head .concl { margin-top: 14px; padding: 12px 14px; background: rgba(255,255,255,.13);
  border-left: 3px solid #f0c987; border-radius: 2px; font-size: 14px; line-height: 1.7; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.stat { background: #fff; border: 1px solid #e6e6e8; border-radius: 4px; padding: 11px 13px; }
.stat .k { font-size: 11.5px; color: #8a8a8f; }
.stat .v { font-size: 18px; font-weight: 700; margin-top: 3px; color: #c7000b; }
.stat .u { font-size: 11px; color: #a8a8ad; font-weight: 400; margin-left: 2px; }
h2 { font-size: 14.5px; margin: 20px 0 10px; display: flex; align-items: center; gap: 8px;
  color: #1a1a1a; }
h2 .bar { width: 3px; height: 14px; border-radius: 1px; background: #c7000b; }
.opp { border-left: 2px solid #dcdce0; padding: 9px 0 9px 14px; margin-bottom: 12px; }
.opp.a { border-color: #c7000b; } .opp.b { border-color: #b8944d; } .opp.c { border-color: #b9b9be; }
.opp .t { font-size: 14px; font-weight: 600; }
.opp .row { font-size: 12.5px; color: #5a5a60; margin-top: 4px; }
.opp .row b { color: #1a1a1a; font-weight: 600; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 2px;
  font-weight: 600; margin-right: 6px; vertical-align: 1px; }
.tag.a { background: #fbe9e9; color: #a80009; }
.tag.b { background: #f7f0e2; color: #8a6c2f; }
.tag.c { background: #eeeef0; color: #56565c; }
.tag.n { background: #e8f2e9; color: #2f6b38; }
.tag.w { background: #fbe9e9; color: #a80009; }
table { width: 100%; border-collapse: collapse; font-size: 12.6px; }
th { text-align: left; font-weight: 600; color: #56565c; background: #fafafa;
  padding: 8px 10px; border-bottom: 1.5px solid #c7000b; font-size: 12px; }
td { padding: 8px 10px; border-bottom: 1px solid #f0f0f2; vertical-align: top; line-height: 1.6; }
td.k { color: #8a8a8f; width: 128px; }
.muted { color: #7a7a80; font-size: 12px; line-height: 1.7; }
.foot { text-align: center; color: #a0a0a5; font-size: 11.5px; margin-top: 20px; line-height: 1.8; }
.pill { display:inline-block; padding:1px 6px; border-radius:2px; font-size:11px;
  background:#f4f4f5; color:#56565c; margin-right:5px; }
@media (max-width: 640px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  body { padding: 16px 10px 40px; }
  table { font-size: 12px; }
}
"""


# ---------------------------------------------------------------------------
# 商机数据：结论来自穿透事实，业务判断标注依据（不含时限与排期）
# ---------------------------------------------------------------------------

BRIEFS: dict[str, dict] = {
    "00700": {
        "name": "腾讯控股",
        "code": "00700.HK",
        "market": "港股主板",
        "doc": "2025 年報（2026-04-09）",
        "seat": "广东省深圳市",
        "target_province": "广东省",
        "jurisdiction": "辖内",
        "chain": "开曼上市主体 → BVI → 香港 → 腾讯科技（深圳）→ VIE → 深圳市腾讯计算机系统",
        "domestic": [
            "腾讯科技（深圳）有限公司（WFOE）",
            "深圳市腾讯计算机系统有限公司（VIE 运营实体）",
        ],
        "ubo": "马化腾（境内 VIE 登记持股 54.29%）",
        "disclosed": "MIH Internet Holdings B.V. 22.80%、Advance Data Services Limited 8.82%",
        "conclusion": (
            "架构清晰、境内主体在深圳且属<b>辖内</b>。成熟期互联网巨头的主体授信非分行角色，"
            "可落地的是<b>员工端与生态商户端</b>两扇门——这是零售/普惠条线能直接承接的部分。"
        ),
        "stats": [
            ("境内实体", "2", "家"),
            ("穿透层级", "4", "层"),
            ("最终受益人", "1", "位"),
            ("披露时点", "2026-04", ""),
        ],
        "opps": [
            (
                "a",
                "员工金融：代发 + 信用卡 + 消费贷",
                "腾讯科技（深圳）/ 深圳市腾讯计算机系统",
                ("以两家境内主体为锚，对接深圳及广州研发、运营团队的代发工资、信用卡、消费信贷与房贷需求；"
                "先拿代发，再带动信用卡与财富。"),
                "境内两家实体承载主要业务与人员（年报 p.13、p.45）",
            ),
            (
                "a",
                "生态商户收单与小微经营贷",
                "本行辖内的游戏/内容/电商服务商、微信生态服务商",
                ("以腾讯生态服务商为线索做对公开户 + 收单 + 基于流水的经营性贷款；"
                "此条不依赖腾讯主体配合，可独立推进。"),
                "VIE 运营实体持有增值电信等牌照、承载平台业务（年报 p.45）",
            ),
            (
                "b",
                "高管与核心员工财富管理",
                "UBO 马化腾及在深高管、核心骨干",
                ("围绕股权激励行权、减持变现后的资金承接与跨境配置，由私行团队前置接触；"
                "客户主账户多不在本行，需与深圳分行联动。"),
                "境内 VIE 登记股东含马化腾 54.29%、张志东 22.86%（工商登记口径）",
            ),
            (
                "b",
                "员工持股平台服务",
                "持股平台及其激励对象",
                "员工持股平台的资金结算、代发与股权质押融资；RSU/期权行权涉及的结售汇与个税代扣配套。",
                "年报披露存在股权激励安排（Equity Incentive Plans 类章节）",
            ),
            (
                "c",
                "境外股东关联方跨境服务",
                "MIH（Naspers/Prosus 体系）、Advance Data Services",
                ("上述股东若在境内有关联主体或资金安排，可承接跨境结算与外汇避险；"
                "属长线机会，需总行跨境条线协同。"),
                "年报 p.79 第XV部披露：MIH 22.80%、Advance Data 8.82%",
            ),
        ],
        # 推进方向：只讲切入方向与分工，不含时限与排期
        "directions": [
            (
                "零售条线",
                "员工代发与零售渗透",
                "以腾讯科技（深圳）等境内主体的员工为起点，代发先行，带动信用卡、消费贷与房贷",
            ),
            (
                "公司条线",
                "生态商户与结算",
                "辖内腾讯生态服务商的收单、结算与经营贷，不依赖集团主体配合",
            ),
            (
                "私人银行",
                "关键人前置接触",
                "经深圳分行联动安排 UBO 及高管圈层接触，先建立个人信任，不谈具体业务",
            ),
        ],
        "notes": [
            "本报告结论全部基于公开披露文件（港交所年报）+ 工商登记数据，不含我行内部客户数据。",
            "营销触达须遵守客户信息保护与适当性要求；涉及境外股东的信息仅限公开披露范围。",
            "员工规模、代发体量等具体数字<b>需业务部门核实</b>，本报告不提供未经核实的推断值。",
        ],
    },
}


def _tag(level: str) -> str:
    """生成优先级标签。

    Args:
        level: a/b/c。

    Returns:
        str: HTML 片段。
    """
    label = {"a": "A 级 · 可独立承接", "b": "B 级 · 需联动", "c": "C 级 · 需总行条线"}[level]
    return f'<span class="tag {level}">{label}</span>'


def _p_tag(p: str) -> str:
    """生成干系人优先级标签。

    Args:
        p: A/B/C。

    Returns:
        str: HTML 片段。
    """
    return f'<span class="tag {p.lower()}">{p} 级</span>'


def render_brief(data: dict) -> str:
    """风格 A：一页纸速览。

    Args:
        data: 企业商机数据。

    Returns:
        str: HTML 文档。
    """
    e = html.escape
    stats = "".join(
        f'<div class="stat"><div class="k">{e(k)}</div>'
        f'<div class="v">{v}<span class="u">{u}</span></div></div>'
        for k, v, u in data["stats"]
    )
    opps = ""
    for level, title, who, how, why in data["opps"]:
        opps += (
            f'<div class="opp {level}">{_tag(level)}<span class="t">{e(title)}</span>'
            f'<div class="row"><b>对象：</b>{e(who)}</div>'
            f'<div class="row"><b>切入：</b>{e(how)}</div>'
            f'<div class="row"><b>依据：</b>{e(why)}</div></div>'
        )
    directions = "".join(
        f"<tr><td><b>{e(line)}</b></td><td>{e(direction)}</td><td>{e(detail)}</td></tr>"
        for line, direction, detail in data["directions"]
    )
    notes = "".join(f"<li>{n}</li>" for n in data["notes"])
    juris = (
        '<span class="tag n">辖内</span>'
        if data["jurisdiction"] == "辖内"
        else '<span class="tag w">非辖内</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>商机速览 · {e(data["name"])}</title><style>{CSS}</style></head>
<body><div class="wrap" style="--w:760px">
  <div class="head">
    <h1>{e(data["name"])} <span style="font-weight:400;opacity:.7;font-size:14px">{e(data["code"])}</span></h1>
    <div class="meta">{e(data["market"])} · {e(data["doc"])} · 注册地 {e(data["seat"])} {juris}</div>
    <div class="concl">{data["conclusion"]}</div>
  </div>

  <div class="stats">{stats}</div>

  <h2><span class="bar"></span>商机分档</h2>
  {opps}

  <h2><span class="bar"></span>推进方向</h2>
  <div class="card" style="padding:6px 4px">
    <table><thead><tr><th style="width:92px">条线</th><th style="width:120px">切入方向</th><th>说明</th></tr></thead>
    <tbody>{directions}</tbody></table>
  </div>

  <div class="card"><div class="muted"><b>说明与边界</b><ul style="margin:8px 0 0;padding-left:18px">{notes}</ul></div></div>

  <div class="foot">
    穿透链条：{e(data["chain"])}<br>
    境内主体：{e("；".join(data["domestic"]))}｜UBO：{e(data["ubo"])}｜披露股东：{e(data["disclosed"])}<br>
    数据来源：公开披露文件 + 工商登记数据，自动穿透生成
  </div>
</div></body></html>"""


def render_deck(data: dict, rows: list | None = None, paths: list | None = None) -> str:
    """风格 B：会前简报（首战指挥版）。

    Args:
        data: 企业商机数据。
        rows: 干系人作战表。
        paths: 接触路径。

    Returns:
        str: HTML 文档。
    """
    e = html.escape
    rows = rows or []
    paths = paths or []

    matrix = "".join(
        f"<tr><td><b>{e(t)}</b></td><td>{e(who)}</td><td>{_tag(level)}</td><td>{e(why)}</td></tr>"
        for level, t, who, _how, why in data["opps"]
    )
    stakeholder_rows = "".join(
        f"<tr><td>{_p_tag(r.priority)}</td><td><b>{e(r.name)}</b></td>"
        f"<td>{e(r.role)}</td><td>{e(r.needs)}</td><td>{e(r.hook)}</td>"
        f"<td>{e(r.reach_label)}</td><td class='muted'>{e(r.note)}</td></tr>"
        for r in rows
    )
    path_cards = "".join(
        f'<div style="padding:10px 0;border-bottom:1px dashed #ececef">'
        f'<div style="font-weight:600;font-size:13.5px;color:#1a1a1a">{e(name)}</div>'
        f'<div class="muted" style="margin-top:4px">{e(desc)}</div></div>'
        for name, desc in paths
    )
    directions = "".join(
        f"<tr><td><b>{e(line)}</b></td><td>{e(direction)}</td><td>{e(detail)}</td></tr>"
        for line, direction, detail in data["directions"]
    )
    notes = "".join(f"<li>{n}</li>" for n in data["notes"])
    domestic = "".join(f"<li>{e(d)}</li>" for d in data["domestic"])
    juris = "辖内" if data["jurisdiction"] == "辖内" else "非辖内（跨区）"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>会前简报 · {e(data["name"])}</title><style>{CSS}</style></head>
<body><div class="wrap" style="--w:1060px">

  <div class="head">
    <div style="font-size:12px;opacity:.75;letter-spacing:3px">红筹企业商机 · 会前简报</div>
    <h1 style="margin-top:6px">{e(data["name"])} <span style="font-weight:400;opacity:.7;font-size:14px">{e(data["code"])}</span></h1>
    <div class="meta">{e(data["market"])} · 披露文件：{e(data["doc"])} · 注册地 {e(data["seat"])} · <b>{e(juris)}</b></div>
    <div class="concl">{data["conclusion"]}</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>① 一页画像</h2>
    <table>
      <tr><td class="k">上市与披露</td><td>{e(data["market"])}，最新披露 {e(data["doc"])}</td>
          <td class="k">注册地 / 属地</td><td>{e(data["seat"])} · <b>{e(juris)}</b></td></tr>
      <tr><td class="k">穿透链条</td><td colspan="3">{e(data["chain"])}</td></tr>
      <tr><td class="k">境内主体</td><td colspan="3"><ul style="margin:0;padding-left:16px">{domestic}</ul></td></tr>
      <tr><td class="k">最终受益人</td><td>{e(data["ubo"])}</td>
          <td class="k">披露口径股东</td><td>{e(data["disclosed"])}</td></tr>
    </table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>② 触点地图：钱在哪里、谁做决策</h2>
    <table>
      <thead><tr><th>层面</th><th>主体</th><th>金融需求落点</th><th>可否独立承接</th></tr></thead>
      <tbody>
        <tr><td>境外股东层</td><td>{e(data["disclosed"])}</td><td>跨境结算、外汇避险</td>
            <td><span class="pill">需总行条线</span></td></tr>
        <tr><td>离岸/香港层</td><td>中间控股公司</td><td>境外账户与资金调拨</td>
            <td><span class="pill">需跨境联动</span></td></tr>
        <tr><td>境内 WFOE</td><td>{e(data["domestic"][0])}</td><td>结算、代发、票据、现金管理</td>
            <td><span class="pill">属地为主</span></td></tr>
        <tr><td>VIE 运营实体</td><td>{e(data["domestic"][-1])}</td><td>结算、代发、收单、经营贷</td>
            <td><span class="pill">属地为主</span></td></tr>
        <tr><td>个人层</td><td>{e(data["ubo"])}</td><td>私行、减持资金、跨境配置</td>
            <td><span class="pill">需联动或报送</span></td></tr>
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>③ 商机矩阵</h2>
    <table>
      <thead><tr><th style="width:26%">商机</th><th style="width:22%">目标对象</th>
      <th style="width:18%">优先级</th><th>判断依据</th></tr></thead>
      <tbody>{matrix}</tbody>
    </table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>④ 干系人作战表（首战切入点）</h2>
    <table>
      <thead><tr><th style="width:56px">优先级</th><th style="width:17%">干系人</th>
      <th style="width:17%">角色定位</th><th style="width:15%">需求落点</th>
      <th style="width:17%">切入由头</th><th style="width:12%">可触达性</th><th>备注</th></tr></thead>
      <tbody>{stakeholder_rows}</tbody>
    </table>
    <div class="muted" style="margin-top:10px">
      优先级判定：辖内主体可独立承接为 A；需引荐或联动为 B；离岸层与名义持股人为 C。
      <b>标注「名义持股人」的干系人不得作为营销对象</b>。
    </div>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>⑤ 接触路径（从谁撬动谁）</h2>
    {path_cards}
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>⑥ 推进方向</h2>
    <table>
      <thead><tr><th style="width:100px">条线</th><th style="width:140px">切入方向</th><th>说明</th></tr></thead>
      <tbody>{directions}</tbody>
    </table>
  </div>

  <div class="card">
    <h2 style="margin-top:0"><span class="bar"></span>⑦ 合规与边界</h2>
    <div class="muted"><ul style="margin:0;padding-left:18px">{notes}</ul></div>
  </div>

  <div class="foot">
    本简报由红筹架构自动穿透系统生成：公开披露文件 → 架构与股东提取 → 工商登记交叉核验 → 商机判断<br>
    所有企业信息均来自公开渠道；业务判断为分析建议，须经业务部门核实后使用
  </div>
</div></body></html>"""


def main() -> None:
    """生成高管商机报告（两种风格），并输出 gh-pages 用的 index.html。"""
    cfg = config_mod.get_settings()
    out_dir = cfg.redchip_output_dir / "briefs"
    out_dir.mkdir(parents=True, exist_ok=True)

    for code, data in BRIEFS.items():
        result_file = cfg.redchip_output_dir / code / "result.json"
        rows: list = []
        paths: list = []
        if result_file.exists():
            report = CompanyReport.model_validate(
                json.loads(result_file.read_text(encoding="utf-8"))
            )
            # 披露时点以穿透结果为准，避免手工维护漂移
            data["doc"] = f"{report.doc_kind}（{report.doc_published_at}）"
            rows = build_stakeholders(report, target_province=data["target_province"])
            paths = build_paths(rows)
            print(f"  → 干系人 {len(rows)} 条")

        brief_html = render_brief(data)
        deck_html = render_deck(data, rows, paths)

        (out_dir / f"{code}_{data['name']}_一页纸速览.html").write_text(
            brief_html, encoding="utf-8"
        )
        (out_dir / f"{code}_{data['name']}_会前简报.html").write_text(deck_html, encoding="utf-8")
        # gh-pages 入口：手机直接访问
        (out_dir / "index.html").write_text(deck_html, encoding="utf-8")
        (out_dir / "brief.html").write_text(brief_html, encoding="utf-8")
        print(f"{code} {data['name']}：已生成一页纸速览 / 会前简报 / index.html")

    print(f"\n输出目录：{out_dir}")


if __name__ == "__main__":
    main()
