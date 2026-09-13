#!/usr/bin/env python3
"""CNBizAPI Key 自检：确认密钥有效、接口可达、适配层能正确解析。

为什么要单独自检
----------------
该服务的排障成本不低：TLS 证书已过期、接口是 GET + query、部分接口按积分计费。
配好 Key 后先跑本脚本，能一次分清「Key 无效 / 网络证书问题 / 字段名不匹配 / 配额不足」，
避免把这些混在一起猜。

两段验证
--------
1. **裸接口**：直接照官网示例发请求，确认服务与密钥状态（不依赖项目代码）
2. **适配层**：走 ``redchip.domestic.sources`` 真实取数，确认字段解析与名称↔代码翻译正常

付费接口默认不调用：``get_shareholders`` 每次消耗 1 积分，需显式加 ``--with-shareholders``。

用法::

    PYTHONPATH=src python scripts/check_cnbizapi.py
    PYTHONPATH=src python scripts/check_cnbizapi.py --name "腾讯科技（深圳）有限公司"
    PYTHONPATH=src python scripts/check_cnbizapi.py --with-shareholders
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import warnings

import httpx

BASE_URL = "https://api.cnbizapi.com"


def _client(key: str = "") -> httpx.Client:
    """构造探测用客户端。

    Args:
        key: API Key；为空时不带鉴权头。

    Returns:
        httpx.Client: 客户端（对过期证书放宽校验，见模块说明）。
    """
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(
        base_url=BASE_URL, timeout=30, verify=False, headers=headers
    )


def probe_raw(key: str, name: str, with_shareholders: bool) -> None:
    """第一段：照官网示例直接调接口，逐项打印状态。

    Args:
        key: API Key。
        name: 用于查询的企业名称。
        with_shareholders: 是否调用付费的股东接口。
    """
    print("=" * 68)
    print("① 裸接口探测（照官网示例，不依赖项目代码）")
    print("=" * 68)

    checks: list[tuple[str, str, dict[str, str]]] = [
        ("服务信息 /v1/tools（免鉴权）", "/v1/tools", {}),
        ("模糊搜索 /v1/company/search（免费）", "/v1/company/search", {"keyword": name[:4]}),
        ("基本信息 /v1/company/basic（免费）", "/v1/company/basic", {"q": name}),
    ]
    if with_shareholders:
        checks.append(("股东信息 /v1/company/shareholders（付费 1 积分）", "/v1/company/shareholders", {"q": name}))
    else:
        print("\n（跳过 /v1/company/shareholders：该接口 1 积分/次，"
              "加 --with-shareholders 才会调用）")

    with _client(key) as client:
        for label, path, params in checks:
            started = time.monotonic()
            try:
                resp = client.get(path, params=params)
                cost = time.monotonic() - started
                body = resp.text[:220].replace("\n", " ")
                print(f"\n▸ {label}")
                print(f"  HTTP {resp.status_code} · {cost:.2f}s")
                print(f"  {body}")
            except httpx.HTTPError as exc:
                print(f"\n▸ {label}\n  ❌ 请求失败：{exc}")


def probe_adapter(key: str, name: str, with_shareholders: bool) -> None:
    """第二段：走项目适配层取数，验证字段解析与标识翻译。

    Args:
        key: API Key。
        name: 用于查询的企业名称。
        with_shareholders: 是否调用付费的股东接口。
    """
    print()
    print("=" * 68)
    print("② 适配层验证（项目代码 redchip.domestic.sources）")
    print("=" * 68)

    sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..", "src")))
    from redchip import config as config_mod
    from redchip.domestic.sources import build_source
    from redchip.domestic.sources.base import IdentifierIndex

    settings = config_mod.Settings(
        cnbizapi_key=key, registry_source="cnbizapi", redchip_allow_expired_cert=True
    )
    source = build_source(settings, IdentifierIndex())
    print(f"\n数据源：{source.name}")

    hits = source.search_company(name[:4], limit=3)
    print(f"\n▸ 搜索「{name[:4]}」→ {len(hits)} 条")
    for item in hits:
        print(f"    {item.name}  [{item.credit_code or '无代码'}]  {item.province or ''}{item.city or ''}")

    if not hits:
        print("  ⚠️ 无结果：可能是 Key 无效、配额耗尽，或服务端字段名与预期不同")

    target = hits[0].name if hits else name
    basic = source.get_company_basic(target)
    print(f"\n▸ 基本信息（{target}）")
    print(f"    名称={basic.name or '（空）'} 代码={basic.credit_code or '（空）'}")
    print(f"    省/市={basic.province or '—'}/{basic.city or '—'} 法定代表人={basic.legal_person or '—'}")
    if not basic.name:
        print("  ⚠️ 字段为空：检查 FIELD_ALIASES 是否覆盖了服务端实际字段名")

    if not with_shareholders:
        return
    holders = source.get_shareholders(basic.credit_code or target)
    print(f"\n▸ 股东（{len(holders)} 位，本次消耗 1 积分）")
    total = 0.0
    for holder in holders:
        total += holder.share_pct
        print(f"    {holder.name}  {holder.share_pct}%  [{holder.kind}]")
    print(f"    合计 {total:.2f}%" + ("  ✅ 接近 100%" if 99.5 <= total <= 100.5 else "  ⚠️ 不为 100%，名册可能不完整"))


def main() -> int:
    """命令行入口。

    Returns:
        int: 进程退出码。
    """
    parser = argparse.ArgumentParser(description="CNBizAPI Key 自检")
    parser.add_argument("--key", default="", help="API Key；默认读环境变量 CNBIZAPI_KEY")
    parser.add_argument("--name", default="腾讯科技（深圳）有限公司", help="用于查询的企业名称")
    parser.add_argument(
        "--with-shareholders", action="store_true", help="同时验证股东接口（付费，1 积分/次）"
    )
    args = parser.parse_args()

    warnings.filterwarnings("ignore")  # 证书过期告警，见模块说明
    # 只保留我们的降级警告：httpx 自身的 INFO 级请求日志会把探测输出淹掉
    logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")

    key = args.key or os.environ.get("CNBIZAPI_KEY", "")
    if not key:
        print("未提供 API Key：请用 --key 传入，或先设置环境变量 CNBIZAPI_KEY", file=sys.stderr)
        print("申请方式见 scripts/get_cnbizapi_key.py", file=sys.stderr)
        return 2

    print(f"目标企业：{args.name}")
    print(f"API Key：{key[:8]}…（共 {len(key)} 字符）")

    probe_raw(key, args.name, args.with_shareholders)
    probe_adapter(key, args.name, args.with_shareholders)

    print()
    print("=" * 68)
    print("自检结束。若裸接口正常但适配层为空，多半是返回字段名差异 ——")
    print("把上面裸接口输出的 JSON 片段贴出来，我按实际字段补 FIELD_ALIASES。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
