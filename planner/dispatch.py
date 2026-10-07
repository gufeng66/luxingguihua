"""
主智能体 astream 解析：把 LangGraph chunk 翻成 SSE 事件，并记下 map/ticket 结果。

只负责「调度这一段」；要不要挂子智能体、怎么汇总，仍在 pipeline.stream_plan。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from planner.async_utils import message_text, ms_since, truncate, watch_aiter
from planner.usage import note_llm

logger = logging.getLogger("travel_planner")


@dataclass
class DispatchRun:
    cancelled: bool = False
    timed_out: bool = False
    map_dispatched: bool = False
    map_chunks: list[str] = field(default_factory=list)
    ticket_chunks: list[str] = field(default_factory=list)
    map_ms: int | None = None
    ticket_ms: int | None = None
    map_active_at: float | None = None
    ticket_active_at: float | None = None
    parallel_tasks: bool = False
    parallel_results: bool = False
    dispatch_ms: int = 0


def _subagent_step_id(name: str | None) -> str | None:
    """DeepAgents 子智能体名字 → 前端四步进度条的 id。"""
    if not name:
        return None
    if name in ("ticket_agent", "ticket"):
        return "ticket"
    if name in ("map_agent", "map"):
        return "map"
    if name in ("summary_agent", "summary"):
        return "summary"
    return None


async def iter_dispatch(
    agen: AsyncIterator[Any],
    *,
    cancel_event: asyncio.Event | None,
    plan_id: str,
    run: DispatchRun,
    timeout_seconds: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """消费 main_agent.astream，边走边 yield 事件；结果写入 run。

    timeout_seconds=None：一直等到流结束或客户端断开（需要 map 时用）。
    """
    tool_call_to_subagent: dict[str, str] = {}
    active_steps: set[str] = set()
    step_started: dict[str, float] = {}
    t_dispatch = time.monotonic()

    try:
        async for chunk in watch_aiter(
            agen,
            cancel_event,
            timeout_seconds=timeout_seconds,
        ):
            if cancel_event is not None and cancel_event.is_set():
                run.cancelled = True
                break
            if not isinstance(chunk, dict):
                continue
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                if node_name == "model":
                    for event in _on_model(
                        state, run, tool_call_to_subagent, active_steps, step_started
                    ):
                        yield event
                elif node_name == "tools":
                    for event in _on_tools(
                        state, run, tool_call_to_subagent, active_steps, step_started
                    ):
                        yield event
    except asyncio.TimeoutError:
        run.timed_out = True
        logger.warning("dispatch_timeout plan_id=%s", plan_id)
        yield {"type": "status", "message": "子智能体调度超时，将根据已有结果汇总"}

    run.dispatch_ms = ms_since(t_dispatch)
    if cancel_event is not None and cancel_event.is_set():
        run.cancelled = True
    if run.cancelled:
        return

    for step_id in list(active_steps):
        active_steps.discard(step_id)
        extra: dict[str, Any] = {
            "type": "step",
            "id": step_id,
            "status": "skipped" if run.timed_out else "done",
        }
        if run.timed_out:
            extra["reason"] = "dispatch_timeout"
        if step_id in step_started:
            extra["elapsed_ms"] = ms_since(step_started[step_id])
            if step_id == "map" and run.map_ms is None:
                run.map_ms = extra["elapsed_ms"]
            if step_id == "ticket" and run.ticket_ms is None:
                run.ticket_ms = extra["elapsed_ms"]
        yield extra


def _on_model(
    state: Any,
    run: DispatchRun,
    tool_call_to_subagent: dict[str, str],
    active_steps: set[str],
    step_started: dict[str, float],
) -> Iterator[dict[str, Any]]:
    task_names: list[str] = []
    for message in state["messages"]:
        note_llm(message)
        text = message_text(getattr(message, "content", None))
        if text:
            yield {"type": "model", "content": text}
        tool_calls = getattr(message, "tool_calls", None) or []
        for tool_call in tool_calls:
            name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
            args = tool_call.get("args") if isinstance(tool_call, dict) else getattr(tool_call, "args", {}) or {}
            call_id = (
                tool_call.get("id")
                if isinstance(tool_call, dict)
                else getattr(tool_call, "id", None)
            )
            if name == "task":
                sub_name = args.get("subagent_type") if isinstance(args, dict) else None
            elif name in ("map_agent", "map", "ticket_agent", "ticket"):
                sub_name = str(name)
            else:
                sub_name = None
            if sub_name:
                task_names.append(str(sub_name))
                if _subagent_step_id(sub_name) == "map":
                    run.map_dispatched = True
                if call_id and sub_name:
                    tool_call_to_subagent[str(call_id)] = str(sub_name)
                step_id = _subagent_step_id(sub_name)
                if step_id and step_id not in active_steps:
                    active_steps.add(step_id)
                    step_started[step_id] = time.monotonic()
                    if step_id == "map":
                        run.map_active_at = step_started[step_id]
                    if step_id == "ticket":
                        run.ticket_active_at = step_started[step_id]
                    yield {"type": "step", "id": step_id, "status": "active"}
                yield {"type": "subagent", "name": sub_name}
            else:
                yield {
                    "type": "tool",
                    "name": name,
                    "args": args if isinstance(args, dict) else {},
                }
    mapped = {n for n in task_names if n in ("map_agent", "map", "ticket_agent", "ticket")}
    if any(n in mapped for n in ("map_agent", "map")) and any(
        n in mapped for n in ("ticket_agent", "ticket")
    ):
        run.parallel_tasks = True


def _on_tools(
    state: Any,
    run: DispatchRun,
    tool_call_to_subagent: dict[str, str],
    active_steps: set[str],
    step_started: dict[str, float],
) -> Iterator[dict[str, Any]]:
    result_subs: list[str] = []
    for message in state["messages"]:
        content = getattr(message, "content", None)
        text = message_text(content)
        yield {"type": "tool_result", "content": truncate(text)}
        tcid = getattr(message, "tool_call_id", None)
        sub = None
        if tcid and str(tcid) in tool_call_to_subagent:
            sub = tool_call_to_subagent[str(tcid)]
        elif len(active_steps) == 1:
            only = next(iter(active_steps))
            if only == "map":
                sub = "map_agent"
                run.map_dispatched = True
            elif only == "ticket":
                sub = "ticket_agent"
        if sub:
            result_subs.append(sub)
            if sub in ("map_agent", "map"):
                run.map_chunks.append(text)
            elif sub in ("ticket_agent", "ticket"):
                run.ticket_chunks.append(text)
            step_id = _subagent_step_id(sub)
            if step_id and step_id in active_steps:
                active_steps.discard(step_id)
                elapsed = ms_since(step_started.get(step_id, time.monotonic()))
                if step_id == "map":
                    run.map_ms = elapsed
                if step_id == "ticket":
                    run.ticket_ms = elapsed
                yield {"type": "step", "id": step_id, "status": "done", "elapsed_ms": elapsed}
    if any(s in result_subs for s in ("map_agent", "map")) and any(
        s in result_subs for s in ("ticket_agent", "ticket")
    ):
        run.parallel_results = True
