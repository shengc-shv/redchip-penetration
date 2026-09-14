"""封面页权威注册地抽取（hkex_cover）单测。

覆盖点
------
1. 封面页固定句式识别（开曼 / 百慕大 / 境内 / 香港）；
2. pypdf 在词内插空格 + 弯引号（"People ’s"）时仍能命中 —— 这是实锤坑；
3. **取最靠前的强命中**：封面句优先于正文/子公司的法域提及（防假阳性红筹）；
4. 放宽版只在封面标题窗口内采信。
"""

from __future__ import annotations

from redchip.overseas.hkex_cover import cover_domicile_from_text

# 实测原文（108344 深圳小阔科技，pypdf 抽出，含词内空格与弯引号）
_COVER_PRC = (
    "Application Proof of Shenzhen Xiaokuo Technology Co., Ltd. 深 圳 小 阔 科 技 股 份 有 限 公 司 "
    "(the ''Company '') (A joint stock company incorporated in the People ’s Republic of China "
    "with limited liability)"
)
_COVER_CAYMAN = (
    "Application Proof of Guangzhou Xaircraft Technology Co., Ltd. "
    "(Incorporated in the Cayman Islands with limited liability)"
)
_COVER_BERMUDA = "Guangdong Tinoos Group Co., Ltd. (Incorporated in Bermuda with limited liability)"
_COVER_HK = "Example Holdings Limited (Incorporated in Hong Kong with limited liability)"


def test_cover_prc_with_inner_spaces() -> None:
    assert cover_domicile_from_text(_COVER_PRC)[0] == "中国(境内)"


def test_cover_cayman() -> None:
    assert cover_domicile_from_text(_COVER_CAYMAN)[0] == "开曼群岛"


def test_cover_bermuda() -> None:
    assert cover_domicile_from_text(_COVER_BERMUDA)[0] == "百慕大"


def test_cover_hong_kong() -> None:
    assert cover_domicile_from_text(_COVER_HK)[0] == "香港"


def test_takes_earliest_strong_hit() -> None:
    """封面句在前、正文子公司提及在后 → 必须取封面句（防假阳性红筹）。"""
    text = _COVER_PRC + (
        " We conduct our operations through our subsidiaries, including a company "
        "incorporated in Hong Kong with limited liability and a shareholder "
        "incorporated in the Cayman Islands with limited liability."
    )
    assert cover_domicile_from_text(text)[0] == "中国(境内)"


def test_relaxed_only_within_cover_window() -> None:
    """放宽版（无 with limited liability）只在封面标题窗口内采信。"""
    early = "XYZ Limited (a company incorporated in the Cayman Islands)"
    assert cover_domicile_from_text(early)[0] == "开曼群岛"
    late = ("A" * 1200) + " a company incorporated in the Cayman Islands"
    assert cover_domicile_from_text(late)[0] == ""


def test_no_match_returns_empty() -> None:
    assert cover_domicile_from_text("Nothing about domicile here.")[0] == ""


# ── VIE 判定（协议控制）单测：口径必须从严 ─────────────────────────────────────

from redchip.overseas.hkex_cover import vie_from_text  # noqa: E402


def test_vie_strong_signal_vie_structure() -> None:
    text = "We conduct our business through our VIE structure in the PRC."
    assert vie_from_text(text)["is_vie"] is True


def test_vie_strong_signal_variable_interest_entity() -> None:
    text = "Shenzhen OpCo is a variable interest entity of the WFOE."
    r = vie_from_text(text)
    assert r["is_vie"] is True and r["evidence"]


def test_vie_weak_signal_needs_local_support() -> None:
    """contractual arrangements 必须与 WFOE/nominee 同现才采信（防普通商务合同误标）。"""
    with_wfoe = "Our WFOE entered into a series of contractual arrangements with the Domestic Company."
    assert vie_from_text(with_wfoe)["is_vie"] is True


