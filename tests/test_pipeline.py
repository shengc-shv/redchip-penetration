"""离线端到端流水线测试（REDCHIP_MOCK=true，不发起任何外部请求）。"""

from redchip.config import reload_settings
from redchip.pipeline import run as pipeline


def _mock_env(monkeypatch, tmp_path):
    """切到离线模式并把产物目录指向临时目录。

    Args:
        monkeypatch: pytest 的 monkeypatch 夹具。
        tmp_path: 临时目录夹具。

    Returns:
        Settings: 生效配置。
    """
    monkeypatch.setenv("REDCHIP_MOCK", "true")
    monkeypatch.setenv("REDCHIP_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("REDCHIP_DATA_DIR", str(tmp_path / "data"))
    return reload_settings()


def test_end_to_end_produces_all_artifacts(monkeypatch, tmp_path):
    """一次运行应产出报告、图谱与各类图文件。"""
    cfg = _mock_env(monkeypatch, tmp_path)
    report = pipeline.run_hk("00700", "腾讯控股", ["腾讯"], cfg)

    assert report.nodes, "应构建出图谱节点"
    assert report.owns, "应构建出股权边"
    out = tmp_path / "output" / "00700"
    for name in ("report.md", "graph.json", "result.json", "llm_a.json", "penetration_graph.svg"):
        assert (out / name).exists(), f"缺少产物 {name}"
    assert (out / "penetration_graph.mermaid").exists()
    assert (out / "penetration_graph.dot").exists()


def test_guangdong_filter_identifies_domestic_entities(monkeypatch, tmp_path):
    """应识别出广东省内运营实体。"""
    cfg = _mock_env(monkeypatch, tmp_path)
    report = pipeline.run_hk("00700", "腾讯控股", ["腾讯"], cfg)
    assert report.is_guangdong is True
    assert any("深圳" in name or "腾讯" in name for name in report.guangdong_entities)


def test_vie_control_edge_is_detected(monkeypatch, tmp_path):
    """含「合约安排」关键词时，应识别为 VIE 架构并生成协议控制边。"""
    cfg = _mock_env(monkeypatch, tmp_path)
    report = pipeline.run_hk("00700", "腾讯控股", ["腾讯"], cfg)
    assert report.controls, "应识别到 VIE 协议控制边"


def test_ubo_is_penetrated(monkeypatch, tmp_path):
    """应穿透出满足阈值的最终受益人。"""
    cfg = _mock_env(monkeypatch, tmp_path)
    report = pipeline.run_hk("00700", "腾讯控股", ["腾讯"], cfg)
    assert any(u.name == "马化腾" for u in report.ubos)
    top = report.ubos[0]
    assert top.cumulative_share >= 0.25


def test_missing_llm_key_is_downgraded_not_crashed(monkeypatch, tmp_path):
    """既无 LLM Key 又无本地分析结果时，应降级为规则兜底并标记需人工复核。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = _mock_env(monkeypatch, tmp_path)
    # 屏蔽 fixtures/manual 下的本地分析结果，验证真正的降级路径
    monkeypatch.setattr(
        pipeline, "manual_llm_a_path", lambda code, cfg: tmp_path / "absent.json"
    )
    report = pipeline.run_hk("00700", "腾讯控股", ["腾讯"], cfg)
    assert report.needs_review is True
    assert any("LLM" in reason for reason in report.review_reasons)


def test_batch_run_writes_summary(monkeypatch, tmp_path):
    """批量运行应写出汇总索引。"""
    _mock_env(monkeypatch, tmp_path)
    reports = pipeline.run_batch(["00700"], names={"00700": "腾讯控股"}, keywords={"00700": ["腾讯"]})
    assert len(reports) == 1
    assert (tmp_path / "output" / "summary.md").exists()


def test_unknown_code_does_not_break_batch(monkeypatch, tmp_path):
    """单家企业失败不应中断整批运行。"""
    _mock_env(monkeypatch, tmp_path)
    reports = pipeline.run_batch(["00000"])
    assert len(reports) == 1
    assert reports[0].needs_review is True
