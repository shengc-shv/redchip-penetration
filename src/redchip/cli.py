"""命令行入口。

常用命令（本地联调）::

    export PYTHONPATH=src
    python -m redchip.cli run --code 00700 --mock     # 离线跑通全流程
    python -m redchip.cli run --all                   # 跑 config/targets.yaml 全部港股
    python -m redchip.cli render --code 00700         # 仅重绘图（graph.json 已存在）
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from redchip import config as config_mod
from redchip.graph import render as render_mod
from redchip.graph.store import Graph
from redchip.pipeline import run as pipeline
from redchip.pipeline.targets import load_targets

app = typer.Typer(help="红筹架构自动穿透系统（港股优先）")


def _bootstrap(mock: bool) -> config_mod.Settings:
    """初始化运行环境。

    Args:
        mock: 是否强制离线模式。

    Returns:
        Settings: 生效的配置。
    """
    if mock:
        import os

        os.environ["REDCHIP_MOCK"] = "true"
    settings = config_mod.reload_settings()
    settings.redchip_output_dir.mkdir(parents=True, exist_ok=True)
    settings.redchip_data_dir.mkdir(parents=True, exist_ok=True)
    return settings


@app.command()
def run(
    code: list[str] | None = typer.Option(  # noqa: B008 - typer 推荐写法
        None, "--code", "-c", help="股票代码，可多次传入"
    ),
    mock: bool = typer.Option(False, "--mock", help="离线模式：不发起任何外部请求"),
    all_targets: bool = typer.Option(False, "--all", help="跑 config/targets.yaml 中的全部港股"),
) -> None:
    """执行港股穿透流水线。"""
    settings = _bootstrap(mock)
    targets = load_targets(codes=list(code) if code else None)
    if all_targets and not code:
        targets = [t for t in load_targets() if t.market.value == "hk"]
    if not targets:
        typer.secho("未指定目标：请用 --code 00700 或 --all", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    names = {t.code: t.name for t in targets}
    keywords = {t.code: t.wfoe_keywords for t in targets}
    reports = pipeline.run_batch([t.code for t in targets], names=names, keywords=keywords)

    for r in reports:
        color = typer.colors.YELLOW if r.needs_review else typer.colors.GREEN
        typer.secho(
            f"{r.code} {r.name or '-'}：节点 {len(r.nodes)} / 股权边 {len(r.owns)} / "
            f"VIE边 {len(r.controls)} / UBO {len(r.ubos)} / 置信度 {r.confidence.total}"
            f"{'  ⚠️需复核' if r.needs_review else ''}",
            fg=color,
        )
    typer.secho(f"\n输出目录：{settings.redchip_output_dir}", fg=typer.colors.CYAN)


@app.command()
def render(code: str = typer.Argument(..., help="股票代码")) -> None:
    """根据已生成的 graph.json 重绘图。"""
    settings = _bootstrap(mock=False)
    graph_path = settings.redchip_output_dir / code / "graph.json"
    if not graph_path.exists():
        typer.secho(f"缺少 {graph_path}，请先执行 run", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    data = json.loads(graph_path.read_text(encoding="utf-8"))
    graph = Graph()
    for node in data["nodes"]:
        if node.get("kind") == "person":
            graph.add_person(node["name"])
        else:
            graph.add_company(
                name=node["name"],
                jurisdiction=node.get("jurisdiction", "其他"),
                kind=node.get("kind", "unknown"),
                credit_code=node.get("credit_code"),
                province=node.get("province"),
                city=node.get("city"),
            )
    for e in data["owns"]:
        graph.owns.append(_rebuild_own(e))
    for e in data["controls"]:
        graph.controls.append(_rebuild_control(e))

    produced = render_mod.render_all(graph, settings.redchip_output_dir / code)
    for fmt, path in produced.items():
        typer.secho(f"{fmt}: {path}", fg=typer.colors.CYAN)


def _rebuild_own(payload: dict[str, object]) -> object:
    """从 JSON 重建股权边。

    Args:
        payload: 边数据。

    Returns:
        OwnEdge: 股权边对象。
    """
    from redchip.models.schema import OwnEdge

    return OwnEdge.model_validate(payload)


def _rebuild_control(payload: dict[str, object]) -> object:
    """从 JSON 重建协议控制边。

    Args:
        payload: 边数据。

    Returns:
        ControlEdge: 控制边对象。
    """
    from redchip.models.schema import ControlEdge

    return ControlEdge.model_validate(payload)


@app.command()
def show(code: str = typer.Argument(..., help="股票代码")) -> None:
    """打印已生成的报告摘要。"""
    settings = _bootstrap(mock=False)
    result_path = settings.redchip_output_dir / code / "result.json"
    if not result_path.exists():
        typer.secho(f"缺少 {result_path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(Path(result_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    app()
