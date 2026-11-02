"""map_agent 没写出 HTML 时，用高德地点检索补一份路线地图。"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

from planner.paths import MAPS_DIR
from planner.route_plan import (
    clock_text_unverified,
    days_to_tasks,
    format_day_list,
    itinerary_markdown,
    plan_route,
    route_cache_key,
    schedule,
)
from planner.summary import parse_plan_id
from slots import days_from_query, destination_from_query, lodging_from_query

logger = logging.getLogger("travel_planner")

_PLACE = "https://restapi.amap.com/v5/place/text"
_ROUTES = {
    "walking": "https://restapi.amap.com/v3/direction/walking",
    "driving": "https://restapi.amap.com/v3/direction/driving",
    "transfer": "https://restapi.amap.com/v3/direction/transit/integrated",
}
_PER_DAY = 3
_TASK_RE = re.compile(r'<script id="amapTaskData" type="application/json">(.*?)</script>', re.S)


def _lodging_hit(opener, city: str, lodging: str | None) -> dict | None:
    text = (lodging or "").strip()
    if not text:
        return None
    queries = [text]
    shorter = text.replace("附近", "").strip()
    if shorter and shorter != text:
        queries.append(shorter)
    for keywords in queries:
        found = _search(opener, city=city, keywords=keywords, types="", limit=1)
        if found:
            hit = dict(found[0])
            hit["name"] = text
            return hit
    return None


def _key() -> str:
    return (os.getenv("AMAP_KEY") or os.getenv("AMAP_WEBSERVICE_KEY") or "").strip()


def _search(opener, *, city: str, keywords: str, types: str, limit: int) -> list[dict]:
    key = _key()
    if not key or not city or not keywords:
        return []
    query = urllib.parse.urlencode(
        {
            "key": key,
            "keywords": keywords,
            "region": city,
            "city_limit": "true",
            "page_size": str(limit),
            "types": types,
        }
    )
    req = urllib.request.Request(_PLACE + "?" + query, headers={"User-Agent": "travel-planner"})
    payload = None
    for attempt in range(3):
        try:
            with opener(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        # ponytail: 个人 key 约 3 QPS；10021 时最多再等两次。仍超限就放弃这一条。
        if str(payload.get("infocode")) == "10021" and attempt < 2:
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    if not payload or str(payload.get("status")) != "1":
        return []
    found: list[dict] = []
    for poi in payload.get("pois") or []:
        loc = str(poi.get("location") or "")
        name = str(poi.get("name") or "").strip()
        if not name or "," not in loc:
            continue
        lng_s, lat_s = loc.split(",", 1)
        try:
            lng, lat = float(lng_s), float(lat_s)
        except ValueError:
            continue
        found.append(
            {
                "poi_id": str(poi.get("id") or ""),
                "name": name,
                "city": city,
                "lng": lng,
                "lat": lat,
                "place_type": str(poi.get("type") or ""),
                "address": str(poi.get("address") or ""),
            }
        )
        if len(found) >= limit:
            break
    return found


def _station_hit(opener, city: str, dest: str) -> dict | None:
    text = (dest or "").strip()
    if not text:
        return None
    for keywords in (f"{text}站", f"{text}高铁站"):
        found = _search(opener, city=city, keywords=keywords, types="", limit=1)
        if found:
            return found[0]
    return None


def _named_lodging(sights: list[dict], lodging: str | None) -> dict | None:
    text = (lodging or "").strip()
    if not text:
        return None
    for poi in sights:
        name = poi["name"]
        if name == text or text in name or name in text:
            hit = dict(poi)
            hit["name"] = text
            return hit
    return None


_NOISE = ("地铁", "公交", "检票", "售票", "停车场", "直通车")
_PLACE_SUFFIX = ("遗址公园", "博物院", "博物馆", "长城", "公园")
_LOOSE_SUFFIX = ("广播电视塔", "电视塔", "塔")


def _core_name(text: str) -> str:
    """去掉括号里的补充，含没写完的「长城（八达岭」。"""
    text = re.sub(r"[（(][^）)]*[）)]?", "", text or "")
    return text.replace("）", "").replace(")", "").strip()


def _loose_stems(text: str) -> list[str]:
    """东方明珠塔对不上东方明珠广播电视塔：去掉末尾的塔再比。"""
    stems = []
    for suffix in _LOOSE_SUFFIX:
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            stems.append(text[: -len(suffix)])
    return stems


def _pick(hits: list[dict], name: str) -> dict | None:
    """高德名常常比用户说法多一个后缀：天坛→天坛公园，故宫→故宫博物院。"""
    if not hits:
        return None
    query = name.strip()
    core = _core_name(query)

    def rank(hit: dict) -> int | None:
        poi_name = hit["name"]
        if poi_name == query:
            score = 100
        elif core and poi_name == core:
            score = 90
        elif core and core in poi_name:
            score = 80
        elif query and query in poi_name:
            score = 70
        elif any(stem and stem in poi_name for stem in _loose_stems(core or query)):
            score = 75
        else:
            return None
        if any(word in poi_name for word in _NOISE):
            score -= 30
        place_type = hit.get("place_type") or ""
        if "风景名胜" in place_type:
            score += 10
        if any(poi_name.endswith(suffix) for suffix in _PLACE_SUFFIX):
            score += 15
        return score

    ranked = [(rank(hit), index, hit) for index, hit in enumerate(hits)]
    matched = [row for row in ranked if row[0] is not None]
    if matched:
        matched.sort(key=lambda row: (-row[0], row[1]))
        chosen = matched[0][2]
    elif len(hits) == 1:
        chosen = hits[0]
    else:
        return None
    picked = dict(chosen)
    if not picked.get("poi_id"):
        picked["poi_id"] = picked["name"]
    return picked


def parse_candidates(text: str) -> list[dict]:
    """只接受一段 JSON 数组。解析不了就当没有候选，不从句子里猜坐标。"""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    hints = []
    for item in data:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            hints.append(item)
    return hints


def collect_candidates(
    opener,
    *,
    city: str,
    days: int,
    lodging: str | None,
    must_visit: list[str] | None = None,
    hints: list[dict] | None = None,
    deadline: float | None = None,
    clock=None,
    cancel_event=None,
) -> tuple[dict | None, list[dict], list[str]]:
    """高德结果覆盖身份和坐标。必去项不在候选里时按名称单查，泛搜不能顶替。"""
    clock = clock or time.monotonic

    def _expired() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        return deadline is not None and clock() >= deadline

    stay = None if _expired() else _lodging_hit(opener, city, lodging)
    wanted: list[tuple[str, dict]] = []
    seen_names: set[str] = set()
    for hint in hints or []:
        name = str(hint.get("name") or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        wanted.append((name, hint))
    for name in must_visit or []:
        name = str(name or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        wanted.append((name, {"name": name, "must_visit": True}))
    resolved: list[dict] = []
    unlocated: list[str] = []
    taken: set[str] = set()
    if wanted:
        for name, hint in wanted:
            if _expired():
                unlocated.append(name)
                continue
            picked = _pick(_search(opener, city=city, keywords=name, types="", limit=3), name)
            if picked is None or picked["name"] in taken:
                unlocated.append(name)
                continue
            picked["must_visit"] = bool(hint.get("must_visit")) or name in (must_visit or [])
            picked["asked_name"] = name
            picked["reason"] = str(hint.get("reason") or "")
            if hint.get("visit_minutes"):
                picked["visit_minutes"] = hint["visit_minutes"]
            resolved.append(picked)
            taken.add(picked["name"])
    # 必去往往挤在同一片，泛搜只补空天，不能代替没定位到的必去。
    limit = min(25, max(_PER_DAY, int(days or 1) * _PER_DAY))
    found = []
    if len(resolved) < limit and not _expired():
        found = _search(opener, city=city, keywords="景点", types="110000", limit=limit)
    for poi in found:
        if poi["name"] in taken or (stay is not None and poi["name"] == stay["name"]):
            continue
        if not poi.get("poi_id"):
            poi["poi_id"] = poi["name"]
        resolved.append(poi)
        taken.add(poi["name"])
        if len(resolved) >= limit:
            break
    return stay, resolved, unlocated


def duration_from_payload(mode: str, payload: dict) -> int | None:
    if str(payload.get("status")) != "1":
        return None
    route = payload.get("route") or {}
    if mode == "transfer":
        rows = route.get("transits") or []
        raw = rows[0].get("duration") if rows else None
    else:
        rows = route.get("paths") or []
        raw = rows[0].get("duration") if rows else None
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _constraints(city, day, days, stay, transport_mode, must_visit, avoid, facts, query_text) -> dict:
    return {
        "date": day,
        "days": days,
        "city": city,
        "lodging_poi": stay,
        "transport_mode": transport_mode or "public_transit",
        "must_visit": list(must_visit or []),
        "avoid": list(avoid or []),
        "facts": list(facts or []),
        "unverified_clock": clock_text_unverified(query_text or ""),
    }


def _context(plan: dict, unlocated: list[str], facts_status: str, route_error: str | None, facts: list[dict] | None = None) -> str:
    lines = [itinerary_markdown(plan).rstrip()]
    if unlocated:
        lines.append("未能定位：" + "、".join(unlocated))
    if facts_status == "unavailable":
        lines.append("开放规则未核实（搜索不可用）")
    links: list[str] = []
    for fact in facts or []:
        url = str(fact.get("source_url") or "").strip()
        if url and url not in links:
            links.append(url)
    if links:
        lines.append("证据链接：" + " ".join(links))
    if route_error:
        lines.append(f"交通未核实：{route_error}")
    return "\n".join(lines)


def _sights_from_html(html: str) -> list[dict]:
    match = _TASK_RE.search(html or "")
    if not match:
        raise ValueError("no amapTaskData")
    data = json.loads(match.group(1))
    if not isinstance(data, list):
        raise ValueError("not a list")
    sights: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "poi":
            continue
        lnglat = item.get("lnglat")
        if not isinstance(lnglat, (list, tuple)) or len(lnglat) != 2:
            continue
        name = str(item.get("text") or "").strip()
        if not name:
            continue
        try:
            lng, lat = float(lnglat[0]), float(lnglat[1])
        except (TypeError, ValueError):
            continue
        sights.append({"name": name, "lng": lng, "lat": lat, "address": str(item.get("remark") or "")})
    return sights


def _itinerary_note(tasks: list[dict], *, head: str) -> str:
    return head + "：\n" + format_day_list(tasks) + "\n行程表顺序以清单为准。"


def render_map_html(tasks: list[dict]) -> str:
    data = json.dumps(tasks, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>html,body{{margin:0;overscroll-behavior:contain}}</style></head>
<body>
<a id="amapOnline" href="#" target="_blank" rel="noopener">在高德在线地图打开</a>
<a id="amapApp" href="#" target="_blank" rel="noopener">在高德App看首站</a>
<script id="amapTaskData" type="application/json">{data}</script>
<script>
var tasks = JSON.parse(document.getElementById('amapTaskData').textContent);
var base = 'https://a.amap.com/jsapi_demo_show/static/openclaw/travel_plan.html';
document.getElementById('amapOnline').href = base + '?data=' + encodeURIComponent(JSON.stringify(tasks));
var poi = tasks.find(function (x) {{ return x && x.type === 'poi' && x.lnglat; }});
if (poi) {{
  document.getElementById('amapApp').href = 'https://uri.amap.com/marker?position=' + poi.lnglat[0] + ',' + poi.lnglat[1] + '&name=' + encodeURIComponent(poi.text || '首站') + '&src=travel-planner&coordinate=gaode&callnative=1';
}}
</script>
</body></html>
"""


