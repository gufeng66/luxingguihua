"""map_agent 没写出 HTML 时，用高德地点检索补一份路线地图。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request

from planner.paths import MAPS_DIR
from planner.route_plan import (
    WALK_MAX_SECONDS,
    clock_text_unverified,
    days_to_tasks,
    encode_route_key,
    format_day_list,
    itinerary_markdown,
    plan_route,
    candidate_retrieval_target,
    route_cache_key,
    schedule,
    sanitize_candidates,
)
from planner.run_stats import bump as bump_stat
from planner.run_stats import note as note_stat
from planner.summary import parse_plan_id
from planner.travel_search import ALLOWED_HOSTS
from planner.urls import safe_evidence_url
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


_AREA_WORDS = ("附近", "周边", "一带", "区域", "市中心")
_LODGING_WORDS = ("酒店", "宾馆", "民宿", "公寓", "旅馆", "客栈", "招待所", "青旅")
_LODGING_HINT_WORDS = ("住宿", "酒店", "民宿", "宾馆", "落脚", "入住")
_STREET_WORDS = ("路", "街", "弄", "巷", "号")
_LOOP_VERIFIED_KINDS = ("exact",)
# 可当排程锚点的精度：exact 是具体酒店，area 是区域参考点（能排接驳，但不能宣称已确认酒店）。
_ANCHOR_READY_PRECISIONS = ("exact", "area")


def _strip_area(text: str) -> str:
    out = str(text or "").strip()
    for word in _AREA_WORDS:
        out = out.replace(word, "")
    return out.strip()


def _looks_specific(text: str) -> bool:
    """去掉区域词后还带具体住宿名或门牌，才算用户指定了具体地点。"""
    core = _strip_area(text)
    if not core:
        return False
    if any(word in core for word in _LODGING_WORDS):
        return True
    return bool(re.search(r"\d+\s*号", core))


def _identity_matches(text: str, poi_name: str) -> bool:
    """用户说法与高德名称要能对上号：核心名相等或互相包含。"""
    left = _core_name(_strip_area(text)).lower()
    right = _core_name(poi_name or "").lower()
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _is_lodging_hint(name: str, lodging: str | None) -> bool:
    """模型会把住宿地写进候选（例如「上海站附近住宿地」），不能当成游览景点。"""
    text = str(name or "").strip()
    if not text:
        return False
    if any(word in text for word in _LODGING_HINT_WORDS):
        return True
    stay = _strip_area(lodging or "")
    return bool(stay) and stay in text


def _lodging_precision(hit: dict | None, text: str, *, scope: str | None) -> tuple[str, str]:
    """返回 (kind, precision)。kind ∈ exact/area/unresolved/none，precision ∈ exact/area/fallback。

    只有「用户明确指定具体地点」且「结果身份对得上」才给 exact；只给区域时按区域参考。
    """
    if hit is None:
        return "none", "fallback"
    if scope == "area":
        return "area", "area"
    specific = _looks_specific(text)
    matched = _identity_matches(text, str(hit.get("name") or ""))
    if not specific:
        return "area", "area"
    return ("exact", "exact") if matched else ("unresolved", "fallback")


def _lodging_hit(opener, city: str, lodging: str | None) -> dict | None:
    text = (lodging or "").strip()
    if not text:
        return None
    queries = [text]
    shorter = _strip_area(text)
    if shorter and shorter != text:
        queries.append(shorter)
    for keywords in queries:
        found = _search(opener, city=city, keywords=keywords, types="", limit=3)
        # 用当前查询词匹配：短名查询（「上海南站附近」→「上海南站」）的结果名里没有「附近」，
        # 若仍拿原串去比对，_pick 会因全部命中都匹配不上而返回 None。
        picked = _pick(found, keywords) if found else None
        if picked is not None:
            picked["asked_name"] = text
            return picked
    return None


def _key() -> str:
    return (os.getenv("AMAP_KEY") or os.getenv("AMAP_WEBSERVICE_KEY") or "").strip()


def _note(stats: dict | None, key: str) -> None:
    if stats is not None:
        stats[key] = int(stats.get(key) or 0) + 1


def _poi_state(result: dict) -> str:
    """把 POI 查询的真实结果折成互斥状态；同一 key 后写覆盖先写。"""
    status = str(result.get("status") or "")
    if status == "rate_limit":
        return "rate_limited"
    if status in {"matched", "ok_empty"}:
        return "succeeded"
    if status in {"budget", "deadline"}:
        return "blocked_by_budget" if status == "budget" else "timed_out"
    if status == "cancelled":
        return "cancelled"
    return "failed"


def _record_poi(result: dict) -> dict:
    """一次 POI 查询结束后，按产生事实的那一层写 RunStats。"""
    note_stat("map_poi", _poi_state(result))
    bump_stat("map_poi")
    return result


def _record_route_error(error: str | None) -> None:
    """路线阶段结束：把聚合错误折成 map_route 状态，细分原因仍留给 amap_stats。"""
    if error in {"rate_limit"}:
        note_stat("map_route", "rate_limited")
    elif error == "no_key":
        note_stat("map_route", "dependency_missing")
    elif error in {"budget"}:
        note_stat("map_route", "blocked_by_budget")
    elif error in {"deadline"}:
        note_stat("map_route", "timed_out")
    elif error in {"no_route"}:
        note_stat("map_route", "succeeded")  # 有效结果：查到但无路线，不是调用失败
    elif error:
        note_stat("map_route", "failed")
    else:
        note_stat("map_route", "succeeded")
    bump_stat("map_route")


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


def _pois_from_payload(payload: dict | None, *, city: str, limit: int) -> list[dict]:
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


async def search_places_result(client, *, city: str, keywords: str, types: str, limit: int, budget=None, cancel_event=None, stats=None, location: str | None = None, radius: int | None = None) -> dict:
    """地点查询的真实状态；调用方不能再把限流和空结果混成同一种失败。"""
    import asyncio

    key = _key()
    if not key or not city or not keywords:
        return {"items": [], "status": "request_failed"}
    params = {
        "key": key,
        "keywords": keywords,
        "region": city,
        "city_limit": "true",
        "page_size": str(limit),
        "types": types,
    }
    if location:
        params["location"] = location
        params["sort_rule"] = "distance"
    if radius:
        params["radius"] = str(radius)
    payload = None
    for attempt in range(3):
        if cancel_event is not None and cancel_event.is_set():
            return {"items": [], "status": "deadline"}
        if budget is not None and not budget.note_attempt():
            return {"items": [], "status": "budget"}
        timeout = 8 if budget is None else budget.timeout_for(8)
        if timeout <= 0:
            return {"items": [], "status": "deadline"}
        _note(stats, "poi_requests")
        try:
            response = await client.get(_PLACE, params=params, timeout=timeout)
            payload = response.json()
        except Exception:
            return {"items": [], "status": "request_failed"}
        limited = str(payload.get("infocode")) == "10021"
        if limited:
            _note(stats, "amap_rate_limits")
        if limited and attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        break
    code = str((payload or {}).get("infocode") or "")
    if code in {"10003", "10021"}:
        return {"items": [], "status": "rate_limit"}
    if str((payload or {}).get("status") or "") != "1":
        return {"items": [], "status": "request_failed"}
    items = _pois_from_payload(payload, city=city, limit=limit)
    return {"items": items, "status": "matched" if items else "ok_empty"}


async def search_places(client, **kwargs) -> list[dict]:
    """兼容旧调用方的列表接口。需要失败原因的候选流程改用 search_places_result。"""
    result = _record_poi(await search_places_result(client, **kwargs))
    return list(result.get("items") or [])


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


def _location_qualifier(text: str) -> str:
    """返回未闭合括号里没写完的地点限定词，例如「长城（八达岭」→「八达岭」。

    只有带开括号却没带闭括号的内容才算地点线索：完整闭合的「故宫（紫禁城）」
    是别名/补充说明，不能把「紫禁城」当成需要同时出现在候选名里的地名；
    「慕田峪）」这种只有闭括号的也不算。
    """
    match = re.search(r"[（(]([^）)]*)$", text or "")
    return match.group(1).strip() if match else ""


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
    qualifier = _location_qualifier(query)

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
        # 未闭合括号里的地点限定词（「长城（八达岭」→「八达岭」）必须参与定址：
        # 否则「八达岭长城」与「八达岭水关长城」同分，只能返回歧义。
        # 地名与景点类型紧密拼接（八达岭长城）比中间插入其它字（八达岭水关长城）
        # 更贴近用户原话，加分更高；两者仍同分时继续走下面的歧义保护。
        if qualifier and qualifier in poi_name and core and core in poi_name:
            score += 25 if poi_name.startswith(qualifier + core) else 10
        return score

    ranked = [(rank(hit), index, hit) for index, hit in enumerate(hits)]
    matched = [row for row in ranked if row[0] is not None]
    if matched:
        matched.sort(key=lambda row: (-row[0], row[1]))
        top = matched[0][0]
        tied = [row for row in matched if row[0] == top]
        # 同一可信度的多个不同地点没有可解释的胜出者，不能借搜索返回顺序静默定址。
        # 但名字完全相同的重复条目是同一个 POI，取第一个不算「用顺序决定地点」。
        if len(tied) > 1 and len({str(row[2].get("name")) for row in tied}) > 1:
            return None
        chosen = tied[0][2]
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


def _unlocated_entry(name: str, *, kind: str = "sight", reason: str = "", asked_name: str = "") -> dict:
    return {
        "name": str(name or "").strip(),
        "kind": kind,
        "reason": str(reason or ""),
        "asked_name": str(asked_name or name or "").strip(),
    }


def _sight_names(unlocated: list | None) -> list[str]:
    """未能定位的景点名，供文本展示；住宿与接驳端点不写进「未能定位」。"""
    names: list[str] = []
    for item in unlocated or []:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "sight")
            name = str(item.get("name") or "").strip()
        else:
            kind, name = "sight", str(item or "").strip()
        if name and kind in {"sight", "must_visit"}:
            names.append(name)
    return names


def _wanted_from(hints: list[dict] | None, must_visit: list[str] | None, lodging: str | None) -> list[tuple[str, dict]]:
    """先模型提示、再用户必去；住宿类提示不进候选池，候选自带 must_visit 一律不信。"""
    wanted: list[tuple[str, dict]] = []
    seen_names: set[str] = set()
    musts = {str(name).strip() for name in (must_visit or []) if str(name).strip()}
    for hint in hints or []:
        name = str(hint.get("name") or "").strip()
        if not name or name in seen_names:
            continue
        if name not in musts and _is_lodging_hint(name, lodging):
            continue
        seen_names.add(name)
        wanted.append((name, hint))
    for name in must_visit or []:
        name = str(name or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        wanted.append((name, {"name": name, "must_visit": True}))
    return wanted


def _copy_hint_duration(picked: dict, hint: dict) -> None:
    """定位只替换身份与坐标，用户/上游已给出的时长及来源必须随候选保留。"""
    if hint.get("visit_minutes") is not None:
        picked["visit_minutes"] = hint["visit_minutes"]
    for key in ("visit_duration_source", "visit_duration_confidence"):
        if hint.get(key) is not None:
            picked[key] = hint[key]


def _lodging_state(
    stay_text: str,
    stay: dict | None,
    *,
    scope: str | None,
    queried: bool = True,
) -> tuple[dict | None, list[dict]]:
    """住宿精度与三态。区域参考不加「已确认」语义，具体地点匹配失败按未定位处理。

    `queried=False` 表示这次没来得及查（截止或取消），不猜它是否可定位，也不声称已核实。
    """
    if not stay_text:
        return None, []
    if not queried:
        return None, []
    notes: list[dict] = []
    kind, precision = _lodging_precision(stay, stay_text, scope=scope)
    if kind == "area":
        if scope != "area":
            notes.append(_unlocated_entry(stay_text, kind="lodging", reason="area"))
    elif kind == "unresolved":
        notes.append(_unlocated_entry(stay_text, kind="lodging", reason="unresolved"))
    elif kind == "none":
        notes.append(_unlocated_entry(stay_text, kind="lodging", reason="unresolved"))
    if stay is None:
        return None, notes
    location = dict(stay)
    location["poi_name"] = str(stay.get("name") or "")
    location["name"] = stay_text
    location["asked_name"] = stay_text
    location["lodging_kind"] = kind
    location["precision"] = precision
    return location, notes


def collect_candidates(
    opener,
    *,
    city: str,
    days: int,
    lodging: str | None,
    must_visit: list[str] | None = None,
    hints: list[dict] | None = None,
    lodging_scope: str | None = None,
    candidate_limit: int | None = None,
    deadline: float | None = None,
    clock=None,
    cancel_event=None,
) -> tuple[dict | None, list[dict], list[dict]]:
    """高德结果覆盖身份和坐标。必去项不在候选里时按名称单查，泛搜不能顶替。"""
    clock = clock or time.monotonic

    def _expired() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        return deadline is not None and clock() >= deadline

    stay_text = (lodging or "").strip()
    expired = _expired()
    hit = None if expired else _lodging_hit(opener, city, lodging)
    stay, unlocated = _lodging_state(stay_text, hit, scope=lodging_scope, queried=not expired)
    wanted = _wanted_from(hints, must_visit, lodging)
    musts = {str(name).strip() for name in (must_visit or []) if str(name).strip()}
    resolved: list[dict] = []
    taken: set[str] = set()
    if wanted:
        for name, hint in wanted:
            if _expired():
                unlocated.append(_unlocated_entry(name, kind="must_visit" if name in musts else "sight", reason="deadline"))
                continue
            picked = _pick(_search(opener, city=city, keywords=name, types="", limit=3), name)
            if picked is None or picked["name"] in taken:
                unlocated.append(
                    _unlocated_entry(name, kind="must_visit" if name in musts else "sight", reason="not_found")
                )
                continue
            picked["must_visit"] = name in musts
            picked["asked_name"] = name
            picked["reason"] = str(hint.get("reason") or "")
            _copy_hint_duration(picked, hint)
            resolved.append(picked)
            taken.add(picked["name"])
    # 必去往往挤在同一片，泛搜只补空天，不能代替没定位到的必去。
    limit = min(25, max(_PER_DAY, int(candidate_limit or 0)))
    found = []
    if len(resolved) < limit and not _expired():
        found = _search(opener, city=city, keywords="景点", types="110000", limit=limit)
    stay_ids = {str((stay or {}).get("poi_id") or "")}
    for poi in found:
        if poi["name"] in taken or str(poi.get("poi_id") or "") in stay_ids:
            continue
        if not poi.get("poi_id"):
            poi["poi_id"] = poi["name"]
        resolved.append(poi)
        taken.add(poi["name"])
        if len(resolved) >= limit:
            break
    return stay, resolved, unlocated


async def collect_candidates_async(
    client,
    *,
    city: str,
    days: int,
    lodging: str | None,
    must_visit: list[str] | None = None,
    hints: list[dict] | None = None,
    lodging_scope: str | None = None,
    candidate_limit: int | None = None,
    budget=None,
    cancel_event=None,
    stats=None,
) -> tuple[dict | None, list[dict], list[dict]]:
    """与 collect_candidates 同一套挑选规则，查询走可取消的异步客户端。"""

    def _expired() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        return budget is not None and budget.work_left() <= 0

    async def find(keywords: str, types: str, limit: int) -> dict:
        return _record_poi(await search_places_result(
            client,
            city=city,
            keywords=keywords,
            types=types,
            limit=limit,
            budget=budget,
            cancel_event=cancel_event,
            stats=stats,
        ))

    stay_hit = None
    text = (lodging or "").strip()
    lodging_queried = False
    if text and not _expired():
        lodging_queried = True
        for keywords in (text, _strip_area(text)):
            if not keywords:
                continue
            result = await find(keywords, "", 3)
            found = result["items"]
            # 同 _lodging_hit：按当前查询词匹配，否则短名结果会被原串的「附近」挡掉。
            candidate = _pick(found, keywords) if found else None
            if candidate is not None:
                candidate["asked_name"] = text
                stay_hit = candidate
                break
    stay, unlocated = _lodging_state(text, stay_hit, scope=lodging_scope, queried=lodging_queried)
    wanted = _wanted_from(hints, must_visit, lodging)
    musts = {str(name).strip() for name in (must_visit or []) if str(name).strip()}
    resolved: list[dict] = []
    taken: set[str] = set()
    for name, hint in wanted:
        if _expired():
            unlocated.append(_unlocated_entry(name, kind="must_visit" if name in musts else "sight", reason="deadline"))
            continue
        result = await find(name, "", 3)
        picked = _pick(result["items"], name)
        # 仅成功空结果或名称匹配不足才尝试一次核心名；失败、限流和预算耗尽不改名重试。
        core = _core_name(name)
        if picked is None and result["status"] in {"ok_empty", "matched"} and core and core != name:
            result = await find(core, "", 3)
            picked = _pick(result["items"], core)
        if picked is None or picked["name"] in taken:
            reason = "ambiguous" if result["status"] == "matched" else result["status"]
            unlocated.append(
                _unlocated_entry(name, kind="must_visit" if name in musts else "sight", reason=reason)
            )
            continue
        picked["must_visit"] = name in musts
        picked["asked_name"] = name
        picked["reason"] = str(hint.get("reason") or "")
        _copy_hint_duration(picked, hint)
        resolved.append(picked)
        taken.add(picked["name"])
    limit = min(25, max(_PER_DAY, int(candidate_limit or 0)))
    found = []
    if len(resolved) < limit and not _expired():
        found = (await find("景点", "110000", limit))["items"]
    stay_ids = {str((stay or {}).get("poi_id") or "")}
    for poi in found:
        if poi["name"] in taken or str(poi.get("poi_id") or "") in stay_ids:
            continue
        if not poi.get("poi_id"):
            poi["poi_id"] = poi["name"]
        resolved.append(poi)
        taken.add(poi["name"])
        if len(resolved) >= limit:
            break
    return stay, resolved, unlocated


ROUTE_STATES = ("ok", "no_route", "failed", "not_queried")


def route_state_from_payload(mode: str, payload: dict) -> tuple[str, int | None]:
    """一次路线响应的结果状态。not_queried 只在没发请求时出现，这里不会返回。

    ok：拿到正数时长；no_route：请求成功但没路径或时长为 0；
    failed：状态码失败、infocode 报错（限流也在内，由调用方单独记次数）。
    """
    if str(payload.get("status")) != "1":
        return "failed", None
    route = payload.get("route") or {}
    if mode == "transfer":
        rows = route.get("transits") or []
        raw = rows[0].get("duration") if rows else None
    else:
        rows = route.get("paths") or []
        raw = rows[0].get("duration") if rows else None
    if raw is None or str(raw).strip() == "":
        return "no_route", None
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return "no_route", None
    if seconds <= 0:
        return "no_route", None
    return "ok", seconds


def duration_from_payload(mode: str, payload: dict) -> int | None:
    """只取时长。0 秒和没有路径都算没查到，不当成「走 0 分钟」。"""
    return route_state_from_payload(mode, payload)[1]


def _route_failure_class(http_status: int | None, infocode: str | None) -> str | None:
    """把路线请求失败归类到顶层粗粒度 error。no_key 只留给本地未配置密钥（由调用方在 _key() 为空时返回）。

    高德官方错误码：10001=密钥不正确或过期、10002=服务权限或请求路径问题、
    10003=日调用量超限、10021=账号在该接口的 QPS 超限。

    - HTTP 429 / infocode 10003 / 10021 → rate_limit（限流）
    - HTTP 401、403 / infocode 10001、10002 → request_failed（鉴权/权限问题，不是本地缺 key）
    - 其余返回 None，由调用方落到默认 request_failed。
    """
    code = str(infocode or "").strip()
    if http_status == 429 or code in ("10003", "10021"):
        return "rate_limit"
    if http_status in (401, 403) or code in ("10001", "10002"):
        return "request_failed"
    return None


# 这些错误的成因不在单条路段，重试后续路段必然同样失败；命中后立即关闭路线预算。
_ROUTE_FATAL_ERRORS = ("rate_limit", "request_failed")


def _constraints(
    city,
    day,
    days,
    stay,
    transport_mode,
    must_visit,
    avoid,
    facts,
    query_text,
    rail_window=None,
    plan_basis=None,
    lodging_scope=None,
    pace=None,
) -> dict:
    location = dict(stay) if isinstance(stay, dict) else None
    return {
        "date": day,
        "days": days,
        "city": city,
        "lodging_poi": location,
        "lodging_kind": str((location or {}).get("lodging_kind") or ""),
        "lodging_precision": str((location or {}).get("precision") or ""),
        "lodging_scope": lodging_scope,
        # 有 exact/area 精度就算「锚点可用」：可以按它排首末接驳；
        # 但它不等于「酒店已确认」，确认语义只看 lodging_loop_verified。
        "lodging_anchor_ready": bool(location) and str(location.get("precision") or "") in _ANCHOR_READY_PRECISIONS,
        "lodging_loop_verified": bool(location) and str(location.get("precision") or "") in _LOOP_VERIFIED_KINDS,
        "transport_mode": transport_mode or "public_transit",
        "pace": str(pace or ""),
        "must_visit": list(must_visit or []),
        "avoid": list(avoid or []),
        "facts": list(facts or []),
        "unverified_clock": clock_text_unverified(query_text or ""),
        "rail_window": dict(rail_window or {}),
        "plan_basis": dict(plan_basis or {}),
    }


def _context(plan: dict, unlocated: list, facts_status: str, route_error: str | None, facts: list[dict] | None = None) -> str:
    lines = [itinerary_markdown(plan).rstrip()]
    names = _sight_names(unlocated)
    if names:
        lines.append("未能定位：" + "、".join(names))
    if facts_status == "unavailable":
        lines.append("开放规则未核实（搜索不可用）")
    links: list[str] = []
    for fact in facts or []:
        # 这段文本直接进成文提示词，链接必须是白名单内的公网 http(s)。
        # 上游已经把不合格链接的 source_url 清空，这里再校验一次，防止别处
        # 绕过 parse_evidence 直接构造事实。
        url = safe_evidence_url(fact.get("source_url"), ALLOWED_HOSTS)
        if url and url not in links:
            links.append(url)
    if links:
        lines.append("证据链接：" + " ".join(links))
    if route_error:
        # need_route=False 时路线查询被短路，route_error 用稳定码 not_queried；
        # 写进正文与地图上下文前翻成中文，不能让裸英文码出现在用户可见文本里。
        reason = {
            "not_queried": "用户明确不查路线，本次未查询交通耗时",
        }.get(str(route_error), str(route_error))
        lines.append(f"交通未核实：{reason}")
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


def _date_unknown_item(poi: dict, role: str) -> dict:
    """日期未知时的行程点：只带坐标与名称，不编造时刻。

    字段与 `route_plan._point` 对齐，`days_to_tasks` 与正文渲染都按同一套键读，
    否则兜底地图会和结构化行程走出两种形状。
    """
    item = {
        "poi_id": poi.get("poi_id") or poi.get("name"),
        "name": poi.get("name") or "",
        "role": role,
        "lng": poi.get("lng"),
        "lat": poi.get("lat"),
        "start": "",
        "end": "",
    }
    for key in ("lodging_kind", "precision", "poi_name"):
        if poi.get(key) is not None:
            item[key] = poi.get(key)
    return item


def _plan_without_date(plan: dict, sights: list[dict], stay: dict | None, days: int) -> dict:
    """日期未知时的兜底行程：按候选顺序均分到各天，保留住宿锚点，不假装已排程。

    只在 `schedule()` 返回 `date_unknown`（没有任何可排的日期）时使用。它存在的
    理由是：这条兜底路径本来就没有结构化行程，用户仍应看得到景点位置；但它必须
    如实标注 unverified / 日期待定，不能让正文读成一份已核实的三日游。
    """
    count = max(1, int(days or 1))
    buckets: list[list[dict]] = [[] for _ in range(count)]
    for index, poi in enumerate(sights):
        buckets[index % count].append(poi)
    rows = []
    for index, bucket in enumerate(buckets, 1):
        items = []
        if stay is not None:
            items.append(_date_unknown_item(stay, "depart"))
        items.extend(_date_unknown_item(poi, "visit") for poi in bucket)
        if stay is not None:
            items.append(_date_unknown_item(stay, "return"))
        rows.append(
            {
                "day": index,
                "date": None,
                "label": f"第{index}天",
                "items": items,
                "legs": [],
                "notes": ["出行日期未确定，开放时间与交通耗时都还没核实"],
                "unverified": True,
                "skip": False,
                "arrival_only": False,
                "window": {
                    "state": "unknown",
                    "start": None,
                    "end": None,
                    "reason": "出行日期未确定，当天可用时段待定",
                    "basis": None,
                    "date_unknown": True,
                },
            }
        )
    out = dict(plan)
    out["days"] = rows
    out["schedule_status"] = "date_unknown"
    notes = list(out.get("notes") or [])
    note = "景点按候选顺序临时分配，出行日期确定后需要重新排程"
    if note not in notes:
        notes.append(note)
    out["notes"] = notes
    return out


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
        names = _sight_names(unlocated)
        if names:
            return "未能定位：" + "、".join(names)
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
    if not tasks and plan.get("schedule_status") == "date_unknown":
        # 日期未知时 schedule 只给出占位天（items 为空），不能因此判定「什么都排不出来」
        # 而不写地图：这条路本来就是给「没有结构化行程、只能兜底出图」的场景用的，
        # 用户仍然要看得到景点位置。按候选顺序均分到各天，不编造日期与时刻。
        plan = _plan_without_date(plan, sights, stay, days)
        tasks = days_to_tasks(plan, city)
    if not tasks:
        logger.warning("map_fallback empty plan_id=%s city=%s", pid, city)
        return None
    path = MAPS_DIR / f"{pid}.html"
    path.write_text(render_map_html(tasks), encoding="utf-8")
    head = "高德地点检索补写的路线地图"
    if stay is not None:
        # 只有精确到酒店的住宿才能说「从住宿地出发」；区域参考点只是接驳的落脚点。
        precision = str(stay.get("precision") or "")
        if precision in {"area", "fallback"}:
            head += "（每天按住宿区域参考点接驳，具体安排待定）"
        else:
            head += "（每天从住宿地出发，结束回到住宿地）"
    note = head + "：\n" + itinerary_markdown(plan)
    names = _sight_names(unlocated)
    if names:
        note += "未能定位：" + "、".join(names) + "\n"
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
    """当前行程的相邻段。已缓存的键不再查询。

    公共交通同时列出步行和换乘两种键：步行先查，换乘只补真正需要的段。
    """
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


def _walk_verdict(table: dict, states: dict, origin_id: str, dest_id: str, day: str, depart: int) -> str:
    """步行已经够好了就不必再查换乘。返回 skip 表示不必查，query 表示该查。"""
    walk_key = route_cache_key(origin_id, dest_id, "walking", day, depart)
    state = states.get(walk_key)
    if state is not None:
        if state == "ok":
            seconds = table.get(walk_key)
            if isinstance(seconds, int) and seconds <= WALK_MAX_SECONDS:
                return "skip"
            return "query"
        if state in ("failed", "not_queried"):
            # 步行没查成不是「太远」，但要避免继续堆请求。
            return "skip"
        return "query"
    cached = table.get(walk_key)
    if walk_key in table:
        if isinstance(cached, int) and cached <= WALK_MAX_SECONDS:
            return "skip"
        return "query"
    return "query"


async def fill_routes(
    plan: dict,
    table: dict,
    *,
    city: str,
    transport_mode: str,
    client=None,
    clock=None,
    cancel_event=None,
    budget=None,
    on_attempt=None,
    stats=None,
    states=None,
) -> str | None:
    """补查相邻段。预算用尽或失败时留下未知耗时，不把直线距离当成耗时。

    两批在同一次调用内完成：先查步行（或唯一模式），再只为真正需要的段补换乘。
    第二批不另起一轮，避免「刚补上换乘、重新排程又产生新边、轮次已结束」。
    返回的 error 区分 budget / rate_limit / request_failed / no_route。
    """
    import asyncio

    import httpx

    from planner.travel_search import SearchBudget

    if not _key():
        note_stat("map_route", "dependency_missing")
        bump_stat("map_route")
        return "no_key"
    clock = clock or (lambda: asyncio.get_running_loop().time())
    own_budget = budget is None
    if own_budget:
        budget = SearchBudget(clock=clock, limit=60, window=60.0, max_timeout=6.0)
    own = client is None
    if own:
        client = httpx.AsyncClient()
    states = states if states is not None else {}
    error = None
    pending = needed_route_queries(plan, transport_mode, table)
    batches = [
        [item for item in pending if item[3] != "transfer"],
        [item for item in pending if item[3] == "transfer"],
    ]
    try:
        for batch in batches:
            for key, origin, dest, mode, day, depart in batch:
                if cancel_event is not None and cancel_event.is_set():
                    budget.closed = True
                    states[key] = "not_queried"
                    break
                if mode == "transfer" and _walk_verdict(table, states, origin["poi_id"], dest["poi_id"], day, depart) == "skip":
                    states[key] = "not_queried"
                    continue
                timeout = await budget.reserve()
                if timeout is None:
                    error = error or "budget"
                    states[key] = "not_queried"
                    budget.closed = True
                    break
                if on_attempt is not None and not on_attempt():
                    error = error or "budget"
                    states[key] = "not_queried"
                    budget.closed = True
                    break
                params = {
                    "key": _key(),
                    "origin": f"{origin['lng']},{origin['lat']}",
                    "destination": f"{dest['lng']},{dest['lat']}",
                }
                if mode == "transfer":
                    params["city"] = city
                    params["cityd"] = city
                # 高德公交换乘接口本身支持 date/time（官方路径规划文档），步行/驾车也接受。
                # 带上规划日期与出发时段，让返回的耗时更接近实际，但接口结果仍只标 reference。
                if day:
                    params["date"] = day
                depart_min = int(depart) if isinstance(depart, (int, float)) else None
                if depart_min is not None and 0 <= depart_min < 24 * 60:
                    params["time"] = f"{depart_min // 60:02d}:{depart_min % 60:02d}"
                _note(stats, "route_requests")
                http_status: int | None = None
                payload: dict | None = None
                try:
                    response = await client.get(_ROUTES[mode], params=params, timeout=timeout)
                    http_status = int(getattr(response, "status_code", 0) or 0)
                    response.raise_for_status()
                    payload = response.json()
                except Exception:
                    code = str((payload or {}).get("infocode", "")) if payload is not None else ""
                    klass = _route_failure_class(http_status, code or None)
                    error = error or (klass or "request_failed")
                    states[key] = "failed"
                    if klass in _ROUTE_FATAL_ERRORS:
                        # 限流或密钥/权限问题不是单条路段的偶发失败：继续请求只会
                        # 继续失败，并把剩余预算烧在必然失败的调用上。
                        budget.closed = True
                    if budget.closed:
                        break
                    continue
                if budget.closed:
                    states[key] = "not_queried"
                    break
                infocode = str((payload or {}).get("infocode") or "")
                if infocode in ("10003", "10021"):
                    _note(stats, "amap_rate_limits")
                    error = error or "rate_limit"
                    states[key] = "failed"
                    # 记成 None 而不是干脆不写：这条路段已经查过了，只是没拿到可用耗时，
                    # 后续补边循环不该再为它发一次请求。
                    table[key] = None
                    budget.closed = True
                    break
                if infocode in ("10001", "10002"):
                    error = error or "request_failed"
                    states[key] = "failed"
                    table[key] = None
                    budget.closed = True
                    break
                state, seconds = route_state_from_payload(mode, payload or {})
                if state == "no_route":
                    error = error or "no_route"
                    states[key] = "no_route"
                else:
                    states[key] = state
                table[key] = seconds
            if budget.closed:
                break
    finally:
        if own_budget:
            budget.closed = True
        if own:
            await client.aclose()
    _record_route_error(error)
    return error


def duration_lookup(table: dict):
    def duration_fn(origin_id, dest_id, mode, day, depart_min):
        return table.get(route_cache_key(origin_id, dest_id, mode, day, depart_min))

    return duration_fn


def _trim_hints(
    hints: list[dict] | None,
    must_visit: list[str] | None,
    days: int,
    lodging: str | None = None,
    candidate_target: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """候选截断，按用户 m04926 第 7 条的顺序：

    1. 先认用户明确必去（只认 must_visit 点名，候选自带的 must_visit 标记只当提示，不单独放行）；
    2. 排除明确的住宿说明 / 已知接驳说明（模型常把住宿地写进候选）；
    3. 按名称去重，再计普通候选额度；
    4. 截断发生在检索之前，才不会先花掉额度再丢弃。定位后由 sanitize_candidates 按可信类型再过滤。
    """
    musts = {str(name).strip() for name in (must_visit or []) if str(name).strip()}
    # 默认常量只保留保护下限；本轮容量目标决定正常候选准备上限。
    quota = min(25, max(_PER_DAY, int(candidate_target or 0)))
    keep: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    normal = 0
    for hint in hints or []:
        name = str(hint.get("name") or "").strip()
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        if name in musts:
            keep.append(hint)
            continue
        if _is_lodging_hint(name, lodging):
            continue
        if normal < quota:
            keep.append(hint)
            normal += 1
        else:
            dropped.append({"name": name, "reason": "候选过多，未检索"})
    return keep, dropped


def _supplement_rank(poi: dict, anchors: list[dict]) -> float:
    if not anchors:
        return 0.0
    return min((float(poi["lng"]) - float(anchor["lng"])) ** 2 + (float(poi["lat"]) - float(anchor["lat"])) ** 2 for anchor in anchors)


def _supplement_keyword(preferences: str | None) -> str:
    text = str(preferences or "")
    for token in ("博物馆", "园林", "历史", "文化", "自然", "公园", "街区", "亲子"):
        if token in text:
            return f"{token} 景点"
    return "景点"


async def _supplement_candidates(client, *, city: str, sights: list[dict], plan: dict, stay: dict | None, preferences: str | None, budget, cancel_event, stats) -> tuple[list[dict], dict]:
    """一次全局补充轮：按缺口日期共享预算查询，3km 只是排序/检索优先范围。"""
    supply = plan.get("candidate_supply") or {}
    gaps = list(supply.get("final_gap") or [])
    if not supply.get("shortage") or not gaps:
        return [], {"reason": "not_needed", "queries": [], "discovered": 0}
    seen = {str(poi.get("poi_id") or poi.get("name") or "") for poi in sights}
    by_day = {day.get("day"): day for day in plan.get("days") or []}
    candidates: list[dict] = []
    queries: list[dict] = []
    # 最大 6 个新增候选是全局保护上限；每个缺口日只拿自己需要的少量替补，
    # 不让单日的搜索结果吞掉整轮预算。
    remaining_cap = 6
    for gap in sorted(gaps, key=lambda row: (-int(row.get("minutes") or 0), row.get("day") or 0)):
        if remaining_cap <= 0:
            break
        day = by_day.get(gap.get("day")) or {}
        anchors = [item for item in day.get("items") or [] if item.get("role") == "visit" and item.get("lng") is not None]
        anchor = anchors[0] if anchors else stay if isinstance(stay, dict) and stay.get("lng") is not None else None
        location = f"{anchor['lng']},{anchor['lat']}" if anchor is not None else None
        result = _record_poi(await search_places_result(
            client,
            city=city,
            keywords=_supplement_keyword(preferences),
            types="110000",
            limit=min(6, max(2, math.ceil(int(gap.get("minutes") or 0) / 90) + 1)),
            budget=budget,
            cancel_event=cancel_event,
            stats=stats,
            location=location,
            radius=3000 if location else None,
        ))
        status = str(result.get("status") or "request_failed")
        queries.append({"day": gap.get("day"), "status": status, "anchor": "day" if anchors else "lodging" if anchor else "city"})
        if status != "matched":
            # 限流、预算或请求失败停止本轮；空结果可继续让下一个缺口日尝试。
            if status in {"rate_limit", "budget", "deadline", "request_failed"}:
                break
            continue
        rows = [poi for poi in result.get("items") or [] if str(poi.get("poi_id") or poi.get("name") or "") not in seen]
        rows.sort(key=lambda poi: (_supplement_rank(poi, [anchor] if anchor else []), poi["name"]))
        wanted = min(remaining_cap, max(1, math.ceil(int(gap.get("minutes") or 0) / 120)))
        for poi in rows[:wanted]:
            ident = str(poi.get("poi_id") or poi.get("name") or "")
            if not ident or ident in seen:
                continue
            poi["supplement_target_day"] = gap.get("day")
            candidates.append(poi)
            seen.add(ident)
            remaining_cap -= 1
            if remaining_cap <= 0:
                break
    reason = "matched" if candidates else (queries[-1]["status"] if queries else "not_needed")
    return candidates, {"reason": reason, "queries": queries, "discovered": len(candidates)}


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
    attempt_budget=None,
    stats=None,
    plan_basis: dict | None = None,
    rail_window: dict | None = None,
    lodging_scope: str | None = None,
    pace: str | None = None,
    preferences: str | None = None,
    need_route: bool = True,
) -> dict:
    """候选校验、搜索、相邻耗时、最多两轮修正，再对最终新增边做只读检查。

    need_route=False（用户明确不查路线）时保留景点候选与结构化行程，
    但不发出任何路线 HTTP 请求：相邻段一律 not_queried、耗时未知，
    到达时刻保持 None，质量层据此判 travel_time=unverified / partial。
    """
    import asyncio
    from functools import partial

    import httpx

    from planner.travel_search import SearchBudget, gather_facts

    if stats is None:
        stats = {"poi_requests": 0, "route_requests": 0, "amap_rate_limits": 0}
    started = time.monotonic()
    client = httpx.AsyncClient()
    preliminary = _constraints(
        city, date, days, None, transport_mode, must_visit, avoid, [], query_text,
        rail_window=rail_window, plan_basis=plan_basis, lodging_scope=lodging_scope, pace=pace,
    )
    candidate_target = candidate_retrieval_target(preliminary)
    hints, dropped_hints = _trim_hints(hints, must_visit, days, lodging, candidate_target=candidate_target)
    # 必要路段（每天景点数 + 抵离住宿的两段）要把次数和时间留出来：
    # 否则 POI 与取证会把额度和截止时间吃光，路线阶段只能全部记 budget。
    # 日期未知时候选目标数为 None（不拿默认日期估算密度），这里按 0 处理。
    route_pairs = max(1, (candidate_target or 0) + max(1, int(days or 1)))
    # 结构化诊断：把候选检索预算与推导口径一并写进结果，供回放/排障对齐。
    # candidate_target 是「按天可用窗口×节奏密度」推导的检索预算，不是必去项数量、
    # 不是候选池总上限，也不进入 plan/candidate_supply 的键集合，故只放在 constraints。
    # 值只依赖日期/天数/节奏，稳定无时钟，可安全随快照保存与复用。
    candidate_retrieval = {
        "candidate_target": candidate_target,
        "reason": (
            "出行日期缺失：无每天可用窗口，不推导检索预算"
            if candidate_target is None
            else f"按天可用窗口与节奏密度累加推导（days={int(days or 1)}、"
                 f"必去项={len(must_visit or [])}、路线预留={route_pairs}）；"
                 "只用作景点泛搜 page_size、hints 截断与路线预留基数"
        ),
    }
    if attempt_budget is not None and hasattr(attempt_budget, "set_reserve"):
        attempt_budget.set_reserve(route_pairs)
    on_attempt = attempt_budget.note_attempt if attempt_budget is not None else None
    route_attempt = (
        partial(attempt_budget.note_attempt, protected=True) if attempt_budget is not None else None
    )

    try:
        poi_started = time.monotonic()
        poi_deadline = time.monotonic() + (
            20 if attempt_budget is None else min(20, attempt_budget.timeout_for(attempt_budget.evidence))
        )
        if opener is not None:
            stay, sights, unlocated = await asyncio.to_thread(
                partial(
                    collect_candidates,
                    opener,
                    city=city,
                    days=days,
                    lodging=lodging,
                    must_visit=must_visit,
                    hints=hints,
                    lodging_scope=lodging_scope,
                    candidate_limit=candidate_target,
                    deadline=poi_deadline,
                    cancel_event=cancel_event,
                )
            )
        else:
            stay, sights, unlocated = await collect_candidates_async(
                client,
                city=city,
                days=days,
                lodging=lodging,
                must_visit=must_visit,
                hints=hints,
                lodging_scope=lodging_scope,
                candidate_limit=candidate_target,
                budget=attempt_budget,
                cancel_event=cancel_event,
                stats=stats,
            )
        poi_verify_ms = int((time.monotonic() - poi_started) * 1000)
        # 取证只花「扣掉必要路段预留之后」的时间：路线补查永远拿得到一点窗口。
        evidence_cap = (
            60.0
            if attempt_budget is None
            else min(60.0, attempt_budget.work_left_without_reserve())
        )
        evidence_end = asyncio.get_running_loop().time() + max(0.0, evidence_cap)

        def evidence_left() -> float:
            return max(0.0, evidence_end - asyncio.get_running_loop().time())

        api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
        facts_started = time.monotonic()
        found = await gather_facts(
            sights,
            city=city,
            day=date or "",
            api_key=api_key,
            cancel_event=cancel_event,
            window=evidence_left(),
            on_attempt=on_attempt,
        )
        facts_ms = int((time.monotonic() - facts_started) * 1000)
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
            rail_window=rail_window,
            plan_basis=plan_basis,
            lodging_scope=lodging_scope,
            pace=pace,
        )
        table: dict = {}
        mode = constraints["transport_mode"]
        schedule_ms = 0
        route_ms = 0

        def timed_plan(*, assignment=None, withdraw_first=None):
            nonlocal schedule_ms
            mark = time.monotonic()
            planned = schedule(sights, constraints, duration_lookup(table), assignment=assignment, withdraw_first=withdraw_first)
            schedule_ms += int((time.monotonic() - mark) * 1000)
            return planned

        async def timed_routes():
            nonlocal route_ms
            mark = time.monotonic()
            try:
                if not need_route:
                    # 用户明确不查路线：不发任何 HTTP。把当前待查段一律标
                    # not_queried，并写进 table（None=耗时未知）以终结后续补边
                    # 循环；结构化行程仍由 timed_plan() 保留，起止时刻保持 None。
                    for key, *_rest in needed_route_queries(plan, mode, table):
                        route_states[key] = "not_queried"
                        table[key] = None
                    note_stat("map_route", "not_needed")
                    bump_stat("map_route")
                    return "not_queried"
                return await fill_routes(
                    plan,
                    table,
                    city=city,
                    transport_mode=mode,
                    client=client,
                    cancel_event=cancel_event,
                    budget=route_budget,
                    on_attempt=route_attempt,
                    stats=stats,
                    states=route_states,
                )
            finally:
                route_ms += int((time.monotonic() - mark) * 1000)

        route_error = None
        route_states: dict = {}
        route_window = 60.0 if attempt_budget is None else attempt_budget.reserved_seconds()
        route_budget = SearchBudget(
            clock=lambda: asyncio.get_running_loop().time(), limit=60, window=route_window, max_timeout=6.0,
        )
        plan = timed_plan()
        supplement = {"attempted": False, "discovered": 0, "accepted": 0, "scheduled": 0, "withdrawn": 0, "reason": "not_needed", "queries": []}
        if sights:
            # 一次 fill_routes 内部就完成「先步行、再换乘」两批；
            # 这里最多再排一次序，把新产生的边补上，不再额外空转一轮。
            for _ in range(2):
                missing = needed_route_queries(plan, mode, table)
                if not missing:
                    break
                route_error = await timed_routes() or route_error
                plan = timed_plan()
            if needed_route_queries(plan, mode, table):
                # 重新排程又产生了新边：只补这些，不再重排，避免无限循环。
                route_error = await timed_routes() or route_error
                plan = timed_plan()
            plan = timed_plan()
        # 零个初始候选同样可以走一次城市级兜底补充；它不能被 if sights 阻断。
        if opener is None and (plan.get("candidate_supply") or {}).get("shortage"):
            supplement["attempted"] = True
            extra, meta = await _supplement_candidates(
                client, city=city, sights=sights, plan=plan, stay=stay, preferences=preferences,
                budget=attempt_budget, cancel_event=cancel_event, stats=stats,
            )
            supplement.update(meta)
            supplement["discovered"] = len(extra)
            extra = sanitize_candidates(extra, constraints)
            supplement["accepted"] = len(extra)
            if extra:
                extra_found = await gather_facts(
                    extra, city=city, day=date or "", api_key=api_key,
                    cancel_event=cancel_event, window=evidence_left(), on_attempt=on_attempt,
                )
                found["facts"].extend(extra_found.get("facts") or [])
                found["attempts"] = int(found.get("attempts") or 0) + int(extra_found.get("attempts") or 0)
                if not found.get("error"):
                    found["error"] = extra_found.get("error")
                supplement["evidence"] = {
                    "attempts": extra_found.get("attempts"), "status": extra_found.get("facts_status"),
                    "error": extra_found.get("error"), "usage": extra_found.get("usage"),
                }
                constraints["facts"] = [fact for fact in found["facts"] if fact.get("status") == "hard"]
                prior_assignment = [list(row) for row in plan.get("assignment") or []]
                while len(prior_assignment) < int(days or 1):
                    prior_assignment.append([])
                sights.extend(extra)
                extra_ids = {str(poi.get("poi_id") or poi.get("name") or "") for poi in extra}
                for poi in extra:
                    target = int(poi.get("supplement_target_day") or 0) - 1
                    if 0 <= target < len(prior_assignment):
                        prior_assignment[target].append(str(poi.get("poi_id") or poi.get("name") or ""))
                # 补充只影响目标缺口日；最后路线批次若判定放不下，schedule 会优先撤回 extra_ids。
                plan = timed_plan(assignment=prior_assignment, withdraw_first=extra_ids)
                if needed_route_queries(plan, mode, table):
                    route_error = await timed_routes() or route_error
                plan = timed_plan(assignment=prior_assignment, withdraw_first=extra_ids)
                scheduled = {poi_id for row in plan.get("assignment") or [] for poi_id in row}
                supplement["scheduled"] = len(extra_ids & scheduled)
                supplement["withdrawn"] = len(extra_ids - scheduled)
        if isinstance(plan.get("candidate_supply"), dict):
            plan["candidate_supply"]["supplement"] = supplement
        assemble_ms = int((time.monotonic() - started) * 1000)
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
            "route_states": route_states,
            "dropped_hints": dropped_hints,
            "candidates": sights,
            "constraints": {**constraints, "candidate_retrieval": candidate_retrieval},
            "context": _context(plan, unlocated, found["facts_status"], route_error, found["facts"]),
            "stats": {
                "assemble_ms": assemble_ms,
                "poi_verify_ms": poi_verify_ms,
                "facts_ms": facts_ms,
                "route_ms": route_ms,
                "schedule_ms": schedule_ms,
                "poi_requests": int(stats.get("poi_requests") or 0),
                "route_requests": int(stats.get("route_requests") or 0),
                "amap_rate_limits": int(stats.get("amap_rate_limits") or 0),
            },
        }
    finally:
        await client.aclose()


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
        # 同 assemble_itinerary：区域参考点不能写成「从住宿地出发」。
        precision = str(stay.get("precision") or "")
        if precision in {"area", "fallback"}:
            head += "（每天按住宿区域参考点接驳，具体安排待定）"
        else:
            head += "（每天从住宿地出发，结束回到住宿地）"
    return _itinerary_note(tasks, head=head)
