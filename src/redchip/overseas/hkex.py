"""港股披露文件抓取层（HKEXnews）。

背景
----
方案文档原定使用 ``ah-disclosure-kit``，实测该包**在 PyPI 上不存在**（pypi.org/simple 返回 404），
因此改为自研实现，直接对接 HKEXnews 官方接口：

1. ``/search/prefix.do``            股票代码 → stockId（如 00700 → 7609）
2. ``/search/titleSearchServlet.do`` 按 stockId 检索披露文件清单（JSON）
3. ``/listedco/listconews/...``     下载 PDF 原文
4. ``pypdf``                        分页抽取文本，交给 FTS 建索引

已在本地实测通过（00700 → stockId 7609，检索接口返回 PDF 清单）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from redchip import config as config_mod

CST = config_mod.CST


HKEX_BASE = "https://www1.hkexnews.hk"
PREFIX_URL = f"{HKEX_BASE}/search/prefix.do"
SEARCH_URL = f"{HKEX_BASE}/search/titleSearchServlet.do"

DocKind = Literal["prospectus", "annual_report", "interim_report", "any"]

# 标题关键字匹配（不区分大小写）。招股书对红筹架构描述最完整，因此优先级最高。
_DOC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "prospectus": ("prospectus", "招股章程", "招股書", "global offering", "首次公开发售"),
    "annual_report": ("annual report", "年報", "年报", "annualreport"),
    "interim_report": ("interim report", "中期報告", "中期报告"),
}


class Filing(BaseModel):
    """一条披露文件记录。"""

    model_config = ConfigDict(extra="ignore")

    news_id: str
    stock_code: str
    stock_name: str = ""
    title: str
    short_text: str = ""
    file_link: str
    file_type: str = "PDF"
    published_at: str = Field(default="", description="YYYY-MM-DD，由 DD/MM/YYYY 归一化")
    date_time_raw: str = ""

    @property
    def url(self) -> str:
        """返回 PDF 绝对地址。"""
        if self.file_link.startswith("http"):
            return self.file_link
        return f"{HKEX_BASE}{self.file_link}"


class PageText(BaseModel):
    """PDF 单页文本。"""

    model_config = ConfigDict(extra="ignore")

    page_no: int
    text: str


def _clean(value: str | None) -> str:
    """去除 HKEX 返回值里混入的 HTML 片段。

    Args:
        value: 原始字段值。

    Returns:
        str: 清洗后的纯文本。
    """
    if not value:
        return ""
    # STOCK_CODE 可能形如 "00700<br/>80700"，只取首个代码
    text = re.sub(r"<br\s*/?>", " ", value)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&#x2f;", "/").replace("&quot;", '"')
    return " ".join(text.split()).strip()


def _normalize_date(raw: str) -> str:
    """把 ``DD/MM/YYYY HH:MM`` 归一化为 ``YYYY-MM-DD``。

    Args:
        raw: HKEX 的 DATE_TIME 字段。

    Returns:
        str: 归一化日期；解析失败返回空串（不回落为抓取当天，避免时间造假）。
    """
    try:
        # 仅做日期字符串归一化，输出为纯日期，不涉及时区换算
        dt = datetime.strptime(raw.split(" ")[0], "%d/%m/%Y")  # noqa: DTZ007
        return dt.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return ""


class HkexClient:
    """HKEXnews 客户端。

    Args:
        settings: 全局配置；缺省时自动载入。
        transport: 仅供测试注入的 httpx 传输层。
    """

    def __init__(self, settings: config_mod.Settings | None = None, transport: Any = None) -> None:
        self.settings = settings or config_mod.get_settings()
        self._client = httpx.Client(
            timeout=self.settings.http_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json, text/javascript, */*",
                "Referer": f"{HKEX_BASE}/search/titlesearch.xhtml",
            },
            transport=transport,
        )

    def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        self._client.close()

    def __enter__(self) -> HkexClient:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    def resolve_stock_id(self, code: str) -> int:
        """股票代码 → HKEXnews 内部 stockId。

        Args:
            code: 港股代码，如 ``00700`` 或 ``700``。

        Returns:
            int: stockId。

        Raises:
            ValueError: 未检索到对应代码。
        """
        normalized = code.strip().upper().zfill(5)
        resp = self._client.get(
            PREFIX_URL,
            params={
                "callback": "callback",
                "lang": "EN",
                "type": "A",
                "name": normalized,
                "market": "SEHK",
            },
        )
        resp.raise_for_status()
        # 接口返回 JSONP：callback({...});
        body = resp.text.strip()
        match = re.search(r"\((\{.*\})\)\s*;?$", body, flags=re.DOTALL)
        payload = json.loads(match.group(1) if match else body)
        infos = payload.get("stockInfo") or []
        for info in infos:
            if str(info.get("code", "")).zfill(5) == normalized:
                return int(info["stockId"])
        raise ValueError(f"HKEXnews 未找到股票代码 {code} 对应的 stockId")

    def search_filings(
        self,
        stock_id: int,
        years_back: int = 3,
        row_range: int = 100,
        lang: str = "EN",
    ) -> list[Filing]:
        """检索指定 stockId 的披露文件清单。

        Args:
            stock_id: HKEXnews 内部 id。
            years_back: 回溯年数，用于限定 fromDate。
            row_range: 单次返回条数上限。
            lang: ``EN`` 或 ``ZH``。

        Returns:
            list[Filing]: 文件清单（按发布时间倒序）。
        """
        today = datetime.now(CST)
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": (today - timedelta(days=365 * years_back)).strftime("%Y%m%d"),
            "toDate": today.strftime("%Y%m%d"),
            "title": "",
            "searchType": "1",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": row_range,
            "lang": lang,
        }
        resp = self._client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        # result 字段本身是 JSON 字符串，需要二次解析
        outer = resp.json()
        raw = outer.get("result", "[]")
        rows = json.loads(raw) if isinstance(raw, str) else raw

        filings: list[Filing] = []
        for row in rows:
            code = _clean(row.get("STOCK_CODE")).split(" ")[0]
            filings.append(
                Filing(
                    news_id=str(row.get("NEWS_ID", "")),
                    stock_code=code,
                    stock_name=_clean(row.get("STOCK_NAME")),
                    title=_clean(row.get("TITLE")),
                    short_text=_clean(row.get("SHORT_TEXT") or row.get("LONG_TEXT")),
                    file_link=str(row.get("FILE_LINK", "")),
                    file_type=_clean(row.get("FILE_TYPE")) or "PDF",
                    published_at=_normalize_date(str(row.get("DATE_TIME", ""))),
                    date_time_raw=str(row.get("DATE_TIME", "")),
                )
            )
        return filings

    def pick_filing(self, filings: Iterable[Filing], kind: DocKind = "prospectus") -> Filing | None:
        """按文档类型挑出最新的一份。

        Args:
            filings: 候选文件清单。
            kind: 目标文档类型。

        Returns:
            Filing | None: 命中的最新文件；无命中返回 None。
        """
        keywords = _DOC_KEYWORDS.get(kind, ())
        hits: list[Filing] = []
        for f in filings:
            blob = f"{f.title} {f.short_text}".lower()
            if not keywords or any(k in blob for k in keywords):
                hits.append(f)
        if not hits:
            return None
        # 发布日期为空的排最后，避免把未知时间的文件当最新
        return max(hits, key=lambda x: (x.published_at, x.news_id))

    def download(self, filing: Filing, dest_dir: Path) -> Path:
        """下载 PDF 到本地（已存在则直接复用，避免重复消耗带宽）。

        Args:
            filing: 目标文件记录。
            dest_dir: 保存目录。

        Returns:
            Path: 本地 PDF 路径。

        Raises:
            httpx.HTTPError: 下载失败。
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filing.news_id or "filing")
        path = dest_dir / f"{filing.stock_code}_{safe}.pdf"
        if path.exists() and path.stat().st_size > 0:
            return path
        with self._client.stream("GET", filing.url) as resp:
            resp.raise_for_status()
            with path.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return path


