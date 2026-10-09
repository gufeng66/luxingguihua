"""单测里的规划落盘不要写进真实 plans.db。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def plans_db_tmp(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANS_DB", str(tmp_path / "plans.db"))


@pytest.fixture(autouse=True)
def no_live_map_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件流单测不打高德。补写用例把 TRAVEL_MAP_FALLBACK 设回 1。"""
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")
