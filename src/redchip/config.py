"""全局配置与密钥变量名集中定义。

设计要点
--------
1. 所有需要通过 GitHub Actions Secrets 注入的敏感值，统一在此声明为环境变量，
   变量名即 Secrets 名，避免散落在各脚本里难以维护。
2. ``REPORT_TZ`` 硬编码为 Asia/Shanghai 且**不允许**通过环境变量覆盖 —— 历史教训：
   CI runner 默认 UTC，若回落到系统时区会导致日期键错位一天。
3. 任何以 ``REDCHIP_`` 开头的变量都可用环境变量覆盖（pydantic-settings 行为）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 日期键 / 时间窗口固定北京时间，禁止 env 覆盖、禁止回落系统时区。
# 全项目统一引用此常量，禁止在其它模块自行构造时区或调用无时区的 datetime.now()。
REPORT_TZ: str = "Asia/Shanghai"
CST = ZoneInfo(REPORT_TZ)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
FIXTURES_DIR = PROJECT_ROOT / "fixtures"
PROMPTS_DIR = Path(__file__).resolve().parent / "llm" / "prompts"


class Settings(BaseSettings):
    """运行期配置。字段名与 GitHub Secrets 名一一对应。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 境内工商数据源 ----------
    cnbizapi_key: str = Field(default="", description="CNBizAPI Key，形如 cbz_xxxx")
    cnbizapi_base_url: str = Field(default="https://api.cnbizapi.com")

    # ---------- LLM ----------
    llm_api_key: str = Field(default="")
    llm_base_url: str = Field(default="https://api.deepseek.com")
    llm_model: str = Field(default="deepseek-chat")
    llm_temperature: float = Field(default=0.1, description="低温度保证输出稳定")
    llm_timeout: int = Field(default=60, description="单次调用超时（秒）")
    llm_max_retries: int = Field(default=2)

    # ---------- 图数据库（可选）----------
    neo4j_uri: str = Field(default="", description="为空则只落盘 JSON，不写图库")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="")

    # ---------- 阶段二：美股 ----------
    sec_identity: str = Field(default="redchip-penetration you@example.com")

    # ---------- 离岸层（可选）----------
    openregistry_mcp_url: str = Field(default="https://openregistry.sophymarine.com/mcp")

    # ---------- 运行开关 ----------
    redchip_mock: bool = Field(
        default=False,
        description="true 时不发起任何外部请求，全部读取 fixtures/ 下的样例数据，用于离线联调",
    )
    redchip_output_dir: Path = Field(default=DEFAULT_OUTPUT_DIR)
    redchip_data_dir: Path = Field(default=DEFAULT_DATA_DIR)

    # ---------- 穿透参数 ----------
    max_depth: int = Field(default=50, ge=1, le=200)
    min_share: float = Field(default=0.25, ge=0.0, le=1.0, description="UBO 判定阈值")

    # ---------- Token 预算（单企业 ≤ 20000）----------
    fts_max_tokens: int = Field(default=6000, description="招股书 FTS 命中段上限")
    candidate_max_tokens: int = Field(default=1000, description="WFOE 候选中列表上限")
    token_budget: int = Field(default=20000)

    # ---------- 地域过滤 ----------
    target_province: str = Field(default="广东省")

    # ---------- 网络 ----------
    http_timeout: int = Field(default=60)
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; redchip-penetration/0.1; +https://github.com/shengc-shv/redchip-penetration)"
    )

    @property
    def market_hk(self) -> Literal["hk"]:
        return "hk"


def reload_settings() -> Settings:
    """强制重新加载配置（CLI 设置环境变量后、测试用例切换 mock 时使用）。

    Returns:
        Settings: 重新构建的配置对象。
    """
    global _SETTINGS
    _SETTINGS = Settings()  # type: ignore[name-defined]
    return _SETTINGS  # type: ignore[no-any-return]


def get_settings() -> Settings:
    """返回全局单例配置。

    Returns:
        Settings: 从环境变量 / .env 载入的配置对象。
    """
    global _SETTINGS
    try:
        return _SETTINGS  # type: ignore[name-defined]
    except NameError:
        _SETTINGS = Settings()  # type: ignore[name-defined]
        return _SETTINGS  # type: ignore[no-any-return]


def token_len(text: str) -> int:
    """粗略估算文本的 token 数。

    中英混排场景：CJK 字符约 1 字 1 token，其余按 4 字符 1 token 折算。
    该估算只用于预算裁剪，不用于计费。

    Args:
        text: 待估算文本。

    Returns:
        int: 估算 token 数。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    others = len(text) - cjk
    return cjk + others // 4


def truncate_tokens(text: str, max_tokens: int) -> str:
    """按 token 预算截断文本（尾部追加省略标记）。

    Args:
        text: 原文。
        max_tokens: 允许的最大 token 数。

    Returns:
        str: 截断后的文本。
    """
    if token_len(text) <= max_tokens:
        return text
    # 二分找到满足预算的最长前缀，避免逐字符累加带来的 O(n) 估算开销
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if token_len(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "\n…[已按 token 预算截断]"
