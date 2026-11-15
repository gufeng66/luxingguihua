"""路由纯函数单测。顶部 CASES 与 evals/cases.md 同构，改一条须两边同步。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner.routing import (  # noqa: E402
    classify_revision,
    derive_revision_from_changes,
    is_same_city,
    needs_rail_ticket,
    normalize_place,
    rail_required,
    resolve_map_state,
    resolve_revision_kind,
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
    # 「不要找景点」否决的是景点检索，与「不要地图产物」不是同一层，reason 必须区分。
    assert skip_map_reason("郑州玩，不要找景点") == "user_no_poi"
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


def _slots(**kw) -> TravelSlots:
    base = dict(origin="新乡", destination="上海", date="2026-11-07", days=1)
    base.update(kw)
    return TravelSlots(**base)


def test_rail_required_separates_dependency_from_query() -> None:
    # 跨城铁路默认依赖去程；单日无往返意图不依赖返程。
    assert rail_required(_slots(), "坐高铁去上海玩") == {"outbound": True, "inbound": False}
    # 多日行程默认需要返程。
    assert rail_required(_slots(days=3), "去上海玩三天") == {"outbound": True, "inbound": True}
    # 自驾/飞机不依赖铁路。
    assert rail_required(_slots(), "自驾去上海") == {
        "outbound": False,
        "inbound": False,
    }
    assert rail_required(_slots(), "坐飞机去上海") == {"outbound": False, "inbound": False}
    # 同城不依赖铁路。
    assert rail_required(
        _slots(origin="郑州", destination="郑州"), "郑州市内一日游"
    ) == {"outbound": False, "inbound": False}
    # 单日明确往返则返程也依赖铁路。
    assert rail_required(_slots(), "去上海当天往返") == {"outbound": True, "inbound": True}
    # 修订只改景点、复用旧票（need_ticket=False）时，铁路依赖仍在——关键回归。
    assert rail_required(_slots(), "把景点换成外滩", previous_query="坐高铁去上海") == {
        "outbound": True,
        "inbound": False,
    }


def test_derive_revision_from_changes_routes_days_and_budget() -> None:
    """P1-2 回归：天数变化重查票与排程；只改预算走 budget_only，不查票不查图。"""
    assert derive_revision_from_changes(["days"]) == "ticket_and_map"
    assert derive_revision_from_changes(["date"]) == "ticket_and_map"
    # transport_mode 是市内游览方式，不能触发跨城票务刷新。
    assert derive_revision_from_changes(["transport_mode"]) is None
    assert derive_revision_from_changes(["lodging"]) == "map_only"
    assert derive_revision_from_changes(["must_visit"]) == "map_only"
    assert derive_revision_from_changes(["budget"]) == "budget_only"
    assert derive_revision_from_changes([]) is None


def test_confirm_background_uses_form_intercity_intent_not_old_query() -> None:
    """结果页确认表单回传的旧「坐高铁」不能压过实际改成自驾的选择。"""
    slots = _slots()
    assert skip_ticket_reason(
        slots,
        "从新乡坐高铁去上海",
        request_ctx={"intercity_mode": "driving"},
        query_is_current=False,
    ) == "no_rail_intent"
    assert rail_required(
        slots,
        "从新乡坐高铁去上海",
        request_ctx={"intercity_mode": "driving", "return_required": False},
        query_is_current=False,
    ) == {"outbound": False, "inbound": False}
    assert skip_ticket_reason(
        slots,
        "原始需求写自驾",
        request_ctx={"intercity_mode": "rail"},
        query_is_current=False,
    ) is None


def test_intercity_context_changes_always_invalidate_reuse() -> None:
    assert resolve_revision_kind([], ["intercity_mode"], "") == "ticket_and_map"
    assert resolve_revision_kind([], ["return_required"], "") == "ticket_and_map"


def test_resolve_revision_kind_context_budget_and_map() -> None:
    """P1-2/P1-3 回归：上下文口径变化参与决策，且默认 map_only 不当升级依据。"""
    # 人数/预算口径变化 → budget_only。
    assert resolve_revision_kind([], ["party_size"], "改成三个人") == "budget_only"
    assert resolve_revision_kind([], ["budget_scope"], "预算改成人均") == "budget_only"
    # 住宿确认范围变化 → map_only。
    assert resolve_revision_kind([], ["lodging_scope"], "住宿改到东站") == "map_only"
    # 没有任何结构化/上下文变化时，即使语句命中交通词也不升级。
    assert (
        resolve_revision_kind([], [], "从新乡自驾去郑州两日游") == "summary_only"
    )
    # 结构化变化优先于自然语言。
    assert resolve_revision_kind(["budget"], [], "预算改成3000") == "budget_only"


def test_resolve_revision_kind_reads_explicit_actions() -> None:
    """P1-2 第三轮回归：明确动作要能升级路由，不能被「没有结构化变化」压成 summary_only。

    反例（用户第三轮复审）：说「重新查一下余票」「调整一下景点顺序」都被判成
    summary_only，票和地图都不动；说「预算改为3000元，并重新查询车票」只算 budget_only。
    """
    # 明确要求刷新票务 → 必须重查票并重排。
    assert resolve_revision_kind([], [], "重新查一下余票") == "ticket_and_map"
    assert resolve_revision_kind([], [], "帮我刷新余票") == "ticket_and_map"
    assert resolve_revision_kind([], [], "预算改为3000元，并重新查询车票") == "ticket_and_map"
    # 明确要求调整顺序 → 必须重排地图。
    assert resolve_revision_kind([], [], "调整一下景点顺序") == "map_only"
    assert resolve_revision_kind([], [], "重排行程") == "map_only"
    # 「重新看看正文」不是查票，「顺序」单独出现也不算调整。
    assert resolve_revision_kind([], [], "重新查看注意事项的措辞") == "summary_only"
    assert resolve_revision_kind([], [], "把注意事项的顺序写清楚一点") == "summary_only"
    # 只有预算变化、又没提查票 → 仍然只重算预算。
    assert resolve_revision_kind(["budget"], [], "预算改为3000元") == "budget_only"


def test_resolve_revision_kind_reads_train_selection() -> None:
    """选车/改返程时段必须重排，不能被压成 summary_only。

    用户实测反例：说「返程坐 G3252」「返程改为下午」都被判成 summary_only，
    结果只重写正文，rail_window 与 days 不重算，地图还是旧路线。
    """
    # 点名车次 + 选择类动词 → 从已有适用事实里选车，只重算排程，不重查票。
    assert resolve_revision_kind([], [], "返程坐 G3252") == "map_only"
    assert resolve_revision_kind([], [], "坐G3252回郑州") == "map_only"
    # 返程语境 + 时段词 → 换返程时段同理。
    assert resolve_revision_kind([], [], "返程改为下午") == "map_only"
    assert resolve_revision_kind([], [], "回程想坐晚上那趟") == "map_only"
    # 顺序仍要先判「明确刷新余票」：两者都命中时以重查票为准。
    assert resolve_revision_kind([], [], "返程坐 G3252，重新查询余票") == "ticket_and_map"
    # 只是提到车次、没有选择动作（如「G3252 几点」），不升级。
    assert resolve_revision_kind([], [], "G3252 是几点") == "summary_only"
    # 时段词单独出现、没有返程语境，也不升级。
    assert resolve_revision_kind([], [], "下午去博物院") == "summary_only"


def test_resolve_revision_kind_puts_ticket_changes_before_selection() -> None:
    """天数/票务区间变化必须排在「选车」前面。

    用户实测反例：三日方案提交「改成 4 日游，返程坐 G3252，不要地图」，
    选车判据先命中被判成 map_only，于是行程改成 4 天、票还是 11-09 那张，
    票务连接一次都没调用。天数变了返程日期就变了，必须重查。
    """
    # 同时命中天数变化与选车 → 以重查票为准。
    assert resolve_revision_kind(["days"], [], "改成 4 日游，返程坐 G3252，不要地图") == "ticket_and_map"
    assert resolve_revision_kind(["days"], [], "改成 4 日游") == "ticket_and_map"
    # 天数不变时，单纯选车仍然只重排。
    assert resolve_revision_kind([], [], "返程坐 G3252") == "map_only"
    assert resolve_revision_kind([], [], "返程改为下午") == "map_only"
    # 明确刷新余票仍然最优先。
    assert resolve_revision_kind([], [], "重新查询余票") == "ticket_and_map"
    # 「重新定位住宿区域」是纯原话动作：没有任何槽位/上下文变化，
    # 但必须真的重跑住宿定位（map_only），不能被压成 summary_only。
    assert resolve_revision_kind([], [], "重新定位住宿区域") == "map_only"
    assert resolve_revision_kind([], [], "再定位住宿") == "map_only"
    assert resolve_revision_kind([], [], "住宿重新定位") == "map_only"
    assert resolve_revision_kind([], [], "重新查住宿") == "map_only"
    assert resolve_revision_kind([], [], "重新找住宿") == "map_only"
    assert resolve_revision_kind([], [], "重新核实住宿") == "map_only"
    assert resolve_revision_kind([], [], "重新定位参考地点") == "map_only"
    # 只确认「按区域规划」、没要求重查，仍不算明确的重定位动作。
    assert resolve_revision_kind([], [], "按区域规划住宿") == "summary_only"
    # 只换住宿区域属于地图字段，不动票务。
    assert resolve_revision_kind(["lodging"], [], "重新定位住宿区域") == "map_only"


def test_rail_required_keeps_round_trip_from_previous_query() -> None:
    """P1-4 第三轮回归：本轮只改景点时，历史里的「当天往返」不能被新文本遮掉。"""
    slots = TravelSlots(origin="新乡", destination="郑州", date="2026-09-19", days=1)
    assert rail_required(slots, "把景点换成外滩", previous_query="坐高铁当天往返郑州") == {
        "outbound": True,
        "inbound": True,
    }
    # 本轮自己说往返时同样成立。
    assert rail_required(slots, "坐高铁当天往返郑州", previous_query="坐高铁去郑州") == {
        "outbound": True,
        "inbound": True,
    }