def extract_pdf_pages(pdf_path: Path, max_pages: int = 400) -> list[PageText]:
    """抽取 PDF 每页文本。

    Args:
        pdf_path: 本地 PDF 路径。
        max_pages: 最多处理页数，防止超长年报打爆内存与后续 FTS。

    Returns:
        list[PageText]: 分页文本；无有效文本时返回空列表。
    """
    from pypdf import PdfReader  # 局部导入：仅在真正解析 PDF 时才付出导入成本

    reader = PdfReader(str(pdf_path))
    pages: list[PageText] = []
    for idx, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页解析失败不应中断整份文档
            text = ""
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            pages.append(PageText(page_no=idx, text=text))
    return pages


def fetch_hk_document(
    code: str,
    data_dir: Path,
    kinds: tuple[DocKind, ...] = ("prospectus", "annual_report"),
    years_back: int = 10,
) -> tuple[Filing, Path, list[PageText]]:
    """端到端抓取：代码 → 文件 → PDF → 分页文本。

    文档类型按 ``kinds`` 顺序依次尝试（默认招股书优先，缺失时回退年报）。

    Args:
        code: 港股代码。
        data_dir: PDF 缓存目录。
        kinds: 尝试的文档类型顺序。
        years_back: 检索回溯年数（招股书可能很早，默认放宽到 10 年）。

    Returns:
        tuple[Filing, Path, list[PageText]]: 命中的文件记录、PDF 路径、分页文本。

    Raises:
        FileNotFoundError: 所有候选类型都未检索到可用文件。
    """
    with HkexClient() as client:
        stock_id = client.resolve_stock_id(code)
        filings = client.search_filings(stock_id, years_back=years_back)
        for kind in kinds:
            filing = client.pick_filing(filings, kind)
            if filing is None:
                continue
            pdf_path = client.download(filing, data_dir / "pdf")
            return filing, pdf_path, extract_pdf_pages(pdf_path)
    raise FileNotFoundError(f"{code}: HKEXnews 未检索到 {kinds} 类型的披露文件")
