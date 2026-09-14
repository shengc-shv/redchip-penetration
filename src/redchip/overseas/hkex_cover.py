"""封面页权威注册地抽取（Multi-Files 分册 + pypdf）。

为什么需要这个模块
------------------
原 ``hkex_listing.extract_structure`` 扫整份 PDF 250 页，收集所有法域命中后按
「离岸 > 中国境内 > 香港」取优先级。该口径有**假阳性**：正文里偶发的一处
"the Cayman Islands" 提及（股东、子公司、法域描述）会压过封面页的真实注册地。

实测反例（2026-09-13）：id=108344「深圳小阔科技股份有限公司」封面明写
*"A joint stock company incorporated in the People's Republic of China with
limited liability"*（H 股），却被 ``extract_structure`` 判成「开曼群岛」。
→ 用整份正文的优先级推断注册地**不可靠**。

权威口径
--------
上市主体的注册地**只由封面页 / 招股章程扉页的固定句式**给出：

    (Incorporated in the Cayman Islands with limited liability)
    (A joint stock company incorporated in the People's Republic of China with limited liability)

其他章节的提及一律不作为注册地依据。

成本优化
--------
港交所申请版本几乎都带「Multi-Files」分册目录（清单 JSON 的 ``u2`` → 一个 htm），
可按章节取**小文件**：WARNING 封面分册 ~110KB / 1 页 / ~3s，
而整份申请版本 5-11MB / 单文件 30-60s → 便宜约一个数量级，且更准。
"""

from __future__ import annotations

import io
import re
from typing import Any

import httpx

from redchip import config as config_mod
from redchip.overseas.hkex_listing import DOC_BASE, _GD_CITIES_EN

# 封面页注册地句式。
# ⚠️ 关键：pypdf 抽取的文本会在**单词内部**插入空格（"Securiti es"、"People ’s"），
# 因此不能对原文直接写带 \s 的正则。这里统一先做「紧凑化」（去引号、去所有非字母、小写），
# 再用无空格的模式匹配："(A joint stock company incorporated in the People's Republic of
# China with limited liability)" → "incorporatedinthepeoplesrepublicofchinawithlimitedliability"。
_COVER_STRICT_RE = re.compile(
    r"(?:incorporated|established|registered|continued)"
    r"(?:asanexemptedcompany)?"
    r"(?:in|underthelawsof)"
    r"(?:the)?"
    r"(?P<jur>caymanislands|bermuda|peoplesrepublicofchina|republicofchina|prc|hongkong)"
    r"[^.]{0,40}?withlimitedliability"
)
# 放宽版：允许 "with limited liability" 缺失（少数文件用作废句）
_COVER_RELAXED_RE = re.compile(
    r"(?:incorporated|established|registered|continued)"
    r"(?:asanexemptedcompany)?"
    r"(?:in|underthelawsof)"
    r"(?:the)?"
    r"(?P<jur>caymanislands|bermuda|peoplesrepublicofchina|republicofchina|hongkong)"
)
_JUR_MAP = {
    "caymanislands": "开曼群岛",
    "bermuda": "百慕大",
    "peoplesrepublicofchina": "中国(境内)",
    "republicofchina": "中国(境内)",
    "prc": "中国(境内)",
    "hongkong": "香港",
}
_RELAXED_WINDOW = 800  # 放宽版只在前 800 个紧凑字符内采信（封面标题区），避免正文子公司提及误配

# 按优先级扫描的分册名（封面 → 扉页 → 目录/概要，命中即停）
_COVER_SECTIONS = ("WARNING", "IMPORTANT", "CONTENTS", "SUMMARY", "CORPORATE INFORMATION")
_MAX_SECTIONS = 3  # 最多下載的分册数（控制成本）
_MAX_PAGES_PER_SECTION = 4


def _norm(text: str) -> str:
    """归一化 PDF 文本：统一引号、压缩空白（用于落证据原文）。"""
    t = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", t)


def _compact(text: str) -> str:
    """紧凑化：去引号 → 只留字母 → 小写。

    用于消除 pypdf 在词内插空格（"People ’s" / "Securiti es"）造成的正则失配。
    """
    t = text.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^A-Za-z]", "", t).lower()


def _map_jurisdiction(jur: str) -> str:
    return _JUR_MAP.get((jur or "").strip().lower(), "")


