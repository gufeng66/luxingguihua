"""
确定性纯函数单测（默认 CI 会跑；不调用真实大模型）。

小白说明：
  - test_unit.py：slots、章节检查、提示词关键字
  - test_stream_plan.py：mock 主智能体后的事件流
  - 运行：pytest evals/test_unit.py evals/test_stream_plan.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

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
    format_slots_for_prompt,
    lodging_route_rules,
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


def test_lodging_empty_is_none_and_not_critical() -> None:
    slots = TravelSlots(origin="杭州", destination="苏州", date="2026-09-26", lodging="  ")
    assert slots.lodging is None
    assert missing_critical(slots) == []
    assert lodging_route_rules(None, "苏州", True) == ""
    assert lodging_route_rules("  ", "苏州", True) == ""


def test_lodging_route_rules_loop_and_rail() -> None:
    loop = lodging_route_rules("平江路亚朵", "苏州", False)
    assert "从住宿地出发" in loop
    assert "回到住宿地" in loop
    assert "放行李" not in loop
    assert "车站" not in loop

    rail = lodging_route_rules("平江路亚朵", "苏州", True)
    assert "放行李" in rail
    assert "回到住宿地之后" in rail
    assert "住宿地 → 车站" in rail


def test_format_slots_without_rail_unchanged_when_no_lodging() -> None:
    slots = TravelSlots(origin="杭州", destination="苏州", date="2026-09-26", days=1)
    text = format_slots_for_prompt(slots)
    assert "住宿地闭环" not in text
    assert "出发地：杭州" in text
    with_rail = format_slots_for_prompt(slots, rail=True)
    assert with_rail == text


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
    mixed = "## 车票建议\n本次未查票（无需铁路）\n## 预算\n暂无上限"
    assert_no_fabricated_trains(mixed)

    for num in ("G7304", "D3121", "C1234", "K123", "T4567"):
        with pytest.raises(AssertionError):
            assert_no_fabricated_trains(f"## 车票建议\n推荐乘坐 {num} 次列车")


def test_resolve_ticket_state_five_state() -> None:
    from planner_service import resolve_ticket_state

    assert (
        resolve_ticket_state(
            need_ticket=True, ticket_mounted=False, has_ticket_result=False, timed_out=False
        )
        == "unavailable"
    )
    assert (
        resolve_ticket_state(
            need_ticket=False, ticket_mounted=False, has_ticket_result=False, timed_out=False
        )
        == "skipped"
    )
    assert (
        resolve_ticket_state(
            need_ticket=True, ticket_mounted=True, has_ticket_result=True, timed_out=False
        )
        == "ok"
    )


def test_ticket_summary_block_skipped_not_unavailable() -> None:
    from planner_service import ticket_summary_block

    block = ticket_summary_block("skipped", "")
    assert "无需铁路" in block or "未查票" in block
    assert "服务不可用" not in block

    unavailable = ticket_summary_block("unavailable", "")
    assert "服务不可用" in unavailable or "暂无可靠票务" in unavailable

    ok = ticket_summary_block("ok", "推荐 G1234 杭州东→苏州")
    assert "G1234" in ok


def test_main_prompt_requires_parallel_and_intent_priority() -> None:
    from planner_service import _build_main_prompt

    built = _build_main_prompt(
        ticket_available=True,
        plan_id="p1",
        need_ticket=True,
        slots=TravelSlots(origin="杭州", destination="苏州", date="2026-09-26", days=1),
    )
    assert "同一轮" in built
    assert "必须调度 map_agent" in built
    assert "车票服务不可用" not in built
    assert "{ticket_patch}" not in built
    assert "{map_patch}" not in built
    assert "必须把路线地图 HTML" in built

    built_off = _build_main_prompt(
        ticket_available=False,
        plan_id="p1",
        legacy=True,
        need_ticket=True,
    )
    assert "车票服务不可用" in built_off

    no_rail = _build_main_prompt(
        ticket_available=True,
        plan_id="p1",
        need_ticket=False,
        skip_reason="no_rail_intent",
        slots=TravelSlots(origin="新乡", destination="洛阳", date="2026-09-26", days=1),
    )
    assert "不要调用 ticket_agent" in no_rail or "禁止调用 ticket_agent" in no_rail
    assert "amapTaskData" in no_rail


def test_map_agent_prompt_amap_task_data_contract() -> None:
    from planner.map_agent import MAP_AGENT_PROMPT

    assert "amapTaskData" in MAP_AGENT_PROMPT
    assert '"day":1' in MAP_AGENT_PROMPT or "day\":1" in MAP_AGENT_PROMPT
    assert "application/json" in MAP_AGENT_PROMPT
    assert "overscroll-behavior" in MAP_AGENT_PROMPT
    assert "encodeURIComponent" in MAP_AGENT_PROMPT
    assert "percent-encoding" in MAP_AGENT_PROMPT
    assert "uri.amap.com/marker" in MAP_AGENT_PROMPT
    assert "amapApp" in MAP_AGENT_PROMPT


def test_map_iframe_sandbox_allows_amap_popups() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "allow-popups" in html
    assert "allow-popups-to-escape-sandbox" in html
    assert 'sandbox="allow-scripts allow-same-origin"' not in html
    assert "map-days" in html
    assert "setMapDay" in html


@pytest.mark.asyncio
async def test_ticket_cache_single_handshake_and_fail_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发只握手一次；失败后冷却期内不再连 MCP。"""
    import asyncio

    import planner.ticket_cache as tc

    tc._ticket_cache = None
    tc._fail_until = 0.0
    calls = 0
    spec = {"name": "ticket_agent"}

    async def ok_build():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return spec, object()

    monkeypatch.setattr(tc, "build_ticket_agent", ok_build)
    first, second = await asyncio.gather(tc.get_ticket_agent(), tc.get_ticket_agent())
    assert first is spec and second is spec
    assert calls == 1

    async def down():
        nonlocal calls
        calls += 1
        raise RuntimeError("mcp down")

    tc._ticket_cache = None
    monkeypatch.setattr(tc, "build_ticket_agent", down)
    assert await tc.get_ticket_agent() is None
    assert await tc.get_ticket_agent() is None
    assert calls == 2
    assert tc.last_lookup()["connect_ms"] == 0.0
    tc._ticket_cache = None
    tc._fail_until = 0.0


@pytest.mark.asyncio
async def test_extract_slots_uses_json_mode() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from planner.llm import extract_slots
    from slots import ExtractedSlots

    llm = MagicMock()
    structured = MagicMock()
    structured.ainvoke = AsyncMock(
        return_value=ExtractedSlots(origin="杭州", destination="苏州", date="2026-09-26")
    )
    llm.with_structured_output = MagicMock(return_value=structured)
    slots = await extract_slots("杭州去苏州", llm, today="2026-09-21")
    assert slots.origin == "杭州"
    llm.with_structured_output.assert_called_once()
    assert llm.with_structured_output.call_args.kwargs["method"] == "json_mode"


