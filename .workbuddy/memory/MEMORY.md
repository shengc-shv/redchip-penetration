# 项目长期约定：redchip-penetration（红筹架构穿透）

## 项目定位
分析港美股红筹架构（开曼/BVI → 香港 → 境内 WFOE → VIE 运营实体）企业的股权穿透、
UBO 识别与 LLM 解读。运行时为 GitHub Actions，无需服务器。

## 阶段划分
- 阶段一（已完成）：港股模块
- 阶段二（待做）：美股 20-F（EdgarTools），骨架见 `src/redchip/overseas/sec.py`

## 硬性约定
- **时区**：全局唯一常量 `redchip.config.CST`（Asia/Shanghai）。禁止 `datetime.now()` 无时区调用、
  禁止 env 覆盖、禁止回落系统时区。
- **LLM 调用点只能有两个**：`llm_a`（架构提取）与 `llm_b`（审查+报告）。不新增第三个。
- **广东省过滤必须在 LLM-A 之后**：先用全量候选让 LLM 完成 WFOE 消歧，确认实体后再过滤，
  否则可能剔除正确实体。零额外 token。
- **披露文件时间只取官方字段**（HKEX 的 `DATE_TIME`），**不得用抓取日期兜底**；解析失败留空。
- **降级优先于中断**：抓取 / LLM / Neo4j / PNG 任一环节失败都只记 `errors` 并标记需人工复核，
  不阻断后续步骤。
- **节点去重**：公司节点 id 优先取统一社会信用代码；无代码时按「名称+注册地」复用，
  有代码时也要先按名称查重并补全代码。

## 已知不可用的外部依赖（勿再尝试）
- `ah-disclosure-kit`：PyPI 不存在（404）。港股抓取一律走自研 `overseas/hkex.py`。
- 本地 `brew install graphviz` 被沙箱拦截；渲染依赖 dot 时要能降级到自绘 SVG。

## 常用命令
```bash
PYTHONPATH=src python -m redchip.cli run --code 00700 --mock   # 离线跑通
PYTHONPATH=src python -m redchip.cli run --all                  # 全量港股
PYTHONPATH=src python -m pytest tests -q                        # 33 个单测
export STOCK_CODES=00700 && python scripts/*.py 依次执行          # 分步
```
