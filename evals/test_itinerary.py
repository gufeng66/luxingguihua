"""排程、证据门槛、槽位合并。不调用模型，不访问外网。"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner.route_plan import day_window, itinerary_markdown, route_cache_key, schedule  # noqa: E402
from planner.summary import replace_itinerary_section, reusable_itinerary  # noqa: E402
from planner.routing import classify_revision, merge_slots  # noqa: E402
from slots import ExtractedSlots, TravelSlots  # noqa: E402


def _poi(poi_id, name, lng, lat, **extra):
    item = {"poi_id": poi_id, "name": name, "lng": lng, "lat": lat, "place_type": extra.pop("place_type", "景点")}
    item.update(extra)
    return item


def _stay():
    return {"poi_id": "hotel", "name": "酒店", "lng": 116.40, "lat": 39.90}


def _table(slow=("丁",)):
    def duration_fn(origin, dest, mode, day, depart):
        if any(token in (origin, dest) for token in slow):
            return 180 * 60
        if mode == "walking":
            return 40 * 60
        return 30 * 60

    return duration_fn


def _names(plan, day):
    for item in plan["days"]:
        if item["day"] == day:
            return [row["name"] for row in item["items"] if row.get("role") == "visit"]
    return []


def _when(plan, day, name):
    for item in plan["days"]:
        if item["day"] != day:
            continue
        for row in item["items"]:
            if row["name"] == name:
                return row["start"], row["end"]
    return None, None


def test_t01_closed_monday_moves_must_and_drops_far_optional():
    """2026-11-02 是周一。必去馆挪到周二，远郊丁写明时间不足。"""
    assert date(2026, 11, 2).weekday() == 0
    candidates = [
        _poi("jia", "甲", 116.41, 39.92, place_type="博物馆", must_visit=True, visit_minutes=150),
        _poi("yi", "乙", 116.45, 39.901, place_type="公园", visit_minutes=90),
        _poi("bing", "丙", 116.43, 39.93, place_type="街区", visit_minutes=90),
        _poi("ding", "丁", 117.20, 39.90, visit_minutes=180),
    ]
    constraints = {
        "date": "2026-11-02",
        "days": 2,
        "lodging_poi": _stay(),
        "transport_mode": "public_transit",
        "must_visit": ["甲"],
        "facts": [
            {"poi_id": "jia", "kind": "closed_weekday", "value": 0, "status": "hard"},
            {"poi_id": "jia", "kind": "opening_hours", "value": [[9 * 60, 17 * 60]], "status": "hard"},
        ],
    }
    plan = schedule(candidates, constraints, _table(slow=("ding",)))
    assert "甲" not in _names(plan, 1)
    assert "甲" in _names(plan, 2)
    assert "丁" not in _names(plan, 1) and "丁" not in _names(plan, 2)
    assert any(item["name"] == "丁" and "时间" in item["reason"] for item in plan["dropped"])
    assert _when(plan, 1, "乙") == ("09:30", "11:00")
    assert _when(plan, 1, "丙") == ("12:45", "14:15")
    assert _when(plan, 2, "甲") == ("09:30", "12:00")
    returns = [row for day in plan["days"] if day["day"] == 2 for row in day["items"] if row.get("role") == "return"]
    assert returns and (returns[0]["start"], returns[0]["end"]) == ("13:45", "13:45")


def test_t05_midday_break_and_last_entry():
    lodging = _stay()
    poi = _poi("m", "馆", 116.41, 39.91, visit_minutes=90)
    constraints = {
        "date": "2026-11-03",
        "days": 1,
        "lodging_poi": lodging,
        "transport_mode": "public_transit",
        "facts": [
            {"poi_id": "m", "kind": "opening_hours", "value": [[9 * 60, 12 * 60], [13 * 60 + 30, 17 * 60]], "status": "hard"},
            {"poi_id": "m", "kind": "last_entry", "value": 16 * 60, "status": "hard"},
        ],
    }

    def duration_fn(origin, dest, mode, day, depart):
        return 150 * 60 if origin == "hotel" else 20 * 60

    plan = schedule([poi], constraints, duration_fn)
    assert _when(plan, 1, "馆") == ("13:30", "15:00")

    constraints["facts"][1]["value"] = 13 * 60
    blocked = schedule([poi], constraints, duration_fn)
    assert "馆" not in _names(blocked, 1)
    assert blocked["conflicts"] or blocked["dropped"]


def test_t07_far_must_is_not_cut_by_distance():
    near = [_poi(f"n{i}", f"近{i}", 116.41 + i * 0.01, 39.91, visit_minutes=90) for i in range(4)]
    far = _poi("far", "远必去", 117.2, 39.9, must_visit=True, visit_minutes=60)
    constraints = {
        "date": "2026-11-03",
        "days": 1,
        "lodging_poi": _stay(),
        "must_visit": ["远必去"],
        "transport_mode": "walking",
    }

    def duration_fn(origin, dest, mode, day, depart):
        return 10 * 60

    plan = schedule([*near, far], constraints, duration_fn)
    assert "远必去" in _names(plan, 1)
    assert all(item["name"] != "远必去" for item in plan["dropped"])


def test_t10_modes_follow_preference_and_short_walk():
    poi = _poi("a", "甲", 116.41, 39.91, visit_minutes=60)
    base = {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "facts": [{"poi_id": "a", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"}]}

    def run(mode, walk_seconds):
        def duration_fn(origin, dest, chosen, day, depart):
            if chosen == "walking":
                return walk_seconds
            if chosen == "driving":
                return 15 * 60
            return 25 * 60

        plan = schedule([poi], {**base, "transport_mode": mode}, duration_fn)
        return plan["days"][0]["legs"][0]["mode"]

    assert run("public_transit", 10 * 60) == "walking"
    assert run("public_transit", 40 * 60) == "transfer"
    assert run("driving", 10 * 60) == "driving"


def test_t11_cache_key_changes_with_direction_date_and_hour():
    morning = route_cache_key("a", "b", "transfer", "2026-11-02", 9 * 60)
    assert morning != route_cache_key("b", "a", "transfer", "2026-11-02", 9 * 60)
    assert morning != route_cache_key("a", "b", "transfer", "2026-11-03", 9 * 60)
    assert morning != route_cache_key("a", "b", "transfer", "2026-11-02", 15 * 60)
    assert morning == route_cache_key("a", "b", "transfer", "2026-11-02", 9 * 60 + 20)


def test_t12_unknown_duration_is_not_zero_and_keeps_the_day():
    poi = _poi("a", "甲", 116.41, 39.91, visit_minutes=60)
    constraints = {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "transport_mode": "public_transit"}
    plan = schedule([poi], constraints, lambda *_args: None)
    assert _names(plan, 1) == ["甲"]
    assert plan["schedule_status"] == "candidate"
    assert _when(plan, 1, "甲") == (None, None)
    assert all(leg["duration_seconds"] is None for leg in plan["days"][0]["legs"])
    assert not plan["conflicts"]


def test_t17_table_and_map_come_from_the_same_days():
    from planner.route_plan import days_to_tasks

    poi = _poi("a", "甲", 116.41, 39.91, visit_minutes=60)
    constraints = {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "transport_mode": "driving"}
    plan = schedule([poi], constraints, lambda *_args: 20 * 60)
    prose = "## 需求摘要\n随便\n\n## 景点建议\n乙在前\n\n## 车票建议\n无\n\n## 预算\n无\n\n## 行程表\n- 第1天：乙 → 甲\n\n## 行程表\n另一份\n\n## 注意事项\n尾\n"
    replaced = replace_itinerary_section(prose, itinerary_markdown(plan))
    assert replaced.count("## 行程表") == 1
    assert "甲" in replaced.split("## 注意事项")[0]
    assert "乙 → 甲" not in replaced
    tasks = days_to_tasks(plan, "北京")
    poi_names = [item["text"] for item in tasks if item["type"] == "poi"]
    assert poi_names[1] == "甲"
    assert tasks[1]["routeType"] == "driving"


def test_lodging_precision_flows_into_map_remark_and_prose():
    """住宿精度贯穿地图任务备注与正文：区域参考起点不冒充具体住宿地。"""
    from planner.route_plan import days_to_tasks, itinerary_markdown

    poi = _poi("a", "甲", 116.41, 39.91, visit_minutes=60)
    stay = {"poi_id": "lodging", "name": "住宿地", "lng": 116.40, "lat": 39.90, "lodging_kind": "area", "precision": "area"}
    constraints = {"date": "2026-11-03", "days": 1, "lodging_poi": stay, "transport_mode": "driving"}
    plan = schedule([poi], constraints, lambda *_args: 20 * 60)

    tasks = days_to_tasks(plan, "北京")
    remarks = [item["remark"] for item in tasks if item.get("remark")]
    assert any("区域参考起点" in remark for remark in remarks)

    prose = itinerary_markdown(plan)
    # 正文交通行应把参考耗时标为「参考」，不能当作已核实。
    assert "参考" in prose


def test_clock_text_stays_unverified():
    from planner.route_plan import clock_text_unverified

    assert clock_text_unverified("已预约 10:00")
    poi = _poi("a", "甲", 116.41, 39.91, visit_minutes=60)
    plan = schedule(
        [poi],
        {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "unverified_clock": True},
        lambda *_args: 20 * 60,
    )
    text = itinerary_markdown(plan)
    assert "尚未纳入结构化时间校验" in text
    assert "09:00" in text


def test_merge_keeps_unset_must_and_mode():
    base = TravelSlots(
        origin="杭州",
        destination="苏州",
        date="2026-11-02",
        days=2,
        must_visit=["甲"],
        avoid=["丙"],
        transport_mode="driving",
    )
    untouched = merge_slots(base, ExtractedSlots(preferences="简短"))
    assert untouched.must_visit == ["甲"]
    assert untouched.transport_mode == "driving"
    cleared = merge_slots(base, ExtractedSlots(must_visit=[]))
    assert cleared.must_visit == []
    assert cleared.avoid == ["丙"]
    kept_mode = merge_slots(base, ExtractedSlots())
    assert kept_mode.transport_mode == "driving"


def test_revision_kinds_for_wording_date_and_mode():
    assert classify_revision("措辞简短些") == "summary_only"
    assert classify_revision("改期到下周") == "ticket_and_map"
    assert classify_revision("改成自驾") == "ticket_and_map"


def test_old_snapshot_is_not_hard_evidence():
    assert reusable_itinerary({"map_context": "<script id=\"amapTaskData\">[]</script>", "answer": "旧文案"}) is None
    kept = {"days": [{"day": 1}]}
    assert reusable_itinerary({"itinerary": kept}) == kept


def test_parse_evidence_examples():
    from planner.travel_search import parse_evidence

    hosts = frozenset({"museum.example"})
    url = "https://museum.example/hours"

    def kinds(summary):
        return [
            (fact["kind"], fact["status"])
            for fact in parse_evidence(summary, url=url, poi_name="甲博物馆", poi_id="jia", allowed_hosts=hosts)
        ]

    assert ("closed_weekday", "hard") in kinds("甲博物馆每周一闭馆。")
    assert ("closed_weekday", "hard") not in kinds("甲博物馆本周一闭馆。")
    assert all(status == "pending" for _kind, status in kinds("甲博物馆本周一闭馆。"))
    assert kinds("甲博物馆周一不闭馆。") == []
    pending = kinds("甲博物馆周一闭馆，法定节假日除外。")
    assert pending and all(status == "pending" for _kind, status in pending)
    other = parse_evidence(
        "乙馆每周一闭馆。",
        url=url,
        poi_name="甲博物馆",
        poi_id="jia",
        allowed_hosts=hosts,
    )
    assert other == []
    off = parse_evidence(
        "甲博物馆每周一闭馆。",
        url="https://notes.example/a",
        poi_name="甲博物馆",
        poi_id="jia",
        allowed_hosts=hosts,
    )
    assert off and off[0]["status"] == "pending"


def test_hard_fact_moves_museum_off_monday():
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "故宫每周一闭馆。",
        url="https://www.dpm.org.cn/visit",
        poi_name="故宫",
        poi_id="dpm",
    )
    hard = [fact for fact in facts if fact["status"] == "hard"]
    assert hard and hard[0]["kind"] == "closed_weekday"
    plan = schedule(
        [_poi("dpm", "故宫", 116.397, 39.918, must_visit=True, visit_minutes=150, place_type="博物馆")],
        {
            "date": "2026-11-02",
            "days": 2,
            "lodging_poi": _stay(),
            "must_visit": ["故宫"],
            "facts": hard,
        },
        _table(slow=()),
    )
    assert "故宫" not in _names(plan, 1)
    assert "故宫" in _names(plan, 2)


def test_t13_attempt_cap_includes_retries():
    from planner.travel_search import gather_facts

    calls = {"n": 0}

    async def post(query, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow")
        return {"results": []}

    clock = {"t": 0.0}

    async def run():
        return await gather_facts(
            [{"poi_id": "a", "name": "甲", "must_visit": True, "place_type": "博物馆"}],
            city="北京",
            day="2026-11-02",
            api_key="test",
            post=post,
            clock=lambda: clock["t"],
        )

    result = asyncio.run(run())
    assert result["attempts"] <= 6
    assert calls["n"] <= 6


def test_t14_deadline_stops_new_work_and_late_results():
    from planner.travel_search import SearchBudget

    clock = {"t": 0.0}
    budget = SearchBudget(clock=lambda: clock["t"], limit=6, window=30)
    clock["t"] = 31
    assert asyncio.run(budget.reserve()) is None


def test_t15_no_key_does_not_call_network():
    from planner.travel_search import gather_facts

    async def post(query, timeout):
        raise AssertionError("should not search")

    result = asyncio.run(
        gather_facts(
            [{"poi_id": "a", "name": "甲"}],
            city="北京",
            day="2026-11-02",
            api_key="",
            post=post,
        )
    )
    assert result["facts_status"] == "unavailable"
    assert result["attempts"] == 0


def test_duration_payload_empty_is_null():
    from planner.map_fallback import duration_from_payload, route_state_from_payload

    assert duration_from_payload("transfer", {"status": "1", "route": {"transits": [{}]}}) is None
    # 0 秒不是「很近」，高德没有可用路线：归为 no_route，不能当成已核实耗时。
    assert duration_from_payload("walking", {"status": "1", "route": {"paths": [{"duration": "0"}]}}) is None
    assert route_state_from_payload("walking", {"status": "1", "route": {"paths": [{"duration": "0"}]}}) == ("no_route", None)
    assert duration_from_payload("walking", {"status": "0", "route": {"paths": [{"duration": "10"}]}}) is None
    assert route_state_from_payload("walking", {"status": "0", "route": {"paths": [{"duration": "10"}]}}) == ("failed", None)
    assert route_state_from_payload("walking", {"status": "1", "route": {"paths": [{"duration": "600"}]}}) == ("ok", 600)


def test_route_states_are_distinct():
    from planner.map_fallback import ROUTE_STATES

    assert ROUTE_STATES == ("ok", "no_route", "failed", "not_queried")


def test_needed_legs_after_drop_are_new_edges():
    from planner.map_fallback import needed_route_queries
    from planner.route_plan import route_cache_key

    plan = {
        "days": [
            {
                "date": "2026-11-02",
                "items": [
                    {"poi_id": "h", "name": "酒店", "lng": 1, "lat": 2, "role": "depart"},
                    {"poi_id": "b", "name": "乙", "lng": 3, "lat": 4, "role": "visit"},
                ],
                "legs": [{"origin_id": "h", "dest_id": "b", "depart": "09:00", "mode": "transfer"}],
            }
        ]
    }
    table = {route_cache_key("h", "a", "transfer", "2026-11-02", 9 * 60): 30}
    queries = needed_route_queries(plan, "public_transit", table)
    keys = [item[0] for item in queries]
    assert route_cache_key("h", "b", "transfer", "2026-11-02", 9 * 60) in keys
    assert route_cache_key("h", "a", "transfer", "2026-11-02", 9 * 60) not in keys


def test_final_check_keeps_closed_must_and_does_not_pass_empty():
    poi = _poi("jia", "甲", 116.41, 39.91, must_visit=True, visit_minutes=90, place_type="博物馆")
    constraints = {
        "date": "2026-11-02",
        "days": 1,
        "lodging_poi": _stay(),
        "must_visit": ["甲"],
        "facts": [{"poi_id": "jia", "kind": "closed_weekday", "value": 0, "status": "hard"}],
    }
    plan = schedule([poi], constraints, _table(slow=()))
    # 排不出可执行方案的日期仍要留在行程里，只是当天没有任何 visit。
    assert len(plan["days"]) == 1
    assert plan["days"][0]["date"] == "2026-11-02"
    assert not any(item.get("role") == "visit" for item in plan["days"][0]["items"])
    assert plan["days"][0]["legs"] == []
    assert plan["conflicts"] and plan["conflicts"][0]["reason"] == "出行日期内关闭"
    again = schedule(
        [poi],
        constraints,
        _table(slow=()),
        assignment=plan["assignment"],
        kept_dropped=plan["dropped"],
        kept_conflicts=plan["conflicts"],
    )
    assert len(again["days"]) == 1
    assert again["days"][0]["date"] == "2026-11-02"
    assert not any(item.get("role") == "visit" for item in again["days"][0]["items"])
    assert again["conflicts"][0]["reason"] == "出行日期内关闭"
    assert again["schedule_status"] == "conflict"
    bare = schedule([poi], constraints, _table(slow=()), assignment=plan["assignment"])
    assert any(item["name"] == "甲" for item in bare["conflicts"])
    assert bare["schedule_status"] != "checked_under_assumptions"


def test_no_lodging_uses_real_poi_not_centroid():
    pois = [
        _poi("jia", "甲", 116.41, 39.92, visit_minutes=60),
        _poi("yi", "乙", 116.45, 39.90, visit_minutes=60),
    ]
    plan = schedule(pois, {"date": "2026-11-03", "days": 1}, _table(slow=()))
    assert _names(plan, 1)
    assert plan["schedule_status"] != "checked_under_assumptions" or plan["days"]


def test_dated_hours_do_not_bind_other_days():
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "甲博物馆2020-01-01开放时间09:00—10:00。",
        url="https://www.dpm.org.cn/hours",
        poi_name="甲博物馆",
        poi_id="jia",
    )
    assert facts[0]["status"] == "hard"
    assert facts[0]["applies_on"] == ["2020-01-01"]
    vague = parse_evidence(
        "甲博物馆2020年1月1日开放时间09:00—17:00。",
        url="https://www.dpm.org.cn/hours",
        poi_name="甲博物馆",
        poi_id="jia",
    )
    assert vague[0]["status"] == "pending"
    assert "applies_on" not in vague[0]
    poi = _poi("jia", "甲博物馆", 116.41, 39.91, must_visit=True, visit_minutes=90)
    plan = schedule(
        [poi],
        {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "must_visit": ["甲博物馆"], "facts": facts},
        lambda *_args: 20 * 60,
    )
    assert "甲博物馆" in _names(plan, 1)


def test_cross_result_hours_conflict_and_partial_coverage():
    from planner.travel_search import gather_facts

    async def post(query, timeout):
        if "甲博物馆" in query:
            return {
                "results": [
                    {"url": "https://www.dpm.org.cn/a", "title": "a", "content": "甲博物馆开放时间09:00—17:00。"},
                    {"url": "https://www.dpm.org.cn/b", "title": "b", "content": "甲博物馆开放时间10:00—16:00。"},
                ]
            }
        return {"results": []}

    result = asyncio.run(
        gather_facts(
            [
                {"poi_id": "jia", "name": "甲博物馆", "must_visit": True, "place_type": "博物馆"},
                {"poi_id": "yi", "name": "乙公园"},
            ],
            city="北京",
            day="2026-11-03",
            api_key="test",
            post=post,
        )
    )
    assert result["facts"]
    assert all(fact["status"] == "pending" for fact in result["facts"])
    assert result["facts_status"] == "partial"


def test_deadline_does_not_start_searches_after_window():
    from planner.travel_search import gather_facts

    clock = {"t": 0.0}
    starts = []

    async def post(query, timeout):
        starts.append(clock["t"])
        await asyncio.sleep(0)
        clock["t"] += 20
        return {"results": []}

    asyncio.run(
        gather_facts(
            [{"poi_id": str(i), "name": f"甲{i}", "must_visit": True} for i in range(6)],
            city="北京",
            day="2026-11-02",
            api_key="test",
            post=post,
            clock=lambda: clock["t"],
        )
    )
    assert starts == [0.0, 0.0]


def test_stage_timeout_cancels_inflight(monkeypatch):
    from planner.travel_search import SearchBudget, gather_facts

    class Fast(SearchBudget):
        def __init__(self, *, clock, limit=6, window=30.0, max_timeout=8.0):
            super().__init__(clock=clock, limit=limit, window=0.05, max_timeout=max_timeout)

    monkeypatch.setattr("planner.travel_search.SearchBudget", Fast)
    started = []

    async def post(query, timeout):
        started.append(1)
        await asyncio.sleep(2)
        return {"results": []}

    async def run():
        started_at = asyncio.get_running_loop().time()
        await gather_facts(
            [{"poi_id": str(i), "name": f"甲{i}"} for i in range(6)],
            city="北京",
            day="2026-11-02",
            api_key="test",
            post=post,
        )
        return asyncio.get_running_loop().time() - started_at

    assert asyncio.run(run()) < 1
    assert len(started) <= 2


def test_pick_matches_official_poi_name():
    from planner.map_fallback import _pick

    def hit(name, place_type="风景名胜;风景名胜;风景名胜"):
        return {"poi_id": name, "name": name, "place_type": place_type}

    assert _pick(
        [hit("故宫博物院"), hit("紫禁甜坊(故宫店)", "餐饮服务"), hit("故宫博物院-午门")],
        "故宫（紫禁城）",
    )["name"] == "故宫博物院"
    assert _pick(
        [hit("八达岭长城"), hit("八达岭长城直通车", "交通设施服务"), hit("八达岭水关长城")],
        "长城（八达岭",
    )["name"] == "八达岭长城"
    assert _pick(
        [hit("慕田峪长城"), hit("慕田峪", "地名地址"), hit("慕田峪环岛")],
        "慕田峪）",
    )["name"] == "慕田峪长城"
    assert _pick(
        [
            hit("天坛公园", "风景名胜;公园广场;公园"),
            hit("天坛东门(地铁站)", "交通设施服务;地铁站"),
            hit("天坛(公交站)", "交通设施服务;公交车站"),
        ],
        "天坛",
    )["name"] == "天坛公园"
    assert _pick(
        [hit("圆明园遗址公园"), hit("圆明园(地铁站)", "交通设施服务;地铁站")],
        "圆明园",
    )["name"] == "圆明园遗址公园"
    assert _pick(
        [hit("什刹海"), hit("什刹海(地铁站)", "交通设施服务;地铁站"), hit("什刹海")],
        "什刹海",
    )["name"] == "什刹海"
    assert _pick([hit("颐和园"), hit("颐和园博物馆"), hit("颐和园-佛香阁")], "颐和园")["name"] == "颐和园"
    assert _pick([hit("甲"), hit("乙")], "丙") is None
    assert _pick(
        [hit("东方明珠广播电视塔"), hit("东方明珠塔售票处", "生活服务;售票处")],
        "东方明珠塔",
    )["name"] == "东方明珠广播电视塔"


def test_pick_unclosed_bracket_uses_location_qualifier():
    """「长城（八达岭」没写完，但「八达岭」是地点线索，不能整段丢掉变成泛化的「长城」。"""
    from planner.map_fallback import _pick

    def hit(name, place_type="风景名胜;风景名胜;风景名胜"):
        return {"poi_id": name, "name": name, "place_type": place_type}

    candidates = [
        hit("八达岭长城"),
        hit("八达岭长城直通车", "交通设施服务"),
        hit("八达岭水关长城"),
    ]
    assert _pick(candidates, "长城（八达岭")["name"] == "八达岭长城"
    # 地点线索与景点类型紧密拼接的胜出，不依赖高德返回顺序：交换候选顺序结果不变。
    assert _pick(list(reversed(candidates)), "长城（八达岭")["name"] == "八达岭长城"
    # 不带地点线索时仍是两个同分景点，保持「同分不猜」的歧义保护。
    assert _pick(candidates, "长城") is None


def test_pick_identical_duplicate_is_not_ambiguous():
    """同名重复条目是同一个 POI，取第一个不算用搜索顺序决定地点。"""
    from planner.map_fallback import _pick

    def hit(name, place_type="风景名胜;风景名胜;风景名胜"):
        return {"poi_id": name, "name": name, "place_type": place_type}

    assert _pick(
        [hit("什刹海"), hit("什刹海(地铁站)", "交通设施服务;地铁站"), hit("什刹海")],
        "什刹海",
    )["name"] == "什刹海"


def test_musts_do_not_block_filler_sights(monkeypatch):
    import json
    import urllib.parse

    from planner.map_fallback import collect_candidates

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(req, timeout=8):
        keywords = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["keywords"][0]
        if keywords == "景点":
            pois = [
                {"id": str(i), "name": f"补{i}", "location": f"121.{i},31.2", "type": "风景名胜", "address": ""}
                for i in range(1, 7)
            ]
        else:
            pois = [{"id": "bund", "name": "外滩", "location": "121.49,31.23", "type": "风景名胜", "address": ""}]
        return Resp({"status": "1", "infocode": "10000", "pois": pois})

    _stay, sights, unlocated = collect_candidates(
        opener, city="上海", days=3, lodging=None, must_visit=["外滩"]
    )
    names = [poi["name"] for poi in sights]
    assert "外滩" in names and any(not poi.get("must_visit") for poi in sights)
    assert unlocated == []


def test_three_day_trip_uses_every_day():
    pois = [_poi(str(i), f"甲{i}", 116.41 + i * 0.01, 39.91, visit_minutes=60) for i in range(6)]
    plan = schedule(
        pois,
        {"date": "2026-11-07", "days": 3, "lodging_poi": _stay(), "transport_mode": "public_transit"},
        lambda *_args: 10 * 60,
    )
    assert _names(plan, 1) and _names(plan, 2) and _names(plan, 3)


def test_search_retries_when_amap_qps_limited(monkeypatch):
    import json

    from planner.map_fallback import _search

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    monkeypatch.setattr("planner.map_fallback.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(_req, timeout=8):
        calls["n"] += 1
        if calls["n"] == 1:
            return Resp({"status": "0", "infocode": "10021", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT"})
        return Resp(
            {
                "status": "1",
                "infocode": "10000",
                "pois": [{"id": "1", "name": "颐和园", "location": "116.27,39.99", "type": "风景名胜", "address": ""}],
            }
        )

    found = _search(opener, city="北京", keywords="颐和园", types="", limit=3)
    assert calls["n"] == 2
    assert found[0]["name"] == "颐和园"


def test_candidate_query_stops_at_deadline():
    from planner.map_fallback import collect_candidates

    def opener(req, timeout=8):
        raise AssertionError("should not search")

    _stay, sights, unlocated = collect_candidates(
        opener,
        city="北京",
        days=1,
        lodging="酒店",
        must_visit=["甲"],
        hints=[{"name": "乙"}],
        deadline=0,
        clock=lambda: 0,
    )
    assert sights == []
    assert [item["name"] for item in unlocated] == ["乙", "甲"]
    assert [item["kind"] for item in unlocated] == ["sight", "must_visit"]


def test_route_budget_is_shared(monkeypatch):
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    calls = {"n": 0}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "1", "route": {"paths": [{"duration": "12"}]}}

    class Client:
        async def get(self, *_a, **_k):
            calls["n"] += 1
            return Resp()

        async def aclose(self):
            return None

    plan = {
        "days": [
            {
                "date": "2026-11-02",
                "items": [
                    {"poi_id": "a", "lng": 1, "lat": 2},
                    {"poi_id": "b", "lng": 3, "lat": 4},
                    {"poi_id": "c", "lng": 5, "lat": 6},
                ],
                "legs": [],
            }
        ]
    }
    budget = SearchBudget(clock=lambda: 0.0, limit=1, window=60, max_timeout=6)
    table: dict = {}
    client = Client()

    async def run():
        await fill_routes(plan, table, city="北京", transport_mode="walking", client=client, clock=lambda: 0.0, budget=budget)
        await fill_routes(plan, table, city="北京", transport_mode="walking", client=client, clock=lambda: 0.0, budget=budget)

    asyncio.run(run())
    assert calls["n"] == 1
    assert budget.used == 1


def test_context_lists_evidence_links():
    from planner.map_fallback import _context

    text = _context(
        {"days": [], "notes": []},
        [],
        "partial",
        None,
        [{"source_url": "https://www.dpm.org.cn/a"}, {"source_url": "https://www.dpm.org.cn/a"}],
    )
    assert "https://www.dpm.org.cn/a" in text


def test_unlocated_must_blocks_checked_status():
    yi = _poi("yi", "乙", 116.42, 39.91, visit_minutes=60)
    plan = schedule(
        [yi],
        {
            "date": "2026-11-03",
            "days": 1,
            "lodging_poi": _stay(),
            "must_visit": ["甲博物馆", "乙"],
            "facts": [{"poi_id": "yi", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"}],
        },
        lambda *_args: 10 * 60,
    )
    assert "乙" in _names(plan, 1)
    assert any(item["name"] == "甲博物馆" and item["reason"] == "未能定位" for item in plan["conflicts"])
    assert plan["schedule_status"] == "conflict"


def test_locked_day_keeps_near_must_when_far_one_does_not_fit():
    near = _poi("a", "甲", 116.41, 39.91, must_visit=True, visit_minutes=60)
    far = _poi("b", "乙", 117.2, 39.90, must_visit=True, visit_minutes=60)

    def duration_fn(origin, dest, mode, day, depart):
        if "b" in (origin, dest):
            return 4 * 60 * 60
        return 20 * 60

    plan = schedule(
        [near, far],
        {
            "date": "2026-11-03",
            "days": 1,
            "lodging_poi": _stay(),
            "must_visit": ["甲", "乙"],
            "transport_mode": "public_transit",
        },
        duration_fn,
        assignment=[["a", "b"]],
    )
    assert _names(plan, 1) == ["甲"]
    assert any(item["name"] == "乙" and item["reason"] == "已知耗时放不下" for item in plan["conflicts"])
    assert not any(item["name"] == "甲" for item in plan["conflicts"])


def test_known_duration_failure_is_conflict_not_unverified():
    poi = _poi("jia", "甲", 116.41, 39.91, must_visit=True, visit_minutes=150)
    constraints = {
        "date": "2026-11-03",
        "days": 1,
        "lodging_poi": _stay(),
        "must_visit": ["甲"],
        "transport_mode": "public_transit",
        "facts": [{"poi_id": "jia", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"}],
    }
    draft = schedule([poi], constraints, lambda *_args: None)
    assert "甲" in _names(draft, 1)

    def slow(origin, dest, mode, day, depart):
        return None if mode == "walking" else 3 * 60 * 60

    final = schedule(
        [poi],
        constraints,
        slow,
        assignment=draft["assignment"],
        kept_dropped=draft["dropped"],
        kept_conflicts=draft["conflicts"],
    )
    assert "甲" not in _names(final, 1)
    assert any(item["name"] == "甲" and item["reason"] == "已知耗时放不下" for item in final["conflicts"])
    assert final["schedule_status"] == "conflict"
    assert all("到达时间未核实" not in note for note in final["notes"])


def test_known_route_beats_unknown_reverse():
    pois = [
        _poi("jia", "甲", 116.50, 39.90, visit_minutes=60),
        _poi("yi", "乙", 116.41, 39.95, visit_minutes=60),
    ]
    known = {("hotel", "jia"), ("jia", "yi"), ("yi", "hotel")}

    def duration_fn(origin, dest, mode, day, depart):
        if mode == "walking":
            return 30 * 60
        if (origin, dest) in known:
            return 10 * 60
        return None

    plan = schedule(
        pois,
        {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "transport_mode": "public_transit"},
        duration_fn,
    )
    assert _names(plan, 1) == ["甲", "乙"]
    assert all(leg["duration_seconds"] == 10 * 60 for leg in plan["days"][0]["legs"])


def test_truncated_and_closed_wording():
    from planner.travel_search import parse_evidence

    hosts = frozenset({"museum.example"})
    url = "https://museum.example/hours"

    def facts(summary):
        return parse_evidence(summary, url=url, poi_name="甲博物馆", poi_id="jia", allowed_hosts=hosts)

    cut = facts("甲博物馆每周一闭馆，...")
    assert cut and cut[0]["kind"] == "closed_weekday" and cut[0]["status"] == "pending"
    assert facts("甲博物馆每周一闭馆，")[0]["status"] == "pending"
    closed = facts("甲博物馆每周一不开放。")
    assert closed and closed[0]["kind"] == "closed_weekday" and closed[0]["status"] == "hard"
    assert facts("甲博物馆周一不闭馆。") == []


def test_split_hours_stay_one_hard_fact():
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "馆开放时间09:00—12:00、13:30—17:00。",
        url="https://www.dpm.org.cn/hours",
        poi_name="馆",
        poi_id="m",
    )
    hours = [fact for fact in facts if fact["kind"] == "opening_hours"]
    assert len(hours) == 1
    assert hours[0]["status"] == "hard"
    assert hours[0]["value"] == [[9 * 60, 12 * 60], [13 * 60 + 30, 17 * 60]]
    poi = _poi("m", "馆", 116.41, 39.91, visit_minutes=90)
    plan = schedule(
        [poi],
        {"date": "2026-11-03", "days": 1, "lodging_poi": _stay(), "facts": hours},
        lambda origin, dest, mode, day, depart: 150 * 60 if origin == "hotel" else 20 * 60,
    )
    assert _when(plan, 1, "馆") == ("13:30", "15:00")


def test_empty_itinerary_keeps_conflict_text():
    from planner.summary import map_summary_block

    poi = _poi("jia", "甲博物馆", 116.41, 39.91, must_visit=True, visit_minutes=90, place_type="博物馆")
    plan = schedule(
        [poi],
        {
            "date": "2026-11-02",
            "days": 1,
            "lodging_poi": _stay(),
            "must_visit": ["甲博物馆"],
            "facts": [{"poi_id": "jia", "kind": "closed_weekday", "value": 0, "status": "hard"}],
        },
        lambda *_args: 10 * 60,
    )
    # 当天闭馆排不出景点，但日期必须留在行程里：空天加冲突说明，而不是把一整天删掉。
    assert len(plan["days"]) == 1
    assert plan["days"][0]["date"] == "2026-11-02"
    assert not any(item.get("role") == "visit" for item in plan["days"][0]["items"])
    text = itinerary_markdown(plan)
    assert "甲博物馆" in text and "无法安排" in text
    shown = map_summary_block("incomplete", text, plan_ready=True)
    assert "甲博物馆" in shown
    assert "没有生成" not in shown
    missed = schedule([], {"date": "2026-11-03", "days": 1, "must_visit": ["甲博物馆"]}, lambda *_args: None)
    assert any(item["name"] == "甲博物馆" and item["reason"] == "未能定位" for item in missed["conflicts"])
    assert missed["schedule_status"] == "conflict"


def test_weekday_hours_and_non_venue_hours_stay_scoped():
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "甲博物馆开放时间工作日09:00—17:00，周末09:00—20:00。",
        url="https://www.dpm.org.cn/hours",
        poi_name="甲博物馆",
        poi_id="jia",
    )
    hours = [fact for fact in facts if fact["kind"] == "opening_hours"]
    assert len(hours) == 2 and all(fact["status"] == "hard" for fact in hours)
    by_week = {tuple(fact["weekdays"]): fact["value"] for fact in hours}
    assert by_week[(0, 1, 2, 3, 4)] == [[9 * 60, 17 * 60]]
    assert by_week[(5, 6)] == [[9 * 60, 20 * 60]]
    phone = parse_evidence(
        "甲博物馆电话咨询服务时间09:00—17:00。",
        url="https://www.dpm.org.cn/hours",
        poi_name="甲博物馆",
        poi_id="jia",
    )
    assert phone and all(fact["status"] == "pending" for fact in phone)
    poi = _poi("jia", "甲博物馆", 116.41, 39.91, visit_minutes=60)
    plan = schedule(
        [poi],
        {"date": "2026-11-07", "days": 1, "lodging_poi": _stay(), "facts": hours},
        lambda *_args: 10 * 60,
    )
    assert date(2026, 11, 7).weekday() == 5
    assert _when(plan, 1, "甲博物馆")[0] == "09:10"


def test_unknown_leg_does_not_hide_known_overtime():
    pois = [
        _poi("a", "甲", 116.41, 39.91, must_visit=True, visit_minutes=60),
        _poi("b", "乙", 116.45, 39.93, must_visit=True, visit_minutes=60),
    ]

    def duration_fn(origin, dest, mode, day, depart):
        if mode == "walking":
            return 30 * 60
        if (origin, dest) == ("hotel", "a"):
            return None
        return 8 * 60 * 60

    plan = schedule(
        pois,
        {
            "date": "2026-11-03",
            "days": 1,
            "lodging_poi": _stay(),
            "must_visit": ["甲", "乙"],
            "transport_mode": "public_transit",
        },
        duration_fn,
    )
    assert _names(plan, 1) == []
    assert {item["name"] for item in plan["conflicts"]} >= {"甲", "乙"}
    assert plan["schedule_status"] == "conflict"


def test_alias_must_is_not_unlocated():
    poi = _poi("dpm", "故宫博物院", 116.397, 39.916, must_visit=True, visit_minutes=60, asked_name="故宫")
    plan = schedule(
        [poi],
        {
            "date": "2026-11-03",
            "days": 1,
            "lodging_poi": _stay(),
            "must_visit": ["故宫"],
            "facts": [{"poi_id": "dpm", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"}],
        },
        lambda *_args: 10 * 60,
    )
    assert "故宫博物院" in _names(plan, 1)
    assert not any(item["name"] == "故宫" for item in plan["conflicts"])
    assert plan["schedule_status"] == "checked_under_assumptions"


def test_two_closed_weekdays_both_stay_hard():
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "甲博物馆每周一闭馆，每周二闭馆。",
        url="https://www.dpm.org.cn/hours",
        poi_name="甲博物馆",
        poi_id="jia",
    )
    closed = [fact for fact in facts if fact["kind"] == "closed_weekday"]
    assert {fact["value"] for fact in closed} == {0, 1}
    assert all(fact["status"] == "hard" for fact in closed)


def test_slack_applies_without_lodging():
    """甲 150 分钟后接乙 310 分钟，无住宿时会排到 17:55，超过 30 分钟机动。"""
    pois = [
        _poi("a", "甲", 116.41, 39.91, must_visit=True, visit_minutes=150),
        _poi("b", "乙", 116.42, 39.92, must_visit=True, visit_minutes=310),
    ]
    plan = schedule(
        pois,
        {
            "date": "2026-11-03",
            "days": 1,
            "must_visit": ["甲", "乙"],
            "facts": [
                {"poi_id": "a", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"},
                {"poi_id": "b", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"},
            ],
        },
        lambda *_args: 0,
    )
    assert "乙" not in _names(plan, 1)
    assert any(item["name"] == "乙" for item in plan["conflicts"])
    assert plan["schedule_status"] == "conflict"
    for day in plan["days"]:
        for item in day["items"]:
            if item.get("end"):
                assert item["end"] <= "17:30"
    kept = schedule(
        [_poi("a", "甲", 116.41, 39.91, visit_minutes=60)],
        {
            "date": "2026-11-03",
            "days": 1,
            "facts": [{"poi_id": "a", "kind": "opening_hours", "value": [[9 * 60, 18 * 60]], "status": "hard"}],
        },
        lambda *_args: 10 * 60,
    )
    assert _when(kept, 1, "甲")[1] <= "17:30"
    assert kept["schedule_status"] == "checked_under_assumptions"


_STAT_KEYS = {
    "assemble_ms",
    "poi_verify_ms",
    "facts_ms",
    "route_ms",
    "schedule_ms",
    "poi_requests",
    "route_requests",
    "amap_rate_limits",
}


def _without_clock(value):
    if isinstance(value, list):
        return [_without_clock(item) for item in value]
    if isinstance(value, dict):
        return {key: _without_clock(item) for key, item in value.items() if key != "retrieved_at"}
    return value


def _contains_stat(value) -> bool:
    if isinstance(value, dict):
        if _STAT_KEYS.intersection(value):
            return True
        return any(_contains_stat(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_stat(item) for item in value)
    if isinstance(value, str):
        return any(key in value for key in _STAT_KEYS)
    return False


def test_fixed_responses_keep_the_same_itinerary(monkeypatch):
    """同一组高德和 Tavily 响应连跑两次，业务结果相同。计时不进入行程。"""
    import httpx

    from planner.map_fallback import assemble_itinerary
    from planner.quality import build_quality, leg_counts, opening_counts, schedule_check
    from planner.travel_search import gather_facts

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    monkeypatch.setenv("TAVILY_API_KEY", "test")

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    class Payload:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    def pois(rows):
        return {"status": "1", "infocode": "10000", "pois": rows}

    def row(poi_id, name, lng, lat, place_type):
        return {
            "id": poi_id,
            "name": name,
            "location": f"{lng},{lat}",
            "type": place_type,
            "address": name,
        }

    async def post(_query, _timeout):
        return {
            "results": [
                {
                    "url": "https://www.dpm.org.cn/hours",
                    "title": "开放时间",
                    "content": "甲景区遗址公园开放时间09:00-17:00。乙公园开放时间09:00-17:00。丙园开放时间09:00-17:00。",
                }
            ]
        }

    async def wrapped(candidates, **kwargs):
        kwargs["post"] = post
        return await gather_facts(candidates, **kwargs)

    monkeypatch.setattr("planner.travel_search.gather_facts", wrapped)

    async def once():
        calls = {"place": [], "routes": 0}

        class Client:
            async def get(self, url, params=None, timeout=None):
                if "place/text" in str(url):
                    keywords = params["keywords"]
                    calls["place"].append((keywords, params["page_size"], params["types"]))
                    hotel_tries = sum(1 for item in calls["place"] if item[0] == "酒店")
                    if keywords == "酒店" and hotel_tries == 1:
                        return Payload({"status": "0", "infocode": "10021", "pois": []})
                    table = {
                        "酒店": [row("hotel", "如家酒店", "116.40", "39.90", "住宿服务")],
                        "甲景区": [
                            row("a", "甲景区", "116.41", "39.91", "商务住宅"),
                            row("b", "甲景区遗址公园", "116.42", "39.92", "风景名胜;公园"),
                        ],
                        "乙公园": [row("yi", "乙公园", "116.43", "39.93", "风景名胜")],
                        "景点": [row("bing", "丙园", "116.44", "39.94", "风景名胜")],
                    }
                    return Payload(pois(table.get(keywords, [])))
                calls["routes"] += 1
                return Payload({"status": "1", "infocode": "10000", "route": {"paths": [{"duration": "600"}]}})

            async def aclose(self):
                return None

        monkeypatch.setattr(httpx, "AsyncClient", lambda *_args, **_kwargs: Client())
        built = await assemble_itinerary(
            city="北京",
            days=1,
            date="2026-11-04",
            lodging="酒店",
            transport_mode="walking",
            must_visit=["甲景区"],
            avoid=[],
            hints=[
                {"name": "甲景区", "visit_minutes": 60},
                {"name": "乙公园", "visit_minutes": 90},
            ],
            query_text="北京一天",
        )
        built["calls"] = calls
        return built

    def business(built):
        stay = built["constraints"]["lodging_poi"]
        openings, opening_total = opening_counts(built["plan"], built["facts"], built["unlocated"])
        legs_ref, legs, leg_total = leg_counts(built["plan"])
        checked = schedule_check(built["plan"], built["unlocated"])
        quality = build_quality(
            summary_degraded=False,
            ticket_applicable=False,
            ticket_facts=[],
            ticket_reused=False,
            opening_required=opening_total,
            opening_verified=openings,
            legs_required=leg_total,
            legs_verified=legs,
            legs_referenced=legs_ref,
            schedule_check=checked,
            budget_status="not_required",
        )
        return {
            "stay": {key: stay[key] for key in ("poi_id", "name", "lng", "lat")},
            "candidates": [
                {
                    "poi_id": poi["poi_id"],
                    "name": poi["name"],
                    "lng": poi["lng"],
                    "lat": poi["lat"],
                    "must_visit": bool(poi.get("must_visit")),
                    "visit_minutes": poi.get("visit_minutes"),
                }
                for poi in built["candidates"]
            ],
            "unlocated": built["unlocated"],
            "plan": _without_clock(built["plan"]),
            "tasks": built["tasks"],
            "facts_status": built["facts_status"],
            "facts": _without_clock(built["facts"]),
            "route_error": built["route_error"],
            "quality": quality,
            "place_calls": built["calls"]["place"],
            "route_requests": built["calls"]["routes"],
        }

    left = asyncio.run(once())
    right = asyncio.run(once())
    assert business(left) == business(right)
    pinned = business(left)
    for key in ("plan", "constraints", "context", "tasks"):
        assert not _contains_stat(left[key])
    stats = left["stats"]
    assert set(_STAT_KEYS) <= set(stats)
    assert all(isinstance(stats[key], int) for key in _STAT_KEYS)
    assert stats["assemble_ms"] >= max(stats["poi_verify_ms"], stats["facts_ms"], stats["route_ms"], stats["schedule_ms"])
    assert stats["poi_requests"] == 5
    assert stats["amap_rate_limits"] == 1
    assert stats["route_requests"] == pinned["route_requests"]
    assert pinned["place_calls"] == [
        ("酒店", "3", ""),
        ("酒店", "3", ""),
        ("甲景区", "3", ""),
        ("乙公园", "3", ""),
        # 泛搜 page_size 来自 candidate_retrieval_target：单日 balanced 节奏预算为 4，
        # 不是必去项数量，也不是旧的固定 _PER_DAY=3。详见 test_density.py 的纯函数契约。
        ("景点", "4", "110000"),
    ]
    assert pinned["stay"] == {"poi_id": "hotel", "name": "酒店", "lng": 116.4, "lat": 39.9}
    assert pinned["candidates"] == [
        {"poi_id": "b", "name": "甲景区遗址公园", "lng": 116.42, "lat": 39.92, "must_visit": True, "visit_minutes": 60},
        {"poi_id": "yi", "name": "乙公园", "lng": 116.43, "lat": 39.93, "must_visit": False, "visit_minutes": 90},
        {"poi_id": "bing", "name": "丙园", "lng": 116.44, "lat": 39.94, "must_visit": False, "visit_minutes": None},
    ]
    assert pinned["unlocated"] == []
    assert pinned["facts_status"] == "partial"
    assert pinned["route_error"] is None
    assert pinned["plan"]["conflicts"] == []
    assert pinned["plan"]["dropped"] == []
    assert pinned["plan"]["schedule_status"] == "candidate"
    assert pinned["quality"]["checks"]["schedule"] == "unverified"
    assert pinned["quality"]["checks"] == {
        "ticket": "not_required",
        "opening_rules": "unverified",
        "travel_time": "unverified",
        "schedule": "unverified",
        "budget": "not_required",
    }
    visits = [
        [(item["name"], item.get("start"), item.get("end")) for item in day["items"] if item.get("role") == "visit"]
        for day in pinned["plan"]["days"]
    ]
    durations = [
        [leg.get("duration_seconds") for leg in day.get("legs") or []]
        for day in pinned["plan"]["days"]
    ]
    # 密度规则把「风景名胜」按半日主活动估算 240 分钟（planner/route_plan.py::visit_minutes，
    # evals/test_density.py 固化该契约）。三个景点里丙园未给显式时长，按 240 分钟占位后，
    # 当天可用窗口放不下全部活动，排程如实降级为 unverified：首项保留可行窗口，其后的
    # 起止时刻与未知路段一律为 None，绝不退回 09:00 编造精确时间（AGENTS.md 的未知状态约束）。
    # 这组期望对齐的是当前有意设计，不是旧版 90 分钟估算；旧断言写于密度规则之前、从未通过。
    assert visits == [[("丙园", "13:00", "17:00"), ("乙公园", None, None), ("甲景区遗址公园", None, None)]]
    assert durations == [[600, None, None, None]]
    assert [task["text"] for task in pinned["tasks"] if task["type"] == "poi"] == ["酒店", "丙园", "乙公园", "甲景区遗址公园", "酒店"]
    # 反编造护栏：当天必须标 unverified；未知路段与后续景点不提供精确时刻。
    assert all(day.get("unverified") for day in pinned["plan"]["days"])
    assert all(leg is None for leg in durations[0][1:])
    assert all(
        start is None and end is None
        for _name, start, end in visits[0][1:]
    )


def test_route_rate_limit_is_counted_without_retry(monkeypatch):
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")

    class Payload:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "0", "infocode": "10021"}

    class Client:
        async def get(self, *_args, **_kwargs):
            return Payload()

    plan = {
        "days": [
            {
                "date": "2026-11-04",
                "items": [
                    {"poi_id": "a", "lng": 1, "lat": 2},
                    {"poi_id": "b", "lng": 3, "lat": 4},
                ],
                "legs": [],
            }
        ]
    }
    table: dict = {}
    stats = {"poi_requests": 0, "route_requests": 0, "amap_rate_limits": 0}
    budget = SearchBudget(clock=lambda: 0.0, limit=5, window=60, max_timeout=6)

    async def run():
        await fill_routes(
            plan,
            table,
            city="北京",
            transport_mode="walking",
            client=Client(),
            clock=lambda: 0.0,
            budget=budget,
            stats=stats,
        )

    asyncio.run(run())
    assert stats["route_requests"] == 1
    assert stats["amap_rate_limits"] == 1
    assert list(table.values()) == [None]


def test_route_rate_limit_stops_further_segments(monkeypatch):
    """限流不是单条路段的偶发失败：第一条命中后不该继续为后面的路段发请求。"""
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")

    class Payload:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "0", "infocode": "10021"}

    class Client:
        def __init__(self):
            self.calls = 0

        async def get(self, *_args, **_kwargs):
            self.calls += 1
            return Payload()

    # 一天三个景点 → 至少两条相邻段，每条还有步行/换乘两种模式。
    plan = {
        "days": [
            {
                "date": "2026-11-04",
                "items": [
                    {"poi_id": "a", "lng": 1, "lat": 2},
                    {"poi_id": "b", "lng": 3, "lat": 4},
                    {"poi_id": "c", "lng": 5, "lat": 6},
                ],
                "legs": [],
            }
        ]
    }
    table: dict = {}
    stats = {"poi_requests": 0, "route_requests": 0, "amap_rate_limits": 0}
    budget = SearchBudget(clock=lambda: 0.0, limit=50, window=600, max_timeout=6)
    client = Client()

    async def run():
        return await fill_routes(
            plan,
            table,
            city="北京",
            transport_mode="walking",
            client=client,
            clock=lambda: 0.0,
            budget=budget,
            stats=stats,
        )

    error = asyncio.run(run())
    assert error == "rate_limit"
    # 第一条路段就限流，后面的路段一条都不许再发。
    assert client.calls == 1
    assert stats["route_requests"] == 1
    assert budget.closed is True


def _rail(*, outbound=None, inbound=None):    return {
        "outbound": outbound or {"state": "not_constrained", "basis": None, "reason": ""},
        "inbound": inbound or {"state": "not_constrained", "basis": None, "reason": ""},
        "assumptions": [],
        "unverified": False,
    }


def _known_outbound(hhmm, arrive_date="2026-11-02"):
    hour, minute = hhmm.split(":")
    return {
        "state": "known",
        "basis": {"train_no": "G1"},
        "arrive_date": arrive_date,
        "arrive": hhmm,
        "earliest_start": int(hour) * 60 + int(minute) + 40,
        "reason": f"按 {arrive_date} {hhmm} 抵达加 40 分钟接驳",
    }


def test_unknown_arrival_keeps_start_unknown():
    window = day_window(
        date(2026, 11, 2),
        {"date": "2026-11-02", "days": 1, "rail_window": _rail(outbound={"state": "unknown", "basis": {"train_no": "G1"}, "reason": "缺抵达时刻"})},
    )
    assert window["state"] == "unknown"
    assert window["start"] is None


def test_known_arrival_constrains_first_day():
    window = day_window(
        date(2026, 11, 2),
        {"date": "2026-11-02", "days": 1, "rail_window": _rail(outbound=_known_outbound("15:27"))},
    )
    assert window["state"] == "constrained"
    assert window["start"] == 16 * 60 + 7


def test_arrival_on_later_date_makes_day_unavailable():
    window = day_window(
        date(2026, 11, 2),
        {"date": "2026-11-02", "days": 1, "rail_window": _rail(outbound=_known_outbound("21:14", arrive_date="2026-11-03"))},
    )
    assert window["state"] == "unavailable" and window["start"] is None


def test_not_constrained_inbound_keeps_default_end():
    window = day_window(
        date(2026, 11, 4),
        {"date": "2026-11-04", "days": 3, "rail_window": _rail()},
    )
    assert window["state"] == "default" and window["end"] == 18 * 60


def test_cross_day_arrival_constrains_arrival_day_not_first_day():
    """P1-5 回归：11/7 出发、11/8 15:27 抵达——7 日不可游，8 日从抵达时刻起收窄。"""
    outbound = _known_outbound("15:27", arrive_date="2026-11-08")
    window_7 = day_window(
        date(2026, 11, 7),
        {"date": "2026-11-07", "days": 2, "rail_window": _rail(outbound=outbound)},
    )
    assert window_7["state"] == "unavailable"
    assert window_7["start"] is None

    window_8 = day_window(
        date(2026, 11, 8),
        {"date": "2026-11-07", "days": 2, "rail_window": _rail(outbound=outbound)},
    )
    # 抵达当天从 16:07（15:27 + 40 分钟接驳）起收窄，而不是默认 09:00。
    assert window_8["state"] == "constrained"
    assert window_8["start"] == 16 * 60 + 7


def test_cross_day_return_constrains_departure_day():
    """P1-5 回归：返程 11/8 发车——8 日收窄，发车之后的日期不可游。"""
    inbound = {
        "state": "known",
        "basis": {"train_no": "G2"},
        "depart_date": "2026-11-08",
        "depart": "18:00",
        "latest_end": 16 * 60 + 50,
        "reason": "按 18:00 发车",
    }
    window_8 = day_window(
        date(2026, 11, 8),
        {"date": "2026-11-07", "days": 2, "rail_window": _rail(inbound=inbound)},
    )
    assert window_8["state"] == "constrained"
    assert window_8["end"] == 16 * 60 + 50


def test_missing_date_day_window_is_unknown_not_fake_first_day():
    """出行日期缺失时没有首末日可比：窗口必须 unknown，且标出 date_unknown。"""
    window = day_window(None, {"days": 2})
    assert window["state"] == "unknown"
    assert window["start"] is None and window["end"] is None
    assert window["date_unknown"] is True
    # state 仍只取四值，不新增第五种。
    assert window["state"] in {"default", "constrained", "unknown", "unavailable"}


def test_known_date_day_window_reports_date_unknown_false():
    window = day_window(date(2026, 11, 4), {"date": "2026-11-04", "days": 1})
    assert window["date_unknown"] is False
    assert window["state"] == "default"


def test_missing_date_schedule_keeps_days_without_fake_date():
    """缺 date 不抛异常，也不回退 2026-01-05：天数保留、日期为 None。"""
    poi = _poi("jia", "甲", 116.41, 39.91, visit_minutes=90)
    plan = schedule([poi], {"days": 2, "lodging_poi": _stay(), "facts": []}, _table(slow=()))
    assert plan["schedule_status"] == "date_unknown"
    assert [day["day"] for day in plan["days"]] == [1, 2]
    assert [day["date"] for day in plan["days"]] == [None, None]
    assert all(day["skip"] is True for day in plan["days"])
    assert all(day["window"]["state"] == "unknown" for day in plan["days"])
    # 未排程要写清原因是「日期未确定」，不能记成关闭或必去冲突。
    assert plan["conflicts"] == []
    assert all("日期未确定" in note for note in plan["notes"] if note.startswith("未安排"))
    markdown = itinerary_markdown(plan)
    assert "2026-01-05" not in markdown
    assert "日期待定" in markdown


def test_missing_date_keeps_candidate_supply_keys_unchanged():
    """retrieval_target 键名是下游契约，日期未知时值可以是 None，但不能改名。"""
    poi = _poi("jia", "甲", 116.41, 39.91, visit_minutes=90)
    plan = schedule([poi], {"days": 1, "facts": []}, _table(slow=()))
    supply = plan["candidate_supply"]
    assert "retrieval_target" in supply and "target" not in supply
    assert supply["retrieval_target"] is None
    assert supply["usable_candidates"] is None
    assert supply["shortage"] is False
    assert supply["date_unknown"] is True


def test_short_window_day_is_arrival_only_and_kept():
    poi = _poi("jia", "甲", 116.41, 39.91, visit_minutes=90)
    plan = schedule(
        [poi],
        {
            "date": "2026-11-02",
            "days": 2,
            "lodging_poi": _stay(),
            "must_visit": ["甲"],
            "rail_window": _rail(outbound=_known_outbound("21:14")),
        },
        _table(slow=()),
    )
    assert [day["day"] for day in plan["days"]] == [1, 2]
    first = plan["days"][0]
    assert first["skip"] is True and first["arrival_only"] is True
    assert first["window"]["state"] == "unavailable"
    assert any("抵离" in note for note in first["notes"])
    assert _names(plan, 2) == ["甲"]
    assert plan["conflicts"] == []
    assert plan["schedule_status"] == "candidate"


def test_must_visit_unplaceable_in_window_still_conflicts():
    poison = _poi("bing", "丙", 116.60, 39.99, must_visit=True, visit_minutes=200)
    plan = schedule(
        [poison],
        {
            "date": "2026-11-02",
            "days": 1,
            "lodging_poi": _stay(),
            "must_visit": ["丙"],
            "rail_window": _rail(outbound=_known_outbound("16:30"), inbound={"state": "known", "basis": {"train_no": "G2"}, "depart_date": "2026-11-02", "depart": "18:00", "latest_end": 16 * 60 + 50, "reason": "按 18:00 发车"}),
        },
        _table(slow=()),
    )
    assert any(item["name"] == "丙" for item in plan["conflicts"])
    assert plan["schedule_status"] == "conflict"


def _opener_by_keyword(table):
    """按 keywords 返回固定高德响应，记录每次查询用到的 keywords。"""

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            import json

            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(req, timeout=8):
        import urllib.parse

        keywords = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["keywords"][0]
        pois = table.get(keywords, [])
        return Resp({"status": "1", "infocode": "10000", "pois": pois})

    return opener


def _amap_poi(poi_id, name, lng, lat, place_type="风景名胜"):
    return {"id": poi_id, "name": name, "location": f"{lng},{lat}", "type": place_type, "address": ""}


def test_area_lodging_is_pending_not_a_sight(monkeypatch):
    from planner.map_fallback import collect_candidates
    from planner.quality import collect_issues, opening_counts

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    opener = _opener_by_keyword(
        {
            "上海站附近": [_amap_poi("sh", "上海站", 121.455, 31.249, "交通设施服务;火车站;火车站")],
            "景点": [_amap_poi("bund", "外滩", 121.49, 31.23)],
        }
    )
    stay, _sights, unlocated = collect_candidates(opener, city="上海", days=3, lodging="上海站附近")
    assert stay is not None
    assert stay["lodging_kind"] == "area" and stay["precision"] == "area"
    assert stay["name"] == "上海站附近" and stay["poi_name"] == "上海站"
    kinds = [(item["kind"], item["reason"]) for item in unlocated]
    assert kinds == [("lodging", "area")]
    # 住宿不冒充景点：不写进「未能定位的景点」，也不进开放规则分母。
    assert opening_counts({"days": []}, [], unlocated) == (0, 0)
    issues = collect_issues(checks={}, unlocated=unlocated)
    assert [item["code"] for item in issues] == ["lodging_area_pending"]
    assert issues[0]["kind"] == "pending" and issues[0]["action"] == "confirm_lodging"


def test_area_lodging_with_scope_choice_is_not_an_issue(monkeypatch):
    from planner.map_fallback import collect_candidates
    from planner.quality import collect_issues

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    opener = _opener_by_keyword(
        {
            "上海站附近": [_amap_poi("sh", "上海站", 121.455, 31.249, "交通设施服务;火车站;火车站")],
            "景点": [],
        }
    )
    _stay, _sights, unlocated = collect_candidates(
        opener, city="上海", days=3, lodging="上海站附近", lodging_scope="area"
    )
    assert unlocated == []
    assert collect_issues(checks={}, unlocated=unlocated) == []


def test_specific_hotel_must_match_identity(monkeypatch):
    from planner.map_fallback import collect_candidates

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    opener = _opener_by_keyword(
        {
            "如家酒店": [_amap_poi("rujia", "如家酒店(上海站店)", 121.455, 31.25, "住宿服务;宾馆酒店;宾馆酒店")],
            "景点": [],
            "上海中心大厦": [_amap_poi("tower", "上海中心大厦", 121.5, 31.23)],
        }
    )
    stay, _sights, unlocated = collect_candidates(opener, city="上海", days=2, lodging="如家酒店")
    assert stay["lodging_kind"] == "exact" and stay["precision"] == "exact"
    assert unlocated == []

    # 用户点名了具体酒店，但高德只回了别的地点：不能把别的地点当成酒店。
    stay2, _sights2, unlocated2 = collect_candidates(
        opener, city="上海", days=2, lodging="上海中心大厦酒店"
    )
    assert [item["reason"] for item in unlocated2] == ["unresolved"]
    assert stay2 is None or stay2.get("lodging_kind") != "exact"


def test_model_must_flag_is_not_accepted(monkeypatch):
    from planner.map_fallback import collect_candidates
    from planner.route_plan import sanitize_candidates

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    opener = _opener_by_keyword(
        {
            "上海站": [_amap_poi("sh", "上海站", 121.455, 31.249, "交通设施服务;火车站;火车站")],
            "景点": [_amap_poi("bund", "外滩", 121.49, 31.23)],
        }
    )
    _stay, sights, _unlocated = collect_candidates(
        opener,
        city="上海",
        days=3,
        lodging="上海站附近",
        hints=[{"name": "上海站", "must_visit": True, "reason": "可以从上海站开始"}],
    )
    station = [poi for poi in sights if poi["name"] == "上海站"]
    assert station == []
    assert all(not poi.get("must_visit") for poi in sights)

    constraints = {"must_visit": [], "avoid": [], "lodging_poi": None}
    mixed = [_poi("sh", "上海站", 121.455, 31.249, must_visit=True, place_type="交通设施服务;火车站;火车站")]
    assert sanitize_candidates(mixed, constraints) == []


def test_lodging_poi_is_not_a_candidate(monkeypatch):
    from planner.map_fallback import collect_candidates
    from planner.route_plan import sanitize_candidates

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    opener = _opener_by_keyword(
        {
            "外滩": [_amap_poi("bund", "外滩", 121.49, 31.23)],
            "景点": [
                _amap_poi("bund", "外滩", 121.49, 31.23),
                _amap_poi("near", "外滩英迪格酒店", 121.5, 31.24, "住宿服务;宾馆酒店;宾馆酒店"),
            ],
        }
    )
    stay, sights, _unlocated = collect_candidates(opener, city="上海", days=1, lodging="外滩")
    assert stay is not None
    assert all(str(poi.get("poi_id") or "") != str(stay.get("poi_id") or "") for poi in sights)
    # 住宿锚点同一 poi 不能再以「景点」身份进排程；酒店类型地点也不是游览项。
    pool = sanitize_candidates(
        [dict(poi, must_visit=False) for poi in sights],
        {"must_visit": [], "avoid": [], "lodging_poi": stay},
    )
    assert [poi["name"] for poi in pool] == []


def _two_point_plan():
    return {
        "days": [
            {
                "date": "2026-11-04",
                "items": [
                    {"poi_id": "a", "lng": 1, "lat": 2},
                    {"poi_id": "b", "lng": 3, "lat": 4},
                ],
                "legs": [],
            }
        ]
    }


def _route_client(walking):
    """walking 返回 walked；transfer 一律返回 900 秒。"""
    from planner.map_fallback import _ROUTES

    sent = {"walking": 0, "transfer": 0}

    class Client:
        async def get(self, url, params=None, timeout=None):
            mode = "transfer" if "transit" in str(url) else "walking"
            sent[mode] += 1

            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    duration = walking if mode == "walking" else "900"
                    if mode == "transfer":
                        return {"status": "1", "infocode": "10000", "route": {"transits": [{"duration": duration}]}}
                    return {"status": "1", "infocode": "10000", "route": {"paths": [{"duration": duration}]}}

            return Resp()

        async def aclose(self):
            return None

    assert _ROUTES["transfer"].endswith("transit/integrated")
    return Client(), sent


def test_short_walk_skips_transfer(monkeypatch):
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    client, sent = _route_client("600")
    table: dict = {}
    states: dict = {}
    budget = SearchBudget(clock=lambda: 0.0, limit=10, window=60, max_timeout=6)
    error = asyncio.run(
        fill_routes(
            _two_point_plan(),
            table,
            city="北京",
            transport_mode="public_transit",
            client=client,
            clock=lambda: 0.0,
            budget=budget,
            states=states,
        )
    )
    assert error is None
    assert sent == {"walking": 1, "transfer": 0}
    assert "not_queried" in states.values()


def test_no_route_walk_falls_back_to_transfer(monkeypatch):
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    client, sent = _route_client("0")
    table: dict = {}
    states: dict = {}
    budget = SearchBudget(clock=lambda: 0.0, limit=10, window=60, max_timeout=6)
    error = asyncio.run(
        fill_routes(
            _two_point_plan(),
            table,
            city="北京",
            transport_mode="public_transit",
            client=client,
            clock=lambda: 0.0,
            budget=budget,
            states=states,
        )
    )
    assert sent == {"walking": 1, "transfer": 1}
    assert error == "no_route"
    assert "ok" in states.values() and "no_route" in states.values()
    assert 900 in table.values()


def test_failed_walk_does_not_retry_transfer(monkeypatch):
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    client, sent = _route_client("600")

    class Failing:
        async def get(self, url, params=None, timeout=None):
            sent["walking" if "transit" not in str(url) else "transfer"] += 1
            raise RuntimeError("boom")

        async def aclose(self):
            return None

    table: dict = {}
    states: dict = {}
    budget = SearchBudget(clock=lambda: 0.0, limit=10, window=60, max_timeout=6)
    error = asyncio.run(
        fill_routes(
            _two_point_plan(),
            table,
            city="北京",
            transport_mode="public_transit",
            client=Failing(),
            clock=lambda: 0.0,
            budget=budget,
            states=states,
        )
    )
    assert sent == {"walking": 1, "transfer": 0}
    assert error == "request_failed"
    assert "failed" in states.values()


def test_budget_keeps_route_attempts_but_limits_search(monkeypatch):
    from planner.budget import RunBudget

    monkeypatch.setenv("PLAN_TOOL_ATTEMPTS", "6")
    budget = RunBudget(clock=lambda: 0.0)
    budget.set_reserve(2)  # 2 条必要路段 × 2 种模式 = 4 次预留
    assert budget.reserved_attempts() == 4
    assert budget.note_attempt() is True
    assert budget.note_attempt() is True
    # 余量跌进预留区：普通检索停下，但必要路段仍能发。
    assert budget.note_attempt() is False
    assert budget.external_blocked is False
    assert budget.note_attempt(protected=True) is True
    assert budget.attempts_left() == 3


def test_budget_reserves_route_time(monkeypatch):
    from planner.budget import RunBudget

    monkeypatch.setenv("PLAN_BUDGET_SECONDS", "240")
    monkeypatch.setenv("PLAN_CLOSEOUT_SECONDS", "10")
    now = {"t": 0.0}
    budget = RunBudget(clock=lambda: now["t"])
    budget.set_reserve(3)
    assert budget.reserved_seconds() == 36.0
    now["t"] = 200.0  # 只剩 30 秒工作时间
    assert budget.work_left() == 30.0
    assert budget.reserved_seconds() == 30.0
    assert budget.work_left_without_reserve() == 0.0


def test_optional_hints_are_trimmed_before_search():
    from planner.map_fallback import _trim_hints

    hints = [{"name": f"普通{i}"} for i in range(6)] + [{"name": "必去"}]
    keep, dropped = _trim_hints(hints, ["必去"], 1)
    # 每天 3 个普通候选，必去不占额度。
    assert [item["name"] for item in keep] == ["普通0", "普通1", "普通2", "必去"]
    assert [item["name"] for item in dropped] == ["普通3", "普通4", "普通5"]
    assert {item["reason"] for item in dropped} == {"候选过多，未检索"}


def test_trim_hints_deduplicates_and_ignores_model_must_flag():
    """候选自带 must_visit 不能单独放行，只能靠用户 must_visit 点名保留。"""
    from planner.map_fallback import _trim_hints

    hints = [
        {"name": "普通0"},
        {"name": "普通0"},
        {"name": "甲博物馆", "must_visit": True},  # 模型打的标记，不信任
        {"name": "普通1"},
    ]
    keep, _dropped = _trim_hints(hints, [], 1)
    # 去重后，模型标记的「甲博物馆」按普通候选计额度，正好占掉 quota=3 的一个名额。
    assert [item["name"] for item in keep] == ["普通0", "甲博物馆", "普通1"]


def test_trim_hints_excludes_lodging_hint_but_keeps_named_must():
    """住宿/接驳说明被排除，但用户点名必去（哪怕名字像住宿）要保留。"""
    from planner.map_fallback import _trim_hints

    hints = [
        {"name": "如家酒店"},
        {"name": "上海站附近住宿地"},
        {"name": "外滩"},
    ]
    keep, dropped = _trim_hints(hints, ["上海站附近住宿地"], 1, lodging="上海站附近")
    assert [item["name"] for item in keep] == ["上海站附近住宿地", "外滩"]
    assert [item["name"] for item in dropped] == []
    # 「如家酒店」是明确的住宿说明，不进候选。
    assert "如家酒店" not in [item["name"] for item in keep]


def test_route_failure_class_maps_amap_and_http():
    from planner.map_fallback import _route_failure_class

    assert _route_failure_class(429, None) == "rate_limit"
    assert _route_failure_class(None, "10003") == "rate_limit"
    assert _route_failure_class(None, "10021") == "rate_limit"
    assert _route_failure_class(401, None) == "request_failed"
    assert _route_failure_class(403, None) == "request_failed"
    assert _route_failure_class(None, "10001") == "request_failed"
    assert _route_failure_class(None, "10002") == "request_failed"
    assert _route_failure_class(None, "20000") is None


def test_fill_routes_sends_date_and_time_and_classifies_http_error(monkeypatch):
    """路线请求带上 date/time；HTTP 429 归类 rate_limit，其余 raise 归 request_failed。"""
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")

    sent: dict = {}

    class Payload:
        def raise_for_status(self):
            raise RuntimeError("boom")

        def json(self):
            return {}

    class Client:
        def __init__(self, status):
            self.status = status

        async def get(self, url, params=None, timeout=None):
            sent.update(params or {})
            return Payload()

    plan = {
        "days": [
            {
                "date": "2026-11-04",
                "items": [
                    {"poi_id": "a", "lng": 1, "lat": 2},
                    {"poi_id": "b", "lng": 3, "lat": 4},
                ],
                "legs": [],
            }
        ]
    }
    table: dict = {}
    stats = {}
    budget = SearchBudget(clock=lambda: 0.0, limit=5, window=60, max_timeout=6)

    async def run():
        return await fill_routes(
            plan,
            table,
            city="北京",
            transport_mode="walking",
            client=Client(200),
            clock=lambda: 0.0,
            budget=budget,
            stats=stats,
        )

    error = asyncio.run(run())
    assert sent["date"] == "2026-11-04"
    assert sent["time"].startswith("09:")
    assert error == "request_failed"


def test_fill_routes_http_429_is_rate_limit(monkeypatch):
    from planner.map_fallback import fill_routes
    from planner.travel_search import SearchBudget

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")

    class Payload:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            raise RuntimeError("rate limited")

        def json(self):
            return {}

    class Client:
        def __init__(self, status):
            self.status = status

        async def get(self, url, params=None, timeout=None):
            return Payload(429)

    plan = {
        "days": [
            {
                "date": "2026-11-04",
                "items": [
                    {"poi_id": "a", "lng": 1, "lat": 2},
                    {"poi_id": "b", "lng": 3, "lat": 4},
                ],
                "legs": [],
            }
        ]
    }
    table: dict = {}
    stats = {"amap_rate_limits": 0}
    budget = SearchBudget(clock=lambda: 0.0, limit=5, window=60, max_timeout=6)

    async def run():
        return await fill_routes(
            plan,
            table,
            city="北京",
            transport_mode="walking",
            client=Client(429),
            clock=lambda: 0.0,
            budget=budget,
            stats=stats,
        )

    error = asyncio.run(run())
    assert error == "rate_limit"


def test_route_window_uses_reserved_seconds(monkeypatch):
    """路线段用 reserved_seconds 而非取证剩余：取证耗尽自己阶段后路线仍有一次必要请求。"""
    from planner.budget import RunBudget

    monkeypatch.setenv("PLAN_BUDGET_SECONDS", "240")
    monkeypatch.setenv("PLAN_CLOSEOUT_SECONDS", "10")
    now = {"t": 0.0}
    budget = RunBudget(clock=lambda: now["t"])
    budget.set_reserve(4)  # 4 条必要路段 → 12*4=48 秒预留
    # 模拟取证耗尽自己阶段时间后，剩余工作时间仍够路线拿预留窗口。
    now["t"] = 180.0
    assert budget.work_left() == 50.0
    # reserved_seconds 受 work_left 与 reserve_seconds 双重约束。
    assert budget.reserved_seconds() == 48.0
    assert budget.work_left_without_reserve() == 2.0


def _area_stay():
    """area 精度的住宿锚点：name 是用户原话，poi_name 是高德检索到的车站名。"""
    return {
        "poi_id": "shnan",
        "name": "上海南站附近",
        "poi_name": "上海南站",
        "lng": 121.429,
        "lat": 31.154,
        "lodging_kind": "area",
        "precision": "area",
    }


def test_area_anchor_seats_every_day_between_depart_and_return():
    """area 锚点也要真的用起来：每天从参考点出发、回到参考点，两段接驳都进 legs。"""
    from planner.quality import leg_counts

    pois = [_poi(str(i), f"甲{i}", 116.41 + i * 0.01, 39.91, visit_minutes=60) for i in range(3)]
    plan = schedule(
        pois,
        {"date": "2026-11-08", "days": 3, "lodging_poi": _area_stay(), "transport_mode": "public_transit"},
        _table(slow=()),
    )
    assert len(plan["days"]) == 3
    for day in plan["days"]:
        roles = [item.get("role") for item in day["items"]]
        assert roles[0] == "depart" and roles[-1] == "return"
        assert day["items"][0]["poi_id"] == day["items"][-1]["poi_id"] == "shnan"
        assert len(day["legs"]) == 2 * len(_names(plan, day["day"]))
        assert all(leg["origin_id"] != leg["dest_id"] for leg in day["legs"])
    _referenced, _verified, required = leg_counts(plan)
    assert required == 6
    assert required == sum(len(day["legs"]) for day in plan["days"])

    prose = itinerary_markdown(plan)
    # area 锚点只能写成「区域参考点」；地名用检索到的 poi_name，不是用户原话里的「附近」。
    assert "以上海南站为住宿区域参考点，酒店到参考点的接驳待定。" in prose
    assert "上海南站附近为住宿区域参考点" not in prose
    assert "已确认酒店" not in prose


def test_exact_lodging_does_not_add_area_reference_note():
    """明确酒店不需要补区域参考点的话，也不能被写成只到区域。"""
    stay = {**_stay(), "lodging_kind": "exact", "precision": "exact"}
    plan = schedule(
        [_poi("a", "甲", 116.41, 39.91, visit_minutes=60)],
        {"date": "2026-11-08", "days": 1, "lodging_poi": stay},
        _table(slow=()),
    )
    assert "住宿区域参考点" not in itinerary_markdown(plan)


def test_no_anchor_keeps_every_day_without_self_loop_legs():
    """没有住宿锚点时天数照样齐，每天有窗口和说明，空天不造「住宿地→住宿地」自环。"""
    pois = [_poi(str(i), f"甲{i}", 116.41 + i * 0.01, 39.91, visit_minutes=60) for i in range(3)]
    plan = schedule(
        pois,
        {"date": "2026-11-08", "days": 3, "transport_mode": "public_transit"},
        _table(slow=()),
    )
    assert len(plan["days"]) == 3
    assert [day["date"] for day in plan["days"]] == ["2026-11-08", "2026-11-09", "2026-11-10"]
    for day in plan["days"]:
        assert isinstance(day["window"], dict)
        assert day["notes"]
        assert all(leg["origin_id"] != leg["dest_id"] for leg in day["legs"])


def test_cross_day_arrival_keeps_first_day_and_names_the_reason():
    """跨日抵达：首日不可游也要留在行程里，写清「才抵达」，不写当天并不存在的出发活动。"""
    outbound = _known_outbound("15:27", arrive_date="2026-11-08")
    plan = schedule(
        [_poi("a", "甲", 116.41, 39.91, visit_minutes=60)],
        {
            "date": "2026-11-07",
            "days": 2,
            "lodging_poi": _area_stay(),
            "rail_window": _rail(outbound=outbound),
        },
        _table(slow=()),
    )
    assert [day["date"] for day in plan["days"]] == ["2026-11-07", "2026-11-08"]
    first = plan["days"][0]
    assert first["skip"] is True and first["arrival_only"] is True
    assert first["window"]["state"] == "unavailable"
    assert any("才抵达" in note for note in first["notes"])
    prose = itinerary_markdown(plan)
    assert "才抵达" in prose
    assert "从住宿地出发" not in prose


def test_window_too_short_keeps_day_and_lists_dropped_sight():
    """窗口排不下景点：该天仍在 days 里并标 unverified，景点进 dropped。"""
    inbound = {
        "state": "known",
        "basis": {"train_no": "G2"},
        "depart_date": "2026-11-08",
        "depart": "12:00",
        "latest_end": 10 * 60 + 50,
        "reason": "按 12:00 发车",
    }
    outbound = _known_outbound("11:20", arrive_date="2026-11-08")
    plan = schedule(
        [_poi("a", "甲", 116.41, 39.91, visit_minutes=120)],
        {
            "date": "2026-11-08",
            "days": 1,
            "lodging_poi": _stay(),
            "rail_window": _rail(outbound=outbound, inbound=inbound),
        },
        _table(slow=()),
    )
    assert len(plan["days"]) == 1
    day = plan["days"][0]
    assert day["date"] == "2026-11-08"
    assert day["window"]["state"] == "unavailable"
    assert day["unverified"] is True
    assert any(row["name"] == "甲" for row in plan["dropped"])
    assert plan["schedule_status"] == "candidate"


def test_all_days_kept_when_nothing_is_scheduled():
    """全程没有可排景点时仍按 constraints 产出每一天，而不是给一个空列表。"""
    plan = schedule(
        [],
        {"date": "2026-11-08", "days": 3, "lodging_poi": _stay()},
        _table(slow=()),
    )
    assert len(plan["days"]) == 3
    assert [day["date"] for day in plan["days"]] == ["2026-11-08", "2026-11-09", "2026-11-10"]
    assert plan["schedule_status"] == "candidate"
    assert all(day["legs"] == [] for day in plan["days"])
    assert all(day["notes"] for day in plan["days"])
    assert len(plan["days"]) == len(plan["assignment"])


def test_lodging_requery_matches_short_name_not_the_area_phrase(monkeypatch):
    """「上海南站附近」要先查原串、再用短名重查，并用当前查询词匹配结果。"""
    from planner.map_fallback import _pick, collect_candidates

    hits = [
        {"poi_id": "shnan", "name": "上海南站", "place_type": "交通设施服务;火车站;火车站"},
        {"poi_id": "shnan_sub", "name": "上海南站（地铁站）", "place_type": "交通设施服务;地铁站;地铁站"},
    ]
    assert _pick(hits, "上海南站")["name"] == "上海南站"
    # 原串带「附近」，两个结果名里都没有这个词：拿原串匹配会全部落空。
    assert _pick(hits, "上海南站附近") is None

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    # 只有短名能查到结果：原串「上海南站附近」在高德侧查不到东西。
    opener = _opener_by_keyword(
        {
            "上海南站": [
                _amap_poi("shnan_sub", "上海南站（地铁站）", 121.4291, 31.1542, "交通设施服务;地铁站;地铁站"),
                _amap_poi("shnan", "上海南站", 121.429, 31.154, "交通设施服务;火车站;火车站"),
            ],
        }
    )
    stay, _sights, unlocated = collect_candidates(opener, city="上海", days=3, lodging="上海南站附近")
    assert stay is not None
    assert stay["lodging_kind"] == "area" and stay["precision"] == "area"
    assert stay["name"] == "上海南站附近" and stay["poi_name"] == "上海南站"
    assert [item["reason"] for item in unlocated] == ["area"]


def test_arrival_day_with_area_anchor_says_connection_is_pending():
    """抵达日被车次收窄、住宿只有区域精度：接驳写「待定」，不写已安排好，也不写已入住。"""
    outbound = _known_outbound("15:27", arrive_date="2026-11-08")
    plan = schedule(
        [_poi("a", "甲", 116.41, 39.91, visit_minutes=60)],
        {
            "date": "2026-11-08",
            "days": 2,
            "lodging_poi": _area_stay(),
            "rail_window": _rail(outbound=outbound),
        },
        _table(slow=()),
    )
    first = plan["days"][0]
    assert first["date"] == "2026-11-08" and first["window"]["state"] == "constrained"
    assert "抵达后按区域参考点接驳，具体安排待定" in first["notes"]
    prose = itinerary_markdown(plan)
    assert "抵达后按区域参考点接驳，具体安排待定" in prose
    assert "已确认酒店" not in prose
    assert "已入住" not in prose
    # 收窄的只是抵达日，之后的正常游览日照旧从锚点出发。
    assert _names(plan, 2) == ["甲"]


def test_anchor_ready_is_not_the_same_as_loop_verified():
    """area 锚点只是「能排首末接驳」，不等于「酒店已确认」；确认语义只认 exact。"""
    from planner.map_fallback import _constraints

    def build(stay):
        return _constraints("上海", "2026-11-08", 3, stay, None, [], [], [], "")

    area = build(_area_stay())
    assert area["lodging_anchor_ready"] is True
    assert area["lodging_loop_verified"] is False

    exact = build({**_stay(), "lodging_kind": "exact", "precision": "exact"})
    assert exact["lodging_anchor_ready"] is True and exact["lodging_loop_verified"] is True

    unresolved = build({**_stay(), "lodging_kind": "unresolved", "precision": "fallback"})
    assert unresolved["lodging_anchor_ready"] is False
    assert unresolved["lodging_loop_verified"] is False

    # 完全没定位到住宿时两个字段都是假：没有锚点，也没有确认过。
    none = build(None)
    assert none["lodging_anchor_ready"] is False and none["lodging_loop_verified"] is False


def test_unresolved_lodging_says_connections_were_not_scheduled():
    """住宿没定位到时正文要说清接驳没纳入排程，不能写成已有参考点。"""
    stay = {**_stay(), "lodging_kind": "unresolved", "precision": "fallback"}
    plan = schedule(
        [_poi("a", "甲", 116.41, 39.91, visit_minutes=60)],
        {"date": "2026-11-08", "days": 1, "lodging_poi": stay},
        _table(slow=()),
    )
    prose = itinerary_markdown(plan)
    assert "住宿参考点未建立，住宿往返接驳没有纳入排程。" in prose
    assert "住宿区域参考点" not in prose
    assert "已确认酒店" not in prose


def test_need_route_false_keeps_itinerary_without_route_queries(monkeypatch):
    """m02971 P1-⑤：用户明确不查路线时，景点候选与结构化行程保留，但不发任何路线 HTTP。

    相邻段一律 not_queried、耗时未知、到达时刻 None；路线图 HTML 与「路线耗时查询」
    是两件事，这里只关后者。
    """
    import httpx

    from planner.map_fallback import assemble_itinerary
    from planner.travel_search import gather_facts

    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "test")
    monkeypatch.setenv("TAVILY_API_KEY", "test")

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    class Payload:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    def pois(rows):
        return {"status": "1", "infocode": "10000", "pois": rows}

    def row(poi_id, name, lng, lat, place_type):
        return {"id": poi_id, "name": name, "location": f"{lng},{lat}",
                "type": place_type, "address": name}

    async def post(_query, _timeout):
        return {"results": []}

    async def wrapped(candidates, **kwargs):
        kwargs["post"] = post
        return await gather_facts(candidates, **kwargs)

    monkeypatch.setattr("planner.travel_search.gather_facts", wrapped)

    async def run(need_route):
        calls = {"routes": 0}

        class Client:
            async def get(self, url, params=None, timeout=None):
                if "place/text" in str(url):
                    keywords = params["keywords"]
                    table = {
                        "酒店": [row("hotel", "如家酒店", "116.40", "39.90", "住宿服务")],
                        "甲景区": [row("a", "甲景区", "116.41", "39.91", "商务住宅")],
                        "乙公园": [row("yi", "乙公园", "116.43", "39.93", "公园")],
                        "景点": [row("bing", "丙园", "116.44", "39.94", "公园")],
                    }
                    return Payload(pois(table.get(keywords, [])))
                calls["routes"] += 1
                return Payload({"status": "1", "infocode": "10000",
                                "route": {"paths": [{"duration": "600"}]}})

            async def aclose(self):
                return None

        monkeypatch.setattr(httpx, "AsyncClient", lambda *_a, **_k: Client())
        built = await assemble_itinerary(
            city="北京", days=1, date="2026-11-04", lodging="酒店",
            transport_mode="walking", must_visit=["甲景区"], avoid=[],
            hints=[{"name": "甲景区", "visit_minutes": 60},
                   {"name": "乙公园", "visit_minutes": 90}],
            query_text="北京一天", need_route=need_route,
        )
        return built, calls["routes"]

    no_route_built, no_route_calls = asyncio.run(run(False))
    _, _ = asyncio.run(run(True))  # 默认路径仍可正常发路线请求，保证短路没有全局副作用

    # 不发任何路线 HTTP 请求，但候选与结构化行程仍在。
    assert no_route_calls == 0
    assert no_route_built["candidates"]
    days = no_route_built["plan"]["days"]
    assert days and any(day.get("items") for day in days)

    # 所有相邻段都标 not_queried，耗时未知 → 起止时刻保持 None。
    states = no_route_built["route_states"]
    assert states and set(states.values()) == {"not_queried"}
    assert all(
        leg.get("duration_seconds") is None
        for day in days
        for leg in day.get("legs", [])
    )
    assert no_route_built["route_error"] == "not_queried"
    assert "用户明确不查路线" in no_route_built["context"]

