"""map_agent 没写出 HTML 时，用高德地点检索补一份路线地图。"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

from planner.paths import MAPS_DIR
from planner.summary import parse_plan_id
from slots import days_from_query, destination_from_query, lodging_from_query

logger = logging.getLogger("travel_planner")

_PLACE = "https://restapi.amap.com/v5/place/text"
_PER_DAY = 3


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


def _near(a: dict, b: dict) -> float:
    return (a["lng"] - b["lng"]) ** 2 + (a["lat"] - b["lat"]) ** 2


def _chunks(sights: list[dict], days: int) -> list[list[dict]]:
    days = max(1, int(days or 1))
    sights = sights[: days * _PER_DAY]
    if len(sights) >= days * _PER_DAY:
        return [sights[i * _PER_DAY : (i + 1) * _PER_DAY] for i in range(days)]
    base, extra = divmod(len(sights), days)
    chunks: list[list[dict]] = []
    index = 0
    for day in range(days):
        count = base + (1 if day < extra else 0)
        chunks.append(sights[index : index + count])
        index += count
    return chunks


def _poi(day: int, poi: dict, remark: str) -> dict:
    return {
        "type": "poi",
        "day": day,
        "lnglat": [poi["lng"], poi["lat"]],
        "sort": f"Day{day}",
        "text": poi["name"],
        "remark": remark,
    }


def _route(day: int, city: str, start: dict, end: dict) -> dict:
    return {
        "type": "route",
        "day": day,
        "routeType": "walking",
        "start": [start["lng"], start["lat"]],
        "end": [end["lng"], end["lat"]],
        "city": city,
        "remark": f"{start['name']}→{end['name']}",
    }


def _tasks(sights: list[dict], days: int, city: str, lodging: dict | None = None) -> list[dict]:
    """每天从住宿地出发，结束回到住宿地。景点按离住宿地远近分天，每天最多 3 个。"""
    # ponytail: 用经纬度平方距离排序，不是路网距离；景点多到需要分区再换路径规划
    if lodging is not None:
        sights = sorted(sights, key=lambda poi: _near(poi, lodging))
    tasks: list[dict] = []
    for day, group in enumerate(_chunks(sights, days), 1):
        if not group and lodging is None:
            continue
        seq = [lodging, *group, lodging] if lodging is not None else list(group)
        prev = None
        for index, poi in enumerate(seq):
            remark = poi.get("address") or ""
            if lodging is not None and index == len(seq) - 1 and len(seq) > 1:
                remark = "返回住宿地"
            elif lodging is not None and index == 0:
                remark = "从住宿地出发"
            tasks.append(_poi(day, poi, remark))
            if prev is not None:
                tasks.append(_route(day, city, prev, poi))
            prev = poi
    return tasks


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
    tasks = _tasks(sights, days, city, lodging=stay)
    path = MAPS_DIR / f"{pid}.html"
    path.write_text(render_map_html(tasks), encoding="utf-8")
    by_day: dict[int, list[str]] = {}
    for item in tasks:
        if item["type"] == "poi":
            by_day.setdefault(item["day"], []).append(item["text"])
    lines = [f"- 第{day}天：" + " → ".join(names) for day, names in sorted(by_day.items())]
    head = "高德地点检索补写的路线地图"
    if stay is not None:
        head += "（每天从住宿地出发，结束回到住宿地）"
    return head + "：\n" + "\n".join(lines)
