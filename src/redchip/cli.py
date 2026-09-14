"""命令行入口。

常用命令（本地联调）::

    export PYTHONPATH=src
    python -m redchip.cli run --code 00700 --mock     # 离线跑通全流程
    python -m redchip.cli run --all                   # 跑 config/targets.yaml 全部港股
    python -m redchip.cli render --code 00700         # 仅重绘图（graph.json 已存在）
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import typer

from redchip import config as config_mod
from redchip.graph import render as render_mod
from redchip.graph.store import Graph
from redchip.models.schema import CompanyReport
from redchip.overseas import hkex_listing as listing_mod
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
        targets = list(load_targets())
    if not targets:
        typer.secho("未指定目标：请用 --code 00700 或 --all", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    names = {t.code: t.name for t in targets}
    keywords = {t.code: t.wfoe_keywords for t in targets}
    markets = {t.code: t.market for t in targets}
    provinces = {t.code: t.province for t in targets}
    reports = pipeline.run_batch(
        [t.code for t in targets],
        names=names,
        keywords=keywords,
        markets=markets,
        provinces=provinces,
    )

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
def pack(
    code: list[str] | None = typer.Option(  # noqa: B008 - typer 推荐写法
        None, "--code", "-c", help="股票代码，可多次传入"
    ),
    mock: bool = typer.Option(
        False, "--mock", help="离线模式：不发起任何外部请求"
    ),
) -> None:
    """导出 LLM 分析包：把需要交给 LLM 的全部输入固化为 JSON + Markdown。

    导出的 llm_pack.md 可直接用于本地分析；分析结果回填为
    output/<code>/llm_a.manual.json 与 report.manual.md 后，再次执行 run 会优先采用。
    """
    settings = _bootstrap(mock)
    targets = load_targets(codes=list(code) if code else None)
    if not targets:
        typer.secho("未指定目标：请用 --code 00700", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    for target in targets:
        state = pipeline.load_state(target.code, settings)
        state.market = target.market  # 决定抓取层：港股走 HKEXnews，美股走 SEC EDGAR
        state = pipeline.stage_fetch(state, settings)
        state = pipeline.stage_candidates(
            state, settings, name=target.name, keywords=target.wfoe_keywords
        )
        path = pipeline.stage_pack(state, settings, target.name)
        typer.secho(f"{target.code} 分析包：{path}", fg=typer.colors.CYAN)


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

    # 若已生成过结果，带上 UBO 标注一起重绘
    result_file = settings.redchip_output_dir / code / "result.json"
    ubos = None
    if result_file.exists():
        report = CompanyReport.model_validate(json.loads(result_file.read_text(encoding="utf-8")))
        ubos = report.ubos

    produced = render_mod.render_all(
        graph,
        settings.redchip_output_dir / code,
        ubos=ubos,
        title=f"{code} 红筹架构穿透图",
    )
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


@app.command()
def listing(
    backfill: bool = typer.Option(False, "--backfill", help="全量回填：拉官方全表+过滤年份+持久化+L1广东标记"),
    daily: bool = typer.Option(False, "--daily", help="增量：与本地状态diff+处理新增/状态变更"),
    l2: bool = typer.Option(False, "--l2", help="对广东候选下載申请版本PDF做L2结构抽取(注册地/运营实体/VIE)"),
    year: int = typer.Option(2026, "--year", help="统计年份起点（'YYYY以来递表'）"),
    mock: bool = typer.Option(False, "--mock", help="离线模式（仅用于测试， listing 默认在线）"),
) -> None:
    """港交所新上市申请官方清单：全量回填与每日增量监测。

    权威源：披露易「新上市申請版本」静态 JSON（详见 redchip.overseas.hkex_listing）。
    产出：data/listing_state.json（状态基线）+ data/listing_<year>.csv（全量名单）
          + output/gd_hk_ipo_<year>_listing.md（广东候选摘要）。
    """
    settings = config_mod.get_settings()
    settings.redchip_output_dir.mkdir(parents=True, exist_ok=True)
    settings.redchip_data_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = settings.redchip_data_dir / "listing_pdfs"
    csv_path = settings.redchip_data_dir / f"listing_{year}.csv"
    md_path = settings.redchip_output_dir / f"gd_hk_ipo_{year}_listing.md"

    if backfill:
        typer.secho("【全量回填】拉取港交所官方申请版本清单…", fg=typer.colors.CYAN)
        records = listing_mod.fetch_index(settings)
        typer.secho(f"  官方全量记录（全状态×板块，去重）：{len(records)} 条", fg=typer.colors.GREEN)
        year_recs = listing_mod.filter_year(records, year)
        typer.secho(f"  {year} 以来递表（按递交日）：{len(year_recs)} 家", fg=typer.colors.GREEN)
        listing_mod.mark_guangdong(year_recs)
        gd_l1 = [r for r in year_recs if r.gd_l1]
        typer.secho(f"  L1 名称命中广东：{len(gd_l1)} 家", fg=typer.colors.YELLOW)
        _run_l2(settings, year_recs, gd_l1, pdf_dir, l2)
        listing_mod.persist(year_recs)
        _write_listing_outputs(year_recs, csv_path, md_path, year)
        typer.secho(f"  状态基线：{listing_mod.state_path()}", fg=typer.colors.CYAN)
        typer.secho(f"  全量名单：{csv_path}", fg=typer.colors.CYAN)
        typer.secho(f"  广东摘要：{md_path}", fg=typer.colors.CYAN)
        return

    if daily:
        old = listing_mod.load_state()
        typer.secho(f"【增量监测】本地基线 {len(old)} 条，拉取官方最新…", fg=typer.colors.CYAN)
        records = listing_mod.fetch_index(settings)
        new = listing_mod.filter_year(records, year)
        result = listing_mod.diff(old, new)
        typer.secho(f"  新增递表：{len(result['added'])} 家", fg=typer.colors.GREEN)
        typer.secho(f"  状态变更：{len(result['changed'])} 家", fg=typer.colors.YELLOW)
        listing_mod.mark_guangdong(result["added"])
        gd_new = [r for r in result["added"] if r.gd_l1]
        if gd_new:
            typer.secho(f"  其中广东候选（L1）：{len(gd_new)} 家", fg=typer.colors.MAGENTA)
        _run_l2(settings, new, gd_new, pdf_dir, l2)
        # 合并：保留旧记录，新增追加，变更覆盖
        merged = {r.id: r for r in old}
        for r in new:
            merged[r.id] = r
        listing_mod.persist(list(merged.values()))
        if result["added"]:
            _write_listing_outputs(new, csv_path, md_path, year, added=result["added"])
        for r in result["added"]:
            typer.secho(f"    + {r.display_name} ({r.board}/{r.status}) 递表 {r.submit_date}", fg=typer.colors.GREEN)
        return

    typer.secho("请指定 --backfill（全量回填）或 --daily（增量监测）", fg=typer.colors.YELLOW)


def _run_l2(settings, year_recs, gd_recs, pdf_dir, l2: bool) -> None:
    """对广东候选做 L2 结构抽取（注册地/运营实体/VIE）。"""
    if not l2 or not gd_recs:
        return
    typer.secho(f"【L2】下載 {len(gd_recs)} 家广东候选的申请版本PDF并抽取结构…", fg=typer.colors.CYAN)
    for r in gd_recs:
        pdf = listing_mod.download_app_proof(r, pdf_dir, settings)
        if not pdf:
            r.redchip_l2 = "待核验(无申请版本链接)"
            continue
        info = listing_mod.extract_structure(pdf)
        r.domicile = info["domicile"]
        r.gd_opco = info["gd_opco"]
        r.is_vie = info["is_vie"]
        r.redchip_l2 = listing_mod.classify_redchip(r)
        typer.secho(f"    {r.display_name}: 注册地={r.domicile or '?'} 广东实体={'有' if r.gd_opco else '未抽得'} VIE={r.is_vie} → {r.redchip_l2}", fg=typer.colors.GREEN)


def _write_listing_outputs(records, csv_path: Path, md_path: Path, year: int, added=None) -> None:
    """写出全量 CSV 与广东候选 Markdown 摘要。"""
    fields = [
        "id", "name_en", "name_cn", "board", "status", "stock_code", "submit_date",
        "gd_l1", "gd_l1_hit", "domicile", "gd_opco", "is_vie", "redchip_l2", "doc_channel", "app_proof_url",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sorted(records, key=lambda x: x.submit_date):
            w.writerow(r.model_dump())
    # Markdown 摘要
    gd = [r for r in records if r.gd_l1]
    by_status: dict[str, int] = {}
    by_board: dict[str, int] = {}
    for r in records:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_board[r.board] = by_board.get(r.board, 0) + 1
    lines = [
        f"# 港交所 {year} 以来新上市申请 · 官方清单（广东商机视角）",
        "",
        f"> 数据来源：港交所披露易「新上市申請版本及相關資料」官方静态 JSON（全状态×板块，已去重）。",
        f"> 统计口径：递交日 ≥ {year}-01-01。秘密递表（非公开方式）官方不刊发，不在清单内。",
        f"> 生成时间：{__import__('datetime').datetime.now(config_mod.CST).strftime('%Y-%m-%d %H:%M')}",
        "",
        f"## 一、总量",
        f"- {year} 以来递表企业（去重）：**{len(records)} 家**",
        f"- 板块分布：{'；'.join(f'{k} {v}' for k, v in by_board.items())}",
        f"- 状态分布：{'；'.join(f'{k} {v}' for k, v in by_status.items())}",
        f"- L1 名称命中广东：**{len(gd)} 家**（下界，正典红筹壳名不带粤字，需 L2 以 PDF 运营实体核实）",
        "",
        f"## 二、广东候选（L1 名称命中，待 L2 PDF 核实）",
        "",
        "| # | 企业名 | 板块 | 状态 | 代码 | 递表日 | L1命中 | 注册地(L2) | 广东实体(L2) | VIE | 红筹判定(L2) | 通道 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(sorted(gd, key=lambda x: x.submit_date), 1):
        lines.append(
            f"| {i} | {r.display_name} | {r.board} | {r.status} | {r.stock_code or '—'} | {r.submit_date} | "
            f"{r.gd_l1_hit} | {r.domicile or '—'} | {'有' if r.gd_opco else '—'} | "
            f"{'是' if r.is_vie else '—'} | {r.redchip_l2 or '待核验'} | {r.doc_channel or '—'} |"
        )
    if added:
        lines += ["", "## 三、本次增量新增", ""]
        for r in added:
            lines.append(f"- {r.display_name}（{r.board}/{r.status}）递表 {r.submit_date} [申请版本]({r.app_proof_url})")
    lines.append("")
    lines.append("## 不确定项")
    lines.append("- L1 名称匹配会漏掉「开曼壳名 + 广东运营实体」的正典红筹，真实广东数需 L2 全量核实。")
    lines.append("- 关系状态（本行是否已开户 / 他行锁定）公开不可得，须行内 CRM 回填，标「待行内核验」。")
    lines.append("- 红筹判定以 L2 抽得的注册地为准；未下載 PDF 者标「待核验」。")
    md_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    app()
