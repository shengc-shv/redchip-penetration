"""CNBizAPI（ChinaBiz MCP API）数据源实现。

接口形式（2026-09 实测根文档 ``https://api.cnbizapi.com/``）
-----------------------------------------------------------
- ``GET /v1/company/search?keyword=<关键词>``   企业模糊搜索（免费）
- ``GET /v1/company/basic?q=<名称或代码>``      基本工商信息（免费）
- ``GET /v1/company/shareholders?q=<名称或代码>`` 股东信息（付费，1 积分/次）
- 鉴权：``Authorization: Bearer <API_KEY>``

注意两点实测结论
----------------
1. 是 **GET + query**，不是 POST + JSON body；
2. 该服务的 TLS 证书在 2026-09 实测时**已过期**（``CN=cnbizapi.com``，2026-04-08 起，
   约 90 天有效期）。严格校验下 httpx 会直接报 ``CERTIFICATE_VERIFY_FAILED``。
   因此这里提供显式开关 ``REDCHIP_ALLOW_EXPIRED_CERT``：仅在用户明确开启时对该域名
   放宽校验，并在每次构造客户端时记录一次警告。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from redchip import config as config_mod
from redchip.domestic.sources.base import (
    CompanyBasic,
    IdentifierIndex,
    Shareholder,
    looks_like_uscc,
    pick_field,
    to_float,
    to_kind,
    unwrap,
)
from redchip.models.schema import CandidateCompany

logger = logging.getLogger(__name__)

_pick = pick_field


class CnbizApiSource:
    """CNBizAPI 数据源。

    Args:
        settings: 全局配置。
        index: 名称↔代码映射缓存（跨数据源共享，由门面注入）。
    """

    name = "cnbizapi"

    def __init__(self, settings: config_mod.Settings, index: IdentifierIndex) -> None:
        self.settings = settings
        self.index = index
        self._http: httpx.Client | None = None
        if settings.redchip_allow_expired_cert:
            logger.warning(
                "工商数据源 %s 已放宽 TLS 校验（REDCHIP_ALLOW_EXPIRED_CERT=true）："
                "该服务证书过期，此设置会失去中间人防护，请尽快推动服务方续证。",
                settings.cnbizapi_base_url,
            )

    # ------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        """懒加载 HTTP 客户端。

        Returns:
            httpx.Client: 指向配置的 base_url 的客户端。
        """
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.settings.cnbizapi_base_url.rstrip("/"),
                timeout=self.settings.http_timeout,
                verify=not self.settings.redchip_allow_expired_cert,
                headers={
                    "Authorization": f"Bearer {self.settings.cnbizapi_key}",
                    "X-API-Key": self.settings.cnbizapi_key,
                    "Accept": "application/json",
                },
            )
        return self._http

    def close(self) -> None:
        """关闭底层连接。"""
        if self._http is not None:
            self._http.close()
            self._http = None

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """发起 GET 请求；任何异常都记警告并返回空结构（降级优先于中断）。

        Args:
            path: 接口路径。
            params: query 参数。

        Returns:
            Any: 响应 JSON；失败时返回 ``{}``。
        """
        try:
            resp = self._client().get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("CNBizAPI %s 返回 %s：%s", path, exc.response.status_code, exc.response.text[:160])
        except httpx.HTTPError as exc:
            logger.warning("CNBizAPI %s 请求失败：%s", path, exc)
        return {}

    # ------------------------------------------------------------------

    def search_company(self, name: str, limit: int = 5) -> list[CandidateCompany]:
        """企业模糊搜索。

        Args:
            name: 企业名称关键词。
            limit: 返回条数上限（客户端截断）。

        Returns:
            list[CandidateCompany]: 候选列表。
        """
        # 只传官方示例确认过的参数：服务端是 NestJS，若开启了未知属性严格校验，
        # 多传 limit 会直接 400；条数上限在客户端截断即可，省一次排障
        payload = self._get("/v1/company/search", {"keyword": name})
        rows = unwrap(payload)
        out: list[CandidateCompany] = []
        for row in rows[:limit]:
            candidate = CandidateCompany(
                name=str(_pick(row, "name")),
                credit_code=str(_pick(row, "credit_code")),
                business_scope=str(_pick(row, "business_scope"))[:200],
                province=_pick(row, "province") or None,
                city=_pick(row, "city") or None,
            )
            self.index.remember(candidate.name, candidate.credit_code)
            out.append(candidate)
        return out

    def get_company_basic(self, identifier: str) -> CompanyBasic:
        """查询企业基本工商信息。

        Args:
            identifier: 统一社会信用代码或企业名称；代码会先翻译成名称。

        Returns:
            CompanyBasic: 基本信息；查不到时各字段为空。
        """
        query, _ = self.index.resolve(identifier)
        payload = self._get("/v1/company/basic", {"q": query})
        rows = unwrap(payload)
        row = rows[0] if rows else {}
        basic = CompanyBasic(
            name=str(_pick(row, "name")),
            credit_code=str(_pick(row, "credit_code")),
            province=str(_pick(row, "province")),
            city=str(_pick(row, "city")),
            reg_status=str(_pick(row, "reg_status")),
            established_at=str(_pick(row, "established_at")),
            business_scope=str(_pick(row, "business_scope"))[:500],
            legal_person=str(_pick(row, "legal_person")),
        )
        self.index.remember(basic.name, basic.credit_code)
        # 入参是代码而响应未回代码时用入参补全，避免穿透链上映射断掉
        if not basic.credit_code and looks_like_uscc(identifier):
            self.index.remember(basic.name, identifier)
            basic.credit_code = identifier
        return basic

    def get_shareholders(self, identifier: str) -> list[Shareholder]:
        """查询股东信息（付费接口，1 积分/次）。

        Args:
            identifier: 统一社会信用代码或企业名称。

        Returns:
            list[Shareholder]: 股东列表。
        """
        query, _ = self.index.resolve(identifier)
        payload = self._get("/v1/company/shareholders", {"q": query})
        out: list[Shareholder] = []
        for row in unwrap(payload):
            holder_name = str(_pick(row, "name"))
            if not holder_name:
                continue
            out.append(
                Shareholder(
                    name=holder_name,
                    kind=to_kind(_pick(row, "type"), holder_name),
                    credit_code=str(_pick(row, "credit_code")),
                    share_pct=to_float(_pick(row, "share_pct", 0)),
                    share_raw=str(_pick(row, "share_raw")),
                )
            )
        return out
