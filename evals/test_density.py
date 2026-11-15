from __future__ import annotations

import asyncio
import json
import os

from planner.map_fallback import _supplement_candidates
from planner.quality import schedule_check, schedule_details
from planner.route_plan import (
    _density,
    _pace_profile,
    activity_weight,
    candidate_retrieval_target,
    classify_day_kind,
    itinerary_markdown,
    schedule,
)


def _poi(poi_id: str, name: str, minutes: int, *, source: str = "planner_estimate") -> dict:
    return {
        "poi_id": poi_id,
        "name": name,
        "lng": 116.4 + len(poi_id) / 100,
        "lat": 39.9,
        "place_type": "公园",
        "visit_minutes": minutes,
        "visit_duration_source": source,
    }


def _plan(candidates: list[dict], *, days: int = 1, pace: str = "适中") -> dict:
    return schedule(
        candidates,
        {"date": "2026-12-01", "days": days, "pace": pace, "transport_mode": "walking", "facts": []},
        lambda *_args: None,
    )


def _plan_without_date(candidates: list[dict], *, days: int = 1, pace: str = "适中") -> dict:
    """出行日期缺失时的排程入口：不再回退任何示例日期。"""
    return schedule(
        candidates,
        {"days": days, "pace": pace, "transport_mode": "walking", "facts": []},
        lambda *_args: None,
    )


def test_missing_date_marks_date_unknown_instead_of_fake_date() -> None:
    plan = _plan_without_date([_poi("a", "甲公园", 60)], days=2)
    assert plan["schedule_status"] == "date_unknown"
    assert [day["day"] for day in plan["days"]] == [1, 2]
    assert [day["date"] for day in plan["days"]] == [None, None]
    for day in plan["days"]:
        assert day["window"]["state"] == "unknown"
        assert day["window"]["date_unknown"] is True
        assert day["window"]["start"] is None and day["window"]["end"] is None
        # 窗口未知时密度只能是 unknown，不能按完整游览日算成 sparse。
        assert day["density"]["status"] == "unknown"
        assert day["density"]["target_min"] is None
    # 正文不得凭空出现任何具体日期。
    markdown = itinerary_markdown(plan)
    assert "2026-01-05" not in markdown
    assert "2026-01-05" not in json.dumps(plan, ensure_ascii=False, default=str)
    assert "日期待定" in markdown


def test_missing_date_keeps_supply_keys_and_does_not_claim_shortage() -> None:
    plan = _plan_without_date([_poi("a", "甲公园", 60), _poi("b", "乙公园", 60)], days=2)
    assert candidate_retrieval_target({"days": 2}) is None
    supply = plan["candidate_supply"]
    # 键名是下游契约：retrieval_target 不能改名成 target。
    assert set(supply) == {
        "retrieval_target",
        "usable_candidates",
        "placed_candidates",
        "final_gap",
        "gap_support",
        "shortage",
        "date_unknown",
    }
    assert supply["date_unknown"] is True
    assert supply["retrieval_target"] is None
    # 没有日期就没有「按日期筛过」的候选可用数，只能是 None。
    assert supply["usable_candidates"] is None
    assert supply["placed_candidates"] == 0
    assert supply["final_gap"] == []
    assert supply["gap_support"] == []
    # 缺口无法评估时不能宣称候选不足。
    assert supply["shortage"] is False
    assert schedule_check(plan, []) == "unverified"

    plan = _plan([_poi("a", "甲公园", 60), _poi("b", "乙公园", 60)], days=3)
    assert all("density" in day for day in plan["days"])
    assert plan["candidate_supply"]["shortage"] is True
    assert schedule_check(plan, []) == "unverified"
    issues = schedule_details(itinerary=plan)
    assert [issue["code"] for issue in issues if issue["code"] == "candidate_shortage"]
    assert not [issue for issue in issues if issue["code"] == "schedule_sparse"]


def test_single_explicit_half_day_activity_is_not_sparse_for_relaxed_pace() -> None:
    plan = _plan([_poi("a", "半日主活动", 240, source="user")], pace="轻松")
    day = plan["days"][0]
    assert day["density"]["status"] == "balanced"
    visit = next(item for item in day["items"] if item["role"] == "visit")
    assert visit["visit_duration_confidence"] == "explicit"


