"""代码直接拉起地图和票务。一方失败不取消另一方。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from planner.async_utils import message_text
from planner.map_agent import MAP_AGENT_PROMPT
from planner.run_stats import note as note_stat
from planner.ticket_agent import TICKET_AGENT_PROMPT
from planner.ticket_facts import model_tool, wrap_ticket_tools

logger = logging.getLogger(__name__)


def cleanup_seconds() -> float:
    """清理窗口：取消子任务后最多等这么久，仍未结束就记为残留。"""
    return float(os.getenv("PLAN_CLEANUP_SECONDS") or 5)


def force_serial() -> bool:
    return (os.getenv("FORCE_SERIAL") or "").strip() == "1"


def _live(llm: Any) -> bool:
    return type(llm).__name__ not in {"MagicMock", "Mock", "AsyncMock"}


def map_tool_names() -> list[str]:
    return ["amap_poi_search"]


def make_poi_tool(budget: Any, cancel_event: asyncio.Event | None, stats: dict | None = None) -> Any:
    import json

    import httpx
    from langchain_core.tools import StructuredTool

    from planner.map_fallback import search_places

    async def amap_poi_search(city: str, keywords: str) -> str:
        city = (city or "").strip()
        keywords = (keywords or "").strip()
        if not city or not keywords or len(keywords) > 40:
            return "[]"
        async with httpx.AsyncClient() as client:
            rows = await search_places(
                client,
                city=city,
                keywords=keywords,
                types="",
                limit=5,
                budget=budget,
                cancel_event=cancel_event,
                stats=stats,
            )
        return json.dumps(rows, ensure_ascii=False)

    return StructuredTool.from_function(
        coroutine=amap_poi_search,
        name="amap_poi_search",
        description="按城市和关键词搜索高德 POI。只返回名称和坐标，不排程。",
    )


async def invoke_tool_with_timeout(
    tool: Any,
    args: Any,
    *,
    timeout: float,
    cancel_event: asyncio.Event | None,
    who: str = "",
) -> Any:
    """带工具级超时的 ainvoke 包装，供 bounded_loop 统一调用。

    工具级超时不能只靠底层 HTTP timeout：工具内部同步阻塞（如 CPU 密集解析、未切到
    事件循环的阻塞调用）或解析阶段卡死时，HTTP 层的超时不生效，仍可能把整个
    「模型→工具」循环拖住。因此这里用 asyncio.wait_for 包住完整的 ainvoke 协程，
    给单次工具调用一个独立、有限的墙钟边界。

    约定：
    - 调用前 cancel_event 已 set：抛 asyncio.CancelledError，让取消传到上层 drain；
    - asyncio.TimeoutError：不抛到循环外，返回含「超时/timeout」的明确 payload 并记 warning；
    - asyncio.CancelledError：原样 re-raise，绝不吞掉取消；
    - 其它 Exception：返回含「失败/failed/不可用」的明确 payload 并记 warning，
      不静默当成空结果、不编造数据；
    - 成功：原样返回 ainvoke 结果，不做截断（截断仍由 bounded_loop 在写入
      ToolMessage 前统一处理）。
    """
    log = logging.getLogger("travel_planner")
    tool_name = str(getattr(tool, "name", "") or "")
    if cancel_event is not None and cancel_event.is_set():
        log.warning("tool_cancelled_before_invoke who=%s tool=%s", who, tool_name)
        raise asyncio.CancelledError()
    if not isinstance(timeout, (int, float)) or not (0 < timeout < float("inf")):
        raise ValueError("invoke_tool_with_timeout 需要有限正数 timeout")
    try:
        return await asyncio.wait_for(tool.ainvoke(args), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("tool_timeout who=%s tool=%s timeout=%.2f", who, tool_name, float(timeout))
        return "工具调用超时（timeout），未获得工具结果"
    except asyncio.CancelledError:
        # 取消不是工具失败，必须原样传到上层，不能用 payload 吞掉。
        raise
    except Exception as exc:
        log.warning("tool_failed who=%s tool=%s error=%s", who, tool_name, type(exc).__name__)
        return f"工具调用失败（failed）：{type(exc).__name__}，工具结果不可用"


async def bounded_loop(
    llm: Any,
    tools: list[Any],
    system: str,
    user: str,
    *,
    who: str,
    budget: Any,
    cancel_event: asyncio.Event | None,
) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    from planner.usage import current_usage, note_llm

    bound = llm.bind_tools(tools)
    messages: list[Any] = [SystemMessage(content=system), HumanMessage(content=user)]
    allowed = {tool.name: tool for tool in tools}
    final = ""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            break
        if not budget.allow_model(who, current_usage().total_tokens()):
            break
        timeout = budget.timeout_for(min(30.0, budget.agents))
        if timeout <= 0:
            break
        budget.note_model(who)
        try:
            ai = await asyncio.wait_for(bound.ainvoke(messages, max_tokens=800), timeout=timeout)
        except TypeError:
            ai = await asyncio.wait_for(bound.ainvoke(messages), timeout=timeout)
        note_llm(ai)
        messages.append(ai)
        final = message_text(getattr(ai, "content", None)) or final
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            break
        for call in calls:
            name = str(call.get("name") or "")
            if name not in allowed:
                payload = "工具不在白名单"
            else:
                tool_timeout = min(30.0, max(1.0, budget.timeout_for(budget.agents) or 30.0))
                payload = await invoke_tool_with_timeout(
                    allowed[name],
                    call.get("args") or {},
                    timeout=tool_timeout,
                    cancel_event=cancel_event,
                    who=who,
                )
            messages.append(
                ToolMessage(content=str(payload)[:4000], tool_call_id=str(call.get("id") or "call"))
            )
    return final


async def run_map_agent(
    *,
    llm: Any,
    query: str,
    budget: Any,
    cancel_event: asyncio.Event | None,
    city: str,
    stats: dict | None = None,
) -> dict[str, Any]:
    tool = make_poi_tool(budget, cancel_event, stats)
    if not _live(llm):
        return {"text": "", "started": True, "timed_out": False, "tools": [tool.name]}
    text = await bounded_loop(
        llm,
        [tool],
        MAP_AGENT_PROMPT,
        f"{query}\n城市：{city}",
        who="map",
        budget=budget,
        cancel_event=cancel_event,
    )
    return {"text": text, "started": True, "timed_out": False, "tools": [tool.name]}


async def run_ticket_agent(
    *,
    llm: Any,
    tools: list[Any],
    query: str,
    budget: Any,
    cancel_event: asyncio.Event | None,
    records: list[dict],
) -> dict[str, Any]:
    wrapped = wrap_ticket_tools(tools, records, budget)
    names = [tool.name for tool in wrapped]
    if not wrapped or not _live(llm):
        return {"started": True, "summary": "", "timed_out": False, "tools": names}
    structured = [model_tool(tool) for tool in wrapped]
    text = await bounded_loop(
        llm,
        structured,
        TICKET_AGENT_PROMPT,
        query,
        who="ticket",
        budget=budget,
        cancel_event=cancel_event,
    )
    return {"started": True, "summary": text, "timed_out": False, "tools": names}


async def iter_agents(
    out: dict[str, Any],
    *,
    need_map: bool,
    need_ticket: bool,
    ticket_tools: list[Any],
    llm: Any,
    query: str,
    budget: Any,
    cancel_event: asyncio.Event | None,
    city: str,
    stats: dict | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """先发出两侧已开始，再等待任务。串行时等票务结束后再启动地图。

    事件约定：除 `step`/`subagent` 之外，取消或超时且清理窗口内仍有子任务未结束时，
    会额外 yield 一条**内部事件** `{"type": "cleanup", "subagent_leak": [任务名...]}`
    （无残留时不发）。它不是 SSE 契约事件，调用方消费后不应转发给浏览器；残留同样
    写在 `out["subagent_leak"]` 里，生成器已被关闭时也能从 `out` 读到。
    """
    out["records"] = out.get("records") or []
    out["parallel"] = bool(need_map and need_ticket and not force_serial())
    out["timed_out"] = False
    out["cancelled"] = False

    async def one(name: str) -> dict[str, Any]:
        started = time.monotonic()
        if name == "map":
            value = await run_map_agent(
                llm=llm, query=query, budget=budget, cancel_event=cancel_event, city=city, stats=stats
            )
        else:
            value = await run_ticket_agent(
                llm=llm,
                tools=ticket_tools,
                query=query,
                budget=budget,
                cancel_event=cancel_event,
                records=out["records"],
            )
        value["started_at"] = started
        value["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return value

    if need_map:
        yield {"type": "step", "id": "map", "status": "active"}
        yield {"type": "subagent", "name": "map_agent"}
    if need_ticket:
        yield {"type": "step", "id": "ticket", "status": "active"}
        yield {"type": "subagent", "name": "ticket_agent"}

    names = [name for name, needed in (("ticket", need_ticket), ("map", need_map)) if needed]
    pending: list[asyncio.Task] = []

    async def drain() -> list[str]:
        """取消并等待未结束的子任务，返回清理窗口结束后仍未结束的任务名（空=全部干净）。

        为什么用 `asyncio.shield`：本函数会从 `finally` 里被调用，此时外层流可能已收到
        GeneratorExit 或第二次取消。没有 shield 时，二次 CancelledError 会直接打断
        `asyncio.wait`，清理被跳过、残留也记录不下来。shield 让等待继续跑完，代价是
        这一段（默认 5 秒，`PLAN_CLEANUP_SECONDS`）内延续清理，最长不超过清理窗口。
        残留同时写进 `out["subagent_leak"]`，即使生成器已关闭也能被调用方读到。
        """
        for task in pending:
            if not task.done():
                task.cancel()
        if not pending:
            return []
        wait_task = asyncio.ensure_future(asyncio.wait(pending, timeout=cleanup_seconds()))
        while not wait_task.done():
            try:
                await asyncio.shield(wait_task)
            except asyncio.CancelledError:
                # 二次取消：shield 保住 wait_task，继续等它收尾（总时长仍受清理窗口约束）。
                continue
            except Exception:  # pragma: no cover - asyncio.wait 不会抛，防御性兜底
                break
        leaked = [task.get_name() for task in pending if not task.done()]
        if leaked:
            merged = list(dict.fromkeys([*(out.get("subagent_leak") or []), *leaked]))
            out["subagent_leak"] = merged
            logger.warning("subagent_leak tasks=%s", ",".join(leaked))
        # drain 会在 close_and_note 与 finally 里各调一次：状态按累计的 out 判定，
        # 而不是本次是否还有 pending，避免第二次调用把「有残留」覆盖成「已成功」。
        if out.get("subagent_leak"):
            note_stat("subagent", "timed_out")
        elif out.get("cancelled"):
            note_stat("subagent", "cancelled")
        elif out.get("timed_out"):
            note_stat("subagent", "timed_out")
        elif pending:
            note_stat("subagent", "succeeded")
        else:
            note_stat("subagent", "not_needed")
        return leaked

    async def close_and_note() -> dict[str, Any] | None:
        """清理并给出内部 `cleanup` 事件；没有残留时返回 None。

        约定：`{"type": "cleanup", "subagent_leak": [任务名...]}` 是**内部事件**，
        不是 SSE 契约事件（`status`/`slots`/`clarify`/`step`/`warning`/`token`/
        `final`/`error`）。调用方（`planner/pipeline.py`）消费后不应转发给浏览器；
        残留同名任务去重后按首次出现顺序排列。
        """
        await drain()
        leaks = list(dict.fromkeys(out.get("subagent_leak") or []))
        if not leaks:
            return None
        return {"type": "cleanup", "subagent_leak": leaks}

    try:
        if force_serial():
            for name in names:
                if cancel_event is not None and cancel_event.is_set():
                    out["cancelled"] = True
                    return
                if budget.work_left() <= 0:
                    out["timed_out"] = True
                    out[name] = {"started": False, "timed_out": True, "text": "", "summary": ""}
                    yield {"type": "step", "id": name, "status": "skipped", "reason": "dispatch_timeout"}
                    continue
                try:
                    out[name] = await asyncio.wait_for(one(name), timeout=budget.timeout_for(budget.agents) or 0.01)
                except asyncio.TimeoutError:
                    out["timed_out"] = True
                    out[name] = {"started": True, "timed_out": True, "text": "", "summary": ""}
                    yield {"type": "step", "id": name, "status": "skipped", "reason": "dispatch_timeout"}
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    out[name] = {"started": True, "error": type(exc).__name__, "text": "", "summary": ""}
                    yield {"type": "step", "id": name, "status": "done"}
                    continue
                yield {"type": "step", "id": name, "status": "done"}
            return

        queue: asyncio.Queue = asyncio.Queue()

        async def guard(name: str) -> None:
            try:
                value = await one(name)
                await queue.put(("ok", name, value))
            except asyncio.CancelledError:
                await queue.put(("cancel", name, None))
                raise
            except Exception as exc:
                await queue.put(("err", name, exc))

        pending = [asyncio.create_task(guard(name), name=name) for name in names]
        left = len(pending)
        deadline = time.monotonic() + (budget.timeout_for(budget.agents) or 0)
        while left:
            if cancel_event is not None and cancel_event.is_set():
                out["cancelled"] = True
                note = await close_and_note()
                if note is not None:
                    yield note
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0 or budget.work_left() <= 0:
                out["timed_out"] = True
                note = await close_and_note()
                if note is not None:
                    yield note
                for name in names:
                    out.setdefault(name, {"started": True, "timed_out": True, "text": "", "summary": ""})
                break
            try:
                kind, name, payload = await asyncio.wait_for(queue.get(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue
            left -= 1
            if kind == "ok":
                out[name] = payload
                yield {"type": "step", "id": name, "status": "done"}
            elif kind == "err":
                out[name] = {"started": True, "error": type(payload).__name__, "text": "", "summary": ""}
                yield {"type": "step", "id": name, "status": "done"}
            else:
                out[name] = {"started": True, "timed_out": True, "text": "", "summary": ""}
                out["timed_out"] = True
        for name in names:
            out.setdefault(name, {"started": False, "timed_out": bool(out["timed_out"]), "text": "", "summary": ""})
            if out["timed_out"] and out[name].get("timed_out"):
                yield {"type": "step", "id": name, "status": "skipped", "reason": "dispatch_timeout"}
    finally:
        # 生成器已被关闭时不能再 yield；这里只做清理并把残留写进 out，供调用方判定。
        await drain()
