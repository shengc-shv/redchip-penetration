"""境内工商数据源：协议定义与共享工具。

设计目的
--------
境内工商数据可来自多家服务（CNBizAPI / 企查查 / 启信宝），各家在鉴权方式、接口形式、
计价模型上都不同。本包把「取数」与「穿透逻辑」解耦：

- :class:`CompanyDataSource` 约定三个最小能力（模糊搜索 / 基本信息 / 股东）
- 每个来源一个模块，新增数据源只需实现该协议并在 :func:`build_source` 登记
- 上层 ``redchip.domestic.cnbiz.CnbizClient`` 保持原类名与签名不变，内部委托当前数据源

统一标识问题
------------
真实接口普遍按**企业名称**查询（如 ``?q=腾讯科技（深圳）有限公司``），而穿透层以
**统一社会信用代码**为主键在图上行走。为免改动调用链，:class:`IdentifierIndex`
维护名称与代码的双向映射，由搜索与基本信息接口回填。
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from redchip.models.schema import CandidateCompany

# 统一社会信用代码：18 位，数字与大写字母（不含 I O S V Z）
USCC_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{18}$")


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


@runtime_checkable
class CompanyDataSource(Protocol):
    """境内工商数据源协议。"""

    name: str

    def search_company(self, name: str, limit: int = 5) -> list[CandidateCompany]:
        """按名称模糊搜索企业。"""
        ...

    def get_company_basic(self, identifier: str) -> CompanyBasic:
        """按统一社会信用代码或企业名称查询基本信息。"""
        ...

    def get_shareholders(self, identifier: str) -> list[Shareholder]:
        """按统一社会信用代码或企业名称查询股东。"""
        ...


class IdentifierIndex:
    """统一社会信用代码与企业名称的双向缓存。

    穿透层始终以信用代码调用，而多数接口按名称查询；本类负责两边的翻译，
    记录来源是各接口返回的候选与企业信息（零额外请求）。
    """

    def __init__(self) -> None:
        self._name_by_code: dict[str, str] = {}
        self._code_by_name: dict[str, str] = {}

    def remember(self, name: str, credit_code: str) -> None:
        """登记一条名称↔代码对应关系。

        Args:
            name: 企业名称。
            credit_code: 统一社会信用代码。
        """
        if not name or not credit_code:
            return
        self._name_by_code[credit_code] = name
        self._code_by_name[name] = credit_code

    def resolve(self, identifier: str) -> tuple[str, str]:
        """把标识解析为「名称 + 代码」二元组。

        调用方可能传名称也可能传代码，这里统一归一化：
        代码能查到名称就用名称（真实接口按名称检索更稳），查不到则原样返回。

        Args:
            identifier: 企业名称或统一社会信用代码。

        Returns:
            tuple[str, str]: ``(查询用名称, 信用代码)``，缺失项为空串。
        """
        value = (identifier or "").strip()
        if not value:
            return "", ""
        if looks_like_uscc(value):
            return self._name_by_code.get(value, value), value
        return value, self._code_by_name.get(value, "")

    def name_of(self, identifier: str) -> str:
        """返回可用于查询的名称。

        Args:
            identifier: 企业名称或统一社会信用代码。

        Returns:
            str: 名称（查不到时原样返回入参）。
        """
        return self.resolve(identifier)[0]


def looks_like_uscc(value: str) -> bool:
    """判断字符串是否为统一社会信用代码格式。

    Args:
        value: 待判断字符串。

    Returns:
        bool: 形如 18 位代码返回 True。
    """
    return bool(USCC_RE.match((value or "").strip().upper()))


def unwrap(payload: Any) -> list[dict[str, Any]]:
    """把响应的常见包装结构统一成列表。

    Args:
        payload: 接口返回的 JSON。

    Returns:
        list[dict[str, Any]]: 记录列表。
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if not payload:
            return []
        for key in ("data", "result", "results", "items", "list", "records"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                return unwrap(value)
        return [payload]
    return []


def to_float(value: Any) -> float:
    """把持股比例的多种写法统一成浮点百分数。

    Args:
        value: 原始值，可能是 ``"60.5%"`` / ``0.605`` / ``"60.5"``。

    Returns:
        float: 百分数形式的持股比例。
    """
    if value in (None, ""):
        return 0.0
    try:
        num = float(str(value).strip().rstrip("%"))
    except ValueError:
        return 0.0
    # 小于 1 且非 0 时视为小数形式，换算为百分数
    return num * 100 if 0 < num <= 1 else num


def to_kind(value: Any, name: str = "") -> str:
    """判断股东是自然人还是公司。

    Args:
        value: 接口给出的类型字段。
        name: 股东名称（类型字段缺失时按名称兜底判断）。

    Returns:
        str: ``"person"`` 或 ``"company"``。
    """
    text = str(value or "").lower()
    if any(k in text for k in ("person", "natural", "自然人", "个人")):
        return "person"
    if any(k in text for k in ("company", "enterprise", "法人", "企业", "公司")):
        return "company"
    # 兜底：名称含公司类后缀视为法人，否则视为自然人
    if any(k in name for k in ("公司", "企业", "合伙", "中心", "集团", "厂", "店")):
        return "company"
    if 2 <= len(name) <= 4 and all("\u4e00" <= ch <= "\u9fa5" for ch in name):
        return "person"
    return "company"


# 字段名容错表：同一语义在不同服务里可能是 camelCase、snake_case 或拼音缩写
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "companyName", "company_name", "entName", "corpName", "Name"),
    "credit_code": (
        "credit_code", "creditCode", "unifiedCode", "uscc", "code", "regNo", "CreditCode", "KeyNo",
    ),
    "business_scope": ("business_scope", "businessScope", "scope", "operatingScope", "Scope"),
    "province": ("province", "provName", "provinceName", "Province"),
    "city": ("city", "cityName", "areaName", "City"),
    "reg_status": ("reg_status", "regStatus", "status", "regState", "Status"),
    "established_at": (
        "established_at", "estiblishTime", "startDate", "establishedDate", "StartDate",
    ),
    "legal_person": (
        "legal_person", "legalPerson", "legalPersonName", "operName", "frName", "OperName",
    ),
    "share_pct": (
        "share_pct", "sharePct", "percent", "ratio", "stockPercent", "fundedRatio", "StockPercent",
    ),
    "type": ("type", "shareholderType", "holderType", "investorType", "entityType", "Type"),
    "share_raw": ("share_raw", "shareRaw", "percentText", "stockPercentText", "PercentText"),
}


def pick_field(row: dict[str, Any], field: str, default: Any = "") -> Any:
    """按别名表从响应行里取值。

    各服务商的字段命名差异很大（``creditCode`` / ``uscc`` / ``KeyNo`` 都是信用代码），
    统一走别名表可以让上层只用一套语义字段名。

    Args:
        row: 接口返回的单条记录。
        field: 语义字段名（见 ``FIELD_ALIASES``）。
        default: 全部别名都缺失时的返回值。

    Returns:
        Any: 命中的值。
    """
    for key in FIELD_ALIASES.get(field, (field,)):
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def fuzzy(keyword: str, name: str) -> bool:
    """极简中文模糊匹配：关键词的任意 2 字子串出现在名称中即命中。

    仅用于离线样例检索，真实数据源一律走服务端的搜索引擎。

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
