"""汇总成文、落盘与 LangSmith 链接。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from slots import TravelSlots, format_slots_for_prompt
from summary_sub_agent import SUMMARY_AGENT_PROMPT

from planner.async_utils import message_text
from planner.paths import RESULTS_DIR


def ticket_summary_block(
    ticket_reason: Literal["unavailable", "not_needed", "ok"],
    ticket_context: str,
) -> str:
    """按三态生成汇总模型可见的票务上下文。"""
    if ticket_reason == "ok" and ticket_context.strip():
        return ticket_context.strip()
    if ticket_reason == "not_needed":
        return (
            "（本次无需铁路出行或未调度查票。车票建议一节请说明「本次未查票（无需铁路）」，"
            "禁止编造车次号/票价/时刻，可简要提示市内交通即可。）"
        )
    return (
        "（暂无票务结果或车票服务不可用。车票建议一节请明确说明暂无可靠票务数据，"
        "禁止编造车次号/票价/时刻。）"
    )


def save_plan(plan_id: str, query: str, final_answer: str) -> tuple[str, str]:
    """代码兜底写盘，返回 (绝对路径, /results/... URL)。"""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = f"旅游规划-{stamp}.md"
    path = RESULTS_DIR / filename
    body = (
        "---\n"
        f"plan_id: {json.dumps(plan_id, ensure_ascii=False)}\n"
        f"date: {json.dumps(datetime.now().strftime('%Y-%m-%d'), ensure_ascii=False)}\n"
        f"query: {json.dumps(query, ensure_ascii=False)}\n"
        "---\n\n"
        f"{final_answer}"
    )
    path.write_text(body, encoding="utf-8")
    return str(path), f"/results/{filename}"


def build_trace_url(run_id: str) -> str | None:
    if not (os.getenv("LANGSMITH_API_KEY") or "").strip():
        return None
    project = os.getenv("LANGSMITH_PROJECT", "travel-planner")
    try:
        from langsmith import Client

        client = Client()
        tenant = client._get_tenant_id()
        project_obj = client.read_project(project_name=project)
        return f"{client._host_url}/o/{tenant}/projects/p/{project_obj.id}/r/{run_id}?poll=true"
    except Exception:
        org = (os.getenv("LANGSMITH_ORG_ID") or "").strip()
        if org:
            return f"https://smith.langchain.com/o/{org}/projects/p/{project}/r/{run_id}"
        return None


async def stream_summary(
    *,
    llm: Any,
    query: str,
    slots: TravelSlots | None,
    map_context: str,
    ticket_context: str,
    ticket_reason: Literal["unavailable", "not_needed", "ok"],
) -> AsyncIterator[dict[str, Any]]:
    """流式生成最终方案；yield token 与内部 _summary_done。"""
    slots_text = format_slots_for_prompt(slots) if slots is not None else "（槽位未结构化确认，请根据用户原话理解）"
    map_block = map_context.strip() or "（暂无景点检索结果，请标注暂缺，不要编造具体景点细节）"
    ticket_block = ticket_summary_block(ticket_reason, ticket_context)
    user_msg = (
        f"用户原话：{query}\n\n{slots_text}\n\n"
        f"【地图子智能体结果】\n{map_block}\n\n"
        f"【车票子智能体结果】\n{ticket_block}\n"
    )
    pieces: list[str] = []
    async for chunk in llm.astream(
        [
            SystemMessage(content=SUMMARY_AGENT_PROMPT),
            HumanMessage(content=user_msg),
        ]
    ):
        delta = message_text(getattr(chunk, "content", None))
        if not delta:
            continue
        pieces.append(delta)
        yield {"type": "token", "content": delta}
    full = "".join(pieces).strip() or "本次没有生成最终结果"
    yield {"type": "_summary_done", "content": full}
