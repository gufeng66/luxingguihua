"""编排层 Tavily 检索与证据门槛。密钥和原始摘要不进模型。"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from pathlib import Path

from planner.run_stats import note as note_stat
from planner.urls import safe_evidence_url

# 公开场馆站。摘要仍须逐条通过子串、场馆身份和语义门槛。
def _load_hosts() -> frozenset[str]:
    path = Path(__file__).with_name("sources.json")
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        hosts = data.get("hosts") or []
    except (OSError, ValueError):
        hosts = []
    return frozenset(str(host).lower() for host in hosts)


ALLOWED_HOSTS = _load_hosts()

_SENTENCE = re.compile(r"[^。！？\n]+[。！？]?")
_RELATIVE = ("本周", "下周", "明天", "明日", "今天", "后天")
_EXCEPTION = ("除外", "除了", "节假日", "假期除外")
_WEEKDAY = "一二三四五六日"
_CLOSED = re.compile(r"(?:每)?周([一二三四五六日])(?:闭馆|关闭|不开放|休息)")
_NOT_CLOSED = re.compile(r"不(?:闭馆|关闭|休息)")
_ELLIPSIS = ("...", "…", "……")
_DATE_CLOSED = re.compile(r"(20\d{2}-\d{2}-\d{2}).{0,12}?(?:闭馆|关闭|不开放)")
_HOURS = re.compile(r"(\d{1,2}:\d{2})\s*[-—–~至到]\s*(\d{1,2}:\d{2})")
_VENUE_HOURS = re.compile(r"开放时间|开馆时间|参观时间|营业时间")
_OTHER_HOURS = re.compile(r"电话|咨询|服务时间|票务")
_CLAUSE = re.compile(r"[^，,；;]+")
_STACKABLE = {"closed_weekday", "closed_date"}
_LAST = re.compile(r"(?:最晚(?:入场|入馆)\s*(\d{1,2}:\d{2})|(\d{1,2}:\d{2})\s*(?:停止入场|停止入馆))")
_ISO_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")
_ISO_RANGE = re.compile(r"(20\d{2}-\d{2}-\d{2})\s*[-—~至到]\s*(20\d{2}-\d{2}-\d{2})")
_UNPARSED_DATE = re.compile(r"20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*[日号]")


class SearchBudget:
    """单次规划的尝试次数和截止时间。取消等待不会把已占次数退回。"""

    def __init__(self, *, clock, limit: int = 6, window: float = 30.0, max_timeout: float = 8.0):
        self.clock = clock
        self.limit = limit
        self.window = window
        self.max_timeout = max_timeout
        self.started = clock()
        self.used = 0
        self.closed = False
        self._lock = asyncio.Lock()

    def remaining(self) -> float:
        return self.window - (self.clock() - self.started)

    async def reserve(self) -> float | None:
        async with self._lock:
            if self.closed or self.used >= self.limit or self.remaining() <= 0:
                return None
            timeout = min(self.max_timeout, self.remaining())
            if timeout <= 0:
                return None
            self.used += 1
            return timeout


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _minutes(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def _sentence_with(summary: str, match: re.Match) -> str | None:
    for sentence in _SENTENCE.findall(summary or ""):
        if match.group(0) in sentence:
            sentence = sentence.strip()
            return sentence or None
    return None


def _blocked(sentence: str) -> bool:
    return any(token in sentence for token in _RELATIVE) or any(token in sentence for token in _EXCEPTION)


def _truncated(sentence: str) -> bool:
    if any(mark in sentence for mark in _ELLIPSIS):
        return True
    tail = sentence.strip()
    return bool(tail) and tail[-1] in "，,、：:；;"


def _date_scope(sentence: str) -> tuple[list[str] | None, bool]:
    """返回 (适用日期, 是否无法判断)。没有日期则适用日期为 None。"""
    if _UNPARSED_DATE.search(sentence):
        return None, True
    dates: list[str] = []
    leftover = sentence
    for match in _ISO_RANGE.finditer(sentence):
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        # ponytail: 超过 31 天的区间视为无法判断，避免把整季规则摊成无界硬约束
        if end < start or (end - start).days > 31:
            return None, True
        cur = start
        while cur <= end:
            dates.append(cur.isoformat())
            cur += timedelta(days=1)
        leftover = leftover.replace(match.group(0), " ")
    for raw in _ISO_DATE.findall(leftover):
        date.fromisoformat(raw)
        dates.append(raw)
    if not dates:
        return None, False
    return list(dict.fromkeys(dates)), False


def _scopes_overlap(left: dict, right: dict) -> bool:
    a, b = left.get("applies_on"), right.get("applies_on")
    if not a or not b:
        return True
    return bool(set(a) & set(b))


def _weeks_overlap(left: dict, right: dict) -> bool:
    a, b = left.get("weekdays"), right.get("weekdays")
    if not a or not b:
        return True
    return bool(set(a) & set(b))


def _weekday_scope(clause: str) -> tuple[list[int] | None, bool]:
    groups: list[list[int]] = []
    if "工作日" in clause:
        groups.append([0, 1, 2, 3, 4])
    if "周末" in clause:
        groups.append([5, 6])
    singles = [_WEEKDAY.index(ch) for ch in re.findall(r"(?:每)?周([一二三四五六日])", clause)]
    if singles:
        groups.append(list(dict.fromkeys(singles)))
    if not groups:
        return None, False
    if len(groups) > 1:
        return None, True
    return groups[0], False


def parse_evidence(summary: str, *, url: str, poi_name: str, poi_id: str, allowed_hosts: frozenset[str] | None = None) -> list[dict]:
    """子串、主机名、场馆名和完整语义都过关才是 hard，否则 pending。"""
    hosts = ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    summary = summary or ""
    # 来源链接既会拼进给模型的提示词，也会带着 poi_id 显示到前端，所以必须是
    # 能通过白名单校验的公网 http(s) 地址。不合格的链接一律不落地：source_url
    # 置空、原文另存 source_url_raw 供排查，并把整条事实降级为 pending。
    # 这样即便上游检索被投毒，也不会把 javascript:/内网地址传出去。
    safe_url = safe_evidence_url(url, hosts)
    hard_host = safe_url is not None and _host(safe_url) in hosts
    found: list[dict] = []

    def add(kind: str, value, sentence: str, *, weekdays: list[int] | None = None, force_pending: bool = False) -> None:
        if not sentence or sentence not in summary or poi_name not in sentence:
            return
        status = "hard" if hard_host and not _blocked(sentence) and not _truncated(sentence) else "pending"
        scope, unclear = _date_scope(sentence)
        if unclear or force_pending:
            status = "pending"
            if unclear:
                scope = None
        if _NOT_CLOSED.search(sentence) and kind.startswith("closed"):
            status = "pending"
            if "不闭馆" in sentence or "不关闭" in sentence or "不休息" in sentence:
                return
        fact = {
            "poi_id": poi_id,
            "kind": kind,
            "value": value,
            "quote": sentence,
            "status": status,
            "source_url": safe_url or "",
        }
        if safe_url is None:
            # 原始链接留档只为排查，不参与展示、不进提示词
            fact["source_url_raw"] = str(url or "")[:2048]
            fact["status"] = "pending"
        if scope:
            fact["applies_on"] = scope
        if weekdays:
            fact["weekdays"] = weekdays
        found.append(fact)

    for match in _CLOSED.finditer(summary):
        sentence = _sentence_with(summary, match)
        if sentence and _NOT_CLOSED.search(sentence):
            continue
        add("closed_weekday", _WEEKDAY.index(match.group(1)), sentence or "")
    for match in _DATE_CLOSED.finditer(summary):
        add("closed_date", match.group(1), _sentence_with(summary, match) or "")
    hour_sentences: list[str] = []
    for match in _HOURS.finditer(summary):
        sentence = _sentence_with(summary, match) or ""
        if sentence and sentence not in hour_sentences:
            hour_sentences.append(sentence)
    for sentence in hour_sentences:
        if "不开放" in sentence or "闭馆" in sentence:
            continue
        venue = bool(_VENUE_HOURS.search(sentence))
        for clause in _CLAUSE.findall(sentence):
            spans = [[_minutes(start), _minutes(end)] for start, end in _HOURS.findall(clause)]
            if not spans:
                continue
            weekdays, unclear = _weekday_scope(clause)
            other = bool(_OTHER_HOURS.search(clause))
            add(
                "opening_hours",
                spans,
                sentence,
                weekdays=weekdays,
                force_pending=other or unclear or not venue,
            )
    for match in _LAST.finditer(summary):
        clock = match.group(1) or match.group(2)
        add("last_entry", _minutes(clock), _sentence_with(summary, match) or "")
    return _downgrade_conflicts(found)


def _downgrade_conflicts(facts: list[dict]) -> list[dict]:
    for index, fact in enumerate(facts):
        for other in facts[index + 1 :]:
            if (fact.get("poi_id"), fact.get("kind")) != (other.get("poi_id"), other.get("kind")):
                continue
            if fact.get("kind") in _STACKABLE:
                continue
            if fact.get("value") == other.get("value") or not _scopes_overlap(fact, other) or not _weeks_overlap(fact, other):
                continue
            fact["status"] = "pending"
            other["status"] = "pending"
    return facts


def _priority(poi: dict) -> tuple:
    limited = str(poi.get("place_type") or "")
    return (
        0 if poi.get("must_visit") else 1,
        0 if ("博物馆" in limited or limited == "museum") else 1,
        str(poi.get("name") or ""),
    )


async def gather_facts(
    candidates: list[dict],
    *,
    city: str,
    day: str,
    api_key: str,
    post=None,
    clock=None,
    client: httpx.AsyncClient | None = None,
    allowed_hosts: frozenset[str] | None = None,
    cancel_event: asyncio.Event | None = None,
    window: float = 30.0,
    on_attempt=None,
) -> dict:
    """固定模板查询。无 Key、401、额度不足直接降级，不重试。"""
    clock = clock or (lambda: asyncio.get_running_loop().time())
    budget = SearchBudget(clock=clock, window=window)
    if not (api_key or "").strip():
        note_stat("evidence_search", "dependency_missing")
        return {"facts": [], "facts_status": "unavailable", "attempts": 0, "error": "no_key", "usage": None}
    own_client = client is None and post is None
    if own_client:
        client = httpx.AsyncClient()
    sem = asyncio.Semaphore(2)
    seen: set[tuple] = set()
    saved: list[dict] = []
    usage = None
    error = None
    fatal = False

    async def once(query: str, timeout: float) -> dict:
        if on_attempt is not None and not on_attempt():
            budget.closed = True
            return {"results": []}
        if post is not None:
            return await post(query, timeout)
        assert client is not None
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "topic": "general",
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
                "include_raw_content": False,
                "auto_parameters": False,
            },
            timeout=timeout,
        )
        if response.status_code in (401, 403, 429, 432, 433):
            raise PermissionError(str(response.status_code))
        response.raise_for_status()
        return response.json()

    async def one(poi: dict) -> None:
        nonlocal usage, error, fatal
        if fatal or budget.closed:
            return
        if cancel_event is not None and cancel_event.is_set():
            budget.closed = True
            return
        key = (city, str(poi.get("poi_id") or poi.get("name")), day)
        if key in seen:
            return
        seen.add(key)
        query = f"{city} {poi.get('name')} {day} 开放时间 预约 闭馆"
        tried = 0
        while tried < 2 and not fatal and not budget.closed:
            if cancel_event is not None and cancel_event.is_set():
                budget.closed = True
                return
            async with sem:
                if budget.closed or (cancel_event is not None and cancel_event.is_set()):
                    budget.closed = True
                    return
                timeout = await budget.reserve()
                if timeout is None:
                    return
                tried += 1
                try:
                    payload = await once(query, timeout)
                except asyncio.CancelledError:
                    raise
                except PermissionError as exc:
                    fatal = True
                    error = str(exc)
                    return
                except Exception as exc:
                    error = type(exc).__name__
                    if tried >= 2:
                        return
                    continue
            if budget.closed:
                return
            if isinstance(payload, dict) and payload.get("usage") is not None:
                usage = payload.get("usage")
            for index, hit in enumerate((payload or {}).get("results") or []):
                summary = str(hit.get("content") or hit.get("snippet") or "")
                url = str(hit.get("url") or "")
                for fact in parse_evidence(
                    summary,
                    url=url,
                    poi_name=str(poi.get("name") or ""),
                    poi_id=str(poi.get("poi_id") or poi.get("name") or ""),
                    allowed_hosts=allowed_hosts,
                ):
                    fact["source_id"] = f"src-{len(saved) + 1}"
                    fact["source_title"] = str(hit.get("title") or "")
                    fact["source_summary"] = summary
                    fact["retrieved_at"] = datetime.now(timezone.utc).isoformat()
                    if fact["quote"] not in summary:
                        fact["status"] = "pending"
                    saved.append(fact)
                    if index >= 4:
                        break
            return

    tasks = []
    stage_timeout = max(0.0, budget.remaining())
    try:
        ordered = sorted(candidates, key=_priority)
        for poi in ordered:
            if fatal or budget.remaining() <= 0 or budget.used >= budget.limit:
                break
            if cancel_event is not None and cancel_event.is_set():
                budget.closed = True
                break
            tasks.append(asyncio.create_task(one(poi)))
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=stage_timeout)
            except TimeoutError:
                budget.closed = True
    finally:
        budget.closed = True
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if own_client and client is not None:
            await client.aclose()
    # 取证阶段是唯一知道「为什么没查成」的一层：401/403/额度不足是 fatal，
    # 单个 POI 的请求异常只是 error，超时由 stage_timeout 提前收口。
    if fatal:
        note_stat("evidence_search", "blocked_by_budget")
    elif stage_timeout <= 0:
        note_stat("evidence_search", "timed_out")
    elif error:
        note_stat("evidence_search", "failed")
    elif saved:
        note_stat("evidence_search", "succeeded")
    else:
        note_stat("evidence_search", "not_needed")
    saved = _downgrade_conflicts(saved)
    wanted = {str(poi.get("poi_id") or poi.get("name") or "") for poi in candidates}
    wanted.discard("")
    hard_ids = {str(fact.get("poi_id") or "") for fact in saved if fact.get("status") == "hard"}
    status = "unavailable" if fatal and not saved else "partial"
    if saved and wanted <= hard_ids and all(fact.get("status") == "hard" for fact in saved) and not error:
        status = "checked"
    if not api_key:
        status = "unavailable"
    return {"facts": saved, "facts_status": status, "attempts": budget.used, "error": error, "usage": usage}
