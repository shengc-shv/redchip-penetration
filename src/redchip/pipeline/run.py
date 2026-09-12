"""港股红筹穿透流水线编排。

流程（对应方案文档第一、七节）
------------------------------
1. 境外抓取：HKEXnews → 招股书/年报 PDF → 分页文本
2. FTS 检索：只取命中「架构 / 合约安排 / VIE」的段落（6k token 预算）
3. 工商候选：按关键词检索境内 WFOE（1k token 预算，最多 8 条）
4. LLM-A：架构提取 + VIE 识别 + WFOE 消歧 + UBO 初判
5. 广东省过滤：LLM-A 消歧之后执行，零额外 token
6. 境内穿透：自下而上采集股东，写入图谱
7. UBO 穿透：累乘持股比，阈值 25%
8. LLM-B：路径审查 + 中文报告 + Mermaid
9. 导出：graph.json / report.md / PNG / SVG / DOT / Mermaid

任何一步失败都只降级（记入 ``errors`` 并标记需人工复核），不阻断后续步骤。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from redchip import config as config_mod
from redchip.domestic import cnbiz as cnbiz_mod
from redchip.domestic.penetration import (
    collect_candidates,
    expand_upward,
    filter_guangdong,
    is_guangdong,
)
from redchip.graph import render as render_mod
from redchip.graph.store import Graph
from redchip.graph.ubo import penetrate_ubo
from redchip.llm import client as llm_mod
from redchip.models import confidence as conf_mod
from redchip.models.schema import (
    CandidateCompany,
    CompanyNode,
    CompanyReport,
    ControlEdge,
    EntityKind,
    Jurisdiction,
    ListedEntity,
    LlmAOutput,
    Market,
    OwnEdge,
    PipelineState,
    VieContract,
    WfoeMatch,
)
from redchip.overseas import fts as fts_mod
from redchip.overseas import hkex as hkex_mod
from redchip.verify import crosscheck as verify_mod

_CN_COMPANY_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}?(?:有限公司|股份有限公司|有限責任公司)")

# 叙述性误匹配的标志词：命中即判定为句子片段而非企业名
_NAME_STOPWORDS = (
    "本公司", "我们", "其股份", "上述", "以下", "包括", "以及", "根据", "由于", "倘若",
    "所有", "任何", "其他", "相关", "及其", "或者", "并且", "同时", "因此", "例如",
    "这些", "那些", "之间", "之一",
)

# 招股书语境下的离岸层提示词
_OFFSHORE_HINTS = ("Cayman", "開曼", "开曼", "BVI", "British Virgin", "Hong Kong", "香港")


def run_hk(
    code: str,
    name: str = "",
    wfoe_keywords: list[str] | None = None,
    settings: config_mod.Settings | None = None,
) -> CompanyReport:
    """跑通单家港股企业的完整穿透。

    各阶段彼此独立、以 ``output/<code>/state.json`` 传递中间态，
    因此既可整体运行，也可由 GitHub Actions 分步调用（见 scripts/）。

    Args:
        code: 港股代码。
        name: 企业中文名（用于候选检索与报告标题）。
        wfoe_keywords: WFOE 检索关键词。
        settings: 全局配置；缺省自动载入。

    Returns:
        CompanyReport: 含图谱、UBO、置信度与报告的完整结果。
    """
    cfg = settings or config_mod.get_settings()
    state = load_state(code, cfg)
    state = stage_fetch(state, cfg)
    state = stage_candidates(state, cfg, name=name, keywords=wfoe_keywords)
    state = stage_llm_a(state, cfg, name)
    state = stage_build(state, cfg, name)
    state = stage_llm_b(state, cfg)
    stage_crosscheck(state, cfg)
    stage_export(state, cfg)
    return load_report(code, cfg) or CompanyReport(code=code)


# ---------------------------------------------------------------------------
# 各阶段实现
# ---------------------------------------------------------------------------


def _fetch_pages(state: PipelineState, cfg: config_mod.Settings) -> list[hkex_mod.PageText]:
    """抓取披露文件并抽取分页文本。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        list[PageText]: 分页文本；mock 模式读 fixture，失败返回空列表。
    """
    if cfg.redchip_mock:
        return _mock_pages(state, cfg)
    try:
        filing, pdf_path, pages = hkex_mod.fetch_hk_document(
            state.code, cfg.redchip_data_dir, kinds=("prospectus", "annual_report"), years_back=10
        )
    except Exception as exc:  # noqa: BLE001 - 抓取失败降级，不阻断流水线
        state.errors.append(f"HKEXnews 抓取失败：{exc}")
        return []
    state.doc_url = filing.url
    state.doc_kind = "prospectus" if "prospectus" in filing.title.lower() else "annual_report"
    state.doc_published_at = filing.published_at
    state.pdf_path = str(pdf_path)
    return pages


def _mock_pages(state: PipelineState, cfg: config_mod.Settings) -> list[hkex_mod.PageText]:
    """从 fixture 读取分页文本（离线联调用）。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        list[PageText]: 分页文本。
    """
    path = config_mod.FIXTURES_DIR / "hk" / f"{state.code}.json"
    if not path.exists():
        state.errors.append(f"mock 模式缺少 fixture：{path}")
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    doc = data.get("doc", {})
    state.doc_kind = doc.get("kind", "annual_report")
    state.doc_url = doc.get("url", "")
    state.doc_published_at = doc.get("published_at", "")
    return [hkex_mod.PageText.model_validate(p) for p in data.get("pages", [])]


def _index_conn(state: PipelineState, cfg: config_mod.Settings) -> Any:
    """返回 FTS 连接；未建索引时返回空内存库。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        Any: sqlite3 连接对象。
    """
    conn = getattr(state, "_conn", None)
    if conn is not None:
        return conn
    import sqlite3

    return sqlite3.connect(":memory:")


def _run_fts(
    state: PipelineState, pages: list[hkex_mod.PageText], cfg: config_mod.Settings
) -> list[fts_mod.FtsHit]:
    """建索引并检索命中段落。

    Args:
        state: 流水线状态。
        pages: 分页文本。
        cfg: 全局配置。

    Returns:
        list[FtsHit]: 命中列表。
    """
    if not pages:
        return []
    db_path = cfg.redchip_data_dir / "index" / f"{state.code}.sqlite"
    conn = fts_mod.build_index(pages, db_path)
    setattr(state, "_conn", conn)  # noqa: B010 - 供后续 VIE 证据判定复用
    hits = fts_mod.search(conn, max_tokens=cfg.fts_max_tokens)
    state.fts_hits = [h.model_dump() for h in hits]
    state.fts_text = config_mod.truncate_tokens(fts_mod.render_hits(hits), cfg.fts_max_tokens)
    return hits


def _run_llm_a(
    state: PipelineState, cfg: config_mod.Settings, company_name: str = ""
) -> LlmAOutput:
    """调用 LLM-A 完成架构提取。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        LlmAOutput: 提取结果；不可用时返回规则兜底结果并标记需人工复核。
    """
    candidate_lines = "\n".join(
        f"{i}. {c.name}｜信用代码：{c.credit_code or '未知'}｜经营范围：{c.business_scope[:80]}"
        for i, c in enumerate(state.candidates)
    ) or "（无候选）"

    client = llm_mod.LLMClient(cfg)

    # 本地分析结果优先：若存在 llm_a.manual.json，视为「按 Prompt 由本地分析产出」的结果，
    # 直接采用且不再标记需人工复核。这样无 API Key 也能得到高质量输出。
    manual = manual_llm_a_path(state.code, cfg)
    if manual.exists():
        try:
            return LlmAOutput.model_validate(json.loads(manual.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            state.errors.append(f"本地分析结果 {manual.name} 解析失败：{exc}")

    if not client.is_available():
        fallback = _fallback_llm_a(state, company_name)
        fallback.needs_review = True
        fallback.review_reason = "未配置 LLM_API_KEY，使用规则兜底提取"
        return fallback

    try:
        template = llm_mod.load_prompt("llm_a.md")
        prompt = llm_mod.render_prompt(
            template,
            fts_retrieved_text=state.fts_text or "（未检索到相关段落）",
            candidates=candidate_lines,
        )
        return client.call_json(prompt, LlmAOutput, tag=f"{state.code}_llm_a")
    except Exception as exc:  # noqa: BLE001
        state.errors.append(f"LLM-A 调用失败：{exc}")
        fallback = _fallback_llm_a(state, company_name)
        fallback.needs_review = True
        fallback.review_reason = f"LLM-A 失败：{exc}"
        return fallback


def _fallback_llm_a(state: PipelineState, company_name: str = "") -> LlmAOutput:
    """规则兜底：从 FTS 文本抽取境内公司名与 VIE 线索。

    仅在 LLM-A 不可用时调用，产出**低置信度**结果并强制标记需人工复核。

    Args:
        state: 流水线状态。
        company_name: 目标企业中文名（用于填充上市主体）。

    Returns:
        LlmAOutput: 低置信度的提取结果。
    """
    names = _extract_company_names(
        state.fts_text, known=[c.name for c in state.candidates]
    )
    has_vie = any(k in state.fts_text for k in ("合约安排", "结构性合约", "可变利益实体", "VIE"))
    out = LlmAOutput(
        listed_entity=ListedEntity(
            name=company_name,
            jurisdiction="Cayman" if any(k in state.fts_text for k in ("开曼", "Cayman")) else "",
        ),
        chain_summary="（规则兜底）" + (f"疑似 VIE 架构，境内实体：{names[0]}" if names else "无有效信息"),
        confidence=0.3,
        source_pages=[str(h.get("page_no")) for h in state.fts_hits[:5]],
    )
    known = [
        c
        for c in state.candidates
        if c.credit_code and c.name and c.name in (state.fts_text or "")
    ]
    known_names = {c.name for c in known}
    others = [n for n in names if n not in known_names]

    out.wfoe.extend(
        WfoeMatch(
            name=c.name,
            credit_code=c.credit_code,
            matched_candidate_index=state.candidates.index(c),
            match_reason="工商候选名称命中披露文本",
        )
        for c in known[:3]
    )
    # 其余未在候选库命中的境内实体，视为可能的 VIE 运营实体
    for n in others[:2]:
        if len(out.wfoe) >= 3:
            break
        out.wfoe.append(
            WfoeMatch(
                name=n,
                credit_code="",
                matched_candidate_index=-1,
                match_reason="规则抽取自披露文件",
            )
        )

    # VIE 协议控制：规则兜底只能做保守推断（先候选库外实体，再候选库内次优实体），
    # 且必须落到「需人工复核」，由 LLM-A 或人工确认真正的 WFOE / 运营实体配对。
    party_a = party_b = ""
    if has_vie:
        if known and others:
            party_a, party_b = known[0].name, others[0]
        elif len(known) >= 2:
            party_a, party_b = known[0].name, known[1].name
    if party_a and party_b:
        out.vie_contracts.append(
            VieContract(type="其他", party_a=party_a, party_b=party_b, shareholders=[])
        )
    return out


def _build_graph_from_llm_a(
    graph: Graph,
    llm_a: LlmAOutput,
    candidates: list[CandidateCompany],
    cnbiz: cnbiz_mod.CnbizClient,
) -> list[str]:
    """把 LLM-A 提取的链条写入图谱。

    方向约定：``OwnEdge(from=股东, to=被持股公司)``，即从上市主体向下。

    Args:
        graph: 目标图谱。
        llm_a: LLM-A 结果。
        candidates: 工商候选（用于补全信用代码与省份）。
        cnbiz: 工商客户端（用于补全地域信息）。

    Returns:
        list[str]: WFOE 节点 id 列表（作为 UBO 穿透起点）。
    """
    wfoe_ids: list[str] = []
    listed = llm_a.listed_entity
    if listed.name:
        graph.add_company(
            name=listed.name,
            jurisdiction=_parse_jurisdiction(listed.jurisdiction),
            kind=EntityKind.LISTED,
        )

    prev_id = f"{listed.name}@{_parse_jurisdiction(listed.jurisdiction).value}" if listed.name else ""
    for ent in llm_a.intermediate_entities:
        if not ent.name:
            continue
        node = graph.add_company(
            name=ent.name,
            jurisdiction=_parse_jurisdiction(ent.jurisdiction),
            kind=_kind_by_jurisdiction(ent.jurisdiction),
        )
        if prev_id and prev_id in graph.nodes:
            graph.add_own(OwnEdge(from_id=prev_id, to_id=node.id, share_pct=100.0, source="llm"))
        prev_id = node.id

    # VIE 协议的被控制方是运营实体，不再建「上市主体 → 运营实体」的股权边，
    # 否则会凭空多出一条不存在的直接持股关系
    vie_opcos = {c.party_b for c in llm_a.vie_contracts if c.party_b}

    for w in llm_a.wfoe:
        if not w.name:
            continue
        cand = candidates[w.matched_candidate_index] if 0 <= w.matched_candidate_index < len(candidates) else None
        credit = w.credit_code or (cand.credit_code if cand else "")
        is_opco = w.name in vie_opcos
        node = graph.add_company(
            name=w.name,
            jurisdiction=Jurisdiction.CN,
            kind=EntityKind.OPCO if is_opco else EntityKind.WFOE,
            credit_code=credit or None,
            province=cand.province if cand else None,
            city=cand.city if cand else None,
        )
        wfoe_ids.append(node.id)
        if prev_id and prev_id in graph.nodes and not is_opco:
            graph.add_own(OwnEdge(from_id=prev_id, to_id=node.id, share_pct=100.0, source="llm"))

    # VIE 协议控制：WFOE → 境内运营实体
    for contract in llm_a.vie_contracts:
        if not contract.party_b:
            continue
        # 运营实体若能在工商候选中命中，直接用信用代码作为节点 id，
        # 与后续境内穿透使用同一实体，避免被拆成「名称节点 + 信用代码节点」两个。
        opco = graph.add_company(
            name=contract.party_b,
            jurisdiction=Jurisdiction.CN,
            kind=EntityKind.OPCO,
            credit_code=_candidate_code(candidates, contract.party_b),
        )
        wfoe_name = contract.party_a or (llm_a.wfoe[0].name if llm_a.wfoe else "")
        wfoe_node = _find_by_name(graph, wfoe_name) if wfoe_name else None
        if wfoe_node:
            graph.add_control(
                ControlEdge(
                    from_id=wfoe_node.id,
                    to_id=opco.id,
                    contract_type=_normalize_contract(contract.type),
                    source="llm",
                )
            )
        for sh_name in contract.shareholders:
            person = graph.add_person(sh_name)
            graph.add_own(OwnEdge(from_id=person.id, to_id=opco.id, share_pct=0.0, source="llm"))
        wfoe_ids.append(opco.id)

    # 尝试补全地域信息（免费接口）
    for node in list(graph.companies()):
        if node.credit_code and not node.province:
            basic = cnbiz.get_company_basic(node.credit_code)
            if basic.province:
                node.province = basic.province
                node.city = basic.city
    return wfoe_ids


def _enrich(graph: Graph, credit_code: str, name: str, province: str, city: str) -> None:
    """补全节点地域信息。

    Args:
        graph: 图谱。
        credit_code: 统一社会信用代码。
        name: 企业名称。
        province: 省份。
        city: 城市。
    """
    node = graph.nodes.get(credit_code)
    if isinstance(node, CompanyNode):
        node.province = province
        node.city = city
        if is_guangdong({"province": province, "city": city}):
            node.kind = EntityKind.WFOE if node.kind == EntityKind.UNKNOWN else node.kind


def _run_llm_b(
    state: PipelineState, report: CompanyReport, graph: Graph, cfg: config_mod.Settings
) -> str:
    """调用 LLM-B 生成审查与报告。

    Args:
        state: 流水线状态。
        report: 当前报告（提供穿透数据）。
        graph: 图谱。
        cfg: 全局配置。

    Returns:
        str: Markdown 报告；不可用时返回空串（调用方用兜底报告）。
    """
    # 本地分析报告优先（与 llm_a.manual.json 配套）
    manual = manual_report_path(state.code, cfg)
    if manual.exists():
        return _extract_mermaid(manual.read_text(encoding="utf-8"), report)

    client = llm_mod.LLMClient(cfg)
    if not client.is_available():
        return ""
    try:
        template = llm_mod.load_prompt("llm_b.md")
        penetration = {
            "ubos": [u.model_dump() for u in report.ubos],
            "nodes": [n.model_dump() for n in report.nodes],
            "owns": [e.model_dump() for e in report.owns],
            "controls": [e.model_dump() for e in report.controls],
            "confidence": report.confidence.model_dump(),
        }
        prompt = llm_mod.render_prompt(
            template,
            penetration_json=json.dumps(penetration, ensure_ascii=False)[:4000],
            llm_a_json=json.dumps(report.llm_a.model_dump(), ensure_ascii=False)[:3000],
        )
        raw = client.complete(prompt, json_mode=False)
        (cfg.redchip_output_dir / "llm_raw").mkdir(parents=True, exist_ok=True)
        (cfg.redchip_output_dir / "llm_raw" / f"{state.code}_llm_b.md").write_text(
            raw, encoding="utf-8"
        )
        return _extract_mermaid(raw, report)
    except Exception as exc:  # noqa: BLE001
        state.errors.append(f"LLM-B 调用失败：{exc}")
        return ""


def _extract_mermaid(md: str, report: CompanyReport) -> str:
    """从 LLM-B 返回中提取 Mermaid 代码并回写到报告。

    Args:
        md: LLM-B 返回的 Markdown。
        report: 报告对象（若 LLM 给出了合法图则覆盖默认图）。

    Returns:
        str: 清理后的 Markdown（保留 Mermaid 代码块）。
    """
    match = re.search(r"```mermaid\s*(.*?)```", md, flags=re.DOTALL)
    if match and "graph" in match.group(1):
        report.mermaid = match.group(1).strip()
    return md


def _fallback_report(report: CompanyReport, graph: Graph) -> str:
    """LLM-B 不可用时生成规则报告，保证交付物不为空。

    Args:
        report: 报告对象。
        graph: 图谱。

    Returns:
        str: Markdown 报告。
    """
    ubo_lines = (
        "\n".join(
            f"- {u.name}：累计持股 {u.cumulative_share * 100:.2f}%"
            f"（{'含VIE协议控制' if u.reached_via_vie else '纯股权'}，路径 "
            f"{' → '.join(u.path_names)}）"
            for u in report.ubos
        )
        or "- 未穿透到满足 25% 阈值的自然人"
    )
    gd = "、".join(report.guangdong_entities) or "未识别到广东省内实体"
    cc = report.crosscheck or {}
    cc_lines = (
        "\n".join(
            f"- {i.get('detail')}（{'⚠️' if i.get('severity') == 'error' else 'ℹ️'}）"
            for i in cc.get("issues", [])
        )
        or "- 披露口径与工商登记口径核对一致，未发现问题"
    )
    disclosed = cc.get("disclosed", [])
    disclosed_md = (
        "、".join(f"{h['name']} {h['share_pct']:g}%（p.{h.get('source_page') or '?'}）" for h in disclosed)
        or "未召回股东权益章节"
    )
    return f"""# {report.name}（{report.code}）红筹架构穿透报告

