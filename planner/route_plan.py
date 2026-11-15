"""把候选景点排成按天的地图任务。不联网。"""

from __future__ import annotations

import itertools
import math
import re
from datetime import date, timedelta

_PER_DAY = 4
_NEAR_M = 200
_M_PER_DEG = 111_320


def plan_route(
    sights: list[dict],
    days: int,
    city: str,
    lodging: dict | None = None,
    station: dict | None = None,
) -> list[dict]:
    """方位角升序后按数量均分。等量切分不保证同簇同天，簇大小不同时会把大簇切开。"""
    # ponytail: 局部直线距离，不是路网；某一天超过 4 个点再换最近邻
    days = max(1, int(days or 1))
    if lodging is not None and station is not None and _is_anchor(station, lodging, lodging):
        station = None
    unique = _dedupe(sights)
    if not unique and lodging is None:
        return []
    origin = lodging if lodging is not None else _centroid(unique)
    if origin is None:
        return []
    pool = [poi for poi in unique if not _is_anchor(poi, lodging, origin) and not _is_anchor(poi, station, origin)]
    cap = days * _PER_DAY
    if len(pool) > cap:
        pool = sorted(pool, key=lambda poi: (_dist2(poi, origin), poi["name"]))[:cap]
    pool.sort(key=lambda poi: (_angle(poi, origin), poi["name"]))
    chunks = _split(pool, days)
    filled = [(day, group) for day, group in enumerate(chunks, 1) if group]
    if not filled:
        return []
    last_day = filled[-1][0]
    tasks: list[dict] = []
    for day, group in filled:
        ordered = _order(group, lodging if lodging is not None else origin, close=lodging is not None)
        seq = list(ordered)
        if lodging is not None:
            seq = [lodging, *seq, lodging]
            if station is not None:
                if day == 1:
                    seq = [station, *seq]
                if day == last_day:
                    seq = [*seq, station]
        tasks.extend(_day_tasks(day, seq, city, lodging))
    return tasks


def format_day_list(tasks: list[dict]) -> str:
    by_day: dict[int, list[str]] = {}
    for item in tasks:
        if item.get("type") == "poi":
            by_day.setdefault(int(item["day"]), []).append(str(item.get("text") or ""))
    return "\n".join(f"- 第{day}天：" + " → ".join(names) for day, names in sorted(by_day.items()))


