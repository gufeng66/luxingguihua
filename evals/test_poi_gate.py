"""B5 结构化事实闸门：「景点建议 / 行程表」不能多写排程里没有的东西。

这批用例存在的原因：落盘前车票、预算、行程表都会被结构化数据整段替换，
只有「景点建议」一直由模型自由发挥。于是模型可以在那里加一个排程里根本没有
的景点、把同一天的顺序写乱、把参考耗时说成实测、或者在日期未定时编个日期。
本文件按四类检查逐条给反向用例，再给正向用例证明「一致时零 issue」——
避免只有严格没有宽容，把正常正文也判成不合格。
"""

from __future__ import annotations

from planner.summary import (
    degraded_plan,
    itinerary_whitelist,
    map_summary_block,
    poi_gate_issues,
    section_body,
    sights_suggestion_markdown,
    summary_gate_issues,
)

# ---------------------------------------------------------------------------
# 测试用的结构化行程：第1天 故宫 → 天安门（交通 12 分钟），第2天 长城。
# dates 有值时 date_unknown=False；传 dates=False 造「日期未定」的场景。
# ---------------------------------------------------------------------------


def _itinerary(*, dates: bool = True, station: bool = False) -> dict:
    day1_date = "2026-11-03" if dates else None
    day2_date = "2026-11-04" if dates else None
    day1_items = [
        {"poi_id": "gugong", "name": "故宫", "role": "visit", "start": "09:00", "end": "11:00"},
        {"poi_id": "tiananmen", "name": "天安门", "role": "visit", "start": "11:20", "end": "12:20"},
    ]
    if station:
        # 没有 lodging_kind/precision/poi_name 的 depart 点按「站点」处理。
        day1_items.insert(0, {"poi_id": "zhan", "name": "北京西站", "role": "depart", "start": "08:30", "end": "08:30"})
    return {
        "days": [
            {
                "day": 1,
                "date": day1_date,
                "window": {"state": "default", "start": 540, "end": 1080},
                "items": day1_items,
                "legs": [
                    {"origin_id": "gugong", "dest_id": "tiananmen", "mode": "walking",
                     "duration_seconds": 720, "depart": "11:00", "confidence": "reference"},
                ],
                "notes": ["午餐 60 分钟"],
                "unverified": False,
            },
            {
                "day": 2,
                "date": day2_date,
                "window": {"state": "default", "start": 540, "end": 1080},
                "items": [
                    {"poi_id": "changcheng", "name": "长城", "role": "visit", "start": "09:30", "end": "12:30"},
                ],
                "legs": [],
                "notes": [],
                "unverified": False,
            },
        ],
        "schedule_status": "ok",
    }


_SECTIONS = """## 需求摘要
- 行程：新乡 → 北京，2 天。

## 景点建议
{sights}

## 车票建议
本次未查票（无需铁路）。

## 预算
暂无可靠估算。

## 行程表
{itinerary}

## 注意事项
- 交通耗时均为参考值。
"""


def _answer(sights: str, itinerary: str = "- 第1天 2026-11-03：故宫 09:00–11:00 → 天安门 11:20–12:20\n- 第2天 2026-11-04：长城 09:30–12:30") -> str:
    return _SECTIONS.format(sights=sights, itinerary=itinerary)


def _whitelist(*, dates: bool = True, station: bool = False, unlocated=None, candidates=None):
    return itinerary_whitelist(_itinerary(dates=dates, station=station), unlocated=unlocated, candidates=candidates)


# ---------------------------------------------------------------------------
# 白名单本身
# ---------------------------------------------------------------------------


def test_whitelist_empty_itinerary_is_date_unknown():
    whitelist = itinerary_whitelist(None)
    assert whitelist["date_unknown"] is True
    assert whitelist["days"] == []
    for key in ("sight_names", "lodging_names", "station_names", "unselected_names", "unlocated_names", "must_visit_names", "durations"):
        assert whitelist[key] == set()


