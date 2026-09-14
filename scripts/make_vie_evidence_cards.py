"""生成「VIE 判定 · 原文证据卡」（逐条可复核）。

为什么需要
----------
`is_vie` 是**线索级结论**，必须能逐条回到招股书原文。本脚本把结果表里的
`vie_evidence`（原文片段）、`vie_source`（依据分册）、`hits`（信号强度）与招股书入口
汇编成一张可点击复核的卡片表，并单列「待核验」与复核方法。

产出
----
``output/vie_evidence_cards.md``

用法::

    PYTHONPATH=src python scripts/make_vie_evidence_cards.py
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from redchip import config as config_mod
from redchip.overseas import hkex_listing as L


def _verdict(r: L.ListingRecord) -> str:
    if not r.vie_scanned:
        return "待核验"
    if r.is_vie:
        return "**是**"
    return "否（仅历史安排）" if r.vie_terminated else "否"


def _entry_url(r: L.ListingRecord) -> str:
    if r.app_proof_url:
        return r.app_proof_url
    if r.multi_url:
        return "https://www1.hkexnews.hk/app/" + r.multi_url.lstrip("/")
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="vie_evidence_cards.md")
    args = ap.parse_args()

    settings = config_mod.get_settings()
    records = L.load_state(settings.redchip_data_dir / "listing_full_state.json")
    if not records:
        raise SystemExit("缺少 data/listing_full_state.json，请先跑 scripts/run_full_scan.py")

    yes = sorted([r for r in records if r.is_vie], key=lambda r: r.display_name)
    hist = sorted([r for r in records if r.vie_terminated and not r.is_vie], key=lambda r: r.display_name)
    pend = sorted([r for r in records if not r.vie_scanned], key=lambda r: r.display_name)
    no = [r for r in records if r.vie_scanned and not r.is_vie and not r.vie_terminated]
    now = dt.datetime.now(config_mod.CST).strftime("%Y-%m-%d %H:%M")

    out: list[str] = [
        "# VIE（协议控制）判定 · 原文证据卡",
        "",
        "> **用途**：逐条复核 `is_vie` 结论——每条给出**原文证据片段**、**依据分册**与**招股书入口**，可直接点回原文核对。",
        "> **判据**：强信号 = `variable interest entity` / **大写** `VIE`/`VIEs` / `协议控制`（大小写敏感，"
        "避免命中英文动词 vie 与断词残留）；弱信号 = `contractual arrangements` 与 `WFOE`/`VIE`/`nominee`/`consolidat` 同现。",
        "> **分册权威性**：港交所招股书若上市主体**当前**存在 VIE，必在「概要」与「风险因素」章披露；"
        "只在「历史沿革」章出现的多属**已终止的历史安排**，记为「否（仅历史安排）」。",
        f"> 生成时间：{now}（北京时间）。",
        "",
        "## 一、复核方法",
        "1. 点开「招股书入口」，进入披露易 Multi-Files 页面；",
        "2. 打开 `SUMMARY`（概要）与 `RISK FACTORS`（风险因素）分册，搜索关键词 `VIE`、`Contractual Arrangements`；",
        "3. 若两章均有 VIE 结构描述 → 结论「是」成立；若仅 `HISTORY` 章出现且含 _terminated / Historical_ 字样 → 「仅历史安排」成立；",
        "4. 证据片段为 pypdf 抽取文本，偶有断词（如 `vie w`、`Vie tnam`），请以 PDF 为准。",
        "",
        "## 二、判定汇总",
        "",
        f"| 结论 | 家数 |",
        "|---|---|",
        f"| 是（当前存在 VIE） | {len(yes)} |",
        f"| 否（仅历史安排，已终止） | {len(hist)} |",
        f"| 否（未检出 VIE 表述） | {len(no)} |",
        f"| 待核验（未取得可判文本） | {len(pend)} |",
        "",
        "---",
        "",
        f"## 三、判定为「是」的 {len(yes)} 家",
        "",
    ]
    if not yes:
        out.append("- 无。")
    for i, r in enumerate(yes, 1):
        out += [
            f"### {i}. {r.display_name}",
            "",
            f"- **结论**：{_verdict(r)}｜**依据分册**：{r.vie_source or '—'}",
            f"- **注册地(封面)**：{r.domicile or '待核验'}｜**状态**：{r.status}｜**代码**：{r.stock_code or '—'}"
            f"｜**递表日**：{r.submit_date}",
            f"- **原文证据**：",
            "",
            f"  > {r.vie_evidence or '⚠️ 未留存片段，需人工翻 PDF'}",
            "",
            f"- 招股书入口：{_entry_url(r) or '—'}",
            "",
        ]

    out += ["---", "", f"## 四、判定为「否（仅历史安排）」的 {len(hist)} 家", "",
            "> 这些企业**曾**搭建 VIE（或相关协议控制安排），招股书披露为已终止/历史安排，**当前结构不计为 VIE**。", ""]
    for i, r in enumerate(hist, 1):
        out += [
            f"### {i}. {r.display_name}",
            "",
            f"- **结论**：{_verdict(r)}｜**依据分册**：{r.vie_source or '—'}",
            f"- **原文证据**：",
            "",
            f"  > {r.vie_evidence or '⚠️ 未留存片段，需人工翻 PDF'}",
            "",
            f"- 招股书入口：{_entry_url(r) or '—'}",
            "",
        ]

    out += ["---", "", f"## 五、待核验 {len(pend)} 家（未取得可判文本）", "",
            "| # | 企业 | 代码 | 状态 | 原因 |", "|---|---|---|---|---|"]
    for i, r in enumerate(pend, 1):
        reason = ("已上市·listedco 招股章程下载超时" if r.stock_code
                  else ("仅有整份申请版本且下载超限" if r.app_proof_url else "官方未刊发文档（秘密递表）"))
        out.append(f"| {i} | {r.display_name} | {r.stock_code or '—'} | {r.status} | {reason} |")
    out += ["", "> 补齐方式：`PYTHONPATH=src python scripts/scan_vie_full.py --workers 3`（只补未扫项）。", ""]

    path = settings.redchip_output_dir / args.output
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"VIE 证据卡已写出：{path}")
    print(f"  是 {len(yes)}｜仅历史 {len(hist)}｜否 {len(no)}｜待核验 {len(pend)}")


if __name__ == "__main__":
    main()
