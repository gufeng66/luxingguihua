"""map_agent 没写出 HTML 时，用高德地点检索补一份路线地图。"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request

from planner.paths import MAPS_DIR
from planner.route_plan import format_day_list, plan_route
from planner.summary import parse_plan_id
from slots import days_from_query, destination_from_query, lodging_from_query

logger = logging.getLogger("travel_planner")

_PLACE = "https://restapi.amap.com/v5/place/text"
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
    try:
        with opener(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    if str(payload.get("status")) != "1":
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
        found.append({"name": name, "lng": lng, "lat": lat, "address": str(poi.get("address") or "")})
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
    stay = _lodging_hit(open_url, city, lodging)
    limit = min(25, max(_PER_DAY, int(days or 1) * _PER_DAY))
    sights = []
    seen = {stay["name"]} if stay else set()
    for poi in _search(open_url, city=city, keywords="景点", types="110000", limit=limit):
        if poi["name"] in seen:
            continue
        sights.append(poi)
        seen.add(poi["name"])
    if not sights:
        logger.warning("map_fallback empty plan_id=%s city=%s", pid, city)
        return None
    station = _station_hit(open_url, city, city) if need_ticket and stay is not None else None
    tasks = plan_route(sights, days, city, lodging=stay, station=station)
    if not tasks:
        logger.warning("map_fallback empty plan_id=%s city=%s", pid, city)
        return None
    path = MAPS_DIR / f"{pid}.html"
    path.write_text(render_map_html(tasks), encoding="utf-8")
    head = "高德地点检索补写的路线地图"
    if stay is not None:
        head += "（每天从住宿地出发，结束回到住宿地）"
    return _itinerary_note(tasks, head=head)


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
