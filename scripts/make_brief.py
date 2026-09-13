#!/usr/bin/env python3
"""生成面向银行高管的红筹企业商机报告（两种风格）。

风格 A —— 一页纸速览（Executive Brief）
    单页、结论先行、分档商机、行动明确，适合手机上 30 秒读完、邮件转发。

风格 B —— 会前简报（Briefing Deck）
    结构化深度版：画像 / 穿透 / 触点地图 / 商机矩阵 / 90 天行动 / 合规提示，
    适合上会前 5 分钟阅读或作为会议材料。

用法::

    python scripts/make_brief.py            # 生成 output/briefs/*.html
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from redchip import config as config_mod
from redchip.brief.stakeholders import build_paths, build_stakeholders, to_csv
from redchip.models.schema import CompanyReport

CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 16px 60px; background: #f1f5f9;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, Helvetica, sans-serif;
  color: #0f172a; -webkit-font-smoothing: antialiased; }
.wrap { max-width: var(--w, 780px); margin: 0 auto; }
.card { background: #fff; border: 1px solid #e2e8f0; border-radius: 14px;
  padding: 20px 22px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
.head { background: linear-gradient(135deg,#0f172a,#1e3a8a); color: #fff; border: none;
  border-radius: 14px; padding: 22px 24px; margin-bottom: 14px; }
.head h1 { margin: 0 0 6px; font-size: 22px; letter-spacing: .3px; }
.head .meta { font-size: 12.5px; opacity: .82; }
.head .concl { margin-top: 14px; padding: 12px 14px; background: rgba(255,255,255,.11);
  border-left: 3px solid #60a5fa; border-radius: 6px; font-size: 14px; line-height: 1.7; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.stat { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 12px 14px; }
.stat .k { font-size: 11.5px; color: #64748b; }
.stat .v { font-size: 19px; font-weight: 700; margin-top: 4px; color: #1e3a8a; }
.stat .u { font-size: 11px; color: #94a3b8; font-weight: 400; margin-left: 2px; }
h2 { font-size: 15px; margin: 22px 0 10px; display: flex; align-items: center; gap: 8px; }
h2 .bar { width: 3px; height: 15px; border-radius: 2px; background: #1d4ed8; }
.opp { border-left: 3px solid #cbd5e1; padding: 10px 0 10px 14px; margin-bottom: 12px; }
.opp.a { border-color: #ea580c; } .opp.b { border-color: #2563eb; } .opp.c { border-color: #94a3b8; }
.opp .t { font-size: 14.5px; font-weight: 600; }
.opp .row { font-size: 12.5px; color: #475569; margin-top: 5px; line-height: 1.65; }
.opp .row b { color: #0f172a; font-weight: 600; }
.tag { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
  font-weight: 600; margin-right: 6px; vertical-align: 1px; }
.tag.a { background: #ffedd5; color: #c2410c; }
.tag.b { background: #dbeafe; color: #1d4ed8; }
.tag.c { background: #e2e8f0; color: #475569; }
.tag.n { background: #dcfce7; color: #15803d; }
.tag.w { background: #fee2e2; color: #b91c1c; }
table { width: 100%; border-collapse: collapse; font-size: 12.8px; }
th { text-align: left; font-weight: 600; color: #475569; background: #f8fafc;
  padding: 9px 10px; border-bottom: 1px solid #e2e8f0; font-size: 12px; }
td { padding: 9px 10px; border-bottom: 1px solid #f1f5f9; vertical-align: top; line-height: 1.6; }
td.k { color: #64748b; width: 132px; }
.muted { color: #64748b; font-size: 12px; line-height: 1.7; }
.foot { text-align: center; color: #94a3b8; font-size: 11.5px; margin-top: 22px; line-height: 1.8; }
.pill { display:inline-block; padding:2px 7px; border-radius:6px; font-size:11px; background:#f1f5f9; color:#475569; margin-right:5px; }
"""


