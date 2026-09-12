#!/usr/bin/env python3
"""阶段一：抓取 HKEXnews 披露文件并建立 FTS 索引。

对应方案文档「Overseas - HKEXnews (HK)」步骤。
"""

from __future__ import annotations

from _common import parse_args, resolve_targets, settings

from redchip.pipeline import run as pipeline


def main() -> None:
    """批量抓取并建索引。"""
    args = parse_args("抓取港股披露文件并建立 FTS 索引")
    cfg = settings()
    for target in resolve_targets(args):
        state = pipeline.load_state(target.code, cfg)
        state = pipeline.stage_fetch(state, cfg)
        print(
            f"{target.code} {target.name}：文档={state.doc_kind or '未获取'} "
            f"发布={state.doc_published_at or '未知'} FTS命中={len(state.fts_hits)} 段"
        )
        for err in state.errors:
            print(f"  ⚠️ {err}")


if __name__ == "__main__":
    main()
