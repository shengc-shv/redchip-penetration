"""全量 VIE（协议控制结构）判定 —— 2026 在审 + 已上市全量申请人。

口径
----
- 只认招股书**专有表述**（`variable interest entity` / `VIE` / `协议控制` / `VIE 架构`），
  不再用裸词 `contractual arrangements`（普通商务合同也用该词，旧口径把它当地 H 股
  公司也标成 VIE —— 实测深圳承泰/天赐高新/德赛西威等被误标）。
- 弱信号（contractual arrangements）须同时出现 WFOE / nominee / consolidate 才采信。
- 全量结果带**原文证据片段**（`vie_evidence`）与来源分册（`vie_source`），供人工复核。

取数路径
--------
- 有 Multi-Files 分册目录 → 只读小分册（SUMMARY / RISK FACTORS / HISTORY…，合计 ~1MB，~6s/家）；
- 无分册目录（多为已上市） → 回落 listedco 招股章程前 N 页。

用法::

    PYTHONPATH=src python scripts/scan_vie_full.py
    PYTHONPATH=src python scripts/scan_vie_full.py --workers 4 --no-listedco
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

LOG_PATH = Path("scripts/vie_full.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("vie_full")


def _scan_sections(rec: L.ListingRecord, settings: config_mod.Settings) -> None:
    time.sleep(__import__("random").uniform(0.0, 0.6))
    if rec.multi_url:
        res = C.scan_vie(rec, settings)
        rec.vie_terminated = bool(res.get("terminated")) or bool(res.get("history_only"))
    elif rec.app_proof_url:
        res = _scan_app_pdf(rec, settings)  # 无分册目录但有整份申请版本 → 前 N 页兜底
        if res is None:
            return
        rec.vie_terminated = bool(res.get("terminated"))
    else:
        return
    rec.is_vie = bool(res["is_vie"])
    rec.vie_evidence = res.get("evidence", "")
    rec.vie_source = res.get("source", "") or ("app_proof(整份前N页)" if not rec.multi_url else "")
    rec.vie_scanned = True


def _scan_app_pdf(rec: L.ListingRecord, settings: config_mod.Settings, max_pages: int = 60):
    """无分册目录但有整份申请版本 → 取前 N 页做 VIE 判定。

    **有界下载**：整体超时 100s、体积上限 12MB。披露易 `/app/` 慢库偶发长时间不返回
    （实测一条卡住 19 分钟仍未结束），无界下载会拖死整条批处理。
    """
    import httpx

    deadline = time.time() + 100
    max_bytes = 12_000_000
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=30.0), follow_redirects=True) as c:
            with c.stream("GET", rec.app_proof_url, headers={"User-Agent": settings.user_agent}) as r:
                if r.status_code != 200:
                    return None
                buf = bytearray()
                for chunk in r.iter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes or time.time() > deadline:
                        break
        if buf[:5] != b"%PDF-":
            return None
        return C.vie_from_text(C._pdf_text(bytes(buf), max_pages=max_pages))
    except Exception as e:  # noqa: BLE001
        log.warning("  app_proof %s 失败: %s", rec.display_name, e)
        return None


def _scan_listedco(rec: L.ListingRecord, settings: config_mod.Settings, tmp_dir: Path,
                   max_pages: int) -> None:
    """无分册目录（已上市）→ listedco 招股章程前 N 页做 VIE 判定；判完即删 PDF。"""
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
    except Exception as e:  # noqa: BLE001 - 单家异常不中断批处理
        log.warning("  listedco %s(%s) VIE 失败: %s", rec.display_name, rec.stock_code, e)
        return
    res = C.vie_from_text(text)
    rec.is_vie = bool(res["is_vie"])
    rec.vie_evidence = res.get("evidence", "")
    rec.vie_source = f"listedco:prospectus(前{max_pages}页)"
    rec.vie_terminated = bool(res.get("terminated"))
    rec.vie_scanned = True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-pages", type=int, default=80, help="listedco 招股章程读取页数")
    ap.add_argument("--listedco", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--redo", action="store_true", help="重跑已判定的（默认跳过）")
    ap.add_argument("--positive-only", action="store_true",
                    help="只复扫当前判定为「是 / 仅历史安排」的记录（口径修正后的定向复核，几分钟完成）")
    args = ap.parse_args()

    settings = config_mod.get_settings()
    full_state = settings.redchip_data_dir / "listing_full_state.json"
    records = L.load_state(full_state)
    if not records:
        raise SystemExit(f"缺少 {full_state}，请先跑 scripts/run_full_scan.py")

    sec_pool = [r for r in records if r.multi_url or r.app_proof_url]
    if args.positive_only:
        sec_pool = [r for r in sec_pool if r.is_vie or r.vie_terminated]
    todo = sec_pool if (args.redo or args.positive_only) else [r for r in sec_pool if not r.vie_scanned]
    log.info("%s全量 %d 家；分册待扫 %d",
             "【定向复扫阳性】" if args.positive_only else "", len(records), len(todo))

    t0 = time.time()
    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_scan_sections, r, settings): r for r in todo}
        for fut in as_completed(futures):
            rec = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("  %s VIE 扫描异常: %s", rec.display_name, e)
            with lock:
                done += 1
                if done % 20 == 0:
                    L.persist(records, full_state)
                    log.info("  进度 %d/%d（%.0fs，落盘）", done, len(todo), time.time() - t0)
    L.persist(records, full_state)

    listed = [r for r in records if not r.multi_url and r.stock_code
              and (args.redo or not r.vie_scanned)] if args.listedco else []
    if args.positive_only:
        listed = [r for r in records if not r.multi_url and r.stock_code
                  and (r.is_vie or r.vie_terminated)] if args.listedco else []
    # 既无分册目录也无股票代码（官方未刊发文档）→ 明确标为待核验，清掉可能继承的旧口径结论
    for r in records:
        if not r.multi_url and not r.stock_code and not r.vie_scanned:
            r.is_vie, r.vie_evidence, r.vie_source, r.vie_terminated = False, "", "", False
    if listed:
        log.info("补充：%d 家无分册目录（已上市）走 listedco 招股章程…", len(listed))
        tmp_dir = settings.redchip_data_dir / "_listedco_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda r: _scan_listedco(r, settings, tmp_dir, args.max_pages), listed))
    L.persist(records, full_state)

    vie = [r for r in records if r.is_vie]
    unknown = [r for r in records if not r.vie_source]
    hist = [r for r in records if r.vie_terminated and not r.is_vie]
    log.info("完成：全量 %d 家，VIE 判定为「是」**%d 家**（另有 %d 家仅为已终止的历史安排），未判定 %d 家，耗时 %.0fs",
             len(records), len(vie), len(hist), len(unknown), time.time() - t0)
    log.info("VIE 名单（按名称）：")
    for r in sorted(vie, key=lambda x: x.display_name):
        log.info("  ✔ %s（%s）来源=%s", r.display_name, r.domicile or "?", r.vie_source[:50])


if __name__ == "__main__":
    main()
