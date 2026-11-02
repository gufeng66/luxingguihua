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

_TIME_IN_TEXT = re.compile(r"\d{1,2}\s*[:：点]")


def visit_minutes(poi: dict) -> int:
    raw = poi.get("visit_minutes")
    if raw is not None and str(raw).strip() != "":
        return max(1, int(raw))
    text = str(poi.get("place_type") or "")
    if "博物馆" in text or text == "museum":
        return 150
    if any(token in text for token in ("公园", "街区", "park", "street")):
        return 60
    return 90


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


def _dates(constraints: dict) -> list[date]:
    raw = str(constraints.get("date") or "").strip()
    start = date.fromisoformat(raw) if raw else date(2026, 1, 5)
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


def _fit_visit(start: int, minutes: int, intervals: list[tuple[int, int]], last_entry: int | None):
    for begin, end in intervals:
        slot = max(start, begin)
        if last_entry is not None and slot > last_entry:
            continue
        if slot + minutes <= end and slot + minutes <= DAY_END:
            return slot, slot + minutes
    return None


def _lunch_before(cursor: int, minutes: int, taken: bool) -> tuple[int, bool]:
    if taken or cursor + minutes <= NOON:
        return cursor, False
    if cursor >= NOON - LUNCH_MIN:
        return cursor + LUNCH_MIN, True
    if cursor < NOON:
        return NOON + LUNCH_MIN, True
    return cursor + LUNCH_MIN, True


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


def _blank(day_plan: dict) -> dict:
    for item in day_plan["items"]:
        item["start"] = None
        item["end"] = None
    day_plan["unverified"] = True
    day_plan["notes"].append("交通耗时未知，不提供精确到达时间")
    return day_plan


def _simulate(group: list[dict], lodging: dict | None, day: date, constraints: dict, duration_fn) -> dict | None:
    if any(_closed(poi, day, constraints) for poi in group):
        return None
    mode_pref = str(constraints.get("transport_mode") or "public_transit")
    day_text = day.isoformat()
    items: list[dict] = []
    legs: list[dict] = []
    notes: list[str] = []
    unverified = False
    cursor = DAY_START
    lunch = False
    prev = lodging
    origin = lodging if lodging is not None else (group[0] if group else None)
    if origin is None:
        return None

    def add_leg(dest: dict, switch: int) -> int | None:
        nonlocal unverified, cursor
        depart = cursor
        mode, seconds = _mode_and_seconds(prev, dest, day_text, depart, mode_pref, duration_fn)
        legs.append(
            {
                "origin_id": prev["poi_id"],
                "dest_id": dest["poi_id"],
                "mode": mode,
                "duration_seconds": seconds,
                "depart": _hhmm(depart),
            }
        )
        if seconds is None:
            unverified = True
            return None
        cursor = depart + switch + seconds // 60
        return cursor

    if lodging is not None:
        items.append(_point(lodging, "depart", DAY_START, DAY_START))
    for index, poi in enumerate(group):
        switch = 0 if index == 0 else SWITCH_MIN
        if prev is None and not (lodging is None and index == 0):
            return None
        arrived = add_leg(poi, switch) if lodging is not None or index else None
        if lodging is None and index == 0:
            arrived = DAY_START
            cursor = DAY_START
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
        fitted = _fit_visit(cursor, minutes, intervals, last_entry)
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
        if back is not None and back > DAY_END - SLACK_MIN:
            return None
    if lodging is None and cursor > DAY_END - SLACK_MIN:
        return None
    if group and _known_load_exceeds(group, legs, lodging is not None):
        return None
    plan = {
        "date": day_text,
        "items": items,
        "legs": legs,
        "notes": notes,
        "unverified": unverified,
        "skip": not group,
    }
    if unverified:
        return _blank(plan)
    return plan


