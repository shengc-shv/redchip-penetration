#!/usr/bin/env python3
"""阶段二、三：检索境内 WFOE 候选 + LLM-A 架构提取。

对应方案文档「LLM-A Structure + VIE + Disambiguation」步骤。
LLM 不可用时自动降级为规则兜底，不会中断流水线。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """检索候选并调用 LLM-A。"""
    args = parse_args("WFOE 候选检索 + LLM-A 架构提取")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        state = pipeline.stage_candidates(
            state, cfg, name=target.name, keywords=target.wfoe_keywords
        )
        state = pipeline.stage_llm_a(state, cfg, target.name)
        flag = "⚠️需复核" if state.llm_a.needs_review else "✅"
        print(
            f"{target.code} {target.name}：候选={len(state.candidates)} 条，"
            f"链条={state.llm_a.chain_summary[:40]}，置信度={state.llm_a.confidence} {flag}"
        )


if __name__ == "__main__":
    main()
