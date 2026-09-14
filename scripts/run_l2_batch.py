"""对 2026 广东 L1 候选批量跑 L2 结构抽取（红筹判定）。

- 双通道路由：已上市→listedco 快 CDN；其余→/app/ 慢库
- 增量持久化：每处理完一家立即写回 listing_state.json，崩溃可续跑
- PDF 缓存：已下载且有效则复用，不重复消耗带宽
- 进度日志：scripts/l2_batch.log

用法：
    PYTHONPATH=src python scripts/run_l2_batch.py
"""

from __future__ import annotations

import logging
import sys

from redchip import config as config_mod
from redchip.overseas import hkex_listing as L

YEAR = 2026

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("scripts/l2_batch.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("l2_batch")


def main() -> None:
    settings = config_mod.get_settings()
    pdf_dir = settings.redchip_data_dir / "listing_pdfs"
    csv_path = settings.redchip_data_dir / f"listing_{YEAR}.csv"
    md_path = settings.redchip_output_dir / f"gd_hk_ipo_{YEAR}_listing.md"

    recs = L.load_state()
    gd = [r for r in recs if r.gd_l1]
    # 仅处理尚未 L2 判定（或上次无链接）的，已判定跳过以续跑
    todo = [r for r in gd if not r.redchip_l2 or r.redchip_l2.startswith("待核验")]
    log.info("L1 广东候选 %d 家，待处理 %d 家（已判定 %d 家跳过）", len(gd), len(todo), len(gd) - len(todo))

    done = 0
    for r in todo:
        try:
            pdf = L.download_app_proof(r, pdf_dir, settings)
            if not pdf:
                r.redchip_l2 = "待核验(无申请版本链接)"
                log.warning("  [%d/%d] %s 无可用文档", done + 1, len(todo), r.display_name)
            else:
                info = L.extract_structure(pdf)
                r.domicile = info["domicile"]
                r.gd_opco = info["gd_opco"]
                r.is_vie = info["is_vie"]
                r.redchip_l2 = L.classify_redchip(r)
                log.info(
                    "  [%d/%d] %s ch=%s dom=%s gd=%d vie=%d -> %s",
                    done + 1, len(todo), r.display_name, r.doc_channel,
                    r.domicile or "?", 1 if r.gd_opco else 0, r.is_vie, r.redchip_l2,
                )
        except Exception as e:  # noqa: BLE001 - 单家异常不中断批处理
            r.redchip_l2 = "待核验(异常)"
            log.error("  [%d/%d] %s 异常: %s", done + 1, len(todo), r.display_name, e)
        done += 1
        # 增量持久化（崩溃续跑）
        L.persist(recs)
        if done % 10 == 0:
            log.info("进度 %d/%d，落盘", done, len(todo))

    # 末次全量落盘 + 产出
    L.persist(recs)
    # 复用 CLI 的写出逻辑
    from redchip.cli import _write_listing_outputs
    _write_listing_outputs(recs, csv_path, md_path, YEAR)
    verified = sum(1 for r in gd if r.redchip_l2 and not r.redchip_l2.startswith("待核验"))
    log.info("完成：L1 广东 %d 家，L2 已判定 %d 家，待核验 %d 家", len(gd), verified, len(gd) - verified)


if __name__ == "__main__":
    main()