> 自动生成于 {report.generated_at}｜置信度 {report.confidence.total}｜{"⚠️ 需人工复核" if report.needs_review else "✅ 自动通过"}

## 路径审查
- 合理性：{"待复核" if report.needs_review else "是"}
- 问题：{"; ".join(report.review_reasons) or "无"}
- 建议：{"补充 LLM 分析（未配置 LLM_API_KEY，当前为规则兜底输出）" if report.needs_review else "—"}

## 分析报告
### 企业概况
- 上市地：{"港股" if report.market.value == "hk" else "美股"}｜披露文件：{report.doc_kind or "未知"}（{report.doc_published_at or "未知日期"}）
- 广东省内实体：{gd}

### 穿透链条
{report.llm_a.chain_summary or "（无）"}

### UBO结果
{ubo_lines}

### 架构特点
- 节点 {len(report.nodes)} 个，股权边 {len(report.owns)} 条，协议控制边 {len(report.controls)} 条
- {"存在 VIE 协议控制结构" if report.controls else "未识别到 VIE 协议控制"}

### 交叉验证（披露口径 ↔ 工商登记口径）
- 披露口径（第XV部镜像）：{disclosed_md}
- 核对结果：
{cc_lines}

### 风险提示
- 离岸层（开曼/BVI）股东名册不公开，穿透存在天然断点
- VIE 架构下登记股东与实际受益人可能不一致，需结合协议文本核验

