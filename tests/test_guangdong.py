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


# ── 词库完整性：广东省 21 个地级市必须全覆盖（中/英/简/繁）──────────────────────
# 背景（2026-09-13 审计）：原词库只有简体中文，而港交所中文名一律用**繁体**
# （如「廣州極飛」「東莞…」）→ 凡「繁体粤地名 + 不含地名的英文壳名」的企业会被漏判。

GD_21_CITIES: list[tuple[str, str, str]] = [
    ("广州", "廣州", "guangzhou"), ("深圳", "深圳", "shenzhen"),
    ("珠海", "珠海", "zhuhai"), ("汕头", "汕頭", "shantou"),
    ("佛山", "佛山", "foshan"), ("韶关", "韶關", "shaoguan"),
    ("湛江", "湛江", "zhanjiang"), ("肇庆", "肇慶", "zhaoqing"),
    ("江门", "江門", "jiangmen"), ("茂名", "茂名", "maoming"),
    ("惠州", "惠州", "huizhou"), ("梅州", "梅州", "meizhou"),
    ("汕尾", "汕尾", "shanwei"), ("河源", "河源", "heyuan"),
    ("阳江", "陽江", "yangjiang"), ("清远", "清遠", "qingyuan"),
    ("东莞", "東莞", "dongguan"), ("中山", "中山", "zhongshan"),
    ("潮州", "潮州", "chaozhou"), ("揭阳", "揭陽", "jieyang"),
    ("云浮", "雲浮", "yunfu"),
]


def test_gd_city_table_covers_all_21_prefecture_cities():
    """港交所词库必须覆盖 21 个地级市 × {简体, 繁体, 拼音}。"""
    from redchip.overseas.hkex_listing import _GD_CITIES_CN, _GD_CITIES_EN

    assert len(GD_21_CITIES) == 21, "广东省地级市应为 21 个"
    missing = [
        (s, t, p) for s, t, p in GD_21_CITIES
        if s not in _GD_CITIES_CN or t not in _GD_CITIES_CN or p not in _GD_CITIES_EN
    ]
    assert not missing, f"词库缺失（简/繁/拼音）：{missing}"


def test_gd_domestic_cities_covers_all_21_prefecture_cities():
    """境内属地词库（工商口径，简体）必须覆盖 21 个地级市。"""
    from redchip.domestic.penetration import GUANGDONG_CITIES

    missing = [s for s, _, _ in GD_21_CITIES if s not in GUANGDONG_CITIES]
    assert not missing, f"境内词库缺失：{missing}"


def test_traditional_city_name_matches_when_english_is_a_shell():
    """繁体粤地名 + 英文壳名 也必须命中（原缺口）。"""
    from redchip.overseas.hkex_listing import ListingRecord, guangdong_l1

    rec = ListingRecord(id=1, name_cn="廣州某某科技股份有限公司", name_en="Congyu Holdings Limited")
    hit, word = guangdong_l1(rec)
    assert hit and word == "廣州"


def test_pinyin_and_simplified_still_match():
    from redchip.overseas.hkex_listing import ListingRecord, guangdong_l1

    assert guangdong_l1(ListingRecord(id=1, name_en="Dongguan Example Ltd"))[0] is True
    assert guangdong_l1(ListingRecord(id=1, name_cn="云浮市某某公司"))[0] is True
    assert guangdong_l1(ListingRecord(id=1, name_en="Zhejiang Example Ltd"))[0] is False