def cover_domicile_from_text(text: str) -> tuple[str, str]:
    """从文本中抽封面页注册地句式（取**最靠前**的强命中）。

    Args:
        text: 页面文本（多页拼接亦可）。

    Returns:
        tuple[str, str]: (注册地中文标签 或 ""，命中证据原文)
    """
    compact = _compact(text)
    match = None
    strict = list(_COVER_STRICT_RE.finditer(compact))
    if strict:
        match = min(strict, key=lambda x: x.start())  # 封面句最先出现 → 取最靠前
    else:
        for m in _COVER_RELAXED_RE.finditer(compact):
            if m.start() > _RELAXED_WINDOW:
                break  # 超出封面标题区，视为正文提及，不采信
            match = m
            break
    if match is None:
        return "", ""
    label = _map_jurisdiction(match.group("jur"))
    return label, _readable_evidence(text, label)


def _readable_evidence(text: str, label: str) -> str:
    """在原文里定位**注册地句本身**，返回可读片段（供人工溯源）。

    仅按法域关键词截取会把免责声明里的 "Hong Kong Limited" 误当证据，
    故优先取同时含关键词与 "limited liability" 的窗口。
    """
    keywords = {
        "开曼群岛": ("Cayman Islands", "Cayman"),
        "百慕大": ("Bermuda",),
        "中国(境内)": ("Republic of China", "PRC"),
        "香港": ("Hong Kong",),
    }.get(label, ())
    norm = _norm(text)
    fallback = ""
    for kw in keywords:
        for m in re.finditer(re.escape(kw), norm):
            window = norm[max(0, m.start() - 120): m.start() + 160].strip()
            if "limited liability" in window.lower():
                return window
            if not fallback:
                fallback = window
    return fallback or label