def test_whitelist_backfills_leg_names_from_poi_id():
    whitelist = _whitelist()
    day1 = whitelist["days"][0]
    assert day1["poi_order"] == ["故宫", "天安门"]
    assert day1["legs"] == [
        {"from": "故宫", "to": "天安门", "duration_minutes": 12, "status": "reference"}
    ]
    assert whitelist["durations"] == {12}
    assert whitelist["date_unknown"] is False


def test_whitelist_unknown_duration_keeps_status_unknown():
    plan = {"days": [{"day": 1, "date": "2026-11-03", "items": [{"poi_id": "a", "name": "甲", "role": "visit", "start": None, "end": None}],
                      "legs": [{"origin_id": "a", "dest_id": "a", "mode": "walking", "duration_seconds": None, "confidence": "unknown"}]}]}
    whitelist = itinerary_whitelist(plan)
    leg = whitelist["days"][0]["legs"][0]
    assert leg["duration_minutes"] is None
    assert leg["status"] == "unknown"


def test_whitelist_marks_candidates_and_unlocated():
    whitelist = _whitelist(
        unlocated=[{"name": "甲博物馆", "kind": "sight"}, {"name": "乙塔", "kind": "must_visit"}],
        candidates=[{"name": "故宫"}, {"name": "天坛"}],
    )
    with_must = _whitelist(unlocated=[{"name": "乙塔", "kind": "must_visit"}])
    assert whitelist["unselected_names"] == {"天坛"}
    assert whitelist["unlocated_names"] == {"甲博物馆", "乙塔"}
    assert with_must["must_visit_names"] == {"乙塔"}


def test_whitelist_separates_station_from_lodging():
    station = _whitelist(station=True)
    assert station["station_names"] == {"北京西站"}
    assert station["lodging_names"] == set()
    lodging = _whitelist()
    lodging["days"]  # 无所谓，只是确保结构可用
    plan = {"days": [{"day": 1, "date": "2026-11-03", "items": [
        {"poi_id": "h1", "name": "北京西站附近", "role": "depart", "start": "08:00", "end": "08:00",
         "lodging_kind": "area", "precision": "area", "poi_name": "北京西站"},
        {"poi_id": "s1", "name": "故宫", "role": "visit", "start": "09:00", "end": "11:00"},
    ], "legs": []}]}
    whitelist = itinerary_whitelist(plan)
    assert whitelist["lodging_names"] == {"北京西站附近"}
    assert whitelist["station_names"] == set()
    assert whitelist["sight_names"] == {"故宫"}


# ---------------------------------------------------------------------------
# 检查 1：名称
# ---------------------------------------------------------------------------


def test_extra_sight_in_suggestion_is_flagged():
    whitelist = _whitelist(candidates=[{"name": "天坛"}])
    answer = _answer("- 第1天：故宫 → 天安门；下午去天坛。\n- 第2天：长城")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "poi_not_in_itinerary:天坛" in issues


def test_unlocated_sight_written_as_scheduled_is_flagged():
    whitelist = _whitelist(unlocated=[{"name": "甲博物馆", "kind": "sight"}])
    answer = _answer("- 第1天：故宫 → 甲博物馆 → 天安门\n- 第2天：长城")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "poi_not_in_itinerary:甲博物馆" in issues


def test_candidate_written_into_itinerary_section_is_flagged():
    whitelist = _whitelist(candidates=[{"name": "天坛"}])
    answer = _answer(
        "- 第1天：故宫 → 天安门\n- 第2天：长城",
        itinerary="- 第1天 2026-11-03：故宫 → 天坛 → 天安门",
    )
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "poi_not_in_itinerary:天坛" in issues


def test_station_written_as_attraction_is_flagged():
    whitelist = _whitelist(station=True)
    ok_answer = _answer("- 第1天：从北京西站出发，故宫 → 天安门\n- 第2天：长城")
    assert poi_gate_issues(ok_answer, whitelist, plan_ready=True) == []
    bad_answer = _answer("- 第1天：游览北京西站 → 故宫 → 天安门\n- 第2天：长城")
    issues = poi_gate_issues(bad_answer, whitelist, plan_ready=True)
    assert "poi_not_in_itinerary:北京西站" in issues


