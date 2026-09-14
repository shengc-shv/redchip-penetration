"""生成「2026 港交所递表 · 广东商机 BD 台账」（新口径：无广东闸门 + 封面页权威注册地）。

口径（用户 2026-09-13 拍板）
---------------------------
- 时间：递交日 ≥ 2026-01-01
- 状态：只留 **处理中 / 处理中(含PHIP) / 已上市**；剔除失效·撤回·被拒绝·被发回
- 地域：**不做广东预过滤**。广东 = ①名称通道（境内 H 股中文名含地名）∪ ②正文通道（离岸壳名的
  「概要/公司资料」分册广东城市词线索），两个派生视图，不是闸门
- 注册地：**封面页/扉页固定句式**（`hkex_cover`），不采信正文其他章节的法域提及

原则（遵循项目「只呈现事实」约束）
----------------------------------
- 业务场景 = 由「状态 + 注册地 + 控制方式(VIE)」推导的**可触达业务信号**，非行动建议/排期。
- 关系状态（本行是否已开户 / 他行锁定）公开不可得，统一标「待行内核验」。

用法::

    PYTHONPATH=src python scripts/make_bd_ledger.py
    PYTHONPATH=src python scripts/make_bd_ledger.py --min-year 2026 --status active,listed
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter

from redchip import config as config_mod
from redchip.overseas import hkex_listing as L


def biz_scenarios(rec: L.ListingRecord) -> str:
    """由状态 + 注册地 + 控制方式推导可触达业务场景信号（事实映射，非建议）。"""
    tags: list[str] = []
    if rec.status_raw == "listed":
        tags += ["上市后募资存续", "高管个人金融"]
    elif rec.status_raw == "active":
        tags += ["上市筹备期融资/保函", "保荐承销协同"]
    if rec.is_offshore:
        tags += ["跨境资金池/内保外贷", "FT 账户"]
    elif rec.domicile == "中国(境内)":
        tags += ["境内主体授信/结算"]
    if rec.is_vie:
        tags += ["VIE 协议控制合规与资金安排"]
    return "、".join(tags) if tags else "—"


def _vie(r: L.ListingRecord) -> str:
    """VIE 列：是 / 否（曾存在已终止安排）/ 待核验。"""
    if not r.vie_scanned:
        return "待核验"
    if r.is_vie:
        return "是"
    return "否(仅历史安排)" if r.vie_terminated else "否"


def _gd_tier(r: L.ListingRecord, min_hits: int = 3) -> str:
    """广东线索分层（按命中所在分册的权威性；词频须达 ``min_hits`` 才给层级）。

    - ``核心``：命中于「概要（SUMMARY）」或「公司资料（CORPORATE INFORMATION）」——公司自述章，
      描述自身总部 / 运营实体 / 主要银行，语义最贴近「运营地」；
    - ``补充``：仅命中「历史沿革（HISTORY）」章——该章含**股东、投资方、中介机构地址**，
      城市名噪音显著更高，故降级为补充；
    - 空：未命中。
    """
    if r.gd_opco_count < min_hits:
        return ""
    src = (r.gd_opco_source or "").split("|")[0].strip().upper()
    if src.startswith("LISTEDCO"):
        return "补充(listedco全文)"
    if "SUMMARY" in src or "CORPORATE INFORMATION" in src:
        return "核心"
    # 「历史沿革」章里**集团自述实体**（our subsidiary / operating entity / wholly-owned）
    # 的语境不是噪音，应归核心层；只有第三方地址（股东 / 投资方 / 中介）才算补充。
    # 实测：Exegenesis 的「广州嘉因 = 主要 PRC 运营实体」正是写在沿革章（2026-09-14 补）。
    if getattr(r, "gd_opco_entity_count", 0) >= min_hits:
        return "核心(沿革章·实体内述)"
    return "补充(仅历史沿革章)"


def _doc(r: L.ListingRecord) -> str:
    if r.multi_url or r.app_proof_url:
        return "有申请版本"
    if r.stock_code:
        return "listedco"
    return "官方未刊发"


def _row(i: int, r: L.ListingRecord) -> str:
    gd = r.gd_l1_hit or ("正文线索" if r.gd_opco_count else "—")
    return (
        f"| {i} | {r.display_name} | {r.board} | {r.status} | {r.stock_code or '—'} | {r.submit_date} | "
        f"{r.domicile or '待核验'} | {'是' if r.is_offshore else '—'} | {gd} | "
        f"{_vie(r)} | {r.redchip_l2 or '待核验'} | "
        f"{_doc(r)} | 待行内核验 |"
    )


_HEAD = [
    "| # | 企业名称 | 板块 | 状态 | 代码 | 递表日 | 注册地(封面页) | 离岸 | 广东 | VIE | 红筹判定 | 文档 | 关系状态 |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-year", type=int, default=2026)
    ap.add_argument("--status", default="active,listed")
    args = ap.parse_args()
    keep = {t.strip() for t in args.status.split(",") if t.strip()}

    settings = config_mod.get_settings()
    records = L.load_state(settings.redchip_data_dir / "listing_full_state.json")
    if not records:
        raise SystemExit("缺少 data/listing_full_state.json，请先跑 scripts/run_full_scan.py")
    records = [r for r in records
               if r.submit_date and int(r.submit_date[:4]) >= args.min_year and r.status_raw in keep]
    records.sort(key=lambda r: (r.submit_date, r.display_name))

    off = [r for r in records if r.is_offshore]
    gd_core = sorted([r for r in off if _gd_tier(r).startswith("核心")], key=lambda r: -r.gd_opco_count)
    gd_supp = sorted([r for r in off if _gd_tier(r).startswith("补充")], key=lambda r: -r.gd_opco_count)
    gd_red = gd_core + gd_supp
    gd_h = [r for r in records if r.gd_l1 and not r.is_offshore]
    unver = [r for r in records if not r.domicile]
    vie = [r for r in records if r.is_vie]

    now = dt.datetime.now(config_mod.CST).strftime("%Y-%m-%d %H:%M")
    c_dom = Counter(r.domicile or "待核验" for r in records)
    c_st = Counter(r.status for r in records)

    out: list[str] = [
        "# 2026 港交所递表 · 广东商机 BD 台账",
        "",
        "> **数据来源**：港交所披露易「新上市申請版本及相關資料」官方静态 JSON（全状态 × 板块，免鉴权，已按 id 去重）。",
        "> **口径**：递交日 ≥ 2026-01-01，且状态 ∈ {处理中, 处理中(含PHIP), 已上市}；"
        "失效·撤回·被拒绝·被发回等无商机状态已剔除。",
        "> **注册地口径**：申请版本**封面页/扉页固定句式**（经 Multi-Files 分册目录只下載封面分册 ~110KB 后抽取）；"
        "**不采信正文其他章节的法域提及**。已上市记录官方清单不含申请版本，改走 `listedco/listconews` 招股章程首页。",
        "> **广东口径**：**不做名称预过滤**。①名称通道 = 境内 H 股中文名含地名；"
        "②正文通道 = 离岸壳名企业经「概要/公司资料」分册的广东城市词**线索**（词频，须人工复核）。",
        f"> **生成时间**：{now}（北京时间）。",
        "> ⚠️ 行外公开信息初筛，须经行内渠道复核：关系状态（是否已开户/他行锁定）公开不可得，标「待行内核验」。",
        "",
        "## 一、总体概览",
        f"- 人口（2026 以来 · 在审+已上市）：**{len(records)} 家**",
        f"- 状态分布：{'；'.join(f'{k} {v}' for k, v in sorted(c_st.items(), key=lambda x: -x[1]))}",
        f"- 注册地分布（封面页）：{'；'.join(f'{k} {v}' for k, v in sorted(c_dom.items(), key=lambda x: -x[1]))}",
        f"- 离岸注册（红筹形态）：**{len(off)} 家**；其中检出广东运营线索 **{len(gd_red)} 家**",
        f"- 境内注册（H 股）：**{c_dom.get('中国(境内)', 0)} 家**；其中名称命中广东 **{len(gd_h)} 家**",
        f"- VIE（协议控制）**{len(vie)} 家**",
        f"- **广东并集：{len(gd_red) + len(gd_h)} 家**（离岸正文线索 {len(gd_red)} + 境内名称 {len(gd_h)}）",
        "",
        "## 二、广东红筹候选 · 核心层（公司自述章命中，本台账重点）",
        "",
        f"> 共 **{len(gd_core)} 家**。上市主体注册在开曼（壳名不含地名），银行可切入的是其**境内运营实体**。"
        "线索强度 = 广东城市词频（命中于「概要 / 公司资料」章），**须人工复核原文**。",
        "",
        "| # | 企业名称 | 状态 | 代码 | 递表日 | 注册地 | 广东线索词频 | 词频构成 | 来源分册 | VIE | 可触达业务场景 | 关系状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(gd_core, 1):
        comp = r.gd_opco_source.split("|")[-1].strip() if "|" in r.gd_opco_source else ""
        sec = r.gd_opco_source.split("|")[0].strip()
        out.append(
            f"| {i} | {r.display_name} | {r.status} | {r.stock_code or '—'} | {r.submit_date} | {r.domicile} | "
            f"{r.gd_opco_count} | {comp} | {sec[:38]} | {_vie(r)} | "
            f"{biz_scenarios(r)} | 待行内核验 |"
        )
    out += [
        "",
        "## 二·补、广东红筹候选 · 补充层（仅历史沿革章命中）",
        "",
        f"> 共 **{len(gd_supp)} 家**。「历史沿革」章含股东 / 投资方 / 中介机构地址，城市名噪音显著更高，"
        "降级为补充层，**须逐家复核原文**。",
        "",
        "| # | 企业名称 | 状态 | 代码 | 递表日 | 注册地 | 广东线索词频 | 词频构成 | 来源分册 | VIE | 关系状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(gd_supp, 1):
        comp = r.gd_opco_source.split("|")[-1].strip() if "|" in r.gd_opco_source else ""
        sec = r.gd_opco_source.split("|")[0].strip()
        out.append(
            f"| {i} | {r.display_name} | {r.status} | {r.stock_code or '—'} | {r.submit_date} | {r.domicile} | "
            f"{r.gd_opco_count} | {comp} | {sec[:38]} | {_vie(r)} | 待行内核验 |"
        )
    out += [
        "",
        "## 三、广东 H 股（境内注册 + 名称含广东地名）",
        "",
        f"> 共 **{len(gd_h)} 家**。境内注册主体即为可开户/可授信实体，名称已确认属地。",
        "",
        *_HEAD,
    ]
    out += [_row(i, r) for i, r in enumerate(gd_h, 1)]

    rest = [r for r in records if r not in gd_red and r not in gd_h]
    out += [
        "",
        "## 四、其余人口（非广东，供全国口径参考）",
        "",
        "| # | 企业名称 | 板块 | 状态 | 代码 | 递表日 | 注册地(封面页) | 离岸 | VIE | 红筹判定 | 文档 | 关系状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rest, 1):
        out.append(
            f"| {i} | {r.display_name} | {r.board} | {r.status} | {r.stock_code or '—'} | {r.submit_date} | "
            f"{r.domicile or '待核验'} | {'是' if r.is_offshore else '—'} | {_vie(r)} | "
            f"{r.redchip_l2 or '待核验'} | {_doc(r)} | 待行内核验 |"
        )

    out += [
        "",
        "## 五、待核验与不确定项",
        f"- **注册地未判定 {len(unver)} 家**：官方未刊发申请版本（秘密递表）/ 已上市 listedco 取数失败 / 封面句未命中。",
        "- **广东正文线索非结论**：词频仅提示运营实体可能在粤，须逐家复核招股书「概要·历史沿革」原文；"
        "境外业务主体在粤设点亦会被计入。",
        "- **未检出 ≠ 非广东**：分册未陈述或词频低于阈值的离岸企业，不能据此排除广东。",
        "- **VIE 为线索**：只认专有表述，仍可能漏判（如披露未使用标准术语）；原文字段 `vie_evidence` 可复核。",
        "- **关系状态**：本行是否已开户 / 他行锁定公开不可得，须行内 CRM 回填。",
        "",
        "## 六、方法论修正（必读）",
        "- 旧口径扫整份 PDF 250 页、按「离岸 > 境内 > 香港」取优先级，**系统性高估红筹**："
        "正文里偶发一处 Cayman 提及即被当作注册地。复核历史 84 家，**翻案 19 条**（全部「离岸 → 中国境内」），"
        "涉及广州极飞、深圳迈瑞、惠州胜宏、广东领益智造、深圳星源材质、广东天农、真健康等。"
        "**旧台账的红筹名单不可再用**。",
        "- VIE 口径同步收严：旧口径用裸词 `contractual arrangements` 判 VIE，把一地 H 股（深圳承泰/天赐高新/"
        "德赛西威）误标；新口径只认 `variable interest entity` / `VIE` / `协议控制`，弱信号须与 WFOE/nominee 同现。",
        "- **边界**：`is_vie` 只描述控制方式，**不参与地域判定**；广东成员只由名称与正文地域线索决定。",
        "",
    ]

    path = settings.redchip_output_dir / f"gd_hk_ipo_{args.min_year}_bd_ledger.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"BD 台账已写出：{path}")
    print(f"  人口 {len(records)}｜离岸 {len(off)}（广东核心 {len(gd_core)} + 补充 {len(gd_supp)}）｜"
          f"广东 H 股 {len(gd_h)}｜广东并集 {len(gd_core) + len(gd_h)}（核心口径）｜VIE {len(vie)}｜未判定 {len(unver)}")


if __name__ == "__main__":
    main()
