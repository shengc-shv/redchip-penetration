#!/usr/bin/env python3
"""阶段五：LLM-B 路径审查 + 中文分析报告。

LLM 不可用时生成规则兜底报告，保证交付物不为空。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """生成分析报告。"""
    args = parse_args("LLM-B 路径审查与报告生成")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        state = pipeline.stage_llm_b(state, cfg)
        report = pipeline.load_report(target.code, cfg)
        length = len(report.report_md) if report else 0
        print(f"{target.code} {target.name}：报告 {length} 字符 → output/{target.code}/report.md")


if __name__ == "__main__":
    main()
