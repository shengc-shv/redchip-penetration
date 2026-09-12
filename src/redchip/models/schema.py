"""全链路数据契约（Pydantic v2）。

所有 LLM 返回都必须经由这里的模型校验；校验失败即标记 ``needs_review=True``，
绝不把未校验的数据写进图库。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redchip import config as config_mod


class Market(str, Enum):
    """上市地。"""

    HK = "hk"
    US = "us"


class EntityKind(str, Enum):
    """股权链条中的实体角色。"""

    LISTED = "listed"  # 上市主体（开曼/BVI 控股公司）
    OFFSHORE = "offshore"  # 离岸中间层（BVI / Cayman）
    HK = "hk"  # 香港中间控股公司
    WFOE = "wfoe"  # 外商独资企业
    OPCO = "opco"  # 境内运营实体（VIE 被控制方）
    PERSON = "person"  # 自然人
    GOV = "gov"  # 国资 / 政府主体
    UNKNOWN = "unknown"


class Jurisdiction(str, Enum):
    """注册地。"""

    CAYMAN = "Cayman"
    BVI = "BVI"
    HK = "HK"
    CN = "CN"
    BERMUDA = "Bermuda"
    OTHER = "其他"


class SourceRef(BaseModel):
    """字段级来源溯源，便于人工复核。"""

    page: str | None = Field(default=None, description="招股书页码或段落编号")
    field: str | None = Field(default=None, description="该来源支撑的字段名")
    origin: Literal["prospectus", "annual_report", "cnbiz", "edgar", "offshore"] = "prospectus"


class CompanyNode(BaseModel):
    """公司节点。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="稳定唯一标识，优先统一社会信用代码，离岸实体用 name+jurisdiction")
    name: str
    jurisdiction: Jurisdiction = Jurisdiction.OTHER
    kind: EntityKind = EntityKind.UNKNOWN
    credit_code: str | None = None
    province: str | None = None
    city: str | None = None
    reg_status: str | None = None
    established_at: str | None = None
    business_scope: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return str(v).strip()


class PersonNode(BaseModel):
    """自然人节点。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    kind: Literal[EntityKind.PERSON] = EntityKind.PERSON
    is_ubo_candidate: bool = False
    sources: list[SourceRef] = Field(default_factory=list)


class OwnEdge(BaseModel):
    """股权关系边（股东 → 被持股公司）。"""

    model_config = ConfigDict(extra="ignore")

    from_id: str = Field(description="股东节点 id")
    to_id: str = Field(description="被持股公司 id")
    share_pct: float = Field(default=0.0, ge=0.0, le=100.0, description="直接持股比例（百分数）")
    share_raw: str | None = Field(default=None, description="工商原文表述，如 '60.5%'")
    source: Literal["cnbiz", "prospectus", "edgar", "offshore", "llm"] = "cnbiz"
    as_of: str | None = None


class ControlEdge(BaseModel):
    """VIE 协议控制边（WFOE → 境内运营实体）。"""

    model_config = ConfigDict(extra="ignore")

    from_id: str
    to_id: str
    contract_type: Literal[
        "独家服务协议", "股权质押", "投票权委托", "独家购买权", "配偶同意函", "其他"
    ] = "其他"
    note: str | None = None
    source: Literal["prospectus", "annual_report", "llm"] = "prospectus"


# ---------------------------------------------------------------------------
# LLM-A 输出契约（严格对应 llm/prompts/llm_a.md 的 Schema）
# ---------------------------------------------------------------------------


class ListedEntity(BaseModel):
    """上市主体。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    jurisdiction: str = ""
    type: str = "上市主体"


class IntermediateEntity(BaseModel):
    """中间层实体。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    jurisdiction: str = ""
    type: str = ""
    parent: str = ""


class WfoeMatch(BaseModel):
    """WFOE 消歧结果。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    credit_code: str = ""
    matched_candidate_index: int = -1
    match_reason: str = ""


class VieContract(BaseModel):
    """VIE 协议控制。"""

    model_config = ConfigDict(extra="ignore")

    type: str = "其他"
    party_a: str = ""
    party_b: str = ""
    shareholders: list[str] = Field(default_factory=list)