def test_honest_unlocated_line_is_not_flagged():
    whitelist = _whitelist(unlocated=[{"name": "甲博物馆", "kind": "sight"}])
    answer = _answer("- 第1天：故宫 → 天安门\n- 第2天：长城\n未能定位：甲博物馆（检索未确认位置，未排入行程）")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


def test_candidate_substring_of_scheduled_name_is_not_flagged():
    # 候选「天坛」而行程排的是「天坛公园」：正文写「天坛公园」不该被判成多写。
    plan = _itinerary()
    plan["days"][1]["items"] = [{"poi_id": "tt", "name": "天坛公园", "role": "visit", "start": "09:30", "end": "12:30"}]
    whitelist = itinerary_whitelist(plan, candidates=[{"name": "天坛"}])
    answer = _answer("- 第1天：故宫 → 天安门\n- 第2天：天坛公园")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


def test_other_sections_are_not_scanned_for_names():
    # 「预算」里提到门票名不算景点建议多写；只查景点建议与行程表。
    whitelist = _whitelist(candidates=[{"name": "天坛"}])
    answer = _answer("- 第1天：故宫 → 天安门\n- 第2天：长城").replace("暂无可靠估算。", "天坛门票 15 元。")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


# ---------------------------------------------------------------------------
# 检查 2：顺序
# ---------------------------------------------------------------------------


def test_reordered_sights_same_day_are_flagged():
    whitelist = _whitelist()
    answer = _answer("- 第1天：天安门 → 故宫\n- 第2天：长城")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "poi_order_mismatch" in issues


def test_order_between_days_going_backwards_is_flagged():
    whitelist = _whitelist()
    answer = _answer("- 第2天：长城\n- 第1天：故宫 → 天安门")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "poi_order_mismatch" in issues


# ---------------------------------------------------------------------------
# 检查 3：耗时
# ---------------------------------------------------------------------------


def test_overclaimed_travel_time_is_flagged():
    whitelist = _whitelist()
    answer = _answer("- 第1天：故宫 → 天安门，约 25 分钟\n- 第2天：长城")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "travel_time_overclaim:25分钟" in issues


def test_known_travel_time_is_accepted():
    whitelist = _whitelist()
    answer = _answer("- 第1天：故宫 → 天安门，步行约 12 分钟\n- 第2天：长城")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


def test_visit_span_minutes_is_accepted():
    # 故宫 09:00–11:00 就是 120 分钟，正文写「参观约 120 分钟」不算超报。
    whitelist = _whitelist()
    answer = _answer("- 第1天：故宫（参观约 120 分钟） → 天安门\n- 第2天：长城")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


def test_itinerary_section_schedule_assumptions_are_not_travel_overclaims():
    # 「行程表」由 itinerary_markdown 生成，头部固定写「午餐 60 分钟，切换 15 分钟」，
    # 那是排程假设不是景点耗时，不能因此报错。
    whitelist = _whitelist()
    answer = _answer(
        "- 第1天：故宫 → 天安门\n- 第2天：长城",
        itinerary="默认每日 09:00–18:00。午餐 60 分钟，切换 15 分钟，机动 30 分钟。\n- 第1天：故宫 → 天安门",
    )
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


# ---------------------------------------------------------------------------
# 检查 4：状态与日期
# ---------------------------------------------------------------------------


def test_reference_leg_written_as_measured_is_flagged():
    whitelist = _whitelist()
    answer = _answer("- 第1天：故宫 → 天安门，实测 12 分钟\n- 第2天：长城")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "travel_time_status_overclaim" in issues


def test_verified_wording_outside_travel_time_is_not_flagged():
    # 「已确认门票需预约」说的是门票，不是交通耗时，不能算状态越界。
    whitelist = _whitelist()
    answer = _answer("- 第1天：故宫 → 天安门，步行约 12 分钟\n- 第2天：长城\n已确认门票需提前预约。")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


