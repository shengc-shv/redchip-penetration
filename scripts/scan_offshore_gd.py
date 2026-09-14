"""离岸子集 → 广东运营实体线索扫描（把「红筹」落到「广东红筹」）。

为什么只扫离岸子集
------------------
境内注册（H 股）申请人的省份已由中文名直接给出（如「廣州極飛科技股份有限公司」），
名称即可落广东；而**离岸壳名**（Huge Dental / Radvance Cayman / Qian Dama ...）不含地名，
必须读招股书正文才知道运营实体在哪 —— 这正是旧「名称预筛」漏掉的那批。

做法（廉价、可溯源）
--------------------
只取 Multi-Files 里的**小分册**（SUMMARY / CORPORATE INFORMATION），统计广东城市词频并留存原文片段。
⚠️ 定位：这是**线索**不是结论（单个城市名可能只出现在风险因素里），故结果带词频与来源分册，
须人工复核后才可对外。默认词频 ≥ ``--min-hits`` 才写 ``gd_opco``，其余保留计数供评估。

用法::

    PYTHONPATH=src python scripts/scan_offshore_gd.py                 # 默认 min-hits=3
    PYTHONPATH=src python scripts/scan_offshore_gd.py --min-hits 2
    PYTHONPATH=src python scripts/scan_offshore_gd.py --field-gd-only  # 未命中广东的也打印，便于调阈值
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from redchip import config as config_mod
from redchip.overseas import hkex_cover as C
from redchip.overseas import hkex_listing as L

LOG_PATH = Path("scripts/offshore_gd.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("offshore_gd")

MIN_HITS_DEFAULT = 3


def _scan_one(rec: L.ListingRecord, settings: config_mod.Settings, min_hits: int) -> None:
    res = C.scan_operating_region(rec, settings)
    rec.gd_opco_count = int(res.get("total", 0))
    rec.gd_opco_entity_count = int(res.get("entity_total", 0))
    hits = res.get("gd_hits") or {}
    top = ", ".join(f"{k}×{v}" for k, v in sorted(hits.items(), key=lambda x: -x[1])[:4])
    section = res.get("section", "")
    rec.gd_opco_source = f"{section} | {top}".strip(" |")
    # 线索强度达标才写 gd_opco（flag 语义），避免 classify_redchip 把弱信号当结论
    if rec.gd_opco_count >= min_hits:
        rec.gd_opco = (res.get("snippets") or [""])[0]
        rec.redchip_l2 = "是(线索待核)"


def _scan_listedco_gd(rec: L.ListingRecord, settings: config_mod.Settings, tmp_dir: Path,
                      min_hits: int, max_pages: int = 80) -> None:
    """已上市无分册目录时：经 listedco 取招股章程前若干页，统计广东城市词。

    与分册法口径不同（整份前 N 页 vs 小分册），故 ``gd_opco_source`` 标注来源为 listedco。
    """
    from redchip.overseas.hkex import HkexClient, extract_pdf_pages

    if not rec.stock_code:
        return
    try:
        with HkexClient(settings) as client:
            sid = client.resolve_stock_id(rec.stock_code)
            if not sid:
                return
            filings = client.search_filings(sid, years_back=5, lang="EN")
            filing = client.pick_filing(filings, "prospectus")
            if filing is None:
                return
            path = client.download(filing, tmp_dir)
            if not path or not path.exists():
                return
            try:
                text = "\n".join(p.text for p in extract_pdf_pages(path, max_pages=max_pages))
            finally:
                path.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 - 单家异常不中断
        log.warning("  listedco %s(%s) 失败: %s", rec.display_name, rec.stock_code, e)
        return
    low = text.lower()
    hits = {t: low.count(t) for t in C._GD_TOKENS if low.count(t)}
    rec.gd_opco_count = sum(hits.values())
    rec.gd_opco_entity_count = C._entity_context_hits(text)
    top = ", ".join(f"{k}×{v}" for k, v in sorted(hits.items(), key=lambda x: -x[1])[:4])
    rec.gd_opco_source = f"listedco:prospectus(前{max_pages}页) | {top}"
    if rec.gd_opco_count >= min_hits:
        i = min((low.find(t) for t in hits if low.find(t) >= 0), default=0)
        rec.gd_opco = text[max(0, i - 90): i + 110].replace("\n", " ").strip()
        rec.redchip_l2 = "是(线索待核)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hits", type=int, default=MIN_HITS_DEFAULT,
                    help="广东城市词频阈值（默认 3；低于阈值仅保留计数，不写 gd_opco）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--show-unmatched", action="store_true", help="同时打印未达阈值的离岸申请人（便于调阈值）")
    ap.add_argument("--redo", action="store_true",
                    help="重算已扫描的（分册匹配口径修正后需要，如 HISTORY 分册名变体）")
    ap.add_argument("--listedco", action=argparse.BooleanOptionalAction, default=True,
                    help="无分册目录的离岸申请人走 listedco 招股章程（默认开）")
    args = ap.parse_args()

    settings = config_mod.get_settings()
    full_state = settings.redchip_data_dir / "listing_full_state.json"
    records = L.load_state(full_state)
    if not records:
        raise SystemExit(f"缺少 {full_state}，请先跑 scripts/run_full_scan.py")

    # 只扫离岸子集（境内 H 股省份由中文名给出，无需正文）
    pool = [r for r in records if r.is_offshore and r.multi_url]
    if args.redo:
        for r in pool:
            r.gd_opco, r.gd_opco_count, r.gd_opco_entity_count, r.gd_opco_source = "", 0, 0, ""
    todo = [r for r in pool if not r.gd_opco or args.redo]
    log.info("离岸子集 %d 家（有分册目录 %d），待扫描 %d 家（阈值 %d）",
             sum(1 for r in records if r.is_offshore), len(pool), len(todo), args.min_hits)

    t0 = time.time()
    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_scan_one, r, settings, args.min_hits): r for r in todo}
        for fut in as_completed(futures):
            rec = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 - 单家异常不中断
                log.warning("  %s 运营地域扫描异常: %s", rec.display_name, e)
            with lock:
                done += 1
                if done % 10 == 0:
                    L.persist(records, full_state)
                    log.info("  进度 %d/%d（%.0fs，落盘）", done, len(todo), time.time() - t0)

    # 无分册目录的离岸申请人（多为已上市）→ listedco 招股章程
    listed = [r for r in records if r.is_offshore and not r.multi_url and r.stock_code
              and (not r.gd_opco or args.redo)] if args.listedco else []
    if listed:
        log.info("补充：%d 家无分册目录的离岸申请人走 listedco…", len(listed))
        tmp_dir = settings.redchip_data_dir / "_listedco_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda r: _scan_listedco_gd(r, settings, tmp_dir, args.min_hits), listed))
    L.persist(records, full_state)

    off = [r for r in records if r.is_offshore]
    hit = sorted([r for r in off if r.gd_opco_count >= args.min_hits], key=lambda r: -r.gd_opco_count)
    log.info("完成：离岸 %d 家，广东线索命中 %d 家（阈值 %d），耗时 %.0fs",
             len(off), len(hit), args.min_hits, time.time() - t0)
    for r in hit:
        log.info("  ✔ %s（%s）词频=%d 来源=%s", r.display_name, r.domicile, r.gd_opco_count, r.gd_opco_source[:70])
    others = sorted([r for r in off if r.gd_opco_count < args.min_hits], key=lambda r: -r.gd_opco_count)
    if args.show_unmatched:
        log.info("  未达标/无命中（词频降序）：")
        for r in others:
            log.info("    · %s 词频=%d", r.display_name, r.gd_opco_count)


if __name__ == "__main__":
    main()
