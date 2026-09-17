"""
LLM 集成测试（需要真实 OPENAI_API_KEY；默认带 @pytest.mark.integration）。

小白说明：
  - 这些测试会真的花钱调 DeepSeek，所以平时 CI 可不跑
  - 运行：pytest -m integration evals/test_integration.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def llm():
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv())
    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        pytest.skip("缺少 OPENAI_API_KEY，跳过 integration")
    from planner_service import build_llm

    return build_llm()


@pytest.mark.asyncio
async def test_extract_slots_hangzhou_suzhou(llm) -> None:
    from planner_service import extract_slots

    slots = await extract_slots(
        "帮我规划下周六从杭州去苏州一日游，预算 800，想看园林",
        llm,
        today="2026-09-17",
    )
    assert slots.origin and "杭州" in slots.origin
    assert slots.destination and "苏州" in slots.destination
    assert slots.date == "2026-09-26"


@pytest.mark.asyncio
async def test_extract_failure_degrades_in_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """抽取抛错时应发 status 降级提示，而不是直接 error 退出入口。"""
    import planner_service as ps

    async def boom(*_a, **_k):
        raise RuntimeError("structured output failed")

    monkeypatch.setattr(ps, "extract_slots", boom)
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "get_ticket_agent", AsyncMock(return_value=None))

    # 避免真跑 deep agent：在创建 agent 前截断
    def fake_create(*_a, **_k):
        raise RuntimeError("stop-after-hitl")

    monkeypatch.setattr(ps, "create_deep_agent", fake_create)

    events = []
    async for ev in ps.stream_plan("去苏州玩"):
        events.append(ev)
        if ev.get("type") == "error" and "stop-after-hitl" in str(ev.get("message")):
            break

    statuses = [e.get("message") for e in events if e.get("type") == "status"]
    assert any("参数解析失败" in str(m) for m in statuses)
    assert not any(e.get("type") == "clarify" for e in events)
