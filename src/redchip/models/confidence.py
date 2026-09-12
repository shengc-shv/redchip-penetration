"""置信度评分器。

对应方案文档第九节：数据源权威性 30% / 多源一致性 25% / 穿透完整性 25% / 时效性 20%。
总分 < 60 标记「需人工复核」。
"""

from __future__ import annotations

from datetime import datetime

from redchip import config as config_mod
from redchip.models.schema import (
    CandidateCompany,
    CompanyReport,
    ConfidenceDetail,
    Jurisdiction,
    LlmAOutput,
)

# 统一使用项目级北京时间常量，禁止各模块自行构造时区
CST = config_mod.CST

# 数据源权威性基准分
_AUTHORITY_BY_ORIGIN = {
    "prospectus": 100.0,  # 招股书 / 年报：官方披露
    "annual_report": 95.0,
    "edgar": 100.0,
    "cnbiz": 85.0,  # 工商登记 API
    "offshore": 70.0,  # 离岸注册处（开曼/BVI 股东名册不公开）
    "llm": 50.0,
}

REVIEW_THRESHOLD = 60.0


def score_authority(origins: list[str]) -> float:
    """按参与的数据源计算权威性得分。

    Args:
        origins: 本次穿透实际用到的数据源标识列表。

    Returns:
        float: 0~100 的权威性得分，无有效源时为 0。
    """
    if not origins:
        return 0.0
    scores = [_AUTHORITY_BY_ORIGIN.get(o, 40.0) for o in origins]
    # 以最高分为主，多源叠加小幅加成，避免堆砌低质量源刷分
    bonus = min(10.0, 3.0 * (len(set(scores)) - 1))
    return round(min(100.0, max(scores) + bonus), 2)


def score_consistency(llm_a: LlmAOutput, candidates: list[CandidateCompany]) -> float:
    """交叉验证得分：LLM 提取链条 vs 工商登记实体。

    判据：
    - WFOE 命中工商候选且带统一社会信用代码 → 满分基准
    - 命中候选但无信用代码 → 70
    - 完全未命中候选 → 30

    Args:
        llm_a: LLM-A 提取结果。
        candidates: 工商候选列表。

    Returns:
        float: 0~100 的一致性得分。
    """
    if not llm_a.wfoe:
        return 20.0
    matched = [w for w in llm_a.wfoe if 0 <= w.matched_candidate_index < len(candidates)]
    if not matched:
        return 30.0
    with_code = [w for w in matched if w.credit_code]
    if not with_code:
        return 70.0
    return 100.0


def score_completeness(report: CompanyReport) -> float:
    """穿透完整性：链条断点扣分。

    每个断点定义：
    - 存在边指向的节点不在节点集合中
    - 穿透未产出任何 UBO
    - 未识别到 WFOE

    Args:
        report: 已构建图的企业报告。

    Returns:
        float: 0~100 的完整性得分（每个断点扣 10 分）。
    """
    score = 100.0
    node_ids = {n.id for n in report.nodes}
    dangling = {
        e.to_id for e in report.owns if e.to_id not in node_ids
    } | {e.from_id for e in report.owns if e.from_id not in node_ids}
    score -= 10.0 * len(dangling)
    if not any(n.id for n in report.nodes if getattr(n, "kind", None) and "wfoe" in str(n.kind)):
        score -= 10.0
    if not report.ubos:
        score -= 10.0
    # 离岸层缺失（没有任何境外实体）通常意味着招股书架构章节未召回
    if not any(
        getattr(n, "jurisdiction", None) in (Jurisdiction.CAYMAN, Jurisdiction.BVI, Jurisdiction.HK)
        for n in report.nodes
    ):
        score -= 10.0
    return max(0.0, round(score, 2))


def score_timeliness(published_at: str | None) -> float:
    """时效性：6 个月内满分，之后线性衰减。

    Args:
        published_at: 文档发布日期字符串（支持 YYYY-MM-DD / DD/MM/YYYY）。

    Returns:
        float: 0~100 的时效性得分；无法解析日期时给 50 分中性值。
    """
    if not published_at:
        return 50.0
    dt = _parse_date(published_at)
    if dt is None:
        return 50.0
    months = (datetime.now(CST) - dt).days / 30.0
    if months <= 6:
        return 100.0
    return max(0.0, round(100.0 - (months - 6) * 5.0, 2))


def _parse_date(value: str) -> datetime | None:
    """解析常见日期格式。

    Args:
        value: 日期字符串。

    Returns:
        datetime | None: 解析失败的返回 None。
    """
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            # 源日期不带时区，解析后统一按北京时间记账
            return datetime.strptime(
                value[:10] if fmt != "%Y%m%d" else value[:8], fmt
            ).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def evaluate(report: CompanyReport, origins: list[str], candidates: list[CandidateCompany]) -> CompanyReport:
    """为报告计算置信度并就地写回。

    Args:
        report: 待评分的企业报告。
        origins: 本次用到的数据源标识。
        candidates: 工商候选列表（用于一致性校验）。

    Returns:
        CompanyReport: 已填充 confidence 与 needs_review 的报告对象。
    """
    detail = ConfidenceDetail.compute(
        authority=score_authority(origins),
        consistency=score_consistency(report.llm_a, candidates),
        completeness=score_completeness(report),
        timeliness=score_timeliness(report.doc_published_at),
    )
    report.confidence = detail
    if detail.total < REVIEW_THRESHOLD:
        report.needs_review = True
        report.review_reasons.append(f"置信度 {detail.total} 低于阈值 {REVIEW_THRESHOLD}")
    return report