def test_reference_leg_written_as_verified_without_number_is_flagged():
    whitelist = _whitelist()
    answer = _answer("- 第1天：故宫 → 天安门，步行耗时已核实\n- 第2天：长城")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "travel_time_status_overclaim" in issues


def test_date_written_when_date_unknown_is_flagged():
    whitelist = _whitelist(dates=False)
    assert whitelist["date_unknown"] is True
    answer = _answer("- 第1天 2026-11-03：故宫 → 天安门\n- 第2天：长城", itinerary="- 第1天 日期待定：故宫")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "date_overclaim" in issues


def test_chinese_date_written_when_date_unknown_is_flagged():
    whitelist = _whitelist(dates=False)
    answer = _answer("- 第1天 11月3日：故宫 → 天安门\n- 第2天：长城", itinerary="- 第1天 日期待定：故宫")
    issues = poi_gate_issues(answer, whitelist, plan_ready=True)
    assert "date_overclaim" in issues


def test_demand_summary_date_is_not_treated_as_overclaim():
    # 「需求摘要」的日期是确定性生成的（写用户给的真实日期），不算编造日期。
    whitelist = _whitelist(dates=False)
    answer = _answer("- 第1天：故宫 → 天安门\n- 第2天：长城", itinerary="- 第1天 日期待定：故宫")
    answer = answer.replace("- 行程：新乡 → 北京，2 天。", "- 行程：新乡 → 北京，2 天。\n- 日期：2026-11-03")
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


# ---------------------------------------------------------------------------
# plan_ready=False：没有结构化行程
# ---------------------------------------------------------------------------


def test_without_plan_names_and_order_are_not_checked():
    whitelist = itinerary_whitelist(None)
    # 没有结构化行程时「多写一个景点」无从判断，只能要求说清待检索。
    answer = _answer("- 待检索：故宫、天坛（暂未排定）", itinerary="- 暂无。")
    assert poi_gate_issues(answer, whitelist, plan_ready=False) == []


def test_without_plan_needs_pending_wording():
    whitelist = itinerary_whitelist(None)
    answer = _answer("- 第1天：故宫 → 天安门", itinerary="- 暂无。")
    issues = poi_gate_issues(answer, whitelist, plan_ready=False)
    assert "poi_claim_without_plan" in issues


def test_without_plan_still_checks_travel_time_and_status():
    whitelist = itinerary_whitelist(None)
    answer = _answer("- 待检索：故宫，约 25 分钟", itinerary="- 暂无。")
    issues = poi_gate_issues(answer, whitelist, plan_ready=False)
    assert "travel_time_overclaim:25分钟" in issues


# ---------------------------------------------------------------------------
# 正向：结构化一致时零 issue
# ---------------------------------------------------------------------------


def test_consistent_answer_has_no_issues():
    whitelist = _whitelist(candidates=[{"name": "天坛"}], unlocated=[{"name": "甲博物馆", "kind": "sight"}])
    answer = _answer(
        "- 第1天（09:00–18:00）：故宫 09:00–11:00 → 天安门 11:20–12:20，步行约 12 分钟\n"
        "- 第2天（09:00–18:00）：长城 09:30–12:30\n"
        "未能定位：甲博物馆\n"
        "备选（未排入行程）：天坛"
    )
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []
    # 同一篇正文也必须能过原有闸门，避免两套校验互相打架。
    assert summary_gate_issues(answer, "skipped") == []


# ---------------------------------------------------------------------------
# 确定性生成器
# ---------------------------------------------------------------------------


