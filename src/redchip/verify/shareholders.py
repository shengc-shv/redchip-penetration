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
from pathlib import Path

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
    # 美股 20-F（英文披露）
    "Major Shareholders",
    "beneficially owned",
    "share ownership",
    "Principal Shareholders",
    # 港股英文年报：第XV部权益披露章节的英文表述
    "Substantial Shareholders",
    "Interests of Substantial",
    "Part XV",
    "beneficial ownership",
    "interests or short positions",
)

# 「名称 + 持股百分比」抽取：
#   覆盖「马化腾先生 54.29%」「马化腾先生（54.29%）」「Ma Huateng: 54.29%」等写法。
#   名称取百分比前最近的 2~12 个中文/英文字符；过滤「合计/total」等汇总词。
_HOLDER_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z·]{2,12})(?:先生|女士|小姐)?\s*[(（:：]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*%"
)
_SUMMARY_WORDS = ("合计", "總計", "总计", "total", "小计", "约", "其余")
# 英文虚词与常见动词：英文披露中「and 1%」「was 8.8%」会被中文抽取器误判为股东名
_EN_STOPWORDS = frozenset(
    ["and", "or", "the", "of", "in", "to", "for", "as", "by", "with", "was", "were", "are", "is", "been", "being", "representing", "representing", "accordingly", "approximately", "respectively", "including", "excluded", "less", "more", "than", "about", "over", "under", "each", "such", "other", "which", "that", "this", "these", "those", "from", "at", "on", "our", "its", "their", "his", "her", "not", "no", "all", "any", "per", "due", "may", "will", "would", "could", "should", "own", "owns", "owned",
    "holding", "holds", "held", "represents",
]
)
# 称谓后缀：name 组是贪婪汉字匹配，会把「先生/女士」一并吞掉，提取后统一剥离
_HONORIFIC_SUFFIX_RE = re.compile(r"(先生|女士|小姐|Mr\.?|Ms\.?|Mrs\.?)$")

# ---------- 英文股东表（港股英文年报 / 美股 20-F 共用）----------
# 表格行式：「名称 + 股份数（含千分位）+ 持股百分比」
# 例：Naspers Limited  2,548,xxx,xxx  24.09%
_EN_TABLE_ROW_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.,'\- ]{2,80})\s+[^%\n]{0,80}?[\d,]{3,}"
    r"\s+(\d{1,3}(?:\.\d{1,2})?)\s*%"
)
# 叙述式：「名称 + beneficially owns / beneficial owner … 百分比」
# 例：Ma Huateng beneficially owns approximately 8.63% of our issued shares
_EN_NARRATIVE_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.,'\- ]{2,60}?)\s+(?:beneficially\s+owns?|beneficial\s+owner)"
    r"[^.]{0,160}?(\d{1,3}(?:\.\d{1,2})?)\s*%"
)
# 股东表的 Capacity 列会紧随名称出现（Beneficial owner / Interest of controlled corporation …），
# 需要截断，否则「Naspers Limited Interest of controlled corporation」会被当成股东名
_EN_CAPACITY_RE = re.compile(
    r"\s+(?:interest\s+of|beneficial\s+owner|investment\s+manager|held\s+by|founder\s+of|"
    r"beneficiary\s+of|trustee|corporation|company|other|long\s+position|short\s+position|"
    r"corporate|beneficial|interest|nature\s+of|note)\b.*$",
    re.IGNORECASE,
)
# 表格标题与页眉噪声词
_EN_NOISE = (
    "table of contents",
    "notes",
    "note",
    "total",
    "subtotal",
    "see",
    "item",
    "part",
    "page",
    "the company",
    "our company",
)


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
        lowered = raw_name.lower()
        if any(w in lowered for w in _SUMMARY_WORDS):
            continue
        # 纯英文虚词（and/was/representing…）不是股东名
        if lowered in _EN_STOPWORDS:
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


def extract_holders(text: str, source_page: str = "") -> list[DisclosedHolder]:
    """中英双通道抽取披露文件中的股东持股记录。

    - 中文通道：年报「主要股东权益」等章节（简体/繁体年报）
    - 英文通道：港股**英文年报**与美股 20-F 的股东表格（Name / shares / %）

    之所以必须支持英文：港股披露易同时提供繁体中文版与英文版，
    而中文版 PDF 用 pypdf 提取会出现 CID 编码乱码，英文版提取完整，
    因此英文披露是港股与美股共同的可靠输入。

    Args:
        text: 披露文本。
        source_page: 源页码标记。

    Returns:
        list[DisclosedHolder]: 去重后的持股记录。
    """
    holders = extract_disclosed_holders(text, source_page=source_page)
    holders.extend(extract_en_holders(text, source_page=source_page))
    return _dedupe_holders(holders)


