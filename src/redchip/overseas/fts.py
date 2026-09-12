"""SQLite FTS5 索引与关键词检索。

为什么不用全文塞给 LLM
----------------------
一份港股年报/招股书 300~800 页，全文动辄 50 万 token。这里只把命中「架构 / 重组 /
合约安排 / VIE / WFOE」等关键词的段落喂给 LLM，控制在 6k token 预算内。

中文子串检索的关键
------------------
FTS5 默认 unicode61 分词器对中文按「连续非空格串」切词，无法命中子串；
这里使用 ``trigram`` 分词器（SQLite ≥ 3.34，实测 3.50.4 可用），可命中任意 3 字以上子串。
不足 3 字的关键词（如「架构」「VIE」中英混排场景下的短词）回退到 LIKE 扫描。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from redchip import config as config_mod
from redchip.overseas.hkex import PageText

# 架构类关键词。命中越多，该页越可能是「公司架构 / 合约安排」章节。
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "合约安排",
    "合同安排",
    "结构性合约",
    "结构性合同",
    "可变利益实体",
    "VIE",
    "WFOE",
    "外商独资企业",
    "股权架构",
    "公司架构",
    "集团架构",
    "重组",
    "独家购买权",
    "股权质押",
    "投票权委托",
    "独家服务协议",
    "contractual arrangements",
    "variable interest",
    "structured contracts",
    "reorganisation",
    "reorganization",
    "corporate structure",
    "最终受益人",
    "实际控制人",
    # 美股 20-F（英文披露）常用表述
    "Organizational Structure",
    "representative VIE",
    "Enhanced VIE Structure",
    "PRC subsidiaries",
    "primary beneficiary",
    "variable interest entities",
    "consolidated affiliated entities",
    "nominee shareholder",
    "contractual arrangements with",
    # VIE 名单 / 实体清单所在段落通常密集出现公司全称
    "Co., Ltd.",
    "designated individuals",
    "equity interest holders",
)

# VIE 专属关键词：命中数为 0 时可跳过协议提取环节，节省约 2k token
VIE_KEYWORDS: tuple[str, ...] = (
    "合约安排",
    "合同安排",
    "结构性合约",
    "可变利益实体",
    "VIE",
    "contractual arrangements",
    "structured contracts",
)


# 架构与股东证据的信号词：命中即加分，确保这些块优先进入 token 预算
# （20-F 的 Risk Factors 章节同样高频出现 VIE，但不含架构主体信息）
BOOST_KEYWORDS: tuple[str, ...] = (
    "Organizational Structure",
    "corporate structure",
    "representative VIE",
    "Enhanced VIE Structure",
    "Major Shareholders",
    "主要股东权益",
    "contractual arrangements with",
)


class FtsHit(BaseModel):
    """一条检索命中。"""

    model_config = ConfigDict(extra="ignore")

    page_no: int
    score: int = 0
    keywords: list[str] = Field(default_factory=list)
    snippet: str = ""


def build_index(pages: list[PageText], db_path: Path) -> sqlite3.Connection:
    """建立（或复用）FTS 索引并写入分页文本。

    Args:
        pages: PDF 分页文本。
        db_path: SQLite 文件路径。

    Returns:
        sqlite3.Connection: 已建索引的连接。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS plain(no INTEGER PRIMARY KEY, body TEXT)")
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(body, tokenize='trigram')")
        conn.execute("DELETE FROM pages")
    except sqlite3.OperationalError:
        # 极旧版本 SQLite 不支持 trigram，退化为纯 LIKE 检索
        pass
    conn.execute("DELETE FROM plain")

    rows = [(p.page_no, p.text) for p in pages]
    conn.executemany("INSERT INTO plain(no, body) VALUES(?, ?)", rows)
    try:
        conn.executemany("INSERT INTO pages(rowid, body) VALUES(?, ?)", rows)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def _fts_pages(conn: sqlite3.Connection, keyword: str) -> set[int]:
    """用 FTS5 trigram 检索单个关键词（要求长度 ≥ 3）。

    Args:
        conn: 已建索引的连接。
        keyword: 关键词。

    Returns:
        set[int]: 命中的页码集合。
    """
    if len(keyword) < 3:
        return set()
    try:
        rows = conn.execute(
            "SELECT rowid FROM pages WHERE body MATCH ?", (f'"{keyword}"',)
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(r[0]) for r in rows}


