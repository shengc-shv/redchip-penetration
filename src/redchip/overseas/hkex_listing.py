"""港交所新上市申请版本官方清单抓取（全量 + 增量）。

权威数据源
----------
港交所披露易「新上市申請版本及相關資料」后台为**静态 JSON**（非 JS 接口、无需鉴权），
覆盖四个状态 × 两个板块，英文/中文各一份：

    https://www1.hkexnews.hk/ncms/json/eds/{文件名}.json

文件名 = 状态前缀 + 板块 + 语言：

    appactive_app     在审（仅申请版本）
    appactive_appphip 在审（含 PHIP）
    appinactive       没有进展（失效 / 撤回 / 被拒绝）
    applisted         已上市
    appreturned       被发回
    + _sehk（主板） / _gem（GEM）
    + _e（英文） / _c（中文）

记录字段（每条申请人唯一 id）：
    id            申请人唯一编号
    d             递交日，DD/MM/YYYY
    a             申请人英文名（a_c 为中文名，来自 _c 文件）
    s             状态子码
    ls            文档列表，含「Application Proof (1st submission)」PDF 链接
    postingDate   展示用登载日期

本模块职责
----------
- ``fetch_index``：拉全状态 × 板块 JSON，按 id 去重，归一化为 ``ListingRecord``
- ``filter_year``：按递交日年份过滤（"2026 以来递表"）
- ``diff``：与本地状态比对，检测新增 / 状态变更（每日增量监测核心）
- ``guangdong_l1``：名称启发式标记广东候选（含开曼壳名 + 粤运营实体的正典红筹会漏，故仅作下界）
- ``extract_structure``：复用 ``hkex.extract_pdf_pages`` 抽申请版本 PDF，判注册地 / 广东运营实体 / VIE（L2 深判）
- ``persist`` / ``load_state``：本地状态落盘（``data/listing_state.json``）

数据真实性边界
--------------
- 官方源本身有地板：「以非公开方式递表的申请人，不须刊发申请版本」→ 秘密递表不在清单内。
- 清单只给名称 / 日期 / 保荐人，**不含红筹 / 广东结构**；结构判定必须下載申请版本 PDF 抽字段（L2）。
- 名称匹配（L1）会漏掉「开曼壳名 + 广东运营实体」的正典红筹；L2 以 PDF 内运营实体所在地为准。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from redchip import config as config_mod

CST = config_mod.CST

LISTING_JSON_BASE = "https://www1.hkexnews.hk/ncms/json/eds/"
# 申请版本 PDF 基址：披露易文档路径自带日期（如 sehk/2026/108324/documents/sehk26032301807.pdf
# 中 2026=年、260323=YYMMDD 文档日），可作为本项目清单按日期归档的锚点。
DOC_BASE = "https://www1.hkexnews.hk/app/"

# 状态前缀 → (中文标签, 原始状态键)
STATUS_FILES: dict[str, tuple[str, str]] = {
    "appactive_app": ("处理中", "active"),
    "appactive_appphip": ("处理中(含PHIP)", "active"),
    "appinactive": ("失效/撤回/被拒绝", "inactive"),
    "applisted": ("已上市", "listed"),
    "appreturned": ("被发回", "returned"),
}
BOARD_MAP: dict[str, str] = {"sehk": "主板", "gem": "GEM"}

# ── 广东地名词库（单一权威表，CN/EN 词库均由本表派生，避免漂移）─────────────────
# 覆盖广东省 **21 个地级市**（简体 + **繁体** + 拼音三写法）＋ 省名 ＋ 顺德。
# ⚠️ 繁体必须收录：港交所中文名一律用繁体（如「廣州極飛」「東莞…」），
#    只收简体时，凡「繁体粤地名 + 不含地名的英文壳名」的企业都会被漏判。
# ⚠️ 区级/开发区地名（南山/前海/横琴/南沙/松山湖…）**刻意不收录**：与他省同名
#    （南山—烟台/三亚、黄埔—上海、白云—机场）或过泛，会引入假阳性；需要时按需加白名单。
_GD_CITY_TABLE: list[tuple[str, str, str]] = [
    ("广州", "廣州", "guangzhou"),
    ("深圳", "深圳", "shenzhen"),
    ("珠海", "珠海", "zhuhai"),
    ("汕头", "汕頭", "shantou"),
    ("佛山", "佛山", "foshan"),
    ("韶关", "韶關", "shaoguan"),
    ("湛江", "湛江", "zhanjiang"),
    ("肇庆", "肇慶", "zhaoqing"),
    ("江门", "江門", "jiangmen"),
    ("茂名", "茂名", "maoming"),
    ("惠州", "惠州", "huizhou"),
    ("梅州", "梅州", "meizhou"),
    ("汕尾", "汕尾", "shanwei"),
    ("河源", "河源", "heyuan"),
    ("阳江", "陽江", "yangjiang"),
    ("清远", "清遠", "qingyuan"),
    ("东莞", "東莞", "dongguan"),
    ("中山", "中山", "zhongshan"),
    ("潮州", "潮州", "chaozhou"),
    ("揭阳", "揭陽", "jieyang"),
    ("云浮", "雲浮", "yunfu"),
    ("顺德", "順德", "shunde"),  # 区级（佛山），但常直接用于企业名，故收录
]
_GD_REGION_TABLE: list[tuple[str, str, str]] = [("广东", "廣東", "guangdong")]

_GD_CITIES_CN = [x for zh_s, zh_t, _ in _GD_CITY_TABLE + _GD_REGION_TABLE for x in (zh_s, zh_t)]
_GD_CITIES_EN = [py for _, _, py in _GD_CITY_TABLE + _GD_REGION_TABLE]
_GD_PATTERN = re.compile(
    "|".join(re.escape(w) for w in _GD_CITIES_CN + _GD_CITIES_EN), re.IGNORECASE
)

# 离岸注册地信号：招股书/申请版本对注册地的表述多变，需覆盖多种动词与连词
# （incorporated / established / registered / continued；in / under the laws of）+ 多法域。
# 中文 PDF 经 pypdf 抽取常成 CID 乱码，故中文模式仅作兜底。
_DOMICILE_PATTERNS = [
    (re.compile(r"(?:incorporated|established|registered|continued)\s+(?:as\s+an\s+exempted\s+company\s+)?(?:in|under\s+(?:the\s+)?laws?\s+of)\s+(?:the\s+)?cayman(\s+islands)?", re.I), "开曼群岛"),
    (re.compile(r"(?:incorporated|established|registered|continued)\s+(?:as\s+an\s+exempted\s+company\s+)?(?:in|under\s+(?:the\s+)?laws?\s+of)\s+(?:the\s+)?bermuda", re.I), "百慕大"),
    (re.compile(r"(?:incorporated|established|registered|continued)\s+(?:in|under\s+(?:the\s+)?laws?\s+of)\s+(?:the\s+)?(?:people'?s\s+republic\s+of\s+china|prc)", re.I), "中国(境内)"),
    (re.compile(r"(?:incorporated|established|registered|continued)\s+(?:in|under\s+(?:the\s+)?laws?\s+of)\s+(?:the\s+)?hong\s+kong", re.I), "香港"),
    (re.compile(r"於(?:開曼群島|百慕大|中華人民共和國|香港).{0,6}(?:註冊|成立)", re.I), "中文注册地"),
]
# 法域优先级：离岸（开曼/百慕大）> 中国境内 > 香港。
# 红筹判定只看是否存在离岸控股层（开曼/百慕大）；PRC 优先于 HK 可避免把「PRC 母公司 + 香港子公司」
# 误判为香港注册（这类实为 H 股）。仅在既无离岸层也无 PRC 时才落到香港（真·香港注册）。
_DOMICILE_PRIORITY = ["开曼群岛", "百慕大", "中国(境内)", "香港", "中文注册地"]
_VIE_PATTERN = re.compile(r"variable interest entity|\bVIE\b|contractual arrangements|合同安排", re.I)

# ── 轻量注册地扫描（HTTP Range + 流解压）────────────────────────────────────────
# 招股书/申请版本封面页固定陈述上市主体注册地，且该页位于 PDF 前部（Range 2MB 内可覆盖）。
# PDF 正文以 FlateDecode 压缩存储，且 Tj/TJ 会按字距拆分单词 → 先解压全部流，
# 再**只保留字母**（消除空格/换行/字距断点）后做子串匹配，避免 "Cayman Islands" 被拆成 "Cayman"+"Islands"。
# 实测：2MB 分段下载 ~5s（全文件 30-60s），比整份下载便宜 ~10 倍。
_SCAN_SUFFIX = "withlimitedliability"  # 封面页注册地句式的固定后缀（容错匹配用）
_SCAN_OFFSHORE_PATS: list[tuple[bytes, str]] = [
    (b"incorporatedinthecaymanislands", "开曼群岛"),
    (b"registeredinthecaymanislands", "开曼群岛"),
    (b"establishedinthecaymanislands", "开曼群岛"),
    (b"continuedinthecaymanislands", "开曼群岛"),
    (b"incorporatedinthebermuda", "百慕大"),
    (b"registeredinthebermuda", "百慕大"),
    (b"establishedinthebermuda", "百慕大"),
    (b"continuedinthebermuda", "百慕大"),
]
_SCAN_PRC_PATS: list[bytes] = [
    b"incorporatedinthepeoplesrepublicofchina",
    b"registeredinthepeoplesrepublicofchina",
    b"establishedinthepeoplesrepublicofchina",
    b"continuedinthepeoplesrepublicofchina",
    b"incorporatedintherepublicofchina",
]
_SCAN_HK_PATS: list[bytes] = [
    b"incorporatedinhongkong",
    b"registeredinhongkong",
    b"establishedinhongkong",
]


def _letters_of_chunk(raw: bytes) -> bytes:
    """把 PDF 分段的 FlateDecode 流全部解压，仅保留字母并转小写。

    保留字母可消除 Tj/TJ 字距断点（"Cayman"+"Islands" → "caymanislands"），
    使封面页注册地句式可以被稳定的子串匹配（而非依赖空格）。

    Args:
        raw: HTTP Range 分段下载的原始字节。

    Returns:
        bytes: 归一化后的字母串（小写）。
    """
    import zlib

    parts: list[bytes] = []
    for m in re.finditer(rb"stream\r?\n", raw):
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue  # 分段截断导致的残流，跳过
        try:
            parts.append(zlib.decompress(raw[start:end]))
        except zlib.error:
            continue  # 非 Flate 编码（图像等），跳过
        if len(parts) > 8000:
            break
    return re.sub(rb"[^A-Za-z]", b"", b"".join(parts)).lower()


def scan_domicile_range(
    url: str,
    nbytes: int = 2_000_000,
    settings: config_mod.Settings | None = None,
) -> dict[str, Any]:
    """用 HTTP Range 分段下載申请人 PDF 前段，仅抽注册地（不做整份下载）。

    比 ``download_app_proof`` + ``extract_structure`` 便宜约一个数量级，
    用于**全量摸底**阶段给每一条申请人廉价打 ``is_offshore`` 标签。

    Args:
        url: 申请版本 PDF 直链。
        nbytes: 分段字节数（默认 2MB；封面页远小于此）。命中不了可增大重试。
        settings: 全局配置（取 user_agent）。

    Returns:
        dict: {"domicile": 开曼群岛/百慕大/中国(境内)/香港/"",
               "confidence": strong/weak/none, "http_status": int, "method": "range"}
    """
    settings = settings or config_mod.get_settings()
    if not url:
        return {"domicile": "", "confidence": "none", "http_status": 0, "method": "range"}
    headers = {"User-Agent": settings.user_agent, "Range": f"bytes=0-{nbytes - 1}"}
    try:
        with httpx.Client(timeout=max(settings.http_timeout, 90), follow_redirects=True) as c:
            r = c.get(url, headers=headers)
    except httpx.HTTPError:
        return {"domicile": "", "confidence": "none", "http_status": 0, "method": "range"}
    letters = _letters_of_chunk(r.content)
    # 强信号：封面页注册地句式
    for pat, label in _SCAN_OFFSHORE_PATS:
        if pat in letters:
            return {"domicile": label, "confidence": "strong", "http_status": r.status_code, "method": "range"}
    for pat in _SCAN_PRC_PATS:
        if pat in letters:
            return {"domicile": "中国(境内)", "confidence": "strong", "http_status": r.status_code, "method": "range"}
    for pat in _SCAN_HK_PATS:
        if pat in letters:
            return {"domicile": "香港", "confidence": "strong", "http_status": r.status_code, "method": "range"}
    # 弱信号：裸法域名（无注册动词也可佐证，标 weak 供人工复核）
    if b"caymanislands" in letters:
        return {"domicile": "开曼群岛", "confidence": "weak", "http_status": r.status_code, "method": "range"}
    if b"bermudaislands" in letters or b"bermuda" in letters:
        return {"domicile": "百慕大", "confidence": "weak", "http_status": r.status_code, "method": "range"}
    return {"domicile": "", "confidence": "none", "http_status": r.status_code, "method": "range"}


class ListingRecord(BaseModel):
    """一条港交所新上市申请记录（归一化后）。"""

    model_config = ConfigDict(extra="ignore")

    id: int
    name_en: str = ""
    name_cn: str = ""
    board: str = ""
    status: str = ""
    status_raw: str = ""
    stock_code: str = ""  # 已上市企业的股票代码（来自 listed JSON 的 st 字段）
    doc_channel: str = ""  # L2 抽取所用文档的下载通道：app(慢库) / listedco(快CDN)
    submit_date: str = ""  # ISO YYYY-MM-DD
    submit_date_raw: str = ""  # DD/MM/YYYY
    posting_date: str = ""
    app_proof_url: str = ""
    multi_url: str = ""  # Multi-Files 分册目录 htm（可按章节取小文件，见 hkex_cover）
    has_phip: bool = False
    # 判定结果（L1/L2 填充）
    gd_l1: bool = False
    gd_l1_hit: str = ""
    is_offshore: bool = False  # 派生标签：注册地是否离岸（开曼/百慕大）；全量摸底主键
    scan_method: str = ""  # 注册地来源：cover(封面页权威) / full(整份PDF) / listedco / range / ""
    scan_confidence: str = ""  # strong / weak / none
    cover_section: str = ""  # 封面命中所在分册（WARNING / IMPORTANT / full-pdf）
    cover_evidence: str = ""  # 封面注册地句原文（可溯源）
    domicile: str = ""  # 最终采用的注册地
    gd_opco: str = ""  # 广东运营实体线索（原文片段）
    gd_opco_count: int = 0  # 广东城市词频（线索强度，非结论）
    gd_opco_entity_count: int = 0  # 其中落在「集团实体语境」句子内的次数（区分自述实体 vs 第三方地址噪音）
    gd_opco_source: str = ""  # 线索来源分册（SUMMARY / CORPORATE INFORMATION ...）
    is_vie: bool = False
    vie_evidence: str = ""  # VIE 判定原文片段（可溯源）
    vie_source: str = ""  # VIE 结论来源分册（SUMMARY / RISK FACTORS / ...）
    vie_terminated: bool = False  # 曾存在 VIE 安排但已终止（不计入 is_vie）
    vie_scanned: bool = False  # 是否已用新口径判定过（区分「未扫」与「扫过=否」）
    redchip_l2: str = ""  # 待核验 / 是 / 疑似 / 否-H股 / 否-其他

    @property
    def display_name(self) -> str:
        return self.name_cn or self.name_en

    @property
    def gd_flag(self) -> bool:
        """派生标签：名称命中广东（**仅作结果表一列，不作过滤闸门**）。"""
        return self.gd_l1


def _normalize_dmy(raw: str) -> str:
    """把 DD/MM/YYYY 归一化为 YYYY-MM-DD；失败返回空串（不回落今天）。"""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw or "")
    if not m:
        return ""
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return ""


def _find_app_docs(ls: list[dict[str, Any]]) -> tuple[str, str]:
    """从文档列表挑申请版本的（整份 PDF 链接, Multi-Files 目录 htm 相对路径）。

    Returns:
        tuple[str, str]: (app_proof_url 绝对链接, multi_url 相对路径)
    """
    for doc in ls or []:
        title = str(doc.get("nF", ""))
        if "application proof" in title.lower():
            u1 = doc.get("u1") or ""
            u2 = doc.get("u2") or ""
            return (DOC_BASE + u1.lstrip("/") if u1 else "", str(u2))
    return "", ""


def _find_app_proof(ls: list[dict[str, Any]]) -> str:
    """从文档列表中挑申请版本 PDF 的相对路径（兼容旧调用）。"""
    return _find_app_docs(ls)[0]


def _fetch_json(client: httpx.Client, fname: str) -> dict[str, Any]:
    """抓取单个清单 JSON 文件。"""
    r = client.get(LISTING_JSON_BASE + fname, timeout=client.timeout)
    r.raise_for_status()
    return r.json()


def fetch_index(
    settings: config_mod.Settings | None = None,
    langs: tuple[str, ...] = ("e", "c"),
) -> list[ListingRecord]:
    """拉取全部状态 × 板块清单，按 id 去重并归一化。

    英文文件提供记录主体，中文文件仅用于补 ``name_cn``，避免重复记录。

    Args:
        settings: 全局配置（取 http_timeout / user_agent）。
        langs: 抓取的语言文件；默认英 + 中（中文仅补名称）。

    Returns:
        list[ListingRecord]: 去重后的全量申请人记录。
    """
    settings = settings or config_mod.get_settings()
    client = httpx.Client(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    )
    records: dict[int, ListingRecord] = {}
    try:
        for status_prefix, (status_label, status_raw) in STATUS_FILES.items():
            for board in BOARD_MAP:
                for lang in langs:
                    fname = f"{status_prefix}_{board}_{lang}.json"
                    try:
                        data = _fetch_json(client, fname)
                    except httpx.HTTPError:
                        # 某些组合（如 appphip_gem）可能不存在，静默跳过
                        continue
                    for row in data.get("app", []) or []:
                        rid = int(row.get("id", 0))
                        if rid == 0:
                            continue
                        if rid not in records:
                            app_url, multi_url = _find_app_docs(row.get("ls", []))
                            rec = ListingRecord(
                                id=rid,
                                name_en=str(row.get("a", "")).strip(),
                                board=BOARD_MAP[board],
                                status=status_label,
                                status_raw=status_raw,
                                stock_code=str(row.get("st", "")).strip(),
                                submit_date_raw=str(row.get("d", "")),
                                submit_date=_normalize_dmy(str(row.get("d", ""))),
                                posting_date=str(row.get("postingDate", "")),
                                app_proof_url=app_url,
                                multi_url=multi_url,
                                has_phip=bool(row.get("hasPhip", False)),
                            )
                            records[rid] = rec
                        # 中文名补登（仅当英文记录已建且中文名为空）
                        if lang == "c":
                            cn = str(row.get("a_c", "") or row.get("a", "")).strip()
                            if cn and not records[rid].name_cn:
                                records[rid].name_cn = cn
    finally:
        client.close()
    return list(records.values())


def filter_year(records: list[ListingRecord], year: int = 2026) -> list[ListingRecord]:
    """按递交日年份过滤（"YYYY 以来递表"）。"""
    out = []
    for r in records:
        if not r.submit_date:
            continue
        if int(r.submit_date.split("-")[0]) >= year:
            out.append(r)
    return out


def guangdong_l1(rec: ListingRecord) -> tuple[bool, str]:
    """名称启发式：申请人名（中/英）是否含广东地域词。

    注意：仅捕获「名称里直接带粤字」的企业，是广东连接的**下界**；
    正典红筹（开曼壳名 + 广东运营实体）需 L2 以 PDF 内运营实体所在地判定。

    Returns:
        tuple[bool, str]: (是否命中, 命中词)
    """
    blob = f"{rec.name_cn} {rec.name_en}"
    m = _GD_PATTERN.search(blob)
    if m:
        return True, m.group(0)
    return False, ""


def extract_structure(pdf_path: Path, max_pages: int = 250) -> dict[str, Any]:
    """下載申请版本 PDF，抽注册地 / 广东运营实体 / VIE 信号（L2 深判）。

    复用 ``hkex.extract_pdf_pages``；默认英文版（避开中文 CID 乱码）。

    注册地判定：招股书常在靠后章节（History / 公司资料）才陈述上市主体注册地，
    故默认扫前 250 页；收集所有法域命中后按优先级取**离岸层优先**
    （开曼/百慕大 > 香港 > 中国境内）——红筹的本质是有离岸控股层。

    Args:
        pdf_path: 本地 PDF 路径。
        max_pages: 最多扫描页数（默认 250，覆盖多数招股书的注册地章节）。

    Returns:
        dict: {domicile, gd_opco, is_vie, snippet}
    """
    from redchip.overseas.hkex import extract_pdf_pages

    pages = extract_pdf_pages(pdf_path, max_pages=max_pages)
    text = "\n".join(p.text for p in pages)
    # 收集全部法域命中，按优先级取离岸层优先
    hits: list[str] = []
    for pat, label in _DOMICILE_PATTERNS:
        if pat.search(text):
            hits.append(label)
    domicile = ""
    for label in _DOMICILE_PRIORITY:
        if label in hits:
            domicile = label
            break
    if domicile == "中文注册地":
        # 中文模式命中但无法细分法域，标记待人工核验
        domicile = ""
    is_vie = bool(_VIE_PATTERN.search(text))
    # 广东运营实体：找广东地域词 + 其所在句子（含运营/总部/注册地上下文）
    gd_opco = ""
    for m in _GD_PATTERN.finditer(text):
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 80)
        snippet = text[start:end].replace("\n", " ").strip()
        if any(k in snippet.lower() for k in [
            "registered office", "principal place", "headquarter", "operation",
            "运营", "总部", "注册地址", "主要营业", "business", "subsidiar", "子公司",
        ]):
            gd_opco = snippet
            break
    return {"domicile": domicile, "gd_opco": gd_opco, "is_vie": is_vie}


def _is_valid_pdf(path: Path) -> bool:
    """校验本地文件确为 PDF（防披露易偶发返回 HTML 错误页被误缓存）。"""
    if not path.exists() or path.stat().st_size < 5:
        return False
    with path.open("rb") as fh:
        return fh.read(5) == b"%PDF-"


def download_app_proof(rec: ListingRecord, dest_dir: Path, settings: config_mod.Settings | None = None) -> Path | None:
    """下載某记录用于 L2 结构抽取的 PDF 到本地（已存在则复用）。

    **双通道路由**（用户要求对齐腾讯做法）：
    - 已上市且带股票代码（``status_raw=="listed"`` 且 ``stock_code`` 非空）
      → 走 ``listedco/listconews`` **快 CDN**（腾讯走的通道，单文件 ~23s）；
      取该股票最新「招股章程」(prospectus)，回落年报。
    - 其余（处理中 / 失效 / 撤回 / 发回）→ 走申请版本 ``/app/`` **慢库**
      （披露易该库连接慢，单文件偶发 >150s，故用较长超时下限 120s）。

    存储按路径内嵌日期归档；``doc_channel`` 记录实际所用通道，供 BD 台账溯源。

    Args:
        rec: 申请人记录。
        dest_dir: 保存根目录。
        settings: 全局配置。

    Returns:
        Path | None: 本地 PDF 路径；无可用文档或下载失败返回 None。
    """
    settings = settings or config_mod.get_settings()
    if rec.status_raw == "listed" and rec.stock_code:
        rec.doc_channel = "listedco"
        return _download_listed_prospectus(rec, dest_dir, settings)
    rec.doc_channel = "app"
    return _download_app_slow(rec, dest_dir, settings)


def _download_app_slow(rec: ListingRecord, dest_dir: Path, settings: config_mod.Settings) -> Path | None:
    """申请版本慢通道：从 ``/app/`` 库下載 Application Proof PDF。"""
    if not rec.app_proof_url:
        return None
    # 从路径抽年与文档日（YYMMDD），用于按日期归档
    m_year = re.search(r"/sehk/(\d{4})/", rec.app_proof_url)
    m_ymd = re.search(r"sehk(\d{6})", rec.app_proof_url)
    year = m_year.group(1) if m_year else "unknown"
    ymd = m_ymd.group(1) if m_ymd else ""
    sub = dest_dir / year
    sub.mkdir(parents=True, exist_ok=True)
    fname = f"{rec.id}_{ymd}.pdf" if ymd else f"{rec.id}.pdf"
    path = sub / fname
    if _is_valid_pdf(path):
        return path
    if path.exists():
        path.unlink()  # 旧缓存非有效 PDF（如 HTML 错误页），清掉重下
    try:
        # 申请版本 PDF 体积大、披露易连接慢，用较长超时（下限 120s）
        to = max(settings.http_timeout, 120)
        with httpx.Client(timeout=to, follow_redirects=True,
                          headers={"User-Agent": settings.user_agent}) as c:
            with c.stream("GET", rec.app_proof_url) as r:
                r.raise_for_status()
                with path.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
        if not _is_valid_pdf(path):
            path.unlink()
            return None
        return path
    except httpx.HTTPError:
        return None


def _download_listed_prospectus(rec: ListingRecord, dest_dir: Path, settings: config_mod.Settings) -> Path | None:
    """已上市快通道：经 ``listedco/listconews`` CDN 取招股章程（腾讯做法）。"""
    from redchip.overseas.hkex import HkexClient

    try:
        with HkexClient(settings) as client:
            stock_id = client.resolve_stock_id(rec.stock_code)
            filings = client.search_filings(stock_id, years_back=3, lang="EN")
            # 优先招股章程；回落年报。任一命中后校验 PDF 完整性，损坏则弃用标待核验。
            for kind in ("prospectus", "annual_report"):
                filing = client.pick_filing(filings, kind)
                if filing is None:
                    continue
                year = (filing.published_at or "unknown").split("-")[0] or "unknown"
                sub = dest_dir / year
                path = client.download(filing, sub)
                if _is_valid_pdf(path):
                    return path
                if path.exists():
                    path.unlink()
                continue  # 该文档损坏，尝试下一类（招股章程→年报）
            return None
    except Exception:
        # 快通道异常不应中断批处理；回落标注待核验
        return None


def classify_redchip(rec: ListingRecord) -> str:
    """基于 L2 抽得字段给出红筹判定。

    - 离岸注册（开曼/百慕大）+ 抽得广东运营实体 → 是（红筹）
    - 离岸注册但广东实体未抽得 → 疑似（需人工核实运营地）
    - 境内注册 → 否-H股（H 股，非红筹）
    - 其他（香港/其他） → 否-其他
    """
    if not rec.domicile:
        return "待核验"
    if rec.domicile in ("开曼群岛", "百慕大"):
        return "是" if rec.gd_opco else "疑似"
    if rec.domicile == "中国(境内)":
        return "否-H股"
    return "否-其他"


def diff(
    old: list[ListingRecord], new: list[ListingRecord]
) -> dict[str, list[ListingRecord]]:
    """与旧状态比对，检测新增与状态变更。

    Args:
        old: 上次持久化的记录。
        new: 本次拉取（已按年份过滤）的记录。

    Returns:
        dict: {"added": 新 id, "changed": 状态变更的记录}
    """
    old_by_id = {r.id: r for r in old}
    added: list[ListingRecord] = []
    changed: list[ListingRecord] = []
    for r in new:
        if r.id not in old_by_id:
            added.append(r)
        elif old_by_id[r.id].status_raw != r.status_raw:
            changed.append(r)
    return {"added": added, "changed": changed}


def state_path(settings: config_mod.Settings | None = None) -> Path:
    settings = settings or config_mod.get_settings()
    return settings.redchip_data_dir / "listing_state.json"


def persist(records: list[ListingRecord], path: Path | None = None) -> Path:
    """持久化状态到本地 JSON（**原子写**：临时文件 + os.replace）。

    后台批处理会周期性落盘，若不原子化，并发的读方（产出脚本）可能读到半截 JSON。
    """
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "count": len(records),
        "records": [r.model_dump() for r in records],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 同分区原子替换
    return path


def load_state(path: Path | None = None) -> list[ListingRecord]:
    """读取本地状态。"""
    path = path or state_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ListingRecord.model_validate(r) for r in data.get("records", [])]


def apply_scan(rec: ListingRecord, result: dict[str, Any]) -> None:
    """把轻量/封面注册地扫描结果写回记录（封面口径优先，不覆盖同类或更强结果）。

    Args:
        rec: 申请人记录（原地修改）。
        result: ``hkex_cover.scan_cover`` 或 ``scan_domicile_range`` 的返回值。
    """
    method = result.get("method", "")
    # 已由封面页（权威）判定过的，不被更弱的通道覆盖
    if rec.scan_method == "cover" and method != "cover":
        return
    dom = result.get("domicile", "")
    if result.get("section"):
        rec.cover_section = result["section"]
    if result.get("evidence"):
        rec.cover_evidence = result["evidence"]
    if not dom:
        return
    rec.domicile = dom
    rec.scan_method = "cover" if method.startswith("cover") else method
    rec.scan_confidence = "strong"
    rec.is_offshore = dom in ("开曼群岛", "百慕大")


def merge_records(
    base: list[ListingRecord],
    enrich: list[ListingRecord],
    carry_domicile: bool = False,
) -> list[ListingRecord]:
    """以 base（本次官方拉取）为准，把历史记录中的结论回填。

    ⚠️ ``carry_domicile`` 默认 **False**：历史 ``domicile`` 不回填。原因是旧
    ``extract_structure`` 扫整份 PDF 250 页按「离岸 > 境内」取优先级，存在假阳性
    （实测 14 条：广州极飞/深圳小阔/迈瑞/天农 等封面明写 PRC 却被判开曼/百慕大）。
    只有 ``listing_full_state.json``（本流程自己的封面口径产物）才允许回填注册地，
    用于**断点续跑**。

    Args:
        base: 本次拉取的全量记录。
        enrich: 历史状态。
        carry_domicile: 是否回填 domicile/scan_* （仅对本流程产物为 True）。

    Returns:
        list[ListingRecord]: 回填后的记录列表。
    """
    old = {r.id: r for r in enrich}
    for r in base:
        o = old.get(r.id)
        if o is None:
            continue
        if carry_domicile and o.domicile:
            r.domicile = o.domicile
            r.scan_method = o.scan_method
            r.scan_confidence = o.scan_confidence
            r.is_offshore = o.domicile in ("开曼群岛", "百慕大")
            r.cover_section = o.cover_section
            r.cover_evidence = o.cover_evidence
            r.doc_channel = o.doc_channel
        if o.gd_opco:
            r.gd_opco = o.gd_opco
            r.gd_opco_count = o.gd_opco_count
            r.gd_opco_entity_count = o.gd_opco_entity_count
            r.gd_opco_source = o.gd_opco_source
        # VIE 结论只回填「带新口径标记」的（旧口径无 vie_scanned，其 is_vie 不可信）
        if o.vie_scanned:
            r.is_vie = o.is_vie
            r.vie_evidence = o.vie_evidence
            r.vie_source = o.vie_source
            r.vie_terminated = o.vie_terminated
            r.vie_scanned = True
        if o.stock_code and not r.stock_code:
            r.stock_code = o.stock_code
    return base


def mark_guangdong(records: list[ListingRecord]) -> None:
    """原地填充 L1 广东标记。"""
    for r in records:
        hit, word = guangdong_l1(r)
        r.gd_l1 = hit
        r.gd_l1_hit = word
