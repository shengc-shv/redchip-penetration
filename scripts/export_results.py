#!/usr/bin/env python3
"""阶段六：渲染穿透路径图并写出汇总索引。

有 graphviz（``dot``）时产出 PNG，否则退化为 SVG + DOT + Mermaid，流水线不中断。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """渲染图文件并生成汇总。"""
    args = parse_args("导出穿透路径图与汇总")
    cfg = settings()
    targets = resolve_targets(args)
    for target in targets:
        state = pipeline.load_state(target.code, cfg)
        produced = pipeline.stage_export(state, cfg)
        print(f"{target.code} {target.name}：{', '.join(sorted(produced))}")

    reports = [r for t in targets if (r := pipeline.load_report(t.code, cfg)) is not None]
    if reports:
        path = pipeline._write_summary(reports, cfg)
        print(f"汇总：{path}")


if __name__ == "__main__":
    main()
