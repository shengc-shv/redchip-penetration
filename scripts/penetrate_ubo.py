#!/usr/bin/env python3
"""阶段四之四：UBO 穿透 + 置信度评分。

自下而上累乘持股比例，阈值默认 25%；置信度 < 60 标记需人工复核。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """执行 UBO 穿透与评分。"""
    args = parse_args("UBO 穿透与置信度评分")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        state = pipeline.stage_ubo(state, cfg, target.name)
        report = pipeline.load_report(target.code, cfg)
        if report is None:
            print(f"{target.code}：结果缺失")
            continue
        print(
            f"{target.code} {report.name}：UBO {len(report.ubos)} 位，"
            f"置信度 {report.confidence.total}"
            + (" ⚠️需复核" if report.needs_review else "")
        )
        for ubo in report.ubos:
            print(f"    - {ubo.name} {ubo.cumulative_share * 100:.2f}%")


if __name__ == "__main__":
    main()