def _pdf_text(data: bytes, max_pages: int = _MAX_PAGES_PER_SECTION) -> str:
    """从内存 PDF 字节抽前若干页文本（失败返回空串，不抛）。"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        out: list[str] = []
        for page in reader.pages[:max_pages]:
            try:
                out.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - 单页失败不中断
                continue
        return "\n".join(out)
    except Exception:  # noqa: BLE001 - 整个分册损坏则该分册无结论
        return ""


def fetch_multi_sections(multi_url: str, settings: config_mod.Settings | None = None) -> list[tuple[str, str]]:
    """抓取「Multi-Files」分册目录，返回 [(章节名, PDF 绝对链接)]（按目录顺序）。

    Args:
        multi_url: 清单 JSON 里的 ``u2``（相对路径，如 ``sehk/2026/108344/2026xxx.htm``）。
        settings: 全局配置。

    Returns:
        list[tuple[str, str]]: 章节名与绝对 PDF 链接；无目录返回空列表。
    """
    if not multi_url:
        return []
    settings = settings or config_mod.get_settings()
    url = DOC_BASE + multi_url.lstrip("/")
    try:
        with httpx.Client(timeout=max(settings.http_timeout, 60), follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": settings.user_agent})
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError:
        return []
    base_dir = url.rsplit("/", 1)[0] + "/"
    out: list[tuple[str, str]] = []
    for row in re.findall(r"<tr.*?</tr>", html, re.S | re.I):
        links = re.findall(r'href=["\']([^"\']+\.pdf)["\']', row)
        if not links:
            continue
        label = re.sub(r"<[^>]+>", " ", row)
        label = re.sub(r"[\s\u00a0►|]+", " ", label).strip().upper()
        out.append((label, base_dir + links[0].lstrip("/")))
    return out


def scan_cover(rec: Any, settings: config_mod.Settings | None = None) -> dict[str, Any]:
    """对一条申请人记录做**封面页权威注册地**抽取。

    路径：
    1. 有 Multi-Files 目录 → 按 ``_COVER_SECTIONS`` 优先级取前 ``_MAX_SECTIONS`` 个分册下載读首页；
    2. 无目录 → 回落整份申请版本 PDF 首页。

    Args:
        rec: ``ListingRecord``（需 ``multi_url`` / ``app_proof_url``）。
        settings: 全局配置。

    Returns:
        dict: {"domicile": 中文标签 或 "", "evidence": 命中原文, "section": 来源章节,
               "method": "cover-section" / "cover-full", "http_status": int}
    """
    settings = settings or config_mod.get_settings()
    timeout = max(settings.http_timeout, 90)
    sections = fetch_multi_sections(getattr(rec, "multi_url", ""), settings)
    # 按优先级挑分册：名字含 WARNING/IMPORTANT/SUMMARY 等，保持目录内的相对顺序
    picked: list[tuple[str, str]] = []
    for want in _COVER_SECTIONS:
        for label, link in sections:
            if want in label and (label, link) not in picked:
                picked.append((label, link))
                break
    if not picked:  # 目录缺失/结构异常 → 按顺序取前几个
        picked = sections[: _MAX_SECTIONS]

    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        for label, link in picked[:_MAX_SECTIONS]:
            try:
                r = c.get(link, headers={"User-Agent": settings.user_agent})
                if r.status_code != 200 or not r.content[:5] == b"%PDF-":
                    continue
            except httpx.HTTPError:
                continue
            dom, ev = cover_domicile_from_text(_pdf_text(r.content))
            if dom:
                return {
                    "domicile": dom, "evidence": ev, "section": label,
                    "method": "cover-section", "http_status": r.status_code,
                }
        # 回落：整份申请版本首页
        if getattr(rec, "app_proof_url", ""):
            try:
                r = c.get(rec.app_proof_url, headers={"User-Agent": settings.user_agent})
                dom, ev = cover_domicile_from_text(_pdf_text(r.content, max_pages=3))
                if dom:
                    return {
                        "domicile": dom, "evidence": ev, "section": "full-pdf",
                        "method": "cover-full", "http_status": r.status_code,
                    }
                return {
                    "domicile": "", "evidence": "", "section": "full-pdf",
                    "method": "cover-full", "http_status": r.status_code,
                }
            except httpx.HTTPError:
                pass
    return {"domicile": "", "evidence": "", "section": "", "method": "cover-section", "http_status": 0}


# ── 运营实体地域线索（把「离岸子集」落到广东）──────────────────────────────────
# 用途：离岸注册只说明是红筹形态；是否**广东**红筹取决于运营实体所在地。
# 做法：只取「概要 / 公司资料」等**小分册**，统计广东城市词频 + 留存原文片段。
# ⚠️ 定位：这是**线索**不是结论（单个城市名可能只出现在风险因素里），故结果标
# ``gd_opco_pending_human``，须人工复核后才可对外。
# 词库与名称匹配共用同一权威来源（`hkex_listing._GD_CITIES_EN`），避免两份清单漂移。
_GD_TOKENS = tuple(_GD_CITIES_EN)
# 分册名有「HISTORY, DEVELOPMENT AND ...」与「HISTORY, REORGANIZATION AND ...」两种写法，
# 故只匹配 "HISTORY" 前缀（曾因写死全名导致 REORGANIZATION 变体的历史沿革章从未被扫描）。
_OPCO_SECTIONS = ("SUMMARY", "CORPORATE INFORMATION", "HISTORY")
_MAX_OPCO_BYTES = 6_000_000  # 超过此体积的分册跳过（正文大部头不在本阶段用途内）
# 每分册的读取页数上限。**「历史沿革」章必须放宽**：该章的实质内容（重组步骤、主要子公司表）
# 位于百余页之后（实测 Exegenesis 的广州主体在第 114–133 页），10 页上限会把关键证据整段漏掉
# ——曾因此把「集团主要 PRC 运营实体在广州」误判为「广东连接仅为非法人团队」（2026-09-14 修正）。
# 该分册体积小（实测 0.5MB 级），放宽页数不会显著增加带宽；体积上限仍由 _MAX_OPCO_BYTES 兜底。
_OPCO_SECTION_PAGES = {"SUMMARY": 12, "CORPORATE INFORMATION": 12, "HISTORY": 60}


def scan_operating_region(
    rec: Any,
    settings: config_mod.Settings | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """从「概要 / 公司资料 / 历史沿革」分册中抽取运营实体地域线索（广东城市词）。

    Args:
        rec: ``ListingRecord``（需 ``multi_url``）。
        settings: 全局配置。
        max_pages: 覆盖所有分册的页数上限；``None`` 时按 ``_OPCO_SECTION_PAGES`` 分册取值。

    Returns:
        dict: {"gd_hits": 城市词→次数, "total": 总命中, "entity_total": 实体语境命中,
               "snippets": [原文片段], "section": 首次命中分册名, "section_hits": {分册名: 命中数}}
    """
    settings = settings or config_mod.get_settings()
    sections = fetch_multi_sections(getattr(rec, "multi_url", ""), settings)
    picked: list[tuple[str, str, int]] = []
    for want in _OPCO_SECTIONS:
        for label, link in sections:
            if want in label:
                picked.append((label, link, max_pages or _OPCO_SECTION_PAGES.get(want, 12)))
                break
    if not picked:
        return {"gd_hits": {}, "total": 0, "entity_total": 0, "snippets": [],
                "section": "", "section_hits": {}}

    hits: dict[str, int] = {}
    section_hits: dict[str, int] = {}
    snippets: list[str] = []
    entity_total = 0
    used = ""
    with httpx.Client(timeout=max(settings.http_timeout, 90), follow_redirects=True) as c:
        for label, link, pages in picked:
            try:
                r = c.get(link, headers={"User-Agent": settings.user_agent})
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or not r.content[:5] == b"%PDF-":
                continue
            if len(r.content) > _MAX_OPCO_BYTES:
                continue
            text = _norm(_pdf_text(r.content, max_pages=pages))
            low = text.lower()
            local = 0
            for tok in _GD_TOKENS:
                n = low.count(tok)
                if n:
                    hits[tok] = hits.get(tok, 0) + n
                    local += n
                    if len(snippets) < 3:
                        i = low.find(tok)
                        snippets.append(text[max(0, i - 90): i + 110].strip())
            entity_total += _entity_context_hits(text)
            if local:
                section_hits[label] = section_hits.get(label, 0) + local
                if not used:
                    used = label
    return {"gd_hits": hits, "total": sum(hits.values()), "entity_total": entity_total,
            "snippets": snippets, "section": used, "section_hits": section_hits}


_GD_TOKEN_RE = re.compile("|".join(_GD_TOKENS), re.I)
_SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")
# 集团实体语境：句子在讲「我们的子公司 / 我们设立的运营实体」。
# 用途：把「历史沿革」章里**真正指向集团自身运营实体**的地域线索，与噪音（股东 / 投资方 /
# 中介机构地址，如"Guangzhou Huiqin Consulting Co., Ltd."）分开。
# 实测价值：Exegenesis 的「广州嘉因 = 集团主要 PRC 运营实体」写在沿革章第 114–133 页，
# 若只按分册名分层会被误降为「补充层（噪音）」；按本判据可正确归入核心层。
_GROUP_ENTITY_RE = re.compile(
    r"our\s+(?:indirect\s+|wholly[- ]owned\s+|principal\s+|PRC\s+)*(?:subsidiar|operating\s+entit|group|business)"
    r"|\bsubsidiar(?:y|ies)\b|\bwholly[- ]owned\b|\bWFOE\b|wholly\s+foreign[- ]owned"
    r"|\boperating\s+entit|\bprincipal\s+PRC\b|\bour\s+Group\b"
    r"|\bwe\s+(?:established|conduct|operate|carry|continue)",
    re.I,
)
# ⚠️ 勿加裸词 ``\bGroup\b``：会命中第三方名称（实测"Guangzhou Finance Holding **Group** Co., Ltd."
# 被误判为集团自述）→ 只用 ``our Group`` 这种所属表达。


def _entity_context_hits(text: str) -> int:
    """统计落在「集团实体语境」句子内的广东城市词次数。

    Args:
        text: 已归一化的分册文本。

    Returns:
        int: 命中次数。
    """
    total = 0
    for sent in _SENT_SPLIT.split(text):
        if not _GROUP_ENTITY_RE.search(sent):
            continue
        low = sent.lower()
        total += sum(low.count(tok) for tok in _GD_TOKENS)
    return total


def gd_evidence_snippets(
    rec: Any,
    settings: config_mod.Settings | None = None,
    max_pages: int = 12,
    max_snippets: int = 6,
) -> list[dict[str, str]]:
    """抽取「运营实体在广东」的**原文句子**证据（供人工复核的卡片）。

    只在实体名/地址类句子里取（含 "Co., Ltd." / "Limited" / "registered" / "headquarter"
    等上下文），避免把风险因素里的泛泛提及当证据。

    Args:
        rec: ``ListingRecord``（需 ``multi_url``）。
        settings: 全局配置。
        max_pages: 每个分册最多读的页数。
        max_snippets: 最多返回句子数。

    Returns:
        list[dict]: [{"section": 分册名, "sentence": 原句}]
    """
    settings = settings or config_mod.get_settings()
    sections = fetch_multi_sections(getattr(rec, "multi_url", ""), settings)
    picked: list[tuple[str, str, int]] = []
    for want in _OPCO_SECTIONS:
        for label, link in sections:
            if want in label:
                picked.append((label, link, max(_OPCO_SECTION_PAGES.get(want, 12), max_pages)))
                break
    if not picked:
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    ctx_re = re.compile(
        r"\bCo\.,?\s*Ltd|\bLimited\b|registered|headquarter|principal place|"
        r"our\s+(?:PRC\s+)?(?:operating\s+)?subsidiar|established|facilit|"
        r"District|Street|Road\b|\bPRC\b|Branch|manufactur|propert|plant|office",
        re.I,
    )
    with httpx.Client(timeout=max(settings.http_timeout, 90), follow_redirects=True) as c:
        for label, link, pages in picked:
            if len(out) >= max_snippets:
                break
            try:
                r = c.get(link, headers={"User-Agent": settings.user_agent})
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or not r.content[:5] == b"%PDF-":
                continue
            if len(r.content) > _MAX_OPCO_BYTES:
                continue
            text = _norm(_pdf_text(r.content, max_pages=pages))
            for sent in _SENT_SPLIT.split(text):
                if len(out) >= max_snippets:
                    break
                sent = sent.strip()
                if not (40 <= len(sent) <= 420):
                    continue
                if not _GD_TOKEN_RE.search(sent):
                    continue
                if not ctx_re.search(sent):
                    continue
                key = sent[:80]
                if key in seen:
                    continue
                seen.add(key)
                out.append({"section": label, "sentence": sent})
    return out


# ── VIE 判定（协议控制结构）────────────────────────────────────────────────────
# 旧口径用裸词 `contractual arrangements` 判 VIE → **明显过宽**（普通商务合同也用该词），
# 实测把一地 H 股（深圳承泰/天赐高新/德赛西威…）误标 VIE。
# 新口径只认招股书里的**专有表述**：
#   强信号：variable interest entity / VIE(s) / 协议控制 / VIE 架构
#   弱信号：contractual arrangements **且** 同段落出现 WFOE / nominee / consolidate / 协议控制
# ⚠️ VIE 信号必须**区分大小写**：招股书一律用大写 `VIE`。
# 若开 re.I，`\bVIEs?\b` 会命中英文动词 "vie"（"players vie for market share"）与
# pypdf 断词残留（"vie w"、"Vie tnam"）——实测造成 7 条假阳性
# （贝尔家居 / 臻驱科技 / 优地机器人 / 彤程新材 / 爱德泰 / 伊戈尔 / 科郦）。
_VIE_STRONG = re.compile(r"variable\s+interest\s+entit|协议控制|\bVIEs?\b|VIE\s*架构|VIE\s*structure")
_VIE_CONTRACT = re.compile(r"contractual\s+arrangements?", re.I)
_VIE_SUPPORT = re.compile(r"\bWFOE\b|\bVIEs?\b|(?i:nominee|consolidat)|协议控制")
# 终止语：**只认结构性终止**（终止协议 / 明文终止某套安排 / 停止并表），
# 不认普通「合同可被终止」条款（否则会把在用的 VIE 误判为已终止）。
_VIE_TERMINATE = re.compile(
    r"terminat\w*\s+agreement"
    r"|terminat\w*\s+of\s+the"
    r"|(?:was|were|is|are|has\s+been|have\s+been)\s+terminat\w*"
    r"|to\s+terminate\s+the"
    r"|ceased?\s+to\s+(?:be\s+)?consolidat"
    r"|de-?consolidat"
    r"|unwind\w*"      # 招股书常用「unwind the VIE structure / unwinding of the Arrangements」表拆除
    r"|unwound",
    re.I,
)
# 历史语：出现在 VIE 提及之前的「历史」字样（招股书常用 Historical Contractual Arrangements）
_VIE_HISTORICAL = re.compile(r"histor(?:ical|ically)", re.I)
# 「Historical Contractual Arrangements」是港交所招股书对**已终止/已拆除** VIE 结构的固定定义术语
# （实测 Exegenesis：受当时外资负面清单限制曾以协议控制经营 CGT 业务，2025-11 完成拆除）。
# 即使周边没有 WFOE/VIE 等支撑词，也应识别为「曾存在但已终止」，避免漏记历史安排。
_VIE_HIST_DEFINED = re.compile(r"historical\s+contractual\s+arrangements?", re.I)
_VIE_SUPPORT_WINDOW = re.compile(
    r"\bWFOE\b|\bVIEs?\b|(?i:nominee|consolidat)|协议控制|variable\s+interest\s+entit"
)
_VIE_SECTIONS = ("SUMMARY", "RISK FACTORS", "HISTORY")  # 同上：兼容 REORGANIZATION 变体
_MAX_VIE_BYTES = 2_000_000  # 只读小分册（概要/风险/沿革，合计 ~1MB）；BUSINESS 4-9MB 不读


def _window_score(snip: str) -> int:
    """证据窗口质量分：越具体越靠前（避免取到目录行或泛泛提及）。"""
    if "variable interest" in snip.lower():
        return 4
    if re.search(r"\bVIEs?\b", snip):
        return 3
    if re.search(r"contractual\s+arrangements?", snip, re.I):
        return 2
    return 1


def _historical_after(norm: str, pos: int, lookahead: int = 140) -> bool:
    """匹配点**之后同一句内**是否出现「历史」限定词。

    招股书常见写法：「… through Hangzhou Jiayin, **our VIE**, based on the **Historical**
    Contractual Arrangements」——限定词在术语**之后**，只看前置窗口会漏判。
    只看到句号为止，避免误伤「…through the VIE Agreements. Historically we also…」这类
    下一句才提历史的在用结构。
    """
    seg = norm[pos:pos + lookahead]
    cut = seg.find(".")
    if cut >= 0:
        seg = seg[:cut]
    return bool(_VIE_HISTORICAL.search(seg))


def _dead_window(norm: str, pos: int) -> bool:
    """判断某处 VIE 提及是否属「已终止 / 历史」语境。"""
    near = norm[max(0, pos - 260): pos + 260]
    pre = norm[max(0, pos - 90): pos]
    return bool(_VIE_TERMINATE.search(near)
                or _VIE_HISTORICAL.search(pre)
                or _historical_after(norm, pos))


def _vie_windows(norm: str) -> list[tuple[bool, str]]:
    """返回 [(是否已终止, 证据片段)]：先扫强信号，再扫带支撑的弱信号，最后补历史定义术语。"""
    out: list[tuple[bool, str]] = []
    seen_pos: set[int] = set()
    for m in _VIE_STRONG.finditer(norm):
        out.append((_dead_window(norm, m.start()), norm[max(0, m.start() - 110): m.start() + 150].strip()))
        seen_pos.add(m.start())
    for m in _VIE_CONTRACT.finditer(norm):
        if m.start() in seen_pos:
            continue
        w = norm[max(0, m.start() - 400): m.start() + 400]
        if not _VIE_SUPPORT_WINDOW.search(w):
            continue  # 普通商务合同，无 VIE 语境支撑 → 不算 VIE 提及
        out.append((_dead_window(norm, m.start()), norm[max(0, m.start() - 110): m.start() + 150].strip()))
        seen_pos.add(m.start())
    # 历史定义术语：即使无支撑词也记为「已终止」
    for m in _VIE_HIST_DEFINED.finditer(norm):
        out.append((True, norm[max(0, m.start() - 110): m.start() + 150].strip()))
    return out


def vie_from_text(text: str) -> dict[str, Any]:
    """在给定文本上做 VIE 判定（供分册法与 listedco 整份法共用）。

    ⚠️ 边界：``is_vie`` 只描述**控制方式**（协议控制 vs 直接持股），
    **不得**参与地域判定（广东视图成员只由 ``gd_l1`` / ``gd_opco`` 决定）。

    判据（从严 + 终止感知 + 大小写敏感）：
    - 强信号：``variable interest entity`` / 大写 ``VIE``/``VIEs`` / ``协议控制`` / ``VIE 架构``；
      **必须区分大小写**——否则会命中英文动词 "vie" 与断词残留（"vie w" / "Vie tnam"）。
    - 弱信号：``contractual arrangements`` 前后 ±400 字符内同现 ``WFOE`` / 大写 ``VIE`` /
      ``nominee`` / ``consolidat``；
    - **终止感知**：命中窗口若含结构性终止语或前置「Historical」→ 归入 terminated，
      **不计入** ``is_vie``（实测 Aqara 的 VIE 于 2022 / 2026-03 已终止）。

    Args:
        text: 待判文本。

    Returns:
        dict: {"is_vie", "evidence", "active", "terminated", "hits"}
    """
    norm = _norm(text)
    windows = _vie_windows(norm)
    active_ws = [s for dead, s in windows if not dead]
    term_ws = [s for dead, s in windows if dead]
    active, terminated = len(active_ws), len(term_ws)
    # 证据优选：取质量分最高的窗口（variable interest entity > VIE > contractual arrangements）
    evidence = max(active_ws, key=_window_score) if active_ws else (max(term_ws, key=_window_score) if term_ws else "")
    hits = {
        "strong": len(_VIE_STRONG.findall(norm)),
        "contract": len(_VIE_CONTRACT.findall(norm)),
        "support": len(_VIE_SUPPORT.findall(norm)),
        "active": active,
        "terminated": terminated,
    }
    return {"is_vie": active > 0, "evidence": evidence, "active": active,
            "terminated": terminated, "hits": hits}


def scan_vie(rec: Any, settings: config_mod.Settings | None = None, max_pages: int = 12) -> dict[str, Any]:
    """VIE（协议控制）判定：只读小分册，取原文证据片段。

    **分册权威性规则**（关键，防「已终止安排」误判）
    -------------------------------------------------
    港交所招股书若上市主体**当前**存在 VIE，必然在「概要（SUMMARY）」与「风险因素（RISK FACTORS）」
    两章强制披露；只在「历史沿革（HISTORY）」章出现的 VIE 表述，通常是**已终止的历史安排**叙事块
    （实测 Aqara：7 处提及全在同一段 *Historical Contractual Arrangements* 内，深圳绿米 2022 终止、
    深圳安卡萨 2026-03 终止，公司当前并无 VIE）。

    故：``is_vie = 概要或风险因素章存在非终止的 VIE 表述``；
    仅历史沿革章出现的记为 ``history_only``（不回填 is_vie，供人工参考）。

    Args:
        rec: ``ListingRecord``（需 ``multi_url``）。
        settings: 全局配置。
        max_pages: 每个分册最多读的页数。

    Returns:
        dict: {"is_vie", "evidence", "source", "hits", "active", "terminated", "history_only"}
    """
    settings = settings or config_mod.get_settings()
    sections = fetch_multi_sections(getattr(rec, "multi_url", ""), settings)
    picked: list[tuple[str, str]] = []
    for want in _VIE_SECTIONS:
        for label, link in sections:
            if want in label:
                picked.append((label, link))
                break
    if not picked:
        return {"is_vie": False, "evidence": "", "source": "", "hits": {}, "active": 0,
                "terminated": 0, "history_only": 0}

    agg = {"strong": 0, "contract": 0, "support": 0, "active": 0, "terminated": 0}
    evidence = ""
    source = ""
    active_auth = 0
    hist_only = 0
    hist_label = ""
    hist_evidence = ""
    term_evidence = ""
    with httpx.Client(timeout=max(settings.http_timeout, 90), follow_redirects=True) as c:
        for label, link in picked:
            try:
                r = c.get(link, headers={"User-Agent": settings.user_agent})
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or not r.content[:5] == b"%PDF-":
                continue
            if len(r.content) > _MAX_VIE_BYTES:
                continue
            res = vie_from_text(_pdf_text(r.content, max_pages=max_pages))
            for k in agg:
                agg[k] += res["hits"].get(k, 0)
            if res["active"]:
                if any(k in label for k in ("SUMMARY", "RISK FACTORS")):
                    active_auth += res["active"]
                    if not evidence:
                        evidence, source = res["evidence"], label
                else:
                    hist_only += res["active"]
                    if not hist_evidence:
                        hist_evidence, hist_label = res["evidence"], label
            elif res["terminated"] and not term_evidence:
                term_evidence = res["evidence"]  # 全部窗口均为已终止 → 也留住原文
    is_vie = active_auth > 0
    if not is_vie and hist_only:
        source = f"{hist_label[:26]}(仅历史沿革章·疑已终止)"
    return {
        "is_vie": is_vie,
        "evidence": evidence if is_vie else (hist_evidence or term_evidence),
        "source": source,
        "hits": agg,
        "active": active_auth,
        "terminated": agg["terminated"],
        "history_only": hist_only,
    }
