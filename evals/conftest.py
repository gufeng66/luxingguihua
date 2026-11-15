"""单测里的规划落盘不要写进真实 plans.db。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_external_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不继承本机追踪配置，避免离线测试访问 LangSmith。"""
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


@pytest.fixture(autouse=True)
def plans_db_tmp(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANS_DB", str(tmp_path / "plans.db"))
    root = tmp_path / "results"
    maps = root / "maps"
    snaps = root / "snapshots"
    for path in (root, maps, snaps):
        path.mkdir()
    monkeypatch.setattr("planner.paths.RESULTS_DIR", root)
    monkeypatch.setattr("planner.paths.MAPS_DIR", maps)
    monkeypatch.setattr("planner.paths.SNAPSHOTS_DIR", snaps)
    monkeypatch.setattr("planner.summary.RESULTS_DIR", root)
    monkeypatch.setattr("planner.summary.SNAPSHOTS_DIR", snaps)
    monkeypatch.setattr("planner.pipeline.MAPS_DIR", maps)
    monkeypatch.setattr("planner.map_fallback.MAPS_DIR", maps)


@pytest.fixture(autouse=True)
def no_live_map_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件流单测不打高德。补写用例把 TRAVEL_MAP_FALLBACK 设回 1。"""
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")