def _soft_unwrap(text: str, min_line: int = 80) -> str:
    """合并 PDF 提取产生的软换行。

    英文表格常被拆成多个短行（名称一行、capacity 一行、数字一行），
    不合并则「名称 + 股份数 + 百分比」无法在同一行内匹配。

    Args:
        text: 原始文本。
        min_line: 小于此长度的行视为续行。

    Returns:
        str: 合并后的文本。
    """
    out: list[str] = []
    buf = ""
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            if buf:
                out.append(buf)
            buf = ""
            continue
        if buf and len(line) < min_line:
            buf = f"{buf} {line}"
        else:
            if buf:
                out.append(buf)
            buf = line
    if buf:
        out.append(buf)
    return "\n".join(out)


def extract_narrative_holders(text: str, source_page: str = "") -> list[DisclosedHolder]:
    """只抽取「叙述式」表述（中文表 + 英文 beneficially owns 句式）。

    用在美股场景：20-F 的股东表已由 HTML 表格通道结构化解析，
    若再对同一份文档跑文本表格行正则，只会引入
    「Eddie Yongming WU 19,706,418」这类把数字列当名字的噪声。

    Args:
        text: 披露文本。
        source_page: 源页码标记。

    Returns:
        list[DisclosedHolder]: 抽取结果。
    """
    holders = extract_disclosed_holders(text, source_page=source_page)
    holders.extend(
        extract_en_holders(text, source_page=source_page, modes=("narrative",))
    )
    return _dedupe_holders(holders)


def extract_en_holders(
    text: str,
    source_page: str = "",
    modes: tuple[str, ...] = ("table", "narrative"),
) -> list[DisclosedHolder]:
    """从英文披露中抽取「名称 + 持股百分比」。

    覆盖两种写法：
    1. 表格行（``modes`` 含 ``table``）：``Naspers Limited  2,548,xxx,xxx  24.09%``
    2. 叙述句（``modes`` 含 ``narrative``）：``Ma Huateng beneficially owns approximately 8.63% ...``

    Args:
        text: 英文披露文本。
        source_page: 源页码标记。
        modes: 启用的抽取模式。

    Returns:
        list[DisclosedHolder]: 抽取结果。
    """
    out: list[DisclosedHolder] = []
    prepared = _soft_unwrap(text)
    patterns: list[re.Pattern[str]] = []
    if "table" in modes:
        patterns.append(_EN_TABLE_ROW_RE)
    if "narrative" in modes:
        patterns.append(_EN_NARRATIVE_RE)
    for regex in patterns:
        for match in regex.finditer(prepared):
            raw_name = match.group(1).strip(" ,.;:-")
            raw_name = _EN_CAPACITY_RE.sub("", raw_name).strip(" ,.;:-")
            pct_text = match.group(2)
            lowered = raw_name.lower()
            if lowered in _EN_STOPWORDS:
                continue
            if any(noise in lowered for noise in _EN_NOISE):
                continue
            if len(raw_name) < 3 or not any(ch.isalpha() for ch in raw_name):
                continue
            try:
                pct = float(pct_text)
            except ValueError:
                continue
            if not (0 < pct <= 100):
                continue
            out.append(
                DisclosedHolder(
                    name=raw_name, share_pct=pct, share_raw=f"{pct:g}%", source_page=source_page
                )
            )
    return _dedupe_holders(out)


def holders_from_html_file(html_path: str | Path, source: str = "HTML表格") -> list[DisclosedHolder]:
    """从 HTML 披露原文的 <table> 中抽取股东持股（SEC 20-F / 6-K）。

    这是美股侧的主通道：20-F 本身就是 HTML，直接解析表格结构比在文本流上做
    正则匹配准确得多（列序、空列、capacity 列都不再是问题）。

    Args:
        html_path: 本地 HTML 文件路径。
        source: 来源标记（写入 source_page 便于溯源）。

    Returns:
        list[DisclosedHolder]: 持股记录；文件不存在或解析失败返回空列表。
    """
    path = Path(html_path)
    if not path.exists():
        return []
    try:
        from redchip.overseas.htmltable import extract_holders_from_html

        raw = path.read_bytes().decode("utf-8", errors="ignore")
        pairs = extract_holders_from_html(raw)
    except Exception:  # noqa: BLE001 - 表格解析失败不应影响主流程
        return []
    return [
        DisclosedHolder(name=name, share_pct=pct, share_raw=f"{pct:g}%", source_page=source)
        for name, pct in pairs
    ]


def _dedupe_holders(holders: list[DisclosedHolder]) -> list[DisclosedHolder]:
    """按「名称 + 比例」去重并保持顺序。

    Args:
        holders: 原始记录。

    Returns:
        list[DisclosedHolder]: 去重后的记录。
    """
    seen: set[tuple[str, float]] = set()
    uniq: list[DisclosedHolder] = []
    for h in holders:
        key = (h.name, h.share_pct)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq


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
