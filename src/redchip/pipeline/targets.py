"""目标企业清单加载。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from redchip import config as config_mod
from redchip.models.schema import Market

TARGETS_PATH = config_mod.PROJECT_ROOT / "config" / "targets.yaml"


class Target(BaseModel):
    """一家待分析企业。"""

    model_config = ConfigDict(extra="ignore")

    code: str
    name: str
    market: Market = Market.HK
    city: str = ""
    wfoe_keywords: list[str] = Field(default_factory=list)


def load_targets(path: Path | None = None, codes: list[str] | None = None) -> list[Target]:
    """加载目标企业清单。

    Args:
        path: 清单文件路径；缺省用 ``config/targets.yaml``。
        codes: 只返回这些代码对应的企业；为空返回全部。

    Returns:
        list[Target]: 目标列表。
    """
    file = path or TARGETS_PATH
    if not file.exists():
        return []
    data: dict[str, Any] = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    targets = [Target.model_validate(t) for t in data.get("targets", [])]
    if codes:
        wanted = {c.strip().upper().zfill(5) for c in codes}
        targets = [t for t in targets if t.code.upper().zfill(5) in wanted]
    return targets
