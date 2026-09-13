#!/usr/bin/env python3
"""申请 CNBizAPI 的 API Key（官网注册按钮失效时的替代入口）。

为什么需要这个脚本
------------------
官网首页的「Get Free API Key」按钮直接指向 ``https://api.cnbizapi.com/v1/auth/register``，
而该端点**只接受 POST**；浏览器点击发出的是 GET，于是返回::

    {"message":"Route GET:/v1/auth/register not found","error":"Not Found","statusCode":404}

实测官网也**没有注册表单页面**（首页无 ``<form>``、无 email/password 输入框，
唯一注册链接就是上述 API 地址），因此这里按接口约定提交注册请求。

接口约定（实测）
----------------
- 方法与路径：``POST /v1/auth/register``
- 请求体：``{"email": "...", "password": "..."}``（密码至少 6 位）
- 返回：含 API Key 的 JSON（字段名未公开，脚本会打印完整响应并尽力提取）

用法::

    PYTHONPATH=src python scripts/get_cnbizapi_key.py
    PYTHONPATH=src python scripts/get_cnbizapi_key.py --dry-run   # 只校验参数，不发请求

安全说明
--------
- 密码用 ``getpass`` 交互输入，不进 shell 历史、不写日志、不落盘
- 该服务 TLS 证书当前已过期（``CN=cnbizapi.com``），此处**仅对本次注册请求**放宽校验
  并在输出中明确提示；流水线与报告侧仍应通过 ``REDCHIP_ALLOW_EXPIRED_CERT`` 显式控制
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import warnings
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.cnbizapi.com"
REGISTER_PATH = "/v1/auth/register"
MIN_PASSWORD_LEN = 6

# 返回体里可能是这些字段名中的任意一个承载 Key（接口未公开字段名）
KEY_FIELDS = ("apiKey", "api_key", "key", "apiKeyPlain", "token", "accessToken", "secret")


def validate(email: str, password: str) -> list[str]:
    """做提交前的本地校验，避免浪费一次请求。

    Args:
        email: 邮箱。
        password: 密码。

    Returns:
        list[str]: 问题列表；为空表示通过。
    """
    problems: list[str] = []
    if "@" not in email or "." not in email.split("@")[-1]:
        problems.append("邮箱格式不正确")
    if len(password) < MIN_PASSWORD_LEN:
        problems.append(f"密码至少需要 {MIN_PASSWORD_LEN} 位")
    return problems


def extract_key(payload: Any) -> str:
    """从返回结构里尽力提取 API Key。

    接口未公开返回字段名，因此按常见命名逐层探测（含 ``data`` / ``user`` 等嵌套层）。

    Args:
        payload: 接口返回的 JSON。

    Returns:
        str: 提取到的 Key；未找到返回空串。
    """
    if not isinstance(payload, dict):
        return ""
    for field in KEY_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    for nested in ("data", "user", "result", "account"):
        inner = payload.get(nested)
        if isinstance(inner, dict):
            found = extract_key(inner)
            if found:
                return found
    return ""


def register(email: str, password: str) -> tuple[int, dict[str, Any]]:
    """调用注册接口。

    Args:
        email: 邮箱。
        password: 密码。

    Returns:
        tuple[int, dict[str, Any]]: HTTP 状态码与响应体。

    Raises:
        httpx.HTTPError: 网络不可达或超时。
    """
    with httpx.Client(base_url=BASE_URL, timeout=30, verify=False) as client:
        resp = client.post(REGISTER_PATH, json={"email": email, "password": password})
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body


def upsert_env(env_path: Path, key: str) -> None:
    """把 Key 写入 .env（存在则原地替换，不存在则追加）。

    Args:
        env_path: .env 路径。
        key: API Key。
    """
    line = f"CNBIZAPI_KEY={key}"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        for idx, existing in enumerate(lines):
            if existing.strip().startswith("CNBIZAPI_KEY="):
                lines[idx] = line
                break
        else:
            lines.append(line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        env_path.write_text(
            "# 由 scripts/get_cnbizapi_key.py 生成\n"
            f"{line}\n"
            "# 该服务 TLS 证书当前已过期，连不上时需同时设置：\n"
            "REDCHIP_ALLOW_EXPIRED_CERT=true\n",
            encoding="utf-8",
        )


def main() -> int:
    """命令行入口。

    Returns:
        int: 进程退出码。
    """
    parser = argparse.ArgumentParser(description="申请 CNBizAPI 的 API Key")
    parser.add_argument("--dry-run", action="store_true", help="只校验参数，不发起请求")
    parser.add_argument("--email", default="", help="邮箱（不传则交互输入）")
    args = parser.parse_args()

    warnings.filterwarnings("ignore")  # 证书过期导致的数据源告警，见模块 docstring

    email = args.email or input("注册邮箱：").strip()

    # dry-run 不应索要真实密码（否则在非交互环境下会阻塞），用占位串走通校验
    if args.dry_run:
        placeholder = "x" * MIN_PASSWORD_LEN
        problems = validate(email, placeholder)
        if problems:
            print("参数有误：" + "；".join(problems), file=sys.stderr)
            return 2
        print(f"[dry-run] 将提交 POST {BASE_URL}{REGISTER_PATH}")
        print(f"[dry-run] body = {json.dumps({'email': email, 'password': '***'}, ensure_ascii=False)}")
        return 0

    password = getpass.getpass(f"设置密码（至少 {MIN_PASSWORD_LEN} 位，输入不回显）：")
    problems = validate(email, password)
    if problems:
        print("参数有误：" + "；".join(problems), file=sys.stderr)
        return 2

    print(f"\n正在提交注册请求到 {BASE_URL}{REGISTER_PATH} …")
    try:
        status, body = register(email, password)
    except httpx.HTTPError as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        print("若提示证书错误，可先访问 https://cnbizapi.com 确认服务状态，"
              "或邮件联系 contact@cnbizapi.com", file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])

    api_key = extract_key(body)
    if not api_key:
        print("\n未在响应中识别到 API Key。两种可能：", file=sys.stderr)
        print("  1) 需要先完成邮箱验证，请查收邮件后在其控制台获取 Key", file=sys.stderr)
        print("  2) 字段名与预期不同 —— 请把上面的响应贴出来，我按实际字段调整提取逻辑", file=sys.stderr)
        return 1

    print(f"\n✅ 已获取 API Key：{api_key[:8]}…（共 {len(api_key)} 字符）")
    if input("是否写入项目 .env？[y/N] ").strip().lower() == "y":
        env_path = Path(__file__).resolve().parents[1] / ".env"
        upsert_env(env_path, api_key)
        print(f"已写入 {env_path}")
        print("提示：该服务证书当前过期，需同时在 .env 中设置 REDCHIP_ALLOW_EXPIRED_CERT=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
