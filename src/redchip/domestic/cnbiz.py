"""境内工商数据源门面。

历史沿革
--------
本模块原名即「CNBizAPI 客户端」，全部实现写在这里。随着数据源从单一供应商扩展为
可切换的适配器（CNBizAPI / 企查查 / 离线样例，见 :mod:`redchip.domestic.sources`），
这里收敛为**门面**：保留原有类名 ``CnbizClient`` 与三个方法签名，调用方（穿透层、
流水线）零改动，实际取数委托给当前选中的数据源。

统一标识
--------
穿透层以统一社会信用代码为主键，而真实接口按企业名称查询；:class:`IdentifierIndex`
在门面内维护名称↔代码映射，由搜索与基本信息接口自动回填，对外无需感知。

计费说明（以 CNBizAPI 为例）
----------------------------
- ``search_company`` / ``get_company_basic``：免费
- ``get_shareholders``：付费（1 积分/次），穿透时的主要消耗，免费额度 200 次/月

离线联调
--------
未配置任何密钥或 ``REDCHIP_MOCK=true`` 时，自动回落 ``fixtures/cnbiz/sample.json``，
保证流水线在无密钥环境端到端跑通（数据为示意口径，不得当作核验事实）。
"""

from __future__ import annotations

from redchip import config as config_mod
from redchip.domestic.sources import (
    CnbizUnavailableError,
    CompanyBasic,
    IdentifierIndex,
    Shareholder,
    build_source,
    fixture_path,
    load_fixtures,
)
from redchip.models.schema import CandidateCompany

__all__ = [
    "CandidateCompany",
    "CnbizClient",
    "CnbizUnavailableError",
    "CompanyBasic",
    "Shareholder",
    "fixture_path",
    "load_fixtures",
]


class CnbizClient:
    """境内工商数据源门面（类名为兼容历史调用保留）。

    Args:
        settings: 全局配置；缺省自动载入。
    """

    def __init__(self, settings: config_mod.Settings | None = None) -> None:
        self.settings = settings or config_mod.get_settings()
        self.index = IdentifierIndex()
        self._source = build_source(self.settings, self.index)

    # ------------------------------------------------------------------

    @property
    def source_name(self) -> str:
        """当前生效的数据源名（``cnbizapi`` / ``qcc`` / ``fixture``）。

        Returns:
            str: 数据源标识。
        """
        return self._source.name

    @property
    def is_live(self) -> bool:
        """是否走真实 API（离线样例源为 False）。

        Returns:
            bool: 真实数据源返回 True。
        """
        return self._source.name != "fixture"

    def close(self) -> None:
        """关闭底层连接（离线源无连接，安全空操作）。"""
        close = getattr(self._source, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------------

    def search_company(self, name: str, limit: int = 5) -> list[CandidateCompany]:
        """企业模糊搜索（免费接口）。

        Args:
            name: 企业名称关键词。
            limit: 返回条数上限（压缩 token 的关键：默认只取 5 条）。

        Returns:
            list[CandidateCompany]: 候选列表。
        """
        return self._source.search_company(name, limit=limit)

    def get_company_basic(self, credit_code: str) -> CompanyBasic:
        """查询基本工商信息（免费接口）。

        Args:
            credit_code: 统一社会信用代码（也可传企业名称）。

        Returns:
            CompanyBasic: 基本信息；查不到时返回空对象。
        """
        return self._source.get_company_basic(credit_code)

    def get_shareholders(self, credit_code: str) -> list[Shareholder]:
        """查询股东信息（付费接口）。

        Args:
            credit_code: 统一社会信用代码（也可传企业名称）。

        Returns:
            list[Shareholder]: 股东列表。
        """
        return self._source.get_shareholders(credit_code)
