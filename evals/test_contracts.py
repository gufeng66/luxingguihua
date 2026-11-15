"""预算、取消、票务参数和开放规则这些契约。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel


def test_poi_gate_sample_manifest_has_required_strata_and_thresholds() -> None:
    payload = json.loads(Path("evals/poi_gate_samples.json").read_text(encoding="utf-8"))
    samples = payload["samples"]
    categories = {sample["category"] for sample in samples}
    assert len(samples) == payload["acceptance"]["sample_count"] == 30
    assert categories == {
        "verified_sight", "no_structured_itinerary", "unlocated", "ambiguous_name",
        "lodging_or_station", "date_inapplicable", "reference_travel_time",
    }
    assert payload["acceptance"]["critical_false_positives"] == 0
    assert payload["acceptance"]["max_false_positive_rate"] == 0.1
    assert payload["acceptance"]["max_false_negative_rate"] == 0.1


def test_integration_matrix_has_unique_required_scenarios() -> None:
    payload = json.loads(Path("evals/integration_matrix.json").read_text(encoding="utf-8"))
    ids = [scenario["id"] for scenario in payload["scenarios"]]
    assert len(ids) == len(set(ids)) == 12
    assert {"integration_model_slot_extract", "integration_mcp_available",
            "integration_amap_poi", "integration_tavily_opening",
            "integration_sse_disconnect"} <= set(ids)


def test_live_ticket_sample_becomes_facts() -> None:
    from planner.ticket_facts import parse_ticket_payload

    payload = json.loads(Path("planner/ticket_samples/mcp_content_block.json").read_text(encoding="utf-8"))
    facts, diag = parse_ticket_payload(
        payload,
        source_id="raw-live",
        queried_at="2026-11-03T00:00:00+00:00",
        expect_date="2026-11-10",
        origin="杭州",
        destination="苏州",
    )
    assert diag is None
    assert facts[0]["value"]["train_no"] == "C3066"
    assert facts[0]["value"]["price"] == 50
    text = Path("planner/ticket_samples/mcp_text.txt").read_text(encoding="utf-8")
    text_facts, text_diag = parse_ticket_payload(
        [{"type": "text", "text": text}],
        source_id="raw-text",
        queried_at="2026-11-03T00:00:00+00:00",
        expect_date="2026-11-10",
        origin="杭州",
        destination="苏州",
        request_date="2026-11-10",
    )
    assert text_diag is None
    assert {item["value"]["seat"] for item in text_facts} >= {"二等座", "商务座"}


def test_opening_fact_must_match_visit_date() -> None:
    from planner.quality import opening_counts

    itinerary = {
        "days": [
            {
                "date": "2026-11-03",
                "items": [{"role": "visit", "poi_id": "m", "name": "博物馆"}],
            }
        ]
    }
    facts = [{"poi_id": "m", "status": "hard", "kind": "opening_hours", "applies_on": ["2026-10-01"]}]
    assert opening_counts(itinerary, facts, []) == (0, 1)
    facts[0]["applies_on"] = ["2026-11-03"]
    assert opening_counts(itinerary, facts, ["没找到的景点"]) == (1, 2)


def test_budget_complete_path_requires_each_item() -> None:
    from planner.ticket_facts import budget_markdown

    items = [
        {"name": "城际交通", "state": "verified", "amount": 100},
        {"name": "市内交通", "state": "verified", "amount": 20},
        {"name": "门票", "state": "verified", "amount": 0},
        {"name": "餐饮", "state": "verified", "amount": 60},
    ]
    _text, status = budget_markdown(
        [],
        user_budget="800",
        days=1,
        need_ticket=True,
        party_known=True,
        party=2,
        items=items,
    )
    assert status == "verified"
    _text, unknown = budget_markdown(
        [],
        user_budget="800",
        days=1,
        need_ticket=False,
        party_known=True,
        party=2,
    )
    assert unknown == "unverified"


def test_same_second_saves_do_not_share_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.summary as summary

    monkeypatch.setattr(summary, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(summary, "datetime", type("D", (), {"now": staticmethod(lambda: datetime(2026, 11, 3, 12, 0, 0))}))
    first, _url_a = summary.save_plan("plan-a", "去苏州", "甲")
    second, _url_b = summary.save_plan("plan-b", "去苏州", "乙")
    assert first != second
    assert "甲" in Path(first).read_text(encoding="utf-8")
    assert "乙" in Path(second).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_tool_schema_reaches_inner_tool() -> None:
    from planner.budget import RunBudget
    from planner.ticket_facts import RecordingTool, model_tool

    class Args(BaseModel):
        date: str
        fromStation: str
        toStation: str

    seen: dict = {}

    class Inner:
        name = "get-tickets"
        description = "查询余票"
        args_schema = Args

        async def ainvoke(self, args: dict) -> str:
            seen.update(args)
            return "ok"

    budget = RunBudget()
    tool = model_tool(RecordingTool(Inner(), [], budget))
    names = set(tool.args.keys()) if isinstance(tool.args, dict) else set(tool.args_schema.model_fields)
    assert names == {"date", "fromStation", "toStation"}
    assert "kwargs" not in names and "inner" not in names
    await tool.ainvoke({"date": "2026-11-10", "fromStation": "杭州", "toStation": "苏州"})
    assert seen == {"date": "2026-11-10", "fromStation": "杭州", "toStation": "苏州"}


@pytest.mark.asyncio
async def test_summary_does_not_start_when_phase_budget_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner_service as ps
    from slots import TravelSlots

    monkeypatch.setenv("PLAN_SUMMARY_SECONDS", "0")
    started = False

    async def summary(**_kwargs):
        nonlocal started
        started = True
        yield {"type": "_summary_done", "content": "不应调用"}

    slots = TravelSlots(
        origin="杭州",
        destination="杭州",
        date="2026-11-10",
        days=1,
        budget="800",
        preferences="园林",
        pace="舒适型节奏",
        transport_mode="driving",
    )
    monkeypatch.setattr(ps, "stream_summary", summary)
    monkeypatch.setattr(ps, "build_llm", lambda: object())
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")
    events = [event async for event in ps.stream_plan("杭州一日游", slots=slots)]
    assert started is False
    assert any(event.get("type") == "final" for event in events)


@pytest.mark.asyncio
async def test_parent_cancel_stops_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from planner.budget import RunBudget
    from planner.subruns import iter_agents

    finished = asyncio.Event()

    async def slow(**_kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()
        return {"text": "", "started": True, "timed_out": False}

    monkeypatch.setattr("planner.subruns.run_map_agent", slow)
    out: dict = {}

    async def consume() -> None:
        async for _event in iter_agents(
            out,
            need_map=True,
            need_ticket=False,
            ticket_tools=[],
            llm=object(),
            query="q",
            budget=RunBudget(),
            cancel_event=None,
            city="苏州",
        ):
            await asyncio.sleep(0)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_mcp_discovery_cancel_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from planner.budget import RunBudget
    from planner.ticket_agent import build_ticket_agent

    closed: list[bool] = []

    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            self.done = False

        async def get_tools(self):
            await asyncio.sleep(30)

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr("planner.ticket_agent.MultiServerMCPClient", Client)
    task = asyncio.create_task(build_ticket_agent(RunBudget()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


@pytest.mark.asyncio
async def test_replay_scripts_have_distinct_results() -> None:
    from evals.demo_replay import replay

    happy = await replay("happy")
    down = await replay("ticket_down")
    unknown = await replay("hours_unknown")
    assert happy["replay"] == "离线回放"
    assert "G7586" in happy["content"]
    assert happy.get("map_url")
    assert "G7586" not in down["content"]
    assert down["quality"]["checks"]["ticket"] == "unverified"
    assert "时刻未核实" in unknown["content"] or "不能据此确认" in unknown["content"]
    assert any(item.get("message") for item in happy["quality"]["issues"])
