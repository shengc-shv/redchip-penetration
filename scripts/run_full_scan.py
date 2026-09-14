"""全量摸底：对港交所官方清单**全量**申请人打派生标签（is_offshore / gd_flag / 文档可得性）。

方法论（用户 2026-09-13 17:11 拍板纠正）
----------------------------------------
❌ 旧路径：官方全量 → 按广东名字过滤(L1) → 只对命中者做 L2。
   会把「开曼壳名 + 广东运营实体」的正典红筹永久挡在门外。
✅ 本脚本：官方全量(**不按广东过滤**) → 对每条打派生标签；
   广东只作结果表一列/一个视图，不是闸门。

注册地取数（封面页权威口径，见 ``redchip.overseas.hkex_cover``）
----------------------------------------------------------------
申请版本几乎都带 Multi-Files 分册目录（``multi_url``）→ 只下載 WARNING 封面分册
（~110KB / 1 页 / ~3s，整份 5-11MB / 30-60s），用封面页固定句式判注册地：

    (Incorporated in the Cayman Islands with limited liability)
    (A joint stock company incorporated in the People's Republic of China with limited liability)

⚠️ 不用「扫整份 PDF 按离岸优先取首命中」——实测会把正文里偶发的 Cayman 提及
当成注册地（id=108344 深圳小阔科技封面明写 PRC，旧口径误判开曼）。

产出
----
- ``data/listing_full_state.json``：全量富集状态（增量落盘，崩溃可续跑）
- ``data/listing_full_audit.json``：新旧注册地口径对照（污染审计）
- ``scripts/full_scan.log``：进度日志

用法::

    PYTHONPATH=src python scripts/run_full_scan.py                  # 全量（2013-2026）
    PYTHONPATH=src python scripts/run_full_scan.py --min-year 2025  # 只看近两年
    PYTHONPATH=src python scripts/run_full_scan.py --only-known     # 只复核历史已判定的
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import sys
from collections import Counter
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from redchip import config as config_mod
from redchip.overseas import hkex_cover as C
from redchip.overseas import hkex_listing as L

LOG_PATH = Path("scripts/full_scan.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("full_scan")


def _scan_one(rec: L.ListingRecord, settings: config_mod.Settings) -> None:
    """封面页权威注册地扫描（分册优先，回落整份首页）。"""
    time.sleep(random.uniform(0.0, 0.6))  # 抖动，避免对披露易形成同步突发
    L.apply_scan(rec, C.scan_cover(rec, settings))


def _scan_listedco(rec: L.ListingRecord, settings: config_mod.Settings, tmp_dir) -> bool:
    """已上市但无申请版本 → 经 listedco 快通道取招股章程，读封面判注册地。

    「已上市」记录在官方清单里**不含**申请版本条目（applisted_sehk 1310 条中含
    Application Proof 的为 0），故只能用 ``listedco/listconews`` 取招股章程。
    招股章程首页即封面（含注册地句式）；年报封面不含，故优先招股章程。
    抽完即删 PDF，避免磁盘堆积。

    Returns:
        bool: 是否成功判定注册地。
    """
    from redchip.overseas.hkex import HkexClient, extract_pdf_pages

    if not rec.stock_code:
        return False
    try:
        with HkexClient(settings) as client:
            stock_id = client.resolve_stock_id(rec.stock_code)
            if not stock_id:
                return False
            filings = client.search_filings(stock_id, years_back=5, lang="EN")
            for kind in ("prospectus", "annual_report"):
                filing = client.pick_filing(filings, kind)
                if filing is None:
                    continue
                rec.doc_channel = "listedco"
                path = client.download(filing, tmp_dir)
                if not path or not path.exists():
                    continue
                try:
                    pages = extract_pdf_pages(path, max_pages=4)
                    dom, ev = C.cover_domicile_from_text("\n".join(p.text for p in pages))
                finally:
                    path.unlink(missing_ok=True)  # 只要封面结论，不留大文件
                if dom:
                    rec.domicile = dom
                    rec.scan_method = "cover"
                    rec.scan_confidence = "strong"
                    rec.is_offshore = dom in ("开曼群岛", "百慕大")
                    rec.cover_section = f"listedco:{kind}"
                    rec.cover_evidence = ev
                    return True
    except Exception as e:  # noqa: BLE001 - 单家异常不中断批处理
        log.warning("  listedco %s(%s) 失败: %s", rec.display_name, rec.stock_code, e)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-year", type=int, default=2026, help="递交日年份下界（默认 2026，即「2026 年以来」）")
    ap.add_argument("--status", default="active,listed",
                    help="保留的申请状态（逗号分隔：active/listed/inactive/returned）。"
                         "默认 active,listed —— 剔除失效/撤回/被拒/发回等无商机状态")
    ap.add_argument("--workers", type=int, default=4, help="并发度（默认 4，避免被披露易限速）")
    ap.add_argument("--only-known", action="store_true", help="只复核历史已判定过 domicile 的记录")
    ap.add_argument("--listedco", action=argparse.BooleanOptionalAction, default=True,
                    help="已上市无申请版本者走 listedco 招股章程首页（默认开）")
    args = ap.parse_args()
    keep_status = {t.strip() for t in args.status.split(",") if t.strip()}

    settings = config_mod.get_settings()
    full_state = settings.redchip_data_dir / "listing_full_state.json"
    audit_path = settings.redchip_data_dir / "listing_full_audit.json"

    log.info("拉取港交所官方清单（全状态 × 板块，免鉴权）…")
    raw_records = L.fetch_index(settings)
    log.info("官方全量去重：%d 条", len(raw_records))

    # 断点续跑：先回填本流程自己的产物（封面口径结论可安全复用）
    if full_state.exists():
        prev = L.load_state(full_state)
        L.merge_records(raw_records, prev, carry_domicile=True)
        log.info("断点续跑：回填本流程产物 %d 条（已判定 %d 条）",
                 len(prev), sum(1 for r in raw_records if r.scan_method == "cover"))
    # 历史富集（旧 L2）：只回填 gd_opco/is_vie/stock_code，**不回填被污染的 domicile**
    legacy_path = L.state_path(settings)
    legacy: list[L.ListingRecord] = []
    if legacy_path.exists():
        legacy = L.load_state(legacy_path)
        L.merge_records(raw_records, legacy, carry_domicile=False)
        log.info("回填历史富集（gd_opco/is_vie/stock_code）%d 条记录来源", len(legacy))

    # ── 口径收窄：年份 + 申请状态（用户 2026-09-13 17:5x 拍板）───────────
    records = [
        r for r in raw_records
        if r.submit_date
        and int(r.submit_date[:4]) >= args.min_year
        and r.status_raw in keep_status
    ]
    dropped_status = Counter(
        r.status for r in raw_records
        if r.submit_date and int(r.submit_date[:4]) >= args.min_year and r.status_raw not in keep_status
    )
    log.info(
        "口径：递交日 ≥ %d 且状态 ∈ %s → %d 条（剔除无商机状态 %d 条：%s）",
        args.min_year, sorted(keep_status), len(records), sum(dropped_status.values()), dict(dropped_status),
    )
    L.mark_guangdong(records)

    legacy_dom = {r.id: r.domicile for r in legacy if r.domicile}
    has_doc = [r for r in records if r.app_proof_url or r.multi_url]
    listed_only = [r for r in records if not (r.app_proof_url or r.multi_url) and r.stock_code]
    no_doc = [r for r in records if not (r.app_proof_url or r.multi_url) and not r.stock_code]
    todo = [r for r in has_doc if r.scan_method != "cover"]
    if args.only_known:
        todo = [r for r in todo if r.id in legacy_dom]
    for r in listed_only:
        if not r.domicile:
            r.redchip_l2 = "待核验(仅listedco可补)"
    for r in no_doc:
        if not r.domicile:
            r.redchip_l2 = "待核验(官方未刊发文档)"
    log.info(
        "文档可得性：有申请版本 %d / 仅已上市代码可补 %d / 无文档 %d；待封面扫描 %d",
        len(has_doc), len(listed_only), len(no_doc), len(todo),
    )

    t0 = time.time()
    done = errors = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_scan_one, r, settings): r for r in todo}
        for fut in as_completed(futures):
            rec = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 - 单家异常不中断批处理
                errors += 1
                log.warning("  %s 扫描异常: %s", rec.display_name, e)
            with lock:
                done += 1
                if done % 20 == 0:
                    L.persist(records, full_state)
                    log.info("  进度 %d/%d（%.0fs，落盘）", done, len(todo), time.time() - t0)
    L.persist(records, full_state)

    # ── 阶段二：已上市无申请版本 → listedco 招股章程封面 ───────────────
    listed_todo = [r for r in listed_only if not r.domicile] if args.listedco else []
    if listed_todo:
        log.info("阶段二：%d 家已上市走 listedco 取招股章程首页判注册地…", len(listed_todo))
        tmp_dir = settings.redchip_data_dir / "_listedco_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ok = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_scan_listedco, r, settings, tmp_dir): r for r in listed_todo}
            for fut in as_completed(futures):
                rec = futures[fut]
                try:
                    ok += 1 if fut.result() else 0
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.warning("  %s listedco 异常: %s", rec.display_name, e)
                with lock:
                    done += 1
                    if done % 20 == 0:
                        L.persist(records, full_state)
                        log.info("  阶段二进度 %d/%d（%.0fs，落盘）", done, len(todo) + len(listed_todo), time.time() - t0)
        L.persist(records, full_state)
        log.info("阶段二完成：%d / %d 家判定成功", ok, len(listed_todo))

    # 用权威注册地重算红筹判定
    for r in records:
        if r.domicile:
            r.redchip_l2 = L.classify_redchip(r)
    L.persist(records, full_state)

    # 新旧口径污染审计
    flips = []
    for r in records:
        old = legacy_dom.get(r.id)
        if old and r.domicile and old != r.domicile:
            flips.append({"id": r.id, "name": r.display_name, "legacy": old, "cover": r.domicile,
                          "section": r.cover_section, "evidence": r.cover_evidence})
    audit = {
        "generated_at": dt.datetime.now(config_mod.CST).isoformat(timespec="seconds"),
        "legacy_judged": len(legacy_dom),
        "rechecked": sum(1 for i in legacy_dom if any(r.id == i and r.scan_method == "cover" for r in records)),
        "flips": flips,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    off = [r for r in records if r.is_offshore]
    prc = [r for r in records if r.domicile == "中国(境内)"]
    hk = [r for r in records if r.domicile == "香港"]
    unk = [r for r in records if not r.domicile]
    log.info(
        "完成：%d 条，耗时 %.0fs，异常 %d\n"
        "  离岸(开曼/百慕大) %d；中国境内 %d；香港 %d；未判定 %d\n"
        "  其中广东名称命中 %d 家（仅作一列）\n"
        "  旧口径 vs 封面口径 翻案 %d 条（详见 %s）\n"
        "  状态：%s",
        len(records), time.time() - t0, errors,
        len(off), len(prc), len(hk), len(unk),
        sum(1 for r in records if r.gd_l1),
        len(flips), audit_path,
        full_state,
    )
    if flips:
        log.info("  翻案样例（旧→新）：")
        for f in flips[:15]:
            log.info("    %s %s：%s → %s", f["id"], f["name"][:32], f["legacy"], f["cover"])


if __name__ == "__main__":
    main()
