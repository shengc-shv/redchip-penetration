#!/usr/bin/env python3
"""阶段四之一：按 LLM-A 结果建图，并执行广东省地域过滤。

过滤器刻意放在 LLM-A 之后：先用全量候选完成 WFOE 消歧，再过滤，
否则可能提前剔除正确实体。零额外 token 消耗。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """建图并执行广东过滤。"""
    args = parse_args("建图 + 广东省过滤")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        state = pipeline.stage_filter(state, cfg, target.name)
        print(
            f"{target.code} {target.name}：广东省内实体 {len(state.gd_credit_codes)} 家"
            + (f" → {state.gd_credit_codes}" if state.gd_credit_codes else "（未命中，跳过境内穿透）")
        )


if __name__ == "__main__":
    main()
