"""mock 主智能体后的 stream_plan 事件流（不调真实大模型）。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from slots import TravelSlots  # noqa: E402


def _fake_ai_message(*, content: str = "", tool_calls: list | None = None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.tool_call_id = None
    return msg


def _fake_tool_message(*, content: str, tool_call_id: str):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    msg.tool_call_id = tool_call_id
    return msg


@pytest.mark.asyncio
async def test_stream_plan_driving_skips_ticket_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """明确自驾：挂载了 ticket 但主智能体只调 map → ticket step=skipped，summary 非「服务不可用」。"""
    import planner_service as ps
    from slots import TravelSlots
    from unittest.mock import AsyncMock, MagicMock

    slots = TravelSlots(
        origin="杭州",
        destination="杭州",
        date="2026-09-20",
        days=1,
        budget="中等",
        preferences="西湖",
        pace="舒适型节奏",
    )
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=slots))
    ticket_mock = AsyncMock(return_value={"name": "ticket_agent", "tools": [object()]})
    monkeypatch.setattr(ps, "get_ticket_agent", ticket_mock)
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())

    async def fake_astream(*_a, **_k):
        yield {
            "model": {
                "messages": [
                    _fake_ai_message(
                        tool_calls=[
                            {
                                "name": "task",
                                "id": "c1",
                                "args": {"subagent_type": "map_agent"},
                            }
                        ]
                    )
                ]
            }
        }
        yield {
            "tools": {
                "messages": [_fake_tool_message(content="西湖断桥推荐", tool_call_id="c1")]
            }
        }
        yield {"model": {"messages": [_fake_ai_message(content="未查票（无需铁路），已完成景点检索")]}}

    agent = MagicMock()
    agent.astream = fake_astream
    monkeypatch.setattr(ps, "create_deep_agent", lambda **_k: agent)

    async def fake_summary(**kwargs):
        assert kwargs.get("ticket_reason") == "skipped"
        block = ps.ticket_summary_block("skipped", "")
        assert "服务不可用" not in block
        yield {"type": "summary_delta", "content": "## 车票建议\n本次未查票（无需铁路）\n"}
        yield {"type": "_summary_done", "content": "## 车票建议\n本次未查票（无需铁路）\n"}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)

    events = [ev async for ev in ps.stream_plan("杭州自驾逛西湖，不坐火车")]
    subagents = [e.get("name") for e in events if e.get("type") == "subagent"]
    assert "ticket_agent" not in subagents
    assert "map_agent" in subagents

    ticket_mock.assert_not_called()
    ticket_steps = [e for e in events if e.get("type") == "step" and e.get("id") == "ticket"]
    assert any(e.get("status") == "skipped" for e in ticket_steps)
    assert any(
        e.get("type") == "status" and "未查票" in str(e.get("message"))
        for e in events
    )


@pytest.mark.asyncio
async def test_stream_plan_parallel_map_ticket_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨城：同轮两个 task → map/ticket 可同时 active，且都不被提前 done。"""
    import planner_service as ps
    from slots import TravelSlots
    from unittest.mock import AsyncMock, MagicMock

    slots = TravelSlots(
        origin="杭州",
        destination="苏州",
        date="2026-09-26",
        days=1,
        budget="800",
        preferences="园林",
        pace="舒适型节奏",
    )
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=slots))
    monkeypatch.setattr(
        ps,
        "get_ticket_agent",
        AsyncMock(return_value={"name": "ticket_agent", "tools": [object()]}),
    )
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())

    async def fake_astream(*_a, **_k):
        # 同一 model 消息内两个 task = 同轮并行
        yield {
            "model": {
                "messages": [
                    _fake_ai_message(
                        tool_calls=[
                            {"name": "task", "id": "m1", "args": {"subagent_type": "map_agent"}},
                            {"name": "task", "id": "t1", "args": {"subagent_type": "ticket_agent"}},
                        ]
                    )
                ]
            }
        }
        yield {
            "tools": {
                "messages": [
                    _fake_tool_message(content="拙政园", tool_call_id="m1"),
                    _fake_tool_message(content="G7586", tool_call_id="t1"),
                ]
            }
        }
        yield {"model": {"messages": [_fake_ai_message(content="已完成查票与景点检索")]}}

    agent = MagicMock()
    agent.astream = fake_astream
    monkeypatch.setattr(ps, "create_deep_agent", lambda **_k: agent)

    async def fake_summary(**kwargs):
        assert kwargs.get("ticket_reason") == "ok"
        yield {"type": "_summary_done", "content": "ok"}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)

    events = [ev async for ev in ps.stream_plan("下周六杭州坐高铁去苏州一日游")]

    # 在 map/ticket 任一 done 之前，两者都曾进入 active（支持并行 UI）
    active_ids: list[str] = []
    for e in events:
        if e.get("type") != "step":
            continue
        sid = str(e.get("id"))
        if sid not in ("map", "ticket"):
            continue
        if e.get("status") == "active":
            active_ids.append(sid)
        if e.get("status") == "done" and sid in ("map", "ticket"):
            break
    assert "map" in active_ids
    assert "ticket" in active_ids

    subagents = [e.get("name") for e in events if e.get("type") == "subagent"]
    assert subagents.count("map_agent") >= 1
    assert subagents.count("ticket_agent") >= 1
    # 两个 subagent 事件之间不应插入对方的 tool_result（同轮发出）
    idx_map = next(i for i, e in enumerate(events) if e.get("type") == "subagent" and e.get("name") == "map_agent")
    idx_ticket = next(
        i for i, e in enumerate(events) if e.get("type") == "subagent" and e.get("name") == "ticket_agent"
    )
    lo, hi = sorted((idx_map, idx_ticket))
    between = events[lo + 1 : hi]
    assert not any(e.get("type") == "tool_result" for e in between)
    finals = [e for e in events if e.get("type") == "final"]
    assert finals
    assert finals[0].get("timings", {}).get("parallel_dispatch") is True


