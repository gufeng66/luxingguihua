"""
一次旅游规划的事件流（CLI 与 Web 的唯一核心入口）。

【小白怎么理解 stream_plan？】
    它是 async 生成器：一边跑一边 yield 字典事件（status / step / clarify / token / final…）。
    网页用 SSE 转发这些事件；命令行则 print。

【正常顺序】
    1. 理解需求：抽槽位；缺出发地/目的地/日期 → yield clarify 后 return
    2. 路由：要不要查票、要不要地图
    3. 连 12306 MCP（需要票时）；失败则降级
    4. 主智能体并行调度 map_agent / ticket_agent
    5. stream_summary 流式成文 → 落盘 md + 快照 JSON → yield final

【修订（带 parent_plan_id）】
    map_only        只改景点，票务沿用快照
    ticket_and_map  改了日期/地点/出行方式，重新抽槽 + 可能再澄清
    summary_only    只改措辞，跳过检索
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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

from planner.backends import backend
from planner.dispatch import DispatchRun, iter_dispatch
from planner.paths import MAPS_DIR
from planner.prompts import build_main_prompt
from planner.routing import (
    classify_revision,
    merge_slots,
    resolve_map_state,
    resolve_ticket_state,
    skip_map_reason,
    skip_ticket_reason,
)
from planner.summary import (
    build_trace_url,
    load_snapshot,
    parse_plan_id,
    rewrite_plan_ids,
    save_plan,
    save_snapshot,
)
from planner.ticket_cache import last_lookup

logger = logging.getLogger("travel_planner")


def _svc():
    """延迟 import 根模块，避免 pipeline ↔ planner_service 循环导入。"""
    import planner_service as svc

    return svc


def _ms_since(t0: float) -> int:
    """monotonic 起点到现在的毫秒数（给 timings / step.elapsed_ms）。"""
    return int((time.monotonic() - t0) * 1000)


async def stream_plan(
    query: str,
    cancel_event: asyncio.Event | None = None,
    slots: TravelSlots | None = None,
    parent_plan_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """一次完整旅游规划的异步事件流（CLI 与 Web 共用）。

    slots: 确认卡回传时带上，跳过「再弹确认」（关键字段仍缺会再澄清）。
    parent_plan_id: 修订上一份方案时传入其 UUID。
    cancel_event: 浏览器断开时 set，尽快停 LLM。
    """
    svc = _svc()
    plan_uuid = uuid4()
    plan_id = str(plan_uuid)
    cancelled = False
    legacy_mode = False
    confirmed: TravelSlots | None = slots
    slots_from_client = slots is not None
    t_total = time.monotonic()
    understand_ms = 0
    ticket_mcp_ms = 0
    dispatch_ms = 0
    map_ms: int | None = None
    ticket_ms: int | None = None
    summary_ms = 0
    ticket_cache_hit = False
    parallel_tasks = False
    parallel_results = False
    map_active_at: float | None = None
    ticket_active_at: float | None = None
    revision_kind = None
    snapshot: dict[str, Any] | None = None
    parent_id: str | None = None
    previous_answer = ""
    reused_ticket_context = ""
    skip_reason: str | None = None
    need_ticket = True
    need_map = True
    skip_map: str | None = None
    timed_out = False
    ticket_agent: dict[str, Any] | None = None
    map_dispatched = False
    map_reason = "missed"
    map_context = ""
    ticket_reason = "skipped"
    ticket_context = ""

    try:
        # —— 修订：读上一版快照，判定改地图 / 改票 / 只改文案 ——
        if parent_plan_id is not None:
            parent_id = parse_plan_id(parent_plan_id)
            if parent_id is None:
                yield {"type": "error", "message": "plan_id 无效，请重新规划"}
                return
            snapshot = load_snapshot(parent_id)
            if snapshot is None:
                yield {"type": "error", "message": "找不到上一份方案快照，请重新规划"}
                return
            prev_state = snapshot.get("ticket_reason")
            revision_kind = classify_revision(query, previous_ticket_state=prev_state)
            previous_answer = rewrite_plan_ids(str(snapshot.get("answer") or ""), parent_id, plan_id)
            reused_ticket_context = rewrite_plan_ids(
                str(snapshot.get("ticket_context") or ""), parent_id, plan_id
            )

        # —— 理解需求：抽槽位或沿用快照；缺关键字段则 clarify 后结束本轮 ——
        yield {
            **({"label": "修订需求"} if revision_kind else {}),
        }
        yield {"type": "status", "message": "正在理解需求…" if not revision_kind else "正在理解修改意见…"}
        t_understand = time.monotonic()

        llm = svc.build_llm()

        if revision_kind == "map_only":
            confirmed = TravelSlots.model_validate(snapshot.get("slots") or {})
            confirmed, _ = apply_soft_defaults(confirmed)
            yield {"type": "slots", "slots": confirmed.model_dump()}
        elif revision_kind == "summary_only":
            confirmed = TravelSlots.model_validate(snapshot.get("slots") or {})
            confirmed, _ = apply_soft_defaults(confirmed)
            yield {"type": "slots", "slots": confirmed.model_dump()}
        elif revision_kind == "ticket_and_map":
            base = TravelSlots.model_validate(snapshot.get("slots") or {})
            try:
                overlay = await svc.extract_slots(query, llm)
                confirmed = merge_slots(base, overlay)
            except Exception:
                confirmed = base
            confirmed, _ = apply_soft_defaults(confirmed)
            yield {"type": "slots", "slots": confirmed.model_dump()}
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
        elif confirmed is not None:
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
                yield {"type": "status", "message": "参数解析失败，按原模式规划"}

        if cancel_event is not None and cancel_event.is_set():
            return

        understand_ms = _ms_since(t_understand)
        yield {"type": "step", "id": "understand", "status": "done", "elapsed_ms": understand_ms}

        # —— 路由：要不要铁路、要不要路线地图 ——
        route_query = query
        if snapshot:
            route_query = f"{snapshot.get('query') or ''} {query}"
        if revision_kind == "map_only":
            need_ticket = False
            skip_reason = "route_no_rail"
        elif revision_kind == "summary_only":
            need_ticket = False
            skip_reason = "route_no_rail"
        else:
            skip_reason = skip_ticket_reason(confirmed, route_query)
            need_ticket = skip_reason is None

        skip_map = skip_map_reason(query)
        if revision_kind == "summary_only":
            need_map = False
            skip_map = skip_map or "route_no_map"
        else:
            need_map = skip_map is None

        logger.info("plan_id=%s need_ticket=%s skip_reason=%s need_map=%s revision=%s", plan_id, need_ticket, skip_reason, need_map, revision_kind)

        if revision_kind == "summary_only":
            ticket_reason = "reused" if reused_ticket_context.strip() else "skipped"
            map_context = rewrite_plan_ids(str(snapshot.get("map_context") or ""), parent_id or "", plan_id)
            ticket_context = reused_ticket_context
            map_reason = "ok" if str(snapshot.get("map_context") or "").strip() else "skipped"
            yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "route_no_rail"}
            yield {"type": "step", "id": "map", "status": "skipped", "reason": "route_no_map"}
        else:
            if not need_ticket:
                ticket_agent = None
                reason = skip_reason or "route_no_rail"
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": reason}
                yield {"type": "status", "message": "未查票（无需铁路）"}
            else:
                yield {"type": "status", "message": "正在连接 12306 MCP 并加载车票工具..."}
                t_mcp = time.monotonic()
                ticket_agent = await svc.get_ticket_agent()
                lookup = last_lookup()
                ticket_cache_hit = bool(lookup.get("cache_hit"))
                ticket_mcp_ms = int(lookup.get("connect_ms") or _ms_since(t_mcp))
                if cancel_event is not None and cancel_event.is_set():
                    return
                if ticket_agent is None:
                    yield {"type": "status", "message": "车票服务暂不可用，本次仅生成景点方案"}
                    yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "mcp_unavailable"}
                else:
                    tool_count = len(ticket_agent.get("tools") or [])
                    yield {"type": "status", "message": f"车票工具已加载，共 {tool_count} 个。"}

            if not need_map:
                yield {"type": "step", "id": "map", "status": "skipped", "reason": skip_map or "user_no_map"}
                yield {"type": "status", "message": "未生成路线地图（用户明确不要地图）"}

            # —— 连 MCP、挂子智能体、主智能体 astream ——
            tracing_on = (os.getenv("LANGSMITH_TRACING") or "").lower() == "true"
            if tracing_on:
                yield {"type": "status", "message": "本次规划已发送到 LangSmith 项目 travel-planner"}

            map_spec = {
                **map_agent,
                "system_prompt": str(map_agent["system_prompt"]).replace("{plan_id}", plan_id),
            }
            subagents: list[Any] = []
            if need_map:
                subagents.append(map_spec)
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
                    need_ticket=need_ticket,
                    skip_reason=skip_reason,
                    revision_map_only=revision_kind == "map_only",
                    need_map=need_map,
                ),
                memory=["/workspace/config/memory/AGENTS.md"],
                subagents=subagents,
            )
            yield {"type": "status", "message": "开始规划"}

            user_content = query
            if confirmed is not None and not legacy_mode:
                user_content = f"{query}\n\n{format_slots_for_prompt(confirmed)}"
            if revision_kind == "map_only":
                user_content = f"修订意见：{query}\n请调整景点与路线。\n\n{format_slots_for_prompt(confirmed)}"

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

            dispatch = DispatchRun()
            async for event in iter_dispatch(
                agen, cancel_event=cancel_event, plan_id=plan_id, run=dispatch
            ):
                yield event
            cancelled = dispatch.cancelled
            timed_out = dispatch.timed_out
            map_dispatched = dispatch.map_dispatched
            map_ms = dispatch.map_ms
            ticket_ms = dispatch.ticket_ms
            map_active_at = dispatch.map_active_at
            ticket_active_at = dispatch.ticket_active_at
            parallel_tasks = dispatch.parallel_tasks
            parallel_results = dispatch.parallel_results
            dispatch_ms = dispatch.dispatch_ms
            if cancelled:
                return

            map_context = "\n\n".join(dispatch.map_chunks)
            ticket_context = "\n\n".join(dispatch.ticket_chunks)
            if revision_kind == "map_only":
                ticket_reason = "reused" if reused_ticket_context.strip() else "skipped"
                ticket_context = reused_ticket_context
            else:
                ticket_reason = resolve_ticket_state(
                    need_ticket=need_ticket,
                    ticket_mounted=ticket_agent is not None,
                    has_ticket_result=bool(dispatch.ticket_chunks),
                    timed_out=timed_out,
                )

            map_file_now = MAPS_DIR / f"{plan_id}.html"
            map_reason = resolve_map_state(
                need_map=need_map,
                dispatched=map_dispatched,
                has_result=bool(dispatch.map_chunks) or map_file_now.is_file(),
                timed_out=timed_out,
            )
            if map_reason == "missed":
                logger.warning("map_missed plan_id=%s", plan_id)
                yield {
                    "type": "warning",
                    "code": "map_not_dispatched",
                    "message": "本应检索景点但未调度 map_agent，汇总将标注暂缺",
                }
            elif map_reason == "timeout":
                logger.warning("map_timeout plan_id=%s", plan_id)
                yield {
                    "type": "warning",
                    "code": "map_dispatch_timeout",
                    "message": "已调度 map_agent 但超时，路线地图可能未写完",
                }
            elif map_reason == "incomplete":
                logger.warning("map_incomplete plan_id=%s", plan_id)
                yield {
                    "type": "warning",
                    "code": "map_no_html",
                    "message": "已调度 map_agent 但未拿到路线地图，汇总将标注暂缺",
                }

            if ticket_reason == "missed":
                logger.warning("ticket_missed plan_id=%s", plan_id)
                yield {
                    "type": "warning",
                    "code": "ticket_not_dispatched",
                    "message": "本应查票但未调度 ticket_agent，汇总将禁止编造车次",
                }
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "not_dispatched"}
            elif ticket_reason == "timeout":
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "dispatch_timeout"}
            elif ticket_reason == "unavailable":
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "mcp_unavailable"}

        if cancelled:
            return

        # —— 流式汇总成文（主智能体不再写长方案）——
        yield {"type": "step", "id": "summary", "status": "active"}
        yield {"type": "status", "message": "正在汇总最终方案…"}
        t_summary = time.monotonic()

        answer = "本次没有生成最终结果"
        async for event in svc.stream_summary(
            llm=llm,
            query=query,
            slots=confirmed,
            map_context=map_context,
            ticket_context=ticket_context,
            ticket_reason=ticket_reason,
            map_reason=map_reason,
            previous_answer=previous_answer or None,
            revision_forbid_old_tickets=revision_kind == "ticket_and_map",
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

        summary_ms = _ms_since(t_summary)
        yield {"type": "step", "id": "summary", "status": "done", "elapsed_ms": summary_ms}

        # —— 落盘 Markdown + 修订快照，带上耗时与地图 URL ——
        _saved_path, saved_url = save_plan(plan_id, query, answer)
        save_snapshot(
            {
                "plan_id": plan_id,
                "parent_plan_id": parent_id,
                "query": query,
                "slots": confirmed.model_dump() if confirmed is not None else None,
                "ticket_reason": ticket_reason,
                "map_context": map_context,
                "ticket_context": ticket_context,
                "answer": answer,
            }
        )
        map_file = MAPS_DIR / f"{plan_id}.html"
        delta_ms = None
        if map_active_at is not None and ticket_active_at is not None:
            delta_ms = int(abs(map_active_at - ticket_active_at) * 1000)
        timings: dict[str, Any] = {
            "understand_ms": understand_ms,
            "ticket_mcp_ms": ticket_mcp_ms,
            "dispatch_ms": dispatch_ms,
            "summary_ms": summary_ms,
            "total_ms": _ms_since(t_total),
            "ticket_cache_hit": ticket_cache_hit,
            "parallel_dispatch": bool(parallel_tasks and parallel_results),
        }
        if map_ms is not None:
            timings["map_ms"] = map_ms
        if ticket_ms is not None:
            timings["ticket_ms"] = ticket_ms
        if delta_ms is not None:
            timings["map_ticket_active_delta_ms"] = delta_ms
        final_event: dict[str, Any] = {
            "type": "final",
            "content": answer,
            "saved_url": saved_url,
            "plan_id": plan_id,
            "timings": timings,
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
        logger.exception("plan_id=%s failed", plan_id)
        yield {"type": "error", "message": str(exc)}
