"""修订约束覆盖与成文等待超时，不调用网络。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from planner.routing import skip_ticket_reason
from planner.summary import degraded_plan, save_snapshot, stream_summary
from slots import ExtractedSlots, TravelSlots


def _slots(**changes) -> TravelSlots:
    return TravelSlots.model_validate({
        "origin": "新乡", "destination": "郑州", "date": "2026-11-10",
        "days": 1, "transport_mode": "public_transit", **changes,
    })


@pytest.mark.parametrize(("mode", "query", "previous", "expected"), [
    ("driving", "改成自驾", "从新乡坐高铁去郑州", "no_rail_intent"),
    ("driving", "推迟一天", "从新乡坐高铁去郑州", "no_rail_intent"),
    ("driving", "改坐高铁", "从新乡自驾去郑州", None),
    ("public_transit", "推迟一天", "从新乡坐飞机去北京", "no_rail_intent"),
])
def test_current_intent_then_merged_slots_then_history(mode, query, previous, expected):
    assert skip_ticket_reason(_slots(transport_mode=mode), query, previous_query=previous) == expected


def _snapshot(query: str, slots: TravelSlots, ticket_reason: str = "skipped") -> str:
    pid = str(uuid4())
    save_snapshot({
        "plan_id": pid, "query": query, "slots": slots.model_dump(),
        "ticket_reason": ticket_reason, "answer": degraded_plan(ticket_reason, []),
    })
    return pid


async def _summary(**_kwargs):
    yield {"type": "_summary_done", "content": degraded_plan("skipped", [])}


@pytest.mark.asyncio
async def test_rail_to_driving_revision_does_not_connect_ticket(monkeypatch):
    import planner_service as ps

    pid = _snapshot("从新乡坐高铁去郑州一日游", _slots(), "ok")
    ticket = AsyncMock(side_effect=AssertionError("自驾不应连票务"))
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=ExtractedSlots(transport_mode="driving")))
    monkeypatch.setattr(ps, "get_ticket_agent", ticket)
    monkeypatch.setattr(ps, "stream_summary", _summary)
    events = [e async for e in ps.stream_plan("改成自驾", parent_plan_id=pid)]
    ticket.assert_not_called()
    assert any(e.get("type") == "final" for e in events)
    assert any(e.get("id") == "ticket" and e.get("reason") == "no_rail_intent" for e in events)


@pytest.mark.asyncio
async def test_longer_revision_clarifies_then_accepts_confirmed_slots(monkeypatch):
    import planner_service as ps

    pid = _snapshot("从新乡自驾去郑州一日游", _slots(transport_mode="driving"))
    extract = AsyncMock(return_value=ExtractedSlots(days=2))
    ticket = AsyncMock(side_effect=AssertionError("未确认住宿不应连接票务"))
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "extract_slots", extract)
    monkeypatch.setattr(ps, "get_ticket_agent", ticket)
    monkeypatch.setattr(ps, "stream_summary", _summary)
    first = [e async for e in ps.stream_plan("改成两天", parent_plan_id=pid)]
    clarify = next(e for e in first if e.get("type") == "clarify")
    assert clarify["missing"] == ["lodging"]
    assert not any(e.get("type") in {"final", "subagent"} for e in first)
    ticket.assert_not_called()

    confirmed = TravelSlots.model_validate({**clarify["slots"], "lodging": "郑州站附近酒店"})
    second = [e async for e in ps.stream_plan("改成两天", slots=confirmed, parent_plan_id=pid)]
    extract.assert_awaited_once()
    assert any(e.get("type") == "final" for e in second)
    assert not any(e.get("type") == "clarify" for e in second)


class _StalledLLM:
    def __init__(self, emit_first: bool):
        self.emit_first = emit_first
        self.closed = False
        self.calls = 0

    async def astream(self, _messages):
        self.calls += 1
        try:
            if self.emit_first:
                yield SimpleNamespace(content="未校验 G1234")
            await asyncio.Event().wait()
        finally:
            self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_first", [False, True])
async def test_summary_deadline_interrupts_wait_and_closes_stream(emit_first):
    llm = _StalledLLM(emit_first)
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def consume():
        return [e async for e in stream_summary(
            llm=llm, query="同城游", slots=None, map_context="", ticket_context="",
            ticket_reason="skipped", timeout_seconds=0.04,
        )]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(consume(), timeout=1)
    assert loop.time() - started < 0.7
    assert llm.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_first", [False, True])
async def test_summary_timeout_finishes_with_structured_fallback(monkeypatch, emit_first):
    import planner_service as ps

    llm = _StalledLLM(emit_first)
    monkeypatch.setattr(ps, "build_llm", lambda: llm)
    monkeypatch.setenv("PLAN_SUMMARY_SECONDS", "0.04")

    async def consume():
        return [e async for e in ps.stream_plan(
            "郑州一日游，不要地图", slots=_slots(origin="郑州"),
        )]

    events = await asyncio.wait_for(consume(), timeout=1)
    final = next(e for e in events if e.get("type") == "final")
    assert final["timings"]["summary_gate"] == "degraded"
    assert "超时" in final["content"]
    assert "G1234" not in final["content"]
    assert not any(e.get("type") == "error" for e in events)
    assert llm.calls == 1 and llm.closed
