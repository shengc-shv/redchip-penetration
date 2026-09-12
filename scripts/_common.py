"""脚本公共入口。

职责：
1. 让 ``python scripts/xxx.py`` 无需预先设置 PYTHONPATH 即可导入 ``redchip``；
2. 统一解析目标企业（``--codes`` / ``STOCK_CODES`` 环境变量 / ``config/targets.yaml``）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from redchip import config as config_mod
from redchip.pipeline.targets import Target, load_targets


def parse_args(description: str) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        description: 脚本说明。

    Returns:
        argparse.Namespace: 解析结果。
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--codes",
        default=os.environ.get("STOCK_CODES", ""),
        help="逗号分隔的股票代码，缺省读环境变量 STOCK_CODES",
    )
    parser.add_argument("--all", action="store_true", help="处理 config/targets.yaml 中的全部港股")
    return parser.parse_args()


def resolve_targets(args: argparse.Namespace | None = None) -> list[Target]:
    """确定待处理的企业列表。

    Args:
        args: 命令行参数；缺省时自动解析。

    Returns:
        list[Target]: 目标企业列表。
    """
    args = args or parse_args("resolve targets")
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
    if codes:
        return load_targets(codes=codes)
    if args.all:
        return [t for t in load_targets() if t.market.value == "hk"]
    return [t for t in load_targets() if t.market.value == "hk"]


def settings() -> config_mod.Settings:
    """载入并确保输出目录存在。

    Returns:
        Settings: 全局配置。
    """
    cfg = config_mod.get_settings()
    cfg.redchip_output_dir.mkdir(parents=True, exist_ok=True)
    cfg.redchip_data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
