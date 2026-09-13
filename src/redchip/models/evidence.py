"""证据等级：给每份穿透结论标注"分量"。

为什么需要它
------------
同样一份报告，可能建立在完全不同的证据基础上：一份是"披露文件 + 工商登记 + 双向核验"，
另一份可能只有工商登记里一条外资股东线索。两者放在一起看，读者容易误判。

因此按证据充分度分为四级，并在报告中显式标注依据：

- **L1 完整穿透**：官方披露文件 + 境内工商登记 + 至少一个 UBO + 交叉核验通过
- **L2 部分穿透**：有官方披露，但境内层或 UBO 环节不完整
- **L3 境内可见**：无官方披露，仅凭境内工商登记的股权结构反推（如 WFOE 股东为境外公司）
- **L4 仅线索**：连境内股权结构都未取得，只有主体名称等基本信息

等级只描述"证据有多足"，不评价业务价值；L4 的企业依然可能是好客户，只是需要人工补料。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from redchip.models.schema import CompanyReport


class EvidenceLevel(str, Enum):
    """证据等级。"""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


LEVEL_LABELS: dict[EvidenceLevel, str] = {
    EvidenceLevel.L1: "完整穿透",
    EvidenceLevel.L2: "部分穿透",
    EvidenceLevel.L3: "境内可见（工商反推）",
    EvidenceLevel.L4: "仅线索",
}

LEVEL_NOTES: dict[EvidenceLevel, str] = {
    EvidenceLevel.L1: "官方披露 + 工商登记 + 交叉核验三方齐备，结论可直接用于业务判断",
    EvidenceLevel.L2: "官方披露与工商登记未完全打通，结论方向可信，细节需人工复核",
    EvidenceLevel.L3: "无官方披露，仅由境内工商登记的境外股东反推架构，属疑似红筹",
    EvidenceLevel.L4: "既无官方披露，也未取得境内股权结构证据，仅可作线索跟进",
}


class EvidenceAssessment(BaseModel):
    """证据等级评定结果。"""

    model_config = ConfigDict(extra="ignore")

    level: EvidenceLevel = EvidenceLevel.L4
    label: str = ""
    note: str = ""
    basis: list[str] = Field(default_factory=list)


def evaluate_evidence(report: CompanyReport) -> EvidenceAssessment:
    """按证据充分度评定 L1~L4。

    Args:
        report: 穿透结果。

    Returns:
        EvidenceAssessment: 等级、说明与判定依据。
    """
    has_official_doc = bool(report.doc_url and report.doc_kind)
    # 境内工商登记证据：识别到目标省份实体，或股权边来自工商数据源
    has_registry = bool(report.guangdong_entities) or any(e.source == "cnbiz" for e in report.owns)
    has_ubo = bool(report.ubos)
    crosscheck = report.crosscheck or {}
    cc_passed = crosscheck.get("passed") is True and bool(crosscheck.get("disclosed"))
    onshore = list(getattr(report, "onshore_signals", []) or [])

    basis: list[str] = []
    basis.append(("✅ " if has_official_doc else "✗ ") + f"官方披露文件：{report.doc_kind or '未取得'}")
    basis.append(("✅ " if has_registry else "✗ ") + "境内工商登记（股东穿透）")
    basis.append(("✅ " if has_ubo else "✗ ") + f"最终受益人：{len(report.ubos)} 位")
    basis.append(("✅ " if cc_passed else "✗ ") + "披露 ↔ 登记交叉核验")
    if onshore:
        basis.append(f"✅ 境内反推信号：{len(onshore)} 条")

    if has_official_doc and has_registry and has_ubo and cc_passed:
        level = EvidenceLevel.L1
    elif has_official_doc:
        # 只要拿到官方披露即至少 L2：披露本身已提供架构与股东信息，
        # 缺的只是境内工商核验或 UBO 穿透
        level = EvidenceLevel.L2
    elif onshore:
        level = EvidenceLevel.L3
    else:
        level = EvidenceLevel.L4

    return EvidenceAssessment(
        level=level,
        label=LEVEL_LABELS[level],
        note=LEVEL_NOTES[level],
        basis=basis,
    )