def test_sights_suggestion_lists_scheduled_sights_only():
    text = sights_suggestion_markdown(
        _itinerary(),
        unlocated=[{"name": "甲博物馆", "kind": "sight"}],
        candidates=[{"name": "天坛"}, {"name": "故宫"}],
    )
    assert "故宫 09:00–11:00" in text
    assert "天安门 11:20–12:20" in text
    assert "长城 09:30–12:30" in text
    assert "未能定位：甲博物馆" in text
    # 已排入的「故宫」不能同时出现在备选里。
    assert "备选（未排入行程）：天坛" in text
    assert "分钟" not in text


def test_sights_suggestion_passes_own_gate():
    whitelist = _whitelist(unlocated=[{"name": "甲博物馆", "kind": "sight"}], candidates=[{"name": "天坛"}])
    sights = sights_suggestion_markdown(
        _itinerary(),
        unlocated=[{"name": "甲博物馆", "kind": "sight"}],
        candidates=[{"name": "天坛"}],
    )
    answer = _answer(sights)
    assert poi_gate_issues(answer, whitelist, plan_ready=True) == []


def test_sights_suggestion_unknown_date_says_pending():
    text = sights_suggestion_markdown(_itinerary(dates=False))
    assert "日期待定" in text
    assert "2026-11" not in text
    whitelist = _whitelist(dates=False)
    assert poi_gate_issues(_answer(text, itinerary="- 第1天 日期待定：故宫"), whitelist, plan_ready=True) == []


def test_sights_suggestion_without_itinerary_uses_placeholder():
    text = sights_suggestion_markdown(None)
    assert "景点检索结果未写入成文" in text


def test_degraded_plan_uses_generator():
    plan = _itinerary()
    text = degraded_plan("skipped", ["缺少章节: 预算"], itinerary=plan)
    sights = section_body(text, "景点建议")
    assert "故宫" in sights and "长城" in sights
    assert "分钟" not in sights
    assert summary_gate_issues(text, "skipped") == []


# ---------------------------------------------------------------------------
# map_summary_block：ok 分支改传白名单摘要
# ---------------------------------------------------------------------------


def test_map_summary_block_ok_uses_whitelist_summary():
    whitelist = _whitelist(unlocated=[{"name": "甲博物馆", "kind": "sight"}], candidates=[{"name": "天坛"}])
    text = map_summary_block(
        "ok",
        "行程表原文……天坛可以加进去",
        plan_ready=True,
        whitelist=whitelist,
    )
    assert "第1天" in text and "故宫 → 天安门" in text
    assert "未能定位：甲博物馆" in text
    assert "备选" not in text.replace("仅候选、未排入行程", "")
    assert "不得新增未列出的景点" in text
    # 不再把 map_context 原文交给模型。
    assert "天坛可以加进去" not in text


def test_map_summary_block_ok_accepts_itinerary_directly():
    text = map_summary_block("ok", "原文", plan_ready=True, itinerary=_itinerary())
    assert "故宫 → 天安门" in text


def test_map_summary_block_drops_foreign_evidence_links():
    whitelist = _whitelist()
    text = map_summary_block(
        "ok",
        "原文",
        plan_ready=True,
        whitelist=whitelist,
        evidence_links=["https://www.dpm.org.cn/visit", "https://evil.example/x", "javascript:alert(1)"],
    )
    assert "https://www.dpm.org.cn/visit" in text
    assert "evil.example" not in text
    assert "javascript:" not in text


def test_map_summary_block_without_evidence_links_module_filters_all(monkeypatch):
    import planner.summary as summary_module

    monkeypatch.setattr(summary_module, "_safe_evidence_url", None)
    text = map_summary_block("ok", "原文", plan_ready=True, whitelist=_whitelist(),
                             evidence_links=["https://www.dpm.org.cn/visit"])
    assert "dpm.org.cn" not in text


def test_map_summary_block_keeps_other_branches_unchanged():
    # incomplete + plan_ready 仍要保留矛盾提示（evals/test_itinerary.py 的既有语义）。
    text = map_summary_block("incomplete", "甲博物馆未能定位", plan_ready=True)
    assert "甲博物馆" in text
    assert map_summary_block("ok", "", plan_ready=True).startswith("（景点检索已完成")
