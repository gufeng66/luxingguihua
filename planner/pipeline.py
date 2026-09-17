"""规划流水线：stream_plan 编排槽位 → 子智能体 → 汇总。

可被单测 monkeypatch 的符号（build_llm / extract_slots / get_ticket_agent /
create_deep_agent / stream_summary）一律经 planner_service 延迟查找。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from map_sub_agent import map_agent
from slots import (
    TravelSlots,
    apply_soft_defaults,
    format_slots_for_prompt,
    missing_critical,
    should_clarify,
)

from planner.async_utils import message_text, truncate, watch_aiter
from planner.backends import backend
from planner.paths import MAPS_DIR
from planner.prompts import build_main_prompt
from planner.summary import build_trace_url, save_plan
from planner.ticket_cache import resolve_ticket_reason


def _subagent_step_id(name: str | None) -> str | None:
    if not name:
        return None
    if name in ("ticket_agent", "ticket"):
        return "ticket"
    if name in ("map_agent", "map"):
        return "map"
    if name in ("summary_agent", "summary"):
        return "summary"
    return None


def _svc():
    """延迟取 planner_service，便于测试替换其中的函数。"""
    import planner_service as svc

    return svc


async def stream_plan(
    query: str,
    cancel_event: asyncio.Event | None = None,
    slots: TravelSlots | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """一次完整旅游规划的异步事件流（CLI 与 Web 的唯一核心入口）。"""
    svc = _svc()
    plan_uuid = uuid4()
    plan_id = str(plan_uuid)
    cancelled = False
    legacy_mode = False
    confirmed: TravelSlots | None = slots
    slots_from_client = slots is not None

    try:
        yield {"type": "step", "id": "understand", "status": "active"}
        yield {"type": "status", "message": "正在理解需求…"}

        llm = svc.build_llm()

        if confirmed is not None:
            if should_clarify(confirmed, slots_from_client=True):
                preview, defaults_applied = apply_soft_defaults(confirmed)
                yield {
                    "type": "clarify",
                    "slots": preview.model_dump(),
                    "missing": missing_critical(confirmed),
                    "defaults_applied": defaults_applied,
                    "message": "请确认或补全关键信息后继续",
                }
                yield {"type": "step", "id": "understand", "status": "done"}
                return
            confirmed, _ = apply_soft_defaults(confirmed)
            yield {"type": "slots", "slots": confirmed.model_dump()}
        else:
            try:
                extracted = await svc.extract_slots(query, llm)
                yield {"type": "slots", "slots": extracted.model_dump()}
                if should_clarify(extracted, slots_from_client=False):
                    preview, defaults_applied = apply_soft_defaults(extracted)
                    yield {
                        "type": "clarify",
                        "slots": preview.model_dump(),
                        "missing": missing_critical(extracted),
                        "defaults_applied": defaults_applied,
                        "message": "请确认或补全关键信息后继续",
                    }
                    yield {"type": "step", "id": "understand", "status": "done"}
                    return
                confirmed, _ = apply_soft_defaults(extracted)
            except Exception:
                legacy_mode = True
                confirmed = None
                yield {
                    "type": "status",
                    "message": "参数解析失败，按原模式规划",
                }

        if cancel_event is not None and cancel_event.is_set():
            return

        yield {"type": "step", "id": "understand", "status": "done"}

        yield {"type": "status", "message": "正在连接 12306 MCP 并加载车票工具..."}
        ticket_agent = await svc.get_ticket_agent()
        if cancel_event is not None and cancel_event.is_set():
            return
        if ticket_agent is None:
            yield {"type": "status", "message": "车票服务暂不可用，本次仅生成景点方案"}
            yield {"type": "step", "id": "ticket", "status": "skipped"}
        else:
            tool_count = len(ticket_agent.get("tools") or [])
            yield {"type": "status", "message": f"车票工具已加载，共 {tool_count} 个。"}

        tracing_on = (os.getenv("LANGSMITH_TRACING") or "").lower() == "true"
        if tracing_on:
            yield {"type": "status", "message": "本次规划已发送到 LangSmith 项目 travel-planner"}

        map_spec = {
            **map_agent,
            "system_prompt": str(map_agent["system_prompt"]).replace("{plan_id}", plan_id),
        }
        subagents: list[Any] = [map_spec]
        if ticket_agent is not None:
            subagents.append(ticket_agent)

        main_agent = svc.create_deep_agent(
            model=llm,
            backend=backend,
            system_prompt=build_main_prompt(
                ticket_available=ticket_agent is not None,
                plan_id=plan_id,
                slots=confirmed,
                legacy=legacy_mode,
            ),
            memory=["/workspace/config/memory/AGENTS.md"],
            subagents=subagents,
        )
        yield {"type": "status", "message": "开始规划"}

        user_content = query
        if confirmed is not None and not legacy_mode:
            user_content = f"{query}\n\n{format_slots_for_prompt(confirmed)}"

        run_config = {
            "run_id": plan_uuid,
            "run_name": "travel-plan",
            "tags": ["travel-planner", "web-sse"],
            "metadata": {"plan_id": plan_id, "query_preview": query[:80]},
        }
        agen = main_agent.astream(
            {"messages": [{"role": "user", "content": user_content}]},
            config=run_config,
        )

        tool_call_to_subagent: dict[str, str] = {}
        map_chunks: list[str] = []
        ticket_chunks: list[str] = []
        active_steps: set[str] = set()

        async for chunk in watch_aiter(agen, cancel_event):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                for message in state["messages"]:
                    if node_name == "model":
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
                                if call_id and sub_name:
                                    tool_call_to_subagent[str(call_id)] = str(sub_name)
                                step_id = _subagent_step_id(sub_name)
                                if step_id and step_id not in active_steps:
                                    active_steps.add(step_id)
                                    yield {"type": "step", "id": step_id, "status": "active"}
                                yield {"type": "subagent", "name": sub_name}
                            else:
                                yield {
                                    "type": "tool",
                                    "name": name,
                                    "args": args if isinstance(args, dict) else {},
                                }
                    elif node_name == "tools":
                        content = getattr(message, "content", None)
                        text = message_text(content)
                        yield {"type": "tool_result", "content": truncate(text)}
                        tcid = getattr(message, "tool_call_id", None)
                        if tcid and str(tcid) in tool_call_to_subagent:
                            sub = tool_call_to_subagent[str(tcid)]
                            if sub in ("map_agent", "map"):
                                map_chunks.append(text)
                            elif sub in ("ticket_agent", "ticket"):
                                ticket_chunks.append(text)
                            step_id = _subagent_step_id(sub)
                            if step_id and step_id in active_steps:
                                active_steps.discard(step_id)
                                yield {"type": "step", "id": step_id, "status": "done"}

        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        if cancelled:
            return

        for step_id in list(active_steps):
            active_steps.discard(step_id)
            yield {"type": "step", "id": step_id, "status": "done"}

        ticket_reason = resolve_ticket_reason(
            ticket_mounted=ticket_agent is not None,
            has_ticket_result=bool(ticket_chunks),
        )
        if ticket_reason == "not_needed":
            yield {"type": "step", "id": "ticket", "status": "skipped"}
            yield {"type": "status", "message": "未查票（无需铁路或未调度 ticket_agent）"}

        yield {"type": "step", "id": "summary", "status": "active"}
        yield {"type": "status", "message": "正在汇总最终方案…"}

        map_context = "\n\n".join(map_chunks)
        ticket_context = "\n\n".join(ticket_chunks)
        answer = "本次没有生成最终结果"
        async for event in svc.stream_summary(
            llm=llm,
            query=query,
            slots=confirmed,
            map_context=map_context,
            ticket_context=ticket_context,
            ticket_reason=ticket_reason,
        ):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if event.get("type") == "_summary_done":
                answer = str(event.get("content") or answer)
            else:
                yield event

        if cancelled:
            return

        yield {"type": "step", "id": "summary", "status": "done"}

        _saved_path, saved_url = save_plan(plan_id, query, answer)
        map_file = MAPS_DIR / f"{plan_id}.html"
        final_event: dict[str, Any] = {
            "type": "final",
            "content": answer,
            "saved_url": saved_url,
            "plan_id": plan_id,
        }
        if map_file.is_file():
            final_event["map_url"] = f"/results/maps/{plan_id}.html"
        trace_url = build_trace_url(plan_id)
        if trace_url:
            final_event["trace_url"] = trace_url
        yield final_event
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            return
        yield {"type": "error", "message": str(exc)}
