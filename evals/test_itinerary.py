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

from planner.route_plan import itinerary_markdown, route_cache_key, schedule  # noqa: E402
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
    from planner.map_fallback import duration_from_payload

    assert duration_from_payload("transfer", {"status": "1", "route": {"transits": [{}]}}) is None
    assert duration_from_payload("walking", {"status": "1", "route": {"paths": [{"duration": "0"}]}}) == 0
    assert duration_from_payload("walking", {"status": "0", "route": {"paths": [{"duration": "10"}]}}) is None


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
    assert plan["days"] == []
    assert plan["conflicts"] and plan["conflicts"][0]["reason"] == "出行日期内关闭"
    again = schedule(
        [poi],
        constraints,
        _table(slow=()),
        assignment=plan["assignment"],
        kept_dropped=plan["dropped"],
        kept_conflicts=plan["conflicts"],
    )
    assert again["days"] == []
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
    assert unlocated == ["乙", "甲"]


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
    assert plan["days"] == []
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