class UboCandidate(BaseModel):
    """UBO 初判候选。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    path: str = ""
    estimated_share: str = ""


class LlmAOutput(BaseModel):
    """LLM-A 的结构化输出。

    ``confidence`` 低于阈值或校验异常时，上层会置 ``needs_review=True``。
    """

    model_config = ConfigDict(extra="ignore")

    listed_entity: ListedEntity = Field(default_factory=ListedEntity)
    intermediate_entities: list[IntermediateEntity] = Field(default_factory=list)
    wfoe: list[WfoeMatch] = Field(default_factory=list)
    vie_contracts: list[VieContract] = Field(default_factory=list)
    ubo_candidates: list[UboCandidate] = Field(default_factory=list)
    chain_summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_pages: list[str] = Field(default_factory=list)

    needs_review: bool = False
    review_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_nested(cls, data: Any) -> Any:
        """兼容调用方以原始 dict 追加子对象。

        Pydantic v2 不会对 ``list.append`` 后的元素做校验，这里在整体校验前统一归一化，
        避免出现「字段是 dict 而非模型」导致的属性访问崩溃。

        Args:
            data: 输入数据。

        Returns:
            Any: 归一化后的数据。
        """
        if not isinstance(data, dict):
            return data
        nested: dict[str, type[BaseModel]] = {
            "listed_entity": ListedEntity,
            "intermediate_entities": IntermediateEntity,
            "wfoe": WfoeMatch,
            "vie_contracts": VieContract,
            "ubo_candidates": UboCandidate,
        }
        for key, model in nested.items():
            value = data.get(key)
            if isinstance(value, dict):
                data[key] = model.model_validate(value)
            elif isinstance(value, list):
                data[key] = [
                    model.model_validate(item) if isinstance(item, dict) else item for item in value
                ]
        return data


class CandidateCompany(BaseModel):
    """CNBizAPI 搜索返回的候选公司（已裁剪为三字段以压缩 token）。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    credit_code: str = ""
    business_scope: str = ""
    province: str | None = None
    city: str | None = None


# ---------------------------------------------------------------------------
# 穿透与置信度
# ---------------------------------------------------------------------------


class UboResult(BaseModel):
    """一条穿透到底的 UBO 结果。"""

    model_config = ConfigDict(extra="ignore")

    person_id: str
    name: str
    cumulative_share: float = Field(description="沿路径累乘后的最终持股比例（0~1）")
    path: list[str] = Field(default_factory=list, description="从上市主体到该自然人的节点 id 序列")
    path_names: list[str] = Field(default_factory=list)
    depth: int = 0
    reached_via_vie: bool = Field(default=False, description="路径中是否包含 VIE 协议控制段")


class ConfidenceDetail(BaseModel):
    """置信度打分明细（权重：权威性 30% / 一致性 25% / 完整性 25% / 时效性 20%）。"""

    model_config = ConfigDict(extra="ignore")

    authority: float = Field(default=0.0, ge=0.0, le=100.0)
    consistency: float = Field(default=0.0, ge=0.0, le=100.0)
    completeness: float = Field(default=0.0, ge=0.0, le=100.0)
    timeliness: float = Field(default=0.0, ge=0.0, le=100.0)
    total: float = Field(default=0.0, ge=0.0, le=100.0)

    @classmethod
    def compute(
        cls,
        authority: float,
        consistency: float,
        completeness: float,
        timeliness: float,
    ) -> ConfidenceDetail:
        """按固定权重合成总分。

        Args:
            authority: 数据源权威性得分（0~100）。
            consistency: 多源一致性得分（0~100）。
            completeness: 穿透完整性得分（0~100）。
            timeliness: 时效性得分（0~100）。

        Returns:
            ConfidenceDetail: 含加权总分的明细对象。
        """
        total = 0.30 * authority + 0.25 * consistency + 0.25 * completeness + 0.20 * timeliness
        return cls(
            authority=authority,
            consistency=consistency,
            completeness=completeness,
            timeliness=timeliness,
            total=round(total, 2),
        )


class CompanyReport(BaseModel):
    """单家企业的完整穿透结果。"""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(description="股票代码，如 00700")
    name: str = ""
    market: Market = Market.HK
    doc_kind: str = ""
    doc_url: str = ""
    doc_published_at: str = ""

    llm_a: LlmAOutput = Field(default_factory=LlmAOutput)
    nodes: list[CompanyNode | PersonNode] = Field(default_factory=list)
    owns: list[OwnEdge] = Field(default_factory=list)
    controls: list[ControlEdge] = Field(default_factory=list)
    ubos: list[UboResult] = Field(default_factory=list)
    confidence: ConfidenceDetail = Field(default_factory=ConfidenceDetail)

    is_guangdong: bool = False
    guangdong_entities: list[str] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    mermaid: str = ""
    report_md: str = ""
    generated_at: str = Field(
        default_factory=lambda: datetime.now(config_mod.CST).isoformat(timespec="seconds")
    )

    @property
    def score(self) -> float:
        """返回置信度总分。"""
        return self.confidence.total


class PipelineState(BaseModel):
    """流水线中间态，供各 stage 之间以 JSON 传递。"""

    model_config = ConfigDict(extra="ignore")

    code: str
    market: Market = Market.HK
    pdf_path: str | None = None
    doc_url: str = ""
    doc_kind: str = ""
    doc_published_at: str = ""
    fts_hits: list[dict[str, Any]] = Field(default_factory=list)
    fts_text: str = ""
    candidates: list[CandidateCompany] = Field(default_factory=list)
    llm_a: LlmAOutput = Field(default_factory=LlmAOutput)
    llm_b_raw: str = ""
    # 通过广东省过滤的境内实体信用代码（供后续穿透阶段使用）
    gd_credit_codes: list[str] = Field(default_factory=list)
    # UBO 穿透的起点节点 id
    ubo_start_ids: list[str] = Field(default_factory=list)
    report: CompanyReport | None = None
    errors: list[str] = Field(default_factory=list)
