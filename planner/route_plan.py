"""把候选景点排成按天的地图任务。不联网。"""

from __future__ import annotations

import itertools
import math

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
