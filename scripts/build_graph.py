#!/usr/bin/env python3
"""阶段四之三：把股权图谱写入 Neo4j。

未配置 ``NEO4J_URI`` 或缺少 neo4j 驱动时静默跳过，不影响 JSON 产物。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """同步图谱到图数据库。"""
    args = parse_args("写入 Neo4j 图数据库")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        written = pipeline.stage_graph_sync(state, cfg)
        print(f"{target.code} {target.name}：写入关系 {written} 条")


if __name__ == "__main__":
    main()
