"""
汇总成文、方案落盘、修订快照、LangSmith 链接。

【小白怎么理解？】
    主智能体只调度检索，不写长文。stream_summary 直接流式调大模型，
    边生成边 yield token 事件给前端打字机。
    快照 JSON 给「改景点 / 改日期」下一轮复用票务或行程上下文。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage

from slots import TravelSlots, format_slots_for_prompt

from planner.async_utils import message_text
from planner.paths import RESULTS_DIR, SNAPSHOTS_DIR
from planner.routing import TicketState, MapState

SUMMARY_AGENT_PROMPT = """
你是一名旅行方案汇总助手。
你不负责外部查询，只负责整理结果。

规则：
1. 将景点方案与车票方案合并
2. 输出最终推荐方案、备选方案、预算估算
3. 必须按以下六个章节标题输出（使用 Markdown ## 标题）：需求摘要、景点建议、车票建议、预算、行程表、注意事项
4. 保持简洁，不重复原始数据
5. 若某一路结果标注暂缺或服务不可用：对应章节如实说明，禁止编造车次号、票价、发车时刻或未检索到的景点细节
"""


def ticket_summary_block(
    ticket_reason: TicketState,
    ticket_context: str,
) -> str:
    """按票务态生成汇总可见上下文；车票建议章节必须保留。"""
    body = ticket_context.strip()
    if ticket_reason == "ok" and body:
        return body
    if ticket_reason == "reused":
        reused = body or "（上一版票务原文缺失）"
        return (
            "（票务沿用上一版查询结果，本次未重新查询。"
            "车票建议一节请标注「车票信息沿用上次查询」，"
            "禁止新增未出现的车次号/票价/时刻。）\n"
            f"{reused}"
        )
    if ticket_reason == "skipped":
        return (
            "（本次无需铁路出行。车票建议一节请说明「本次未查票（无需铁路）」，"
            "禁止编造车次号/票价/时刻，可简要提示市内交通即可。）"
        )
    if ticket_reason == "missed":
        return (
            "（本应查票但未调度 ticket_agent。车票建议一节请说明「本应查票但未调度」，"
            "禁止编造车次号/票价/时刻。）"
        )
    if ticket_reason == "timeout":
        return (
            "（查票超时，无可靠票务结果。车票建议一节请说明「查票超时」，"
            "禁止编造车次号/票价/时刻。）"
        )
    return (
        "（车票服务不可用。车票建议一节请明确说明暂无可靠票务数据、车票服务不可用，"
        "禁止编造车次号/票价/时刻。）"
    )


def map_summary_block(map_reason: MapState, map_context: str) -> str:
    """按地图态生成给汇总模型看的约束段落（缺图时禁止编景点细节）。"""
    body = map_context.strip()
    if map_reason == "ok" and body:
        return body
    if map_reason == "ok":
        return "（景点检索已完成；路线地图 HTML 已落盘。景点建议可依据文件，不要编造未出现的景点。）"
    if map_reason == "skipped":
        return "（用户明确不要路线地图。景点建议可简要文字，不要声称已生成地图。）"
    if map_reason == "timeout":
        return (
            "（map_agent 已调度但超时，路线地图可能未写完。"
            "景点建议请说明检索超时/地图暂缺，禁止编造具体未确认的景点细节。）"
        )
    if map_reason == "incomplete":
        return (
            "（map_agent 已调度但没有生成路线地图 HTML。正文即使有内容也不得当作本次景点方案。"
            "景点建议请说明地图未生成，禁止编造具体景点细节。）"
        )
    return "（本应检索景点但未调度 map_agent。请标注暂缺，不要编造具体景点细节。）"


def parse_plan_id(raw: str | None) -> str | None:
    """只接受标准 UUID 字符串，防止随意路径读快照文件。"""
    if raw is None:
        return None
    try:
        return str(UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None


def rewrite_plan_ids(text: str, old_id: str, new_id: str) -> str:
    """修订时把旧方案里的 plan_id（地图文件名）换成这一轮的 id。"""
    if not text or old_id == new_id:
        return text
    return text.replace(old_id, new_id)


def snapshot_path(plan_id: str):
    """workspace/results/snapshots/{uuid}.json"""
    return SNAPSHOTS_DIR / f"{plan_id}.json"


def save_snapshot(payload: dict[str, Any]) -> None:
    """写入修订所需上下文：槽位、票务态、子智能体原文、成文。"""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(str(payload["plan_id"]))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot(plan_id: str) -> dict[str, Any] | None:
    """没有对应文件则返回 None（前端应提示重新规划）。"""
    path = snapshot_path(plan_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_plan(plan_id: str, query: str, final_answer: str) -> tuple[str, str]:
    """把方案写成带 YAML 头的 Markdown。返回 (本地路径, 给浏览器的 /results/… URL)。"""
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
    """拼 LangSmith 本次 run 链接；没 Key 返回 None。API 失败则退回环境变量拼 URL。"""
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
    ticket_reason: TicketState,
    map_reason: MapState = "missed",
    previous_answer: str | None = None,
    revision_forbid_old_tickets: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """流式生成最终 Markdown。对外 yield token；最后一帧 _summary_done 仅给 pipeline 收全文。"""
    slots_text = (
        format_slots_for_prompt(slots, rail=ticket_reason != "skipped")
        if slots is not None
        else "（槽位未结构化确认，请根据用户原话理解）"
    )
    map_block = map_summary_block(map_reason, map_context)
    ticket_block = ticket_summary_block(ticket_reason, ticket_context)
    parts = [
        f"用户原话 / 修改意见：{query}\n",
        slots_text,
        f"\n【地图子智能体结果】\n{map_block}\n",
        f"\n【车票子智能体结果】\n{ticket_block}\n",
    ]
    if previous_answer and previous_answer.strip():
        extra = ""
        if revision_forbid_old_tickets:
            extra = "其中车次/票价/时刻一律不得沿用，必须以上述本次票务上下文为准。"
        parts.append(f"\n【上一版方案（仅供保持结构一致）】{extra}\n{previous_answer.strip()}\n")
    user_msg = "\n".join(parts)
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