def _dedupe(sights: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for poi in sights:
        name = str(poi.get("name") or "").strip()
        if not name or name in seen:
            continue
        try:
            lng, lat = float(poi["lng"]), float(poi["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(name)
        unique.append({**poi, "name": name, "lng": lng, "lat": lat})
    return unique


def _centroid(points: list[dict]) -> dict | None:
    if not points:
        return None
    n = len(points)
    return {
        "name": "",
        "lng": sum(poi["lng"] for poi in points) / n,
        "lat": sum(poi["lat"] for poi in points) / n,
    }


def _is_anchor(poi: dict, anchor: dict | None, origin: dict) -> bool:
    if anchor is None:
        return False
    if poi["name"] and poi["name"] == str(anchor.get("name") or ""):
        return True
    return _meters(poi, anchor, origin["lat"]) <= _NEAR_M


def _split(sights: list[dict], days: int) -> list[list[dict]]:
    base, extra = divmod(len(sights), days)
    chunks: list[list[dict]] = []
    index = 0
    for day in range(days):
        count = base + (1 if day < extra else 0)
        chunks.append(sights[index : index + count])
        index += count
    return chunks


def _order(group: list[dict], anchor: dict, *, close: bool) -> list[dict]:
    if len(group) <= _PER_DAY:
        close_at = anchor if close else None
        best = min(
            itertools.permutations(group),
            key=lambda perm: (_cost(perm, close_at), [poi["name"] for poi in perm]),
        )
        return list(best)
    return _nearest(group, anchor)


def _nearest(group: list[dict], anchor: dict) -> list[dict]:
    rest = list(group)
    ordered: list[dict] = []
    current = anchor
    while rest:
        nxt = min(rest, key=lambda poi: (_dist2(current, poi), poi["name"]))
        ordered.append(nxt)
        rest.remove(nxt)
        current = nxt
    return ordered


def _cost(perm: tuple[dict, ...], close_at: dict | None) -> float:
    pts = list(perm)
    if close_at is not None:
        pts = [close_at, *pts, close_at]
    return sum(_dist2(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _day_tasks(day: int, seq: list[dict], city: str, lodging: dict | None) -> list[dict]:
    lodging_at = [index for index, poi in enumerate(seq) if lodging is not None and poi is lodging]
    tasks: list[dict] = []
    prev = None
    for index, poi in enumerate(seq):
        remark = str(poi.get("address") or "")
        if lodging_at and index == lodging_at[0]:
            remark = "从住宿地出发"
        elif lodging_at and index == lodging_at[-1]:
            remark = "返回住宿地"
        tasks.append(
            {
                "type": "poi",
                "day": day,
                "lnglat": [poi["lng"], poi["lat"]],
                "sort": f"Day{day}",
                "text": poi["name"],
                "remark": remark,
            }
        )
        if prev is not None:
            tasks.append(
                {
                    "type": "route",
                    "day": day,
                    "routeType": "walking",
                    "start": [prev["lng"], prev["lat"]],
                    "end": [poi["lng"], poi["lat"]],
                    "city": city,
                    "remark": f"{prev['name']}→{poi['name']}",
                }
            )
        prev = poi
    return tasks


def _xy(poi: dict, origin: dict) -> tuple[float, float]:
    scale = math.cos(math.radians(origin["lat"]))
    return ((poi["lng"] - origin["lng"]) * scale, poi["lat"] - origin["lat"])


def _angle(poi: dict, origin: dict) -> float:
    x, y = _xy(poi, origin)
    return math.atan2(y, x)


def _dist2(a: dict, b: dict) -> float:
    x, y = _xy(a, b)
    return x * x + y * y


def _meters(a: dict, b: dict, lat: float) -> float:
    scale = math.cos(math.radians(lat))
    x = (a["lng"] - b["lng"]) * scale
    y = a["lat"] - b["lat"]
    return math.hypot(x, y) * _M_PER_DEG


DAY_START = 9 * 60
DAY_END = 18 * 60
LUNCH_MIN = 60
SWITCH_MIN = 15
SLACK_MIN = 30
NOON = 12 * 60
WALK_MAX_SECONDS = 20 * 60

_PACE_TARGETS = {
    "relaxed": (180, 270),
    "balanced": (240, 360),
    "compact": (300, 420),
}

_PACE_COUNT_TARGETS = {
    "relaxed": (1, 2),
    "balanced": (2, 3),
    "compact": (3, 4),
}
_BOUNDARY_COUNT_TARGETS = {
    "relaxed": (0, 2),
    "balanced": (1, 2),
    "compact": (1, 3),
}
_PACE_WEIGHT_TARGETS = {
    "relaxed": (1.0, 2.0),
    "balanced": (2.0, 3.0),
    "compact": (2.5, 4.0),
}

_TIME_IN_TEXT = re.compile(r"\d{1,2}\s*[:：点]")


def visit_minutes(poi: dict) -> int:
    raw = poi.get("visit_minutes")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    text = str(poi.get("place_type") or "")
    # 第一阶段只区分短活动、常规活动和半日主活动。这里依据 POI 的可解释类别，
    # 不采纳模型自行标记的「大型」；规模未知仍是常规估算，不能借此豁免密度检查。
    if any(token in text for token in ("风景名胜", "主题公园", "动物园", "植物园", "古镇", "度假")):
        return 240
    if any(token in text for token in ("公园", "街区", "park", "street")):
        return 60
    return 120


def activity_weight(poi: dict) -> float:
    """活动负荷估算，不表示景点重要程度或用户偏好。"""
    place_type = str(poi.get("place_type") or "").lower()
    if any(token in place_type for token in ("风景名胜", "主题公园", "动物园", "植物园", "古镇", "度假")):
        return 2.0
    if any(token in place_type for token in ("公园", "街区", "park", "street")):
        return 0.5
    minutes = visit_minutes(poi)
    if minutes <= 90:
        return 0.5
    if minutes <= 180:
        return 1.0
    return 2.0


def _activity_size(weight: float) -> str:
    if weight >= 2.0:
        return "large"
    if weight <= 0.5:
        return "short"
    return "normal"


def _query_duration_minutes(poi: dict, constraints: dict) -> int | None:
    """只接受用户原话中紧邻已定位景点名的停留时长，不从泛化偏好猜具体时长。"""
    text = str(constraints.get("query_text") or "").replace(" ", "")
    name = str(poi.get("asked_name") or poi.get("name") or "").strip()
    if not text or not name or name not in text:
        return None
    escaped = re.escape(name)
    around = rf"{escaped}.{{0,12}}?(?:玩|逛|待|停留|安排).{{0,6}}?(半天|全天|\d+(?:\.\d+)?小时)"
    reverse = rf"(?:玩|逛|待|停留|安排).{{0,6}}?(半天|全天|\d+(?:\.\d+)?小时).{{0,12}}?{escaped}"
    direct = rf"{escaped}.{{0,4}}?(半天|全天|\d+(?:\.\d+)?小时)"
    match = re.search(around, text) or re.search(reverse, text) or re.search(direct, text)
    if match is None:
        return None
    value = match.group(1)
    if value == "半天":
        return 240
    if value == "全天":
        return 360
    try:
        return max(1, round(float(value.removesuffix("小时")) * 60))
    except ValueError:
        return None


def _duration_metadata(poi: dict, constraints: dict) -> None:
    """把本轮实际采用的游览时长与其保守来源固定到候选上。"""
    raw = poi.get("visit_minutes")
    if raw is not None and str(raw).strip() != "":
        poi["visit_minutes"] = visit_minutes(poi)
        poi.setdefault("visit_duration_source", "planner_estimate")
        poi.setdefault("visit_duration_confidence", "explicit" if poi.get("visit_duration_source") == "user" else "estimated")
        return
    explicit = _query_duration_minutes(poi, constraints)
    if explicit is not None:
        poi["visit_minutes"] = explicit
        poi["visit_duration_source"] = "user"
        poi["visit_duration_confidence"] = "explicit"
        return
    poi["visit_minutes"] = visit_minutes(poi)
    poi["visit_duration_source"] = "planner_estimate"
    text = str(poi.get("place_type") or "")
    poi["visit_duration_confidence"] = "estimated" if text else "unknown"


def _pace_profile(constraints: dict) -> str:
    text = str(constraints.get("pace") or "").replace(" ", "")
    # 先识别否定，再看正向词，避免「不想打卡」被末尾的「打卡」误判为紧凑。
    if any(token in text for token in ("不打卡", "不想打卡", "不要太紧凑", "别排太满", "不用赶景点", "少折腾", "轻松", "慢游")):
        return "relaxed"
    if any(token in text for token in ("紧凑", "打卡", "特种兵")):
        return "compact"
    return "balanced"


def classify_day_kind(day_date: date | None, dates: list[date] | None, rail_window: dict | None, plan_basis: dict | None = None) -> str:
    """识别抵达/返程日，不把受限窗口直接当成日类型。"""
    if day_date is None or dates is None:
        return "unknown"
    rail = rail_window if isinstance(rail_window, dict) else {}
    basis = plan_basis if isinstance(plan_basis, dict) else {}
    outbound = rail.get("outbound") or basis.get("outbound") or {}
    inbound = rail.get("inbound") or basis.get("inbound") or {}
    arrival = (
        str(outbound.get("state") or "") == "known"
        and _window_date(outbound.get("arrive_date")) == day_date
        and outbound.get("earliest_start") is not None
    )
    departure = (
        str(inbound.get("state") or "") == "known"
        and _window_date(inbound.get("depart_date")) == day_date
        and inbound.get("latest_end") is not None
    )
    if arrival and departure:
        return "arrival_departure"
    if arrival:
        return "arrival"
    if departure:
        return "departure"
    if str(outbound.get("state") or "") == "unknown" and day_date == dates[0]:
        return "unknown"
    if str(inbound.get("state") or "") == "unknown" and day_date == dates[-1]:
        return "unknown"
    return "full"


def _density_targets(window: dict, constraints: dict) -> tuple[int, int] | None:
    """完整日目标会随已知抵离窗口缩放；未知窗口不凭默认 09:00–18:00 作密度结论。"""
    base_min, base_max = _PACE_TARGETS[_pace_profile(constraints)]
    state = str(window.get("state") or "default")
    if state in {"unknown", "unavailable"}:
        return None
    if state != "constrained":
        return base_min, base_max
    start, end = window.get("start"), window.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    ratio = min(1.0, max(0.0, (end - start) / (DAY_END - DAY_START)))
    # 30 分钟是诊断下限，不是强制填充配额；短抵离日依旧由可行性模拟控制。
    return max(30, math.ceil(base_min * ratio)), max(30, math.ceil(base_max * ratio))


def _effective_window_minutes(window: dict) -> int | None:
    start, end = window.get("start"), window.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    reserve = SLACK_MIN + (LUNCH_MIN if start < NOON < end else 0)
    return max(0, end - start - reserve)


def _count_targets(day_kind: str, window: dict, constraints: dict) -> tuple[int, int] | None:
    if day_kind == "unknown" or window.get("state") in {"unknown", "unavailable"}:
        return None
    profile = _pace_profile(constraints)
    base = _BOUNDARY_COUNT_TARGETS[profile] if day_kind in {"arrival", "departure", "arrival_departure"} else _PACE_COUNT_TARGETS[profile]
    if day_kind == "arrival_departure" or window.get("state") == "unavailable":
        return (0, 0)
    if day_kind == "full" and window.get("state") != "constrained":
        return base
    effective = _effective_window_minutes(window)
    if effective is None:
        return None
    full = DAY_END - DAY_START - LUNCH_MIN - SLACK_MIN
    ratio = min(1.0, max(0.0, effective / full))
    minimum, maximum = base
    if ratio < 0.25:
        return (0, 0)
    if ratio < 0.5:
        return (0, min(1, maximum))
    return (min(minimum, max(0, math.floor(maximum * ratio))), max(1, math.ceil(maximum * ratio)))


def _density(day_plan: dict, constraints: dict) -> dict:
    """活动负荷与时间可行性分开：这里不因未知交通把活动密度伪装成零。"""
    window = day_plan.get("window") or {}
    day_kind = str(day_plan.get("day_kind") or "full")
    targets = _density_targets(window, constraints)
    count_targets = _count_targets(day_kind, window, constraints)
    weight_targets = _PACE_WEIGHT_TARGETS[_pace_profile(constraints)] if count_targets is not None and day_kind == "full" else None
    target_min, target_max = targets if targets is not None else (None, None)
    visits = [item for item in day_plan.get("items") or [] if item.get("role") == "visit"]
    visit = sum(int(item.get("visit_minutes") or 0) for item in visits)
    weight = sum(float(item.get("activity_weight") or 0) for item in visits)
    known = sum(int(leg.get("duration_seconds") or 0) // 60 for leg in day_plan.get("legs") or [] if isinstance(leg.get("duration_seconds"), int) and leg.get("duration_seconds") > 0)
    unknown = sum(1 for leg in day_plan.get("legs") or [] if not isinstance(leg.get("duration_seconds"), int) or leg.get("duration_seconds") <= 0)
    count = len(visits)
    count_status = "unknown" if count_targets is None else ("below" if count < count_targets[0] else "over" if count > count_targets[1] else "met")
    weight_status = "unknown" if weight_targets is None else ("below" if weight < weight_targets[0] else "over" if weight > weight_targets[1] else "met")
    minutes_status = "unknown" if targets is None else ("below" if visit < target_min else "over" if visit > target_max else "met")
    if window.get("state") == "unavailable" or bool(day_plan.get("arrival_only")):
        status, reason = "not_applicable", "当天主要用于抵离，未按完整游览日评估"
    elif targets is None:
        status, reason = "unknown", "抵离窗口未知，暂不按完整游览日评估"
    elif visit < target_min and (count_status == "below" or day_kind != "full"):
        status, reason = "sparse", "游览活动或景点数量低于当天节奏目标"
    elif visit > target_max:
        status, reason = "balanced", "游览活动超过当天参考上限，安排偏紧但不等同于时间冲突"
    elif count_status == "over" or weight_status == "over":
        status, reason = "tight", "景点数量或活动负荷超过当前节奏参考上限"
    else:
        status, reason = "balanced", "游览活动负荷处于当前节奏的参考范围"
    if count_status == "below" and status == "balanced" and targets is not None and visit <= target_max:
        reason = "景点数量低于当前节奏目标，但活动负荷已达到参考范围"
    if unknown:
        reason += "；交通耗时待核实"
    feasibility = "unknown" if unknown or window.get("state") == "unknown" else ("infeasible" if day_plan.get("skip") and day_kind not in {"arrival", "departure", "arrival_departure"} else "feasible")
    return {
        "status": status,
        "pace": _pace_profile(constraints),
        "visit_minutes": visit,
        "activity_count": len(visits),
        "activity_weight": round(weight, 2),
        "activity_weight_source": "duration_rule",
        "estimated_activities": sum(1 for item in visits if item.get("visit_duration_confidence") != "explicit"),
        "known_travel_minutes": known,
        "unknown_legs": unknown,
        "target_min": target_min,
        "target_max": target_max,
        "day_kind": day_kind,
        "target_count_min": count_targets[0] if count_targets is not None else None,
        "target_count_max": count_targets[1] if count_targets is not None else None,
        "target_weight_min": weight_targets[0] if weight_targets is not None else None,
        "target_weight_max": weight_targets[1] if weight_targets is not None else None,
        "count_status": count_status,
        "weight_status": weight_status,
        "minutes_status": minutes_status,
        "feasibility": feasibility,
        "window_state": str(window.get("state") or "default"),
        "over_target": bool(targets is not None and visit > target_max),
        "reason": reason,
    }


def candidate_retrieval_target(constraints: dict) -> int | None:
    """按「每天可用窗口 × 节奏密度目标」推导的候选检索预算（不是质量配额）。

    语义（避免把 page_size 误读成别的东西）：
    - 它不是必去项数量，也不是所有候选的总上限；
    - 它是 assemble_itinerary 每轮「景点」泛搜的 page_size，同时作为
      _trim_hints 的截断配额与路线预留次数的计算基数；
    - 节奏由 _pace_profile 决定（relaxed/balanced/compact 的每天基础分钟数），
      已知出行日期才按 day_window 累加；未知日期返回 None，下游按无密度目标处理。

    出行日期缺失时返回 None：没有日期就没有每天的可用窗口，不能凭默认 09:00–18:00
    编出一个检索预算让下游当成已知。
    """
    dates = _dates(constraints)
    if dates is None:
        return None
    minutes = 0
    for day in dates:
        targets = _density_targets(day_window(day, constraints), constraints)
        if targets is not None:
            minutes += targets[0]
    return max(1, math.ceil(minutes / 90 * 1.3))


def _candidate_supply(days: list[dict], pool: list[dict], placed: set[str], constraints: dict, lodging: dict | None, duration_fn) -> dict:
    profile = _pace_profile(constraints)
    target_min = _PACE_TARGETS[profile][0]
    full_days = [day for day in days if (day.get("density") or {}).get("status") not in {"not_applicable", "unknown"}]
    retrieval_target = candidate_retrieval_target(constraints)
    final_gap = []
    for day in full_days:
        density = day.get("density") or {}
        if density.get("status") != "sparse":
            continue
        final_gap.append({
            "day": day.get("day"),
            "date": day.get("date"),
            "minutes": max(0, int(density.get("target_min") or target_min) - int(density.get("visit_minutes") or 0)),
        })
    dates = _dates(constraints)
    date_unknown = dates is None
    # 日期未知时无法判断候选在出行日是否关闭，usable 只能如实留 None，
    # 不能退回「全部可用」假装已经按日期筛过。
    usable = None if dates is None else [poi for poi in pool if any(not _closed(poi, day, constraints) for day in dates)]
    by_id = {poi["poi_id"]: poi for poi in pool}
    unused = {poi["poi_id"]: poi for poi in (usable or []) if poi.get("poi_id") not in placed}
    support = []
    # 按具体缺口日验证候选：同一候选只能服务一个日期，且必须通过既有可行性模拟。
    for gap in sorted(final_gap, key=lambda row: (-row["minutes"], row.get("day") or 0)):
        day_index = int(gap.get("day") or 0) - 1
        if day_index < 0 or day_index >= len(days):
            continue
        if dates is None or day_index >= len(dates):
            continue
        group = [by_id[item.get("poi_id")] for item in days[day_index].get("items") or []
                 if item.get("role") == "visit" and item.get("poi_id") in by_id]
        when = dates[day_index]
        remaining = int(gap["minutes"])
        accepted: list[str] = []
        for poi in sorted(list(unused.values()), key=lambda item: (-visit_minutes(item), item["name"])):
            if _closed(poi, when, constraints) or _best(group + [poi], when, lodging, constraints, duration_fn) is None:
                continue
            group.append(poi)
            unused.pop(poi["poi_id"], None)
            accepted.append(poi["poi_id"])
            remaining = max(0, remaining - visit_minutes(poi))
            if remaining == 0:
                break
        support.append({**gap, "candidate_ids": accepted, "remaining_minutes": remaining})
    shortage = any(row["remaining_minutes"] > 0 for row in support)
    return {
        "retrieval_target": retrieval_target,
        "usable_candidates": None if usable is None else len(usable),
        "placed_candidates": len(placed),
        "final_gap": final_gap,
        "gap_support": support,
        # 日期未知时没有可评估的缺口，不能据此宣称候选不足。
        "shortage": False if date_unknown else (bool(final_gap) and shortage),
        "date_unknown": date_unknown,
    }


def _hhmm(minute: int | None) -> str | None:
    if minute is None:
        return None
    minute = int(minute)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _clock(text: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def route_cache_key(origin_id: str, dest_id: str, mode: str, day: str, depart_min: int) -> tuple:
    """出发时段按小时分桶。方向、日期、模式不同则不能复用。"""
    return (str(origin_id), str(dest_id), str(mode), str(day), int(depart_min) // 60)


_ROUTE_KEY_SEP = "|"


def encode_route_key(key: tuple) -> str:
    """把 route_cache_key 的元组转成可序列化字符串。全项目唯一编码入口。"""
    return _ROUTE_KEY_SEP.join(str(part) for part in key)


def decode_route_key(text: str) -> tuple | None:
    """把 encode_route_key 的字符串还原为 (origin_id, dest_id, mode, day, depart_hour)。

    键结构固定 5 段：起点、终点、模式、日期、出发小时。格式不符返回 None。
    """
    if not isinstance(text, str):
        return None
    parts = text.split(_ROUTE_KEY_SEP)
    if len(parts) != 5:
        return None
    origin_id, dest_id, mode, day, depart_hour = parts
    if not origin_id or not dest_id or not mode or not day:
        return None
    try:
        hour = int(depart_hour)
    except (TypeError, ValueError):
        return None
    return (origin_id, dest_id, mode, day, hour)


def _dates(constraints: dict) -> list[date] | None:
    """按约束展开出行日期；出行日期未知时返回 None。

    这里不再回退到某个固定日期：编一个假日期会把「日期未知」伪装成已知，
    让窗口、关闭日校验和排程都基于不存在的出行日给出结论。缺日期就该往上
    传递 None，由调用方标记 `date_unknown`，而不是抛异常打断整条链路。
    """
    start = _window_date(constraints.get("date"))
    if start is None:
        return None
    count = max(1, int(constraints.get("days") or 1))
    return [start + timedelta(days=offset) for offset in range(count)]


def _fact_applies(fact: dict, day: date | None) -> bool:
    scope = fact.get("applies_on")
    if scope and day is not None and day.isoformat() not in scope:
        return False
    weeks = fact.get("weekdays")
    if weeks and day is not None and day.weekday() not in weeks:
        return False
    return True


def _hard_facts(constraints: dict, poi_id: str, day: date | None = None) -> list[dict]:
    found = []
    for fact in constraints.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        if fact.get("status") != "hard" or str(fact.get("poi_id") or "") != str(poi_id):
            continue
        if not _fact_applies(fact, day):
            continue
        found.append(fact)
    return found


def _closed(poi: dict, day: date, constraints: dict) -> bool:
    for fact in _hard_facts(constraints, poi.get("poi_id") or poi.get("name"), day):
        kind = fact.get("kind")
        if kind == "closed_weekday" and int(fact.get("value")) == day.weekday():
            return True
        if kind == "closed_date" and str(fact.get("value")) == day.isoformat():
            return True
    return False


def _open_window(poi: dict, day: date, constraints: dict) -> tuple[list[tuple[int, int]], int | None]:
    intervals = [(0, 24 * 60)]
    last_entry = None
    for fact in _hard_facts(constraints, poi.get("poi_id") or poi.get("name"), day):
        if fact.get("kind") == "opening_hours" and fact.get("value"):
            intervals = [(int(a), int(b)) for a, b in fact["value"]]
        elif fact.get("kind") == "last_entry" and fact.get("value") is not None:
            last_entry = int(fact["value"])
    return intervals, last_entry


def _fit_visit(
    start: int,
    minutes: int,
    intervals: list[tuple[int, int]],
    last_entry: int | None,
    window_end: int | None = None,
):
    limit = DAY_END if window_end is None else min(DAY_END, int(window_end))
    for begin, end in intervals:
        slot = max(start, begin)
        if last_entry is not None and slot > last_entry:
            continue
        if slot + minutes <= end and slot + minutes <= limit:
            return slot, slot + minutes
    return None


def _lunch_before(cursor: int, minutes: int, taken: bool) -> tuple[int, bool]:
    if taken or cursor + minutes <= NOON:
        return cursor, False
    # 午后抵达的短窗口不补一顿虚构午餐；午餐只在尚未过午时占用容量。
    if cursor >= NOON:
        return cursor, False
    if cursor >= NOON - LUNCH_MIN:
        return cursor + LUNCH_MIN, True
    if cursor < NOON:
        return NOON + LUNCH_MIN, True
    return cursor + LUNCH_MIN, True


def _window_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def day_window(day: date | None, constraints: dict) -> dict:
    """当天真正可用的时间窗口。

    state=default  没有抵离约束，按 09:00–18:00
    state=constrained  被抵离时刻收窄，start/end 是收窄后的值
    state=unknown  有依据但缺时刻，不能回退成 09:00–18:00
    state=unavailable  当天整段不可用（例如车次次月才到）

    绝不写 `start or DAY_START`：unknown 时 start 就是 None，由调用方决定怎么降级。
    出行日期本身未知（`constraints` 缺 `date`）时没有「首日/末日」可比，也无法判断
    抵离约束落在哪一天，因此整段返回 unknown，并用 `date_unknown` 标出原因；state
    仍只取 default/constrained/unknown/unavailable 四值。
    """
    dates = _dates(constraints)
    if dates is None:
        return {
            "state": "unknown",
            "start": None,
            "end": None,
            "reason": "出行日期未确定，当天可用时段待定",
            "basis": None,
            "date_unknown": True,
        }
    first, last = dates[0], dates[-1]
    rail = constraints.get("rail_window") or {}
    start_state, start, start_reason = "default", DAY_START, ""
    end_state, end, end_reason = "default", DAY_END, ""
    basis = {}

    outbound = rail.get("outbound") or {}
    ostate = outbound.get("state")
    if ostate == "unavailable":
        return {
            "state": "unavailable",
            "start": None,
            "end": None,
            "reason": str(outbound.get("reason") or "当天无法开始"),
            "basis": outbound.get("basis"),
            "date_unknown": False,
        }
    if ostate == "known":
        # 按实际抵达日期判定，而不是绑定「首日」：跨日抵达时，抵达前一天不可游、
        # 抵达当天从抵达时刻起收窄，之后恢复普通窗口。
        arrive_date = _window_date(outbound.get("arrive_date"))
        earliest = outbound.get("earliest_start")
        if arrive_date is not None:
            if day < arrive_date:
                return {
                    "state": "unavailable",
                    "start": None,
                    "end": None,
                    "reason": f"车次 {arrive_date.isoformat()} 才抵达，当天无法安排游览",
                    "basis": outbound.get("basis"),
                    "date_unknown": False,
                }
            if day == arrive_date and earliest is not None:
                start = max(DAY_START, int(earliest))
                start_state = "constrained"
                start_reason = str(outbound.get("reason") or "")
                basis["outbound"] = outbound.get("basis")
    elif ostate == "unknown":
        # 缺抵达时刻：只把首日标 unknown（跨日未知抵达无法精确定位，保守起见不扩散）。
        if day == first:
            start_state, start = "unknown", None
            start_reason = str(outbound.get("reason") or "首日最早开始时间未知")

    inbound = rail.get("inbound") or {}
    istate = inbound.get("state")
    if istate == "known":
        # 按实际发车日期判定，而不是绑定「末日」：返程当天收窄，发车之后不可游。
        depart_date = _window_date(inbound.get("depart_date"))
        latest = inbound.get("latest_end")
        if depart_date is not None:
            if day > depart_date:
                return {
                    "state": "unavailable",
                    "start": None,
                    "end": None,
                    "reason": f"返程车次 {depart_date.isoformat()} 已发车，当天无法安排游览",
                    "basis": inbound.get("basis"),
                    "date_unknown": False,
                }
            if day == depart_date and latest is not None:
                end = min(DAY_END, int(latest))
                end_state = "constrained"
                end_reason = str(inbound.get("reason") or "")
                basis["inbound"] = inbound.get("basis")
    elif istate == "unknown":
        # 缺发车时刻：只把末日标 unknown。
        if day == last:
            end_state, end = "unknown", None
            end_reason = str(inbound.get("reason") or "末日最晚结束时间未知")

    if start_state == "unknown" or end_state == "unknown":
        state = "unknown"
    elif start_state == "constrained" or end_state == "constrained":
        state = "constrained"
    else:
        state = "default"

    if state != "unknown":
        if start is not None and end is not None and start >= end:
            return {
                "state": "unavailable",
                "start": None,
                "end": None,
                "reason": f"可用窗口不足（{_hhmm(start)}–{_hhmm(end)}）",
                "basis": basis,
                "date_unknown": False,
            }

    reason = "；".join(part for part in (start_reason, end_reason) if part)
    return {
        "state": state,
        "start": start,
        "end": end,
        "reason": reason,
        "basis": basis,
        "date_unknown": False,
    }


def _mode_and_seconds(origin: dict, dest: dict, day: str, depart: int, mode_pref: str, duration_fn) -> tuple[str, int | None]:
    if mode_pref == "driving":
        mode = "driving"
        return mode, duration_fn(origin["poi_id"], dest["poi_id"], mode, day, depart)
    if mode_pref == "walking":
        mode = "walking"
        return mode, duration_fn(origin["poi_id"], dest["poi_id"], mode, day, depart)
    walk = duration_fn(origin["poi_id"], dest["poi_id"], "walking", day, depart)
    if walk is not None and walk <= WALK_MAX_SECONDS:
        return "walking", walk
    return "transfer", duration_fn(origin["poi_id"], dest["poi_id"], "transfer", day, depart)


def _mask_after_unknown(items: list[dict], legs: list[dict], has_lodging: bool) -> None:
    """第一段未知耗时之后不再展示到达时刻。前面已经确定的出发时刻可以保留。"""
    index = next((i for i, leg in enumerate(legs) if leg.get("duration_seconds") is None), None)
    if index is None:
        return
    dest = index + 1 if has_lodging else index
    for item in items[dest:]:
        item["start"] = None
        item["end"] = None


def _blank(day_plan: dict) -> dict:
    _mask_after_unknown(day_plan["items"], day_plan["legs"], bool(day_plan["items"] and day_plan["items"][0].get("role") == "depart"))
    day_plan["unverified"] = True
    day_plan["notes"] = [note for note in day_plan["notes"] if not re.search(r"\d{1,2}:\d{2}", note)]
    day_plan["notes"].append("未知交通之后不提供精确到达时间，不能据此确认是否赶上最晚入场")
    return day_plan


def _simulate(group: list[dict], lodging: dict | None, day: date, constraints: dict, duration_fn) -> dict | None:
    if any(_closed(poi, day, constraints) for poi in group):
        return None
    window = day_window(day, constraints)
    if window["state"] == "unavailable":
        return None
    # 未知抵离时刻不能回退成 09:00 当成已核实；内部仍按默认上下限估可行性，但整天标 unverified。
    clock_unknown = window["state"] == "unknown"
    day_start = window["start"] if window["start"] is not None else DAY_START
    day_end = window["end"] if window["end"] is not None else DAY_END
    mode_pref = str(constraints.get("transport_mode") or "public_transit")
    day_text = day.isoformat()
    items: list[dict] = []
    legs: list[dict] = []
    notes: list[str] = []
    unverified = False
    cursor = day_start
    lunch = False
    prev = lodging
    origin = lodging if lodging is not None else (group[0] if group else None)
    if origin is None:
        return None

    def add_leg(dest: dict, switch: int) -> int | None:
        nonlocal unverified, cursor
        depart = cursor
        # 同一地点间的空跑是占位，不产生真实路段，也不占用覆盖率统计。
        if str(prev.get("poi_id") or "") == str(dest.get("poi_id") or ""):
            return depart
        mode, seconds = _mode_and_seconds(prev, dest, day_text, depart, mode_pref, duration_fn)
        legs.append(
            {
                "origin_id": prev["poi_id"],
                "dest_id": dest["poi_id"],
                "mode": mode,
                "duration_seconds": seconds,
                "depart": _hhmm(depart),
                # 当前路线接口不带日期/时段，取得结果也只能算「参考」，不能冒充已核实。
                "confidence": "reference" if isinstance(seconds, int) and seconds > 0 else "unknown",
            }
        )
        if seconds is None:
            unverified = True
            return None
        cursor = depart + switch + seconds // 60
        return cursor

    if lodging is not None:
        items.append(_point(lodging, "depart", day_start, day_start))
    for index, poi in enumerate(group):
        switch = 0 if index == 0 else SWITCH_MIN
        if prev is None and not (lodging is None and index == 0):
            return None
        arrived = add_leg(poi, switch) if lodging is not None or index else None
        if lodging is None and index == 0:
            arrived = day_start
            cursor = day_start
        if arrived is None and not (lodging is None and index == 0):
            items.append(_point(poi, "visit", None, None, visit_minutes(poi)))
            prev = poi
            continue
        minutes = visit_minutes(poi)
        intervals, last_entry = _open_window(poi, day, constraints)
        start_at, took = _lunch_before(cursor, minutes, lunch)
        if took:
            lunch = True
            notes.append(f"午餐 {_hhmm(cursor)}–{_hhmm(start_at)}")
            cursor = start_at
        fitted = _fit_visit(cursor, minutes, intervals, last_entry, day_end)
        if fitted is None:
            if unverified:
                items.append(_point(poi, "visit", None, None, minutes))
                prev = poi
                continue
            return None
        start, end = fitted
        items.append(_point(poi, "visit", start, end, minutes))
        cursor = end
        prev = poi
    if lodging is not None and prev is not None:
        if not lunch and not unverified:
            notes.append(f"午餐 {_hhmm(cursor)}–{_hhmm(cursor + LUNCH_MIN)}")
            cursor += LUNCH_MIN
            lunch = True
        back = add_leg(lodging, SWITCH_MIN if group else 0)
        end_at = back if back is not None else None
        items.append(_point(lodging, "return", end_at, end_at))
        if back is not None and back > day_end - SLACK_MIN:
            return None
    if lodging is None and cursor > day_end - SLACK_MIN:
        return None
    if group and _known_load_exceeds(group, legs, lodging is not None, window=window):
        return None
    if clock_unknown:
        # 抵离时刻未知时不能把 09:00–18:00 当成已核实，整天改成「时刻待定」。
        for item in items:
            item["start"] = None
            item["end"] = None
        unverified = True
        notes.append("首末抵离时刻未核实，当天时刻只是可行性估计，不是已核实安排")
    plan = {
        "date": day_text,
        "items": items,
        "legs": legs,
        "notes": notes,
        "unverified": unverified,
        "skip": not group,
        "window": window,
    }
    if unverified:
        return _blank(plan)
    return plan


def _known_load_exceeds(group: list[dict], legs: list[dict], with_lodging: bool, *, window: dict | None = None) -> bool:
    """已知交通、游览、切换、午餐和机动已经超出当天时，未知路段不能把它留在主路线。"""
    window = window or {}
    if window.get("state") == "unknown":
        return False
    start = window.get("start") if window.get("start") is not None else DAY_START
    end = window.get("end") if window.get("end") is not None else DAY_END
    visit = sum(visit_minutes(poi) for poi in group)
    travel = sum(int(leg["duration_seconds"]) // 60 for leg in legs if isinstance(leg.get("duration_seconds"), int))
    switches = SWITCH_MIN * (max(0, len(legs) - 1) if with_lodging else len(legs))
    # 下午抵达或上午返程的小窗口不应凭空扣一顿午餐；跨午间的完整窗口才预留它。
    lunch = LUNCH_MIN if start < NOON < end else 0
    return start + visit + travel + switches + lunch > end - SLACK_MIN


def _point(poi: dict, role: str, start: int | None, end: int | None, minutes: int | None = None) -> dict:
    item = {
        "poi_id": poi.get("poi_id") or poi.get("name"),
        "name": poi.get("name") or "",
        "role": role,
        "lng": poi.get("lng"),
        "lat": poi.get("lat"),
        "start": _hhmm(start),
        "end": _hhmm(end),
    }
    # 住宿精度（exact/area/unresolved）与类型贯穿到行程点，正文和地图任务备注都要读它。
    if poi.get("lodging_kind") is not None:
        item["lodging_kind"] = poi.get("lodging_kind")
    if poi.get("precision") is not None:
        item["precision"] = poi.get("precision")
    # area 锚点的 name 是用户原话（如「上海南站附近」），poi_name 才是检索到的地名；
    # 正文要写「以上海南站为住宿区域参考点」，必须把 poi_name 一起带上。
    if poi.get("poi_name"):
        item["poi_name"] = poi.get("poi_name")
    if minutes is not None:
        item["visit_minutes"] = minutes
        item["visit_duration_source"] = poi.get("visit_duration_source", "planner_estimate")
        item["visit_duration_confidence"] = poi.get("visit_duration_confidence", "estimated")
        item["activity_weight"] = float(poi.get("activity_weight") or activity_weight(poi))
        item["activity_weight_source"] = poi.get("activity_weight_source", "duration_rule")
        item["activity_size"] = poi.get("activity_size") or _activity_size(item["activity_weight"])
    return item


def _best(group: list[dict], day: date, lodging: dict | None, constraints: dict, duration_fn):
    if not group:
        window = day_window(day, constraints)
        return {
            "date": day.isoformat(),
            "items": [],
            "legs": [],
            "notes": [],
            "unverified": window["state"] in {"unknown", "unavailable"},
            "skip": True,
            "window": window,
        }
    origin = lodging if lodging is not None else group[0]
    if len(group) > _PER_DAY:
        ordered = _nearest(group, origin)
        return _simulate(ordered, lodging, day, constraints, duration_fn)
    angle_names = [poi["name"] for poi in sorted(group, key=lambda poi: (_angle(poi, origin), poi["name"]))]
    ranked = []
    for perm in itertools.permutations(group):
        sim = _simulate(list(perm), lodging, day, constraints, duration_fn)
        if sim is None:
            continue
        unknown = sum(1 for leg in sim["legs"] if leg.get("duration_seconds") is None)
        cost = sum(int(leg["duration_seconds"]) for leg in sim["legs"] if leg.get("duration_seconds") is not None)
        names = [poi["name"] for poi in perm]
        ranked.append((unknown, cost, 0 if names == angle_names else 1, names, sim))
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[:4])
    return ranked[0][4]


def _best_with_reason(group: list[dict], day: date, lodging: dict | None, constraints: dict, duration_fn) -> tuple[dict | None, dict | None]:
    """保守解释排程失败；无法由已知事实证明时返回 unknown_feasibility。"""
    if any(_closed(poi, day, constraints) for poi in group):
        return None, {"reason_code": "opening_conflict", "reason": "景点在当天关闭或没有适用开放规则"}
    window = day_window(day, constraints)
    if window.get("state") == "unavailable":
        return None, {"reason_code": "return_window_too_short", "reason": str(window.get("reason") or "当天可用窗口不足")}
    effective = _effective_window_minutes(window)
    if effective is not None and any(visit_minutes(poi) > effective for poi in group):
        return None, {"reason_code": "visit_time_exceeded", "reason": "单个活动时长已超过当天有效游览窗口"}
    unknown = False
    known_travel = 0
    previous = lodging or (group[0] if group else None)
    for poi in (group if lodging is not None else group[1:]):
        if previous is None:
            previous = poi
            continue
        mode = str(constraints.get("transport_mode") or "public_transit")
        seconds = duration_fn(previous["poi_id"], poi["poi_id"], "walking" if mode == "walking" else mode, day.isoformat(), DAY_START)
        if not isinstance(seconds, int) or seconds <= 0:
            unknown = True
        else:
            known_travel += seconds // 60
        previous = poi
    if effective is not None and not unknown and known_travel + sum(visit_minutes(poi) for poi in group) > effective:
        return None, {"reason_code": "travel_too_long", "reason": "已知游览和交通时间超过当天有效游览窗口"}
    if unknown:
        return None, {"reason_code": "unknown_travel", "reason": "交通时间尚未核实，无法确认是否放得下"}
    return None, {"reason_code": "unknown_feasibility", "reason": "已知条件不足以解释排程失败"}


_LODGING_TYPE = "住宿服务"
_STATION_TYPE = "交通设施服务"


def _plain_core(text: str) -> str:
    """去掉括号补充后的核心名，用于别名比对。"""
    return re.sub(r"[（(][^）)]*[）)]?", "", str(text or "")).replace("）", "").replace(")", "").strip()


def _is_lodging(poi: dict) -> bool:
    return _LODGING_TYPE in str(poi.get("place_type") or "") or poi.get("role") in {"lodging", "lodging_area"}


def _is_station(poi: dict) -> bool:
    return _STATION_TYPE in str(poi.get("place_type") or "") or poi.get("role") == "station"


def _named_must(poi: dict, names: set[str]) -> bool:
    """必去只认用户点名的名称或别名，候选自带的 must_visit 不算。"""
    if poi["name"] in names:
        return True
    asked = str(poi.get("asked_name") or "").strip()
    return bool(asked) and (asked in names or _plain_core(asked) in names)


def sanitize_candidates(candidates: list[dict], constraints: dict) -> list[dict]:
    """角色过滤集中在这里：先认用户必去，再排住宿与接驳端点，最后通用过滤。"""
    names = {str(name).strip() for name in (constraints.get("must_visit") or []) if str(name).strip()}
    avoid = {str(name).strip() for name in (constraints.get("avoid") or []) if str(name).strip()}
    lodging = constraints.get("lodging_poi")
    lodging_id = str(lodging.get("poi_id") or "") if isinstance(lodging, dict) else ""
    seen: set[str] = set()
    pool: list[dict] = []
    for raw in candidates:
        poi = _mark(raw, constraints)
        if not poi["name"] or poi["name"] in seen or poi["name"] in avoid:
            continue
        seen.add(poi["name"])
        if poi["must_visit"]:
            pool.append(poi)
            continue
        # 住宿地、住宿接驳与车站只是锚点，不是游览项；用户点名的例外。
        if _is_lodging(poi) or _is_station(poi):
            continue
        if lodging_id and str(poi.get("poi_id") or "") == lodging_id:
            continue
        pool.append(poi)
    return pool


def _mark(poi: dict, constraints: dict) -> dict:
    item = dict(poi)
    item["poi_id"] = str(item.get("poi_id") or item.get("name") or "")
    item["name"] = str(item.get("name") or "").strip()
    names = {str(name).strip() for name in (constraints.get("must_visit") or []) if str(name).strip()}
    item["must_visit"] = _named_must(item, names)
    return item


_ARRIVAL_ANCHOR_NOTE = "抵达后按区域参考点接驳，具体安排待定"


def _note_arrival_anchor(sim: dict, day: date, lodging: dict | None, constraints: dict) -> None:
    """抵达日被车次收窄、住宿又只有 area 精度的，接驳只能写成待定，不能写成已经安排好了。

    抵达日按车次实际抵达日期判定（跨日抵达时是第二天），不是固定第一天。
    """
    window = sim.get("window") or {}
    if str(window.get("state") or "") != "constrained":
        return
    if str((lodging or {}).get("precision") or "") != "area":
        return
    outbound = (constraints.get("rail_window") or {}).get("outbound") or {}
    arrive_date = _window_date(outbound.get("arrive_date"))
    if arrive_date is None or arrive_date != day:
        return
    notes = list(sim.get("notes") or [])
    if _ARRIVAL_ANCHOR_NOTE not in notes:
        notes.append(_ARRIVAL_ANCHOR_NOTE)
    sim["notes"] = notes


def _prepare(candidates: list[dict], constraints: dict) -> tuple[list[dict], dict | None]:
    pool = []
    for poi in sanitize_candidates(candidates, constraints):
        try:
            poi["lng"] = float(poi["lng"])
            poi["lat"] = float(poi["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        _duration_metadata(poi, constraints)
        weight = activity_weight(poi)
        poi["activity_weight"] = weight
        poi["activity_weight_source"] = "category_rule" if str(poi.get("place_type") or "").strip() else "duration_rule"
        poi["activity_size"] = _activity_size(weight)
        pool.append(poi)
    lodging = constraints.get("lodging_poi")
    if isinstance(lodging, dict) and lodging.get("lng") is not None:
        stay = _mark({**lodging, "must_visit": False}, constraints)
        stay["poi_id"] = str(stay.get("poi_id") or "lodging")
        stay["name"] = str(stay.get("name") or "住宿地")
        stay["lng"] = float(stay["lng"])
        stay["lat"] = float(stay["lat"])
    else:
        stay = None
    return pool, stay


def _date_unknown_schedule(pool: list[dict], lodging: dict | None, constraints: dict, duration_fn, dropped: list[dict], conflicts: list[dict]) -> dict:
    """出行日期未知时的占位排程：保留天数结构，但不假装已经排好。

    不能返回空 days：正文、地图和校验都按 days 生成「第N天」，没有日期时仍要说清
    「有几天、每天待定」。也不能把候选塞进去，否则等于用不存在的出行日做了可行性判断。
    """
    count = max(1, int(constraints.get("days") or 1))
    window = day_window(None, constraints)
    days = []
    for index in range(1, count + 1):
        day = {
            "day": index,
            "date": None,
            "label": f"第{index}天",
            "items": [],
            "legs": [],
            "notes": ["出行日期未确定，暂时无法判断开放时间、抵离窗口和交通耗时"],
            "unverified": True,
            "skip": True,
            "arrival_only": False,
            "window": dict(window),
            "day_kind": "unknown",
        }
        day["density"] = _density(day, constraints)
        days.append(day)
    for poi in pool:
        # 没有日期就没法排，但要说清是「未排」而不是「关闭」或「冲突」。
        dropped.append({
            "poi_id": poi.get("poi_id"),
            "name": poi.get("name") or "",
            "reason": "出行日期未确定，无法判断当天是否开放",
        })
    candidate_supply = _candidate_supply(days, pool, set(), constraints, lodging, duration_fn)
    notes = ["出行日期未确定，行程只在确定日期后才能真正排程。"]
    for item in dropped:
        notes.append(f"未安排{item['name']}：{item['reason']}")
    for item in conflicts:
        notes.append(f"必去项无法安排{item['name']}：{item['reason']}")
    return {
        "days": days,
        "dropped": dropped,
        "conflicts": conflicts,
        "schedule_status": "date_unknown",
        "notes": notes,
        "assignment": [[] for _ in days],
        "candidate_supply": candidate_supply,
    }


def schedule(
    candidates: list[dict],
    constraints: dict,
    duration_fn,
    *,
    corrections: int = 2,
    assignment: list[list[str]] | None = None,
    kept_dropped: list[dict] | None = None,
    kept_conflicts: list[dict] | None = None,
    withdraw_first: set[str] | None = None,
) -> dict:
    """纯排程。duration_fn(origin_id, dest_id, mode, date, depart_min) -> 秒或 None。不发 HTTP。

    出行日期缺失时不抛异常，也不编造日期：返回 `schedule_status="date_unknown"`
    的占位行程（天数为 `constraints["days"]`），每天 `date=None`，窗口是 unknown。
    这样正文、地图和校验拿到的都是「日期待定」，而不是一份看似已排好的行程。
    """
    pool, lodging = _prepare(candidates, constraints)
    by_id = {poi["poi_id"]: poi for poi in pool}
    dates = _dates(constraints)
    dropped: list[dict] = [dict(row) for row in (kept_dropped or [])]
    conflicts: list[dict] = [dict(row) for row in (kept_conflicts or [])]
    if dates is None:
        return _date_unknown_schedule(pool, lodging, constraints, duration_fn, dropped, conflicts)
    if assignment is not None:
        groups = [[by_id[poi_id] for poi_id in day_ids if poi_id in by_id] for day_ids in assignment]
        while len(groups) < len(dates):
            groups.append([])
        corrections = 0
    else:
        groups = _initial_groups(pool, dates, lodging, constraints, conflicts, dropped, duration_fn)
    groups, dropped, conflicts = _relieve(groups, dates, lodging, constraints, duration_fn, corrections, dropped, conflicts)
    days = []
    assignment_rows: list[list[str]] = []
    for index, (day, group) in enumerate(zip(dates, groups), 1):
        sim = _simulate(group, lodging, day, constraints, duration_fn) if assignment is not None else _best(group, day, lodging, constraints, duration_fn)
        if sim is None and assignment is not None:
            pending = list(group)
            while pending:
                pool = [poi for poi in pending if not poi.get("must_visit")] or pending
                supplemental = [poi for poi in pool if poi["poi_id"] in (withdraw_first or set())]
                if supplemental:
                    pool = supplemental
                victim = max(
                    pool,
                    key=lambda poi: (_dist2(poi, lodging), poi["name"]) if lodging is not None else (0, poi["name"]),
                )
                pending.remove(victim)
                bucket = conflicts if victim.get("must_visit") else dropped
                bucket.append({"poi_id": victim["poi_id"], "name": victim["name"], "reason": "已知耗时放不下"})
                sim = _simulate(pending, lodging, day, constraints, duration_fn) if pending else None
                if sim is not None and not sim.get("skip"):
                    break
                sim = None
        if sim is None:
            # 这一天排不出可执行方案，但日期本身必须在行程里：三日游只输出两天，
            # 正文和地图的日期就会和 constraints 对不上。用占位 day 走下面的 skip 分支。
            if assignment is None:
                for poi in group:
                    bucket = conflicts if poi.get("must_visit") else dropped
                    bucket.append({"poi_id": poi["poi_id"], "name": poi["name"], "reason": "最终检查未通过"})
            sim = {
                "date": day.isoformat(),
                "items": [],
                "legs": [],
                "notes": [],
                "unverified": True,
                "skip": True,
                "window": day_window(day, constraints),
                "dropped_visits": [{"poi_id": poi["poi_id"], "name": poi["name"]} for poi in group],
            }
        if sim.get("skip"):
            # 空天不能直接丢掉：三日游只展示两天会让日期和地图对不上。
            window = sim.get("window") or day_window(day, constraints)
            arrival_only = window["state"] in {"constrained", "unavailable"}
            sim["day"] = index
            sim["date"] = day.isoformat()
            sim["label"] = f"第{index}天"
            sim["arrival_only"] = bool(arrival_only)
            sim["window"] = window
            sim["day_kind"] = classify_day_kind(day, dates, constraints.get("rail_window"), constraints.get("plan_basis"))
            sim.setdefault("items", [])
            sim.setdefault("legs", [])
            sim["notes"] = list(sim.get("notes") or [])
            if window["state"] == "unknown":
                sim["notes"].append("当天没有排入景点，抵离时刻也还没核实")
            elif arrival_only:
                # 不可用时先把原因写清（例如「车次 2026-11-08 才抵达」），再说明当天只用于抵离；
                # 不能写成「从住宿地出发」这类当天并不存在的活动。
                reason = str(window.get("reason") or "").strip()
                if reason:
                    sim["notes"].append(reason)
                sim["notes"].append("当天主要用于抵离，窗口不足以安排游览")
            else:
                sim["notes"].append("当天没有排入景点")
            if window["state"] == "unavailable":
                sim["unverified"] = True
            _note_arrival_anchor(sim, day, lodging, constraints)
            days.append(sim)
            assignment_rows.append([])
            continue
        sim["day"] = index
        sim["date"] = day.isoformat()
        sim["label"] = f"第{index}天"
        sim["window"] = sim.get("window") or day_window(day, constraints)
        sim["day_kind"] = classify_day_kind(day, dates, constraints.get("rail_window"), constraints.get("plan_basis"))
        _note_arrival_anchor(sim, day, lodging, constraints)
        days.append(sim)
        assignment_rows.append([item["poi_id"] for item in sim["items"] if item.get("role") == "visit"])
    # 全程一个 visit 都没排进来时也保留按日期生成的空天：清空 days 会让「三日游」
    # 变成零天，正文、地图和校验都拿不到日期。空天的语义由 schedule_status 表达。
    placed = {poi_id for row in assignment_rows for poi_id in row}
    recorded = {row.get("poi_id") for row in conflicts}
    for poi in pool:
        if not poi.get("must_visit") or poi["poi_id"] in placed or poi["poi_id"] in recorded:
            continue
        conflicts.append({"poi_id": poi["poi_id"], "name": poi["name"], "reason": "最终核对未安排"})
    placed_names = {
        item["name"]
        for day in days
        for item in day["items"]
        if item.get("role") == "visit" and item.get("name")
    }
    known_names = {row.get("name") for row in conflicts + dropped}
    handled_ids = {row.get("poi_id") for row in conflicts + dropped} | placed
    satisfied = set(placed_names)
    for poi in pool:
        asked = str(poi.get("asked_name") or "").strip()
        if not asked:
            continue
        if poi["poi_id"] in placed or poi["name"] in placed_names:
            satisfied.add(asked)
        if poi["poi_id"] in handled_ids:
            known_names.add(asked)
    avoided = {str(name).strip() for name in (constraints.get("avoid") or []) if str(name).strip()}
    for raw in constraints.get("must_visit") or []:
        name = str(raw).strip()
        if not name or name in satisfied or name in known_names or name in avoided:
            continue
        conflicts.append({"poi_id": name, "name": name, "reason": "未能定位"})
        known_names.add(name)
    for day in days:
        day["density"] = _density(day, constraints)
    candidate_supply = _candidate_supply(days, pool, placed, constraints, lodging, duration_fn)
    unverified = any(day.get("unverified") for day in days) or bool(constraints.get("unverified_clock"))
    missing_fact = _missing_hard(days, constraints)
    has_visit = bool(placed)
    # 排程完成状态只按「关键事实是否核实、必去项是否排入」判定，规则表：
    #   conflict                  必去项无法排入
    #   date_unknown              出行日期未知（_date_unknown_schedule 已提前返回）
    #   candidate                 关键事实未核实、缺硬开放规则或没有可排景点
    #   checked_under_assumptions 硬事实已满足，但仍存在规划假设（默认窗口/午餐等）
    #   verified                  当前项目不产出，避免宣称完全核实
    # 密度 sparse 与候选检索预算不足只是「规划假设/候选覆盖诊断」，照旧写进
    # candidate_supply 与 schedule_details 的告警，不能把硬事实已满足的排程
    # 从 checked_under_assumptions 压成 candidate。
    if conflicts:
        status = "conflict"
    elif unverified or missing_fact or not has_visit:
        status = "candidate"
    else:
        status = "checked_under_assumptions"
    notes = []
    if constraints.get("unverified_clock"):
        notes.append("用户原话中的具体时刻尚未纳入结构化时间校验。")
    notes.append("默认每日 09:00–18:00，午餐 60 分钟，景点切换 15 分钟，机动 30 分钟。")
    for item in dropped:
        notes.append(f"未安排{item['name']}：{item['reason']}")
    for item in conflicts:
        notes.append(f"必去项无法安排{item['name']}：{item['reason']}")
    for day in days:
        for item in day["items"]:
            if item.get("role") != "visit":
                continue
            if not _hard_facts(constraints, item["poi_id"], _window_date(day.get("date"))):
                day["notes"].append(f"{item['name']}开放规则未核实")
    return {
        "days": days,
        "dropped": dropped,
        "conflicts": conflicts,
        "schedule_status": status,
        "notes": notes,
        "assignment": assignment_rows,
        "candidate_supply": candidate_supply,
    }


def _missing_hard(days: list[dict], constraints: dict) -> bool:
    for day in days:
        # 日期未知时 _window_date 返回 None，_fact_applies 会跳过日期维度，
        # 只按 POI 判定开放规则是否存在，不去猜一个不存在的出行日。
        when = _window_date(day.get("date"))
        for item in day["items"]:
            if item.get("role") == "visit" and not _hard_facts(constraints, item["poi_id"], when):
                return True
    return False


def _initial_groups(pool, dates, lodging, constraints, conflicts, dropped, duration_fn):
    groups: list[list[dict]] = [[] for _ in dates]
    pinned_ids: set[str] = set()
    for poi in pool:
        open_days = [index for index, day in enumerate(dates) if not _closed(poi, day, constraints)]
        if not open_days:
            bucket = conflicts if poi.get("must_visit") else dropped
            bucket.append({"poi_id": poi["poi_id"], "name": poi["name"], "reason": "出行日期内关闭"})
            continue
        if poi.get("must_visit") and len(open_days) < len(dates):
            groups[open_days[0]].append(poi)
            pinned_ids.add(poi["poi_id"])
    rest = [poi for poi in pool if poi["poi_id"] not in pinned_ids and not any(poi["poi_id"] == row["poi_id"] for row in dropped + conflicts)]
    # 先处理耗时长的主活动，随后每次优先补足明显过疏的日期；住宿只在当天没有
    # 主活动时作为距离锚点。未知路线不按零分钟奖励。
    rest.sort(key=lambda poi: (-visit_minutes(poi), poi["name"]))
    def group_anchor(group: list[dict]) -> dict | None:
        if not group:
            return lodging
        # 用组内到其他点平均距离最小的点代表片区，不能让候选加入顺序决定片区锚点。
        return min(group, key=lambda item: (sum(_dist2(item, other) for other in group), item["name"]))

    for poi in rest:
        placed = False
        open_days = [
            index
            for index, day in enumerate(dates)
            if not _closed(poi, day, constraints)
            and _best(groups[index] + [poi], day, lodging, constraints, duration_fn) is not None
        ]
        if open_days:
            def rank(index: int) -> tuple:
                group = groups[index]
                load = sum(visit_minutes(item) for item in group)
                targets = _density_targets(day_window(dates[index], constraints), constraints)
                window = day_window(dates[index], constraints)
                kind = classify_day_kind(dates[index], dates, constraints.get("rail_window"), constraints.get("plan_basis"))
                count_targets = _count_targets(kind, window, constraints)
                count_gap = max(0, (count_targets[0] if count_targets else 0) - len(group) - 1)
                weight_targets = _PACE_WEIGHT_TARGETS[_pace_profile(constraints)] if count_targets is not None and kind == "full" else None
                projected_weight = sum(activity_weight(item) for item in group) + activity_weight(poi)
                weight_gap = max(0.0, (weight_targets[0] if weight_targets else 0.0) - projected_weight)
                # 未知抵离窗口不拿完整日目标去填满；已有可用日优先消除加入后的缺口。
                if targets is None:
                    target_min, target_max = (0, math.inf)
                    day_kind = 1
                else:
                    target_min, target_max = targets
                    day_kind = 0
                projected = load + visit_minutes(poi)
                remaining_gap = max(0, target_min - projected)
                over_target = projected > target_max
                anchor = group_anchor(group)
                distance = _dist2(anchor, poi) if anchor is not None else 0
                known_seconds = None
                if anchor is not None and anchor.get("poi_id"):
                    mode = str(constraints.get("transport_mode") or "public_transit")
                    if mode == "public_transit":
                        # 排程缓存将市内公交拆成 transfer / walking；没有其中任一条时
                        # 仍保持未知，不能让 public_transit 这个抽象槽位变成 0 分钟。
                        known_seconds = duration_fn(anchor["poi_id"], poi["poi_id"], "transfer", dates[index].isoformat(), DAY_START)
                        if known_seconds is None:
                            known_seconds = duration_fn(anchor["poi_id"], poi["poi_id"], "walking", dates[index].isoformat(), DAY_START)
                    else:
                        known_seconds = duration_fn(anchor["poi_id"], poi["poi_id"], mode, dates[index].isoformat(), DAY_START)
                # 缺口优先于片区相近：同样能放下时，先避免一个完整日保持空白；
                # 随后才在同片区、已有路线信息和稳定日期间打破平局。
                empty_group = 0 if not group else 1
                return (day_kind, empty_group, count_gap, -weight_gap, -remaining_gap, over_target, distance, known_seconds is None, known_seconds or 0, projected, index)

            groups[min(open_days, key=rank)].append(poi)
            placed = True
        if not placed:
            bucket = conflicts if poi.get("must_visit") else dropped
            explanation = None
            for index, day in enumerate(dates):
                if _closed(poi, day, constraints):
                    continue
                _, explanation = _best_with_reason(groups[index] + [poi], day, lodging, constraints, duration_fn)
                if explanation and explanation.get("reason_code") != "unknown_feasibility":
                    break
            explanation = explanation or {"reason_code": "unknown_feasibility", "reason": "已知条件不足以解释排程失败"}
            bucket.append({
                "poi_id": poi["poi_id"],
                "name": poi["name"],
                "reason": explanation["reason"],
                "reason_code": explanation["reason_code"],
                "attempt": "normal_assignment",
            })
    return groups


def _relieve(groups, dates, lodging, constraints, duration_fn, rounds, dropped, conflicts):
    for _ in range(max(0, rounds)):
        moved = False
        for index, group in enumerate(groups):
            if _best(group, dates[index], lodging, constraints, duration_fn) is not None:
                continue
            optional = [poi for poi in group if not poi.get("must_visit")]
            if optional and lodging is not None:
                victim = max(optional, key=lambda poi: (_dist2(poi, lodging), poi["name"]))
            elif optional:
                victim = optional[-1]
            else:
                victim = None
            if victim is not None:
                group.remove(victim)
                placed = False
                for other_index, other in enumerate(groups):
                    if other_index == index or _closed(victim, dates[other_index], constraints):
                        continue
                    if _best(other + [victim], dates[other_index], lodging, constraints, duration_fn) is not None:
                        other.append(victim)
                        placed = True
                        break
                if not placed:
                    dropped.append({"poi_id": victim["poi_id"], "name": victim["name"], "reason": "时间不足"})
                moved = True
                break
            for must in [poi for poi in group if poi.get("must_visit")]:
                shifted = False
                for other_index, other in enumerate(groups):
                    if other_index == index or _closed(must, dates[other_index], constraints):
                        continue
                    remain = [poi for poi in group if poi is not must]
                    if _best(other + [must], dates[other_index], lodging, constraints, duration_fn) is None:
                        continue
                    if remain and _best(remain, dates[index], lodging, constraints, duration_fn) is None:
                        continue
                    group.remove(must)
                    other.append(must)
                    shifted = True
                    break
                if shifted:
                    moved = True
                    break
                group.remove(must)
                conflicts.append({"poi_id": must["poi_id"], "name": must["name"], "reason": "开放日或时间放不下"})
                moved = True
                break
            if moved:
                break
        if not moved:
            break
    return groups, dropped, conflicts


def days_to_tasks(plan: dict, city: str) -> list[dict]:
    tasks = []
    for day in plan.get("days") or []:
        items = [item for item in day.get("items") or [] if item.get("lng") is not None and item.get("name")]
        prev = None
        for item in items:
            if prev is not None:
                leg = _match_leg(day.get("legs") or [], prev, item)
                tasks.append(
                    {
                        "type": "route",
                        "day": int(day["day"]),
                        "routeType": (leg or {}).get("mode") or "transfer",
                        "start": [prev["lng"], prev["lat"]],
                        "end": [item["lng"], item["lat"]],
                        "city": city,
                        "remark": f"{prev['name']}→{item['name']}",
                    }
                )
            remark = ""
            if item.get("role") == "depart":
                remark = _lodging_remark(item, "从住宿地出发")
            elif item.get("role") == "return":
                remark = _lodging_remark(item, "返回住宿地")
            tasks.append(
                {
                    "type": "poi",
                    "day": int(day["day"]),
                    "lnglat": [item["lng"], item["lat"]],
                    "sort": f"Day{day['day']}",
                    "text": item["name"],
                    "remark": remark,
                }
            )
            prev = item
    return tasks


def _match_leg(legs: list[dict], origin: dict, dest: dict) -> dict | None:
    for leg in legs:
        if leg.get("origin_id") == origin.get("poi_id") and leg.get("dest_id") == dest.get("poi_id"):
            return leg
    return None


def _lodging_remark(item: dict, fallback: str) -> str:
    """按住宿精度写地图任务备注：exact 用住宿地，area 用「区域参考起点」，未定位用「按目的地中心暂代」。"""
    kind = str(item.get("lodging_kind") or "")
    precision = str(item.get("precision") or "")
    if kind in {"area", "unresolved"} or precision in {"area", "fallback"}:
        return "区域参考起点"
    if kind == "none" and precision == "fallback":
        return "按目的地中心暂代"
    return fallback


def _window_text(window: dict | None) -> str:
    """把当天窗口写成一句话。未知就说未知，绝不回退成默认值冒充已核实。"""
    window = window or {}
    state = window.get("state")
    if state == "unavailable":
        return str(window.get("reason") or "当天不可用")
    if state == "unknown":
        return "抵离时刻未核实，当天可用时段待定"
    start, end = window.get("start"), window.get("end")
    if start is None or end is None:
        return ""
    return f"{_hhmm(start)}–{_hhmm(end)}"


def _lodging_anchor_note(days: list[dict]) -> str:
    """按住宿锚点精度写一句总述：area 说明只是区域参考，unresolved 说明参考点没建立。

    只能从 depart 行程点里读：exact 是明确酒店，无需补话；住宿完全没锚点时
    连 depart 点都不会生成，这里返回空串，交给告警与提醒去表达。
    """
    area = ""
    unresolved = False
    for day in days:
        for item in day.get("items") or []:
            if item.get("role") != "depart":
                continue
            kind = str(item.get("lodging_kind") or "")
            precision = str(item.get("precision") or "")
            # area 锚点的 name 是用户原话（「上海南站附近」），poi_name 是检索到的地名
            # （「上海南站」）；正文引用地名更清楚，取不到再退回原话。
            name = str(item.get("poi_name") or item.get("name") or "").strip()
            if precision == "area" or kind == "area":
                area = area or name
            elif precision == "fallback" or kind == "unresolved":
                unresolved = True
    if area:
        return f"以{area}为住宿区域参考点，酒店到参考点的接驳待定。"
    if unresolved:
        return "住宿参考点未建立，住宿往返接驳没有纳入排程。"
    return ""


def itinerary_markdown(plan: dict) -> str:
    days = plan.get("days") or []
    constrained = [day.get("window") for day in days if isinstance(day.get("window"), dict) and day["window"].get("state") != "default"]
    if constrained:
        head = "首末日的可用时段按车次抵离时刻收窄。午餐 60 分钟，切换 15 分钟，机动 30 分钟。"
    else:
        head = "默认每日 09:00–18:00。午餐 60 分钟，切换 15 分钟，机动 30 分钟。"
    lines = [head]
    anchor_note = _lodging_anchor_note(days)
    if anchor_note:
        lines.append(anchor_note)
    for note in plan.get("notes") or []:
        if note not in lines:
            lines.append(note)
    for day in days:
        bits = []
        for item in day.get("items") or []:
            if item.get("start") and item.get("role") != "depart":
                bits.append(f"{item['name']} {item['start']}–{item['end']}")
            elif item.get("role") == "depart" and item.get("start"):
                bits.append(f"{item['name']} {item['start']}出发")
            else:
                bits.append(f"{item['name']}（时刻未核实）")
        modes = []
        for leg in day.get("legs") or []:
            seconds = leg.get("duration_seconds")
            if seconds is None:
                shown = "未核实"
            elif leg.get("confidence") == "verified":
                shown = f"{int(seconds) // 60}分钟"
            else:
                shown = f"参考 {int(seconds) // 60}分钟"
            modes.append(f"{leg.get('mode')} {shown}")
        window_text = _window_text(day.get("window"))
        # 出行日期未知时不写 "None"：那天就是「日期待定」，不能凭空印一个日期。
        day_text = str(day.get("date") or "").strip() or "日期待定"
        head_text = f"- 第{day['day']}天 {day_text}"
        if window_text:
            head_text += f"（{window_text}）"
        lines.append(head_text + "：" + (" → ".join(bits) if bits else "无安排"))
        if modes:
            lines.append("  交通：" + "；".join(modes))
        for note in day.get("notes") or []:
            lines.append(f"  {note}")
    if plan.get("schedule_status"):
        lines.append(f"排程状态：{plan['schedule_status']}")
    return "\n".join(lines) + "\n"


def clock_text_unverified(*texts: str) -> bool:
    return any(_TIME_IN_TEXT.search(text or "") for text in texts)