def test_short_activity_remains_sparse_without_route_estimates() -> None:
    plan = _plan([_poi("a", "短活动", 60)])
    density = plan["days"][0]["density"]
    assert density["status"] == "sparse"
    assert density["visit_minutes"] == 60


def test_not_checking_in_is_relaxed_not_compact() -> None:
    assert _pace_profile({"pace": "不打卡、少折腾"}) == "relaxed"
    assert _pace_profile({"pace": "不想打卡"}) == "relaxed"
    assert _pace_profile({"pace": "别排太满"}) == "relaxed"
    assert _pace_profile({"pace": "这次想紧凑一点"}) == "compact"
    assert _pace_profile({"pace": "舒适型节奏"}) == "balanced"


def test_constrained_afternoon_window_scales_density_target() -> None:
    plan = schedule(
        [_poi("a", "下午活动", 120)],
        {
            "date": "2026-12-01", "days": 1, "pace": "适中", "transport_mode": "walking", "facts": [],
            "rail_window": {"outbound": {"state": "known", "arrive_date": "2026-12-01", "earliest_start": 15 * 60}},
        },
        lambda *_args: None,
    )
    density = plan["days"][0]["density"]
    assert density["status"] == "balanced", plan
    assert density["target_min"] < 120
    assert not plan["candidate_supply"]["final_gap"]


def test_unknown_window_keeps_activity_but_does_not_trigger_full_day_gap() -> None:
    plan = schedule(
        [_poi("a", "时刻待定活动", 120)],
        {
            "date": "2026-12-01", "days": 1, "pace": "适中", "transport_mode": "walking", "facts": [],
            "rail_window": {"outbound": {"state": "unknown"}},
        },
        lambda *_args: None,
    )
    density = plan["days"][0]["density"]
    assert density["status"] == "unknown"
    assert density["visit_minutes"] == 120
    assert not plan["candidate_supply"]["final_gap"]


def test_over_target_is_marked_tight_not_claimed_in_range() -> None:
    density = _density(
        {"items": [{"role": "visit", "visit_minutes": 420}], "legs": [], "window": {"state": "default"}},
        {"pace": "适中"},
    )
    assert density["status"] == "balanced"
    assert density["over_target"] is True
    assert "超过当天参考上限" in density["reason"]


def test_user_duration_from_query_reaches_scheduled_visit() -> None:
    plan = schedule(
        [{"poi_id": "a", "name": "故宫", "asked_name": "故宫", "lng": 116.4, "lat": 39.9, "place_type": "博物馆"}],
        {"date": "2026-12-01", "days": 1, "query_text": "故宫玩半天", "transport_mode": "walking", "facts": []},
        lambda *_args: None,
    )
    visit = next(item for item in plan["days"][0]["items"] if item["role"] == "visit")
    assert visit["visit_minutes"] == 240
    assert visit["visit_duration_source"] == "user"


def test_initial_assignment_does_not_leave_a_matching_day_empty() -> None:
    plan = _plan([_poi(str(i), f"活动{i}", 120) for i in range(4)], days=2)
    assert all(day["density"]["activity_count"] >= 1 for day in plan["days"])


def test_zero_initial_candidates_can_use_one_global_supplement_round() -> None:
    class Response:
        def json(self):
            return {
                "status": "1",
                "pois": [
                    {"id": "p1", "name": "候选一", "location": "116.40,39.90", "type": "风景名胜", "address": "甲"},
                    {"id": "p2", "name": "候选二", "location": "116.41,39.90", "type": "风景名胜", "address": "乙"},
                ],
            }

    class Client:
        def __init__(self):
            self.calls = []

        async def get(self, *_args, **kwargs):
            self.calls.append(kwargs.get("params") or {})
            return Response()

    client = Client()
    plan = {
        "candidate_supply": {
            "shortage": True,
            "final_gap": [{"day": 1, "minutes": 120}, {"day": 2, "minutes": 120}],
        },
        "days": [{"day": 1, "items": []}, {"day": 2, "items": []}],
    }
    prior = os.environ.get("AMAP_KEY")
    os.environ["AMAP_KEY"] = "test-key"
    try:
        candidates, meta = asyncio.run(
            _supplement_candidates(client, city="测试市", sights=[], plan=plan, stay=None, preferences=None, budget=None, cancel_event=None, stats={})
        )
    finally:
        if prior is None:
            os.environ.pop("AMAP_KEY", None)
        else:
            os.environ["AMAP_KEY"] = prior
    assert len(client.calls) == 2
    assert {row["supplement_target_day"] for row in candidates} == {1, 2}
    assert meta["reason"] == "matched"


