"""境内工商数据源适配器层测试。

覆盖三块：
1. 共享工具（标识识别、字段别名、比例与类型归一化）
2. 数据源选择逻辑（密钥缺失时的降级路径）
3. CNBizAPI 真实接口形式（GET + query、Bearer 鉴权、代码→名称翻译）
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from redchip import config as config_mod
from redchip.domestic.cnbiz import CnbizClient
from redchip.domestic.sources import build_source
from redchip.domestic.sources.base import (
    IdentifierIndex,
    looks_like_uscc,
    pick_field,
    to_float,
    to_kind,
    unwrap,
)
from redchip.domestic.sources.cnbizapi import CnbizApiSource
from redchip.domestic.sources.fixtures import FixtureSource
from redchip.domestic.sources.qcc import QccSource


def _settings(**env: object) -> config_mod.Settings:
    """构造隔离配置。

    Args:
        **env: 需要覆盖的字段。

    Returns:
        Settings: 配置对象（不依赖进程环境与 .env）。
    """
    base: dict[str, object] = {
        "redchip_mock": False,
        "cnbizapi_key": "",
        "registry_source": "auto",
        "qcc_app_key": "",
        "qcc_secret_key": "",
        "redchip_allow_expired_cert": False,
    }
    base.update(env)
    return config_mod.Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 共享工具
# ---------------------------------------------------------------------------


def test_looks_like_uscc():
    """18 位统一社会信用代码识别，普通名称不误判。"""
    assert looks_like_uscc("9144030071526726XG") is True
    assert looks_like_uscc("91110108551385082Q") is True
    assert looks_like_uscc("腾讯科技（深圳）有限公司") is False
    assert looks_like_uscc("") is False


def test_index_translates_both_directions():
    """名称↔代码双向翻译。"""
    index = IdentifierIndex()
    index.remember("腾讯科技（深圳）有限公司", "9144030071526726XG")
    assert index.resolve("9144030071526726XG")[0] == "腾讯科技（深圳）有限公司"
    assert index.resolve("腾讯科技（深圳）有限公司")[1] == "9144030071526726XG"


def test_index_passes_through_unknown_identifier():
    """未登记的代码原样返回，避免因缺映射而查不到。"""
    index = IdentifierIndex()
    assert index.resolve("91440300700000000X") == ("91440300700000000X", "91440300700000000X")


def test_unwrap_empty_payload_yields_no_rows():
    """空响应不能产生一条空记录（否则会污染节点）。"""
    assert unwrap({}) == []
    assert unwrap({"success": True}) == [{"success": True}]


def test_unwrap_nested_data():
    """嵌套结构能逐层拆到记录列表。"""
    payload = {"success": True, "data": {"list": [{"name": "A"}, {"name": "B"}]}}
    assert [row["name"] for row in unwrap(payload)] == ["A", "B"]


def test_pick_field_handles_provider_naming():
    """各服务商的字段命名差异由别名表抹平。"""
    row = {"companyName": "甲公司", "creditCode": "91310000X", "stockPercent": "60.5%"}
    assert pick_field(row, "name") == "甲公司"
    assert pick_field(row, "credit_code") == "91310000X"
    assert pick_field(row, "share_pct") == "60.5%"
    assert pick_field(row, "province", "—") == "—"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(60.5, 60.5), ("60.5%", 60.5), ("0.6", 60.0), (0.6, 60.0), ("", 0.0), (None, 0.0), ("abc", 0.0)],
)
def test_to_float(raw, expected):
    """持股比例的小数/百分数/字符串写法都要归一成百分数。"""
    assert to_float(raw) == expected


def test_to_kind_detects_person_and_company():
    """股东类型判断：显式字段优先，缺失时按名称兜底。"""
    assert to_kind("自然人") == "person"
    assert to_kind("person") == "person"
    assert to_kind("企业法人") == "company"
    assert to_kind("", "马化腾") == "person"
    assert to_kind("", "腾讯科技（深圳）有限公司") == "company"
    assert to_kind("", "Tencent Technology (Hong Kong) Limited") == "company"


# ---------------------------------------------------------------------------
# 数据源选择
# ---------------------------------------------------------------------------


def test_build_source_falls_back_to_fixture_without_keys():
    """无任何密钥时回落离线样例（保证 CI 可跑）。"""
    source = build_source(_settings(), IdentifierIndex())
    assert isinstance(source, FixtureSource)
    assert source.name == "fixture"


def test_build_source_prefers_cnbizapi_when_key_present():
    """配置了 CNBizAPI Key 时优先使用它。"""
    source = build_source(_settings(cnbizapi_key="cbz_test"), IdentifierIndex())
    assert isinstance(source, CnbizApiSource)
    assert CnbizClient(_settings(cnbizapi_key="cbz_test")).is_live is True


def test_build_source_prefers_qcc_when_only_qcc_keys_present():
    """只有企查查密钥时选企查查。"""
    source = build_source(
        _settings(qcc_app_key="app", qcc_secret_key="secret"), IdentifierIndex()
    )
    assert isinstance(source, QccSource)
    assert source.name == "qcc"


def test_mock_flag_forces_fixture_even_with_keys():
    """REDCHIP_MOCK=true 时不发起任何外部请求。"""
    source = build_source(_settings(redchip_mock=True, cnbizapi_key="cbz_test"), IdentifierIndex())
    assert isinstance(source, FixtureSource)


def test_explicit_source_without_key_degrades():
    """显式指定数据源但缺密钥时降级为样例，不抛异常。"""
    source = build_source(_settings(registry_source="cnbizapi"), IdentifierIndex())
    assert isinstance(source, FixtureSource)


# ---------------------------------------------------------------------------
# CNBizAPI 真实接口形式
# ---------------------------------------------------------------------------


def _install_transport(monkeypatch, source: CnbizApiSource, handler) -> None:
    """把 MockTransport 注入数据源的 HTTP 客户端。

    不能直接替换 ``source._http``：那样会绕过产品代码设置的表头，鉴权头就测不到了。
    这里包装 ``httpx.Client`` 工厂——让真实客户端带着 headers 构造，只换传输层。

    Args:
        monkeypatch: pytest 的 monkeypatch fixture。
        source: 数据源实例。
        handler: 请求处理函数。
    """
    real_client = httpx.Client

    def factory(**kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", factory)
    source._http = None


def test_cnbizapi_uses_get_with_keyword_query(monkeypatch):
    """搜索接口必须是 GET + keyword 查询参数（实测的真实形式）。"""
    source = CnbizApiSource(_settings(cnbizapi_key="cbz_k"), IdentifierIndex())
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            method=request.method,
            path=request.url.path,
            params=dict(request.url.params),
            auth=request.headers.get("authorization"),
        )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"name": "腾讯科技（深圳）有限公司", "creditCode": "9144030071526726XG"}],
            },
        )

    _install_transport(monkeypatch, source, handler)
    rows = source.search_company("腾讯", limit=2)

    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/company/search"
    assert seen["params"]["keyword"] == "腾讯"
    assert seen["auth"] == "Bearer cbz_k"
    assert rows[0].credit_code == "9144030071526726XG"


def test_cnbizapi_translates_code_to_name_for_shareholders(monkeypatch):
    """股东接口按名称查询：传代码时应先翻译成已登记的名称。"""
    source = CnbizApiSource(_settings(cnbizapi_key="cbz_k"), IdentifierIndex())
    source.index.remember("深圳市腾讯计算机系统有限公司", "91440300708461136T")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, params=dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"name": "马化腾", "type": "自然人", "percent": "54.29%"},
                    {"name": "张志东", "type": "自然人", "percent": "22.86%"},
                ],
            },
        )

    _install_transport(monkeypatch, source, handler)
    holders = source.get_shareholders("91440300708461136T")

    assert seen["path"] == "/v1/company/shareholders"
    assert seen["params"]["q"] == "深圳市腾讯计算机系统有限公司"
    assert [h.name for h in holders] == ["马化腾", "张志东"]
    assert holders[0].kind == "person"
    assert holders[0].share_pct == 54.29


def test_cnbizapi_degrades_on_error_without_raising(monkeypatch):
    """接口报错时记警告并返回空结果，不中断流水线。"""
    source = CnbizApiSource(_settings(cnbizapi_key="cbz_k"), IdentifierIndex())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, source, handler)
    assert source.get_shareholders("91440300700000000X") == []
    assert source.search_company("腾讯") == []


def test_allow_expired_cert_switch_sets_verify_false(monkeypatch):
    """证书开关打开时才对底层客户端放宽校验（默认严格）。"""
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    strict = CnbizApiSource(_settings(cnbizapi_key="cbz_k"), IdentifierIndex())
    strict._client()
    assert captured["verify"] is True

    relaxed = CnbizApiSource(
        _settings(cnbizapi_key="cbz_k", redchip_allow_expired_cert=True), IdentifierIndex()
    )
    relaxed._client()
    assert captured["verify"] is False


def test_qcc_signature_is_uppercase_md5():
    """企查查签名 = MD5(key + Timespan + SecretKey) 的 32 位大写。"""
    source = QccSource(_settings(qcc_app_key="app", qcc_secret_key="sec"), IdentifierIndex())
    expected = hashlib.md5(b"app1700000000sec").hexdigest().upper()
    assert source._token("1700000000") == expected
    assert len(source._token("1700000000")) == 32
