"""披露口径与工商登记口径的交叉核对。

三个校验器
----------
1. **合计校验**：登记股东比例之和应 ≈ 100%，否则股东名册不完整；
2. **披露 vs 登记对比**：年报「主要股东权益」章节（第XV部法定镜像）中的持股比例
   与工商登记口径按人名对齐，偏差超过容差即记入问题清单；
3. **结论传递**：error 级问题会拉低置信度的「多源一致性」维度并写入需人工复核原因。

所有问题都只降级、不中断（符合项目「降级优先于中断」约定）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from redchip import config as config_mod
from redchip.graph.store import Graph
from redchip.models.schema import CompanyReport, PersonNode
from redchip.overseas import fts as fts_mod
from redchip.verify.shareholders import (
    SHAREHOLDER_KEYWORDS,
    DisclosedHolder,
    extract_holders,
    extract_narrative_holders,
    is_complete_sum,
    sum_check,
)

# 披露口径与登记口径的允许偏差（百分数点）：
# 两者数据源不同（披露通常按最近权益变动日，工商按最新登记），小数级别差异属正常
PCT_TOLERANCE = 2.0
NAME_HONORIFIC_RE = re.compile(r"(先生|女士|小姐|Mr\.?|Ms\.?|Mrs\.?)$")


class CrosscheckIssue(BaseModel):
    """一条核对问题。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["sum_mismatch", "pct_mismatch", "holder_only_in_disclosure"] = "sum_mismatch"
    severity: Literal["info", "error"] = "info"
    detail: str = ""
    entity: str = ""


class CrosscheckResult(BaseModel):
    """交叉核对结果。"""

    model_config = ConfigDict(extra="ignore")

    issues: list[CrosscheckIssue] = Field(default_factory=list)
    disclosed: list[DisclosedHolder] = Field(default_factory=list)
    checked_entities: list[str] = Field(default_factory=list)
    passed: bool = True


def _normalize_name(name: str) -> str:
    """归一化人名用于对齐（去称谓、去空白）。

    Args:
        name: 原始姓名。

    Returns:
        str: 归一化姓名。
    """
    cleaned = NAME_HONORIFIC_RE.sub("", name.strip())
    return re.sub(r"\s+", "", cleaned)


def _registry_persons(graph: Graph) -> dict[str, list[tuple[str, float]]]:
    """从图谱收集每家公司的自然人登记股东。

    Args:
        graph: 图谱。

    Returns:
        dict[str, list[tuple[str, float]]]: 公司名 → [(股东名, 持股%)]（0% 占位边不计）。
    """
    out: dict[str, list[tuple[str, float]]] = {}
    for edge in graph.owns:
        if edge.share_pct <= 0:
            continue
        person = graph.nodes.get(edge.from_id)
        company = graph.nodes.get(edge.to_id)
        if isinstance(person, PersonNode) and company is not None:
            out.setdefault(company.name, []).append((person.name, edge.share_pct))
    return out


