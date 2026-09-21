"""
查票 / 出地图 / 修订分流（纯函数，不联网）

【小白怎么理解？】
    不是每次规划都要调 12306。同城、用户说自驾/坐飞机、或明确不要地图时，
    这里用关键词 + 槽位算出「跳过原因」，pipeline 按原因发 step 事件。
    调度结束后再用 resolve_*_state 告诉汇总：ok / skipped / missed…，禁止瞎编车次。
"""

from __future__ import annotations

import re
from typing import Literal

from slots import TravelSlots

# 票务最终态：ok 有结果；reused 修订沿用上版；其余都禁止编造车次
TicketState = Literal["ok", "skipped", "unavailable", "missed", "timeout", "reused"]
# 用户改上一版方案时走哪条流水线
RevisionKind = Literal["map_only", "ticket_and_map", "summary_only"]
MapState = Literal["ok", "skipped", "missed", "timeout", "incomplete"]

_NO_MAP = (
    "不要地图",
    "不用地图",
    "不需要地图",
    "无需地图",
    "不生成地图",
    "不要路线图",
    "不用路线图",
    "不需要路线图",
    "无需路线图",
)

_RAIL_POS = ("高铁", "动车", "12306", "车票", "坐火车", "乘火车", "火车去")
_DRIVE = ("自驾", "开车", "租车", "不坐火车")
_PLANE = ("飞机", "航班", "机票", "坐飞机", "乘飞机")
_NO_PLANE = ("不坐飞机", "不乘飞机", "不坐航班")

_DATE_CHANGE = (
    "推迟",
    "提前",
    "改期",
    "改天",
    "日期",
    "下周再",
    "改到周",
    "提前到",
    "推迟一天",
    "改明天",
    "改后天",
)
_PLACE_CHANGE = ("出发地", "目的地", "改从", "改去", "换个城市", "换成从")
_TRANSPORT_CHANGE = (
    "高铁",
    "火车",
    "动车",
    "自驾",
    "开车",
    "租车",
    "飞机",
    "航班",
    "机票",
    "打车",
    "交通",
    "地铁",
    "更快的交通",
    "换个快点",
)
_ITINERARY = ("景点", "路线", "景区", "不要", "换成", "少走", "充电", "行程", "顺路", "少林", "白马")
_COPY_ONLY = ("注意事项", "措辞", "语气", "写法", "润色")


def normalize_place(name: str | None) -> str:
    """去掉「省/市/区」便于模糊同城比较。空串表示没法比。"""
    raw = (name or "").strip().replace(" ", "")
    if not raw:
        return ""
    raw = re.sub(r"^[\u4e00-\u9fff]{1,8}省", "", raw)
    return raw.replace("市", "").replace("区", "")


def is_same_city(origin: str | None, destination: str | None) -> bool:
    """判不出则 False（视为跨城 / 需要查票）。新乡 vs 新乡县视为不同城。"""
    a = (origin or "").strip()
    b = (destination or "").strip()
    if not a or not b:
        return False
    if ("县" in a) != ("县" in b):
        return False
    fa, fb = normalize_place(a), normalize_place(b)
    if not fa or not fb:
        return False
    return fa == fb or fa in fb or fb in fa


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    """原文是否包含任一关键词（子串匹配，够用且好测）。"""
    return any(n in text for n in needles)


def rail_intent_flags(query: str) -> dict[str, bool]:
    """从用户原话抽出铁路 / 自驾 / 飞机意图。坐高铁优先于自驾、飞机。"""
    q = query or ""
    no_plane = _has_any(q, _NO_PLANE)
    plane = (not no_plane) and _has_any(q, _PLANE)
    drive = _has_any(q, _DRIVE)
    rail = (not drive) and (
        _has_any(q, _RAIL_POS) or ("火车" in q and "不坐火车" not in q)
    )
    if "坐高铁" in q or "乘高铁" in q or "高铁去" in q:
        rail = True
        drive = False
        plane = False
    return {"rail": rail, "drive": drive, "plane": plane, "no_plane": no_plane}


def skip_ticket_reason(slots: TravelSlots | None, query: str) -> str | None:
    """不需要铁路时的 step.reason；需要铁路则 None。"""
    flags = rail_intent_flags(query)
    if flags["rail"] or flags["no_plane"]:
        return None
    if flags["drive"] or flags["plane"]:
        return "no_rail_intent"
    if slots is not None and is_same_city(slots.origin, slots.destination):
        return "intra_city"
    if flags["drive"]:
        return "no_rail_intent"
    return None


def needs_rail_ticket(slots: TravelSlots | None, query: str) -> bool:
    """本次是否需要铁路查票。仅依据本次 query 与槽位，不用默认画像。"""
    return skip_ticket_reason(slots, query) is None


def skip_map_reason(query: str) -> str | None:
    """用户明确不要路线地图时跳过；未说明则必须生成。"""
    if _has_any(query or "", _NO_MAP):
        return "user_no_map"
    return None


def needs_route_map(query: str) -> bool:
    """默认要出路线地图；用户明确拒绝才 False。"""
    return skip_map_reason(query) is None


def resolve_map_state(
    *,
    need_map: bool,
    dispatched: bool,
    has_result: bool,
    timed_out: bool,
) -> MapState:
    """ok > skipped > missed（未发 task）> timeout（已调度后超时）> incomplete（已调度无 HTML/正文）。"""
    if has_result:
        return "ok"
    if not need_map:
        return "skipped"
    if not dispatched:
        return "missed"
    if timed_out:
        return "timeout"
    return "incomplete"


def resolve_ticket_state(
    *,
    need_ticket: bool,
    ticket_mounted: bool,
    has_ticket_result: bool,
    timed_out: bool,
) -> TicketState:
    """优先级固定：ok > skipped > unavailable > timeout > missed。"""
    if has_ticket_result:
        return "ok"
    if not need_ticket:
        return "skipped"
    if not ticket_mounted:
        return "unavailable"
    if timed_out:
        return "timeout"
    return "missed"


def classify_revision(
    instruction: str,
    *,
    previous_ticket_state: TicketState | None = None,
) -> RevisionKind:
    """是否改变 (date, origin, destination, 出行方式)？是 → ticket_and_map。"""
    text = instruction or ""
    four_changed = (
        _has_any(text, _DATE_CHANGE)
        or _has_any(text, _PLACE_CHANGE)
        or _has_any(text, _TRANSPORT_CHANGE)
    )
    if four_changed:
        return "ticket_and_map"
    if previous_ticket_state == "ok" and _has_any(text, _TRANSPORT_CHANGE):
        return "ticket_and_map"
    copy_only = _has_any(text, _COPY_ONLY) and not _has_any(text, _ITINERARY)
    if copy_only:
        return "summary_only"
    if _has_any(text, _ITINERARY) or len(text.strip()) > 0:
        return "map_only"
    return "map_only"


def merge_slots(base: TravelSlots, overlay: TravelSlots) -> TravelSlots:
    """修订时：用新抽取覆盖旧槽位；overlay 里的空值不覆盖。"""
    data = base.model_dump()
    for key, val in overlay.model_dump().items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        data[key] = val
    return TravelSlots.model_validate(data)