def ensure_route_map(
    plan_id: str,
    *,
    destination: str,
    days: int = 1,
    lodging: str | None = None,
    need_ticket: bool = False,
    opener=None,
    must_visit: list[str] | None = None,
    avoid: list[str] | None = None,
    date: str | None = None,
    transport_mode: str | None = None,
    hints: list[dict] | None = None,
) -> str | None:
    """写出 maps/{{plan_id}}.html。成功返回给汇总用的景点清单，失败返回 None。"""
    # ponytail: 单测用 TRAVEL_MAP_FALLBACK=0 关掉外呼；正式运行不设这个变量
    if os.getenv("TRAVEL_MAP_FALLBACK") == "0":
        return None
    pid = parse_plan_id(plan_id)
    city = (destination or "").strip()
    if pid is None or not city:
        logger.warning("map_fallback skip plan_id=%s city=%s", plan_id, city or "-")
        return None
    open_url = opener or urllib.request.urlopen
    stay, sights, unlocated = collect_candidates(
        open_url,
        city=city,
        days=days,
        lodging=lodging,
        must_visit=must_visit,
        hints=hints,
    )
    if not sights:
        logger.warning("map_fallback empty plan_id=%s city=%s", pid, city)
        if unlocated:
            return "未能定位：" + "、".join(unlocated)
        return None
    constraints = _constraints(
        city,
        date,
        days,
        stay,
        transport_mode,
        must_visit,
        avoid,
        [],
        "",
    )
    plan = schedule(sights, constraints, lambda *_args: None)
    tasks = days_to_tasks(plan, city)
    if not tasks:
        logger.warning("map_fallback empty plan_id=%s city=%s", pid, city)
        return None
    path = MAPS_DIR / f"{pid}.html"
    path.write_text(render_map_html(tasks), encoding="utf-8")
    head = "高德地点检索补写的路线地图"
    if stay is not None:
        head += "（每天从住宿地出发，结束回到住宿地）"
    note = head + "：\n" + itinerary_markdown(plan)
    if unlocated:
        note += "未能定位：" + "、".join(unlocated) + "\n"
    if need_ticket:
        note += "车站衔接仍按候选处理。\n"
    return note


