"""票务原文、解析事实和模型总结分开存放。无法识别的响应不生成车次事实。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from planner.run_stats import bump as bump_stat
from planner.run_stats import note as note_stat


def _num(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# 单次票务工具查询的等待上限。任务层取消只能结束外层协程，MCP 客户端内部的
# HTTP 请求要靠这里的 wait_for 断开；取不到就按剩余工作时间等。
_TOOL_QUERY_SECONDS = _num("TICKET_TOOL_TIMEOUT_SECONDS", 30)

# 12306 MCP（Joooook/12306-mcp，本仓库默认演示端）里查询会用到的工具。
# 不在表内的工具不挂载。发现结果若改名，宁可不查，也不要全量放开。
TICKET_QUERY_TOOLS = frozenset(
    {
        "get-current-date",
        "get-stations-code-in-city",
        "get-station-code-of-citys",
        "get-station-code-by-names",
        "get-station-by-telecode",
        "get-tickets",
        "get-interline-tickets",
        "get-train-route-stations",
    }
)

# 只有这几类工具会返回车次列表，才交给车次解析器。站点、城市、当前日期等响应
# 是成功结果，只是结构不同；把它们送进车次解析器只会得到假的「无法识别」。
TRAIN_RESULT_TOOLS = frozenset({"get-tickets"})
# 中转结果结构与直达不同，解析器还没有真实样本验证，先只留原始记录。
TICKET_TOOLS_UNPARSED = frozenset({"get-interline-tickets"})

_TRAIN = re.compile(r"^[GDCZTK]\d{1,4}$")
_REQUIRED = ("train_no", "date", "origin", "destination", "seat", "price", "availability")
_TEXT_TRAIN = re.compile(
    r"^(?P<train>[GDCZTK]\d{1,4}) (?P<origin>.+?)\(telecode:[^)]+\) -> (?P<dest>.+?)\(telecode:[^)]+\) "
    r"(?P<depart>\d{2}:\d{2}) -> (?P<arrive>\d{2}:\d{2})"
    r"(?: 历时：(?P<lishi>\d{2}:\d{2}))?"
)
_TEXT_SEAT = re.compile(
    r"^- (?P<seat>[^:]+): (?:剩余(?P<count>\d+)张票|(?P<state>有票|无票)) (?P<price>\d+(?:\.\d+)?)元\s*$"
)
_PARTY = re.compile(r"(\d+)\s*(?:个|位)?\s*人")
# 用户说人数很少写阿拉伯数字——「改成三个人」「我们俩去」都是常见说法。只认数字
# 会让自然语言改人数静默失效，预算继续按旧人数算。这里只覆盖 1-99 的常见写法，
# 更大的数量本来就该用阿拉伯数字（「12 人」）。
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "俩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_PARTY_CN_TEENS = re.compile(r"(?P<tens>[一二两三四五六七八九])?十(?P<unit>[一二三四五六七八九])?\s*(?:人|个人|位)")
_PARTY_CN_SINGLE = re.compile(r"(?P<one>[一二两俩三四五六七八九])\s*(?:人|个人|位)")
# 「两个人」「俩人」这类口语不带量词，只在明确含出行语境时才认。
_PARTY_CN_BARE = re.compile(r"(?:我们|咱们|我)\s*(?P<one>[一二两俩三四五六七八九])\s*(?:个人|人)")
# 「我们俩」「咱仨」后面不带量词，只有这种人称限定才认，避免把「一个人玩」当人数。
_PARTY_CN_PLURAL = re.compile(r"(?:我们|咱们|咱|我俩|我)\s*(?P<one>俩|仨)")


def select_ticket_tools(tools: list[Any]) -> list[Any]:
    return [tool for tool in tools if getattr(tool, "name", "") in TICKET_QUERY_TOOLS]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    return str(value)


def _as_object(payload: Any) -> dict | None:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _cn_party(text: str) -> int | None:
    """从中文写法里认出人数：三个人 / 十二人 / 俩人去 / 两大一小。"""
    matched = (
        _PARTY_CN_PLURAL.search(text)
        or _PARTY_CN_BARE.search(text)
        or _PARTY_CN_TEENS.search(text)
        or _PARTY_CN_SINGLE.search(text)
    )
    if matched:
        groups = matched.groupdict()
        if "tens" in groups:
            # 「十五人」tens=一；「十人」tens 空但仍是十。
            total = _CN_DIGITS.get(groups.get("tens") or "一", 0) * 10
            if groups.get("unit"):
                total += _CN_DIGITS.get(groups["unit"], 0)
            return total or None
        one = groups.get("one")
        if one in ("俩",):
            return 2
        if one in ("仨",):
            return 3
        if one:
            return _CN_DIGITS.get(one) or None
    # 「两大一小」：两个大人加一个小孩，按三人算。
    if re.search(r"[一二两]\s*大\s*[一二两]\s*小", text):
        return 3
    return None


def party_size(*texts: str | None) -> int | None:
    for text in texts:
        match = _PARTY.search(text or "")
        if match:
            return int(match.group(1))
        found = _cn_party(text or "")
        if found:
            return found
    return None


def _rows_from_payload(payload: Any, request_date: str | None) -> tuple[list[dict] | None, str | None]:
    """把内容块、JSON 字符串、对象或 12306 文本表收成统一行。认不出返回 (None, code)。"""
    if isinstance(payload, list) and payload and all(isinstance(item, dict) and item.get("type") == "text" for item in payload):
        rows: list[dict] = []
        for item in payload:
            found, code = _rows_from_payload(item.get("text"), request_date)
            if code == "ticket_empty":
                continue
            if found is None:
                return None, code
            rows.extend(found)
        return (rows, "ticket_empty") if not rows else (rows, None)
    if isinstance(payload, str):
        text = payload.strip()
        if not text or text == "没有查询到相关车次信息":
            return [], "ticket_empty"
        try:
            return _rows_from_payload(json.loads(text), request_date)
        except json.JSONDecodeError:
            parsed = _rows_from_text(text, request_date)
            return (None, "unrecognized_ticket_payload") if parsed is None else (parsed, "ticket_empty" if not parsed else None)
    if isinstance(payload, list):
        if not payload:
            return [], "ticket_empty"
        if all(isinstance(item, dict) and item.get("start_train_code") for item in payload):
            return _rows_from_live(payload), None
        return None, "unrecognized_ticket_payload"
    data = _as_object(payload)
    if not data:
        return None, "unrecognized_ticket_payload"
    if data.get("schema") == "ticket-query-v1":
        trains = data.get("trains")
        if not isinstance(trains, list):
            return None, "unrecognized_ticket_payload"
        return trains, "ticket_empty" if not trains else None
    if data.get("start_train_code"):
        return _rows_from_live([data]), None
    return None, "unrecognized_ticket_payload"


def _rows_from_live(trains: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for train in trains:
        code = str(train.get("start_train_code") or "")
        for seat in train.get("prices") or []:
            if not isinstance(seat, dict):
                continue
            rows.append(
                {
                    "train_no": code,
                    "date": str(train.get("start_date") or ""),
                    "origin": str(train.get("from_station") or ""),
                    "destination": str(train.get("to_station") or ""),
                    "depart": str(train.get("start_time") or ""),
                    "arrive": str(train.get("arrive_time") or ""),
                    "depart_date": str(train.get("start_date") or ""),
                    "arrive_date": str(train.get("arrive_date") or ""),
                    "lishi": str(train.get("lishi") or ""),
                    "seat": str(seat.get("seat_name") or ""),
                    "price": seat.get("price"),
                    "availability": str(seat.get("num") or ""),
                }
            )
    return rows


def _rows_from_text(text: str, request_date: str | None) -> list[dict] | None:
    rows: list[dict] = []
    current: dict | None = None
    matched = False
    for raw in text.splitlines():
        line = raw.strip()
        header = _TEXT_TRAIN.match(line)
        if header:
            matched = True
            current = {
                "train_no": header.group("train"),
                "date": request_date or "",
                "origin": header.group("origin"),
                "destination": header.group("dest"),
                "depart": header.group("depart"),
                "arrive": header.group("arrive"),
                "lishi": header.group("lishi"),
            }
            continue
        seat = _TEXT_SEAT.match(line)
        if seat and current is not None:
            matched = True
            state = seat.group("state")
            count = seat.group("count")
            rows.append(
                {
                    **current,
                    "seat": seat.group("seat"),
                    "price": seat.group("price"),
                    "availability": f"剩余{count}张" if count else state,
                }
            )
    if not matched:
        return None
    return rows


def _minutes(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    head, _, tail = text.partition(":")
    if not head.isdigit() or not tail.isdigit():
        return None
    return int(head) * 60 + int(tail)


def _clock_fields(row: dict) -> dict:
    """抵离时刻与日期。抵达日期只从工具字段或「出发日期 + 历时」得到，不靠钟点大小猜跨日。"""
    depart = str(row.get("depart") or "").strip() or None
    arrive = str(row.get("arrive") or "").strip() or None
    depart_date = str(row.get("depart_date") or row.get("date") or "").strip() or None
    arrive_date = str(row.get("arrive_date") or "").strip() or None
    source = "tool" if arrive_date else None
    lishi = str(row.get("lishi") or "").strip() or None
    if arrive_date is None and depart_date and lishi:
        try:
            start = date.fromisoformat(depart_date)
        except ValueError:
            start = None
        span = _minutes(lishi)
        if start is not None and span is not None:
            arrive_date = (start + timedelta(minutes=span)).isoformat()
            source = "derived_from_lishi"
    return {
        "depart": depart,
        "arrive": arrive,
        "depart_date": depart_date,
        "arrive_date": arrive_date,
        "lishi": lishi,
        "arrive_date_source": source,
    }


def parse_ticket_payload(
    payload: Any,
    *,
    source_id: str,
    queried_at: str,
    expect_date: str | None,
    origin: str | None,
    destination: str | None,
    request_date: str | None = None,
) -> tuple[list[dict], dict | None]:
    """内容块、JSON 和 12306 文本表都能进事实表。认不出就只留诊断。"""
    trains, code = _rows_from_payload(payload, request_date)
    if trains is None:
        return [], {"code": code or "unrecognized_ticket_payload", "source_id": source_id}
    if not trains:
        return [], {"code": "ticket_empty", "source_id": source_id}
    facts: list[dict] = []
    for index, row in enumerate(trains):
        if not isinstance(row, dict) or any(key not in row for key in _REQUIRED):
            return [], {"code": "unrecognized_ticket_payload", "source_id": source_id}
        train_no = str(row["train_no"]).strip()
        if not _TRAIN.match(train_no):
            return [], {"code": "unrecognized_ticket_payload", "source_id": source_id}
        try:
            price = float(row["price"])
        except (TypeError, ValueError):
            return [], {"code": "unrecognized_ticket_payload", "source_id": source_id}
        date = str(row["date"])
        row_origin = str(row["origin"])
        row_dest = str(row["destination"])
        if expect_date and date != expect_date:
            continue
        if origin and origin not in row_origin:
            continue
        if destination and destination not in row_dest:
            continue
        facts.append(
            {
                "id": f"{source_id}-{index}",
                "source_id": source_id,
                "kind": "train",
                "subject": train_no,
                "value": {
                    "train_no": train_no,
                    "date": date,
                    "origin": row_origin,
                    "destination": row_dest,
                    "seat": str(row["seat"]),
                    "price": price,
                    "availability": str(row["availability"]),
                    **_clock_fields(row),
                },
                "queried_at": queried_at,
                "applicable_date": date,
                "status": "verified",
            }
        )
    if not facts:
        return [], {"code": "ticket_mismatch", "source_id": source_id}
    return facts, None


class RecordingTool:
    """在 MCP 工具边界留下原始返回。模型稍后看到的仍是工具结果，不是这里的事实表。"""

    def __init__(self, inner: Any, records: list[dict], budget: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.description = getattr(inner, "description", "") or inner.name
        self.args_schema = getattr(inner, "args_schema", None)
        self.records = records
        self.budget = budget

    def _query_timeout(self) -> float:
        """单次工具查询的等待上限。

        任务层的 `asyncio.wait_for` 只能结束外层协程，MCP 客户端内部的 HTTP
        请求要靠这里断开；预算不够时就按剩余可用时间等，绝不留无上限等待。
        """
        cap = _TOOL_QUERY_SECONDS
        left = float(self.budget.work_left()) if hasattr(self.budget, "work_left") else cap
        if left <= 0:
            return 0.0
        return max(0.5, min(cap, left))

    async def ainvoke(self, args: dict) -> Any:
        if not self.budget.note_attempt():
            note_stat("ticket_query", "blocked_by_budget")
            return "工作时间或工具次数已用尽，未发起查询"
        timeout = self._query_timeout()
        if timeout <= 0:
            note_stat("ticket_query", "blocked_by_budget")
            return "工作时间已用尽，未发起查询"
        bump_stat("ticket_query")
        try:
            raw = await asyncio.wait_for(self.inner.ainvoke(args), timeout=timeout)
        except asyncio.TimeoutError:
            note_stat("ticket_query", "timed_out")
            raise
        except asyncio.CancelledError:
            note_stat("ticket_query", "cancelled")
            raise
        except Exception:
            note_stat("ticket_query", "failed")
            raise
        note_stat("ticket_query", "succeeded")
        self.records.append(
            {
                "id": f"raw-{len(self.records) + 1}",
                "tool": self.name,
                "arguments": _jsonable(args),
                "payload": _jsonable(raw),
                "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        return raw


def wrap_ticket_tools(tools: list[Any], records: list[dict], budget: Any) -> list[RecordingTool]:
    return [RecordingTool(tool, records, budget) for tool in select_ticket_tools(tools)]


_ARG_DATE_KEYS = ("date", "departDate", "start_date", "trainDate")
_ARG_FROM_KEYS = ("fromStation", "from_station", "from", "fromStationName", "fromCity")
_ARG_TO_KEYS = ("toStation", "to_station", "to", "toStationName", "toCity")

OUTBOUND = "outbound"
INBOUND = "inbound"


def _first_arg(args: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _city_hit(city: str | None, station: str | None) -> bool:
    """城市名与站名互相包含即视为同城；「新乡」命中「新乡东」「新乡南」。"""
    if not city or not station:
        return False
    return city in station or station in city


def _station_match(row_station: str, requested: str | None, city: str | None) -> str:
    if requested and row_station == requested:
        return "exact"
    if _city_hit(city, row_station):
        return "same_city"
    return "other"


def _annotate(fact: dict, *, expect_date: str | None, return_date: str | None,
              origin: str | None, destination: str | None,
              from_station: str | None, to_station: str | None) -> dict:
    """标方向、站名匹配度和是否适用于本方案。不适用的事实仍留在集合里，理由可查。"""
    value = fact.get("value") or {}
    row_date = str(value.get("date") or "")
    row_origin = str(value.get("origin") or "")
    row_dest = str(value.get("destination") or "")
    forward = _city_hit(origin, row_origin) and _city_hit(destination, row_dest)
    backward = _city_hit(origin, row_dest) and _city_hit(destination, row_origin)
    direction: str | None = None
    reason: str | None = None
    if forward and not backward:
        direction = OUTBOUND
    elif backward and not forward:
        direction = INBOUND
    elif not forward and not backward:
        reason = "station_other_city"
    else:
        reason = "direction_ambiguous"
    if direction is OUTBOUND:
        station_match = _station_match(row_origin, from_station, origin)
        wanted = expect_date
    else:
        # 返程记录（如「上海虹桥 -> 新乡东」）的到达站对应出发城市 origin，
        # 不是 destination；否则 city 传错会把正确的返程站判成 station_other_city。
        station_match = _station_match(row_dest, to_station, origin)
        wanted = return_date
    if direction is not None:
        if station_match == "other":
            reason = "station_other_city"
            direction = None
        elif wanted and not row_date:
            # 期望日期明确但事实缺日期：无法确认适用，按不适用处理，不能静默放行。
            reason = "date_unknown"
            role_direction = direction
            direction = None
            applied = False
            return {
                **fact,
                "value": {**value, "direction": None, "intended_direction": role_direction,
                          "applied": applied, "reason": reason, "station_match": station_match},
            }
        elif wanted and row_date != wanted:
            reason = "date_mismatch"
            role_direction = direction
            direction = None
            applied = False
            return {
                **fact,
                "value": {**value, "direction": None, "intended_direction": role_direction,
                          "applied": applied, "reason": reason, "station_match": station_match},
            }
    applied = direction is not None
    return {
        **fact,
        "value": {**value, "direction": direction, "applied": applied, "reason": reason,
                  "station_match": station_match},
    }


def revalidate_facts(
    facts: list[dict],
    *,
    expect_date: str | None,
    origin: str | None,
    destination: str | None,
    return_date: str | None = None,
) -> list[dict]:
    """按当前行程重算每行事实的方向、适用性、理由与站名匹配，不丢任何行。

    复用父方案时，事实里的 applied 是上一版行程（日期、天数、区间）算出来的。
    行程改过之后直接沿用，会把「按旧日期成立」的票当成当前适用，于是出现
    「行程已改 4 天、依据还是 11-09 那张票」的矛盾。这里用与 facts_from_records
    完全相同的 _annotate 口径重算，只更新判定字段，票号、票价、余票与来源原样保留。

    重算后可能一行都不适用，那不是丢数据：不适用本身就是要如实报出的结论。
    """
    reviewed: list[dict] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        value = fact.get("value") or {}
        # 上一轮不适用时留下的 intended_direction 是「原本想走哪个方向」的残影，
        # 重算后可能已经适用，残影必须清掉，否则下游会读到两个互相矛盾的方向。
        base = {key: item for key, item in value.items() if key != "intended_direction"}
        previous_match = str(value.get("station_match") or "")
        reviewed_fact = _annotate(
            {**fact, "value": base},
            expect_date=expect_date,
            return_date=return_date,
            origin=origin,
            destination=destination,
            # 原始查询的出发/到达站参数没有随事实保存；传 None 时 _annotate
            # 退回按城市比较，不会把同城车站误判成别的城市。
            from_station=None,
            to_station=None,
        )
        current_match = str((reviewed_fact.get("value") or {}).get("station_match") or "")
        if previous_match == "exact" and current_match == "same_city":
            # 行内站名没变，上一版是拿着查询参数比对出的 exact，这一版拿不到参数，
            # 只能退到同城。用降级结果覆盖已验证的精确匹配等于凭空丢信息，保留 exact。
            reviewed_fact["value"] = {**(reviewed_fact.get("value") or {}), "station_match": "exact"}
        reviewed.append(reviewed_fact)
    return reviewed


def applicable_facts(facts: list[dict]) -> list[dict]:
    """只有适用于本方案的事实才参与状态判定、预算和车次白名单。旧快照没有该字段视为适用。"""
    return [fact for fact in facts if (fact.get("value") or {}).get("applied", True)]


def applicability_note(facts: list[dict]) -> dict | None:
    """解析成功却全都不适用时给出可解释的一条说明，而不是笼统的「票务响应无法识别」。"""
    if not facts or applicable_facts(facts):
        return None
    reasons = sorted({str((fact.get("value") or {}).get("reason") or "unknown") for fact in facts})
    source_ids = sorted({str(fact.get("source_id")) for fact in facts})
    return {
        "code": "ticket_not_applicable",
        "tool": None,
        "ref": "、".join(source_ids),
        "reasons": reasons,
        "message": "本次查到了车次，但区间或日期不适用于本方案，没有可引用的车次事实。",
    }


RAIL_BOARD_MIN = 30      # 进站缓冲（规划假设，不是已核实事实）
RAIL_TRANSFER_MIN = 40   # 车站到住宿/首个景点的接驳（规划假设）

# 出现这些词说明用户不是在「选这一趟」，不能判成 user_selected。
_SELECTION_BLOCKERS = ("不要", "别", "除了", "不用", "比较", "对比", "还是", "或者", "看看")
_RETURN_HINT = (("晚上", 19 * 60), ("傍晚", 18 * 60), ("下午", 15 * 60), ("中午", 12 * 60))
_SEAT_UNBOOKABLE = "无票"
# 表达返程意图的语境词。只在这些子句里读返程时间倾向，避免把整句的去程时间
# （如「下午从新乡出发」）误当成返程偏好。
_RETURN_CTX = ("返程", "回程", "返回", "回来", "回去", "当天回", "当天返回", "往返", "来回")


_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?" + chr(10) + r"]")


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT.split(text or "") if part.strip()]


def _has_return_ctx(clause: str) -> bool:
    """只在明确表达返程意图的子句里读时间倾向；单个「回」字不算返程语境。"""
    return any(word in (clause or "") for word in _RETURN_CTX)


def user_selected_train(query: str, train_no: str) -> bool:
    """只有「选择语境」里的车次号才算用户点名。否定、比较语境都不算。"""
    if not train_no:
        return False
    for clause in _clauses(query):
        if train_no not in clause:
            continue
        return not any(word in clause for word in _SELECTION_BLOCKERS)
    return False


def _train_groups(facts: list[dict]) -> list[dict]:
    """同一 (车次, 出发日期, 出发站, 到达站) 的多种座席归并为一条车次。"""
    groups: dict[tuple, dict] = {}
    for fact in facts:
        value = fact.get("value") or {}
        key = (
            str(value.get("train_no") or ""),
            str(value.get("depart_date") or value.get("date") or ""),
            str(value.get("origin") or ""),
            str(value.get("destination") or ""),
        )
        entry = groups.get(key)
        if entry is None:
            entry = {
                "train_no": key[0],
                "depart_date": key[1] or None,
                "arrive_date": value.get("arrive_date"),
                "arrive_date_source": value.get("arrive_date_source"),
                "depart": value.get("depart"),
                "arrive": value.get("arrive"),
                "from_station": key[2] or None,
                "to_station": key[3] or None,
                "lishi": value.get("lishi"),
                "station_match": value.get("station_match"),
                "direction": value.get("direction"),
                "source_id": fact.get("source_id"),
                "seats": [],
            }
            groups[key] = entry
        availability = str(value.get("availability") or "")
        bookable = availability == "有票" or bool(re.search(r"剩余\s*[1-9]\d*\s*张", availability))
        entry["seats"].append(
            {
                "seat": str(value.get("seat") or ""),
                "price": value.get("price"),
                "availability": availability,
                "bookable": bookable,
            }
        )
    return [entry for entry in groups.values() if entry["train_no"]]


def _basis(entry: dict, *, origin: str, reason: str) -> dict:
    return {
        "train_no": entry["train_no"],
        "depart_date": entry.get("depart_date"),
        "arrive_date": entry.get("arrive_date"),
        "arrive_date_source": entry.get("arrive_date_source"),
        "depart": entry.get("depart"),
        "arrive": entry.get("arrive"),
        "from_station": entry.get("from_station"),
        "to_station": entry.get("to_station"),
        "lishi": entry.get("lishi"),
        "station_match": entry.get("station_match"),
        "seats": list(entry.get("seats") or []),
        "source_id": entry.get("source_id"),
        "origin": origin,
        "reason": reason,
    }


def _sort_key(entry: dict) -> tuple:
    """最早抵达优先，价格次级；没有抵达时刻的车次不参加排序（返回 None 由调用方跳过）。"""
    if not entry.get("arrive") or not entry.get("arrive_date"):
        return None
    prices = [seat["price"] for seat in entry.get("seats") or [] if isinstance(seat.get("price"), (int, float))]
    return (str(entry["arrive_date"]), str(entry["arrive"]), min(prices) if prices else float("inf"))


def choose_plan_basis(
    facts: list[dict],
    *,
    query: str = "",
    direction: str = OUTBOUND,
) -> dict | None:
    """从适用事实里挑一趟车作为排程依据。返回 None 表示这次没有可用的规划依据。"""
    groups = [entry for entry in _train_groups(facts) if entry.get("direction") in (None, direction)]
    if not groups:
        return None
    named = [
        entry for entry in groups
        if user_selected_train(query, entry["train_no"]) and any(seat["bookable"] for seat in entry["seats"])
    ]
    if named:
        return _basis(named[0], origin="user_selected", reason=f"用户原话指定 {named[0]['train_no']}")

    bookable = [entry for entry in groups if any(seat["bookable"] for seat in entry["seats"])]
    if direction == OUTBOUND:
        ranked = []
        for entry in bookable:
            key = _sort_key(entry)
            if key is not None:
                ranked.append((key, entry))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        winner = ranked[0][1]
        return _basis(winner, origin="proposed", reason="系统建议：当日最早抵达的可售车次")

    # 返程时间倾向只从明确的返程语境子句里读，不用整句兜底：
    # 「下午从新乡出发」里的「下午」属于去程，不能拿去选下午的返程车次。
    return_clauses = [c for c in _clauses(query) if _has_return_ctx(c)]
    hinted = [
        (target, entry)
        for entry in bookable
        for clause in return_clauses
        for word, target in _RETURN_HINT
        if word in clause
    ]
    if not hinted:
        return None
    # 缺出发时刻的候选不参与排序，避免「无时刻的车」被当成分数最小的项误选。
    with_clock = [(target, entry) for target, entry in hinted if _clock(entry.get("depart")) is not None]
    if not with_clock:
        return None
    with_clock.sort(key=lambda item: (abs(_clock(item[1].get("depart")) - item[0]), item[0]))
    return _basis(with_clock[0][1], origin="proposed", reason="系统建议：按原话的返程时间倾向取最接近的可售车次")


def _clock(text: str | None) -> int | None:
    return _minutes(text)


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _one_way(basis, *, transfer: int, direction: str, first_day: date, required) -> dict:
    """把一条 plan_basis 换算成一个「当天最早可开始 / 最晚必须结束」的边界。

    required 三态：
    - False：该方向不依赖铁路（自驾 / 飞机 / 同城 / 未要求返程），返回 not_constrained。
    - True：依赖铁路但缺依据时返回 unknown（不能回退成默认 09:00–18:00，
      那会把「需要铁路但没核实」误报成「无需铁路」）。
    - None：未显式指定，保持旧行为（缺依据不约束、不标 unknown）。
    """
    if required is False:
        return {
            "state": "not_constrained",
            "basis": None,
            "reason": "无需铁路" if direction == OUTBOUND else "无需返程铁路，末日晚间不受限制",
        }
    if not isinstance(basis, dict):
        if required is True:
            return {
                "state": "unknown",
                "basis": None,
                "reason": (
                    "需要铁路但没有可用车次依据，首日最早开始时间未知"
                    if direction == OUTBOUND
                    else "需要返程但没有返程车次依据，末日最晚结束时间未知"
                ),
            }
        return {
            "state": "not_constrained",
            "basis": None,
            "reason": "没有可用的车次依据" if direction == OUTBOUND else "没有返程车次依据，末日晚间不受限制",
        }
    if direction == OUTBOUND:
        arrive = basis.get("arrive")
        arrive_date = _as_date(basis.get("arrive_date"))
        minute = _minutes(arrive)
        if minute is None or arrive_date is None:
            return {
                "state": "unknown",
                "basis": basis,
                "arrive_date": basis.get("arrive_date"),
                "arrive": arrive,
                "earliest_start": None,
                "reason": "该车次没有可用的抵达时刻，首日最早开始时间未知",
            }
        return {
            "state": "known",
            "basis": basis,
            "arrive_date": arrive_date.isoformat(),
            "arrive": arrive,
            "earliest_start": minute + transfer,
            "reason": f"按 {arrive_date.isoformat()} {arrive} 抵达加 {transfer} 分钟接驳估算最早 {_hhmm_text(minute + transfer)} 开始",
        }
    depart = basis.get("depart")
    depart_date = _as_date(basis.get("depart_date"))
    minute = _minutes(depart)
    if minute is None or depart_date is None:
        return {
            "state": "unknown",
            "basis": basis,
            "depart_date": basis.get("depart_date"),
            "depart": depart,
            "latest_end": None,
            "reason": "该返程车次没有可用的出发时刻，末日最晚结束时间未知",
        }
    return {
        "state": "known",
        "basis": basis,
        "depart_date": depart_date.isoformat(),
        "depart": depart,
        "latest_end": minute - RAIL_BOARD_MIN - transfer,
        "reason": f"按 {depart_date.isoformat()} {depart} 发车，预留 {RAIL_BOARD_MIN} 分钟进站与 {transfer} 分钟接驳",
    }


def _hhmm_text(minute: int | None) -> str:
    if minute is None:
        return "未知"
    return f"{int(minute) // 60:02d}:{int(minute) % 60:02d}"


def rail_window(
    plan_basis: dict,
    *,
    first_day: date,
    last_day: date,
    transfer_minutes: int | None = None,
    required: dict[str, bool] | None = None,
) -> dict:
    """把规划依据换算成首末日的可用窗口。只描述「依据」，不代表已经买到票。

    required 传 {"outbound": bool, "inbound": bool} 时，缺依据的那一侧会标 unknown
    而不是 not_constrained，避免把「需要铁路但没核实」当成「无需铁路」。
    """
    transfer = RAIL_TRANSFER_MIN if transfer_minutes is None else max(0, int(transfer_minutes))
    basis = plan_basis if isinstance(plan_basis, dict) else {}
    if isinstance(required, dict):
        outbound_required = required.get("outbound", True)
        inbound_required = required.get("inbound", True)
    else:
        # 未显式指定是否依赖铁路：保持旧行为。
        outbound_required = None
        inbound_required = None
    outbound = _one_way(
        basis.get(OUTBOUND),
        transfer=transfer,
        direction=OUTBOUND,
        first_day=first_day,
        required=outbound_required,
    )
    inbound = _one_way(
        basis.get(INBOUND),
        transfer=transfer,
        direction=INBOUND,
        first_day=first_day,
        required=inbound_required,
    )
    assumptions = [
        f"车站与住宿/首个景点之间按 {transfer} 分钟接驳估算，不是已核实耗时",
        f"进站候车按 {RAIL_BOARD_MIN} 分钟估算，不是已核实耗时",
    ]
    unverified = outbound["state"] == "unknown" or inbound["state"] == "unknown"
    return {
        "outbound": outbound,
        "inbound": inbound,
        "assumptions": assumptions,
        "unverified": unverified,
        "first_day": first_day.isoformat(),
        "last_day": last_day.isoformat(),
    }


def facts_from_records(
    records: list[dict],
    *,
    expect_date: str | None,
    origin: str | None,
    destination: str | None,
    return_date: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """解析全部车次类记录，并逐条标注是否适用于本方案。

    解析阶段不再按日期/区间丢行：丢掉的正是「为什么不适用」的证据。适用性由
    applied 字段与 reason 表达，消费方用 applicable_facts 取子集。
    """
    facts: list[dict] = []
    diagnostics: list[dict] = []
    for record in records:
        source_id = str(record.get("id"))
        tool = str(record.get("tool") or "")
        if tool and tool not in TRAIN_RESULT_TOOLS:
            continue
        args = record.get("arguments") or {}
        arg_date = _first_arg(args, _ARG_DATE_KEYS)
        parsed, diag = parse_ticket_payload(
            record.get("payload"),
            source_id=source_id,
            queried_at=str(record.get("queried_at") or ""),
            expect_date=None,
            origin=None,
            destination=None,
            request_date=arg_date,
        )
        from_station = _first_arg(args, _ARG_FROM_KEYS)
        to_station = _first_arg(args, _ARG_TO_KEYS)
        for fact in parsed:
            facts.append(
                _annotate(
                    fact,
                    expect_date=expect_date,
                    return_date=return_date,
                    origin=origin,
                    destination=destination,
                    from_station=from_station,
                    to_station=to_station,
                )
            )
        if diag:
            diagnostic = {**diag, "tool": tool or None, "ref": source_id}
            if diag["code"] == "ticket_empty":
                conditions = " ".join(str(value) for value in (arg_date, from_station, to_station) if value)
                train_filter = str(args.get("trainFilterFlags") or "")
                label = "高铁查询" if train_filter == "G" else "车次查询"
                diagnostic["message"] = f"本次{label}未找到车次（{conditions or '当前查询条件'}）"
            diagnostics.append(diagnostic)
    return facts, diagnostics


def unparsed_tool_notes(records: list[dict]) -> list[dict]:
    """未启用解析的工具只产说明，不算告警，也不参与票务状态判定。"""
    notes: list[dict] = []
    for record in records:
        tool = str(record.get("tool") or "")
        if tool not in TICKET_TOOLS_UNPARSED:
            continue
        notes.append(
            {
                "code": "ticket_parser_not_enabled",
                "tool": tool,
                "ref": str(record.get("id")),
                "message": f"{tool} 的结果已保存，但解析器尚未验证，不作为车次事实。",
            }
        )
    return notes


def allowed_train_numbers(facts: list[dict]) -> set[str]:
    found: set[str] = set()
    for fact in facts:
        if fact.get("status") != "verified":
            continue
        value = fact.get("value") or {}
        number = str(value.get("train_no") or "")
        if number:
            found.add(number)
    return found


# 父方案没查到票时的失败细分：正文要照实说失败在哪一步，不能写成「沿用上次结果」。
_PARENT_FAIL_TEXT = {
    "unavailable": "服务不可用或依赖缺失",
    "missed": "未调度或空结果",
    "timeout": "超时",
}


def _ticket_head(
    *,
    reused: bool,
    queried_at: str | None,
    reason: str,
    reuse_source: str,
    parent_reason: str,
    has_facts: bool,
) -> str:
    """票务正文首句。优先级：本次跳过 > 父级失败 > 更早查询 > 父级 ok > 旧复用 > 兜底。

    父快照本身没查到票（unavailable/missed/timeout）时，复用只是「这次也没重查」，
    不能写「上次查询结果，未刷新」——那会把一次失败说成一份有余票含义的依据。
    """
    stamp = queried_at or "未知"
    if reason == "skipped" or reuse_source == "parent_skipped":
        return "本次未查票（无需铁路）。"
    if reuse_source == "parent_failed":
        cause = _PARENT_FAIL_TEXT.get(str(parent_reason or ""), "原因未记录")
        return f"上次查询失败（{cause}），本次未重新查询，余票未知。余票不是当前余票。"
    if reuse_source == "parent_reused":
        return f"沿用更早一次查询（查询时间 {stamp}），余票未刷新。余票不是当前余票。"
    if reuse_source == "parent_ok":
        return f"上次查询结果，未刷新（查询时间 {stamp}）。余票不是当前余票。"
    if reuse_source:
        # parent_unknown 及任何未知来源：父方案没说清查没查到，不能声称有上次结果。
        return "本次未重新查询，余票状态未知。余票不是当前余票。"
    if reused:
        # 旧调用方不传 reuse_source：保持原有文案，向后兼容。
        return f"上次查询结果，未刷新（查询时间 {stamp}）。余票不是当前余票。"
    if not has_facts:
        return "没有可解析的车次事实。未核实，不列出车次或票价。"
    return "以下车次和票价只来自本次工具原始响应。"


def ticket_markdown(
    facts: list[dict],
    *,
    reused: bool,
    queried_at: str | None,
    reason: str = "",
    reuse_source: str = "",
    parent_reason: str = "",
) -> str:
    head = _ticket_head(
        reused=reused,
        queried_at=queried_at,
        reason=reason,
        reuse_source=reuse_source,
        parent_reason=parent_reason,
        has_facts=bool(facts),
    )
    lines = [head]
    for fact in facts:
        value = fact.get("value") or {}
        window = _row_window(value)
        lines.append(
            "- {train_no} {origin}→{destination} {date}{window} {seat} {price} 元，余票字段：{availability}（来源 {source_id}）".format(
                source_id=fact.get("source_id"),
                window=window,
                **value,
            )
        )
    return "\n".join(lines) + "\n"


def _row_window(value: dict) -> str:
    """有工具给的抵离时刻就写出来；没有就什么都不写，绝不补一个推测的时刻。"""
    depart = str(value.get("depart") or "").strip()
    arrive = str(value.get("arrive") or "").strip()
    if not depart or not arrive:
        return ""
    return f" {depart}–{arrive}"


def budget_markdown(
    facts: list[dict],
    *,
    user_budget: str | None,
    days: int | None,
    need_ticket: bool,
    party_known: bool,
    party: int | None = None,
    items: list[dict] | None = None,
    budget_scope: str | None = None,
) -> tuple[str, str]:
    """返回正文和检查状态。完整性与是否超预算分开。

    budget_scope: "total" 表示用户给的预算就是总额；"per_person" 表示人均，
    要乘人数再比。没给口径时按总额理解，并在正文里写明这是假设。
    """
    count = party if party_known and party else None
    scope = str(budget_scope or "").strip() or None
    if count and scope == "per_person":
        head = f"口径：{count} 人，预算为人均，按人均 × {count} 人比较总额。"
    elif count:
        head = f"口径：{count} 人，预算为{_scope_text(scope)}，比较总额。"
    elif scope:
        head = f"口径：人数未在槽位中单独给出，预算为{_scope_text(scope)}。"
    else:
        head = "口径：人数未在槽位中单独给出。"
    lines = [
        head,
        "比较方式：只把状态为已核实的项目加进总额。估算和未知不算已核实费用。",
        "城际交通" + ("纳入本次口径。" if need_ticket else "本次不需要。"),
        "住宿" + ("多日行程需要单列，没有核实单价时只是估算。" if (days or 1) > 1 else "一日行程不纳入主要费用。"),
    ]
    built = list(items) if items is not None else _default_items(facts, need_ticket=need_ticket, party=count)
    verified_total = 0.0
    complete = bool(count) and bool(built)
    for item in built:
        state = item.get("state")
        amount = item.get("amount")
        if state == "verified" and isinstance(amount, (int, float)):
            verified_total += float(amount)
            lines.append(f"{item.get('name')}：已核实 {float(amount):g} 元。")
        elif state == "estimated":
            complete = False
            lines.append(f"{item.get('name')}：估算，不计入已核实总额。")
        else:
            complete = False
            lines.append(f"{item.get('name')}：未知。")
    amount = _money(user_budget)
    if amount is not None and scope == "per_person" and count:
        amount = amount * count
        lines.append(f"预算按人均换算：{amount / count:g} 元/人 × {count} 人 = {amount:g} 元。")
    status = "unverified"
    if not count:
        lines.append("人数口径不清楚，预算未核实，不能宣称预算达标。")
    elif amount is None:
        lines.append("用户预算不是可比较的金额，不能判断是否超预算。")
    elif verified_total > amount:
        status = "conflict"
        lines.append(f"已核实费用 {verified_total:g} 元已超过预算 {amount:g} 元。")
    elif complete:
        status = "verified"
        lines.append(f"已核实费用合计 {verified_total:g} 元，未超过预算 {amount:g} 元。")
    else:
        lines.append("仍有主要费用未核实，不能宣称预算达标。")
    return "\n".join(lines) + "\n", status


def _scope_text(scope: str | None) -> str:
    return "人均口径" if scope == "per_person" else "总额口径"


def _default_items(facts: list[dict], *, need_ticket: bool, party: int | None) -> list[dict]:
    prices = [float(fact["value"]["price"]) for fact in facts if fact.get("kind") == "train"]
    items: list[dict] = []
    if need_ticket:
        if prices and party:
            items.append({"name": "城际交通", "state": "verified", "amount": min(prices) * party})
        else:
            items.append({"name": "城际交通", "state": "unknown", "amount": None})
    items.append({"name": "市内交通", "state": "unknown", "amount": None})
    items.append({"name": "门票", "state": "unknown", "amount": None})
    items.append({"name": "餐饮", "state": "estimated", "amount": None})
    return items


def _money(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(text))
    if not match:
        return None
    return float(match.group(0))


_PROSE_TRAIN = re.compile(r"(?<![A-Za-z0-9])([GDCZTK]\d{1,4})(?!\d)")


def strip_unlisted_trains(text: str, allowed: set[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        number = match.group(1)
        return number if number in allowed else "未核实车次"

    return _PROSE_TRAIN.sub(repl, text or "")


def model_tool(recording: RecordingTool) -> Any:
    """交给模型的工具保留原参数结构。闭包不出现在参数里。"""
    from langchain_core.tools import StructuredTool

    async def _call(**kwargs: Any) -> str:
        return str(await recording.ainvoke(kwargs))

    fields: dict[str, Any] = {
        "name": recording.name,
        "description": recording.description or recording.name,
        "coroutine": _call,
    }
    if recording.args_schema is not None:
        fields["args_schema"] = recording.args_schema
    else:
        fields["args_schema"] = {"type": "object", "properties": {}}
    return StructuredTool(**fields)
