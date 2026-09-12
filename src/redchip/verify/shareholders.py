"""从披露文件中召回「股东 / 权益」章节并提取持股数据。

为什么能核 UBO
--------------
港股年报的「主要股东权益」章节是《证券及期货条例》第XV部申报数据的**法定镜像**，
披露易文档流里的 Next Day Disclosure Return 也是同一数据源的逐笔申报表。
因此无需直连 DI 系统（其旧接口已 302 弃用、新页面对部分网络环境不可用），
用已有的 FTS 抓取链路即可拿到同一权威口径的数据。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from redchip.overseas.fts import FtsHit
from redchip.overseas.hkex import PageText

# 股东权益章节的关键词（与架构关键词并列使用，独立召回）
SHAREHOLDER_KEYWORDS: tuple[str, ...] = (
    "主要股东权益",
    "主要股東權益",
    "须申报的权益",
    "須申報的權益",
    "substantial shareholders",
    "disclosable interests",
    "股东名册",
    "股東名冊",
    "大股东权益",
)

# 「名称 + 持股百分比」抽取：
#   覆盖「马化腾先生 54.29%」「马化腾先生（54.29%）」「Ma Huateng: 54.29%」等写法。
#   名称取百分比前最近的 2~12 个中文/英文字符；过滤「合计/total」等汇总词。
_HOLDER_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z·]{2,12})(?:先生|女士|小姐)?\s*[(（:：]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*%"
)
_SUMMARY_WORDS = ("合计", "總計", "总计", "total", "小计", "约", "其余")
# 称谓后缀：name 组是贪婪汉字匹配，会把「先生/女士」一并吞掉，提取后统一剥离
_HONORIFIC_SUFFIX_RE = re.compile(r"(先生|女士|小姐|Mr\.?|Ms\.?|Mrs\.?)$")


class DisclosedHolder(BaseModel):
    """一条从披露文件中提取的股东持股记录。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    share_pct: float = Field(default=0.0, description="披露口径持股比例（百分数）")
    share_raw: str = ""
    source_page: str = ""


def recall_shareholder_pages(pages: list[PageText]) -> list[PageText]:
    """从分页文本中召回含股东权益章节的页。

    Args:
        pages: PDF 分页文本。

    Returns:
        list[PageText]: 命中页。
    """
    hits: list[PageText] = []
    for page in pages:
        blob = page.text.lower()
        if any(kw.lower() in blob for kw in SHAREHOLDER_KEYWORDS):
            hits.append(page)
    return hits


def extract_disclosed_holders(text: str, source_page: str = "") -> list[DisclosedHolder]:
    """从文本中抽取「名称 + 持股百分比」记录。

    Args:
        text: 披露文本（可以是单页或 FTS 命中拼接）。
        source_page: 源页码标记。

    Returns:
        list[DisclosedHolder]: 去重后的持股记录。
    """
    holders: list[DisclosedHolder] = []
    seen: set[tuple[str, float]] = set()
    for match in _HOLDER_RE.finditer(text or ""):
        raw_name = match.group(1).strip()
        pct_text = match.group(2)
        # 汇总行（合计/total）与其余、约数表述不是具体股东
        if any(w in raw_name.lower() for w in _SUMMARY_WORDS):
            continue
        name = re.sub(r"[，。；、\s]+$", "", raw_name)
        name = _HONORIFIC_SUFFIX_RE.sub("", name)
        if len(name) < 2:
            continue
        try:
            pct = float(pct_text)
        except ValueError:
            continue
        if not (0 < pct <= 100):
            continue
        key = (name, pct)
        if key in seen:
            continue
        seen.add(key)
        holders.append(DisclosedHolder(name=name, share_pct=pct, share_raw=f"{pct:g}%", source_page=source_page))
    return holders


def extract_from_hits(hits: Iterable[FtsHit]) -> list[DisclosedHolder]:
    """从 FTS 命中结果中批量提取。

    Args:
        hits: 命中列表（snippet 含页码）。

    Returns:
        list[DisclosedHolder]: 提取结果。
    """
    out: list[DisclosedHolder] = []
    for hit in hits:
        out.extend(extract_disclosed_holders(hit.snippet, source_page=str(hit.page_no)))
    return out


def sum_check(holders: Iterable[DisclosedHolder], tolerance: float = 0.5) -> float:
    """登记股东持股比例合计校验。

    Args:
        holders: 股东记录（同一实体的登记股东）。
        tolerance: 合计与 100% 的容差（百分数点）。

    Returns:
        float: 合计值；调用方据此判断是否接近 100。
    """
    return round(sum(h.share_pct for h in holders), 4)


def is_complete_sum(total: float, tolerance: float = 0.5) -> bool:
    """判断合计是否在容差内等于 100%。

    Args:
        total: 合计值。
        tolerance: 容差。

    Returns:
        bool: 完整返回 True。
    """
    return abs(total - 100.0) <= tolerance