def _modes_for(transport_mode: str) -> list[str]:
    if transport_mode == "driving":
        return ["driving"]
    if transport_mode == "walking":
        return ["walking"]
    return ["walking", "transfer"]


def needed_route_queries(plan: dict, transport_mode: str, table: dict) -> list[tuple]:
    """当前行程的相邻段。已缓存的键不再查询。"""
    queries = []
    modes = _modes_for(transport_mode)
    points = {}
    for day in plan.get("days") or []:
        for item in day.get("items") or []:
            if item.get("poi_id") and item.get("lng") is not None:
                points[item["poi_id"]] = item
        ids = [item["poi_id"] for item in day.get("items") or [] if item.get("poi_id")]
        for origin_id, dest_id in zip(ids, ids[1:]):
            depart = 9 * 60
            for leg in day.get("legs") or []:
                if leg.get("origin_id") == origin_id and leg.get("dest_id") == dest_id and leg.get("depart"):
                    hour, minute = leg["depart"].split(":")
                    depart = int(hour) * 60 + int(minute)
                    break
            for mode in modes:
                key = route_cache_key(origin_id, dest_id, mode, day["date"], depart)
                if key in table:
                    continue
                origin = points.get(origin_id)
                dest = points.get(dest_id)
                if origin is None or dest is None:
                    continue
                queries.append((key, origin, dest, mode, day["date"], depart))
    return queries


