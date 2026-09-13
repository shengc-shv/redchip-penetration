# 红筹架构自动穿透系统

自动抓取**港股**（阶段二扩展美股）红筹架构企业的披露文件，完成股权穿透、VIE 协议识别、
UBO 判定与 LLM 解读，输出穿透路径图与中文分析报告。运行时为 GitHub Actions，无需服务器。

当前状态：**港股、美股两个模块均已完成**，并分别用腾讯控股（00700，离线样例）与
阿里巴巴（BABA，真实 20-F）端到端验证通过。

## 与参考方案的两处重要偏差

| 方案文档原设计 | 实际情况 | 处理方式 |
| --- | --- | --- |
| 港股端用 `ah-disclosure-kit` | **该包在 PyPI 上不存在**（`pypi.org/simple/ah-disclosure-kit/` 返回 404） | 自研 `redchip/overseas/hkex.py`，直接对接 HKEXnews 官方接口（`prefix.do` → `titleSearchServlet.do` → PDF → `pypdf` 分页），已实测可用（00700 → stockId 7609） |
| PNG 依赖 `neo4j-graphviz`（npm） | 该包依赖 Node + Graphviz，链路长且易失败 | 渲染层自带三层降级：**Graphviz PNG**（有 `dot` 时）→ **零依赖 SVG** → **DOT / Mermaid** 源文件，任何环境都能出图 |
| 美股端用 `edgartools` | 该库在本环境安装不稳定（pip 进程被中断） | 用 **SEC 官方 REST API** 直连（`company_tickers.json` → `submissions/CIK*.json` → Archives 原文），零第三方依赖，已实测 BABA：CIK 1577552、12 份 20-F、最新一份 11.7MB HTML |

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
│   ├── overseas/                      # hkex（港股）/ sec（美股 20-F）/ fts（FTS5 trigram）
│   ├── verify/                        # 交叉验证器：披露口径 ↔ 工商登记口径
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

# 3) 美股（20-F）
python -m redchip.cli run --code BABA

# 4) 批量跑 config/targets.yaml 中的全部目标（港股 + 美股）
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

| Secret / Variable | 用途 | 未配置的后果 |
| --- | --- | --- |
| `CNBIZAPI_KEY` | 境内工商数据（CNBizAPI，免费 200 次/月） | 依次尝试下一数据源，最终走 `fixtures/cnbiz` 样例 |
| `QCC_APP_KEY` / `QCC_SECRET_KEY` | 境内工商数据（企查查开放平台，需企业实名认证） | 同上 |
| `REGISTRY_SOURCE`（Variable） | 强制指定数据源：`auto` / `cnbizapi` / `qcc` / `fixture` | 默认 `auto`，按可用密钥自动选择 |
| `REDCHIP_ALLOW_EXPIRED_CERT`（Variable） | 放宽工商数据源 TLS 校验（服务方证书过期时的临时兜底） | 默认 `false`，严格校验 |
| `LLM_API_KEY` | LLM-A / LLM-B | 规则兜底提取，结果标记「需人工复核」 |
| `LLM_BASE_URL` | OpenAI 兼容端点，默认 `https://api.deepseek.com` | 同上 |
| `LLM_MODEL` | 默认 `deepseek-chat` | 同上 |
| `NEO4J_PASSWORD` | Neo4j Service Container 密码 | 用默认 `redchip_dev`，仅本地容器 |
| `SEC_IDENTITY` | 美股用，格式 `"项目名 邮箱"` | 使用默认示例值 |

本地联调可写入 `.env`（已在 `.gitignore` 中）。

## 境内工商数据源（可切换）

穿透层以**统一社会信用代码**为主键，而各数据源普遍按**企业名称**检索，
因此接入层（`src/redchip/domestic/sources/`）做了两件事：抹平各家字段命名差异，
并用 `IdentifierIndex` 维护名称↔代码映射（由搜索与基本信息接口自动回填，零额外请求）。

| 数据源 | 免费额度 | 开通门槛 | 备注 |
| --- | --- | --- | --- |
| **CNBizAPI** | 200 次/月（仅覆盖免费工具） | 邮箱注册即用 | ⚠️ **2026-09-13 实测数据层不可用**：华为/阿里云/腾讯均 404，search 接口 500，股东查询另需购买积分（免费额度不含）；接口形式 `GET + query`，TLS 证书已过期需放宽校验 |
| **企查查开放平台** | 每接口 20 次（一次性） | **企业实名 + 应用场景审核** | 含股东信息的「企业工商详情」2 元/次；签名 `MD5(key+Timespan+SecretKey)` |
| **离线样例** | 不限 | 无 | 自动兜底，数据为示意口径，**不得当作核验事实** |

新增数据源只需实现 `CompanyDataSource` 协议（搜索 / 基本信息 / 股东三个方法）
并在 `build_source()` 登记，穿透层无需改动。