def test_vie_ordinary_contracts_not_flagged() -> None:
    """旧口径（裸词 contractual arrangements）会把普通合同误标 —— 必须为 False。"""
    text = ("We entered into contractual arrangements with our suppliers. "
            "We also entered into contractual arrangements with our customers.")
    assert vie_from_text(text)["is_vie"] is False


def test_vie_no_signal() -> None:
    assert vie_from_text("A leading manufacturer of electrical equipment in Guangdong.")["is_vie"] is False


def test_vie_does_not_touch_region_fields() -> None:
    """边界：VIE 只描述控制方式，不得携带任何地域信息。"""
    r = vie_from_text("Our VIEs are consolidated in our financial statements.")
    assert set(r) == {"is_vie", "evidence", "active", "terminated", "hits"}
    assert not any("gd" in k or "domicile" in k for k in r)


def test_vie_terminated_arrangement_not_counted() -> None:
    """已终止的 VIE 安排不计入 is_vie（实测 Aqara 于 2022/2026-03 终止）。"""
    text = ("Historical Contractual Arrangements (a) Shenzhen Lumi Historically and up to March 2022, "
            "Shenzhen Lumi was consolidated to our Group pursuant to certain contractual arrangements. "
            "On March 27, 2026, our Group entered into a termination agreement with Shenzhen Ankasa "
            "to terminate the Shenzhen Ankasa Contractual Arrangements.")
    r = vie_from_text(text)
    assert r["is_vie"] is False and r["terminated"] >= 1


def test_vie_generic_termination_clause_not_treated_as_terminated() -> None:
    """普通「协议可被终止」条款不得把在用的 VIE 判成已终止。"""
    text = ("We conduct our business through contractual arrangements with our WFOE and the Domestic Company. "
            "The Exclusive Business Cooperation Agreement may be terminated by the WFOE in certain events.")
    r = vie_from_text(text)
    assert r["is_vie"] is True


def test_vie_active_arrangement_after_history() -> None:
    """历史安排已终止、但存在在用安排时仍应为 True。"""
    text = ("Historically we relied on contractual arrangements with Shenzhen Lumi. "
            "Currently, our WFOE has entered into contractual arrangements with the Domestic Company, "
            "pursuant to which the Domestic Company is consolidated into our Group.")
    assert vie_from_text(text)["is_vie"] is True


# ── VIE 大小写敏感：实测假阳性反例（2026-09-13 复核当场抓出）──────────────────

def test_vie_verb_vie_not_flagged() -> None:
    """英文动词 vie（"players vie for market share"）不得判为 VIE。"""
    text = "The market is highly competitive as more players vie for a share of the market."
    assert vie_from_text(text)["is_vie"] is False


def test_vie_wordbreak_artifacts_not_flagged() -> None:
    """pypdf 断词残留（"vie w" / "Vie tnam"）不得判为 VIE。"""
    a = "We expanded production, with a vie w to establishing replicable production lines."
    b = "Our manufacturing bases in the PRC and Vie tnam serve global customers."
    assert vie_from_text(a)["is_vie"] is False
    assert vie_from_text(b)["is_vie"] is False


def test_vie_uppercase_token_still_flagged() -> None:
    """大写 VIE / VIEs 仍须命中。"""
    assert vie_from_text("We control our PRC operating entities through the VIE Agreements.")["is_vie"] is True
    assert vie_from_text("Our VIEs are consolidated in our financial statements.")["is_vie"] is True


def test_vie_evidence_prefers_strongest_window() -> None:
    """证据优先取含 variable interest entity 的窗口，而非泛泛提及。"""
    text = ("We entered into contractual arrangements with our WFOE. "
            "The PRC Operating Company is a variable interest entity of the WFOE.")
    assert "variable interest entity" in vie_from_text(text)["evidence"].lower()


