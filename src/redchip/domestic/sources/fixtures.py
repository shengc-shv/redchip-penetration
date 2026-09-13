"""离线样例数据源：无密钥环境下的降级实现。

读取 ``fixtures/cnbiz/sample.json``，保证流水线在没有任何 API Key 时也能端到端跑通，
用于算法回归与 CI。

⚠️ 该文件是**公开工商登记风格的示意数据**（见文件内 ``_note`` 字段），
持股比例仅用于验证穿透算法，**不得当作核验事实对外呈现**。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from redchip import config as config_mod
from redchip.domestic.sources.base import (
    CompanyBasic,
    IdentifierIndex,
    Shareholder,
    fuzzy,
    pick_field,
    to_float,
    to_kind,
)
from redchip.models.schema import CandidateCompany


@lru_cache(maxsize=1)
def load_fixtures() -> dict[str, Any]:
    """加载离线样例数据。

    Returns:
        dict[str, Any]: fixture 内容；文件不存在时返回空结构。
    """
    path = fixture_path()
    if not path.exists():
        return {"candidates": [], "companies": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_path() -> Path:
    """返回 fixture 文件路径。

    Returns:
        Path: ``fixtures/cnbiz/sample.json``。
    """
    return config_mod.FIXTURES_DIR / "cnbiz" / "sample.json"


class FixtureSource:
    """离线样例数据源。

    Args:
        settings: 全局配置（本实现只读 fixture，不使用其中的密钥）。
        index: 名称↔代码映射缓存。
    """

    name = "fixture"

    def __init__(self, settings: config_mod.Settings, index: IdentifierIndex) -> None:
        self.settings = settings
        self.index = index
        self._data = load_fixtures()

    def _companies(self) -> dict[str, dict[str, Any]]:
        """返回「信用代码 → 企业档案」映射（兼容列表写法）。

        Returns:
            dict[str, dict[str, Any]]: 企业档案。
        """
        raw = self._data.get("companies", {})
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        out: dict[str, dict[str, Any]] = {}
        for row in raw if isinstance(raw, list) else []:
            code = str(pick_field(row, "credit_code"))
            if code:
                out[code] = row
        return out

    # ------------------------------------------------------------------

    def search_company(self, name: str, limit: int = 5) -> list[CandidateCompany]:
        """按名称在样例库里模糊搜索。

        Args:
            name: 企业名称关键词。
            limit: 返回条数上限。

        Returns:
            list[CandidateCompany]: 候选列表。
        """
        out: list[CandidateCompany] = []
        for row in self._data.get("candidates", []):
            if not fuzzy(name, str(row.get("name", ""))):
                continue
            out.append(CandidateCompany.model_validate(row))
            if len(out) >= limit:
                break
        for candidate in out:
            self.index.remember(candidate.name, candidate.credit_code)
        return out

    def get_company_basic(self, identifier: str) -> CompanyBasic:
        """按信用代码或名称取样例企业档案。

        Args:
            identifier: 统一社会信用代码或企业名称。

        Returns:
            CompanyBasic: 基本信息；查不到时各字段为空。
        """
        row = self._lookup(identifier)
        if not row:
            return CompanyBasic()
        basic = CompanyBasic.model_validate(row)
        self.index.remember(basic.name, basic.credit_code)
        return basic

    def get_shareholders(self, identifier: str) -> list[Shareholder]:
        """按信用代码或名称取样例股东。

        Args:
            identifier: 统一社会信用代码或企业名称。

        Returns:
            list[Shareholder]: 股东列表。
        """
        row = self._lookup(identifier)
        return [Shareholder.model_validate(item) for item in row.get("shareholders", [])]

    # ------------------------------------------------------------------

    def _lookup(self, identifier: str) -> dict[str, Any]:
        """按代码或名称定位企业档案。

        Args:
            identifier: 统一社会信用代码或企业名称。

        Returns:
            dict[str, Any]: 企业档案；未命中返回空字典。
        """
        companies = self._companies()
        if identifier in companies:
            return companies[identifier]
        for code, row in companies.items():
            if str(pick_field(row, "name")) == identifier:
                return {**row, "credit_code": row.get("credit_code") or code}
        return {}


__all__ = ["FixtureSource", "fixture_path", "load_fixtures", "to_float", "to_kind"]
