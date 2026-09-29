"""路由纯函数单测。顶部 CASES 与 evals/cases.md 同构，改一条须两边同步。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner.routing import (  # noqa: E402
    classify_revision,
    is_same_city,
    needs_rail_ticket,
    normalize_place,
    resolve_map_state,
    resolve_ticket_state,
    skip_map_reason,
    skip_ticket_reason,
)
from slots import TravelSlots  # noqa: E402

# 与 evals/cases.md 同构；改任一条两边同步。勿写 md 解析器。
CASES = [
    {
        "id": "cross_city_hsr",
        "query": "这周六从新乡坐高铁去郑州一日游",
        "slots": TravelSlots(origin="新乡", destination="郑州", date="2026-09-19", days=1),
        "need_ticket": True,
        "skip_reason": None,
    },
    {
        "id": "xinxiang_drive_luoyang",
        "query": "下周从新乡租电车自驾去洛阳两天，别安排开封和宝泉",
        "slots": TravelSlots(origin="新乡", destination="洛阳", date="2026-09-26", days=2),
        "need_ticket": False,
        "skip_reason": "no_rail_intent",
    },
    {
        "id": "intra_city",
        "query": "杭州自驾逛西湖，不坐火车",
        "slots": TravelSlots(origin="杭州", destination="杭州", date="2026-09-20", days=1),
        "need_ticket": False,
        "skip_reason": "no_rail_intent",
    },
    {
        "id": "explicit_hsr",
        "query": "从新乡坐高铁去郑州",
        "slots": TravelSlots(origin="新乡", destination="郑州", date="2026-09-19", days=1),
        "need_ticket": True,
        "skip_reason": None,
    },
    {
        "id": "plane_no_rail",
        "query": "从新乡坐飞机去北京玩三天",
        "slots": TravelSlots(origin="新乡", destination="北京", date="2026-09-20", days=3),
        "need_ticket": False,
        "skip_reason": "no_rail_intent",
    },
    {
        "id": "no_plane_needs_rail",
        "query": "从新乡去北京，不坐飞机",
        "slots": TravelSlots(origin="新乡", destination="北京", date="2026-09-20", days=1),
        "need_ticket": True,
        "skip_reason": None,
    },
]


def test_cases_need_ticket() -> None:
    for case in CASES:
        slots = case["slots"]
        query = str(case["query"])
        assert needs_rail_ticket(slots, query) is case["need_ticket"], case["id"]
        assert skip_ticket_reason(slots, query) == case["skip_reason"], case["id"]


def test_normalize_hangzhou_westlake_same_city() -> None:
    assert is_same_city("杭州", "杭州市西湖区") is True
    assert "杭州" in normalize_place("杭州市西湖区") or normalize_place("杭州") in normalize_place(
        "杭州市西湖区"
    )


def test_normalize_xinxiang_vs_county_not_same() -> None:
    assert is_same_city("新乡", "新乡县") is False


def test_resolve_ticket_state_priority() -> None:
    # 1. 有结果就是 ok
    assert (
        resolve_ticket_state(
            need_ticket=False, ticket_mounted=False, has_ticket_result=True, timed_out=True
        )
        == "ok"
    )
    # 2. 不需要铁路，即使 MCP 挂了也是 skipped
    assert (
        resolve_ticket_state(
            need_ticket=False, ticket_mounted=False, has_ticket_result=False, timed_out=False
        )
        == "skipped"
    )
    # 3. 需要但未挂载 → unavailable
    assert (
        resolve_ticket_state(
            need_ticket=True, ticket_mounted=False, has_ticket_result=False, timed_out=True
        )
        == "unavailable"
    )
    # 4. 已挂载超时 → timeout
    assert (
        resolve_ticket_state(
            need_ticket=True, ticket_mounted=True, has_ticket_result=False, timed_out=True
        )
        == "timeout"
    )
    # 5. 否则 missed
    assert (
        resolve_ticket_state(
            need_ticket=True, ticket_mounted=True, has_ticket_result=False, timed_out=False
        )
        == "missed"
    )


def test_skip_map_and_resolve_map_state() -> None:
    assert skip_map_reason("郑州一日游") is None
    assert skip_map_reason("郑州玩，不要地图") == "user_no_map"
    assert skip_map_reason("郑州玩，不要找景点") == "user_no_map"
    assert (
        resolve_map_state(need_map=True, dispatched=True, has_result=True, timed_out=True)
        == "ok"
    )
    assert (
        resolve_map_state(need_map=False, dispatched=False, has_result=False, timed_out=False)
        == "skipped"
    )
    assert (
        resolve_map_state(need_map=True, dispatched=False, has_result=False, timed_out=False)
        == "missed"
    )
    assert (
        resolve_map_state(need_map=True, dispatched=True, has_result=False, timed_out=True)
        == "timeout"
    )
    assert (
        resolve_map_state(need_map=True, dispatched=True, has_result=False, timed_out=False)
        == "incomplete"
    )


def test_classify_revision_four_factors() -> None:
    assert classify_revision("不要龙门石窟，改白马寺，路线顺一点") == "map_only"
    assert classify_revision("推迟一天出发") == "ticket_and_map"
    assert classify_revision("改坐高铁去") == "ticket_and_map"
    assert classify_revision("换个快点的交通") == "ticket_and_map"
    assert classify_revision("注意事项写得客气一点") == "summary_only"
    assert (
        classify_revision("换个景点", previous_ticket_state="ok") == "map_only"
    )
    assert (
        classify_revision("交通换快一点", previous_ticket_state="ok") == "ticket_and_map"
    )
