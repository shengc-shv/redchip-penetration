"""企查查开放平台数据源实现。

鉴权方式（依官方接口文档，见 ``mapi.qcc.com/dataApi/*``）
------------------------------------------------------
- 请求头 ``Token``：``MD5(key + Timespan + SecretKey)`` 的 32 位大写字符串
- 请求头 ``Timespan``：精确到秒的 Unix 时间戳
- Query 参数 ``key``：应用 AppKey；``searchKey``：统一社会信用代码或企业名称

⚠️ 实测状态
-----------
本实现**未经真实 Key 联调**：签名算法与参数名取自官方文档，但下列接口路径按命名
惯例推断，接入前须以开放平台实际文档校正（常量集中在文件头部，改动只涉及这几行）。
另注：官方页面标注「限企业实名用户使用」且「需提供应用场景审核」，个人账号无法开通。
"""

from __future__ import annotations

import hashlib
import logging
import time
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

# ---- 待联调校正的接口路径（以开放平台实际文档为准）----
SEARCH_PATH = "/ECICompanySearch/GetList"
BASIC_PATH = "/ECIInfoOverview/GetInfo"
SHAREHOLDER_PATH = "/ECIStockHolder/GetList"

# 查询类型：1-最新公示，2-工商登记
SEARCH_TYPE = "2"


class QccSource:
    """企查查开放平台数据源。

    Args:
        settings: 全局配置。
        index: 名称↔代码映射缓存。
    """

    name = "qcc"

    def __init__(self, settings: config_mod.Settings, index: IdentifierIndex) -> None:
        self.settings = settings
        self.index = index
        self._http: httpx.Client | None = None

    # ------------------------------------------------------------------

    def _token(self, timestamp: str) -> str:
        """计算接口签名。

        Args:
            timestamp: 与请求头一致的 Unix 秒级时间戳字符串。

        Returns:
            str: 32 位大写 MD5 签名。
        """
        raw = f"{self.settings.qcc_app_key}{timestamp}{self.settings.qcc_secret_key}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

    def _client(self) -> httpx.Client:
        """懒加载 HTTP 客户端。

        Returns:
            httpx.Client: 指向开放平台网关的客户端。
        """
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.settings.qcc_base_url.rstrip("/"),
                timeout=self.settings.http_timeout,
                verify=not self.settings.redchip_allow_expired_cert,
                headers={"Accept": "application/json"},
            )
        return self._http

    def close(self) -> None:
        """关闭底层连接。"""
        if self._http is not None:
            self._http.close()
            self._http = None

    def _get(self, path: str, search_key: str, extra: dict[str, Any] | None = None) -> Any:
        """发起带签名的 GET 请求；异常一律记警告并返回空结构。

        Args:
            path: 接口路径。
            search_key: 搜索关键词（名称或信用代码）。
            extra: 附加 query 参数。

        Returns:
            Any: 响应 JSON；失败时返回 ``{}``。
        """
        timestamp = str(int(time.time()))
        params: dict[str, Any] = {
            "key": self.settings.qcc_app_key,
            "searchKey": search_key,
            "searchType": SEARCH_TYPE,
            **(extra or {}),
        }
        try:
            resp = self._client().get(
                path, params=params, headers={"Token": self._token(timestamp), "Timespan": timestamp}
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("企查查 %s 返回 %s：%s", path, exc.response.status_code, exc.response.text[:160])
        except httpx.HTTPError as exc:
            logger.warning("企查查 %s 请求失败：%s", path, exc)
        return {}

    # ------------------------------------------------------------------

    def search_company(self, name: str, limit: int = 5) -> list[CandidateCompany]:
        """企业模糊搜索。

        Args:
            name: 企业名称关键词。
            limit: 返回条数上限。

        Returns:
            list[CandidateCompany]: 候选列表。
        """
        payload = self._get(SEARCH_PATH, name, {"pageIndex": "1", "pageSize": str(limit)})
        out: list[CandidateCompany] = []
        for row in unwrap(payload)[:limit]:
            candidate = CandidateCompany(
                name=str(pick_field(row, "name")),
                credit_code=str(pick_field(row, "credit_code")),
                business_scope=str(pick_field(row, "business_scope"))[:200],
                province=pick_field(row, "province") or None,
                city=pick_field(row, "city") or None,
            )
            self.index.remember(candidate.name, candidate.credit_code)
            out.append(candidate)
        return out

    def get_company_basic(self, identifier: str) -> CompanyBasic:
        """查询企业基本工商信息。

        Args:
            identifier: 统一社会信用代码或企业名称。

        Returns:
            CompanyBasic: 基本信息；查不到时各字段为空。
        """
        query, _ = self.index.resolve(identifier)
        rows = unwrap(self._get(BASIC_PATH, query))
        row = rows[0] if rows else {}
        basic = CompanyBasic(
            name=str(pick_field(row, "name")),
            credit_code=str(pick_field(row, "credit_code")),
            province=str(pick_field(row, "province")),
            city=str(pick_field(row, "city")),
            reg_status=str(pick_field(row, "reg_status")),
            established_at=str(pick_field(row, "established_at")),
            business_scope=str(pick_field(row, "business_scope"))[:500],
            legal_person=str(pick_field(row, "legal_person")),
        )
        self.index.remember(basic.name, basic.credit_code)
        if not basic.credit_code and looks_like_uscc(identifier):
            self.index.remember(basic.name, identifier)
            basic.credit_code = identifier
        return basic

    def get_shareholders(self, identifier: str) -> list[Shareholder]:
        """查询股东信息。

        Args:
            identifier: 统一社会信用代码或企业名称。

        Returns:
            list[Shareholder]: 股东列表。
        """
        query, _ = self.index.resolve(identifier)
        out: list[Shareholder] = []
        for row in unwrap(self._get(SHAREHOLDER_PATH, query)):
            holder_name = str(pick_field(row, "name"))
            if not holder_name:
                continue
            out.append(
                Shareholder(
                    name=holder_name,
                    kind=to_kind(pick_field(row, "type"), holder_name),
                    credit_code=str(pick_field(row, "credit_code")),
                    share_pct=to_float(pick_field(row, "share_pct", 0)),
                    share_raw=str(pick_field(row, "share_raw")),
                )
            )
        return out