async def fill_routes(plan: dict, table: dict, *, city: str, transport_mode: str, client=None, clock=None, cancel_event=None, budget=None) -> str | None:
    """补查相邻段。预算用尽或失败时留下未知耗时，不把直线距离当成耗时。"""
    import asyncio

    import httpx

    from planner.travel_search import SearchBudget

    if not _key():
        return "no_key"
    clock = clock or (lambda: asyncio.get_running_loop().time())
    own_budget = budget is None
    if own_budget:
        budget = SearchBudget(clock=clock, limit=60, window=60.0, max_timeout=6.0)
    own = client is None
    if own:
        client = httpx.AsyncClient()
    error = None
    try:
        for key, origin, dest, mode, _day, _depart in needed_route_queries(plan, transport_mode, table):
            if cancel_event is not None and cancel_event.is_set():
                budget.closed = True
                break
            timeout = await budget.reserve()
            if timeout is None:
                error = error or "budget"
                break
            params = {
                "key": _key(),
                "origin": f"{origin['lng']},{origin['lat']}",
                "destination": f"{dest['lng']},{dest['lat']}",
            }
            if mode == "transfer":
                params["city"] = city
                params["cityd"] = city
            try:
                response = await client.get(_ROUTES[mode], params=params, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                error = error or "request_failed"
                if budget.closed:
                    break
                continue
            if budget.closed:
                break
            table[key] = duration_from_payload(mode, payload)
    finally:
        if own_budget:
            budget.closed = True
        if own:
            await client.aclose()
    return error


def duration_lookup(table: dict):
    def duration_fn(origin_id, dest_id, mode, day, depart_min):
        return table.get(route_cache_key(origin_id, dest_id, mode, day, depart_min))

    return duration_fn


async def assemble_itinerary(
    *,
    city: str,
    days: int,
    date: str | None,
    lodging: str | None,
    transport_mode: str | None,
    must_visit: list[str] | None,
    avoid: list[str] | None,
    hints: list[dict] | None,
    query_text: str = "",
    opener=None,
    cancel_event=None,
) -> dict:
    """候选校验、搜索、相邻耗时、最多两轮修正，再对最终新增边做只读检查。"""
    import asyncio
    from functools import partial

    from planner.travel_search import SearchBudget, gather_facts

    open_url = opener or urllib.request.urlopen
    # ponytail: urlopen 放到工作线程，取消只在两次查询之间生效；单次请求仍会跑完。
    stay, sights, unlocated = await asyncio.to_thread(
        partial(
            collect_candidates,
            open_url,
            city=city,
            days=days,
            lodging=lodging,
            must_visit=must_visit,
            hints=hints,
            deadline=time.monotonic() + 20,
            cancel_event=cancel_event,
        )
    )
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    found = await gather_facts(
        sights,
        city=city,
        day=date or "",
        api_key=api_key,
        cancel_event=cancel_event,
    )
    hard = [fact for fact in found["facts"] if fact.get("status") == "hard"]
    constraints = _constraints(
        city,
        date,
        days,
        stay,
        transport_mode,
        must_visit,
        avoid,
        hard,
        query_text,
    )
    table: dict = {}
    mode = constraints["transport_mode"]
    plan = schedule(sights, constraints, duration_lookup(table))
    route_error = None
    if sights:
        route_budget = SearchBudget(clock=lambda: asyncio.get_running_loop().time(), limit=60, window=60.0, max_timeout=6.0)
        for _ in range(2):
            missing = needed_route_queries(plan, mode, table)
            if not missing:
                break
            route_error = await fill_routes(
                plan,
                table,
                city=city,
                transport_mode=mode,
                cancel_event=cancel_event,
                budget=route_budget,
            )
            plan = schedule(sights, constraints, duration_lookup(table))
        if needed_route_queries(plan, mode, table):
            route_error = await fill_routes(
                plan,
                table,
                city=city,
                transport_mode=mode,
                cancel_event=cancel_event,
                budget=route_budget,
            ) or route_error
        # 最后一次补查后按已知耗时重排。锁死上一轮分组会把同一天里放不下的必去整组丢掉。
        plan = schedule(sights, constraints, duration_lookup(table))
    return {
        "plan": plan,
        "tasks": days_to_tasks(plan, city),
        "unlocated": unlocated,
        "facts_status": found["facts_status"],
        "facts": found["facts"],
        "search_attempts": found["attempts"],
        "search_error": found["error"],
        "search_usage": found["usage"],
        "route_error": route_error,
        "candidates": sights,
        "constraints": constraints,
        "context": _context(plan, unlocated, found["facts_status"], route_error, found["facts"]),
    }


def reorder_existing_map(
    plan_id: str,
    *,
    destination: str,
    days: int = 1,
    lodging: str | None = None,
    need_ticket: bool = False,
    opener=None,
) -> str | None:
    """已有地图 HTML 时按坐标重排并覆盖。解析失败或没有景点时不写文件，返回 None。"""
    pid = parse_plan_id(plan_id)
    if pid is None:
        return None
    path = MAPS_DIR / f"{pid}.html"
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    try:
        sights = _sights_from_html(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not sights:
        return None
    city = (destination or "").strip()
    live = os.getenv("TRAVEL_MAP_FALLBACK") != "0"
    open_url = opener or urllib.request.urlopen
    stay = _named_lodging(sights, lodging)
    if stay is None and live and city:
        stay = _lodging_hit(open_url, city, lodging)
    station = _station_hit(open_url, city, city) if need_ticket and stay is not None and live and city else None
    tasks = plan_route(sights, days, city, lodging=stay, station=station)
    if not tasks:
        return None
    path.write_text(render_map_html(tasks), encoding="utf-8")
    head = "按坐标重排的路线"
    if stay is not None:
        head += "（每天从住宿地出发，结束回到住宿地）"
    return _itinerary_note(tasks, head=head)
