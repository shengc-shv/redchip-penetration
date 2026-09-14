"""生成「全量摸底明细表 · 字段中文说明」（自校验：CSV 新增字段若缺说明会报错）。

字段清单直接取自 ``make_full_listing.CSV_FIELDS``，避免文档与代码漂移。

产出
----
``output/字段说明_全量摸底表.md``

用法::

    PYTHONPATH=src python scripts/make_field_dictionary.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redchip import config as config_mod  # noqa: E402

import make_full_listing as M  # noqa: E402  （同目录脚本）

# 字段 → (中文名, 含义, 取值/口径, 来源与可信度)
SPEC: dict[str, tuple[str, str, str, str]] = {
    "id": ("企业编号", "港交所披露易为新上市申请分配的唯一编号，**跨年度唯一**，是各表勾稽主键",
           "整数（如 108832）", "官方清单 JSON"),
    "name_en": ("英文企业名", "申请人的英文名称，以官方清单为准（可能与最终招股书微调）",
                "文本", "官方清单 JSON"),
    "name_cn": ("中文企业名", "申请人中文名称，取自官方中文版清单；官方未给中文名时回落英文名",
                "文本", "官方清单 JSON（中文版）"),
    "board": ("上市板块", "申请所在交易板块", "主板 / GEM", "官方清单 JSON"),
    "status": ("申请状态", "申请当前进展。**已按口径剔除「失效/撤回/被拒绝/被发回」**",
               "处理中 / 处理中(含PHIP) / 已上市", "官方清单 JSON"),
    "stock_code": ("股票代码", "已上市企业的港交所股票代码；尚未上市为空",
                   "5 位代码（如 00700）", "官方清单 JSON（listed 的 st 字段）"),
    "submit_date": ("递表日", "上市申请首次递交日，**取官方字段，非抓取日**",
                    "YYYY-MM-DD（由官方 DD/MM/YYYY 归一化）", "官方清单 JSON（d 字段）"),
    "gd_flag": ("广东名称标记", "企业名称是否含广东地名。**仅作结果表一列，不作筛选闸门**",
                "是 / 空", "名称启发式（23 个粤城市词中英匹配）"),
    "gd_l1_hit": ("名称命中词", "名称匹配命中的具体地名，便于复核",
                  "文本（如 廣州 / Shenzhen）", "名称启发式"),
    "is_offshore": ("是否离岸注册", "上市主体注册地是否为开曼/百慕大，即**红筹形态的必要条件**",
                    "是 / 空", "封面页权威口径（可信）"),
    "domicile": ("注册地（封面页）", "上市主体注册地。**只认封面页/扉页固定句式**，不采信正文其他章节的法域提及",
                 "开曼群岛 / 百慕大 / 中国(境内) / 香港 / 空(待核验)", "招股书封面页（权威）"),
    "scan_method": ("判定通道", "注册地取数所走的通道", "cover（封面分册）/ cover-full（整份首页）/ 空",
                    "本项目流程"),
    "cover_section": ("封面依据分册", "命中注册地句式所在的分册名，可据此回原文定位",
                      "WARNING / IMPORTANT / full-pdf / listedco:prospectus", "披露易 Multi-Files"),
    "cover_evidence": ("封面原文句", "注册地判定所依据的原文片段，**可直接用于复核**",
                       "文本（英文原句）", "招股书原文"),
    "gd_opco": ("广东运营线索", "含广东地名且带实体/地址语境的原文片段",
                "文本", "概要 / 公司资料 / 历史沿革分册（**线索，非结论**）"),
    "gd_opco_count": ("广东线索词频", "广东城市词在上述分册中出现的次数，作为线索强度",
                      "整数（阈值 ≥3 记为线索）", "同上（**线索，非结论**）"),
    "gd_opco_entity_count": (
        "广东线索·实体语境词频",
        "`gd_opco_count` 中落在**集团自述实体**句内的次数（句含 our subsidiary / "
        "wholly-owned / operating entity / our Group 等）。用于把「本公司运营实体在粤」"
        "与「股东/投资方/中介地址在粤」分开——历史沿革章的实体表述不算噪音",
        "整数（阈值 ≥3 可作为核心层依据）",
        "本项目推导（**线索，非结论**）"),
    "gd_opco_source": ("线索来源与构成", "格式为「分册名 | 城市词×次数」，便于判断依据",
                       "文本（如 SUMMARY | shenzhen×34, guangdong×1）", "同上"),
    "gd_tier": ("广东线索分层", "按命中分册的权威性分层：`核心` = 命中「概要 / 公司资料」"
               "（公司自述总部/运营实体/主要银行）；`补充` = 仅命中「历史沿革」"
               "（该章含股东/投资方/中介地址，噪音高）；`核心(沿革章·实体内述)` = 虽命中「历史沿革」"
               "但其上下文为集团自述实体（`gd_opco_entity_count ≥ 3`），非噪音",
               "核心 / 补充(仅历史沿革章) / 补充(listedco全文) / 空", "本项目推导（**分层规则见筛选条件文档**）"),
    "is_vie": ("是否 VIE", "上市主体**当前**是否通过协议控制（VIE）控制境内运营实体。"
               "只描述控制方式，**不参与地域判定**",
               "是 / 否 / 否(仅历史安排) / 待核验", "概要+风险因素章专有表述（可信度中高）"),
    "vie_scanned": ("VIE 是否已判定", "区分「未扫」与「扫过=否」；未扫的 is_vie 不可用",
                    "是 / 空", "本项目流程"),
    "vie_terminated": ("是否仅历史 VIE", "曾存在协议控制安排但披露为已终止/历史安排，**不计入当前 VIE**",
                       "是 / 空", "招股书历史沿革章"),
    "vie_source": ("VIE 依据分册", "VIE 结论所在分册，可据此回原文定位",
                   "SUMMARY / RISK FACTORS / HISTORY… / listedco:prospectus", "披露易 Multi-Files"),
    "vie_evidence": ("VIE 原文证据", "VIE 判定所依据的原文片段，**可直接用于复核**",
                     "文本（英文原句）", "招股书原文"),
    "redchip_l2": ("红筹判定", "由「离岸注册 + 广东运营线索」推导："
                   "是(线索待核) / 疑似 / 否-H股 / 否-其他 / 待核验",
                   "文本", "本项目推导（**线索级**）"),
    "doc_available": ("文档可得性", "能否取得用于判定的招股书文档",
                      "有申请版本 / 仅listedco可补 / 官方未刊发", "官方清单 JSON"),
    "app_proof_url": ("申请版本链接", "披露易申请版本 PDF 直链，可点开核对原文",
                      "URL", "官方清单 JSON"),
}


def main() -> None:
    fields = list(M.CSV_FIELDS)
    missing = [f for f in fields if f not in SPEC]
    extra = [f for f in SPEC if f not in fields]
    if missing:
        raise SystemExit(f"❌ 以下 CSV 字段缺少中文说明，请补 SPEC：{missing}")
    if extra:
        print(f"⚠️ SPEC 中有 {len(extra)} 个字段不在当前 CSV：{extra}")

    settings = config_mod.get_settings()
    lines: list[str] = [
        "# 全量摸底明细表 · 字段中文说明",
        "",
        f"> 对应文件：`data/listing_full.csv`（{len(fields)} 列）｜口径：递交日 ≥ 2026 且状态 ∈ {{处理中, 含PHIP, 已上市}}。",
        "> ⚠️ 全表均为**行外公开信息初筛**，须经行内渠道复核；关系状态（是否开户/他行锁定）公开不可得，未列入本表。",
        "",
        "| 字段 | 中文名 | 含义 | 取值 / 口径 | 来源与可信度 |",
        "|---|---|---|---|---|",
    ]
    for f in fields:
        cn, mean, vals, src = SPEC[f]
        lines.append(f"| 【{f}】 | {cn} | {mean} | {vals} | {src} |")

    lines += [
        "",
        "## 可信度分级（重要，用于判断哪些数字能用）",
        "",
        "| 等级 | 字段 | 说明 |",
        "|---|---|---|",
        "| **高（可直接用）** | `id` `name_en` `name_cn` `board` `status` `stock_code` `submit_date` "
        "`app_proof_url` | 港交所官方清单原始字段，未加工 |",
        "| **高（权威口径）** | `domicile` `is_offshore` `scan_method` `cover_section` `cover_evidence` | "
        "取招股书**封面页固定句式**，可逐条回原文复核 |",
        "| **中（先判定后分类）** | `is_vie` `vie_scanned` `vie_terminated` `vie_source` `vie_evidence` | "
        "只认专有表述（大小写敏感）+ 终止感知；仍属判定，需复核 |",
        "| **低（线索，须人工复核）** | `gd_flag` `gd_l1_hit` `gd_opco` `gd_opco_count` "
        "`gd_opco_entity_count` `gd_opco_source` | "
        "名称启发式 / 城市词频，**不能单独作为结论** |",
        "| **推导** | `redchip_l2` `doc_available` | 由上述字段按规则推导 |",
        "",
        "## 复核入口",
        "- 注册地：看 `cover_evidence` 原文句 → 对照 `cover_section` 分册 → 用 `app_proof_url` 打开 PDF 首屏；",
        "- 广东连接：看 `gd_opco_source`（分册+词频）→ 打开对应分册搜索城市词；",
        "- VIE：看 `vie_source` + `vie_evidence` → 打开 `SUMMARY` / `RISK FACTORS` 章搜索 `VIE`、`Contractual Arrangements`；",
        "- 汇编视图：`output/redchip_gd_evidence_cards.md`（广东红筹候选）、`output/vie_evidence_cards.md`（VIE）。",
        "",
    ]
    out = settings.redchip_output_dir / "字段说明_全量摸底表.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"字段说明已写出：{out}（{len(fields)} 个字段）")


if __name__ == "__main__":
    main()
