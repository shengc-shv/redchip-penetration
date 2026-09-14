"""为「广东红筹候选」生成**原文证据卡**（人工复核用）。

背景
----
`is_offshore` + `gd_opco_count` 只是**线索**（词频）。对外交付前必须能逐家翻到原文。
本脚本把每家的招股书「概要 / 公司资料 / 历史沿革」分册中**含广东地名且有实体/地址语境**
的句子抽出来，形成一张可复核的证据卡，并附上封面注册地原文句。

产出
----
``output/redchip_gd_evidence_cards.md``

用法::

    PYTHONPATH=src python scripts/make_evidence_cards.py
    PYTHONPATH=src python scripts/make_evidence_cards.py --min-hits 3 --max-snippets 8
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from redchip import config as config_mod
from redchip.overseas import hkex_cover as C
from redchip.overseas import hkex_listing as L

from make_full_listing import _gd_tier

LOG_PATH = Path("scripts/evidence_cards.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("evidence")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hits", type=int, default=3)
    ap.add_argument("--max-snippets", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    settings = config_mod.get_settings()
    records = L.load_state(settings.redchip_data_dir / "listing_full_state.json")
    if not records:
        raise SystemExit("缺少 data/listing_full_state.json，请先跑 scripts/run_full_scan.py")

    pool = sorted(
        [r for r in records if r.is_offshore and r.gd_opco_count >= args.min_hits and r.multi_url],
        key=lambda r: -r.gd_opco_count,
    )
    core = sum(1 for r in pool if _gd_tier(r) == "核心")
    log.info("待生成证据卡的广东红筹候选：%d 家（核心层 %d + 补充层 %d）", len(pool), core, len(pool) - core)

    t0 = time.time()
    results: dict[int, list[dict[str, str]]] = {}
    lock = threading.Lock()

    def work(rec: L.ListingRecord) -> None:
        time.sleep(__import__("random").uniform(0.0, 0.6))
        snips = C.gd_evidence_snippets(rec, settings, max_snippets=args.max_snippets)
        with lock:
            results[rec.id] = snips

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(work, r) for r in pool]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("  抽取异常: %s", e)
            if i % 5 == 0:
                log.info("  进度 %d/%d（%.0fs）", i, len(pool), time.time() - t0)

    now = dt.datetime.now(config_mod.CST).strftime("%Y-%m-%d %H:%M")
    out: list[str] = [
        "# 广东红筹候选 · 原文证据卡（人工复核用）",
        "",
        "> **用途**：逐家核对「上市主体离岸注册 + 境内运营实体在粤」是否成立。",
        "> **数据来源**：港交所披露易申请版本 Multi-Files 分册（概要 / 公司资料 / 历史沿革），英文版，pypdf 抽取。",
        "> ⚠️ 摘句为机器抽取，可能被断句；请以 PDF 原文为准。**本卡是复核材料，不是结论**。",
        f"> 生成时间：{now}（北京时间）｜候选 {len(pool)} 家。",
        "",
        "---",
        "",
    ]
    for i, r in enumerate(pool, 1):
        out += [
            f"## {i}. {r.display_name}",
            "",
            f"- **申请状态**：{r.status}｜**板块**：{r.board}｜**代码**：{r.stock_code or '—'}｜**递表日**：{r.submit_date}",
            f"- **封面上巿主体注册地**：`{r.domicile}`（分册：{r.cover_section or '—'}）",
            f"- **封面原文句**：{(r.cover_evidence or '—')[:220]}",
            f"- **线索分层**：{_gd_tier(r) or '—'}（核心 = 命中「概要/公司资料」；补充 = 仅「历史沿革」）",
            f"- **VIE（协议控制）**："
            f"{'待核验' if not r.vie_scanned else ('是' if r.is_vie else ('否（仅历史安排）' if r.vie_terminated else '否'))}"
            f"（依据：{r.vie_source or '—'}）",
            f"- **广东线索**：词频 {r.gd_opco_count}｜构成 {r.gd_opco_source.split('|')[-1].strip() if '|' in r.gd_opco_source else '—'}",
            "",
            "**原文摘句（含广东地名 + 实体/地址语境）**：",
            "",
        ]
        snips = results.get(r.id) or []
        if snips:
            for s in snips:
                out.append(f"- 〔{s['section'][:34]}〕{s['sentence']}")
        else:
            out.append("- ⚠️ 未抽到带实体语境的广东句（可能为泛泛提及或分册未陈述），**须人工翻 PDF**。")
        if r.vie_evidence:
            out += ["", f"**VIE 相关句子**（{'在用' if r.is_vie else '仅历史/已终止'}）：", "",
                    f"- {r.vie_evidence[:300]}"]
        out += [
            "",
            f"- 招股书入口：{r.app_proof_url or ('（多分册目录）' + r.multi_url)}",
            "",
            "---",
            "",
        ]

    path = settings.redchip_output_dir / "redchip_gd_evidence_cards.md"
    path.write_text("\n".join(out), encoding="utf-8")
    got = sum(1 for r in pool if results.get(r.id))
    log.info("完成：%d 家，其中 %d 家抽到实体级摘句，耗时 %.0fs → %s", len(pool), got, time.time() - t0, path)
    print(f"证据卡已写出：{path}")


if __name__ == "__main__":
    main()
