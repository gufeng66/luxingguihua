"""
确定性纯函数单测（默认 CI 会跑；不调用真实大模型）。

小白说明：
  - test_unit.py：测 slots 换算、提示词关键字、票务三态、以及用 mock 假数据测 stream_plan 事件
  - 运行：pytest evals/test_unit.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.checkers import (  # noqa: E402
    assert_no_fabricated_trains,
    has_required_sections,
    missing_sections,
)
from slots import (  # noqa: E402
    TravelSlots,
    apply_soft_defaults,
    missing_critical,
    resolve_relative_date,
    should_clarify,
    soft_defaults_needed,
)


def test_missing_critical_only_origin_dest_date() -> None:
    slots = TravelSlots(origin="杭州", destination="苏州", date="2026-09-26")
    assert missing_critical(slots) == []
    assert soft_defaults_needed(slots) == ["days", "budget", "preferences", "pace"]


def test_soft_missing_does_not_block() -> None:
    slots = TravelSlots(origin="杭州", destination="苏州", date="2026-09-26")
    assert should_clarify(slots, slots_from_client=False) is False
    filled, applied = apply_soft_defaults(slots)
    assert filled.days == 1
    assert filled.budget == "中等"
    assert filled.preferences == "经典景点 + 少折腾"
    assert filled.pace == "舒适型节奏"
    assert set(applied) == {"days", "budget", "preferences", "pace"}


def test_critical_missing_triggers_clarify() -> None:
    slots = TravelSlots(destination="苏州", date="2026-09-26", days=2, budget="800")
    assert "origin" in missing_critical(slots)
    assert should_clarify(slots, slots_from_client=False) is True


def test_client_slots_complete_no_reclarify() -> None:
    """slots 已提供且 critical 齐 → 不得再次 clarify。"""
    slots = TravelSlots(
        origin="杭州",
        destination="苏州",
        date="2026-09-26",
        days=1,
        budget="中等",
        preferences="园林",
        pace="舒适型节奏",
    )
    assert should_clarify(slots, slots_from_client=True) is False


def test_client_slots_still_missing_critical_clarifies() -> None:
    slots = TravelSlots(origin="杭州", destination="苏州")  # date 缺
    assert should_clarify(slots, slots_from_client=True) is True


def test_resolve_tomorrow() -> None:
    today = date(2026, 9, 17)  # 周四
    assert resolve_relative_date("明天", today=today) == "2026-09-18"


def test_resolve_next_saturday() -> None:
    today = date(2026, 9, 17)  # 周四
    assert resolve_relative_date("下周六", today=today) == "2026-09-26"


def test_resolve_iso_passthrough() -> None:
    assert resolve_relative_date("2026-10-01", today=date(2026, 9, 17)) == "2026-10-01"


def test_travel_slots_rejects_bad_date() -> None:
    with pytest.raises(ValidationError):
        TravelSlots(origin="杭州", destination="苏州", date="下周六")


def test_travel_slots_rejects_non_positive_days() -> None:
    with pytest.raises(ValidationError):
        TravelSlots(origin="杭州", destination="苏州", date="2026-09-26", days=0)


def test_required_sections_checker() -> None:
    ok = """
## 需求摘要
x
## 景点建议
x
## 车票建议
暂无可靠票务数据
## 预算
x
## 行程表
x
## 注意事项
x
"""
    assert has_required_sections(ok)
    bad = "## 需求摘要\n只有一节"
    assert missing_sections(bad) == [
        "景点建议",
        "车票建议",
        "预算",
        "行程表",
        "注意事项",
    ]


def test_no_fabricate_train_gdckt() -> None:
    clean = "## 车票建议\n暂无可靠票务数据，请稍后重试。"
    assert_no_fabricated_trains(clean)

    for num in ("G7304", "D3121", "C1234", "K123", "T4567"):
        with pytest.raises(AssertionError):
            assert_no_fabricated_trains(f"推荐乘坐 {num} 次列车")


def test_resolve_ticket_reason_tri_state() -> None:
    from planner_service import resolve_ticket_reason

    assert resolve_ticket_reason(ticket_mounted=False, has_ticket_result=False) == "unavailable"
    assert resolve_ticket_reason(ticket_mounted=True, has_ticket_result=False) == "not_needed"
    assert resolve_ticket_reason(ticket_mounted=True, has_ticket_result=True) == "ok"


def test_ticket_summary_block_not_needed_not_unavailable() -> None:
    """业务跳过不得写成「车票服务不可用」。"""
    from planner_service import ticket_summary_block

    block = ticket_summary_block("not_needed", "")
    assert "无需铁路" in block or "未调度查票" in block
    assert "服务不可用" not in block

    unavailable = ticket_summary_block("unavailable", "")
    assert "服务不可用" in unavailable or "暂无可靠票务" in unavailable

    ok = ticket_summary_block("ok", "推荐 G1234 杭州东→苏州")
    assert "G1234" in ok


def test_main_prompt_requires_parallel_and_intent_priority() -> None:
    from planner_service import MAIN_AGENT_PROMPT, MAIN_AGENT_PROMPT_LEGACY, _build_main_prompt

    for text in (MAIN_AGENT_PROMPT, MAIN_AGENT_PROMPT_LEGACY):
        assert "同一轮" in text
        assert "不要调用 ticket_agent" in text
        assert "优先于" in text and "用户画像" in text

    built = _build_main_prompt(
        ticket_available=True,
        plan_id="p1",
        slots=TravelSlots(origin="杭州", destination="苏州", date="2026-09-26", days=1),
    )
    assert "车票服务不可用" not in built
    assert "{ticket_patch}" not in built

    built_off = _build_main_prompt(ticket_available=False, plan_id="p1", legacy=True)
    assert "车票服务不可用" in built_off


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
        assert kwargs.get("ticket_reason") == "not_needed"
        block = ps.ticket_summary_block("not_needed", "")
        assert "服务不可用" not in block
        yield {"type": "summary_delta", "content": "## 车票建议\n本次未查票（无需铁路）\n"}
        yield {"type": "_summary_done", "content": "## 车票建议\n本次未查票（无需铁路）\n"}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)

    events = [ev async for ev in ps.stream_plan("杭州自驾逛西湖，不坐火车")]
    subagents = [e.get("name") for e in events if e.get("type") == "subagent"]
    assert "ticket_agent" not in subagents
    assert "map_agent" in subagents

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