# ---------------------------------------------------------------------------
# 商机数据：结论来自穿透事实，业务判断标注依据
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
        "domestic": ["腾讯科技（深圳）有限公司（WFOE）", "深圳市腾讯计算机系统有限公司（VIE 运营实体）"],
        "ubo": "马化腾（境内 VIE 登记持股 54.29%）",
        "disclosed": "MIH Internet Holdings B.V. 22.80%、Advance Data Services Limited 8.82%",
        "conclusion": (
            "架构清晰、境内主体在深圳且属<b>辖内</b>。但成熟期互联网巨头的主体授信非分行角色，"
            "真正可落地的是<b>员工端与生态商户端</b>两扇门——这是零售/普惠条线能直接承接的商机。"
        ),
        "stats": [
            ("境内实体", "2", "家"),
            ("穿透层级", "4", "层"),
            ("最终受益人", "1", "位"),
            ("披露时点", "2026-04", ""),
        ],
        "opps": [
            ("a", "员工金融：代发 + 信用卡 + 消费贷",
             "腾讯科技（深圳）/ 深圳市腾讯计算机系统",
             (
                 "以两家境内主体为锚，对接深圳及广州研发、运营团队的代发工资、信用卡、消费信贷与房贷需求；"
                 "先拿代发，再带动信用卡与财富。"
             ),
             "境内两家实体承载主要业务与人员（年报 p.13、p.45）"),
            ("a", "生态商户收单与小微经营贷",
             "本行辖内的游戏/内容/电商服务商、微信生态服务商",
             (
                 "以腾讯生态服务商名单为线索，做对公开户 + 收单 + 基于流水的经营性贷款；"
                 "此条不依赖腾讯主体配合，可独立推进。"
             ),
             "VIE 运营实体持有增值电信等牌照、承载平台业务（年报 p.45）"),
            ("b", "高管与核心员工财富管理",
             "UBO 马化腾及在深高管、核心骨干",
             (
                 "围绕股权激励行权、减持变现后的资金承接与跨境配置，配置私行团队前置接触；"
                 "需与深圳分行联动（客户主账户多不在我行）。"
             ),
             "境内 VIE 登记股东含马化腾 54.29%、张志东 22.86%（工商登记口径）"),
            ("b", "员工持股平台服务",
             "持股平台及其激励对象",
             "员工持股平台的资金结算、代发与股权质押融资；RSU/期权行权涉及的结售汇与个税代扣配套。",
             "年报披露存在股权激励安排（20-F/年报「Equity Incentive Plans」类章节）"),
            ("c", "境外股东关联方跨境服务",
             "MIH（Naspers/Prosus 体系）、Advance Data Services",
             (
                 "上述股东若在境内有关联主体或资金安排，可承接跨境结算与外汇避险；"
                 "属长线机会，需总行跨境条线协同。"
             ),
             "年报 p.79 第XV部披露：MIH 22.80%、Advance Data 8.82%"),
        ],
        "actions": [
            ("个金部", "梳理深圳/广州两地腾讯系员工规模与代发线索，30 天内完成名单与接触路径", "30 天"),
            ("公司部", "圈定辖内腾讯生态服务商清单，以收单+经营贷切入，首批 20 户", "45 天"),
            ("私行中心", "经深圳分行联动，安排 UBO 及高管圈层触达（先触达，不承诺）", "90 天"),
        ],
        "notes": [
            "本报告结论全部基于公开披露文件（港交所年报）+ 工商登记数据，不含我行内部客户数据。",
            "营销触达须遵守客户信息保护与适当性要求；涉及境外股东的信息仅限公开披露范围。",
            "员工规模、代发体量等具体数字<b>需业务部门核实</b>，本报告不提供未经核实的推断值。",
        ],
    },
    "BABA": {
        "name": "阿里巴巴",
        "code": "BABA",
        "market": "美股（20-F）",
        "doc": "FY2026 20-F（2026-05-20）",
        "seat": "浙江省杭州市",
        "target_province": "浙江省",
        "jurisdiction": "非辖内",
        "chain": "开曼上市主体 → 离岸/香港层 → 阿里巴巴（中国）/ 淘宝（中国）软件 → VIE → 浙江淘宝网络、浙江天猫网络",
        "domestic": [
            "阿里巴巴（中国）有限公司、淘宝（中国）软件有限公司（WFOE）",
            "浙江淘宝网络有限公司、浙江天猫网络有限公司（VIE 运营实体）",
        ],
        "ubo": "郑俊芳、邵晓锋、吴泽明、蒋方（各 25%，VIE 指定持股人）",
        "disclosed": "马云及其关联方 8.8%；董事及高管合计 1.9%",
        "conclusion": (
            "注册与主体均在杭州，<b>非辖内</b>，分行不做主体。可行路径是"
            "<b>生态侧切入 + 属地联动</b>：本地商家与阿里系区域实体是分行能独立拿下的部分；"
            "注意 VIE 登记股东是<b>代持</b>，营销对象应锚定决策人而非登记人。"
        ),
        "stats": [
            ("境内实体", "4", "家"),
            ("穿透层级", "4", "层"),
            ("最终受益人", "4", "位"),
            ("披露时点", "2026-05", ""),
        ],
        "opps": [
            ("a", "本地生态商家：收单 + 结算 + 小微贷",
             "本行辖内的天猫/淘宝卖家、服务商、本地生活商户",
             (
                 "以平台商家为客群做收单、结算与基于经营流水的贷款，属地分行可独立推进，"
                 "不依赖阿里总部配合。"
             ),
             "VIE 运营实体（浙江淘宝/天猫网络）持有平台与支付相关牌照（20-F p.284）"),
            ("a", "阿里系区域实体：对公结算 + 代发",
             "阿里云、菜鸟、本地生活在广东的分支/子公司",
             "承接区域实体对公开户、资金结算与员工代发，属可独立触达的属地客群。",
             "20-F 明确集团含多家 PRC subsidiaries（含区域运营主体）"),
            ("b", "本地员工消费金融",
             "阿里系区域实体在粤员工",
             "代发落地后带动信用卡、消费贷与房贷；与 A 类两项形成组合拳。",
             "集团在境内设有多家运营实体（20-F Item 4.C）"),
            ("c", "高管/实控人私行服务",
             "马云及核心管理层（主要在杭州）",
             (
                 "<b>异地客户</b>，需总行私人银行与属地分行联动，分行单独难以承接；"
                 "建议作为名单报送而非直接营销。"
             ),
             "20-F Item 7：马云及其关联方持股 8.8%"),
            ("c", "跨境与境外架构配套",
             "开曼/香港层及境外机构股东",
             "跨境结算、外汇避险等，需总行跨境条线主导。",
             "离岸层与香港层为持股架构（20-F Item 4.C）"),
        ],
        "actions": [
            ("公司部", "梳理辖内天猫/淘宝头部商家与服务商，首批 30 户切入收单与结算", "30 天"),
            ("公司部", "摸排阿里系区域实体在粤机构清单，建立触达路径", "45 天"),
            ("私行中心", "将高管/实控人名单报总行私行，按联动机制推进（不直接营销）", "90 天"),
        ],
        "notes": [
            "本报告结论全部基于公开披露文件（SEC 20-F）+ 工商登记数据，不含我行内部客户数据。",
            (
                "<b>VIE 登记股东为名义持股人</b>（20-F 披露为 designated individuals），"
                "不代表最终受益权益；营销定位应以实际决策人为准。"
            ),
            "异地客户的营销须遵守属地分工与客户归属规则，避免跨区冲突。",
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
    label = {"a": "A 级 · 本季度可动", "b": "B 级 · 半年内", "c": "C 级 · 长线"}[level]
    return f'<span class="tag {level}">{label}</span>'


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
    actions = "".join(
        f"<tr><td><b>{e(who)}</b></td><td>{e(what)}</td><td>{e(when)}</td></tr>"
        for who, what, when in data["actions"]
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
<body><div class="wrap" style="--w:780px">
  <div class="head">
    <h1>{e(data["name"])} <span style="font-weight:400;opacity:.7;font-size:15px">{e(data["code"])}</span></h1>
    <div class="meta">{e(data["market"])} · {e(data["doc"])} · 注册地 {e(data["seat"])} {juris}</div>
    <div class="concl">{data["conclusion"]}</div>
  </div>

  <div class="stats">{stats}</div>

  <h2><span class="bar"></span>商机分档</h2>
  {opps}

  <h2><span class="bar"></span>下一步行动</h2>
  <div class="card" style="padding:8px 4px">
    <table><thead><tr><th style="width:110px">责任</th><th>动作</th><th style="width:70px">时限</th></tr></thead>
    <tbody>{actions}</tbody></table>
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
    rows = rows or []
    paths = paths or []
    e = html.escape
    matrix = "".join(
        f'<tr><td><b>{e(t)}</b></td><td>{e(who)}</td><td>{_tag(level)}</td><td>{e(why)}</td></tr>'
        for level, t, who, _how, why in data["opps"]
    )
    actions = "".join(
        f'<tr><td><b>{e(who)}</b></td><td>{e(what)}</td><td>{e(when)}</td></tr>'
        for who, what, when in data["actions"]
    )
    notes = "".join(f"<li>{n}</li>" for n in data["notes"])
    domestic = "".join(f"<li>{e(d)}</li>" for d in data["domestic"])
    juris = "辖内" if data["jurisdiction"] == "辖内" else "非辖内（跨区）"

    def _p_tag(p: str) -> str:
        cls = {"A": "a", "B": "b", "C": "c"}[p]
        return f'<span class="tag {cls}">{p} 级</span>'

    stakeholder_rows = "".join(
        f"<tr><td>{_p_tag(r.priority)}</td><td><b>{e(r.name)}</b></td>"
        f"<td>{e(r.role)}</td><td>{e(r.needs)}</td><td>{e(r.hook)}</td>"
        f"<td>{e(r.reach_label)}</td>"
        f"<td class='muted'>{e(r.note)}</td></tr>"
        for r in rows
    )
    path_cards = "".join(
        f'<div style="padding:10px 0;border-bottom:1px dashed #e2e8f0">'
        f'<div style="font-weight:600;font-size:13.5px">{e(name)}</div>'
        f'<div class="muted" style="margin-top:4px">{e(desc)}</div></div>'
        for name, desc in paths
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>会前简报 · {e(data["name"])}</title><style>{CSS}</style></head>
<body><div class="wrap" style="--w:1060px">

  <div class="head">
    <div style="font-size:12px;opacity:.7;letter-spacing:2px">红筹企业商机 · 会前简报</div>
    <h1 style="margin-top:6px">{e(data["name"])} <span style="font-weight:400;opacity:.7;font-size:15px">{e(data["code"])}</span></h1>
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
      <thead><tr><th>层面</th><th>主体</th><th>金融需求落点</th><th>我行可否独立承接</th></tr></thead>
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
      <thead><tr><th style="width:56px">优先级</th><th style="width:18%">干系人</th>
      <th style="width:18%">角色定位</th><th style="width:16%">需求落点</th>
      <th style="width:18%">切入由头</th><th style="width:13%">可触达性</th><th>备注</th></tr></thead>
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
    <h2 style="margin-top:0"><span class="bar"></span>⑥ 90 天行动表</h2>
    <table><thead><tr><th style="width:110px">责任</th><th>动作</th><th style="width:70px">时限</th></tr></thead>
    <tbody>{actions}</tbody></table>
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
    """生成两种风格的高管商机报告。"""
    cfg = config_mod.get_settings()
    out_dir = cfg.redchip_output_dir / "briefs"
    out_dir.mkdir(parents=True, exist_ok=True)

    for code, data in BRIEFS.items():
        # 用真实穿透结果覆盖披露时点，避免手工维护漂移
        result_file = cfg.redchip_output_dir / code / "result.json"
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            data["doc"] = f"{result.get('doc_kind', '')}（{result.get('doc_published_at', '')}）"

        rows: list = []
        paths: list = []
        if result_file.exists():
            report = CompanyReport.model_validate(
                json.loads(result_file.read_text(encoding="utf-8"))
            )
            rows = build_stakeholders(report, target_province=data["target_province"])
            paths = build_paths(rows)
            csv_path = to_csv(rows, out_dir / f"{code}_{data['name']}_干系人线索.csv")
            print(f"  → 干系人 {len(rows)} 条：{csv_path.name}")

        brief = out_dir / f"{code}_{data['name']}_一页纸速览.html"
        deck = out_dir / f"{code}_{data['name']}_会前简报.html"
        brief.write_text(render_brief(data), encoding="utf-8")
        deck.write_text(render_deck(data, rows, paths), encoding="utf-8")
        print(f"{code} {data['name']}：{brief.name} / {deck.name}")

    print(f"\n输出目录：{out_dir}")


if __name__ == "__main__":
    main()
