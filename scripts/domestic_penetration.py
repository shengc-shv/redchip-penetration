#!/usr/bin/env python3
"""阶段四之二：CNBizAPI 递归穿透境内股东。

``get_shareholders`` 每次消耗 1 积分，免费额度 200 次/月，
因此只对通过广东过滤的实体做穿透，并限制最大层数。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """递归采集境内股东并写回图谱。"""
    args = parse_args("境内股东递归穿透")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        before = len(state.ubo_start_ids)
        state = pipeline.stage_penetrate(state, cfg)
        print(f"{target.code} {target.name}：穿透 {len(state.gd_credit_codes)} 家境内实体（起点 {before} 个）")


if __name__ == "__main__":
    main()
