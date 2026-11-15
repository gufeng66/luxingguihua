"""离线回放不能污染下一次真实规划，临时产物必须有明确生命周期。"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from evals.demo_replay import replay


def _bindings() -> list[tuple[object, str, object]]:
    import planner.pipeline as pipeline
    import planner_service as ps

    bindings = [(ps, name, getattr(ps, name)) for name in (
        "build_llm", "get_ticket_agent", "stream_summary",
    )]
    bindings.append((pipeline, "assemble_itinerary", pipeline.assemble_itinerary))
    for modname in ("planner.paths", "planner.summary", "planner.pipeline", "planner.map_fallback"):
        mod = importlib.import_module(modname)
        bindings.extend((mod, name, getattr(mod, name)) for name in (
            "RESULTS_DIR", "MAPS_DIR", "SNAPSHOTS_DIR",
        ) if hasattr(mod, name))
    return bindings


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_replay_restores_globals_and_cleans_temp_files(monkeypatch, fail: bool) -> None:
    import planner.paths as paths
    import planner_service as ps

    monkeypatch.delenv("TRAVEL_MAP_FALLBACK", raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-only-not-a-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    before = _bindings()
    env_before = {name: os.environ.get(name) for name in (
        "PLANS_DB", "TRAVEL_MAP_FALLBACK", "LANGSMITH_API_KEY",
        "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
    )}
    stream_plan = ps.stream_plan
    roots: list[Path] = []

    async def observe(*args, **kwargs):
        roots.append(paths.RESULTS_DIR)
        assert roots[-1].is_dir()
        assert os.environ["LANGSMITH_API_KEY"] == ""
        assert os.environ["LANGSMITH_TRACING"] == "false"
        if fail:
            raise RuntimeError("回放中断")
        async for event in stream_plan(*args, **kwargs):
            yield event

    monkeypatch.setattr(ps, "stream_plan", observe)
    if fail:
        with pytest.raises(RuntimeError, match="回放中断"):
            await replay("happy")
    else:
        final = await replay("happy")
        assert "G7586" in final["content"]
        assert final.get("map_url")

    for mod, name, value in before:
        assert getattr(mod, name) is value
    assert {name: os.environ.get(name) for name in env_before} == env_before
    assert roots and all(not root.exists() for root in roots)


@pytest.mark.asyncio
async def test_replay_can_keep_artifacts_in_caller_directory(tmp_path: Path) -> None:
    root = tmp_path / "replay-output"
    final = await replay("happy", output_dir=str(root))
    assert (root / "maps" / f"{final['plan_id']}.html").is_file()
    assert (root / "snapshots" / f"{final['plan_id']}.json").is_file()
    assert final["map_url"] == f"/results/maps/{final['plan_id']}.html"
