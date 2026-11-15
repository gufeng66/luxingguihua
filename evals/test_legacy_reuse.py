"""旧快照复用：时刻和耗时是上一版口径算的，复用只能当参考。"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from planner.summary import degraded_plan, save_snapshot
from slots import TravelSlots


def _raw_text() -> list[dict]:
    return [
        {
            "type": "text",
            "text": "\n".join(
                [
                    "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                    "G1813 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 09:35 -> 15:27 历时：05:52",
                    "- 二等座: 有票 493元",
                ]
            ),
        }
    ]


def _legacy_snapshot() -> str:
    pid = str(uuid4())
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "从新乡坐高铁去上海",
            "schema_version": 3,
            "slots": TravelSlots(
                origin="新乡",
                destination="上海",
                date="2026-11-07",
                days=1,
                transport_mode="public_transit",
                budget="2000",
                lodging="上海站附近酒店",
            ).model_dump(),
            "ticket_reason": "ok",
            "ticket_context": "",
            "map_context": "",
            "answer": degraded_plan("skipped", []),
            "ticket_raw": [
                {
                    "id": "raw-3",
                    "tool": "get-tickets",
                    "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"},
                    "payload": _raw_text(),
                    "queried_at": "2026-11-03T00:00:00+00:00",
                }
            ],
            "ticket_facts": [
                {
                    "kind": "train",
                    "status": "verified",
                    "source_id": "raw-3",
                    "value": {
                        "train_no": "G1813",
                        "date": "2026-11-07",
                        "origin": "新乡东",
                        "destination": "上海虹桥",
                        "seat": "二等座",
                        "price": 493.0,
                        "availability": "有票",
                    },
                }
            ],
            "itinerary": {
                "days": [
                    {
                        "day": 1,
                        "date": "2026-11-07",
                        "items": [{"poi_id": "a", "name": "外滩", "role": "visit", "start": "16:07", "end": "17:07"}],
                        "legs": [{"origin_id": "h", "dest_id": "a", "mode": "walking", "duration_seconds": 600}],
                    }
                ],
                "conflicts": [],
                "schedule_status": "candidate",
            },
        }
    )
    return pid


async def _summary(**_kwargs):
    yield {"type": "_summary_done", "content": degraded_plan("skipped", [])}


def _stateful_snapshot() -> dict:
    """带失败状态、未定位项和路线状态的父快照：复用链必须原样带过来。"""
    return {
        "plan_id": "parent-1",
        "query": "从新乡坐高铁去上海",
        "slots": TravelSlots(
            origin="新乡",
            destination="上海",
            date="2026-11-07",
            days=1,
            transport_mode="public_transit",
            budget="2000",
            lodging="上海站附近酒店",
        ).model_dump(),
        "ticket_reason": "ok",
        "ticket_queried_at": "2026-11-03T00:00:00+00:00",
        "ticket_facts": [{"kind": "train", "value": {"train_no": "G1813", "applied": True}}],
        "ticket_raw": [{"id": "raw-3", "tool": "get-tickets"}],
        "route_error": "budget",
        "search_error": "rate_limit",
        "route_states": {"新乡东|上海虹桥|public_transit": {"status": "ok"}},
        "unlocated": [{"name": "外滩", "kind": "sight"}],
        "ticket_diagnostics": [{"code": "train_not_applicable"}],
        "ticket_notes": [{"code": "unparsed_tool_output"}],
        "rail_window": {"outbound": {"state": "known"}},
        "plan_basis": {"outbound": {"train_no": "G1813"}, "inbound": None},
        "map_context": "旧地图",
        "itinerary": {"days": [{"day": 1, "date": "2026-11-07", "items": [], "legs": []}]},
        "itinerary_needs_recheck": False,
        "facts_status": "verified",
        "candidates": [{"poi_id": "a", "name": "外滩"}],
        "constraints": {"lodging_kind": "area"},
        "facts": [],
        "search_usage": {"attempts": 1},
    }


def test_inherit_from_parent_summary_only_restores_every_field() -> None:
    """summary_only 走完整复用：失败、定位、路线状态、窗口、依据都要恢复。"""
    from planner.pipeline import _inherit_from_parent

    state = _inherit_from_parent(_stateful_snapshot(), "summary_only")

    assert state["route_error"] == "budget"
    assert state["search_error"] == "rate_limit"
    assert state["route_states"] == {"新乡东|上海虹桥|public_transit": {"status": "ok"}}
    assert state["unlocated"] == [{"name": "外滩", "kind": "sight"}]
    assert state["ticket_diagnostics"] == [{"code": "train_not_applicable"}]
    assert state["ticket_notes"] == [{"code": "unparsed_tool_output"}]
    assert state["plan_basis"]["outbound"]["train_no"] == "G1813"
    assert state["rail_window"] == {"outbound": {"state": "known"}}
    assert state["inherited_unknown_fields"] == []


def test_inherit_from_parent_budget_only_matches_summary_only() -> None:
    """budget_only 同样只重算预算，不该丢掉任何复用字段。"""
    from planner.pipeline import _inherit_from_parent

    full = _inherit_from_parent(_stateful_snapshot(), "summary_only")
    budget = _inherit_from_parent(_stateful_snapshot(), "budget_only")

    assert budget["route_error"] == full["route_error"] == "budget"
    assert budget["unlocated"] == full["unlocated"]
    assert budget["rail_window"] == full["rail_window"]


def test_inherit_from_parent_map_only_keeps_tickets_but_rebuilds_route_state() -> None:
    """map_only 只沿用票务事实与未改动方向的依据；路线状态、窗口必须用重建结果。"""
    from planner.pipeline import _inherit_from_parent

    state = _inherit_from_parent(_stateful_snapshot(), "map_only")

    # 票务四字段沿用。
    assert state["ticket_reason"] == "ok"
    assert state["ticket_queried_at"] == "2026-11-03T00:00:00+00:00"
    assert state["ticket_facts"][0]["value"]["train_no"] == "G1813"
    assert state["ticket_raw"][0]["id"] == "raw-3"
    # 路线状态、失败、未定位项、窗口都不继承（本轮重排会产出新结果）。
    assert state["route_error"] is None
    assert state["search_error"] is None
    assert state["route_states"] == {}
    assert state["unlocated"] == []
    assert state["rail_window"] is None
    # 去程方向没改，依据沿用；返程本来就是空的。
    assert state["plan_basis"]["outbound"]["train_no"] == "G1813"


def test_inherit_from_parent_map_only_drops_changed_direction_basis() -> None:
    """本轮改了票务区间或天数时，对应方向的旧依据不能再当成本轮依据。"""
    from planner.pipeline import _inherit_from_parent

    snapshot = _stateful_snapshot()
    snapshot["plan_basis"] = {"outbound": {"train_no": "G1813"}, "inbound": {"train_no": "G1814"}}

    # 日期/区间变化：两个方向一起失效。
    both = _inherit_from_parent(snapshot, "map_only", changed=["date"])
    assert both["plan_basis"] == {"outbound": None, "inbound": None}

    # 只改天数：去程日期不变仍可用，返程日期变了必须失效。
    days = _inherit_from_parent(snapshot, "map_only", changed=["days"])
    assert days["plan_basis"]["outbound"]["train_no"] == "G1813"
    assert days["plan_basis"]["inbound"] is None


def test_inherit_from_parent_ticket_and_map_inherits_nothing() -> None:
    """ticket_and_map 全部重建，任何旧状态都不能带进来。"""
    from planner.pipeline import _INHERIT_ALL_FIELDS, _inherit_from_parent

    state = _inherit_from_parent(_stateful_snapshot(), "ticket_and_map")

    assert state["route_error"] is None
    assert state["unlocated"] == []
    assert state["rail_window"] is None
    assert state["ticket_facts"] == []
    assert state["plan_basis"] == {"outbound": None, "inbound": None}
    # 全部字段都记为「没有可继承的父方案状态」。
    assert set(state["inherited_unknown_fields"]) == set(_INHERIT_ALL_FIELDS)


def test_inherit_from_parent_records_missing_fields() -> None:
    """父方案本来就没有的字段要记进 inherited_unknown_fields，而不是静默当空。"""
    from planner.pipeline import _inherit_from_parent

    state = _inherit_from_parent({"plan_id": "p1"}, "summary_only")

    assert state["route_error"] is None
    assert "route_error" in state["inherited_unknown_fields"]
    assert "rail_window" in state["inherited_unknown_fields"]


@pytest.mark.asyncio
async def test_reuse_keeps_parent_failure_and_unlocated(monkeypatch) -> None:
    """只改措辞时，父方案的失败原因与未定位项必须出现在子快照里。"""
    import planner_service as ps
    from planner.summary import load_snapshot

    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "stream_summary", _summary)
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")

    pid = str(uuid4())
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "从新乡坐高铁去上海",
            "schema_version": 4,
            "slots": TravelSlots(
                origin="新乡",
                destination="上海",
                date="2026-11-07",
                days=1,
                transport_mode="public_transit",
                budget="2000",
                lodging="上海站附近酒店",
            ).model_dump(),
            "ticket_reason": "ok",
            "answer": degraded_plan("skipped", []),
            "ticket_raw": [],
            "ticket_facts": [],
            "route_error": "budget",
            "unlocated": [{"name": "外滩", "kind": "visit"}],
            "route_states": {},
        }
    )

    events = [ev async for ev in ps.stream_plan("把注意事项的措辞改顺一点", parent_plan_id=pid)]
    final = next(e for e in events if e.get("type") == "final")
    child = load_snapshot(final["plan_id"])

    assert child is not None
    assert child["route_error"] == "budget"
    assert child["unlocated"] == [{"name": "外滩", "kind": "visit"}]
    assert final["quality"]["route_error"] == "budget"


@pytest.mark.asyncio
async def test_reuse_recomputes_basis_and_keeps_constraints_in_sync(monkeypatch) -> None:
    """复用分支也要重算依据：父方案的约束里写的是上一版依据，不能原样带到子快照。"""
    import planner_service as ps
    from planner.summary import load_snapshot

    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "stream_summary", _summary)
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")

    pid = str(uuid4())
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "从新乡坐高铁去上海",
            "schema_version": 4,
            "slots": TravelSlots(
                origin="新乡",
                destination="上海",
                date="2026-11-07",
                days=1,
                transport_mode="public_transit",
            ).model_dump(),
            "ticket_reason": "ok",
            "answer": degraded_plan("skipped", []),
            # 父方案只留了依据，没有对应的票务事实（历史上查询失败或跳过的方案就是这样）。
            "ticket_raw": [],
            "ticket_facts": [],
            "plan_basis": {"outbound": {"train_no": "G1813", "depart": "09:35"}, "inbound": None},
            "rail_window": {"outbound": {"state": "known"}},
            "candidates": [{"poi_id": "a", "name": "外滩"}],
            # 约束里是上一版依据；不覆盖就会和顶层依据互相矛盾。
            "constraints": {"lodging_kind": "area", "plan_basis": {"outbound": {"train_no": "OLD"}}},
            "request_context": {"party_size": 2},
        }
    )

    events = [ev async for ev in ps.stream_plan("把注意事项的措辞改顺一点", parent_plan_id=pid)]
    final = next(e for e in events if e.get("type") == "final")
    child = load_snapshot(final["plan_id"])

    assert child is not None
    # 没有适用事实时仍沿用父方案里仍然成立的依据，而不是整体清空。
    assert child["plan_basis"]["outbound"]["train_no"] == "G1813"
    # 顶层、请求上下文与约束三处必须是同一份依据。
    assert child["constraints"]["plan_basis"]["outbound"]["train_no"] == "G1813"
    assert child["request_context"]["plan_basis"]["outbound"]["train_no"] == "G1813"
    # 约束里的窗口也是按本次依据派生的，不是父方案的旧窗口。
    assert child["constraints"]["rail_window"]["outbound"]["state"] == "unknown"
    assert child["rail_window"]["outbound"]["state"] == "unknown"
    # 候选与约束与行程能否复用无关，照样要继承。
    assert child["candidates"][0]["name"] == "外滩"
    assert child["constraints"]["lodging_kind"] == "area"
    # 返程没有选定，快照要如实记录「待定」而不是假装已选。
    assert child["return_basis"] == {"selected": False, "options": []}


@pytest.mark.asyncio
async def test_legacy_snapshot_marks_reused_itinerary_unverified(monkeypatch) -> None:
    """沿用旧方案的行程时，交通耗时与排程都不能算已核实。"""
    import planner_service as ps

    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "stream_summary", _summary)
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")
    pid = _legacy_snapshot()

    events = [ev async for ev in ps.stream_plan("把注意事项的措辞改顺一点", parent_plan_id=pid)]

    final = next(e for e in events if e.get("type") == "final")
    quality = final["quality"]
    assert quality["checks"]["travel_time"] == "unverified"
    assert quality["checks"]["schedule"] == "unverified"
    notes = [item.get("code") for item in quality.get("notes") or []]
    assert "reused_legacy_snapshot" in notes


@pytest.mark.asyncio
async def test_legacy_itinerary_stays_unverified_across_repeated_reuse(monkeypatch) -> None:
    """旧口径行程连续复用多次，只要没有按当前规则重建，就持续保持待复核。"""
    import planner_service as ps
    from planner.summary import load_snapshot

    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "stream_summary", _summary)
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")
    pid = _legacy_snapshot()

    # 第一次复用：沿用旧口径行程。
    first_events = [ev async for ev in ps.stream_plan("把注意事项的措辞改顺一点", parent_plan_id=pid)]
    first_final = next(e for e in first_events if e.get("type") == "final")
    child_id = first_final["plan_id"]
    assert child_id != pid
    assert first_final["quality"]["checks"]["travel_time"] == "unverified"
    assert first_final["quality"]["checks"]["schedule"] == "unverified"

    # 落盘后的 v4 快照必须仍带 itinerary_needs_recheck=True，第二次复用才不会「文件格式已升级」被误判为已核实。
    child_snapshot = load_snapshot(child_id)
    assert child_snapshot is not None
    assert child_snapshot["itinerary_needs_recheck"] is True

    # 第二次复用（旧口径 → v4 → 再次沿用）：仍然待复核。
    second_events = [ev async for ev in ps.stream_plan("把注意事项再改客气一点", parent_plan_id=child_id)]
    second_final = next(e for e in second_events if e.get("type") == "final")
    assert second_final["quality"]["checks"]["travel_time"] == "unverified"
    assert second_final["quality"]["checks"]["schedule"] == "unverified"
    notes = [item.get("code") for item in second_final["quality"].get("notes") or []]
    assert "reused_legacy_snapshot" in notes


@pytest.mark.asyncio
async def test_current_itinerary_not_downgraded_when_only_wording_changes(monkeypatch) -> None:
    """当前口径生成的行程只改措辞，不能因为本轮没有重排就被额外降级。"""
    import planner_service as ps
    from planner.summary import save_snapshot, load_snapshot

    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "stream_summary", _summary)
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")

    # 当前口径快照：itinerary_needs_recheck=False，交通与排程都标记为已核实。
    pid = str(uuid4())
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "从新乡坐高铁去上海",
            "schema_version": 4,
            "slots": TravelSlots(
                origin="新乡",
                destination="上海",
                date="2026-11-07",
                days=1,
                transport_mode="public_transit",
                budget="2000",
                lodging="上海站附近酒店",
            ).model_dump(),
            "ticket_reason": "ok",
            "ticket_context": "",
            "map_context": "",
            "answer": degraded_plan("skipped", []),
            "ticket_raw": [],
            "ticket_facts": [],
            "itinerary": {
                "days": [
                    {
                        "day": 1,
                        "date": "2026-11-07",
                        "items": [{"poi_id": "a", "name": "外滩", "role": "visit", "start": "16:07", "end": "17:07"}],
                        "legs": [{"origin_id": "h", "dest_id": "a", "mode": "walking", "duration_seconds": 600, "confidence": "verified"}],
                    }
                ],
                "conflicts": [],
                "schedule_status": "checked_under_assumptions",
            },
            "itinerary_needs_recheck": False,
        }
    )

    events = [ev async for ev in ps.stream_plan("把注意事项的措辞改顺一点", parent_plan_id=pid)]
    final = next(e for e in events if e.get("type") == "final")
    assert final["quality"]["checks"]["travel_time"] == "verified"
    assert final["quality"]["checks"]["schedule"] == "verified"
    notes = [item.get("code") for item in final["quality"].get("notes") or []]
    assert "reused_legacy_snapshot" not in notes
