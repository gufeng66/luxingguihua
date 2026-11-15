"""补充与确认卡：合并新值、重判分支、确认状态持久化。不访问网络。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from planner.summary import degraded_plan, load_snapshot, save_snapshot
from slots import ExtractedSlots, TravelSlots


def _slots(**changes) -> TravelSlots:
    data = {
        "origin": "新乡",
        "destination": "郑州",
        "date": "2026-11-10",
        "days": 2,
        "transport_mode": "driving",
        "budget": "2000",
        "lodging": "郑州站附近",
    }
    data.update(changes)
    return TravelSlots.model_validate(data)


def _snapshot(slots: TravelSlots, *, request_context: dict | None = None) -> str:
    pid = str(uuid4())
    payload = {
        "plan_id": pid,
        "parent_plan_id": None,
        "query": "从新乡自驾去郑州两日游",
        "slots": slots.model_dump(),
        "ticket_reason": "skipped",
        "answer": degraded_plan("skipped", []),
    }
    if request_context is not None:
        payload["request_context"] = request_context
    save_snapshot(payload)
    return pid


async def _summary(**_kwargs):
    yield {"type": "_summary_done", "content": degraded_plan("skipped", [])}


def _patch(monkeypatch, ps) -> None:
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    monkeypatch.setattr(ps, "stream_summary", _summary)
    monkeypatch.setattr(ps, "get_ticket_agent", AsyncMock(return_value=None))
    monkeypatch.setenv("TRAVEL_MAP_FALLBACK", "0")


@pytest.mark.asyncio
async def test_confirm_merges_edited_lodging_and_keeps_parent(monkeypatch) -> None:
    import planner_service as ps

    _patch(monkeypatch, ps)
    pid = _snapshot(_slots())
    edited = _slots(lodging="郑州东站如家酒店")

    events = [
        ev
        async for ev in ps.stream_plan(
            "从新乡自驾去郑州两日游",
            slots=edited,
            parent_plan_id=pid,
            mode="confirm",
            lodging_scope="exact",
        )
    ]

    assert any(e.get("type") == "final" for e in events)
    assert not any(e.get("type") == "clarify" for e in events)
    final = next(e for e in events if e.get("type") == "final")
    child = load_snapshot(final["plan_id"])
    assert child is not None
    assert child["slots"]["lodging"] == "郑州东站如家酒店"
    assert child["parent_plan_id"] == pid
    # 确认状态要落盘，第二次修订才不用重新问住宿。
    assert child["request_context"]["lodging_scope"] == "exact"


@pytest.mark.asyncio
async def test_confirmed_area_is_not_asked_again(monkeypatch) -> None:
    import planner_service as ps

    _patch(monkeypatch, ps)
    pid = _snapshot(_slots(), request_context={"lodging_scope": "area", "lodging_kind": "area"})

    events = [
        ev
        async for ev in ps.stream_plan(
            "从新乡自驾去郑州两日游",
            slots=_slots(),
            parent_plan_id=pid,
            mode="confirm",
            lodging_scope="area",
        )
    ]

    assert any(e.get("type") == "final" for e in events)
    assert not any(e.get("type") == "clarify" for e in events)
    assert not any(
        e.get("type") == "warning" and e.get("code") == "lodging_area_pending" for e in events
    )


@pytest.mark.asyncio
async def test_confirm_without_changes_stays_offline(monkeypatch) -> None:
    """什么都没改时不许重查票、不许重跑模型抽取，只重出一版正文。"""
    import planner_service as ps

    _patch(monkeypatch, ps)
    extract = AsyncMock(side_effect=AssertionError("确认卡没有改动时不应再抽取槽位"))
    monkeypatch.setattr(ps, "extract_slots", extract)
    pid = _snapshot(_slots())

    events = [
        ev
        async for ev in ps.stream_plan(
            "从新乡自驾去郑州两日游",
            slots=_slots(),
            parent_plan_id=pid,
            mode="confirm",
        )
    ]

    extract.assert_not_called()
    assert any(e.get("type") == "final" for e in events)


@pytest.mark.asyncio
async def test_party_size_is_reused_from_request_context(monkeypatch) -> None:
    """第一次确认过人数后，第二次修订不能把预算退回「人数未知」。"""
    import planner_service as ps

    _patch(monkeypatch, ps)
    # 无变化的修订：抽取返回空 overlay（不改任何槽位），应走 summary_only 复用，
    # 而不是触发真实抽取。这里显式 mock 抽取，避免 P1-3 把未解析的变化当成 error。
    extract = AsyncMock(return_value=ExtractedSlots())
    monkeypatch.setattr(ps, "extract_slots", extract)
    pid = _snapshot(_slots(), request_context={"party_size": 2, "budget_scope": "total"})

    events = [
        ev
        async for ev in ps.stream_plan(
            "从新乡自驾去郑州两日游",
            parent_plan_id=pid,
            mode="revise",
        )
    ]

    final = next(e for e in events if e.get("type") == "final")
    assert "人数口径不清楚" not in final["content"]
    assert "口径：2 人" in final["content"]


def _rich_snapshot(*, itinerary_needs_recheck: bool = True) -> str:
    """带结构化行程、约束和地图文件的父方案：只改预算时要能整套沿用。"""
    import planner.pipeline as pipeline

    pid = str(uuid4())
    itinerary = {
        "days": [
            {
                "day": 1,
                "date": "2026-11-10",
                "items": [
                    {"poi_id": "hotel", "name": "郑州站附近", "role": "depart", "lng": 113.6, "lat": 34.7},
                    {"poi_id": "a", "name": "二七广场", "role": "visit", "lng": 113.66, "lat": 34.74,
                     "start": "09:15", "end": "10:15"},
                ],
                "legs": [
                    {"origin_id": "hotel", "dest_id": "a", "mode": "walking",
                     "duration_seconds": 600, "confidence": "verified", "depart": "09:00"},
                ],
            }
        ],
        "conflicts": [],
        "schedule_status": "checked_under_assumptions",
    }
    (pipeline.MAPS_DIR / f"{pid}.html").write_text("<html>parent map</html>", encoding="utf-8")
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "从新乡自驾去郑州两日游",
            "slots": _slots().model_dump(),
            "ticket_reason": "skipped",
            "answer": degraded_plan("skipped", []),
            "ticket_facts": [],
            "itinerary": itinerary,
            "itinerary_needs_recheck": itinerary_needs_recheck,
            "facts_status": "unavailable",
            "constraints": {"lodging_kind": "area", "transport_mode": "driving"},
            "candidates": [{"poi_id": "a", "name": "二七广场"}],
            "facts": [],
            "request_context": {"party_size": 2, "budget_scope": "total"},
        }
    )
    return pid


@pytest.mark.asyncio
async def test_budget_only_revision_keeps_itinerary_and_map(monkeypatch) -> None:
    """只改预算时不能把行程、地图、约束丢掉，也不能替父方案解除待复核标记。"""
    import planner.pipeline as pipeline
    import planner_service as ps

    _patch(monkeypatch, ps)
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=ExtractedSlots(budget="3000")))
    pid = _rich_snapshot()

    events = [
        ev
        async for ev in ps.stream_plan("预算改成3000", parent_plan_id=pid, mode="revise")
    ]

    final = next(e for e in events if e.get("type") == "final")
    child = load_snapshot(final["plan_id"])
    assert child is not None
    assert child["slots"]["budget"] == "3000"
    # 行程沿用父方案，不能变 null。
    assert child["itinerary"]["days"][0]["items"][1]["name"] == "二七广场"
    # 待复核标记继承父方案，不能因为「本轮没重排」就当成已重建。
    assert child["itinerary_needs_recheck"] is True
    # 约束和候选也要留着，正文的事实替换才有依据。
    assert child["constraints"]["lodging_kind"] == "area"
    assert child["candidates"][0]["name"] == "二七广场"
    # 地图要复制到子方案，方案链接才有效。
    assert (pipeline.MAPS_DIR / f"{final['plan_id']}.html").is_file()
    assert final.get("map_url") == f"/results/maps/{final['plan_id']}.html"


@pytest.mark.asyncio
async def test_said_party_size_is_written_back(monkeypatch) -> None:
    """原话说「改成三个人」时，人数要落进本轮快照，下一轮不能退回旧口径。"""
    import planner_service as ps

    _patch(monkeypatch, ps)
    # 模型没抽到任何槽位变化，人数只能靠原话解析。
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=ExtractedSlots()))
    pid = _rich_snapshot()

    events = [
        ev
        async for ev in ps.stream_plan("我们改成三个人一起去", parent_plan_id=pid, mode="revise")
    ]

    final = next(e for e in events if e.get("type") == "final")
    child = load_snapshot(final["plan_id"])
    assert child is not None
    assert child["request_context"]["party_size"] == 3
    assert final["request_context"]["party_size"] == 3


def test_saved_plan_basis_is_inherited_when_still_applicable() -> None:
    """只改景点时沿用父方案已选车次，不能被重新排序换成另一趟车。"""
    from planner.pipeline import _plan_basis
    from planner.ticket_facts import INBOUND, OUTBOUND

    def fact(train, depart, arrive):
        return {
            "kind": "train",
            "status": "verified",
            "source_id": "raw-1",
            "value": {
                "train_no": train,
                "date": "2026-11-10",
                "origin": "新乡东",
                "destination": "郑州东",
                "seat": "二等座",
                "price": 120.0,
                "availability": "有票",
                "depart": depart,
                "arrive": arrive,
                "arrive_date": "2026-11-10",
                "applied": True,
                "direction": OUTBOUND,
            },
        }

    facts = [fact("G1951", "08:00", "08:40"), fact("G1234", "07:20", "08:05")]
    saved = {OUTBOUND: {"train_no": "G1951", "origin": "system"}, INBOUND: None}

    basis = _plan_basis(facts, "把景点换成二七广场", saved=saved)
    assert basis[OUTBOUND]["train_no"] == "G1951"

    # 父方案选的车次已经不在本轮结果里，就必须改用新的可售车次。
    basis = _plan_basis([fact("G1234", "07:20", "08:05")], "把景点换成二七广场",
                        saved={OUTBOUND: {"train_no": "G1951"}, INBOUND: None})
    assert basis[OUTBOUND]["train_no"] == "G1234"


def test_refreshed_selected_train_rebuilds_all_fact_fields_or_degrades() -> None:
    """刷新同车次必须用新时刻/价格/来源；用户选车失效不能静默换车。"""
    from planner.pipeline import _plan_basis
    from planner.ticket_facts import INBOUND, OUTBOUND

    def fact(train: str, *, depart: str, price: float, source: str, availability: str = "有票") -> dict:
        return {
            "kind": "train", "status": "verified", "source_id": source,
            "value": {
                "train_no": train, "date": "2026-11-10", "depart_date": "2026-11-10",
                "origin": "新乡东", "destination": "郑州东", "seat": "二等座",
                "price": price, "availability": availability, "depart": depart, "arrive": "09:40",
                "arrive_date": "2026-11-10", "applied": True, "direction": OUTBOUND,
            },
        }

    refreshed = _plan_basis(
        [fact("G1951", depart="09:00", price=40, source="fresh")],
        "把景点换成二七广场",
        saved={OUTBOUND: {"train_no": "G1951", "origin": "user_selected", "depart": "08:30", "price": 30}, INBOUND: None},
    )
    assert refreshed[OUTBOUND]["depart"] == "09:00"
    assert refreshed[OUTBOUND]["source_id"] == "fresh"
    assert refreshed[OUTBOUND]["seats"][0]["price"] == 40

    unavailable = _plan_basis(
        [fact("G1951", depart="09:00", price=40, source="fresh", availability="无票"), fact("G1234", depart="08:00", price=50, source="fresh")],
        "把景点换成二七广场",
        saved={OUTBOUND: {"train_no": "G1951", "origin": "user_selected"}, INBOUND: None},
    )
    assert unavailable[OUTBOUND] is None


def _train_fact(train: str, depart: str, arrive: str, direction: str, *, day: str = "2026-11-10") -> dict:
    """一条可用的车次事实：区间与方向都成立，供选车依据测试使用。"""
    return {
        "kind": "train",
        "status": "verified",
        "source_id": "raw-1",
        "value": {
            "train_no": train,
            "date": day,
            "depart_date": day,
            "arrive_date": day,
            "origin": "郑州东",
            "destination": "新乡东",
            "seat": "二等座",
            "price": 120.0,
            "availability": "有票",
            "depart": depart,
            "arrive": arrive,
            "applied": True,
            "direction": direction,
            "station_match": "ok",
        },
    }


def test_return_period_revision_overrides_saved_inbound() -> None:
    """已选返程 G1952 后再改时段，本轮新意图必须生效，不能被父方案旧依据盖住。

    这正是用户实测的现象：单独选车会推荐 G3252，合并后却还是 G1952。
    """
    from planner.pipeline import _resolve_plan_basis
    from planner.ticket_facts import INBOUND, OUTBOUND

    snapshot = {
        "slots": _slots().model_dump(),
        "request_context": {
            "plan_basis": {
                OUTBOUND: {"train_no": "G1951"},
                INBOUND: {"train_no": "G1952"},
            }
        },
    }
    facts = [
        _train_fact("G1951", "08:00", "08:40", OUTBOUND),
        _train_fact("G1952", "17:00", "17:40", INBOUND),
        _train_fact("G3252", "14:00", "14:40", INBOUND),
    ]

    basis, window = _resolve_plan_basis(
        snapshot, _slots(), "返程改为下午", facts, day_count=2, travel_date="2026-11-10"
    )

    # 返程换成更接近「下午」的 G3252，去程仍沿用父方案的 G1951。
    assert basis[INBOUND]["train_no"] == "G3252"
    assert basis[OUTBOUND]["train_no"] == "G1951"
    # 依据变了，抵离窗口必须跟着重算，不能沿用父方案那份。
    assert window["inbound"]["basis"]["train_no"] == "G3252"


def test_earlier_return_train_shrinks_last_day_window() -> None:
    """选定更早的返程车后，末日可用窗口要收窄，末日活动不能原样保留。"""
    from datetime import date

    from planner.pipeline import _rail_window
    from planner.route_plan import day_window
    from planner.ticket_facts import INBOUND, OUTBOUND

    late = {OUTBOUND: None, INBOUND: _train_fact("G1952", "17:00", "17:40", INBOUND)["value"]}
    early = {OUTBOUND: None, INBOUND: _train_fact("G3252", "14:00", "14:40", INBOUND)["value"]}

    def window_of(basis: dict) -> dict:
        rail = _rail_window(basis, "2026-11-10", 1, required={"outbound": False, "inbound": True})
        return day_window(date(2026, 11, 10), {"date": "2026-11-10", "days": 1, "rail_window": rail})

    old_day = window_of(late)
    new_day = window_of(early)

    assert old_day["state"] == "constrained"
    assert new_day["state"] == "constrained"
    # 17:00 发车 → 15:50 结束；改到 14:00 发车后只剩 12:50，末日必须重排。
    assert old_day["end"] == 950
    assert new_day["end"] == 770
    assert new_day["end"] < old_day["end"]


def _errored_snapshot() -> str:
    """父方案带着「上次路线查询因预算未完成」的失败状态。"""
    pid = str(uuid4())
    save_snapshot(
        {
            "plan_id": pid,
            "parent_plan_id": None,
            "query": "从新乡自驾去郑州两日游",
            "slots": _slots().model_dump(),
            "ticket_reason": "skipped",
            "answer": degraded_plan("skipped", []),
            "route_error": "budget",
            "search_error": None,
            "unlocated": [{"name": "郑州博物馆", "kind": "sight"}],
            "route_states": {},
            "constraints": {"lodging_kind": "area", "transport_mode": "driving"},
        }
    )
    return pid


@pytest.mark.asyncio
async def test_rebuilt_plan_drops_inherited_route_error(monkeypatch) -> None:
    """本轮按新条件重建时，不能把父方案的旧 route_error 继承过来当成这次的失败。"""
    import planner_service as ps

    _patch(monkeypatch, ps)
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=ExtractedSlots(date="2026-11-12")))
    pid = _errored_snapshot()

    events = [
        ev async for ev in ps.stream_plan("改到 11 月 12 日出发", parent_plan_id=pid, mode="revise")
    ]

    final = next(e for e in events if e.get("type") == "final")
    # 日期变了走重建分支：旧错误不再适用，质量里必须是 None 而不是 budget。
    assert final["quality"]["route_error"] is None
    assert not any(
        e.get("type") == "warning" and e.get("code") == "route_unverified" for e in events
    )
    child = load_snapshot(final["plan_id"])
    assert child is not None
    assert child["route_error"] is None


@pytest.mark.asyncio
async def test_reused_failure_warning_says_inherited(monkeypatch) -> None:
    """复用带回的失败要说明是「沿用」，不能让人以为本次也查了、也失败了。"""
    import planner_service as ps

    _patch(monkeypatch, ps)
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=ExtractedSlots()))
    pid = _errored_snapshot()

    events = [
        ev async for ev in ps.stream_plan("把注意事项的措辞改顺一点", parent_plan_id=pid, mode="revise")
    ]

    final = next(e for e in events if e.get("type") == "final")
    # 复用分支仍要如实保留父方案的失败状态。
    assert final["quality"]["route_error"] == "budget"
    warnings = [
        e for e in events if e.get("type") == "warning" and e.get("code") == "route_unverified"
    ]
    assert warnings, "复用带回的失败必须给出告警"
    message = str(warnings[0]["message"])
    assert "沿用" in message
    assert "因查询额度或时间已用完" in message
    # 父方案的未定位项也要带过来，否则复用后行程与正文对不上。
    child = load_snapshot(final["plan_id"])
    assert child is not None
    assert child["unlocated"][0]["name"] == "郑州博物馆"


def test_budget_scope_per_person_multiplies_party() -> None:
    from planner.ticket_facts import budget_markdown

    facts = [
        {
            "kind": "train",
            "status": "verified",
            "source_id": "raw-1",
            "value": {"price": 100.0, "train_no": "G1234", "date": "2026-11-10",
                      "origin": "新乡", "destination": "郑州", "seat": "二等座",
                      "availability": "有票"},
        }
    ]
    text, status = budget_markdown(
        facts,
        user_budget="500",
        days=1,
        need_ticket=True,
        party_known=True,
        party=2,
        budget_scope="per_person",
    )
    assert "人均" in text
    assert status in {"unverified", "verified", "conflict"}
    assert "200" not in text or "人均换算" in text