## Mermaid架构图
```mermaid
{report.mermaid}
```
"""


def _merge_ubos(ubos: list[Any]) -> list[Any]:
    """合并多起点穿透结果，同一自然人保留最高持股比例。

    Args:
        ubos: 原始结果。

    Returns:
        list[UboResult]: 合并后的结果。
    """
    best: dict[str, Any] = {}
    for u in ubos:
        cur = best.get(u.person_id)
        if cur is None or u.cumulative_share > cur.cumulative_share:
            best[u.person_id] = u
    return sorted(best.values(), key=lambda x: x.cumulative_share, reverse=True)


def _export(report: CompanyReport, graph: Graph, cfg: config_mod.Settings) -> Path:
    """导出全部交付物。

    Args:
        report: 报告对象。
        graph: 图谱。
        cfg: 全局配置。

    Returns:
        Path: 输出目录。
    """
    out_dir = cfg.redchip_output_dir / report.code
    out_dir.mkdir(parents=True, exist_ok=True)
    graph.save(out_dir / "graph.json")
    (out_dir / "llm_a.json").write_text(
        json.dumps(report.llm_a.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(report.report_md, encoding="utf-8")
    (out_dir / "result.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    render_mod.render_all(graph, out_dir)
    graph.sync_neo4j()
    return out_dir


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _extract_company_names(
    text: str, limit: int = 6, known: list[str] | None = None
) -> list[str]:
    """从文本中抽取境内公司全称。

    抽取策略（按可靠性降序）：
    1. **已知名优先**：工商候选里出现的名称一定准确，先全词匹配；
    2. 正则兜底：匹配「…有限公司」并剔除叙述性误匹配（含「本公司 / 我们 / 包括」等停用词，
       或长度超过 20 字的几乎都是句子而非公司名）。

    Args:
        text: 披露文本。
        limit: 返回条数上限。
        known: 已知公司名（来自工商候选），优先采用。

    Returns:
        list[str]: 去重后的公司名。
    """
    haystack = text or ""
    known_set = {k for k in (known or []) if k}
    result: list[str] = []

    for name in known or []:
        if name and name in haystack and name not in result:
            result.append(name)

    for match in _CN_COMPANY_RE.finditer(haystack):
        name = match.group(0)
        if len(name) > 20 or name in result:
            continue
        if any(stop in name for stop in _NAME_STOPWORDS):
            continue
        # 已知名的「加长变体」（如被"向/与/及其"等前导词污染）一律丢弃，只保留精确的已知名
        if any(k != name and k in name for k in known_set):
            continue
        result.append(name)

    return sorted(result, key=len, reverse=True)[:limit]


def _parse_jurisdiction(value: str) -> Jurisdiction:
    """把 LLM 给出的注册地文本映射为枚举。

    Args:
        value: 注册地文本。

    Returns:
        Jurisdiction: 枚举值；无法识别返回 OTHER。
    """
    text = (value or "").upper()
    if "CAYMAN" in text or "开曼" in (value or ""):
        return Jurisdiction.CAYMAN
    if "BVI" in text or "VIRGIN" in text:
        return Jurisdiction.BVI
    if "HK" in text or "香港" in (value or "") or "HONG KONG" in text:
        return Jurisdiction.HK
    if "CN" in text or "中国" in (value or "") or "境内" in (value or ""):
        return Jurisdiction.CN
    return Jurisdiction.OTHER


def _kind_by_jurisdiction(value: str) -> EntityKind:
    """按注册地推断实体角色。

    Args:
        value: 注册地文本。

    Returns:
        EntityKind: 实体角色。
    """
    jur = _parse_jurisdiction(value)
    if jur in (Jurisdiction.CAYMAN, Jurisdiction.BVI, Jurisdiction.BERMUDA):
        return EntityKind.OFFSHORE
    if jur == Jurisdiction.HK:
        return EntityKind.HK
    if jur == Jurisdiction.CN:
        return EntityKind.OPCO
    return EntityKind.UNKNOWN


def _candidate_code(candidates: list[CandidateCompany], name: str) -> str | None:
    """按企业名在候选列表中查找统一社会信用代码。

    Args:
        candidates: 工商候选列表。
        name: 企业名称。

    Returns:
        str | None: 命中的信用代码；未命中返回 None。
    """
    if not name:
        return None
    for cand in candidates:
        if cand.name == name and cand.credit_code:
            return cand.credit_code
    for cand in candidates:
        if cand.credit_code and (name in cand.name or cand.name in name):
            return cand.credit_code
    return None


def _find_by_name(graph: Graph, name: str) -> CompanyNode | None:
    """按名称查找公司节点。

    Args:
        graph: 图谱。
        name: 企业名称。

    Returns:
        CompanyNode | None: 命中节点。
    """
    for node in graph.companies():
        if node.name == name or (name and name in node.name):
            return node
    return None


def _normalize_contract(value: str) -> Any:
    """把协议类型归一化为受控枚举。

    Args:
        value: LLM 给出的协议类型。

    Returns:
        Any: 合法枚举值；无法识别返回「其他」。
    """
    allowed = ("独家服务协议", "股权质押", "投票权委托", "独家购买权", "配偶同意函")
    return value if value in allowed else "其他"


def run_batch(
    codes: list[str],
    names: dict[str, str] | None = None,
    keywords: dict[str, list[str]] | None = None,
) -> list[CompanyReport]:
    """批量跑多家企业并生成汇总。

    Args:
        codes: 股票代码列表。
        names: 代码 → 中文名。
        keywords: 代码 → WFOE 检索关键词。

    Returns:
        list[CompanyReport]: 各家企业的报告。
    """
    cfg = config_mod.get_settings()
    reports: list[CompanyReport] = []
    for code in codes:
        try:
            reports.append(
                run_hk(code, (names or {}).get(code, ""), (keywords or {}).get(code), cfg)
            )
        except Exception as exc:  # noqa: BLE001 - 单家失败不影响整批
            reports.append(
                CompanyReport(
                    code=code,
                    needs_review=True,
                    review_reasons=[f"流水线异常：{exc}"],
                    report_md=f"# {code} 处理失败\n\n{exc}",
                )
            )
    _write_summary(reports, cfg)
    return reports


def _write_summary(reports: list[CompanyReport], cfg: config_mod.Settings) -> Path:
    """写出汇总索引。

    Args:
        reports: 报告列表。
        cfg: 全局配置。

    Returns:
        Path: 汇总文件路径。
    """
    cfg.redchip_output_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 红筹架构穿透汇总",
        "",
        f"- 企业数：{len(reports)}",
        f"- 需人工复核：{sum(1 for r in reports if r.needs_review)}",
        "",
        "| 代码 | 名称 | 广东实体 | UBO 数 | 置信度 | 状态 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in reports:
        lines.append(
            f"| {r.code} | {r.name or '-'} | {len(r.guangdong_entities)} | {len(r.ubos)} | "
            f"{r.confidence.total} | {'⚠️复核' if r.needs_review else '✅'} |"
        )
    path = cfg.redchip_output_dir / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 分步执行入口：GitHub Actions 逐步调用，也可整体运行
# ---------------------------------------------------------------------------


def state_path(code: str, cfg: config_mod.Settings) -> Path:
    """返回中间态文件路径。

    Args:
        code: 股票代码。
        cfg: 全局配置。

    Returns:
        Path: ``output/<code>/state.json``。
    """
    return cfg.redchip_output_dir / code / "state.json"


def graph_path(code: str, cfg: config_mod.Settings) -> Path:
    """返回图谱文件路径。"""
    return cfg.redchip_output_dir / code / "graph.json"


def result_path(code: str, cfg: config_mod.Settings) -> Path:
    """返回结构化结果文件路径。"""
    return cfg.redchip_output_dir / code / "result.json"


def load_state(code: str, cfg: config_mod.Settings) -> PipelineState:
    """读取已有中间态，不存在则新建。

    Args:
        code: 股票代码。
        cfg: 全局配置。

    Returns:
        PipelineState: 流水线状态。
    """
    path = state_path(code, cfg)
    if path.exists():
        return PipelineState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return PipelineState(code=code)


def save_state(state: PipelineState, cfg: config_mod.Settings) -> PipelineState:
    """持久化中间态。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        PipelineState: 原状态对象（便于链式调用）。
    """
    path = state_path(state.code, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def load_report(code: str, cfg: config_mod.Settings) -> CompanyReport | None:
    """读取已生成的结果。

    Args:
        code: 股票代码。
        cfg: 全局配置。

    Returns:
        CompanyReport | None: 不存在时返回 None。
    """
    path = result_path(code, cfg)
    if not path.exists():
        return None
    return CompanyReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def stage_fetch(state: PipelineState, cfg: config_mod.Settings) -> PipelineState:
    """阶段一：抓取境外披露文件并建立 FTS 索引。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        PipelineState: 更新后的状态。
    """
    pages = _fetch_pages(state, cfg)
    if pages:
        _run_fts(state, pages, cfg)
        if not fts_mod.has_vie_evidence(_index_conn(state, cfg)):
            state.errors.append("未检索到 VIE 协议关键词，已跳过协议提取环节")
    return save_state(state, cfg)


def stage_candidates(
    state: PipelineState,
    cfg: config_mod.Settings,
    name: str = "",
    keywords: list[str] | None = None,
) -> PipelineState:
    """阶段二：检索境内 WFOE 候选（免费接口）。

    Args:
        state: 流水线状态。
        cfg: 全局配置。
        name: 企业中文名。
        keywords: 额外检索关键词。

    Returns:
        PipelineState: 更新后的状态。
    """
    cnbiz = cnbiz_mod.CnbizClient(cfg)
    wanted = list(keywords or [])
    if not wanted and name:
        wanted = [name]
    wanted += _extract_company_names(state.fts_text)[:5]
    state.candidates = collect_candidates(wanted, cnbiz, limit=5)[:8]
    return save_state(state, cfg)


def stage_llm_a(
    state: PipelineState, cfg: config_mod.Settings, name: str = ""
) -> PipelineState:
    """阶段三：LLM-A 架构提取（失败自动降级为规则兜底）。

    Args:
        state: 流水线状态。
        cfg: 全局配置。
        name: 企业中文名。

    Returns:
        PipelineState: 更新后的状态。
    """
    state.llm_a = _run_llm_a(state, cfg, name)
    return save_state(state, cfg)


def stage_build(state: PipelineState, cfg: config_mod.Settings, name: str = "") -> PipelineState:
    """阶段四（整体）：建图 → 广东过滤 → 境内穿透 → UBO → 评分。

    拆分为三个子阶段，便于 GitHub Actions 分步执行与排障。

    Args:
        state: 流水线状态。
        cfg: 全局配置。
        name: 企业中文名。

    Returns:
        PipelineState: 更新后的状态。
    """
    state = stage_filter(state, cfg, name)
    state = stage_penetrate(state, cfg)
    state = stage_ubo(state, cfg, name)
    return state


def stage_llm_b(state: PipelineState, cfg: config_mod.Settings) -> PipelineState:
    """阶段五：LLM-B 路径审查 + 中文报告。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        PipelineState: 更新后的状态。
    """
    report = load_report(state.code, cfg)
    if report is None:
        state.errors.append("LLM-B 前置结果缺失，请先执行 stage_build")
        return save_state(state, cfg)
    graph = Graph.load(graph_path(state.code, cfg))
    report.report_md = _run_llm_b(state, report, graph, cfg) or _fallback_report(report, graph)

    out_dir = cfg.redchip_output_dir / state.code
    (out_dir / "report.md").write_text(report.report_md, encoding="utf-8")
    result_path(state.code, cfg).write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return save_state(state, cfg)


def stage_export(state: PipelineState, cfg: config_mod.Settings) -> dict[str, Path]:
    """阶段六：渲染图谱并同步图库。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        dict[str, Path]: 产出的图文件（格式 → 路径）。
    """
    out_dir = cfg.redchip_output_dir / state.code
    graph = Graph.load(graph_path(state.code, cfg))
    report = load_report(state.code, cfg)
    title = f"{report.name or state.code}（{state.code}）红筹架构穿透图" if report else "红筹架构穿透图"
    produced = render_mod.render_all(
        graph, out_dir, ubos=report.ubos if report else None, title=title
    )
    graph.sync_neo4j()
    return produced


def stage_filter(state: PipelineState, cfg: config_mod.Settings, name: str = "") -> PipelineState:
    """阶段四之一：按 LLM-A 结果建图，并执行广东省地域过滤。

    过滤器放在 LLM-A 之后，避免提前过滤导致 WFOE 消歧找不到正确实体。

    Args:
        state: 流水线状态。
        cfg: 全局配置。
        name: 企业中文名（暂未使用，保持签名一致）。

    Returns:
        PipelineState: 更新后的状态。
    """
    graph = Graph()
    cnbiz = cnbiz_mod.CnbizClient(cfg)
    wfoe_ids = _build_graph_from_llm_a(graph, state.llm_a, state.candidates, cnbiz)
    matched = filter_guangdong(state.candidates, cnbiz)
    state.gd_credit_codes = [m.credit_code for m in matched]
    state.ubo_start_ids = wfoe_ids
    graph.save(graph_path(state.code, cfg))
    return save_state(state, cfg)


def stage_penetrate(state: PipelineState, cfg: config_mod.Settings) -> PipelineState:
    """阶段四之二：对广东省内实体递归采集境内股东。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        PipelineState: 更新后的状态。
    """
    graph = Graph.load(graph_path(state.code, cfg))
    cnbiz = cnbiz_mod.CnbizClient(cfg)
    for credit_code in state.gd_credit_codes:
        expand_upward(graph, credit_code, cnbiz, max_depth=6)
        basic = cnbiz.get_company_basic(credit_code)
        _enrich(graph, credit_code, basic.name, basic.province, basic.city)
    graph.save(graph_path(state.code, cfg))
    return save_state(state, cfg)


def stage_graph_sync(state: PipelineState, cfg: config_mod.Settings) -> int:
    """阶段四之三：把图谱同步到 Neo4j（未配置图库时静默跳过）。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        int: 写入的关系数量。
    """
    graph = Graph.load(graph_path(state.code, cfg))
    return graph.sync_neo4j()


def stage_ubo(state: PipelineState, cfg: config_mod.Settings, name: str = "") -> PipelineState:
    """阶段四之四：UBO 穿透 + 置信度评分 + 结构化结果落盘。

    Args:
        state: 流水线状态。
        cfg: 全局配置。
        name: 企业中文名。

    Returns:
        PipelineState: 更新后的状态。
    """
    graph = Graph.load(graph_path(state.code, cfg))
    cnbiz = cnbiz_mod.CnbizClient(cfg)

    matched_names = [
        n.name for n in graph.companies() if n.credit_code in set(state.gd_credit_codes)
    ]
    report = CompanyReport(
        code=state.code,
        name=name or state.llm_a.listed_entity.name,
        market=Market.HK,
        doc_kind=state.doc_kind,
        doc_url=state.doc_url,
        doc_published_at=state.doc_published_at,
        llm_a=state.llm_a,
        is_guangdong=bool(state.gd_credit_codes),
        guangdong_entities=matched_names,
    )
    for start_id in state.ubo_start_ids or list(graph.nodes)[:1]:
        report.ubos.extend(penetrate_ubo(graph, start_id))
    report.ubos = _merge_ubos(report.ubos)
    report.nodes = list(graph.nodes.values())
    report.owns = graph.owns
    report.controls = graph.controls
    report.mermaid = render_mod.to_mermaid(graph, title=f"{report.name} 红筹架构")

    origins: list[str] = []
    if state.doc_kind:
        origins.append("prospectus" if state.doc_kind == "prospectus" else "annual_report")
    if cnbiz.is_live:
        origins.append("cnbiz")
    if state.llm_a.needs_review:
        origins.append("llm")
    conf_mod.evaluate(report, origins, state.candidates)
    if state.llm_a.needs_review:
        report.needs_review = True
        report.review_reasons.append(f"LLM-A：{state.llm_a.review_reason}")

    out_dir = cfg.redchip_output_dir / state.code
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path(state.code, cfg).write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "llm_a.json").write_text(
        json.dumps(state.llm_a.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return save_state(state, cfg)


# ---------------------------------------------------------------------------
# 本地分析支持：把 LLM 的输入固化成分析包，并允许回填分析结果
# ---------------------------------------------------------------------------


def manual_llm_a_path(code: str, cfg: config_mod.Settings) -> Path:
    """返回「本地分析产出的 LLM-A 结果」路径。

    Args:
        code: 股票代码。
        cfg: 全局配置。

    Returns:
        Path: ``output/<code>/llm_a.manual.json``。
    """
    return cfg.redchip_output_dir / code / "llm_a.manual.json"


def manual_report_path(code: str, cfg: config_mod.Settings) -> Path:
    """返回「本地分析产出的报告」路径。

    Args:
        code: 股票代码。
        cfg: 全局配置。

    Returns:
        Path: ``output/<code>/report.manual.md``。
    """
    return cfg.redchip_output_dir / code / "report.manual.md"


def stage_pack(state: PipelineState, cfg: config_mod.Settings, name: str = "") -> Path:
    """导出 LLM 分析包：把需要交给 LLM 的全部输入固化成 JSON + 可读 Markdown。

    用途：在没有 API Key（或不希望调用外部模型）时，把材料导出后由本地完成分析，
    再把结果写回 ``llm_a.manual.json`` / ``report.manual.md``，流水线会优先采用。

    Args:
        state: 流水线状态。
        cfg: 全局配置。
        name: 企业中文名。

    Returns:
        Path: 分析包 Markdown 路径。
    """
    out_dir = cfg.redchip_output_dir / state.code
    out_dir.mkdir(parents=True, exist_ok=True)

    pack = {
        "code": state.code,
        "name": name,
        "market": "hk",
        "doc": {
            "kind": state.doc_kind,
            "url": state.doc_url,
            "published_at": state.doc_published_at,
        },
        "fts_hits": state.fts_hits,
        "fts_text": state.fts_text,
        "candidates": [c.model_dump() for c in state.candidates],
        "prompts": {
            "llm_a": llm_mod.load_prompt("llm_a.md"),
            "llm_b": llm_mod.load_prompt("llm_b.md"),
        },
        "output_contract": {
            "llm_a": "写回 output/<code>/llm_a.manual.json（严格遵循 prompts.llm_a 的 JSON Schema）",
            "llm_b": "写回 output/<code>/report.manual.md（严格遵循 prompts.llm_b 的分节格式）",
        },
    }
    (out_dir / "llm_pack.json").write_text(
        json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    candidates_md = "\n".join(
        f"| {i} | {c.name} | {c.credit_code or '—'} | {c.province or '—'}/{c.city or '—'} | "
        f"{c.business_scope[:60]} |"
        for i, c in enumerate(state.candidates)
    ) or "| — | 无候选 | — | — | — |"

    md = f"""# 红筹架构分析包：{state.code} {name}