@pytest.mark.asyncio
async def test_stream_plan_missed_ticket_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner_service as ps
    from unittest.mock import AsyncMock, MagicMock

    slots = TravelSlots(
        origin="新乡",
        destination="郑州",
        date="2026-09-19",
        days=1,
        budget="500",
        preferences="少折腾",
        pace="舒适型节奏",
    )
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=slots))
    monkeypatch.setattr(
        ps,
        "get_ticket_agent",
        AsyncMock(return_value={"name": "ticket_agent", "tools": [object()]}),
    )
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())

    async def fake_astream(*_a, **_k):
        yield {
            "model": {
                "messages": [
                    _fake_ai_message(
                        tool_calls=[{"name": "task", "id": "m1", "args": {"subagent_type": "map_agent"}}]
                    )
                ]
            }
        }
        yield {"tools": {"messages": [_fake_tool_message(content="二七塔", tool_call_id="m1")]}}

    agent = MagicMock()
    agent.astream = fake_astream
    monkeypatch.setattr(ps, "create_deep_agent", lambda **_k: agent)

    async def fake_summary(**kwargs):
        assert kwargs.get("ticket_reason") == "missed"
        yield {"type": "_summary_done", "content": "ok"}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)
    events = [ev async for ev in ps.stream_plan("新乡坐高铁去郑州")]
    assert any(e.get("type") == "warning" and e.get("code") == "ticket_not_dispatched" for e in events)


@pytest.mark.asyncio
async def test_watch_aiter_timeout_closes() -> None:
    import asyncio

    from planner.async_utils import watch_aiter

    closed = {"n": 0}

    async def gen():
        try:
            yield 1
            await asyncio.sleep(5)
            yield 2
        finally:
            closed["n"] += 1

    with pytest.raises(asyncio.TimeoutError):
        async for _ in watch_aiter(gen(), timeout_seconds=0.15):
            pass
    assert closed["n"] == 1


