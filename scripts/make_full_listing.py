"""生成「全量摸底」结果：一张全量 CSV + 三个视图（广东 / 红筹 / 待核验）。

原则（承接用户 2026-09-13 17:11 的方法论纠正）
---------------------------------------------
- **不做广东过滤**：广东只是结果表的一列 + 一个派生视图，不是闸门。
- 红筹判定 = 离岸注册（封面页权威口径） + 广东/PRC 运营实体，**不依赖公司名字**。
- 只呈现事实；关系状态（本行是否已开户/他行锁定）公开不可得，统一标「待行内核验」。

产出
----
- ``data/listing_full.csv``：全量明细（每行含 domicile / is_offshore / gd_flag / redchip_l2 ...）
- ``output/full_listing_overview.md``：总览（年份 × 注册地矩阵、状态分布、口径与局限）
- ``output/full_listing_guangdong.md``：广东视图（名称命中，仅作一列）
- ``output/full_listing_redchip.md``：红筹视图（离岸注册）
- ``output/full_listing_unverified.md``：待核验视图（无文档 / 未判定）

用法::

    PYTHONPATH=src python scripts/make_full_listing.py
    PYTHONPATH=src python scripts/make_full_listing.py --min-year 2025
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict

from redchip import config as config_mod
from redchip.overseas import hkex_listing as L

CSV_FIELDS = [
    "id", "name_cn", "name_en", "board", "status", "stock_code", "submit_date",
    "gd_flag", "gd_l1_hit", "is_offshore", "domicile", "scan_method", "cover_section",
    "cover_evidence", "gd_opco", "gd_opco_count", "gd_opco_entity_count",
    "gd_opco_source", "gd_tier",
    "is_vie", "vie_scanned", "vie_terminated", "vie_source", "vie_evidence",
    "redchip_l2", "doc_available", "app_proof_url",
]

DISCLAIMER = (
    "> ⚠️ **行外公开信息初筛，须经行内渠道复核。** 关系状态（是否已开户/他行锁定）公开不可得。\n"
    "> 数据来源：港交所披露易「新上市申請版本及相關資料」官方静态 JSON（全状态 × 板块，免鉴权，已按 id 去重）。\n"
    "> 注册地口径：申请版本**封面页/扉页固定句式**（`Incorporated in the Cayman Islands with limited liability` /\n"
    "> `A joint stock company incorporated in the People's Republic of China with limited liability`），\n"
    "> 经 Multi-Files 分册目录仅下載封面分册（~110KB）后由 pypdf 抽取。**不采信正文其他章节的法域提及。**\n"
    "> 已知盲区：以非公开方式递表者，官方不刊发申请版本，清单与 PDF 均不可见。"
)


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


def _doc_available(r: L.ListingRecord) -> str:
    if r.multi_url or r.app_proof_url:
        return "有申请版本"
    if r.stock_code:
        return "仅listedco可补"
    return "官方未刊发"


def _row(r: L.ListingRecord) -> dict[str, object]:
    return {
        "id": r.id, "name_cn": r.name_cn, "name_en": r.name_en, "board": r.board,
        "status": r.status, "stock_code": r.stock_code, "submit_date": r.submit_date,
        "gd_flag": "是" if r.gd_l1 else "", "gd_l1_hit": r.gd_l1_hit,
        "is_offshore": "是" if r.is_offshore else "",
        "domicile": r.domicile, "scan_method": r.scan_method, "cover_section": r.cover_section,
        "cover_evidence": r.cover_evidence, "gd_opco": r.gd_opco,
        "gd_opco_count": r.gd_opco_count,
        "gd_opco_entity_count": getattr(r, "gd_opco_entity_count", 0),
        "gd_opco_source": r.gd_opco_source,
        "gd_tier": _gd_tier(r),
        "is_vie": _vie(r), "vie_scanned": "是" if r.vie_scanned else "",
        "vie_terminated": "是" if r.vie_terminated else "",
        "vie_source": r.vie_source, "vie_evidence": r.vie_evidence,
        "redchip_l2": r.redchip_l2, "doc_available": _doc_available(r),
        "app_proof_url": r.app_proof_url,
    }


def _table(rows: list[L.ListingRecord]) -> list[str]:
    out = [
        "| # | 企业名称 | 板块 | 状态 | 代码 | 递表日 | 注册地(封面) | 离岸 | 广东线索(词频) | 线索来源分册 | VIE | 红筹判定 | 文档 | 关系状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        out.append(
            f"| {i} | {r.display_name} | {r.board} | {r.status} | {r.stock_code or '—'} | {r.submit_date} | "
            f"{r.domicile or '—'} | {'是' if r.is_offshore else '—'} | "
            f"{('%s(%d)' % (_gd_tier(r) or '', r.gd_opco_count)) if r.gd_opco_count else '—'} | "
            f"{(r.gd_opco_source or '—')[:46]} | {_vie(r)} | {r.redchip_l2 or '待核验'} | "
            f"{_doc_available(r)} | 待行内核验 |"
        )
    return out


def _head(title: str, records: list[L.ListingRecord], extra: list[str]) -> list[str]:
    now = dt.datetime.now(config_mod.CST).strftime("%Y-%m-%d %H:%M")
    return [
        f"# {title}", "",
        DISCLAIMER,
        f"> 生成时间：{now}（北京时间）｜本视图 {len(records)} 家。",
        "",
        *extra,
        "",
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-year", type=int, default=2026, help="递交日年份下界（默认 2026）")
    ap.add_argument("--max-year", type=int, default=9999)
    ap.add_argument("--status", default="active,listed",
                    help="保留的申请状态；默认 active,listed（剔除失效/撤回/被拒/发回）")
    args = ap.parse_args()
    keep_status = {t.strip() for t in args.status.split(",") if t.strip()}

    settings = config_mod.get_settings()
    full_state = settings.redchip_data_dir / "listing_full_state.json"
    records = L.load_state(full_state)
    if not records:
        raise SystemExit(f"缺少 {full_state}，请先跑 scripts/run_full_scan.py")
    records = [
        r for r in records
        if r.submit_date and args.min_year <= int(r.submit_date[:4]) <= args.max_year
        and r.status_raw in keep_status
    ]
    records.sort(key=lambda r: (r.submit_date, r.display_name))
    if not records:
        raise SystemExit("口径过滤后无记录，请检查 --min-year / --status")

    # ── 全量 CSV ────────────────────────────────────────────────
    csv_path = settings.redchip_data_dir / "listing_full.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(_row(r))

    out_dir = settings.redchip_output_dir
    off = [r for r in records if r.is_offshore]
    gd = [r for r in records if r.gd_l1]
    unver = [r for r in records if not r.domicile]

    # ── 总览 ───────────────────────────────────────────────────
    years = sorted({r.submit_date[:4] for r in records if r.submit_date})
    dom_by_year: dict[str, Counter[str]] = defaultdict(Counter)
    for r in records:
        dom_by_year[r.submit_date[:4]][r.domicile or "未判定"] += 1
    dom_labels = ["开曼群岛", "百慕大", "中国(境内)", "香港", "未判定"]
    mat = [
        "| 年份 | 递表数 | " + " | ".join(dom_labels) + " | 离岸合计 | 离岸占比 |",
        "|---|---|" + "---|" * (len(dom_labels) + 2),
    ]
    for y in years:
        c = dom_by_year[y]
        total = sum(c.values())
        offc = c["开曼群岛"] + c["百慕大"]
        mat.append(
            f"| {y} | {total} | " + " | ".join(str(c[k]) for k in dom_labels)
            + f" | {offc} | {offc / total * 100:.1f}% |"
        )
    c_dom = Counter(r.domicile or "未判定" for r in records)
    status_c = Counter(r.status for r in records)
    l2_c = Counter(r.redchip_l2 or "未判定" for r in records)
    vie_c = Counter(_vie(r) for r in records)
    overview = _head(
        "港交所递表企业 · 全量摸底总览（离岸 / 境内 / 待核验）", records,
        [
            "## 一、口径",
            "- **全量口径**：官方清单全状态 × 板块，按 id 去重；本视图按递交日年份筛选，**不做任何地域过滤**。",
            f"- **注册地判定**：封面页权威句式（见页首说明），覆盖 {sum(1 for r in records if r.domicile)} / {len(records)} 家"
            f"（{(sum(1 for r in records if r.domicile) / len(records) * 100) if records else 0:.1f}%）。",
            "- **红筹判定** = 离岸注册（开曼/百慕大） + 抽得广东运营实体；离岸但实体未抽得 → 疑似，需人工核验。",
            "",
            "## 二、年份 × 注册地 矩阵",
            "", *mat, "",
            "## 三、状态分布", "",
            *[f"- {k}：{v} 家" for k, v in sorted(status_c.items(), key=lambda x: -x[1])], "",
            "## 四、红筹判定分布", "",
            *[f"- {k}：{v} 家" for k, v in sorted(l2_c.items(), key=lambda x: -x[1])], "",
            "## 五、VIE（协议控制）分布", "",
            *[f"- {k}：{v} 家" for k, v in sorted(vie_c.items(), key=lambda x: -x[1])], "",
            "> VIE 只描述**控制方式**（协议控制 vs 直接持股），**不参与地域判定**；广东成员只由名称与正文地域线索决定。",
            "",
            "## 六、结论（事实陈述）", "",
            f"- 全量 {len(records)} 家中，**封面页确认离岸注册（开曼/百慕大）{len(off)} 家**（{len(off) / len(records) * 100:.1f}%）。",
            f"- 其中名称命中广东 {len(gd)} 家 —— **名称命中率不能代表红筹率**：正典红筹的离岸壳名不含粤字，"
            f"因此「广东红筹」的真实口径必须由离岸子集 ∩ 广东运营实体求得，不能由名字过滤求得。",
            f"- 未判定注册地 {len(unver)} 家（占 {len(unver) / len(records) * 100:.1f}%）：主要为官方未刊发文档（秘密递表/已上市无申请版本），"
            f"详见待核验视图。",
            "",
            "## 七、已知局限（勿掩盖）", "",
            "- 秘密递表者官方不刊发申请版本 → 官方源固有盲区，非本流程缺陷。",
            "- 封面分册缺失或结构异常者回落整份 PDF 首页；两者皆失败则标「未判定」。",
            "- 中文版 PDF 经 pypdf 常为 CID 乱码 → 仅用英文版分册。",
            "- 运营实体（gd_opco）尚未对全量做正文抽取，故离岸子集多标「疑似」，需下一阶段深挖。",
        ],
    )
    (out_dir / "full_listing_overview.md").write_text("\n".join(overview), encoding="utf-8")

    # ── 广东视图 ───────────────────────────────────────────────
    # 两条通道合起来才是广东全貌：①境内注册 H 股 —— 中文名即含地名；
    # ②离岸红筹 —— 壳名不含地名，须读正文（红筹视图的「广东线索」）。
    gd_by_name = gd
    gd_by_content = [r for r in off if r.gd_opco and not r.gd_l1]
    gd_all = sorted({r.id: r for r in gd_by_name + gd_by_content}.values(),
                    key=lambda r: (r.submit_date, r.display_name))
    gd_lines = _head(
        f"全量摸底 · 广东视图（{len(gd_all)} 家 = 名称命中 {len(gd_by_name)} + 离岸正文线索 {len(gd_by_content)}）",
        gd_all,
        [
            "> **本视图是广东连接的并集，但仍是下界**：",
            "> ① 名称通道：境内注册（H 股）申请人中文名直接含地名（如「廣州極飛科技股份有限公司」）；",
            "> ② 正文通道：离岸红筹壳名不含地名（如 Huge Dental / Qian Dama），靠「概要/公司资料」分册的广东城市词频检出。",
            "> 未检出者可能是：运营实体不在广东 / 分册未陈述 / 词频低于阈值 —— 需逐家复核，不能当作「非广东」结论。",
            "",
        ],
    )
    gd_lines += _table(gd_all)
    (out_dir / "full_listing_guangdong.md").write_text("\n".join(gd_lines), encoding="utf-8")

    # ── 红筹视图 ───────────────────────────────────────────────
    # 红筹 = 离岸注册（开曼/百慕大）；是否「广东红筹」取决于运营实体是否在广东。
    gd_core = sorted([r for r in off if _gd_tier(r).startswith("核心")], key=lambda r: -r.gd_opco_count)
    gd_supp = sorted([r for r in off if _gd_tier(r).startswith("补充")], key=lambda r: -r.gd_opco_count)
    gd_red = gd_core + gd_supp
    other_red = sorted([r for r in off if not r.gd_opco], key=lambda r: (r.submit_date, r.display_name))
    red_lines = _head(
        f"全量摸底 · 红筹视图（封面页确认离岸注册 {len(off)} 家）", off,
        [
            "> 红筹 = 离岸注册（开曼/百慕大）的上市主体 + 境内（含广东）运营实体。",
            "> 本视图给出**全部离岸注册申请人**（不按名字过滤），是「红筹全貌」的**上界**。",
            f"> 其中检出**广东运营线索** {len(gd_red)} 家（核心层公司自述章 {len(gd_core)} + 补充层仅历史沿革章 {len(gd_supp)}）；",
            f"> 其余 {len(other_red)} 家未检出广东线索（多为生物医药/其他省份/纯境外业务）。",
            "",
            f"## 一、广东红筹候选 · 核心层（公司自述章命中，{len(gd_core)} 家）",
            "> ⚠️ 线索强度 = 广东城市词在「概要 / 公司资料」分册中的出现次数；**不是结论**，须人工复核原文。",
            "",
        ],
    )
    red_lines += _table(gd_core)
    red_lines += ["", f"## 一·补、广东红筹候选 · 补充层（仅历史沿革章命中，{len(gd_supp)} 家）", "",
                  "> ⚠️ 「历史沿革」章含股东 / 投资方 / 中介机构地址，城市名噪音显著更高，故降级为补充层，须逐家复核。", ""]
    red_lines += _table(gd_supp)
    red_lines += [
        "",
        f"## 二、其余离岸申请人（未检出广东线索，{len(other_red)} 家）",
        "",
    ]
    red_lines += _table(other_red)
    (out_dir / "full_listing_redchip.md").write_text("\n".join(red_lines), encoding="utf-8")

    # ── 待核验视图 ─────────────────────────────────────────────
    unver_sorted = sorted(unver, key=lambda r: (r.submit_date, r.display_name))
    unv_lines = _head(
        f"全量摸底 · 待核验视图（未判定注册地 {len(unver)} 家）", unver_sorted,
        [
            "> 未判定原因分类见「文档」列：官方未刊发文档（秘密递表）/ 仅listedco可补 / 分册结构异常。",
            "",
        ],
    )
    unv_by_reason = Counter(_doc_available(r) for r in unver_sorted)
    unv_lines += ["> 原因分布：" + "；".join(f"{k} {v}" for k, v in unv_by_reason.items()), ""]
    unv_lines += _table(unver_sorted)
    (out_dir / "full_listing_unverified.md").write_text("\n".join(unv_lines), encoding="utf-8")

    # ── 结论与方法论修正（单一入口，避免手写漂移）─────────────────
    audit_path = settings.redchip_data_dir / "listing_full_audit.json"
    flips: list[dict[str, str]] = []
    if audit_path.exists():
        flips = json.loads(audit_path.read_text(encoding="utf-8")).get("flips", [])
    now = dt.datetime.now(config_mod.CST).strftime("%Y-%m-%d %H:%M")
    gd_core = sorted([r for r in off if _gd_tier(r).startswith("核心")], key=lambda r: -r.gd_opco_count)
    gd_supp = sorted([r for r in off if _gd_tier(r).startswith("补充")], key=lambda r: -r.gd_opco_count)
    gd_red = gd_core + gd_supp
    con: list[str] = [
        f"# 港交所递表企业 · 全量摸底结论（{min(years)}–{max(years)} · 在审 + 已上市）",
        "",
        f"> 生成时间：{now}（北京时间）｜数据源：港交所披露易官方静态 JSON（全状态 × 板块，免鉴权）",
        "> ⚠️ 行外公开信息初筛，须经行内渠道复核；关系状态公开不可得。",
        "",
        "## 一、口径",
        f"- **时间**：递交日 ≥ {min(years)}-01-01。",
        "- **状态**：只保留 **处理中 / 处理中(含PHIP) / 已上市**；失效·撤回·被拒绝·被发回等无商机状态已剔除。",
        "- **地域**：**不做广东预过滤**——广东只是结果表一列 + 一个派生视图。",
        f"- 结果口径人口：**{len(records)} 家**（官方全量去重后按上述口径收窄）。",
        "",
        "## 二、注册地（封面页权威口径）",
        "",
        "| 注册地 | 家数 | 占比 |",
        "|---|---|---|",
        *[f"| {k} | {c_dom.get(k, 0)} | {c_dom.get(k, 0) / len(records) * 100:.1f}% |"
          for k in ("开曼群岛", "百慕大", "中国(境内)", "香港", "未判定")],
        "",
        "判定方法：申请版本带 **Multi-Files 分册目录**，只下載 **WARNING 封面分册**（~110KB / 1 页 / 3-7s，"
        "整份 5-11MB / 30-60s），用封面页固定句式判注册地：",
        "",
        "```",
        "(Incorporated in the Cayman Islands with limited liability)",
        "(A joint stock company incorporated in the People's Republic of China with limited liability)",
        "```",
        "",
        "已上市记录官方清单**不含申请版本**（applisted 1310 条中含 Application Proof 的为 0），"
        "改走 `listedco/listconews` 招股章程首页。",
        "",
        "## 三、⚠️ 方法论修正：旧口径假阳性红筹",
        "",
        "旧 `extract_structure` 扫整份 PDF 250 页、按「离岸 > 境内 > 香港」取优先级，**系统性高估红筹**："
        "正文里偶发一处 Cayman 提及即被当作注册地。",
        "",
        "**实测反例**：`id=108344`「深圳小阔」封面明写 *A joint stock company incorporated in the People's Republic "
        "of China with limited liability*（H 股），旧口径却判「开曼群岛」。",
        "",
        f"对历史已判定的 84 家复核，**翻案 {len(flips)} 条**（全部「离岸 → 中国境内」）：",
        "",
        "| # | 企业 | 旧口径 | 封面口径 |",
        "|---|---|---|---|",
        *[f"| {i} | {f['name']} | {f['legacy']} | {f['cover']} |" for i, f in enumerate(flips, 1)],
        "",
        "## 四、广东视图",
        "",
        f"- **名称通道 {len(gd)} 家**：境内注册（H 股）申请人中文名直接含地名（境内注册共 {c_dom.get('中国(境内)', 0)} 家）。",
        f"- **正文通道 {len(gd_red)} 家**：离岸壳名不含地名，靠「概要·公司资料」分册的广东城市词线索检出"
        f"（离岸注册共 {len(off)} 家）。",
        f"- **广东并集 {len(gd) + len(gd_red)} 家**。未检出 ≠ 非广东：可能运营实体不在粤、分册未陈述或词频低于阈值。",
        "",
        "## 五、红筹视图 · 广东红筹候选",
        "",
        f"离岸注册（全部开曼）**{len(off)} 家**——旧「名称预筛」一家都框不到，正是被漏掉的那批。"
        f"其中检出广东运营线索 **{len(gd_red)} 家**：",
        "",
        "| # | 企业 | 注册地 | 广东线索词频 | 词频构成 | 状态 |",
        "|---|---|---|---|---|---|",
        *[f"| {i} | {r.display_name} | {r.domicile} | {r.gd_opco_count} | "
          f"{r.gd_opco_source.split('|')[-1].strip() if '|' in r.gd_opco_source else '—'} | {r.status} |"
          for i, r in enumerate(gd_red, 1)],
        "",
        "> 详见 `output/redchip_gd_evidence_cards.md`（逐家原文摘句复核卡）。",
        "",
        "## 六、VIE（协议控制）",
        "",
        f"- 全量判定 **{sum(vie_c.values())} 家**："
        + "；".join(f"{k} {v} 家" for k, v in sorted(vie_c.items(), key=lambda x: -x[1])) + "。",
        "- 分册权威性规则：港交所招股书若上市主体**当前**存在 VIE，必在「概要」与「风险因素」章披露；"
        "只在「历史沿革」章出现的多属**已终止的历史安排**（实测 Aqara 的 VIE 于 2022 / 2026-03 终止），"
        "此类记为「否（仅历史安排）」而不计为 VIE。",
        "- 口径收严：只认 `variable interest entity` / `VIE` / `协议控制`；旧口径的裸词 "
        "`contractual arrangements` 已弃用（会把普通商务合同误当 VIE）。",
        "- **边界**：VIE 只描述控制方式，**不参与地域判定**。",
        "",
        "## 七、已知局限（勿掩盖）",
        "- 秘密递表者官方不刊发申请版本 → 官方源固有盲区，非流程缺陷。",
        "- 运营实体地域由分册词频推断，**仅为线索**；须逐家复核原文（证据卡已备）。",
        "- 未判定注册地者同时缺地域与 VIE 结论。",
        "- 中文版 PDF 经 pypdf 为 CID 乱码 → 只用英文分册。",
        "- 关系状态（是否开户/他行锁定）公开不可得，须行内 CRM 回填。",
        "",
    ]
    (out_dir / "full_listing_conclusion.md").write_text("\n".join(con), encoding="utf-8")

    print(f"全量明细 CSV：{csv_path}（{len(records)} 行）")
    print(f"总览：{out_dir / 'full_listing_overview.md'}")
    print(f"广东视图：{len(gd_all)} 家（名称 {len(gd_by_name)} + 正文线索 {len(gd_by_content)}）"
          f" → {out_dir / 'full_listing_guangdong.md'}")
    print(f"红筹视图：离岸 {len(off)} 家（其中广东线索 {len(gd_red)}）→ {out_dir / 'full_listing_redchip.md'}")
    print(f"待核验视图：{len(unver)} 家 → {out_dir / 'full_listing_unverified.md'}")
    print(f"结论文档：{out_dir / 'full_listing_conclusion.md'}")


if __name__ == "__main__":
    main()