## 一、披露文件
- 类型：{state.doc_kind or '未知'}
- 发布：{state.doc_published_at or '未知'}
- 链接：{state.doc_url or '—'}

## 二、命中段落（FTS 检索自招股书/年报，已按 6k token 预算裁剪）

{state.fts_text or '（无）'}

## 三、境内 WFOE 候选（CNBizAPI，已裁剪字段）

| # | 名称 | 统一社会信用代码 | 省/市 | 经营范围 |
| --- | --- | --- | --- | --- |
{candidates_md}

## 四、LLM-A Prompt

```text
{pack['prompts']['llm_a']}
```

## 五、产物回填路径
- `output/{state.code}/llm_a.manual.json` —— 严格按上述 Schema 输出
- `output/{state.code}/report.manual.md` —— 严格按 llm_b Prompt 的分节输出
"""
    path = out_dir / "llm_pack.md"
    path.write_text(md, encoding="utf-8")
    return path


def stage_crosscheck(state: PipelineState, cfg: config_mod.Settings) -> PipelineState:
    """阶段七：披露口径 ↔ 工商登记口径交叉核对。

    - 年报「主要股东权益」章节是《证券及期货条例》第XV部申报数据的法定镜像，
      无需直连 DI 系统（旧接口已弃用）即可获得同一权威口径；
    - 登记股东合计校验：比例之和应 ≈ 100%；
    - error 级问题写入需人工复核原因并拉低一致性得分。

    Args:
        state: 流水线状态。
        cfg: 全局配置。

    Returns:
        PipelineState: 更新后的状态。
    """
    report = load_report(state.code, cfg)
    if report is None:
        state.errors.append("交叉验证前置结果缺失，请先执行 stage_ubo")
        return save_state(state, cfg)
    graph = Graph.load(graph_path(state.code, cfg))

    # 用股东权益关键词再做一次定向召回（与架构关键词分开，避免互相挤占预算）
    index_path = cfg.redchip_data_dir / "index" / f"{state.code}.sqlite"
    if index_path.exists():
        import sqlite3

        conn = sqlite3.connect(index_path)
        hits = fts_mod.search(conn, keywords=verify_mod.SHAREHOLDER_KEYWORDS, max_tokens=3000)
        fts_payload = [h.model_dump() for h in hits]
        state.fts_hits = state.fts_hits or fts_payload
    else:
        fts_payload = []

    from redchip.verify import crosscheck as cc_mod

    cc = cc_mod.crosscheck(report, graph, fts_payload, cfg)

    result_path(state.code, cfg).write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"{state.code} 交叉验证：披露口径 {len(cc.disclosed)} 条，"
        f"问题 {len(cc.issues)} 项（{'通过' if cc.passed else '需复核'}）"
    )
    return save_state(state, cfg)