def _known_load_exceeds(group: list[dict], legs: list[dict], with_lodging: bool) -> bool:
    """已知交通、游览、切换、午餐和机动已经超出当天时，未知路段不能把它留在主路线。"""
    visit = sum(visit_minutes(poi) for poi in group)
    travel = sum(int(leg["duration_seconds"]) // 60 for leg in legs if isinstance(leg.get("duration_seconds"), int))
    switches = SWITCH_MIN * (max(0, len(legs) - 1) if with_lodging else len(legs))
    return DAY_START + visit + travel + switches + LUNCH_MIN > DAY_END - SLACK_MIN


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
    if minutes is not None:
        item["visit_minutes"] = minutes
    return item


def _best(group: list[dict], day: date, lodging: dict | None, constraints: dict, duration_fn):
    if not group:
        return {"date": day.isoformat(), "items": [], "legs": [], "notes": [], "unverified": False, "skip": True}
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


def _mark(poi: dict, constraints: dict) -> dict:
    item = dict(poi)
    item["poi_id"] = str(item.get("poi_id") or item.get("name") or "")
    item["name"] = str(item.get("name") or "").strip()
    names = {str(name).strip() for name in (constraints.get("must_visit") or []) if str(name).strip()}
    item["must_visit"] = bool(item.get("must_visit")) or item["name"] in names
    return item


def _prepare(candidates: list[dict], constraints: dict) -> tuple[list[dict], dict | None]:
    avoid = {str(name).strip() for name in (constraints.get("avoid") or []) if str(name).strip()}
    seen: set[str] = set()
    pool = []
    for raw in candidates:
        poi = _mark(raw, constraints)
        if not poi["name"] or poi["name"] in avoid or poi["name"] in seen:
            continue
        try:
            poi["lng"] = float(poi["lng"])
            poi["lat"] = float(poi["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(poi["name"])
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


def schedule(
    candidates: list[dict],
    constraints: dict,
    duration_fn,
    *,
    corrections: int = 2,
    assignment: list[list[str]] | None = None,
    kept_dropped: list[dict] | None = None,
    kept_conflicts: list[dict] | None = None,
) -> dict:
    """纯排程。duration_fn(origin_id, dest_id, mode, date, depart_min) -> 秒或 None。不发 HTTP。"""
    pool, lodging = _prepare(candidates, constraints)
    by_id = {poi["poi_id"]: poi for poi in pool}
    dates = _dates(constraints)
    dropped: list[dict] = [dict(row) for row in (kept_dropped or [])]
    conflicts: list[dict] = [dict(row) for row in (kept_conflicts or [])]
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
            if assignment is None:
                for poi in group:
                    bucket = conflicts if poi.get("must_visit") else dropped
                    bucket.append({"poi_id": poi["poi_id"], "name": poi["name"], "reason": "最终检查未通过"})
            assignment_rows.append([])
            continue
        if sim.get("skip"):
            assignment_rows.append([])
            continue
        sim["day"] = index
        sim["date"] = day.isoformat()
        days.append(sim)
        assignment_rows.append([item["poi_id"] for item in sim["items"] if item.get("role") == "visit"])
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
    unverified = any(day.get("unverified") for day in days) or bool(constraints.get("unverified_clock"))
    missing_fact = _missing_hard(days, constraints)
    has_visit = bool(placed)
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
            if not _hard_facts(constraints, item["poi_id"], date.fromisoformat(day["date"])):
                day["notes"].append(f"{item['name']}开放规则未核实")
    return {
        "days": days,
        "dropped": dropped,
        "conflicts": conflicts,
        "schedule_status": status,
        "notes": notes,
        "assignment": assignment_rows,
    }


def _missing_hard(days: list[dict], constraints: dict) -> bool:
    for day in days:
        when = date.fromisoformat(day["date"])
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
    geo = lodging if lodging is not None else (_centroid(rest) if rest else None)
    if geo is not None and rest:
        rest.sort(key=lambda poi: (_angle(poi, geo), poi["name"]))
    # 质心没有 poi_id，只能算方位。交通端点必须是住宿或真实景点。
    if lodging is not None:
        start, probe = lodging, (rest[0] if rest else None)
    else:
        start = rest[0] if rest else None
        probe = rest[1] if len(rest) > 1 else None
    known = False
    if start is not None and probe is not None and start.get("poi_id") and probe.get("poi_id"):
        sample_day = dates[0].isoformat()
        known = any(
            duration_fn(start["poi_id"], probe["poi_id"], mode, sample_day, DAY_START) is not None
            for mode in ("walking", "transfer", "driving")
        )
    if not known:
        chunks = _split(rest, len(dates)) if rest else [[] for _ in dates]
        return [groups[index] + chunks[index] for index in range(len(dates))]
    for poi in rest:
        # ponytail: 放进当前最少的一天，避免 3 日游全堆在第 1 天。景点多到要按片区排再改。
        placed = False
        open_days = [
            index
            for index, day in enumerate(dates)
            if not _closed(poi, day, constraints)
            and _best(groups[index] + [poi], day, lodging, constraints, duration_fn) is not None
        ]
        if open_days:
            groups[min(open_days, key=lambda index: (len(groups[index]), index))].append(poi)
            placed = True
        if not placed:
            bucket = conflicts if poi.get("must_visit") else dropped
            bucket.append({"poi_id": poi["poi_id"], "name": poi["name"], "reason": "时间不足"})
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
                remark = "从住宿地出发"
            elif item.get("role") == "return":
                remark = "返回住宿地"
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


def itinerary_markdown(plan: dict) -> str:
    lines = ["默认每日 09:00–18:00。午餐 60 分钟，切换 15 分钟，机动 30 分钟。"]
    for note in plan.get("notes") or []:
        if note not in lines:
            lines.append(note)
    for day in plan.get("days") or []:
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
            shown = "未核实" if leg.get("duration_seconds") is None else f"{int(leg['duration_seconds']) // 60}分钟"
            modes.append(f"{leg.get('mode')} {shown}")
        lines.append(f"- 第{day['day']}天 {day['date']}：" + " → ".join(bits))
        if modes:
            lines.append("  交通：" + "；".join(modes))
        for note in day.get("notes") or []:
            lines.append(f"  {note}")
    if plan.get("schedule_status"):
        lines.append(f"排程状态：{plan['schedule_status']}")
    return "\n".join(lines) + "\n"


def clock_text_unverified(*texts: str) -> bool:
    return any(_TIME_IN_TEXT.search(text or "") for text in texts)