@pytest.mark.asyncio
async def test_plan_id_path_traversal_rejected() -> None:
    import planner_service as ps

    events = [ev async for ev in ps.stream_plan("改景点", parent_plan_id="../../.env")]
    assert events
    assert events[0].get("type") == "error"
    assert "plan_id" in str(events[0].get("message"))


@pytest.mark.asyncio
async def test_map_only_revision_skips_extract_and_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4
    from unittest.mock import AsyncMock, MagicMock

    import planner_service as ps
    from planner.summary import save_snapshot

    pid = str(uuid4())
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "新乡租车去洛阳",
            "slots": TravelSlots(
                origin="新乡",
                destination="洛阳",
                date="2026-09-26",
                days=2,
                budget="中等",
                preferences="轻松",
                pace="舒适型节奏",
            ).model_dump(),
            "ticket_reason": "skipped",
            "map_context": "龙门石窟",
            "ticket_context": "",
            "answer": "## 需求摘要\n旧方案",
        }
    )
    extract_mock = AsyncMock(side_effect=AssertionError("extract_slots 不应被调用"))
    ticket_mock = AsyncMock(side_effect=AssertionError("get_ticket_agent 不应被调用"))
    monkeypatch.setattr(ps, "extract_slots", extract_mock)
    monkeypatch.setattr(ps, "get_ticket_agent", ticket_mock)
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())

    async def fake_astream(*_a, **_k):
        yield {
            "model": {
                "messages": [
                    _fake_ai_message(
                        tool_calls=[{"name": "task", "id": "m1", "args": {"subagent_type": "map_agent"}}]
                    )
                ]
            }
        }
        yield {"tools": {"messages": [_fake_tool_message(content="白马寺", tool_call_id="m1")]}}

    agent = MagicMock()
    agent.astream = fake_astream
    monkeypatch.setattr(ps, "create_deep_agent", lambda **_k: agent)

    async def fake_summary(**kwargs):
        yield {"type": "_summary_done", "content": "revised"}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)
    events = [
        ev
        async for ev in ps.stream_plan(
            "不要龙门石窟，改白马寺，路线顺一点",
            parent_plan_id=pid,
        )
    ]
    extract_mock.assert_not_called()
    ticket_mock.assert_not_called()


@pytest.mark.asyncio
async def test_stream_plan_map_timeout_not_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import planner_service as ps

    slots = TravelSlots(
        origin="新乡",
        destination="郑州",
        date="2026-09-19",
        days=1,
        budget="500",
        preferences="少折腾",
        pace="舒适型节奏",
    )
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=slots))
    monkeypatch.setattr(ps, "get_ticket_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setenv("DISPATCH_TIMEOUT_SECONDS", "0.2")

    async def fake_astream(*_a, **_k):
        yield {
            "model": {
                "messages": [
                    _fake_ai_message(
                        tool_calls=[{"name": "task", "id": "m1", "args": {"subagent_type": "map_agent"}}]
                    )
                ]
            }
        }
        await asyncio.sleep(2)

    agent = MagicMock()
    agent.astream = fake_astream
    monkeypatch.setattr(ps, "create_deep_agent", lambda **_k: agent)

    async def fake_summary(**kwargs):
        assert kwargs.get("map_reason") == "timeout"
        yield {"type": "_summary_done", "content": "ok"}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)
    events = [ev async for ev in ps.stream_plan("这周六从新乡坐高铁去郑州一日游")]
    assert any(e.get("type") == "warning" and e.get("code") == "map_dispatch_timeout" for e in events)
    assert not any(e.get("code") == "map_not_dispatched" for e in events)
    map_steps = [e for e in events if e.get("type") == "step" and e.get("id") == "map"]
    assert any(e.get("status") == "skipped" and e.get("reason") == "dispatch_timeout" for e in map_steps)
    assert any(e.get("type") == "final" for e in events)