def _like_pages(conn: sqlite3.Connection, keyword: str) -> set[int]:
    """用 LIKE 检索（FTS 不可用或关键词过短时的兜底）。

    Args:
        conn: 已建索引的连接。
        keyword: 关键词。

    Returns:
        set[int]: 命中的页码集合。
    """
    rows = conn.execute("SELECT no FROM plain WHERE body LIKE ?", (f"%{keyword}%",)).fetchall()
    return {int(r[0]) for r in rows}


def search(
    conn: sqlite3.Connection,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    max_tokens: int | None = None,
    snippet_chars: int = 900,
) -> list[FtsHit]:
    """按关键词检索命中页，按命中词数量降序返回。

    Args:
        conn: 已建索引的连接。
        keywords: 检索词。
        max_tokens: 命中文本总 token 预算；缺省取配置中的 ``fts_max_tokens``。
        snippet_chars: 每页摘录的最大字符数。

    Returns:
        list[FtsHit]: 命中列表（已按 token 预算裁剪）。
    """
    settings = config_mod.get_settings()
    budget = max_tokens if max_tokens is not None else settings.fts_max_tokens

    page_keywords: dict[int, list[str]] = {}
    for kw in keywords:
        hits = _fts_pages(conn, kw) or _like_pages(conn, kw)
        for page_no in hits:
            page_keywords.setdefault(page_no, []).append(kw)

    bodies = {int(r[0]): r[1] for r in conn.execute("SELECT no, body FROM plain").fetchall()}

    def _score(page_no: int, kws: list[str]) -> int:
        base = len(kws)
        body = bodies.get(page_no, "")
        boost = sum(3 for b in BOOST_KEYWORDS if b.lower() in body.lower())
        return base + boost

    hits = [
        FtsHit(
            page_no=page_no,
            score=_score(page_no, kws),
            keywords=sorted(set(kws)),
            snippet=_make_snippet(bodies.get(page_no, ""), kws[0], snippet_chars),
        )
        for page_no, kws in page_keywords.items()
    ]
    hits.sort(key=lambda h: (-h.score, h.page_no))

    # 按 token 预算裁剪：命中数过多时只保留高分页
    trimmed: list[FtsHit] = []
    used = 0
    for hit in hits:
        cost = config_mod.token_len(hit.snippet)
        if used + cost > budget:
            continue
        used += cost
        trimmed.append(hit)
    return trimmed or hits[:3]


def _make_snippet(body: str, keyword: str, max_chars: int) -> str:
    """截取围绕首个关键词的上下文片段。

    Args:
        body: 整页文本。
        keyword: 关键词。
        max_chars: 片段最大长度。

    Returns:
        str: 截取的片段。
    """
    if not body:
        return ""
    pos = body.find(keyword)
    start = max(0, pos - max_chars // 3) if pos >= 0 else 0
    return body[start : start + max_chars].strip()


def has_vie_evidence(conn: sqlite3.Connection) -> bool:
    """判断是否存在 VIE 协议控制的证据，用于决定是否跳过协议提取。

    Args:
        conn: 已建索引的连接。

    Returns:
        bool: 命中 VIE 关键词返回 True。
    """
    return bool(search(conn, keywords=VIE_KEYWORDS, max_tokens=200))


def render_hits(hits: list[FtsHit]) -> str:
    """把命中结果渲染成带页码标记的上下文，供 Prompt 使用。

    Args:
        hits: 命中列表。

    Returns:
        str: 拼接后的文本，每段以 ``[p.页码]`` 开头，便于 LLM 溯源。
    """
    return "\n\n".join(f"[p.{h.page_no}]（命中：{'/'.join(h.keywords)}）\n{h.snippet}" for h in hits)
