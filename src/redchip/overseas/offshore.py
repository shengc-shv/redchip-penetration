"""离岸层验证（可选）：OpenRegistry MCP。

用途
----
覆盖 27+ 个国家/地区的官方公司注册处，用于核验开曼 / BVI / 香港中间层的
**存续状态与基础注册信息**。

关键限制
--------
开曼与 BVI 的完整股东名册不向公众开放，因此该层**只能验证存在性**，
不能替代境外披露文件做股权穿透。默认不参与主流程，需显式调用。
"""

from __future__ import annotations

from typing import Any

import httpx

from redchip import config as config_mod


def search_companies(
    name: str,
    jurisdiction: str = "KY",
    settings: config_mod.Settings | None = None,
) -> list[dict[str, Any]]:
    """检索离岸公司注册信息。

    Args:
        name: 公司名关键词。
        jurisdiction: 注册地代码（KY=开曼，VG=BVI，HK=香港）。
        settings: 全局配置；缺省自动载入。

    Returns:
        list[dict[str, Any]]: 命中记录；接口不可用或出错时返回空列表。
    """
    cfg = settings or config_mod.get_settings()
    payload = {
        "tool": "search_companies",
        "args": {"name": name, "jurisdiction": jurisdiction},
    }
    try:
        resp = httpx.post(
            cfg.openregistry_mcp_url,
            json=payload,
            timeout=cfg.http_timeout,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 - 离岸层为可选增强，失败不应影响主流程
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "data", "items", "companies"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    return []


def verify_exists(name: str, jurisdiction: str = "KY") -> bool:
    """判断离岸实体是否存在于官方注册处。

    Args:
        name: 公司名。
        jurisdiction: 注册地代码。

    Returns:
        bool: 命中返回 True。
    """
    return bool(search_companies(name, jurisdiction))


__all__ = ["search_companies", "verify_exists"]
