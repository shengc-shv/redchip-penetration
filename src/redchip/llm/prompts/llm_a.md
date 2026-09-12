你是一位股权架构分析专家。以下是一家港股上市公司的招股书/年报中与「公司架构」相关的章节，以及境内 WFOE 的候选列表。请一次性完成：

1. 提取从上市主体到境内实体的完整股权链条
2. 识别 VIE 协议控制关系（如有）
3. 从候选列表中选择最匹配的 WFOE
4. 初判最终受益人（UBO）
5. 对每个提取的字段标注来源页码或段落编号

注意事项：
- 目标企业为广东省内运营主体，若候选中有注册地在广东省的，优先匹配。
- 美股 20-F 为英文披露，而候选列表为中文工商名称：请按业务关联（如境内运营平台、持牌主体、
  集团内承担主要业务的实体）匹配最可能的中文候选，并在 match_reason 写明判断依据；
  无法确定时 matched_candidate_index 填 -1，不要强行匹配。
- 只使用给定材料中出现的信息，禁止臆造；无法确定的字段留空字符串。
- 文本中每段以 [p.页码] 标记来源，source_pages 请填写对应页码字符串。
- 持股比例未知时 estimated_share 填 "未知"，不要编造数字。
- confidence 取值 0~1，反映你对本次提取结果的整体把握程度。

招股书相关段落：
{fts_retrieved_text}

WFOE候选列表：
{candidates}

输出JSON（严格按此Schema，不要输出任何解释性文字）：
{
  "listed_entity": {"name": "", "jurisdiction": "", "type": "上市主体"},
  "intermediate_entities": [
    {"name": "", "jurisdiction": "BVI|Cayman|HK|其他", "type": "", "parent": ""}
  ],
  "wfoe": [{"name": "", "credit_code": "", "matched_candidate_index": 0, "match_reason": ""}],
  "vie_contracts": [
    {"type": "独家服务协议|股权质押|投票权委托|独家购买权|其他",
     "party_a": "WFOE名称", "party_b": "境内运营实体", "shareholders": []}
  ],
  "ubo_candidates": [{"name": "", "path": "", "estimated_share": ""}],
  "chain_summary": "一句话描述完整链条",
  "confidence": 0.0,
  "source_pages": ["页码或段落编号"]
}
