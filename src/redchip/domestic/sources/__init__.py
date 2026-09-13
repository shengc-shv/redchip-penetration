"""境内工商数据源适配器。

用法::

    from redchip.domestic.sources import build_source, IdentifierIndex

    source = build_source(settings, IdentifierIndex())
    holders = source.get_shareholders("9144030071526726XG")

选择逻辑（环境变量 ``REGISTRY_SOURCE``）
---------------------------------------
- ``auto``（默认）：有 CNBizAPI Key 就用它；否则有企查查密钥就用企查查；都没有则回落离线样例
- ``cnbizapi`` / ``qcc`` / ``fixture``：强制指定，便于排障与对比

``REDCHIP_MOCK=true`` 时一律使用离线样例，不发起任何外部请求。

新增数据源
----------
实现 :class:`~redchip.domestic.sources.base.CompanyDataSource` 的三个方法（搜索 / 基本信息 /
股东），在 :func:`build_source` 登记即可，穿透层无需改动。
"""

from __future__ import annotations

import logging
from typing import Literal

from redchip import config as config_mod
from redchip.domestic.sources.base import (
    CnbizUnavailableError,
    CompanyBasic,
    CompanyDataSource,
    IdentifierIndex,
    Shareholder,
    looks_like_uscc,
    pick_field,
    to_float,
    to_kind,
    unwrap,
)
from redchip.domestic.sources.cnbizapi import CnbizApiSource
from redchip.domestic.sources.fixtures import FixtureSource, fixture_path, load_fixtures
from redchip.domestic.sources.qcc import QccSource

logger = logging.getLogger(__name__)

SourceName = Literal["auto", "cnbizapi", "qcc", "fixture"]

__all__ = [
    "CnbizApiSource",
    "CnbizUnavailableError",
    "CompanyBasic",
    "CompanyDataSource",
    "FixtureSource",
    "IdentifierIndex",
    "QccSource",
    "Shareholder",
    "SourceName",
    "build_source",
    "fixture_path",
    "load_fixtures",
    "looks_like_uscc",
    "pick_field",
    "to_float",
    "to_kind",
    "unwrap",
]


def build_source(settings: config_mod.Settings, index: IdentifierIndex) -> CompanyDataSource:
    """按配置构造工商数据源实例。

    Args:
        settings: 全局配置。
        index: 名称↔代码映射缓存（由门面持有，跨请求复用）。

    Returns:
        CompanyDataSource: 数据源实例；配置缺失时回落离线样例。
    """
    choice: str = settings.registry_source if not settings.redchip_mock else "fixture"

    if choice == "auto":
        if settings.cnbizapi_key:
            choice = "cnbizapi"
        elif settings.qcc_app_key and settings.qcc_secret_key:
            choice = "qcc"
        else:
            choice = "fixture"

    if choice == "cnbizapi":
        if not settings.cnbizapi_key:
            logger.warning("指定了 cnbizapi 但未配置 CNBIZAPI_KEY，回落离线样例数据")
            return FixtureSource(settings, index)
        return CnbizApiSource(settings, index)

    if choice == "qcc":
        if not (settings.qcc_app_key and settings.qcc_secret_key):
            logger.warning("指定了 qcc 但未配置 QCC_APP_KEY / QCC_SECRET_KEY，回落离线样例数据")
            return FixtureSource(settings, index)
        return QccSource(settings, index)

    return FixtureSource(settings, index)