> **数据定位**：本项目的报告基于**公开披露 + 商业工商数据源**在行外生成，用于**线索初筛**；
> 涉及客户准入、授信与合规的结论，须经行内渠道数据复核（报告页顶部已作声明）。
> 官方权威口径可用国家企业信用信息公示系统的人工查询通道核验
> （实名登录后可申请 PDF 版《企业信用信息公示报告》），该系统无程序化接口。

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

## 准确性核准：交叉验证器

单靠一份披露文件判 UBO 风险很高，因此流水线内置交叉核对（`src/redchip/verify/`）：

| 校验器 | 逻辑 |
| --- | --- |
| 披露 ↔ 登记比例对比 | 年报「主要股东权益」章节是《证券及期货条例》第XV部申报数据的法定镜像，与工商登记按姓名对齐，偏差 >±2% 记 error |
| 合计校验 | 登记股东比例之和应 ≈100%（容差 ±0.5%），否则股东名册不完整 |
| 离岸股东提示 | 披露有、工商无的股东记 info 级，不误伤通过状态 |

抽取器支持中英双通道：
- 中文：「马化腾先生（54.29%）」类表述，过滤合计/總計等汇总行；
- 英文（PDF 文本）：港股英文年报股东表，自动剔除 `Long position` / `Corporate (Note 1)` 等
  capacity 列，并合并 PDF 软换行（名称常被拆到多行）。
  实测腾讯年报 p.79 抽出 MIH Internet Holdings B.V 22.80%、Advance Data Services Limited 8.82%；
- **HTML 表格通道**（`overseas/htmltable.py`）：美股 20-F 本身是 HTML，直接解析 `<table>`
  行列比文本正则可靠得多（列序、空列、capacity 列都不再是问题），栈式解析兼容表格嵌套。
  实测阿里 20-F Item 7 抽出 5 位董事及高管的持股记录。
  边界：港股年报是 PDF，不适用此通道；20-F 若无非 5% 以上外部股东，Item 7 仅列董事高管。

error 级问题会拉低置信度的「多源一致性」维度并写入需人工复核原因。
注：港交所 DI 系统直连不可用（旧接口 302 弃用、新页面对部分网络不可用），
故改以年报法定章节 + 披露易申报表（实测 146/200 条为第XV部申报表）作为同一权威口径的来源。

## 关键设计

- **Token 预算**：FTS 只取命中「合约安排 / VIE / 股权架构」等关键词的段落（默认 6k），
  候选列表裁剪为名称 + 信用代码 + 经营范围（1k），单企业总量控制在 20k 以内。
- **中文子串检索**：SQLite FTS5 使用 `trigram` 分词器，支持「合约安排」这类任意中文子串命中；
  不足 3 字的关键词自动回退到 `LIKE`。
- **英文优先**：披露易同时提供繁体中文版与英文版年报，**中文版用 pypdf 提取会出现 CID 编码乱码**
  （实测：简体与繁体关键词命中均为 0），而英文版提取完整（VIE 56 页、第XV部股东章节 p.79 清晰可读）。
  因此港股抓取默认 `lang=EN`，股东表解析器支持中英双通道。
- **广东过滤位置**：放在 LLM-A **之后**——先用全量候选完成 WFOE 消歧，再过滤，
  否则可能提前剔除正确实体。零额外 token。
- **时间真实性**：披露文件发布时间只取官方 `DATE_TIME` 字段，**不使用抓取日期兜底**；
  解析失败时该字段留空并在时效性维度按中性值计分。
- **美股差异**：20-F 为英文，FTS 关键词含英文表述（contractual arrangements / variable interest /
  Organizational Structure / representative VIE 等），且对架构与股东类信号词加权，
  避免 Risk Factors 章节挤占 6k token 预算；地域过滤默认广东，可按目标配置省份
  （BABA 配置为浙江省以便验证完整穿透）。
- **降级策略**：抓取 / LLM / 图库 / PNG 任一环节失败都只记入 `errors` 并标记需人工复核，
  不阻断后续步骤（Actions 中 LLM 步骤设 `continue-on-error: true`）。

## 阶段规划

- [x] 港股：HKEXnews 抓取、FTS 检索、LLM-A/B、地域过滤、境内穿透、UBO、交叉验证、渲染
- [x] 美股：SEC EDGAR 官方 API 抓取 20-F（ticker → CIK → 申报列表 → 原文 → 分块 → FTS），
      复用全部下游阶段；已用 BABA 真实数据验证
- [x] 英文股东表解析器（港股英文年报 + 美股 20-F 共用）
- [x] 20-F Item 7 股东表：HTML `<table>` 结构化解析（栈式，兼容嵌套）
- [ ] 中文繁体年报支持：需先换 pdfplumber/PyMuPDF 修复中文 PDF 提取，再加简繁归一化
