"""境内工商数据源客户端（CNBizAPI）。

计费说明
--------
- ``search_company`` / ``get_company_basic``：**免费**
- ``get_shareholders``：1 积分/次（穿透时的主要消耗，免费额度 200 次/月）

离线联调
--------
未配置 ``CNBizAPI_KEY`` 或 ``REDCHIP_MOCK=true`` 时，全部读取 ``fixtures/cnbiz/*.json``，
保证流水线可在无密钥环境下端到端跑通。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from redchip import config as config_mod
from redchip.models.schema import CandidateCompany


class Shareholder(BaseModel):
    """一条股东记录。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    kind: str = Field(default="company", description="company | person")
    credit_code: str = ""
    share_pct: float = Field(default=0.0, description="持股比例（百分数，如 60.5）")
    share_raw: str = ""
    subscribe_amount: str = ""


class CompanyBasic(BaseModel):
    """企业基本工商信息的裁剪视图。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    credit_code: str = ""
    province: str = ""
    city: str = ""
    reg_status: str = ""
    established_at: str = ""
    business_scope: str = ""
    legal_person: str = ""


class CnbizUnavailableError(RuntimeError):
    """工商数据源不可用。"""


class CnbizClient:
    """CNBizAPI 客户端（带 fixture 降级）。

    Args:
        settings: 全局配置；缺省自动载入。
    """

    def __init__(self, settings: config_mod.Settings | None = None) -> None:
        self.settings = settings or config_mod.get_settings()
        self.mock = self.settings.redchip_mock or not self.settings.cnbizapi_key
        self._http: httpx.Client | None = None
        self._fixtures = _load_fixtures()

    # ------------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        """是否走真实 API。"""
        return not self.mock

    def _client(self) -> httpx.Client:
        """懒加载 HTTP 客户端。"""
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.settings.cnbizapi_base_url.rstrip("/"),
                timeout=self.settings.http_timeout,
                headers={
                    "Authorization": f"Bearer {self.settings.cnbizapi_key}",
                    "X-API-Key": self.settings.cnbizapi_key,
                    "Content-Type": "application/json",
                },
            )
        return self._http

    def close(self) -> None:
        """关闭底层连接。"""
        if self._http is not None:
            self._http.close()

    def search_company(self, name: str, limit: int = 5) -> list[CandidateCompany]:
        """企业模糊搜索（免费）。

        Args:
            name: 企业名称关键词。
            limit: 返回条数上限（压缩 token 的关键：默认只取 5 条）。

        Returns:
            list[CandidateCompany]: 候选列表。
        """
        if self.mock:
            hits = [c for c in self._fixtures.get("candidates", []) if _fuzzy(name, c["name"])]
            return [CandidateCompany.model_validate(c) for c in hits[:limit]]

        resp = self._client().post(
            "/v1/company/search", json={"keyword": name, "limit": limit}
        )
        resp.raise_for_status()
        rows = _unwrap(resp.json())
        return [
            CandidateCompany(
                name=str(r.get("name", "")),
                credit_code=str(r.get("credit_code") or r.get("creditCode") or ""),
                business_scope=str(r.get("business_scope") or r.get("businessScope") or "")[:200],
                province=r.get("province"),
                city=r.get("city"),
            )
            for r in rows
        ]

    def get_company_basic(self, credit_code: str) -> CompanyBasic:
        """查询基本工商信息（免费）。

        Args:
            credit_code: 统一社会信用代码。

        Returns:
            CompanyBasic: 基本信息；查不到时返回空对象。
        """
        if self.mock:
            data = self._fixtures.get("companies", {}).get(credit_code)
            return CompanyBasic.model_validate(data) if data else CompanyBasic()

        resp = self._client().post("/v1/company/basic", json={"credit_code": credit_code})
        resp.raise_for_status()
        payload = _unwrap(resp.json())
        row = payload[0] if isinstance(payload, list) and payload else payload
        return CompanyBasic.model_validate(row or {})

    def get_shareholders(self, credit_code: str) -> list[Shareholder]:
        """查询股东信息（1 积分/次）。

        Args:
            credit_code: 统一社会信用代码。

        Returns:
            list[Shareholder]: 股东列表。
        """
        if self.mock:
            data = self._fixtures.get("companies", {}).get(credit_code, {})
            return [Shareholder.model_validate(s) for s in data.get("shareholders", [])]

        resp = self._client().post("/v1/company/shareholders", json={"credit_code": credit_code})
        resp.raise_for_status()
        rows = _unwrap(resp.json())
        return [
            Shareholder(
                name=str(r.get("name", "")),
                kind="person" if r.get("type") in ("自然人", "person") else "company",
                credit_code=str(r.get("credit_code") or r.get("creditCode") or ""),
                share_pct=_to_float(r.get("share_pct") or r.get("sharePct") or r.get("percent")),
                share_raw=str(r.get("share_raw") or r.get("shareRaw") or ""),
            )
            for r in rows
        ]


def _to_float(value: Any) -> float:
    """把持股比例的多种写法统一成浮点数（百分数）。

    Args:
        value: 原始值，可能是 ``"60.5%"`` / ``0.605`` / ``"60.5"``。

    Returns:
        float: 百分数形式的持股比例。
    """
    if value in (None, ""):
        return 0.0
    try:
        text = str(value).strip().rstrip("%")
        num = float(text)
    except ValueError:
        return 0.0
    # 小于 1 且非 0 时视为小数形式，换算为百分数
    return num * 100 if 0 < num <= 1 else num


def _unwrap(payload: Any) -> list[dict[str, Any]]:
    """把响应的常见包装结构统一成列表。

    Args:
        payload: 接口返回的 JSON。

    Returns:
        list[dict[str, Any]]: 记录列表。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return _unwrap(value)
        return [payload]
    return []


def _fuzzy(keyword: str, name: str) -> bool:
    """极简中文模糊匹配：关键词的任意 2 字子串出现在名称中即命中。

    Args:
        keyword: 搜索词。
        name: 候选名称。

    Returns:
        bool: 是否命中。
    """
    if not keyword:
        return False
    if keyword in name or name in keyword:
        return True
    return any(keyword[i : i + 2] in name for i in range(max(1, len(keyword) - 1)))


@lru_cache(maxsize=1)
def _load_fixtures() -> dict[str, Any]:
    """加载离线样例数据。

    Returns:
        dict[str, Any]: fixture 内容；文件不存在时返回空结构。
    """
    path = config_mod.FIXTURES_DIR / "cnbiz" / "sample.json"
    if not path.exists():
        return {"candidates": [], "companies": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_path() -> Path:
    """返回 fixture 文件路径。"""
    return config_mod.FIXTURES_DIR / "cnbiz" / "sample.json"
