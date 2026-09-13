# 项目长期约定：redchip-penetration（红筹架构穿透）

## 项目定位
分析港美股红筹架构（开曼/BVI → 香港 → 境内 WFOE → VIE 运营实体）企业的股权穿透、
UBO 识别与解读。GitHub Actions 运行，无需服务器。
仓库 <https://github.com/shengc-shv/redchip-penetration>；报告站点在 gh-pages 分支
（<https://shengc-shv.github.io/redchip-penetration/>，微信转发卡片用）。

## 阶段划分
- 阶段一（已完成）：港股模块（自研 HKEXnews 抓取，非 ah-disclosure-kit）
- 阶段二（已完成）：美股模块（SEC EDGAR 官方 REST API，非 edgartools）

## ⚠️ 数据真实性边界（最容易混淆，务必先看这张表）
| 数据 | 需注册？ | 当前实际来源 | 真实吗 |
|---|---|---|---|
| 港交所披露易（年报/招股书） | 不需要 | 官方公开接口真实下载 | ✅ 真实 |
| SEC EDGAR（20-F） | 不需要（仅 UA） | 官方 REST API 真实下载 | ✅ 真实 |
| 境内工商（CNBizAPI） | **需要 Key** | `fixtures/cnbiz/sample.json`（**手写示意数据**） | ❌ 非真实 |
| LLM 解读 | 需要 Key | `fixtures/manual/<code>_llm_a.json`（WorkBuddy 本地分析） | 分析真、输入含样例 |
| Neo4j / graphviz | 可选 | 未配置 → 静默跳过 / 降级自绘 SVG | — |

**结论：境内持股比例（如腾讯马化腾 54.29%）目前是算法验证口径，不是核出来的事实；
对外交付前必须接入真实工商数据源。** 境外披露口径的数字（如 MIH 22.80%、Advance Data 8.82%）是真实的。
代码里的降级开关：`cnbiz.py` 中 `self.mock = settings.redchip_mock or not cnbizapi_key`。

## 硬性约定
- **时区**：唯一常量 `redchip.config.CST`（Asia/Shanghai）。禁止无时区 `datetime.now()`、禁止 env 覆盖。
- **LLM 调用点只有两个**：`llm_a`（架构提取）与 `llm_b`（审查+报告）。
- **地域过滤必须在 LLM-A 之后**（先全量候选消歧再过滤），默认广东省，可按目标覆盖。
- **披露时间只取官方字段**（HKEX `DATE_TIME`、SEC `filingDate`），**不得用抓取日兜底**。
- **降级优先于中断**：抓取/LLM/Neo4j/渲染任一失败只记 `errors` + 标记需人工复核，不阻断。
- **节点去重**：优先统一社会信用代码；无代码按「名称+注册地」复用，有代码也先按名称查重。
- **报告只呈现事实信息**：不要输出时间安排、排期、行动建议（用户方自有打法）；
  LLM-B 提示词已加此约束。

## 关键实现位置
- 卡片元信息/图标：`scripts/make_brief.py`（card_title/card_desc/page_head/thumb_img）、`scripts/make_icon.py`
- 证据等级 L1~L4：`models/evidence.py`；境内反推（**仅广东**）：`domestic/onshore.py`
- 交叉验证（披露↔工商）：`verify/crosscheck.py`、`verify/shareholders.py`（中英双通道）
- 美股 HTML 表格解析（Item 7）：`overseas/htmltable.py`
- 红筹判定口径：注册地+上市地均在境外即为红筹；按控制人分国资/民营，按控制方式分股权/协议（VIE）。

## 境内工商数据源（适配器架构）
- 实现位于 `src/redchip/domestic/sources/`：`base.py`（协议 + 共享工具）/ `cnbizapi.py` /
  `qcc.py`（企查查，**接口路径未联调**）/ `fixtures.py`（离线兜底）
- `domestic/cnbiz.py` 已收敛为**兼容门面**：`CnbizClient` 类名与三个方法签名不变，
  穿透层与流水线零改动；新增数据源只需实现 `CompanyDataSource` 并在 `build_source()` 登记
- **标识统一**：穿透层用统一社会信用代码，而真实接口按企业名称检索 →
  `IdentifierIndex` 维护双向映射（由搜索/基本信息接口回填，零额外请求）
- 环境变量：`REGISTRY_SOURCE`（auto/cnbizapi/qcc/fixture）、`QCC_APP_KEY`、`QCC_SECRET_KEY`、
  `REDCHIP_ALLOW_EXPIRED_CERT`（默认 false；开启时仅对该数据源放宽并记警告）
- 报告顶部已加「行外公开信息初筛，须经行内渠道复核」声明条 + 打印/存 PDF
  （行内网络可能访问不了 github.io）

## 已知不可用的外部依赖（勿再尝试）
- `ah-disclosure-kit`：PyPI 404。港股走自研 `overseas/hkex.py`。
- `edgartools`：本环境安装不稳定，改用 SEC 官方 REST API。
- `brew install graphviz` 被沙箱拦截；渲染需能降级到自绘 SVG。
- 港交所 DI 权益披露站直连不可用（旧接口 302 弃用）→ 改用年报第XV部章节 + 披露易申报表。
- **CNBizAPI（2026-09-13 实测）**：服务真实存在（7700 万+企业，邮箱注册，免费 200 次/月），
  但 ① **TLS 证书已过期**，httpx 默认校验直接连不上；② 真实接口是 **GET + query**
  （`/v1/company/search?keyword=`、`/basic?q=`、`/shareholders?q=`），
  与 `domestic/cnbiz.py` 里的 POST + JSON body 不符，接入前必须先改代码；
  ③ `get_shareholders` 属**付费工具（1 积分/次）**，免费额度只覆盖 search/basic/verify；
  ④ **官网「Get Free API Key」按钮失效**（指向 POST-only 的 `/v1/auth/register`，
  浏览器点击必 404，且官网无注册表单）→ 用 `scripts/get_cnbizapi_key.py` 注册。

## 常用命令
```bash
PYTHONPATH=src python -m redchip.cli run --code 00700        # 港股真实抓取
PYTHONPATH=src python -m redchip.cli run --code BABA         # 美股真实抓取
PYTHONPATH=src python -m redchip.cli run --code 00700 --mock # 全离线
PYTHONPATH=src python scripts/make_brief.py                  # 生成高管报告 + 站点页
PYTHONPATH=src python -m pytest tests -q                     # 67 个单测
```
