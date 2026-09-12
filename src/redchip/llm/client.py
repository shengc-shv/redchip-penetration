"""LLM 客户端封装（OpenAI 兼容接口）。

约束（对应方案文档第八节）
-------------------------
- 全流程只有两个调用点：LLM-A（架构提取）与 LLM-B（审查 + 报告），不新增第三个。
- ``temperature=0.1`` 保证输出稳定，减少幻觉。
- JSON 输出强制 ``response_format={"type": "json_object"}``。
- 所有返回先落盘 ``output/llm_raw/`` 再解析，便于审计与 Prompt 迭代。
- 未配置 ``LLM_API_KEY`` 且未开启 mock 时，调用点按方案要求 ``continue-on-error`` 处理，
  由上层降级为「需人工复核」，而不是让整条流水线崩溃。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from redchip import config as config_mod

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """LLM 不可用（缺 Key / 网络失败 / 重试耗尽）。"""


class LLMClient:
    """OpenAI 兼容接口的极简封装。

    Args:
        settings: 全局配置；缺省自动载入。
        raw_dir: 原始返回落盘目录；缺省写入 ``<output>/llm_raw``。
    """

    def __init__(
        self,
        settings: config_mod.Settings | None = None,
        raw_dir: Path | None = None,
    ) -> None:
        self.settings = settings or config_mod.get_settings()
        self.raw_dir = raw_dir or (self.settings.redchip_output_dir / "llm_raw")
        self._client: Any = None

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """判断 LLM 是否可用（配置了 Key 即可用）。"""
        return bool(self.settings.llm_api_key)

    def _ensure_client(self) -> Any:
        """懒加载 openai 客户端，避免在 mock 环境下强制安装依赖。"""
        if self._client is None:
            from openai import OpenAI  # 局部导入：mock 模式下无需该依赖

            self._client = OpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                timeout=self.settings.llm_timeout,
                max_retries=0,  # 自行控制重试，便于记录失败原因
            )
        return self._client

    def complete(self, prompt: str, json_mode: bool = True, system: str | None = None) -> str:
        """发起一次对话补全。

        Args:
            prompt: 用户消息。
            json_mode: 是否强制 JSON 输出。
            system: 系统指令；缺省不发送。

        Returns:
            str: 模型返回文本。

        Raises:
            LLMUnavailableError: 未配置 Key，或重试后仍然失败。
        """
        if not self.is_available():
            raise LLMUnavailableError("未配置 LLM_API_KEY，跳过 LLM 调用")

        client = self._ensure_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error: Exception | None = None
        for attempt in range(1, self.settings.llm_max_retries + 2):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.settings.llm_model,
                    "messages": messages,
                    "temperature": self.settings.llm_temperature,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
                return str(resp.choices[0].message.content or "")
            except Exception as exc:  # noqa: BLE001 - 统一收敛为领域异常
                last_error = exc
                if attempt <= self.settings.llm_max_retries:
                    time.sleep(2**attempt)
        raise LLMUnavailableError(f"LLM 调用失败：{last_error}")

    def call_json(self, prompt: str, schema: type[T], tag: str, system: str | None = None) -> T:
        """调用 LLM 并把返回解析为指定 Pydantic 模型。

        原始返回先落盘再解析，解析失败时抛出 ``ValidationError`` 由上层标记需人工复核。

        Args:
            prompt: 完整 Prompt。
            schema: 目标 Pydantic 模型。
            tag: 落盘文件名标识，如 ``00700_llm_a``。
            system: 系统指令。

        Returns:
            T: 校验通过的模型实例。

        Raises:
            ValidationError: JSON 与 Schema 不匹配。
            LLMUnavailableError: 调用失败。
        """
        raw = self.complete(prompt, json_mode=True, system=system)
        self._dump_raw(tag, raw)
        return schema.model_validate(_extract_json(raw))

    def _dump_raw(self, tag: str, raw: str) -> None:
        """保存原始返回，便于审计。"""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / f"{tag}.json").write_text(raw, encoding="utf-8")


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型返回中抽取 JSON 对象。

    部分模型即使设置 json_object 也会包裹 ```json 代码围栏，这里做一次兜底清理。

    Args:
        raw: 模型原始返回。

    Returns:
        dict[str, Any]: 解析出的对象。

    Raises:
        ValidationError: 无法解析为 JSON 对象。
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValidationError.from_exception_data("llm_output", [])  # type: ignore[arg-type]
    return data


def load_prompt(name: str) -> str:
    """加载 Prompt 模板。

    Args:
        name: 模板文件名，如 ``llm_a.md``。

    Returns:
        str: 模板内容。
    """
    return (config_mod.PROMPTS_DIR / name).read_text(encoding="utf-8")


def render_prompt(template: str, **placeholders: str) -> str:
    """用占位符替换渲染 Prompt。

    刻意不使用 ``str.format``：模板内含大量 JSON 花括号，format 会误解析。

    Args:
        template: 模板文本。
        **placeholders: 占位符键值。

    Returns:
        str: 渲染后的 Prompt。
    """
    out = template
    for key, value in placeholders.items():
        out = out.replace("{" + key + "}", value)
    return out