def test_vie_historical_defined_term_marks_terminated() -> None:
    """「Historical Contractual Arrangements」是已终止 VIE 的固定定义术语，须记为历史。"""
    text = ("Reorganization and Unwinding of Historical Contractual Arrangements. "
            "We historically conducted the CGT Business under the Historical Contractual Arrangements "
            "through Hangzhou Jiayin and Nanjing Yinling. The unwinding was completed in November 2025.")
    r = vie_from_text(text)
    assert r["is_vie"] is False
    assert r["terminated"] >= 1


def test_vie_historical_term_does_not_kill_active_vie() -> None:
    """SUMMARY/风险因素里存在在用 VIE 时，历史术语不得把 is_vie 拉成 False。"""
    text = ("We control our PRC operating entities through the VIE Agreements. "
            "Historically we also relied on Historical Contractual Arrangements with another entity.")
    assert vie_from_text(text)["is_vie"] is True


def test_vie_unwind_marks_terminated() -> None:
    """「unwind the VIE structure」是拆除动作，须记为已终止。"""
    text = ("Our Directors are of the view that it is in the best interests of our Company to unwind "
            "the VIE structure and to simplify our corporate structure.")
    r = vie_from_text(text)
    assert r["is_vie"] is False and r["terminated"] >= 1


# ── 运营地域线索：分册页数预算 + 集团实体语境（2026-09-14 修正）──────────────────
# 背景：Exegenesis 的「广州嘉因 = 集团主要 PRC 运营实体」写在「历史沿革」分册第 114–133 页，
#      旧口径每册只读 10 页 → 该证据整段漏掉，误判成「广东连接仅为非法人团队」。


def test_opco_history_section_has_deeper_page_budget() -> None:
    """「历史沿革」章的读取页数必须显著大于概要/公司资料章（其内容在百余页之后）。"""
    from redchip.overseas.hkex_cover import _OPCO_SECTION_PAGES

    assert _OPCO_SECTION_PAGES["HISTORY"] >= 40
    assert _OPCO_SECTION_PAGES["HISTORY"] > _OPCO_SECTION_PAGES["SUMMARY"]


def test_group_entity_context_matches_self_reference() -> None:
    """集团自述实体句式必须被识别（our wholly-owned subsidiary / principal PRC operating entity）。"""
    from redchip.overseas.hkex_cover import _GROUP_ENTITY_RE

    assert _GROUP_ENTITY_RE.search(
        "our Company established a wholly-owned subsidiary, Guangzhou Jiayin, in Guangzhou")
    assert _GROUP_ENTITY_RE.search(
        "the Guangzhou Jiayin became our principal PRC operating entity")
    assert _GROUP_ENTITY_RE.search("our PRC subsidiaries in Guangdong")


def test_group_entity_context_excludes_third_party_addresses() -> None:
    """第三方（股东 / 投资方 / 中介）地址不得算作集团实体语境。"""
    from redchip.overseas.hkex_cover import _GROUP_ENTITY_RE

    assert not _GROUP_ENTITY_RE.search(
        "Mr. Zhao served as a partner at Guangzhou Huiqin Consulting Co., Ltd. since 2021")
    assert not _GROUP_ENTITY_RE.search(
        "Guangzhou Finance Holding Group Co., Ltd., a company controlled by the Finance Bureau")


def test_entity_context_hits_counts_only_entity_sentences() -> None:
    """同一段里，只有集团自述实体句内的城市词计入 entity_total。"""
    from redchip.overseas.hkex_cover import _entity_context_hits

    text = ("Our wholly-owned subsidiary, Guangzhou Jiayin, is in Guangdong. "
            "Mr. Zhao was a partner at Guangzhou Huiqin Consulting Co., Ltd.")
    # 第一句：Guangzhou + Guangdong = 2；第二句为第三方地址，不计
    assert _entity_context_hits(text) == 2


def test_entity_context_hits_zero_without_entity_sentence() -> None:
    from redchip.overseas.hkex_cover import _entity_context_hits

    assert _entity_context_hits("The office is at Shenzhen Bay, Nanshan District.") == 0
