"""广东省地域过滤测试。"""

from redchip.domestic.cnbiz import CompanyBasic
from redchip.domestic.penetration import GUANGDONG_CITIES, is_guangdong


def test_province_field_matches():
    """province 为「广东省」即命中。"""
    assert is_guangdong(CompanyBasic(province="广东省", city="深圳")) is True


def test_city_alias_without_province():
    """部分数据源只返回城市名，也应命中。"""
    assert is_guangdong(CompanyBasic(province="", city="佛山")) is True


def test_dict_input_is_supported():
    """允许直接传 dict，兼容不同数据源返回结构。"""
    assert is_guangdong({"province": "广东省", "city": "广州"}) is True


def test_non_guangdong_is_rejected():
    """非广东主体不应通过。"""
    assert is_guangdong(CompanyBasic(province="北京市", city="北京市")) is False


def test_empty_input_is_safe():
    """空值不应抛异常。"""
    assert is_guangdong(None) is False
    assert is_guangdong({}) is False


def test_all_prefecture_cities_covered():
    """广东 21 个地级市应全部覆盖。"""
    assert len(GUANGDONG_CITIES) == 21
    for city in ("广州", "深圳", "汕头", "云浮"):
        assert city in GUANGDONG_CITIES
