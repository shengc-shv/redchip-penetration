"""美股披露文件抓取层（SEC EDGAR 官方 REST API）。

为什么不用 edgartools
---------------------
方案文档原定 ``pip install edgartools``。实测在本环境安装不稳定（pip 进程被中断），
且该库对 20-F 的解析并不比官方 API 更省事。这里直接对接 SEC 官方 REST：

1. ``SEC/files/company_tickers.json``    ticker → CIK（BABA → 1577552）
2. ``data.sec.gov/submissions/CIK##########.json``  该公司的全部申报列表
3. ``www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dash>/<doc>``  20-F 原文（HTML）

已在本地实测：Alibaba Group Holding Ltd，CIK 0001577552，12 份 20-F，
最新一份 2026-05-20（11.7MB HTML → 纯文本约 132 万字符），
关键词命中：Contractual Arrangements 7 / variable interest 57 / WFOE 22 / Major Shareholders 6。

硬性要求
--------
SEC 要求所有请求在 ``User-Agent`` 携带**应用名与联系邮箱**（配置 ``SEC_IDENTITY``），
否则会被 403。速率上限约 10 请求/秒。
"""

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from redchip import config as config_mod
from redchip.overseas.hkex import PageText

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
TICKERS_URL = f"{SEC_WWW}/files/company_tickers.json"

# 20-F 是 HTML 而非 PDF，按字符数切块以模拟「分页」，便于 FTS 定位与溯源
CHUNK_CHARS = 2500
MAX_CHUNKS = 900


class UsFiling(BaseModel):
    """一条 SEC 申报记录。"""

    model_config = ConfigDict(extra="ignore")

    cik: str
    ticker: str = ""
    company: str = ""
    form: str = "20-F"
    accession: str = ""
    filing_date: str = ""
    primary_document: str = ""

    @property
    def url(self) -> str:
        """返回申报原文地址。"""
        acc = self.accession.replace("-", "")
        cik = self.cik.lstrip("0") or self.cik
        return f"{SEC_WWW}/Archives/edgar/data/{cik}/{acc}/{self.primary_document}"


def _identity(settings: config_mod.Settings) -> str:
    """构造 SEC 要求的 User-Agent。

    Args:
        settings: 全局配置。

    Returns:
        str: ``"应用名 邮箱"`` 形式的身份串。
    """
    return settings.sec_identity or "redchip-penetration you@example.com"