def test_candidate_retrieval_target_is_density_budget_not_must_visit_count() -> None:
    """候选检索预算由「每天可用窗口×节奏密度」推导，不是必去项数量/总上限。

    这条把 m02971 指出的「候选数量规则没有被稳定定义」固定成可回归的纯函数契约：
    - 已知出行日期才推导（默认节奏 balanced，每天基础 240 分钟 → ceil(240/90*1.3)=4）；
    - 天数、节奏会改变预算；必去项数量不会；
    - 出行日期缺失时返回 None，不编造预算；
    - 结果只依赖入参、稳定可复现，可安全随快照落盘。
    """
    base = {"date": "2026-11-04", "days": 1}
    assert candidate_retrieval_target(base) == 4
    assert candidate_retrieval_target({**base, "days": 2}) == 7
    # 节奏（而不是必去项数量）决定预算。
    assert candidate_retrieval_target({**base, "pace": "轻松"}) < 4
    assert candidate_retrieval_target({**base, "pace": "紧凑"}) > 4
    assert candidate_retrieval_target({**base, "must_visit": ["甲", "乙", "丙", "丁"]}) == 4
    # 没有出行日期就没有每天窗口，明确返回 None 而非用默认 09:00–18:00 估算。
    assert candidate_retrieval_target({"days": 2}) is None
    assert candidate_retrieval_target({}) is None
    # 同一入参重复调用结果一致，保证回放/快照可比对。
    assert candidate_retrieval_target(base) == candidate_retrieval_target(dict(base))


def test_activity_weight_prefers_category_before_duration() -> None:
    assert activity_weight({"place_type": "主题公园", "visit_minutes": 181}) == 2.0
    assert activity_weight({"place_type": "公园", "visit_minutes": 180}) == 0.5
    assert activity_weight({"place_type": "博物馆", "visit_minutes": 120}) == 1.0


def test_day_kind_requires_valid_boundary_times() -> None:
    from datetime import date

    first = date(2026, 12, 1)
    last = date(2026, 12, 2)
    dates = [first, last]
    rail = {
        "outbound": {"state": "known", "arrive_date": first.isoformat(), "earliest_start": 900},
        "inbound": {"state": "known", "depart_date": last.isoformat(), "latest_end": 960},
    }
    assert classify_day_kind(first, dates, rail) == "arrival"
    assert classify_day_kind(last, dates, rail) == "departure"
    same_day = {"outbound": rail["outbound"], "inbound": {"state": "known", "depart_date": first.isoformat(), "latest_end": 960}}
    assert classify_day_kind(first, [first], same_day) == "arrival_departure"
    assert classify_day_kind(first, dates, {"outbound": {"state": "unknown"}}) == "unknown"
    assert classify_day_kind(first, dates, None) == "full"


def test_pace_targets_expose_count_and_weight_without_changing_minutes_status() -> None:
    plan = _plan([_poi(str(i), f"短活动{i}", 60) for i in range(3)], pace="紧凑")
    density = plan["days"][0]["density"]
    assert density["target_count_min"] == 3
    assert density["activity_weight"] == 1.5
    assert density["count_status"] == "met"
    assert density["minutes_status"] == "below"


def test_unknown_window_does_not_create_count_target() -> None:
    plan = schedule(
        [_poi("a", "待定活动", 120)],
        {
            "date": "2026-12-01", "days": 1, "pace": "紧凑", "transport_mode": "walking", "facts": [],
            "rail_window": {"outbound": {"state": "unknown"}},
        },
        lambda *_args: None,
    )
    density = plan["days"][0]["density"]
    assert density["day_kind"] == "unknown"
    assert density["target_count_min"] is None
    assert density["count_status"] == "unknown"
