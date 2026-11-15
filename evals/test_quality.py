"""质量状态、票务原文和未知耗时。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from planner.quality import build_quality, leg_counts
from planner.route_plan import itinerary_markdown, schedule
from planner.ticket_facts import parse_ticket_payload, select_ticket_tools, strip_unlisted_trains
from planner.subruns import map_tool_names


def test_unknown_leg_does_not_confirm_last_entry():
    poi = {
        "poi_id": "m",
        "name": "博物馆",
        "lng": 116.4,
        "lat": 39.9,
        "visit_minutes": 60,
    }
    lodging = {"poi_id": "h", "name": "酒店", "lng": 116.41, "lat": 39.91, "visit_minutes": 0}
    plan = schedule([poi], {"date": "2026-11-03", "days": 1, "lodging_poi": lodging}, lambda *_args: None)
    text = itinerary_markdown(plan)
    assert "已确认赶上" not in text
    assert "不能据此确认是否赶上最晚入场" in text
    day_lines = [line for line in text.splitlines() if line.startswith("- 第")]
    assert day_lines and all("时刻未核实" in line for line in day_lines)


def test_g1234_in_summary_is_removed_when_raw_lacks_it():
    payload = {
        "schema": "ticket-query-v1",
        "trains": [
            {
                "train_no": "G7586",
                "date": "2026-09-26",
                "origin": "杭州东",
                "destination": "苏州",
                "seat": "二等座",
                "price": 48,
                "availability": "有票",
            }
        ],
    }
    facts, diag = parse_ticket_payload(
        payload,
        source_id="raw-1",
        queried_at="2026-09-01T00:00:00+00:00",
        expect_date="2026-09-26",
        origin="杭州",
        destination="苏州",
    )
    assert diag is None
    allowed = {facts[0]["value"]["train_no"]}
    text = strip_unlisted_trains("模型总结包含 G1234 和 G7586", allowed)
    assert "G1234" not in text
    assert "G7586" in text


def test_empty_ticket_payload_is_not_verified():
    facts, diag = parse_ticket_payload(
        {"schema": "ticket-query-v1", "trains": []},
        source_id="raw-2",
        queried_at="2026-09-01T00:00:00+00:00",
        expect_date=None,
        origin=None,
        destination=None,
    )
    assert facts == []
    assert diag["code"] == "ticket_empty"
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[],
        ticket_reused=False,
        opening_required=0,
        opening_verified=0,
        legs_required=0,
        legs_verified=0,
        schedule_check="not_required",
        budget_status="not_required",
    )
    assert quality["checks"]["ticket"] == "unverified"
    assert quality["status"] == "partial"
    assert quality["coverage"]["opening_rules"]["label"] == "不适用"


def test_prose_is_not_a_ticket_fact():
    facts, diag = parse_ticket_payload(
        "推荐 G1234 二等座 50 元",
        source_id="raw-3",
        queried_at="2026-09-01T00:00:00+00:00",
        expect_date=None,
        origin=None,
        destination=None,
    )
    assert facts == []
    assert diag["code"] == "unrecognized_ticket_payload"


def test_tool_allowlist_is_exact():
    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    selected = select_ticket_tools([Tool("get-tickets"), Tool("execute"), Tool("read_file")])
    assert [tool.name for tool in selected] == ["get-tickets"]
    assert map_tool_names() == ["amap_poi_search"]


def test_old_plans_db_gains_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "old.db"
    monkeypatch.setenv("PLANS_DB", str(path))
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE plans (plan_id TEXT PRIMARY KEY, query TEXT, slots TEXT, status TEXT, timings TEXT, artifact TEXT, created_at TEXT)")
    conn.execute("INSERT INTO plans VALUES ('old', 'q', NULL, 'ok', '{}', NULL, '2020-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    from planner.plans_store import list_plans, record_plan_sync

    record_plan_sync(
        plan_id="new",
        query="q2",
        slots=None,
        status="partial",
        timings={"total_ms": 1, "prompt_tokens": 2},
        artifact=None,
        ok=False,
        outcome="finished",
        quality={"status": "partial"},
        usage={"prompt_tokens": 2, "cost_label": "成本未知"},
    )
    rows = {row["plan_id"]: row for row in list_plans(5)}
    assert rows["old"]["quality"] is None
    assert rows["old"]["quality_label"] == "未记录核实状态"
    assert rows["new"]["quality"]["status"] == "partial"
    assert rows["new"]["timings"]["prompt_tokens"] == 2
    record_plan_sync(
        plan_id="new",
        query="q2",
        slots=None,
        status="partial",
        timings={},
        artifact=None,
        ok=False,
        outcome="finished",
    )


def test_results_dir_not_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from planner.paths import RESULTS_DIR

    workspace = Path(__file__).resolve().parents[1] / "workspace" / "results"
    assert RESULTS_DIR != workspace


def test_reused_ticket_is_not_verified():
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[{"status": "verified", "source_id": "raw-1", "value": {"train_no": "G1"}}],
        ticket_reused=True,
        opening_required=1,
        opening_verified=1,
        legs_required=1,
        legs_verified=1,
        schedule_check="verified",
        budget_status="verified",
    )
    assert quality["checks"]["ticket"] == "unverified"
    assert quality["status"] == "partial"


def test_work_deadline_blocks_new_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAN_BUDGET_SECONDS", "10")
    monkeypatch.setenv("PLAN_CLOSEOUT_SECONDS", "10")
    from planner.budget import RunBudget

    budget = RunBudget()
    assert budget.allow_external() is False
    assert budget.note_attempt() is False


def test_inflight_barrier_rejects_third(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAN_MAX_INFLIGHT", "2")
    import server

    gate = server.Inflight()
    barrier = threading.Barrier(3)
    held: list[object] = []

    def hold() -> None:
        lease = gate.try_acquire()
        held.append(lease)
        barrier.wait()
        if lease is not None:
            lease.release()
            lease.release()

    threads = [threading.Thread(target=hold) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item is not None for item in held) == 2
    assert gate.count == 0


def test_heartbeat_does_not_block_disconnect() -> None:
    from planner.async_utils import watch_aiter

    idle = 0

    async def slow():
        await asyncio.sleep(0.7)
        yield {"type": "token"}

    async def on_idle() -> None:
        nonlocal idle
        idle += 1

    async def run():
        return [event async for event in watch_aiter(slow(), on_idle=on_idle, heartbeat_seconds=0.05)]

    events = asyncio.run(run())
    assert idle >= 1
    assert any(event.get("type") == "_heartbeat" for event in events)


@pytest.mark.asyncio
async def test_missing_slots_stop_before_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner_service as ps
    from slots import TravelSlots

    async def extracted(*_args, **_kwargs):
        return TravelSlots(destination="苏州")

    monkeypatch.setattr(ps, "extract_slots", extracted)
    events = [event async for event in ps.stream_plan("去苏州")]
    kinds = {event.get("type") for event in events}
    ids = {event.get("id") for event in events}
    assert "clarify" in kinds
    assert "final" not in kinds
    assert "subagent" not in kinds
    assert "map" not in ids and "ticket" not in ids and "summary" not in ids


@pytest.mark.asyncio
async def test_unconsumed_response_releases_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    import server
    from starlette.requests import Request

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(30)
        yield {"type": "status", "message": "不应发出"}

    monkeypatch.setattr(server, "stream_plan", hang)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/plan",
        "headers": [],
        "client": ("127.0.0.1", 9),
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    server._rate_hits.clear()
    before = server._inflight.count
    response = await server.plan(server.PlanRequest(query="去杭州"), Request(scope, receive))
    assert server._inflight.count == before + 1
    await response.body_iterator.aclose()
    assert server._inflight.count == before


def _train_text(value: str) -> list[dict]:
    return [{"type": "text", "text": value}]


def test_station_lookup_is_not_a_train_diagnostic():
    from planner.ticket_facts import facts_from_records

    records = [
        {
            "id": "raw-1",
            "tool": "get-stations-code-in-city",
            "arguments": {"city": "新乡"},
            "payload": _train_text("新乡：新乡站(telecode:XXF) 新乡东(telecode:EGF) 新乡南(telecode:ENF)"),
            "queried_at": "2026-11-05T00:00:00+00:00",
        },
        {
            "id": "raw-2",
            "tool": "get-station-code-of-citys",
            "arguments": {"citys": "上海"},
            "payload": _train_text("上海：上海站(telecode:SHH) 上海虹桥(telecode:AOH)"),
            "queried_at": "2026-11-05T00:00:00+00:00",
        },
    ]
    facts, diagnostics = facts_from_records(records, expect_date="2026-11-07", origin="新乡", destination="上海")
    assert facts == []
    assert diagnostics == []


def test_station_lookup_does_not_break_train_facts():
    from planner.ticket_facts import facts_from_records

    trains = _train_text(
        "\n".join(
            [
                "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                "G1813 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 09:35 -> 15:27 历时：05:52",
                "- 商务座: 剩余6张票 1569元",
                "- 二等座: 有票 493元",
                "G1951 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 15:43 -> 21:14 历时：05:31",
                "- 二等座: 有票 532元",
            ]
        )
    )
    records = [
        {
            "id": "raw-1",
            "tool": "get-stations-code-in-city",
            "arguments": {"city": "新乡"},
            "payload": _train_text("新乡：新乡站(telecode:XXF)"),
            "queried_at": "2026-11-05T00:00:00+00:00",
        },
        {
            "id": "raw-3",
            "tool": "get-tickets",
            "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"},
            "payload": trains,
            "queried_at": "2026-11-05T00:00:00+00:00",
        },
    ]
    facts, diagnostics = facts_from_records(records, expect_date="2026-11-07", origin="新乡", destination="上海")
    assert len(facts) == 3
    assert diagnostics == []
    assert {fact["source_id"] for fact in facts} == {"raw-3"}


def test_interline_tool_is_not_misreported():
    from planner.ticket_facts import facts_from_records, unparsed_tool_notes

    records = [
        {
            "id": "raw-5",
            "tool": "get-interline-tickets",
            "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"},
            "payload": _train_text("中转方案：新乡东 -> 郑州东 -> 上海虹桥"),
            "queried_at": "2026-11-05T00:00:00+00:00",
        }
    ]
    facts, diagnostics = facts_from_records(records, expect_date="2026-11-07", origin="新乡", destination="上海")
    assert facts == []
    assert diagnostics == []
    notes = unparsed_tool_notes(records)
    assert [note["code"] for note in notes] == ["ticket_parser_not_enabled"]
    assert notes[0]["tool"] == "get-interline-tickets"
    assert notes[0]["ref"] == "raw-5"


def _ticket_records() -> list[dict]:
    trains = _train_text(
        "\n".join(
            [
                "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                "G1813 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 09:35 -> 15:27 历时：05:52",
                "- 二等座: 有票 493元",
                "G3251 新乡南(telecode:ENF) -> 上海虹桥(telecode:AOH) 11:04 -> 17:27 历时：06:23",
                "- 二等座: 有票 517元",
            ]
        )
    )
    return [
        {
            "id": "raw-3",
            "tool": "get-tickets",
            "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"},
            "payload": trains,
            "queried_at": "2026-11-05T00:00:00+00:00",
        }
    ]


def test_same_city_station_records_keep_reason():
    from planner.ticket_facts import applicable_facts, facts_from_records

    facts, diagnostics = facts_from_records(
        _ticket_records(), expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    assert diagnostics == []
    by_train = {fact["value"]["train_no"]: fact["value"] for fact in facts}
    assert by_train["G1813"]["station_match"] == "exact"
    assert by_train["G1813"]["applied"] is True
    assert by_train["G3251"]["station_match"] == "same_city"
    assert by_train["G3251"]["applied"] is True
    assert len(applicable_facts(facts)) == 2


def test_other_city_records_are_parsed_but_not_applied():
    from planner.ticket_facts import applicable_facts, applicability_note, facts_from_records

    facts, diagnostics = facts_from_records(
        _ticket_records(), expect_date="2026-11-07", origin="新乡", destination="北京"
    )
    assert diagnostics == []
    assert len(facts) == 2
    assert applicable_facts(facts) == []
    assert all(fact["value"]["applied"] is False for fact in facts)
    assert all(fact["value"]["reason"] == "station_other_city" for fact in facts)
    note = applicability_note(facts)
    assert note is not None
    assert note["code"] == "ticket_not_applicable"
    assert note["ref"] == "raw-3"


def test_date_mismatch_is_not_applied():
    from planner.ticket_facts import applicable_facts, facts_from_records

    facts, diagnostics = facts_from_records(
        _ticket_records(), expect_date="2026-12-01", origin="新乡", destination="上海"
    )
    assert diagnostics == []
    assert facts != []
    assert applicable_facts(facts) == []
    assert {fact["value"]["reason"] for fact in facts} == {"date_mismatch"}


def test_ticket_status_ignores_inapplicable_facts():
    from planner.quality import _ticket_status

    verified = {"status": "verified", "source_id": "raw-3", "value": {"applied": True}}
    skipped = {"status": "verified", "source_id": "raw-3", "value": {"applied": False}}
    assert _ticket_status([verified], False) == "verified"
    assert _ticket_status([skipped], False) == "unverified"
    assert _ticket_status([verified, skipped], False) == "verified"


def test_inapplicable_ticket_issue_explains_reason():
    from planner.quality import collect_issues

    issues = collect_issues(
        checks={"ticket": "unverified"},
        ticket_applicability={
            "code": "ticket_not_applicable",
            "ref": "raw-3",
            "reasons": ["station_other_city"],
        },
    )
    assert [item["code"] for item in issues] == ["ticket_not_applicable"]
    assert issues[0]["ref"] == "raw-3"
    assert issues[0]["reasons"] == ["station_other_city"]
    assert "ticket_unverified" not in [item["code"] for item in issues]


def _screen_records():
    text = "\n".join(
        [
            "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
            "G1951 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 15:43 -> 21:14 历时：05:31",
            "- 二等座: 有票 532元",
            "- 无座: 无票 532元",
            "G1813 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 09:35 -> 15:27 历时：05:52",
            "- 二等座: 有票 493元",
            "G2811 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 10:15 -> 15:59 历时：05:44",
            "- 二等座: 无票 546元",
        ]
    )
    return [
        {
            "id": "raw-3",
            "tool": "get-tickets",
            "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"},
            "payload": [{"type": "text", "text": text}],
            "queried_at": "2026-11-03T00:00:00+00:00",
        }
    ]


def test_tool_clock_fields_are_kept():
    from planner.ticket_facts import applicable_facts, facts_from_records

    facts, diagnostics = facts_from_records(
        _screen_records(), expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    assert diagnostics == []
    first = applicable_facts(facts)[0]["value"]
    assert first["depart"] == "15:43"
    assert first["arrive"] == "21:14"
    assert first["lishi"] == "05:31"
    assert first["arrive_date"] == "2026-11-07"
    assert first["arrive_date_source"] == "derived_from_lishi"


def test_plan_basis_prefers_earliest_bookable_arrival():
    from planner.ticket_facts import applicable_facts, choose_plan_basis, facts_from_records

    facts, _ = facts_from_records(
        _screen_records(), expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    basis = choose_plan_basis(applicable_facts(facts), query="这周六从新乡坐高铁去上海")
    assert basis["train_no"] == "G1813"
    assert basis["origin"] == "proposed"
    assert basis["depart"] == "09:35" and basis["arrive"] == "15:27"


def test_plan_basis_ignores_negated_train():
    from planner.ticket_facts import applicable_facts, choose_plan_basis, facts_from_records

    facts, _ = facts_from_records(
        _screen_records(), expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    applicable = applicable_facts(facts)
    assert choose_plan_basis(applicable, query="不要 G1951")["origin"] == "proposed"
    picked = choose_plan_basis(applicable, query="就坐 G1951 去")
    assert picked["train_no"] == "G1951" and picked["origin"] == "user_selected"


def test_inbound_basis_stays_none_without_preference():
    from planner.ticket_facts import applicable_facts, choose_plan_basis, facts_from_records

    facts, _ = facts_from_records(
        _screen_records(), expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    assert choose_plan_basis(applicable_facts(facts), query="去上海玩三天", direction="inbound") is None


def test_missing_date_is_not_applied():
    """P1-9：期望日期明确但事实缺日期，不能静默判适用，应为不适用。"""
    from planner.ticket_facts import applicable_facts, facts_from_records

    records = [
        {
            "id": "raw-x",
            "tool": "get-tickets",
            "arguments": {"fromStation": "新乡东", "toStation": "上海"},  # 无 date 键 → 事实缺日期
            "payload": _train_text(
                "\n".join(
                    [
                        "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                        "G1813 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 09:35 -> 15:27 历时：05:52",
                        "- 二等座: 有票 493元",
                    ]
                )
            ),
            "queried_at": "2026-11-05T00:00:00+00:00",
        }
    ]
    facts, diagnostics = facts_from_records(
        records, expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    assert diagnostics == []
    assert facts != []
    assert applicable_facts(facts) == []
    assert {fact["value"]["reason"] for fact in facts} == {"date_unknown"}


def test_inbound_preference_only_reads_return_clause():
    """P1-9：返程时间倾向只读返程语境子句，整句的去程时间不能当成返程偏好。"""
    from planner.ticket_facts import applicable_facts, choose_plan_basis, facts_from_records

    records = [
        {
            "id": "raw-y",
            "tool": "get-tickets",
            "arguments": {"fromStation": "上海虹桥", "toStation": "新乡"},
            "payload": _train_text(
                "\n".join(
                    [
                        "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                        "G2812 上海虹桥(telecode:AOH) -> 新乡东(telecode:EGF) 19:00 -> 23:40 历时：04:40",
                        "- 二等座: 有票 532元",
                    ]
                )
            ),
            "queried_at": "2026-11-05T00:00:00+00:00",
        }
    ]
    facts, _ = facts_from_records(
        records, expect_date="2026-11-07", origin="新乡", destination="上海"
    )
    inbound = applicable_facts(facts)
    # 整句的「下午」落在去程子句，不构成返程偏好 → 返程依据保持待定。
    assert choose_plan_basis(inbound, query="下午从新乡出发，上海玩三天", direction="inbound") is None
    # 明确的返程语境「晚上回」才参与选择。
    basis = choose_plan_basis(inbound, query="返程想坐晚上那趟", direction="inbound")
    assert basis is not None and basis["train_no"] == "G2812"


def test_inbound_missing_departure_not_selected():
    """P1-9：缺出发时刻的候选不参与返程排序，不能仅凭缺时刻被选中。"""
    from planner.ticket_facts import choose_plan_basis

    facts = [
        {
            "source_id": "r1",
            "value": {
                "train_no": "G1", "date": "2026-11-07",
                "origin": "上海虹桥", "destination": "新乡东",
                "seat": "二等座", "price": 500, "availability": "有票",
                "depart": None, "arrive": "20:00", "direction": "inbound",
            },
        },
        {
            "source_id": "r2",
            "value": {
                "train_no": "G2", "date": "2026-11-07",
                "origin": "上海虹桥", "destination": "新乡东",
                "seat": "二等座", "price": 480, "availability": "有票",
                "depart": "18:00", "arrive": "22:00", "direction": "inbound",
            },
        },
    ]
    # 有缺时刻候选 + 有有效时刻候选时，只从有效时刻候选里选。
    basis = choose_plan_basis(facts, query="返程想坐下午的", direction="inbound")
    assert basis["train_no"] == "G2"
    # 只有缺时刻候选时，不能把它当成可用的返程依据。
    only_missing = [fact for fact in facts if fact["value"]["train_no"] == "G1"]
    assert choose_plan_basis(only_missing, query="返程想坐下午的", direction="inbound") is None


def test_rail_window_reports_unknown_and_not_constrained():
    from datetime import date

    from planner.ticket_facts import rail_window

    window = rail_window(
        {"outbound": None, "inbound": None},
        first_day=date(2026, 11, 7),
        last_day=date(2026, 11, 9),
    )
    assert window["outbound"]["state"] == "not_constrained"
    assert window["inbound"]["state"] == "not_constrained"
    assert window["unverified"] is False

    partial = rail_window(
        {"outbound": {"train_no": "G1", "arrive": None, "arrive_date": None}, "inbound": None},
        first_day=date(2026, 11, 7),
        last_day=date(2026, 11, 9),
    )
    assert partial["outbound"]["state"] == "unknown"
    assert partial["unverified"] is True


def test_ticket_markdown_shows_clock_only_when_known():
    from planner.ticket_facts import ticket_markdown

    with_clock = {"source_id": "raw-3", "value": {"train_no": "G1813", "origin": "新乡东", "destination": "上海虹桥", "date": "2026-11-07", "seat": "二等座", "price": 493, "availability": "有票", "depart": "09:35", "arrive": "15:27"}}
    without = {"source_id": "raw-3", "value": {"train_no": "G1813", "origin": "新乡东", "destination": "上海虹桥", "date": "2026-11-07", "seat": "二等座", "price": 493, "availability": "有票"}}
    assert "09:35–15:27" in ticket_markdown([with_clock], reused=False, queried_at=None)
    assert "–" not in ticket_markdown([without], reused=False, queried_at=None)


def test_sources_are_grouped_per_query():
    """一次 get-tickets 返回 20 条座席，页面上只能算一次查询。"""
    from planner.pipeline import _group_sources

    records = [
        {"id": "raw-1", "tool": "get-stations-code-in-city", "arguments": {"city": "新乡"}, "queried_at": "2026-11-05T00:00:00+00:00"},
        {"id": "raw-3", "tool": "get-tickets", "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"}, "queried_at": "2026-11-05T00:01:00+00:00"},
    ]
    facts = [{"source_id": "raw-3", "value": {"seat": f"座{i}"}} for i in range(20)]
    rows = _group_sources(records, facts)
    assert len(rows) == 1
    assert rows[0]["ref"] == "raw-3"
    # 一次查询、20 条记录：record_count 是「查询次数」，fact_count 才是座席条数。
    assert rows[0]["record_count"] == 1
    assert rows[0]["fact_count"] == 20
    assert "新乡东→上海" in rows[0]["label"]
    assert "2026-11-07" in rows[0]["label"]
    assert _group_sources(records, []) == []


def test_pending_makes_status_partial():
    """有「等用户确认」的项时不能算完整：确认之前方案只是草稿。

    但也不能为了降状态伪造 unverified —— checks 要反映实际核实结果。
    """
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[{"status": "verified", "source_id": "raw-1", "value": {"train_no": "G1"}}],
        ticket_reused=False,
        opening_required=1,
        opening_verified=1,
        legs_required=1,
        legs_verified=1,
        schedule_check="verified",
        budget_status="verified",
    )
    assert quality["status"] == "complete"
    assert quality["pending"] == []

    pending = [{"field": "lodging", "actions": ["exact", "area"], "message": "住宿只给到区域"}]
    waiting = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[{"status": "verified", "source_id": "raw-1", "value": {"train_no": "G1"}}],
        ticket_reused=False,
        opening_required=1,
        opening_verified=1,
        legs_required=1,
        legs_verified=1,
        schedule_check="verified",
        budget_status="verified",
        pending=pending,
    )
    assert waiting["status"] == "partial"
    assert waiting["pending"] == pending
    # 状态下调不能来自伪造的未核实项。
    assert all(value == "verified" for value in waiting["checks"].values())


def test_issue_dedupe_by_identifier():
    """同一 code + ref + kind 只留一条：两个阶段都报同一件事时不能刷屏。"""
    from planner.quality import collect_issues

    issues = collect_issues(
        checks={"ticket": "not_required", "opening_rules": "unverified", "travel_time": "verified",
                "schedule": "verified", "budget": "verified"},
        unlocated=[
            {"name": "外滩", "kind": "sight"},
            {"name": "外滩", "kind": "sight"},
        ],
    )
    keys = [(item["code"], item["ref"], item["kind"]) for item in issues]
    assert len(keys) == len(set(keys))
    sightings = [item for item in issues if item["code"] == "candidate_unlocated"]
    assert len(sightings) == 1


def test_same_arguments_twice_counts_two_records():
    """同参数查了两次：算两次查询，标签上要写清楚，不能当成一条。"""
    from planner.pipeline import _group_sources

    args = {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"}
    records = [
        {"id": "raw-3", "tool": "get-tickets", "arguments": args, "queried_at": "2026-11-05T00:01:00+00:00"},
        {"id": "raw-5", "tool": "get-tickets", "arguments": dict(args), "queried_at": "2026-11-05T00:03:00+00:00"},
    ]
    facts = [
        {"source_id": "raw-3", "value": {"seat": "二等座"}},
        {"source_id": "raw-5", "value": {"seat": "一等座"}},
    ]
    rows = _group_sources(records, facts)
    assert len(rows) == 1
    assert rows[0]["record_count"] == 2
    assert rows[0]["fact_count"] == 2
    assert "2 次相同参数查询" in rows[0]["label"]


def test_web_evidence_keeps_its_own_time():
    """网页取证按来源 URL 聚合，时间用自己的抓取时间，不借用票务查询时间。"""
    from planner.pipeline import _group_sources
    from planner.travel_search import ALLOWED_HOSTS

    # 用白名单内的主机：来源行里的 url 最终会变成前端 <a href>，非白名单会被
    # _group_sources 丢弃。这里要验的是「聚合与时间归属」，不是 URL 过滤本身。
    host = sorted(ALLOWED_HOSTS)[0]
    url = f"https://{host}/a"
    facts = [
        {"source_id": "web-1", "source_url": url, "source_title": "官方开放时间", "queried_at": "2026-11-05T02:00:00+00:00"},
        {"source_id": "web-2", "source_url": url, "source_title": "官方开放时间", "queried_at": "2026-11-05T02:00:00+00:00"},
    ]
    rows = _group_sources([], facts)
    assert len(rows) == 1
    assert rows[0]["url"] == url
    assert rows[0]["record_count"] == 2
    assert rows[0]["queried_at"] == "2026-11-05T02:00:00+00:00"


def test_leg_counts_separates_reference_from_verified():
    """取得有效路线结果只算「参考」，只有带完整适用性依据（confidence=verified）才算已核实。"""
    itinerary = {
        "days": [
            {
                "day": 1,
                "date": "2026-11-07",
                "legs": [
                    {"origin_id": "h", "dest_id": "a", "mode": "walking", "duration_seconds": 600},
                    {"origin_id": "a", "dest_id": "b", "mode": "transfer", "duration_seconds": 900, "confidence": "verified"},
                    {"origin_id": "b", "dest_id": "c", "mode": "walking", "duration_seconds": None},
                ],
            }
        ]
    }
    referenced, verified, required = leg_counts(itinerary)
    assert required == 3
    assert referenced == 2  # 两条有秒数，一条缺失
    assert verified == 1  # 只有 confidence=verified 的那条


def test_leg_counts_skips_same_poi_placeholder():
    """同一地点间的空跑是占位路段，不计入覆盖率。"""
    itinerary = {
        "days": [
            {
                "day": 1,
                "date": "2026-11-07",
                "legs": [
                    {"origin_id": "a", "dest_id": "a", "mode": "walking", "duration_seconds": 0},
                    {"origin_id": "a", "dest_id": "b", "mode": "walking", "duration_seconds": 600},
                ],
            }
        ]
    }
    referenced, verified, required = leg_counts(itinerary)
    assert required == 1
    assert referenced == 1
    assert verified == 0


def test_travel_time_reference_forces_partial_status():
    """所有路段都有参考耗时但没有已核实依据时，travel_time=reference，整体至少 partial。"""
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=False,
        ticket_facts=[],
        ticket_reused=False,
        opening_required=0,
        opening_verified=0,
        legs_required=2,
        legs_verified=0,
        legs_referenced=2,
        schedule_check="verified",
        budget_status="not_required",
    )
    assert quality["checks"]["travel_time"] == "reference"
    assert quality["status"] == "partial"
    assert quality["coverage"]["travel_time"]["label"] == "参考 2/2"


def test_travel_time_verified_when_all_confidence_verified():
    """全部路段带 confidence=verified 时才算已核实，整体 complete。"""
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=False,
        ticket_facts=[],
        ticket_reused=False,
        opening_required=0,
        opening_verified=0,
        legs_required=2,
        legs_verified=2,
        legs_referenced=2,
        schedule_check="verified",
        budget_status="not_required",
    )
    assert quality["checks"]["travel_time"] == "verified"
    assert quality["status"] == "complete"
    assert quality["coverage"]["travel_time"]["label"] == "已核实 2/2"


# --- 责任方与票务文案分流 -------------------------------------------------


def _visit(poi_id: str, name: str) -> dict:
    return {"role": "visit", "poi_id": poi_id, "name": name}


def test_reused_ticket_issue_says_reused_not_missing():
    """复用未刷新要说清是「沿用上次查询」，不能说成「没有可引用的有效车次」。"""
    from planner.quality import collect_issues

    state = {
        "reason": "reused",
        "queried_at": "2026-11-06T00:25:34+00:00",
        "facts_count": 84,
        "applicable_count": 84,
        "diagnostics": [],
    }
    issues = collect_issues(checks={"ticket": "unverified"}, ticket_state=state)
    codes = [item["code"] for item in issues]
    assert "ticket_reused" in codes
    assert "ticket_unverified" not in codes
    reused = next(item for item in issues if item["code"] == "ticket_reused")
    assert reused["kind"] == "info"
    assert reused["owner"] == "system"
    assert reused["action"] == "recheck_tickets"
    assert reused["ref"] == "2026-11-06T00:25:34+00:00"
    # 复用不是已核实：文案变了，checks.ticket 仍是 unverified。
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[{"status": "hard"}],
        ticket_reused=True,
        opening_required=0,
        opening_verified=0,
        legs_required=0,
        schedule_check="verified",
        budget_status="not_required",
    )
    assert quality["checks"]["ticket"] == "unverified"
    assert quality["status"] == "partial"


def test_ticket_state_ok_and_skipped_do_not_raise_issue():
    """查到且适用、以及本方案不需要查票，都不该产生票务告警。"""
    from planner.quality import collect_issues

    ok = {"reason": "ok", "queried_at": "2026-11-06T00:25:34+00:00", "facts_count": 84, "applicable_count": 84}
    assert collect_issues(checks={"ticket": "verified"}, ticket_state=ok) == []
    skipped = {"reason": "skipped", "queried_at": "", "facts_count": 0, "applicable_count": 0}
    assert collect_issues(checks={"ticket": "not_required"}, ticket_state=skipped) == []


def test_ticket_state_failure_explains_reason():
    """失败/空结果/超时分别说明，不能一句「没有有效车次」概括。"""
    from planner.quality import collect_issues

    for reason, keyword in (("failed", "失败"), ("empty", "没有返回车次"), ("timeout", "超时")):
        issues = collect_issues(
            checks={"ticket": "unverified"},
            ticket_state={"reason": reason, "queried_at": "", "facts_count": 0, "applicable_count": 0},
        )
        assert [item["code"] for item in issues] == ["ticket_unverified"]
        assert keyword in issues[0]["message"]
        assert issues[0]["owner"] == "system"


def test_ticket_state_without_applicable_facts_keeps_applicability_reason():
    """有事实但都不适用时，仍按 applicability 说明原因。"""
    from planner.quality import collect_issues

    issues = collect_issues(
        checks={"ticket": "unverified"},
        ticket_applicability={"code": "ticket_not_applicable", "ref": "raw-3", "reasons": ["station_other_city"]},
        ticket_state={"reason": "ok", "queried_at": "", "facts_count": 3, "applicable_count": 0},
    )
    assert [item["code"] for item in issues] == ["ticket_not_applicable"]
    assert issues[0]["reasons"] == ["station_other_city"]


def test_budget_unverified_is_system_side_info():
    """费用未核实是系统待核实：不是用户待确认，也不该带确认动作。"""
    from slots import draft_pendings

    from planner.quality import collect_issues

    issues = collect_issues(checks={"budget": "unverified"})
    item = next(entry for entry in issues if entry["code"] == "budget_unverified")
    assert item["owner"] == "system"
    assert item["kind"] == "info"
    assert item["action"] is None
    assert "市内交通" in item["message"]
    # 人数与口径都明确时，用户没有可回答的待确认项。
    pending = draft_pendings({"party_known": True, "budget_scope": "per_person"})
    assert pending == []
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=False,
        ticket_facts=[],
        ticket_reused=False,
        opening_required=0,
        opening_verified=0,
        legs_required=0,
        schedule_check="verified",
        budget_status="unverified",
        issues=issues,
        pending=pending,
    )
    assert quality["pending"] == []
    assert quality["checks"]["budget"] == "unverified"
    assert quality["status"] == "partial"


# --- 排程明细 -------------------------------------------------------------


def _day(date_text: str, poi_ids: list[str], legs: list[dict]) -> dict:
    return {
        "date": date_text,
        "items": [_visit(poi_id, f"景点{poi_id}") for poi_id in poi_ids],
        "legs": legs,
    }


def _leg(origin_id: str, dest_id: str, seconds=None, mode: str = "transfer", depart: str = "11:30") -> dict:
    return {
        "origin_id": origin_id,
        "dest_id": dest_id,
        "mode": mode,
        "duration_seconds": seconds,
        "depart": depart,
        "confidence": "reference" if isinstance(seconds, int) and seconds > 0 else "unknown",
    }


def test_schedule_details_leg_count_matches_coverage_gap():
    """未知路段条数必须等于 leg_counts 的分母减已有参考，口径不能另起一套。"""
    from planner.quality import schedule_details

    itinerary = {
        "days": [
            _day("2026-11-08", ["a", "b", "c"], [_leg("a", "b"), _leg("b", "c", 649, mode="walking")]),
            _day("2026-11-09", ["c", "d"], [_leg("c", "d"), _leg("d", "d", 0, mode="walking")]),
        ]
    }
    needed = leg_counts(itinerary)[2] - leg_counts(itinerary)[0]
    travel = [item for item in schedule_details(itinerary=itinerary) if item["code"] == "travel_leg_unknown"]
    assert len(travel) == needed == 2
    first = travel[0]
    assert first["date"] == "2026-11-08"
    assert "11 月 8 日" in first["message"]
    assert first["owner"] == "system"
    assert first["action"] == "rerun_route"
    assert first["leg"]["origin_id"] == "a" and first["leg"]["dest_id"] == "b"


def test_schedule_details_leg_keeps_route_failure_reason():
    """路线查询失败原因要保留到明细，不能只剩「未核实」。"""
    from planner.quality import schedule_details

    itinerary = {"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b")])]}
    route_states = {("a", "b", "transfer", "2026-11-08", 11): "no_route"}
    travel = schedule_details(itinerary=itinerary, route_states=route_states)[0]
    assert travel["detail"]["reason"] == "no_route"
    assert travel["detail"]["reason_text"] == "没有可用路线"


def test_schedule_details_opening_missing_matches_coverage_gap():
    """缺开放规则的条数必须等于 opening_counts 的分母减已核实。"""
    from planner.quality import opening_counts, schedule_details

    itinerary = {"days": [_day("2026-11-03", ["m", "n"], [])]}
    facts = [{"poi_id": "m", "status": "hard", "kind": "opening_hours", "applies_on": ["2026-11-03"]}]
    unlocated = [{"name": "没找到的景点", "kind": "sight"}]
    verified, required = opening_counts(itinerary, facts, unlocated)
    missing = [item for item in schedule_details(itinerary=itinerary, unlocated=unlocated, facts=facts) if item["code"] == "opening_missing"]
    assert len(missing) == required - verified == 2
    covered = {item["poi_id"] for item in missing}
    assert covered == {"n", "没找到的景点"}
    for item in missing:
        assert item["owner"] == "system"
        assert item["action"] == "view_opening"
        assert item["poi_id"] == item["ref"].split("@")[0]


def test_schedule_details_return_basis_and_lodging_anchor():
    """返程候选已查到、住宿只到区域时，两者都要请用户补一步。"""
    from planner.quality import schedule_details

    details = schedule_details(
        itinerary=None,
        plan_basis={"outbound": {"train_no": "G1813"}, "inbound": None},
        return_options=[{"train_no": "G1814", "depart": "18:20"}],
        lodging_state={"scope": "area", "text": "上海南站附近"},
    )
    by_code = {item["code"]: item for item in details}
    assert by_code["return_basis_pending"]["owner"] == "user"
    assert by_code["return_basis_pending"]["kind"] == "info"
    assert by_code["return_basis_pending"]["action"] == "select_return"
    assert by_code["return_basis_pending"]["options"] == [{"train_no": "G1814", "depart": "18:20"}]
    assert by_code["lodging_anchor_missing"]["owner"] == "user"
    assert by_code["lodging_anchor_missing"]["action"] == "retry_lodging"
    assert by_code["lodging_anchor_missing"]["detail"] == {"scope": "area", "text": "上海南站附近", "reason": "not_located"}
    # 已有返程依据、住宿已定位时不再提示。
    assert schedule_details(
        itinerary=None,
        plan_basis={"outbound": None, "inbound": {"train_no": "G1814"}},
        return_options=[{"train_no": "G1814"}],
        lodging_state={"scope": "exact", "poi_id": "h1", "text": "某酒店"},
    ) == []


def test_collect_issues_expands_details_instead_of_aggregate():
    """有明细时不再叠一条同类聚合告警；没有 itinerary 时仍退回聚合项。"""
    from planner.quality import collect_issues

    itinerary = {"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b")])]}
    checks = {"opening_rules": "unverified", "travel_time": "unverified", "schedule": "unverified"}
    expanded = [item["code"] for item in collect_issues(checks=checks, itinerary=itinerary)]
    assert "travel_leg_unknown" in expanded
    assert "opening_missing" in expanded
    # 交通明细只能解释交通耗时，不能替排程结论说话：没有抵离明细时聚合项仍在。
    assert "schedule_unverified" in expanded
    assert "travel_unverified" not in expanded
    assert "opening_unverified" not in expanded
    aggregate = [item["code"] for item in collect_issues(checks=checks)]
    assert set(aggregate) >= {"travel_unverified", "opening_unverified", "schedule_unverified"}


def test_every_issue_has_known_owner():
    """页面按 owner 分成「需要你补充 / 系统待核实」，取值只能是这两种。"""
    from planner.quality import collect_issues

    issues = collect_issues(
        checks={
            "ticket": "unverified",
            "opening_rules": "unverified",
            "travel_time": "unverified",
            "schedule": "unverified",
            "budget": "unverified",
        },
        ticket_diagnostics=[{"code": "ticket_empty", "ref": "raw-1"}],
        unlocated=[
            {"name": "外滩", "kind": "sight"},
            {"name": "上海南站附近", "kind": "lodging", "reason": "area"},
            {"name": "某民宿", "kind": "lodging", "reason": "unresolved"},
        ],
        ticket_state={"reason": "reused", "queried_at": "2026-11-06T00:25:34+00:00", "facts_count": 84, "applicable_count": 84},
        itinerary={"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b")])]},
        lodging_state={"scope": "area", "text": "上海南站附近"},
        plan_basis={"outbound": None, "inbound": None},
        return_options=[{"train_no": "G1814"}],
        summary_degraded=True,
    )
    assert issues
    assert {item["owner"] for item in issues} <= {"user", "system"}
    assert any(item["owner"] == "user" for item in issues)
    assert any(item["owner"] == "system" for item in issues)
    assert all(item["message"] for item in issues)


# --- 抵离时刻未知的明细 ---------------------------------------------------


def _rail_window(inbound: dict, outbound: dict | None = None) -> dict:
    return {
        "outbound": outbound or {"state": "known", "basis": {"train_no": "G1813"}, "earliest_start": 600},
        "inbound": inbound,
        "assumptions": [],
        "unverified": False,
        "first_day": "2026-11-08",
        "last_day": "2026-11-09",
    }


def test_rail_window_unknown_replaces_schedule_aggregate():
    """抵离时刻未知要单独成条，不能只剩一句「排程仍有未知条件」。"""
    from planner.quality import collect_issues, schedule_details

    details = schedule_details(
        itinerary={"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b", 600)])]},
        rail_window=_rail_window(
            {"state": "unknown", "basis": None, "reason": "需要返程但没有返程车次依据，末日最晚结束时间未知"}
        ),
        return_options=[{"train_no": "G1814"}],
    )
    rail = [item for item in details if item["code"] == "rail_window_unknown"]
    assert len(rail) == 1
    assert rail[0]["direction"] == "inbound"
    assert rail[0]["owner"] == "system"
    assert "返程" in rail[0]["message"]
    # 返程候选已查到但没选：用户侧提示仍要单独保留，两者不是互相替代。
    assert any(item["code"] == "return_basis_pending" for item in details)

    issues = collect_issues(
        checks={"schedule": "unverified"},
        itinerary={"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b", 600)])]},
        rail_window=_rail_window({"state": "unknown", "basis": None}),
    )
    codes = [item["code"] for item in issues]
    assert "rail_window_unknown" in codes
    assert "schedule_unverified" not in codes


def test_rail_window_falls_back_to_day_window_state():
    """没有窗口依据时，按天的 unknown 也要能指出是哪一天。"""
    from planner.quality import schedule_details

    day = _day("2026-11-08", ["a", "b"], [_leg("a", "b", 600)])
    day["window"] = {"state": "unknown", "start": None, "end": None}
    details = schedule_details(itinerary={"days": [day]})
    rail = [item for item in details if item["code"] == "rail_window_unknown"]
    assert len(rail) == 1
    assert rail[0]["date"] == "2026-11-08"
    assert "11 月 8 日" in rail[0]["message"]
    # 窗口已知时不产生这条明细。
    day["window"] = {"state": "default", "start": 540, "end": 1080}
    assert not [item for item in schedule_details(itinerary={"days": [day]}) if item["code"] == "rail_window_unknown"]


def test_rail_window_unknown_names_the_chosen_train():
    """已选车次缺时刻时要点出车次号，不能只说「未知」。"""
    from planner.quality import schedule_details

    details = schedule_details(
        itinerary=None,
        rail_window={
            "outbound": {"state": "known", "basis": {"train_no": "G1813"}},
            "inbound": {"state": "unknown", "basis": {"train_no": "G3252", "depart": None}},
            "first_day": "2026-11-08",
            "last_day": "2026-11-09",
        },
    )
    rail = [item for item in details if item["code"] == "rail_window_unknown"]
    assert len(rail) == 1
    assert "G3252" in rail[0]["message"]


def test_detail_covers_only_its_own_category():
    """交通明细不能替开放规则或排程说话，聚合项要按类别各自保留。"""
    from planner.quality import collect_issues

    itinerary = {"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b")])]}
    checks = {"opening_rules": "unverified", "travel_time": "unverified", "schedule": "unverified"}
    codes = [item["code"] for item in collect_issues(checks=checks, itinerary=itinerary)]
    # 该行程 0 条适用开放规则，所以 opening_missing 与 travel_leg_unknown 都会出现。
    assert "travel_leg_unknown" in codes
    assert "travel_unverified" not in codes
    # 没有抵离明细，排程聚合项不能因为有别的明细就被吞掉。
    assert "schedule_unverified" in codes


# --- 复用异常诊断 ---------------------------------------------------------


def test_reused_ticket_without_applicable_facts_says_not_applicable():
    """复用的结果若都不适用，要说「不适用于本方案」，不能说成没有车次。"""
    from planner.quality import collect_issues

    issues = collect_issues(
        checks={"ticket": "unverified"},
        ticket_applicability={"code": "ticket_not_applicable", "ref": "raw-3", "reasons": ["station_other_city"]},
        ticket_state={"reason": "reused", "queried_at": "2026-11-06T00:25:34+00:00", "facts_count": 3, "applicable_count": 0},
    )
    codes = [item["code"] for item in issues]
    assert codes == ["ticket_not_applicable"]
    assert issues[0]["reasons"] == ["station_other_city"]
    assert issues[0]["owner"] == "system"
    assert "沿用" in issues[0]["detail"]
    # 复用一律不算已核实，文案分流不改变 checks.ticket。
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[{"status": "hard"}],
        ticket_reused=True,
        opening_required=0,
        opening_verified=0,
        legs_required=0,
        schedule_check="verified",
        budget_status="not_required",
    )
    assert quality["checks"]["ticket"] == "unverified"


def test_reused_ticket_after_parent_failure_keeps_failure_reason():
    """父方案没查到车次时，复用要如实说明失败原因，且不得伪装成「余票已刷新」。

    票务父状态是六态（ok/skipped/unavailable/missed/timeout/reused）之一；这里覆盖
    三个失败态与「只有诊断说明、没有父状态」的兜底。失败/未知父状态一律是未核实，
    message 不能说「余票未刷新」或「本轮沿用」（没有可刷新的余票依据）。
    """
    from planner.quality import collect_issues

    # (父状态, 该状态特有的失败措辞)
    for parent, keyword in (
        ("unavailable", "服务不可用或依赖缺失"),
        ("missed", "未调度或返回空结果"),
        ("timeout", "查票超时"),
    ):
        issues = collect_issues(
            checks={"ticket": "unverified"},
            ticket_state={
                "reason": "reused",
                "parent_reason": parent,
                "queried_at": "2026-11-06T00:25:34+00:00",
                "facts_count": 0,
                "applicable_count": 0,
            },
        )
        assert [item["code"] for item in issues] == ["ticket_unverified"]
        message = issues[0]["message"]
        assert keyword in message
        assert "本次未重新查询" in message
        assert "余票未知" in message
        # 父方案失败没有可核实的余票，不能说成「沿用/已刷新」。
        assert "余票未刷新" not in message
        assert "本轮沿用" not in message
        assert "已查到" not in message
        # 父方案失败、本轮沿用，仍然是未核实。
        quality = build_quality(
            summary_degraded=False,
            ticket_applicable=True,
            ticket_facts=[],
            ticket_reused=True,
            opening_required=0,
            opening_verified=0,
            legs_required=0,
            schedule_check="verified",
            budget_status="not_required",
        )
        assert quality["checks"]["ticket"] == "unverified"

    # 未知/非法父状态（例如旧词 failed、empty）也不得包装成有余票依据。
    for parent in ("failed", "empty", ""):
        issues = collect_issues(
            checks={"ticket": "unverified"},
            ticket_state={
                "reason": "reused",
                "parent_reason": parent,
                "queried_at": "2026-11-06T00:25:34+00:00",
                "facts_count": 0,
                "applicable_count": 0,
            },
        )
        assert [item["code"] for item in issues] == ["ticket_unverified"]
        assert "余票未刷新" not in issues[0]["message"]
        assert "本次未重新查询" in issues[0]["message"]

    # 父方案只留下失败说明、没有 reason 时，也要如实说「上次未成功」。
    issues = collect_issues(
        checks={"ticket": "unverified"},
        ticket_state={
            "reason": "reused",
            "queried_at": "2026-11-06T00:25:34+00:00",
            "facts_count": 0,
            "applicable_count": 0,
            "parent_reason": "",
            "parent_diagnostics": [{"code": "ticket_empty", "message": "票务响应里没有车次"}],
        },
    )
    assert [item["code"] for item in issues] == ["ticket_unverified"]
    assert "上次车次查询未成功" in issues[0]["message"]
    assert "票务响应里没有车次" in issues[0]["message"]
    assert "本次未重新查询" in issues[0]["message"]
    assert "余票未刷新" not in issues[0]["message"]


def test_reused_ticket_with_applicable_facts_stays_reused():
    """上次结果适用时仍是「未刷新」，不能升级成已核实。"""
    from planner.quality import collect_issues

    state = {
        "reason": "reused",
        "parent_reason": "ok",
        "queried_at": "2026-11-06T00:25:34+00:00",
        "facts_count": 84,
        "applicable_count": 84,
    }
    issues = collect_issues(checks={"ticket": "unverified"}, ticket_state=state)
    assert [item["code"] for item in issues] == ["ticket_reused"]
    quality = build_quality(
        summary_degraded=False,
        ticket_applicable=True,
        ticket_facts=[{"status": "hard"}],
        ticket_reused=True,
        opening_required=0,
        opening_verified=0,
        legs_required=0,
        schedule_check="verified",
        budget_status="not_required",
    )
    assert quality["checks"]["ticket"] == "unverified"


# --- 明细文案可读性 -------------------------------------------------------


def test_opening_missing_message_uses_place_name_and_date():
    """开放规则明细要说「哪一天 哪个景点」，不能把 poi_id 丢给用户看。"""
    from planner.quality import schedule_details

    itinerary = {"days": [_day("2026-11-08", ["B001575WDA"], [])]}
    item = next(entry for entry in schedule_details(itinerary=itinerary) if entry["code"] == "opening_missing")
    assert "11 月 8 日" in item["message"]
    assert "景点B001575WDA" in item["message"]
    assert item["poi_id"] == "B001575WDA"

    # 有中文名时只说中文名，poi_id 只留在技术字段里。
    named = {
        "days": [
            {
                "date": "2026-11-08",
                "items": [{"role": "visit", "poi_id": "B001575WDA", "name": "上海博物馆"}],
                "legs": [],
            }
        ]
    }
    named_item = next(entry for entry in schedule_details(itinerary=named) if entry["code"] == "opening_missing")
    assert named_item["message"] == "11 月 8 日 上海博物馆：尚无适用于当天的开放规则"
    assert "B001575WDA" not in named_item["message"]
    assert named_item["poi_id"] == "B001575WDA"

    # 未定位的景点也要用名称，不能让 poi_id 冒充地名。
    unlocated_item = next(
        entry
        for entry in schedule_details(itinerary=None, unlocated=[{"name": "田子坊", "kind": "must_visit"}])
        if entry["code"] == "opening_missing"
    )
    assert "田子坊" in unlocated_item["message"]


def test_travel_leg_unknown_message_uses_transport_mode():
    """步行、驾车、换乘要分别说清，不能一律说成「公共交通」。"""
    from planner.quality import schedule_details

    def message_for(mode: str) -> str:
        itinerary = {"days": [_day("2026-11-08", ["a", "b"], [_leg("a", "b", mode=mode)])]}
        item = next(entry for entry in schedule_details(itinerary=itinerary) if entry["code"] == "travel_leg_unknown")
        return item["message"]

    assert "驾车" in message_for("driving")
    assert "步行" in message_for("walking")
    assert "换乘" in message_for("transfer")

@pytest.mark.parametrize("payload,code", [
    ("没有查询到相关车次信息", "ticket_empty"),
    ([{"type": "text", "text": "没有查询到相关车次信息"}], "ticket_empty"),
    ("服务异常，请稍后重试", "unrecognized_ticket_payload"),
])
def test_live_empty_text_is_distinct_from_unknown_response(payload, code):
    facts, diag = parse_ticket_payload(
        payload, source_id="raw-1", queried_at="", expect_date=None,
        origin=None, destination=None,
    )
    assert facts == []
    assert diag["code"] == code


def test_empty_query_does_not_discard_successful_train_facts():
    from planner.ticket_facts import facts_from_records, applicable_facts
    records = [
        {"id": "raw-1", "tool": "get-tickets",
         "arguments": {"date": "2026-11-07", "trainFilterFlags": "G"},
         "payload": [{"type": "text", "text": "没有查询到相关车次信息"}]},
        {"id": "raw-2", "tool": "get-tickets", "arguments": {"date": "2026-11-07"},
         "payload": [{"type": "text", "text":
             "K180 新乡(telecode:XXF) -> 北京西(telecode:BXP) 23:22 -> 06:15 历时：06:53\n- 硬座: 有票 81元"}]},
    ]
    facts, diagnostics = facts_from_records(
        records, expect_date="2026-11-07", origin="新乡", destination="北京",
    )
    assert len(applicable_facts(facts)) == 1
    assert facts[0]["value"]["train_no"] == "K180"
    assert [d["code"] for d in diagnostics] == ["ticket_empty"]
    assert "高铁查询未找到车次" in diagnostics[0]["message"]
