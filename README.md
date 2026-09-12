# 红筹架构自动穿透系统

自动抓取**港股**（阶段二扩展美股）红筹架构企业的披露文件，完成股权穿透、VIE 协议识别、
UBO 判定与 LLM 解读，输出穿透路径图与中文分析报告。运行时为 GitHub Actions，无需服务器。

当前状态：**港股模块已完成并通过离线端到端验证**；美股模块为阶段二，已预留接口骨架。

## 与参考方案的两处重要偏差

| 方案文档原设计 | 实际情况 | 处理方式 |
| --- | --- | --- |
| 港股端用 `ah-disclosure-kit` | **该包在 PyPI 上不存在**（`pypi.org/simple/ah-disclosure-kit/` 返回 404） | 自研 `redchip/overseas/hkex.py`，直接对接 HKEXnews 官方接口（`prefix.do` → `titleSearchServlet.do` → PDF → `pypdf` 分页），已实测可用（00700 → stockId 7609） |
| PNG 依赖 `neo4j-graphviz`（npm） | 该包依赖 Node + Graphviz，链路长且易失败 | 渲染层自带三层降级：**Graphviz PNG**（有 `dot` 时）→ **零依赖 SVG** → **DOT / Mermaid** 源文件，任何环境都能出图 |

其余设计（Token 预算、两次 LLM 调用、广东过滤位置、置信度权重、Neo4j Service Container）
均按方案文档落地。

## 目录结构

```
├── .github/workflows/redchip-hk.yml   # Actions 流水线（分步执行）
├── config/targets.yaml                # 目标企业清单（广东红筹样本）
├── fixtures/                          # 离线联调样例（无密钥也能跑通全流程）
├── scripts/                           # 分步入口，与 Actions 步骤一一对应
│   ├── overseas_hkex.py               # 抓取 + FTS 索引
│   ├── llm_a.py                       # WFOE 候选 + LLM-A
│   ├── filter_guangdong.py            # 建图 + 广东省过滤
│   ├── domestic_penetration.py        # 境内股东递归穿透
│   ├── build_graph.py                 # 同步 Neo4j
│   ├── penetrate_ubo.py               # UBO 穿透 + 置信度评分
│   ├── llm_b.py                       # 审查 + 报告
│   └── export_results.py              # 渲染 + 汇总
├── src/redchip/
│   ├── config.py                      # 配置与 Secrets 变量名（含 Token 预算参数）
│   ├── models/                        # Pydantic 数据契约 + 置信度评分
│   ├── overseas/                      # hkex（港股）/ fts（FTS5 trigram）/ sec（阶段二）
│   ├── domestic/                      # cnbiz（工商 API）/ penetration（穿透 + 广东过滤）
│   ├── graph/                         # store（图）/ ubo（穿透算法）/ render（渲染）
│   ├── llm/                           # client（OpenAI 兼容）+ prompts/
│   └── pipeline/run.py                # 编排：既可整体运行，也可分步执行
└── tests/                             # 33 个单测（检索 / 穿透 / 过滤 / 渲染 / 端到端）
```

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,neo4j]"

# 1) 离线跑通全流程（不发起任何外部请求，使用 fixtures/）
python -m redchip.cli run --code 00700 --mock

# 2) 真实运行：先补齐密钥
cp .env.example .env   # 填入 CNBIZAPI_KEY / LLM_API_KEY 等
python -m redchip.cli run --code 00700

# 3) 批量跑 config/targets.yaml 中的全部港股
python -m redchip.cli run --all

# 单独重绘图（不需要重跑流水线）
python -m redchip.cli render 00700
```

分步执行（与 Actions 完全一致）：

```bash
export STOCK_CODES=00700
python scripts/overseas_hkex.py && python scripts/llm_a.py \
  && python scripts/filter_guangdong.py && python scripts/domestic_penetration.py \
  && python scripts/build_graph.py && python scripts/penetrate_ubo.py \
  && python scripts/llm_b.py && python scripts/export_results.py
```

## 需要你补齐的密钥（Secrets）

在仓库 **Settings → Secrets and variables → Actions** 中配置，变量名如下（代码已全部引用，
**未配置时会自动降级，不会中断流水线**）：

| Secret | 用途 | 未配置的后果 |
| --- | --- | --- |
| `CNBIZAPI_KEY` | 境内工商数据（免费 200 次/月） | 走 `fixtures/cnbiz` 样例数据 |
| `LLM_API_KEY` | LLM-A / LLM-B | 规则兜底提取，结果标记「需人工复核」 |
| `LLM_BASE_URL` | OpenAI 兼容端点，默认 `https://api.deepseek.com` | 同上 |
| `LLM_MODEL` | 默认 `deepseek-chat` | 同上 |
| `NEO4J_PASSWORD` | Neo4j Service Container 密码 | 用默认 `redchip_dev`，仅本地容器 |
| `SEC_IDENTITY` | 阶段二美股用，格式 `"项目名 邮箱"` | 美股模块未启用，无影响 |

本地联调可写入 `.env`（已在 `.gitignore` 中）。

## 输出产物

```
output/00700/
├── penetration_graph.png      # 穿透路径图（有 graphviz 时）
├── penetration_graph.svg      # 零依赖兜底图
├── penetration_graph.dot      # Graphviz 源文件
├── penetration_graph.mermaid  # Mermaid 源码
├── graph.json                 # 节点 + 股权边 + 协议控制边
├── llm_a.json                 # LLM-A 结构化提取结果
├── result.json                # 含 UBO、置信度明细的完整结果
├── state.json                 # 分步执行的中间态
└── report.md                  # 中文分析报告
output/summary.md              # 批量运行汇总
output/llm_raw/                # LLM 原始返回（审计与 Prompt 迭代用）
```

## 关键设计

- **Token 预算**：FTS 只取命中「合约安排 / VIE / 股权架构」等关键词的段落（默认 6k），
  候选列表裁剪为名称 + 信用代码 + 经营范围（1k），单企业总量控制在 20k 以内。
- **中文子串检索**：SQLite FTS5 使用 `trigram` 分词器，支持「合约安排」这类任意中文子串命中；
  不足 3 字的关键词自动回退到 `LIKE`。
- **广东过滤位置**：放在 LLM-A **之后**——先用全量候选完成 WFOE 消歧，再过滤，
  否则可能提前剔除正确实体。零额外 token。
- **时间真实性**：披露文件发布时间只取官方 `DATE_TIME` 字段，**不使用抓取日期兜底**；
  解析失败时该字段留空并在时效性维度按中性值计分。
- **降级策略**：抓取 / LLM / 图库 / PNG 任一环节失败都只记入 `errors` 并标记需人工复核，
  不阻断后续步骤（Actions 中 LLM 步骤设 `continue-on-error: true`）。

## 阶段规划

- [x] 港股：HKEXnews 抓取、FTS 检索、LLM-A/B、广东过滤、境内穿透、UBO、渲染
- [ ] 美股：EdgarTools 抓取 20-F（`src/redchip/overseas/sec.py` 已留骨架与调用要点）