def crosscheck(
    report: CompanyReport,
    graph: Graph,
    fts_hits: list[dict],
    cfg: config_mod.Settings | None = None,
    extra_holders: list[DisclosedHolder] | None = None,
) -> CrosscheckResult:
    """执行交叉核对并就地更新报告的置信度与复核原因。

    Args:
        report: 当前报告（会被修改：confidence.consistency、review_reasons）。
        graph: 图谱。
        fts_hits: FTS 命中（含股东章节段落）。
        cfg: 全局配置。
        extra_holders: 从 HTML 表格结构化解析得到的持股记录（美股主通道）。

    Returns:
        CrosscheckResult: 核对结果。
    """
    result = CrosscheckResult()

    # ---------- 1. 登记口径：合计校验 ----------
    registry = _registry_persons(graph)
    result.checked_entities = list(registry)
    for company, holders in registry.items():
        total = sum_check(
            (DisclosedHolder(name=n, share_pct=p) for n, p in holders)
        )
        if holders and not is_complete_sum(total):
            result.issues.append(
                CrosscheckIssue(
                    type="sum_mismatch",
                    severity="error",
                    entity=company,
                    detail=f"登记股东合计 {total:g}%（容差 ±0.5%），股东名册可能不完整",
                )
            )

    # ---------- 2. 披露口径提取 ----------
    hits = [fts_mod.FtsHit.model_validate(h) for h in fts_hits]
    # 双通道：HTML 表格（美股 20-F 主通道）+ 文本（港股年报 PDF / 叙述式表述）
    html_holders = [h for h in (extra_holders or []) if h.share_pct > 0]
    if html_holders:
        # HTML 表格已给出结构化结果，文本通道退化为仅抽取叙述式表述，避免数字列被当股东名
        text_holders = [
            h for h in _extract_from_hits_by_keywords(hits, narrative_only=True) if h.share_pct > 0
        ]
    else:
        text_holders = [h for h in _extract_from_hits_by_keywords(hits) if h.share_pct > 0]
    result.disclosed = _dedupe_disclosed(html_holders + text_holders)
    if result.disclosed:
        registry_index = {
            _normalize_name(n): (pct, company)
            for company, holders in registry.items()
            for n, pct in holders
        }
        for holder in result.disclosed:
            key = _normalize_name(holder.name)
            entry = registry_index.get(key)
            if entry is None:
                # 披露有、登记无：多为离岸层股东（工商查不到），属信息级提示
                result.issues.append(
                    CrosscheckIssue(
                        type="holder_only_in_disclosure",
                        severity="info",
                        entity=holder.name,
                        detail=(
                            f"披露口径持股 {holder.share_pct:g}%（p.{holder.source_page}），"
                            "在境内工商登记中未找到对应自然人（离岸层属正常现象）"
                        ),
                    )
                )
                continue
            reg_pct, company = entry
            if abs(reg_pct - holder.share_pct) > PCT_TOLERANCE:
                result.issues.append(
                    CrosscheckIssue(
                        type="pct_mismatch",
                        severity="error",
                        entity=company,
                        detail=(
                            f"{holder.name}：披露口径 {holder.share_pct:g}%（p.{holder.source_page}）"
                            f" vs 工商登记 {reg_pct:g}%，偏差超出 ±{PCT_TOLERANCE:g}%"
                        ),
                    )
                )

    # ---------- 3. 结论写入报告 ----------
    result.passed = not any(i.severity == "error" for i in result.issues)
    report.crosscheck = result.model_dump()
    for issue in result.issues:
        if issue.severity == "error":
            report.review_reasons.append(f"交叉验证：{issue.detail}")
    if not result.passed:
        report.needs_review = True
        # error 级问题拉低多源一致性得分（下限 0）
        report.confidence.consistency = max(0.0, report.confidence.consistency - 20.0)
        report.confidence.total = round(
            0.30 * report.confidence.authority
            + 0.25 * report.confidence.consistency
            + 0.25 * report.confidence.completeness
            + 0.20 * report.confidence.timeliness,
            2,
        )
        if report.confidence.total < conf_threshold():
            report.needs_review = True
    return result


def _dedupe_disclosed(holders: list[DisclosedHolder]) -> list[DisclosedHolder]:
    """按「名称 + 比例」去重，优先保留带溯源标记的记录。

    Args:
        holders: 原始记录。

    Returns:
        list[DisclosedHolder]: 去重后的记录。
    """
    best: dict[tuple[str, float], DisclosedHolder] = {}
    for h in holders:
        key = (h.name.lower(), h.share_pct)
        current = best.get(key)
        if current is None or (not current.source_page and h.source_page):
            best[key] = h
    return list(best.values())


def conf_threshold() -> float:
    """复核阈值，与 confidence 模块保持一致。

    Returns:
        float: 阈值 60。
    """
    from redchip.models import confidence as conf_mod

    return conf_mod.REVIEW_THRESHOLD


def _extract_from_hits_by_keywords(
    hits: list[fts_mod.FtsHit], narrative_only: bool = False
) -> list[DisclosedHolder]:
    """优先从股东关键词命中的段落提取；无命中时从全部段落兜底提取。

    Args:
        hits: FTS 命中。
        narrative_only: 仅抽取叙述式表述（HTML 表格通道已提供结构化结果时使用）。

    Returns:
        list[DisclosedHolder]: 提取结果。
    """
    # 优先从股东关键词命中的段落提取，并保留页码溯源（source_page）
    shareholder_hits = [
        h for h in hits if any(kw.lower() in h.snippet.lower() for kw in SHAREHOLDER_KEYWORDS)
    ]
    source = shareholder_hits or hits
    extractor = extract_narrative_holders if narrative_only else extract_holders
    out: list[DisclosedHolder] = []
    for hit in source:
        out.extend(extractor(hit.snippet, source_page=str(hit.page_no)))
    return out