def _client(settings: config_mod.Settings) -> httpx.Client:
    """构造带 SEC 身份头的 HTTP 客户端。

    Args:
        settings: 全局配置。

    Returns:
        httpx.Client: 客户端实例。
    """
    return httpx.Client(
        timeout=max(settings.http_timeout, 120),
        follow_redirects=True,
        headers={
            "User-Agent": _identity(settings),
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/html, */*",
        },
    )


def resolve_cik(ticker: str, settings: config_mod.Settings | None = None) -> tuple[str, str]:
    """ticker → (CIK, 公司名)。

    Args:
        ticker: 股票代码，如 ``BABA``。
        settings: 全局配置；缺省自动载入。

    Returns:
        tuple[str, str]: 10 位补零 CIK 与公司名。

    Raises:
        ValueError: 未在 SEC 的 ticker 表中找到该代码。
    """
    cfg = settings or config_mod.get_settings()
    wanted = ticker.strip().upper()
    with _client(cfg) as client:
        resp = client.get(TICKERS_URL)
        resp.raise_for_status()
        data = resp.json()
    for item in data.values():
        if str(item.get("ticker", "")).upper() == wanted:
            return str(item["cik_str"]).zfill(10), str(item.get("title", ""))
    raise ValueError(f"SEC ticker 表中未找到 {ticker}")


def fetch_submissions(cik: str, settings: config_mod.Settings | None = None) -> dict:
    """拉取公司的全部申报列表。

    Args:
        cik: 10 位 CIK。
        settings: 全局配置。

    Returns:
        dict: submissions JSON（含 filings.recent 与 filings.files）。
    """
    cfg = settings or config_mod.get_settings()
    with _client(cfg) as client:
        resp = client.get(f"{SEC_DATA}/submissions/CIK{cik}.json")
        resp.raise_for_status()
        return dict(resp.json())


def pick_filing(
    submissions: dict,
    cik: str,
    ticker: str = "",
    company: str = "",
    form: str = "20-F",
) -> UsFiling | None:
    """从申报列表中挑出最新一份指定表单。

    Args:
        submissions: submissions JSON。
        cik: CIK。
        ticker: 股票代码（写入结果）。
        company: 公司名（写入结果）。
        form: 目标表单类型。

    Returns:
        UsFiling | None: 命中的最新申报；无命中返回 None。
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    latest: UsFiling | None = None
    for i, current in enumerate(forms):
        if current != form:
            continue
        filing = UsFiling(
            cik=cik,
            ticker=ticker,
            company=company,
            form=current,
            accession=accessions[i],
            filing_date=dates[i],
            primary_document=docs[i],
        )
        if latest is None or filing.filing_date > latest.filing_date:
            latest = filing
    return latest


def download(filing: UsFiling, dest_dir: Path, settings: config_mod.Settings | None = None) -> Path:
    """下载申报原文（已存在则复用）。

    Args:
        filing: 申报记录。
        dest_dir: 保存目录。
        settings: 全局配置。

    Returns:
        Path: 本地文件路径。

    Raises:
        httpx.HTTPError: 下载失败（多为 User-Agent 不符合 SEC 要求）。
    """
    cfg = settings or config_mod.get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filing.accession or "filing")
    path = dest_dir / f"{filing.ticker or filing.cik}_{filing.form}_{safe}.htm"
    if path.exists() and path.stat().st_size > 0:
        return path
    with _client(cfg) as client, client.stream("GET", filing.url) as resp:
        resp.raise_for_status()
        with path.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    return path


def extract_html_pages(
    path: Path, chunk_chars: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS
) -> list[PageText]:
    """把 20-F 的 HTML 转为分块文本。

    20-F 动辄十几 MB，直接整篇塞进索引会让 FTS 命中定位过粗，
    因此按固定字符数切块，块序号即「伪页码」，用于溯源（如 p.37）。

    Args:
        path: 本地 HTML 文件。
        chunk_chars: 每块字符数。
        max_chunks: 最大块数，防止超大文件打爆内存。

    Returns:
        list[PageText]: 分块文本。
    """
    raw = path.read_bytes().decode("utf-8", errors="ignore")
    # 先剔除脚本与样式，再把标签折叠为空格（保留段落间的换行）
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)

    chunks: list[PageText] = []
    for idx, start in enumerate(range(0, len(text), chunk_chars), start=1):
        if idx > max_chunks:
            break
        block = text[start : start + chunk_chars].strip()
        if len(block) > 200:  # 过短的尾块不入库
            chunks.append(PageText(page_no=idx, text=block))
    return chunks


def fetch_us_document(
    ticker: str,
    data_dir: Path,
    form: str = "20-F",
    settings: config_mod.Settings | None = None,
) -> tuple[UsFiling, Path, list[PageText]]:
    """端到端：ticker → CIK → 20-F → 本地文件 → 分块文本。

    Args:
        ticker: 美股代码。
        data_dir: 缓存目录。
        form: 目标表单类型。
        settings: 全局配置。

    Returns:
        tuple[UsFiling, Path, list[PageText]]: 申报记录、文件路径、分块文本。

    Raises:
        FileNotFoundError: 未找到该类型的申报。
    """
    cfg = settings or config_mod.get_settings()
    cik, company = resolve_cik(ticker, cfg)
    submissions = fetch_submissions(cik, cfg)
    filing = pick_filing(submissions, cik, ticker=ticker, company=company, form=form)
    if filing is None:
        raise FileNotFoundError(f"{ticker}: SEC 未检索到 {form}")
    path = download(filing, data_dir / "html", cfg)
    return filing, path, extract_html_pages(path)
