"""
汇总成文、方案落盘、修订快照、LangSmith 链接。

【小白怎么理解？】
    主智能体只调度检索，不写长文。stream_summary 直接流式调大模型，
    边生成边 yield token 事件给前端打字机。
    快照 JSON 给「改景点 / 改日期」下一轮复用票务或行程上下文。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage

from slots import TravelSlots, format_slots_for_prompt

from evals.checkers import TRAIN_NUMBER_RE, assert_no_fabricated_trains, missing_sections

from planner.async_utils import message_text
from planner.paths import RESULTS_DIR, SNAPSHOTS_DIR
from planner.routing import TicketState, MapState
from planner.usage import note_llm, usage_call

# planner.urls 由并行任务提供，可能还没落地。这里可选导入：拿不到时证据链接
# 一律过滤为空（宁可不给链接，也不把未校验 URL 塞进模型上下文）。
try:  # pragma: no cover - 正常仓库里都能导入成功
    from planner.urls import safe_evidence_url as _safe_evidence_url
except ImportError:  # pragma: no cover - 仅在并行任务未落地时走到
    _safe_evidence_url = None

SUMMARY_AGENT_PROMPT = """
你是一名旅行方案汇总助手。
你不负责外部查询，只负责整理结果。

规则：
1. 将景点方案与车票方案合并
2. 输出最终推荐方案、备选方案、预算估算
3. 必须按以下六个章节标题输出（使用 Markdown ## 标题）：需求摘要、景点建议、车票建议、预算、行程表、注意事项
4. 保持简洁，不重复原始数据
5. 若某一路结果标注暂缺或服务不可用：对应章节如实说明，禁止编造车次号、票价、发车时刻或未检索到的景点细节
6. 行程表一节会被系统替换。不要另写一套时刻表；景点与交通以【地图子智能体结果】里的规范行程为准
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


def replace_named_section(text: str, title: str, body: str) -> str:
    """用程序正文替换一个二级标题。多份只留一份；没有标题就补一节。"""
    block = f"## {title}\n" + (body or "").strip() + "\n\n"
    parts = re.split(r"(?=^##\s+)", text or "", flags=re.M)
    seen = False
    replaced = False
    out: list[str] = []
    for part in parts:
        first = part.strip().splitlines()[0] if part.strip() else ""
        if re.match(rf"##\s+{re.escape(title)}\s*$", first):
            if seen:
                continue
            seen = True
            replaced = True
            out.append(block)
            continue
        out.append(part)
    if not replaced:
        out.append("\n" + block)
    return "".join(out).strip() + "\n"


def replace_itinerary_section(text: str, body: str) -> str:
    """用程序行程替换「行程表」章节。"""
    return replace_named_section(text, "行程表", body)


def section_body(text: str, title: str) -> str:
    """取「## 标题」到下一个二级标题之间的正文；没有该章节返回空串。"""
    body = text or ""
    match = re.search(rf"^##\s+{re.escape(title)}\s*$", body, flags=re.M)
    if not match:
        return ""
    rest = body[match.end() :]
    nxt = re.search(r"^##\s+", rest, flags=re.M)
    return (rest[: nxt.start()] if nxt else rest).strip()


def _days_text(date_text: str | None, days: int | None) -> str:
    """日期 + 天数 → 「2026-11-10 至 2026-11-11，共 2 天」。"""
    try:
        first = date.fromisoformat(str(date_text or "").strip())
    except (TypeError, ValueError):
        return "日期：未定"
    span = max(1, int(days or 1))
    if span == 1:
        return f"日期：{first.isoformat()}（共 1 天）"
    last = first + timedelta(days=span - 1)
    return f"日期：{first.isoformat()} 至 {last.isoformat()}，共 {span} 天"


def _rail_line(*, need_ticket: bool, rail_state: dict | None) -> str:
    """「铁路」一行：区分「不需要铁路」「需要但本轮没查」「查了」「沿用了上次结果」。

    need_ticket 是调度标志（这一轮要不要连票务），不等于「这趟行程不需要铁路」。
    复用分支根本不连票务，早期写法于是把「沿用旧票」说成「本次不需要（未查票）」，
    读者会以为这趟行程压根没有铁路段。rail_state 为 None 时保持旧的两态行为，
    兼容既有调用方与单测。
    """
    if rail_state is None:
        return (
            "- 铁路：本次需要查票，结果见「车票建议」一节"
            if need_ticket
            else "- 铁路：本次不需要（未查票）"
        )
    required = bool(rail_state.get("required", need_ticket))
    if not required:
        return "- 铁路：本次无需铁路"
    if rail_state.get("queried"):
        return "- 铁路：本次需要查票，结果见「车票建议」一节"
    if rail_state.get("reused"):
        return "- 铁路：需要铁路，本轮沿用上次查询结果（余票未刷新）"
    return "- 铁路：需要铁路，本轮未查票，车次与时刻以现有依据为准"


def demand_summary_markdown(
    *,
    origin: str | None,
    destination: str | None,
    date_text: str | None,
    days: int | None,
    party: int | None,
    lodging_state: dict[str, Any] | None,
    need_ticket: bool,
    extra_notes: list[str] | None = None,
    rail_state: dict[str, Any] | None = None,
) -> str:
    """「需求摘要」的确定性正文：只写已确认的结构化状态，不猜。

    模型原文里的人数、住宿口径常与结构化状态不一致（例如人数已知 2 人，
    正文却写「按 1 人估算」）。这里整节替换，口径唯一，不再叠加原段落。

    extra_notes 只用于保留降级/未通过校验这类说明，不能用来塞模型的其他说法。
    rail_state 用来区分「复用旧票」与「真的不需要铁路」，为 None 时退回旧两态。
    """
    route = "未定"
    if origin and destination:
        route = f"{origin} → {destination}"
    elif destination:
        route = f"目的地：{destination}"
    lines = [f"- 行程：{route}", f"- {_days_text(date_text, days)}"]
    lines.append(f"- 人数：{party} 人" if isinstance(party, int) and party > 0 else "- 人数：未定")
    state = lodging_state or {}
    precision = str(state.get("precision") or "")
    text = str(state.get("text") or "").strip()
    if precision == "exact":
        lines.append("- 住宿：已给具体酒店")
    elif precision == "area" or (state.get("scope") == "area"):
        lines.append(f"- 住宿：按{text or '给定区域'}参考规划（酒店未确认）")
    elif text:
        lines.append(f"- 住宿：{text}（未定位）")
    else:
        lines.append("- 住宿：未定位")
    lines.append(_rail_line(need_ticket=need_ticket, rail_state=rail_state))
    for note in extra_notes or []:
        line = str(note or "").strip()
        if line:
            lines.append(line if line.startswith("-") else f"- {line}")
    return "\n".join(lines)


# 降级/未通过的说明必须保留：结构化的摘要换掉模型原文后，
# 「为什么这版是降级的」只有这几句话还在正文里。
_DEGRADED_NOTE = re.compile(r"未通过校验|已降级|降级说明|时间已用完|超时")


def degraded_note_lines(body: str) -> list[str]:
    """从原「需求摘要」里挑出降级说明行，其它模型说法一律不保留。"""
    notes: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if line and _DEGRADED_NOTE.search(line):
            notes.append(line)
    return notes


def _pending_lines(pending_items: list[dict] | None) -> list[str]:
    """待用户确认的清单：只来自结构化 pending，没有就整段不写。"""
    lines: list[str] = []
    for item in pending_items or []:
        message = str((item or {}).get("message") or "").strip()
        if message:
            lines.append(f"- {message}")
    return lines


def _unverified_lines(
    *,
    plan_basis: dict | None,
    rail_window: dict | None,
    lodging_state: dict | None,
    return_pending: list[dict] | None,
    rail_state: dict[str, Any] | None = None,
) -> list[str]:
    """「未核实 / 系统待补」：由结构化状态生成，不读质量诊断。"""
    lines: list[str] = []
    basis = plan_basis or {}
    window = rail_window or {}
    options = [str(item.get("train_no") or "") for item in (return_pending or []) if item]
    options = [item for item in options if item]
    # 「返程尚未选定」只在本次行程确实需要返程铁路时才成立。自驾/单程一日游
    # 本来就没有返程车次，写这一条等于凭空造出一个待办。
    rail = rail_state or {}
    outbound_required = bool(rail.get("outbound", True)) if rail else True
    inbound_required = bool(rail.get("inbound", True)) if rail else True
    if inbound_required and basis.get("inbound") is None:
        if options:
            # 候选只是候选：写成「已锁定」会误导用户以为返程已经定了。
            lines.append(f"- 返程尚未选定，候选：{'、'.join(options)}（未选定，需在修订中指定）")
        else:
            lines.append("- 返程尚未选定：需要补充返程时间倾向后再定车次")
    if (
        outbound_required
        and basis.get("outbound") is None
        and (window.get("outbound") or {}).get("state") == "unknown"
    ):
        lines.append("- 去程车次尚未确定，首日最早开始时间未知")
    inbound_window = window.get("inbound") or {}
    if inbound_window.get("state") == "unknown" and basis.get("inbound") is not None:
        lines.append("- 返程车次缺少可用的出发时刻，末日最晚结束时间未知")
    state = lodging_state or {}
    if state.get("scope") == "area" and not state.get("poi_id"):
        lines.append("- 住宿只到区域，出发与返回点按该区域参考，酒店未确认")
    lines.append("- 抵离时刻来自车次事实；接驳与进站缓冲是规划假设，实际请留出余量")
    return lines


_CONFLICT_LINE = re.compile(r"待确认|待补充|尽早锁定|已锁定|已选定")


def cautions_markdown(
    model_body: str,
    *,
    pending_items: list[dict] | None = None,
    plan_basis: dict | None = None,
    rail_window: dict | None = None,
    lodging_state: dict | None = None,
    return_pending: list[dict] | None = None,
    rail_state: dict[str, Any] | None = None,
) -> str:
    """「注意事项」的确定性正文：待确认清单 + 未核实项 + 保留模型的可保留建议。

    与状态矛盾的行（把候选写成已定、旧的人数/住宿待确认）直接丢掉，
    模型其余提示（交通、饮食）保留，但不能覆盖上面两段确定内容。
    rail_state 决定「返程尚未选定」这类提示要不要出现；为 None 时保持旧行为。
    """
    kept: list[str] = []
    for raw in (model_body or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _CONFLICT_LINE.search(line):
            continue
        kept.append(line)
    blocks: list[str] = []
    pending = _pending_lines(pending_items)
    if pending:
        blocks.append("### 待你确认\n" + "\n".join(pending))
    unverified = _unverified_lines(
        plan_basis=plan_basis,
        rail_window=rail_window,
        lodging_state=lodging_state,
        return_pending=return_pending,
        rail_state=rail_state,
    )
    if unverified:
        blocks.append("### 未核实 / 系统待补\n" + "\n".join(unverified))
    if kept:
        blocks.append("\n".join(kept))
    return "\n\n".join(blocks).strip()


def _evidence_links_block(evidence_links: Any) -> str:
    """证据链接只保留能通过白名单校验的。safe_evidence_url 缺失时一律不给链接。"""
    if _safe_evidence_url is None or not evidence_links:
        return ""
    allowed: list[str] = []
    for raw in evidence_links:
        try:
            safe = _safe_evidence_url(raw, _evidence_hosts())
        except Exception:  # pragma: no cover - 校验器自身异常按「不可信」处理
            safe = None
        if safe and safe not in allowed:
            allowed.append(safe)
    return "证据链接：" + " ".join(allowed) if allowed else ""


def _evidence_hosts() -> set[str]:
    """来源白名单复用 planner/sources.json；读不到就给空集合（等于不放行任何链接）。"""
    try:
        from planner.travel_search import ALLOWED_HOSTS
    except Exception:  # pragma: no cover - 只可能发生在模块被替换时
        return set()
    return set(ALLOWED_HOSTS or ())


def whitelist_summary_block(
    whitelist: dict[str, Any] | None,
    *,
    evidence_links: Any = None,
) -> str:
    """把结构化行程摊成给模型看的一段摘要，取代原来整段 itinerary_markdown。

    原实现把 map_context 原文塞给模型，里面既有行程表又有证据链接和各类未核实
    说明，模型很容易顺手扩写景点。这里只给白名单事实，并明确禁止新增景点。
    """
    data = whitelist if isinstance(whitelist, dict) else {}
    days = [day for day in (data.get("days") or []) if isinstance(day, dict)]
    lines: list[str] = []
    if not whitelist:
        # 没拿到结构化行程就拿不出一份可信清单。此时宁可让模型以「行程表」为准，
        # 也不能凭空给它一份「当天没有景点」的假事实。
        return "（景点检索已完成，但未提供结构化行程清单。景点建议以「行程表」章节为准，不得新增未列出的景点。）"
    if days:
        for day in days:
            poi_order = [str(name) for name in (day.get("poi_order") or []) if name]
            head = f"第{day.get('day')}天"
            if day.get("date"):
                head += f" {day['date']}"
            lines.append(head + "：" + (" → ".join(poi_order) if poi_order else "无景点安排"))
    else:
        lines.append("结构化行程为空：没有任何已排入的景点。")
    unlocated_names = [str(name) for name in (data.get("unlocated_names") or set()) if name]
    if unlocated_names:
        lines.append("未能定位：" + "、".join(unlocated_names))
    # 必去项排不进当天时，plan["conflicts"] 会带 name + reason；这是「为什么空了一天」
    # 的唯一结构化事实，必须写进摘要，否则模型看到空行程会自己补一个景点。
    # 「必去但未能安排」自带未安排语义，不会把冲突名当成已排入景点。
    blocked: list[str] = []
    seen_blocked: set[str] = set()
    for row in (data.get("conflicts") or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in seen_blocked:
            continue
        seen_blocked.add(name)
        reason = str(row.get("reason") or "").strip()
        blocked.append(f"{name}（{reason}）" if reason else name)
    for name in (data.get("must_visit_names") or set()):
        text = str(name)
        if text and text not in seen_blocked:
            seen_blocked.add(text)
            blocked.append(text)
    if blocked:
        lines.append("必去但未能安排：" + "、".join(blocked))
    unselected = [str(name) for name in (data.get("unselected_names") or set()) if name]
    if unselected:
        lines.append("仅候选、未排入行程：" + "、".join(unselected))
    durations = sorted(int(value) for value in (data.get("durations") or set()) if isinstance(value, int))
    if durations:
        lines.append("已知交通耗时（参考）：" + "、".join(f"{value}分钟" for value in durations))
    if data.get("date_unknown"):
        lines.append("出行日期未确定：不要写具体日期。")
    evidence = _evidence_links_block(evidence_links)
    if evidence:
        lines.append(evidence)
    lines.append("以上为本次唯一可写的景点清单；不得新增未列出的景点，也不得把未定位或仅候选的地点写成已排入行程。")
    return "\n".join(lines)


# 各降级 reason 的固定措辞。刻意不含「没有生成」这类会被当成正文断言的字样：
# 降级态只交代「地图暂缺」，不能对 HTML 是否落盘下确定结论。
_DEGRADED_MAP_TEXT = {
    "skipped": "（用户明确不要路线地图。景点建议可简要文字，不要声称已生成地图。）",
    "timeout": (
        "（map_agent 已调度但超时，路线地图可能未写完。"
        "景点建议请说明检索超时/地图暂缺，禁止编造具体未确认的景点细节。）"
    ),
    "incomplete": (
        "（map_agent 已调度但路线地图 HTML 未确认生成。正文即使有内容也不得当作本次景点方案。"
        "景点建议请说明地图暂缺，禁止编造具体景点细节。）"
    ),
}
_DEGRADED_MAP_DEFAULT = "（本应检索景点但未调度 map_agent。请标注暂缺，不要编造具体景点细节。）"


def _honest_lines(body: str) -> list[str]:
    """从 map agent 原文里只摘录「如实交代未安排/未定位」的否定性行。

    降级态不能把整段正文交给模型，但排程的冲突说明（必去项为什么没排进去）是
    唯一能解释「为什么空了一天」的结构化事实，丢了它模型反而会自己补一个景点。
    用 _HONEST_MARKERS 做白名单：只有命中否定/未确认语义的行才保留；即便命中，
    只要这一行还夹着 URL 或具体分钟数（说明是正文混进来了）也整行丢掉，避免
    顺手把「颐和园、步行25分钟」这类具体景点细节泄漏给模型。
    """
    lines: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or not any(marker in line for marker in _HONEST_MARKERS):
            continue
        if "http://" in line or "https://" in line or "分钟" in line:
            continue
        lines.append(line)
    return lines


def _degraded_map_text(map_reason: MapState, body: str, *, plan_ready: bool) -> str:
    """降级 reason 的统一出口：固定措辞 + （仅 plan_ready 时）诚实行摘录。

    plan_ready=False 时完全不碰 body；plan_ready=True 时把「必去项无法安排X」这类
    否定性说明附在措辞后，让模型如实交代冲突，而不是把 body 当景点方案扩写。
    """
    base = _DEGRADED_MAP_TEXT.get(map_reason, _DEGRADED_MAP_DEFAULT)
    if not plan_ready:
        return base
    honest = _honest_lines(body)
    if not honest:
        return base
    return (
        base
        + "\n排程如实交代的未安排/未定位项（只能照此说明未能安排，不得当作已排入景点）："
        + "\n"
        + "\n".join(honest)
    )


def map_summary_block(
    map_reason: MapState,
    map_context: str,
    *,
    plan_ready: bool = False,
    whitelist: dict[str, Any] | None = None,
    evidence_links: Any = None,
    itinerary: dict[str, Any] | None = None,
    unlocated: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> str:
    """规范行程优先。没有可核验的结构化行程时，绝不把 map_context 原文交给模型。

    map_context 原文里既有行程表又有证据链接和自由文本（景点名、顺序、耗时），
    没有 plan_ready 时这些都不是已确认事实，模型照着写就会编造具体方案。
    因此：
    - plan_ready=False：一律忽略 body，只返回固定占位/降级措辞；
    - plan_ready=True + ok：只走 whitelist_summary_block（结构化白名单），不返回原始 body；
    - plan_ready=True + 降级 reason：固定措辞，外加从 body 摘录的否定性冲突行。
    """
    if not plan_ready:
        if map_reason == "ok":
            # 有 HTML 但没有结构化行程：只说文件可能已生成，不描述任何景点细节。
            return (
                "路线地图文件可能已生成，但本轮没有可核验的结构化行程。"
                "景点、游览顺序、距离和交通耗时不能作为已确认事实。"
            )
        return _degraded_map_text(map_reason, map_context, plan_ready=False)
    # plan_ready=True：有结构化行程，只能用白名单摘要；原文 body 永远不交给模型。
    # 不设 body 非空门槛：即使 map_context 为空，只要给了 whitelist/itinerary 也要走
    # 白名单摘要；连白名单都没有时 whitelist_summary_block(None) 自带「未提供结构化
    # 行程清单」的保守说明，绝不会回落到原始 body。
    if map_reason == "ok":
        facts = whitelist
        if facts is None and isinstance(itinerary, dict):
            facts = itinerary_whitelist(itinerary, unlocated=unlocated, candidates=candidates)
        return whitelist_summary_block(facts, evidence_links=evidence_links)
    return _degraded_map_text(map_reason, map_context, plan_ready=True)


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


def reusable_itinerary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """旧快照没有结构化 days 时，不能把 HTML 当成已核实行程。"""
    if not isinstance(snapshot, dict):
        return None
    plan = snapshot.get("itinerary")
    if isinstance(plan, dict) and plan.get("days"):
        return plan
    return None


SNAPSHOT_VERSION = 4

_CLOCK_FIELDS = ("depart", "arrive", "depart_date", "arrive_date", "lishi", "arrive_date_source")


def _clock_index(records: Any) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    """用旧快照里的票务原文重跑当前解析器，按（车次/日期/座席/出发站/到达站）取回抵离时刻。

    旧版本解析器把出发到达时间丢了，但原文还在。能补就补，补不出来保持 None。

    文本格式的车次行里没有日期，解析出来的 date 是空串；这时用查询参数上的日期兜底
    （那次查询就是按这个日期查的），否则和快照里的事实对不上号。

    键必须带上出发站和到达站：同一趟车在不同车站的时刻不同（例如 G3251 在新乡南
    11:04 开、在新乡东 10:46 开），只按车次+座席匹配会把两站的时刻串到一起。
    """
    from planner.ticket_facts import parse_ticket_payload

    index: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("tool") or "") != "get-tickets":
            continue
        args = record.get("arguments") or {}
        asked_date = ""
        if isinstance(args, dict):
            for key in ("date", "departDate", "start_date", "trainDate"):
                if args.get(key):
                    asked_date = str(args[key])
                    break
        facts, _diag = parse_ticket_payload(
            record.get("payload"),
            source_id=str(record.get("id") or ""),
            queried_at=str(record.get("queried_at") or ""),
            expect_date=None,
            origin=None,
            destination=None,
        )
        for fact in facts:
            value = fact.get("value") or {}
            clock = {field: value.get(field) for field in _CLOCK_FIELDS if value.get(field)}
            if not clock:
                continue
            day = str(value.get("date") or "") or asked_date
            train_no = str(value.get("train_no") or "")
            seat = str(value.get("seat") or "")
            key = (
                train_no,
                day,
                seat,
                str(value.get("origin") or ""),
                str(value.get("destination") or ""),
            )
            index[key] = clock
            # 日期对不上时还有一层兜底：同一车次同一座席、同一区间，时刻是唯一的。
            index.setdefault((train_no, "", seat, key[3], key[4]), clock)
    return index


def _clock_for(
    index: dict[tuple[str, str, str, str, str], dict[str, Any]],
    value: dict[str, Any],
) -> dict[str, Any] | None:
    """按「先带站点、再放宽日期、最后才忽略站点」的顺序取旧快照里的抵离时刻。

    旧事实通常带出发站/到达站，先用区间键命中；只有确实没有站点信息时，才退回到
    只按车次+座席匹配（这种模糊匹配可能串站，属于最后的兜底）。
    """
    train_no = str(value.get("train_no") or "")
    day = str(value.get("date") or "")
    seat = str(value.get("seat") or "")
    origin = str(value.get("origin") or "")
    dest = str(value.get("destination") or "")
    for key in (
        (train_no, day, seat, origin, dest),
        (train_no, "", seat, origin, dest),
    ):
        clock = index.get(key)
        if clock:
            return clock
    if origin or dest:
        # 事实带了站点却在区间键里找不到：宁可不补，也不要拿别的站的时刻冒名顶替。
        return None
    for key in ((train_no, day, seat, "", ""), (train_no, "", seat, "", "")):
        clock = index.get(key)
        if clock:
            return clock
    return None


def load_snapshot_v4(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """把任意版本的快照归一化成当前口径。旧版本差异集中在这里处理。

    v3 及更早缺三样东西：车次抵离时刻、`request_context`、适用性标记。
    三者都不能猜：时刻从原文重解析，补不出来就留空；确认状态按「本轮未确认」；
    适用性标成 None（未知），质量侧据此算 unverified，而不是当成已核实。
    """
    if not isinstance(raw, dict):
        return None
    snapshot = dict(raw)
    try:
        version = int(snapshot.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    snapshot["schema_version"] = SNAPSHOT_VERSION
    snapshot.setdefault("request_context", {})
    if version >= SNAPSHOT_VERSION:
        # 文件格式已是当前版本，不代表行程已按当前口径重建。是否需要复核由持久标记
        # itinerary_needs_recheck 决定；v4 快照没写这个键时默认是当前口径（False）。
        snapshot["legacy"] = False
        snapshot.setdefault("itinerary_needs_recheck", False)
        return snapshot

    snapshot["legacy"] = True
    # 旧口径的行程在真正按当前规则重建前持续保持「待复核」。
    snapshot["itinerary_needs_recheck"] = True
    index = _clock_index(snapshot.get("ticket_raw"))
    filled = 0
    unresolved = 0
    facts: list[dict] = []
    for fact in snapshot.get("ticket_facts") or []:
        if not isinstance(fact, dict):
            continue
        item = dict(fact)
        value = dict(item.get("value") or {})
        clock = _clock_for(index, value)
        if clock:
            for field, got in clock.items():
                if got and not value.get(field):
                    value[field] = got
            filled += 1
        elif not (value.get("depart") and value.get("arrive")):
            unresolved += 1
        if "applied" not in value:
            value["applied"] = None
        item["value"] = value
        facts.append(item)
    snapshot["ticket_facts"] = facts
    snapshot["legacy_filled"] = filled
    snapshot["legacy_unresolved"] = unresolved
    return snapshot


def load_snapshot(plan_id: str) -> dict[str, Any] | None:
    """没有对应文件则返回 None（前端应提示重新规划）。"""
    path = snapshot_path(plan_id)
    if not path.is_file():
        return None
    return load_snapshot_v4(json.loads(path.read_text(encoding="utf-8")))


def save_plan(plan_id: str, query: str, final_answer: str) -> tuple[str, str]:
    """把方案写成带 YAML 头的 Markdown。返回 (本地路径, 给浏览器的 /results/… URL)。"""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = f"旅游规划-{stamp}-{plan_id}.md"
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


def summary_gate_issues(text: str, ticket_reason: TicketState) -> list[str]:
    """成文闸门：六章节必须在；无可靠票务时车票建议里不能有车次号。"""
    issues: list[str] = []
    missing = missing_sections(text)
    if missing:
        issues.append("缺少章节: " + "、".join(missing))
    if ticket_reason not in ("ok", "reused"):
        try:
            assert_no_fabricated_trains(text)
        except AssertionError as exc:
            issues.append(str(exc))
    return issues


# 「按 1 人」这类说法在人数已知时就是矛盾。刻意不匹配「每人」「人均」「单人票价」：
# 那些是单价口径，跟「按几个人估算总额」不是一回事，误伤会让正常正文反复被打回。
_PARTY_ONE_HINTS = (
    "按 1 人估算",
    "按1人估算",
    "按 1 人",
    "按1人",
    "1 人估算",
    "1人估算",
    "按一人估算",
    "按一人",
)
_CONFIRMED_LODGING_HINTS = ("已确认酒店", "已入住", "已订好酒店", "已预订酒店")
_LOCKED_RETURN = re.compile(r"(已锁定|已选定|已经选定|已确定)[^。\n]*?([GDCZTK]\d{1,4})")


def state_consistency_issues(answer: str, state: dict[str, Any] | None) -> list[str]:
    """正文与结构化状态的一致性校验（只看确定性可判的矛盾）。

    state 键：party、lodging_state、plan_basis、need_ticket、date、days、rail_state。
    与 summary_gate_issues 互补：那边管「章节齐不齐、车次号有没有依据」，
    这边管「同一篇正文里的人数和住宿、返程口径是否自相矛盾」。
    """
    text = answer or ""
    data = state or {}
    issues: list[str] = []
    party = data.get("party")
    if isinstance(party, int) and party > 1:
        hits = [hint for hint in _PARTY_ONE_HINTS if hint in text]
        if hits:
            issues.append(f"人数已确认为 {party} 人，正文却出现「{hits[0]}」这类按 1 人估算的说法")
    lodging = data.get("lodging_state") or {}
    precision = str(lodging.get("precision") or "")
    scope = str(lodging.get("scope") or "")
    if precision in {"area", "fallback"} or (scope == "area" and not lodging.get("poi_id")):
        hits = [hint for hint in _CONFIRMED_LODGING_HINTS if hint in text]
        if hits:
            issues.append(f"住宿只到区域、未确认具体酒店，正文却写「{hits[0]}」")
    basis = data.get("plan_basis") or {}
    # 只有本次行程确实需要返程铁路时，「返程已锁定」才算矛盾。自驾/单程本来
    # 就没有返程车次，正文里出现车次号也不会被这条拦下（仍受车票章节校验约束）。
    rail = data.get("rail_state") or {}
    inbound_required = bool(rail.get("inbound", True)) if rail else True
    if inbound_required and basis.get("inbound") is None:
        locked = _LOCKED_RETURN.search(text)
        if locked:
            issues.append(f"返程尚未选定，正文却把 {locked.group(2)} 写成{locked.group(1)}")
        elif "尽早锁定" in text:
            issues.append("返程尚未选定，正文却写「尽早锁定」某趟返程，应改成候选表述")
    return issues


# ---------------------------------------------------------------------------
# B5：结构化行程白名单 → 景点建议/行程表的事实闸门
#
# 为什么需要：落盘前「车票建议」「预算」「行程表」都会被结构化数据整段替换，
# 只有「景点建议」一直由模型自由发挥。模型可以在那里加一个排程里根本没有的
# 景点、把同一天的顺序写乱、把参考耗时写成实测。这里把结构化行程摊成白名单，
# 再用它反查正文；命中就给 issue，交给上层重试或确定性降级。
#
# 刻意不做的事：
# - 不校验距离（结构化 legs 里没有 distance_meters，无从判断，方案已明确留空）。
# - 不为了「让正文过闸」放宽成「正文里有数字就算过」。
# ---------------------------------------------------------------------------

# itinerary 为空时「景点建议」的占位文案。degraded_plan 与降级替换共用同一句，
# 保证两类降级产物的口吻一致，也不编造任何景点。
_SIGHTS_EMPTY_TEXT = "景点检索结果未写入成文。请重新规划，或查看已生成的地图文件。"

# 这些词说明该行是在如实交代「未排入/未定位」，不是把条目当成已排入景点写。
_HONEST_MARKERS = (
    "未能定位",
    "未定位",
    "未找到",
    "未安排",
    "无法安排",
    "未排入",
    "未选用",
    "备选",
    "候选",
    "待检索",
    "暂缺",
)

# 把站名/住宿锚点写成「要去的地方」才算越界；「从北京西站出发」这类交代不算。
_VISIT_CONTEXT = re.compile(r"游览|参观|逛|打卡|游玩|去")
_VERIFIED_TIME_WORDS = ("实测", "实际耗时", "已实测", "已核实", "已确认", "确认无误")
# 与耗时有关的词：只有「已核实/实测」和这些词出现在同一行，才算把参考耗时说成确定值。
_TIME_CUES = ("分钟", "小时", "车程", "步行", "耗时", "用时", "路程", "公里", "km")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CN_DAY_DATE_RE = re.compile(r"第\s*\d+\s*天[^\n]{0,16}?\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
_DAY_MARK_RE = re.compile(r"第\s*(\d+)\s*天")
_MINUTES_RE = re.compile(r"(\d+)\s*分钟")


def _clock_minutes(value: Any) -> int | None:
    """把 "09:30" 解析成当天分钟数；解析不了返回 None（"时刻未核实" 就是 None）。"""
    match = _HHMM_RE.match(str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _span_minutes(start: Any, end: Any) -> int | None:
    begin, finish = _clock_minutes(start), _clock_minutes(end)
    if begin is None or finish is None or finish < begin:
        return None
    return finish - begin


def _duration_minutes(seconds: Any) -> int | None:
    """legs 只给秒数。取不到正数就是 unknown，绝不补 0。"""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if seconds <= 0:
        return None
    return int(round(seconds / 60))


def _day_window_text(window: Any) -> str:
    """当天窗口写成一句话；未知就说未知，不回退成默认时间冒充已核实。"""
    data = window if isinstance(window, dict) else {}
    state = data.get("state")
    if state == "unavailable":
        return str(data.get("reason") or "当天不可用")
    if state == "unknown":
        return "抵离时刻未核实，当天可用时段待定"
    start, end = data.get("start"), data.get("end")
    if start is None or end is None:
        return ""
    return f"{int(start) // 60:02d}:{int(start) % 60:02d}–{int(end) // 60:02d}:{int(end) % 60:02d}"


def _empty_whitelist() -> dict[str, Any]:
    return {
        "date_unknown": True,
        "days": [],
        "sight_names": set(),
        "lodging_names": set(),
        "station_names": set(),
        "unselected_names": set(),
        "unlocated_names": set(),
        "must_visit_names": set(),
        # 必去项因闭馆/未定位等原因排不进当天时，plan["conflicts"] 是唯一能
        # 告诉模型「为什么空了一天」的结构化事实；收进白名单并在摘要里渲染，
        # 免得模型看到空行程后自己补一个景点。
        "conflicts": [],
        "durations": set(),
        # 游览段 start–end 之差。任务口径把它算作「已知耗时」，检查③用它放过正文里
        # 写「参观用了 120 分钟」这类合理说法；没有它会把正常正文全判成超报。
        "visit_minutes": set(),
    }


def _entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("poi_name") or "").strip()
    if isinstance(entry, str):
        return entry.strip()
    return ""


def itinerary_whitelist(
    itinerary: dict[str, Any] | None,
    *,
    unlocated: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    lodging: Any = None,
) -> dict[str, Any]:
    """把结构化行程摊成「正文可以怎么写」的白名单。

    只读 itinerary / unlocated / candidates，不联网、不调模型，纯函数，方便单测。
    itinerary 为空时返回 date_unknown=True 的空白名单：此时没有可比对的依据，
    闸门只能靠「正文有没有把不确定写成确定」来判断。
    """
    whitelist = _empty_whitelist()
    sight_names: set[str] = set()
    lodging_names: set[str] = set()
    station_names: set[str] = set()
    durations: set[int] = set()
    visit_minutes: set[int] = set()
    days: list[dict[str, Any]] = []
    date_known = False

    plan = itinerary if isinstance(itinerary, dict) else {}
    raw_days = [day for day in (plan.get("days") or []) if isinstance(day, dict)]
    for index, day in enumerate(raw_days, start=1):
        try:
            day_no = int(day.get("day") or index)
        except (TypeError, ValueError):
            day_no = index
        date_text = str(day.get("date") or "").strip() or None
        if date_text:
            date_known = True
        items = [item for item in (day.get("items") or []) if isinstance(item, dict)]
        names_by_id: dict[str, str] = {}
        for item in items:
            poi_id = str(item.get("poi_id") or item.get("name") or "").strip()
            name = str(item.get("name") or "").strip()
            if poi_id and name:
                names_by_id[poi_id] = name
        day_durations: set[int] = set()
        day_visit_minutes: set[int] = set()
        poi_order: list[str] = []
        for item in items:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            role = str(item.get("role") or "")
            if role == "visit":
                sight_names.add(name)
                if name not in poi_order:
                    poi_order.append(name)
            elif role in ("depart", "return"):
                # 住宿锚点会在 items 里带 lodging_kind/precision/poi_name；
                # 没有这些标记的 depart/return 名按站点处理（车站名不能当景点推荐）。
                if item.get("lodging_kind") or item.get("precision") or item.get("poi_name"):
                    lodging_names.add(name)
                else:
                    station_names.add(name)
            span = _span_minutes(item.get("start"), item.get("end"))
            if span:
                visit_minutes.add(span)
                day_visit_minutes.add(span)
        legs_rows: list[dict[str, Any]] = []
        for leg in day.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            minutes = _duration_minutes(leg.get("duration_seconds"))
            origin_name = names_by_id.get(str(leg.get("origin_id") or ""), "")
            dest_name = names_by_id.get(str(leg.get("dest_id") or ""), "")
            if minutes is not None:
                durations.add(minutes)
                day_durations.add(minutes)
            legs_rows.append(
                {
                    "from": origin_name,
                    "to": dest_name,
                    "duration_minutes": minutes,
                    # legs 只有 reference/unknown：取得路线结果也只能算参考。
                    "status": "reference" if minutes is not None else "unknown",
                }
            )
        days.append(
            {
                "day": day_no,
                "date": date_text,
                "poi_order": poi_order,
                "legs": legs_rows,
                # 当天可引用的耗时（边 + 游览段）。检查③按天比对，避免把别的天
                # 的数字当成这一天的依据。
                "durations": sorted(day_durations),
                "visit_minutes": sorted(day_visit_minutes),
            }
        )

    candidate_names = [_entry_name(entry) for entry in (candidates or [])]
    scheduled = sight_names | lodging_names | station_names
    unselected = {name for name in candidate_names if name and name not in scheduled}

    unlocated_entries = [entry for entry in (unlocated or []) if isinstance(entry, dict)]
    unlocated_names = {_entry_name(entry) for entry in unlocated_entries if _entry_name(entry)}
    must_visit_names = {
        _entry_name(entry)
        for entry in unlocated_entries
        if str(entry.get("kind") or "") == "must_visit" and _entry_name(entry)
    }

    lodging_name = _entry_name(lodging)
    if lodging_name:
        lodging_names.add(lodging_name)

    # 必去项排不进当天时，plan["conflicts"] 带 name + reason；这是「为什么空了一天」
    # 的唯一结构化事实。白名单带上它，whitelist_summary_block 才能渲染「必去但未能
    # 安排」，否则模型看见空行程会自己补一个景点。只保留 name/reason，不复制其它键。
    conflicts: list[dict[str, str]] = []
    for row in (plan.get("conflicts") or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        conflicts.append({"name": name, "reason": str(row.get("reason") or "").strip()})

    whitelist.update(
        {
            "date_unknown": not date_known,
            "days": days,
            "sight_names": sight_names,
            "lodging_names": lodging_names,
            "station_names": station_names,
            "unselected_names": unselected,
            "unlocated_names": unlocated_names,
            "must_visit_names": must_visit_names,
            "conflicts": conflicts,
            "durations": durations,
            "visit_minutes": visit_minutes,
        }
    )
    return whitelist


def _name_hits_in_section(
    section: str,
    forbidden: set[str],
    allowed: set[str],
    *,
    station_visit_only: bool = False,
) -> list[str]:
    """按行找「多写出来的景点名」。

    两处刻意的宽容，都是为了不误伤正常正文：
    - 该行已经写明「未能定位 / 备选 / 未安排」时，提到名字不算把它写成已排入；
    - 候选「天坛」而行程排的是「天坛公园」时，正文写「天坛公园」不算多写。
    站点/住宿锚点只在被写成「要去游览的地方」时才算越界（「从北京西站出发」不算）。
    """
    if not section or not forbidden:
        return []
    hits: list[str] = []
    for line in section.splitlines():
        raw = line.strip()
        if not raw or any(marker in raw for marker in _HONEST_MARKERS):
            continue
        for name in sorted(forbidden):
            if not name or name not in raw or name in hits:
                continue
            if any(name != other and name in other for other in allowed if other):
                continue
            if station_visit_only and not _VISIT_CONTEXT.search(raw):
                continue
            hits.append(name)
    return hits


def _day_segments(section: str) -> list[tuple[int, str]]:
    """把「景点建议」按「第N天」切段，返回 [(天号, 该段正文)]。"""
    marks = list(_DAY_MARK_RE.finditer(section or ""))
    segments: list[tuple[int, str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(section)
        segments.append((int(mark.group(1)), section[mark.end(): end]))
    return segments


def _poi_order_violated(section: str, whitelist: dict[str, Any]) -> bool:
    """同一天里景点的出现顺序必须是 poi_order 的子序列，且天号不能倒退。"""
    days = [day for day in (whitelist.get("days") or []) if isinstance(day, dict)]
    if not days:
        return False
    order_by_day: dict[int, list[str]] = {}
    for day in days:
        try:
            order_by_day[int(day.get("day") or 0)] = [str(name) for name in (day.get("poi_order") or []) if name]
        except (TypeError, ValueError):
            continue
    segments = _day_segments(section)
    previous_day = 0
    for day_no, chunk in segments:
        if day_no < previous_day:
            return True
        previous_day = day_no
        order = order_by_day.get(day_no)
        if not order:
            continue
        appeared = [(chunk.find(name), name) for name in order if name in chunk]
        appeared.sort()
        ranks = [order.index(name) for _pos, name in appeared]
        if ranks != sorted(ranks):
            return True
    return False


def _allowed_minutes(whitelist: dict[str, Any], day_no: int | None) -> set[int]:
    """当天可引用的耗时；当天没有已知耗时或正文没分段时退回全局集合。"""
    global_minutes: set[int] = set()
    for value in whitelist.get("durations") or []:
        if isinstance(value, int):
            global_minutes.add(value)
    for value in whitelist.get("visit_minutes") or []:
        if isinstance(value, int):
            global_minutes.add(value)
    if day_no is None:
        return global_minutes
    for day in whitelist.get("days") or []:
        if not isinstance(day, dict):
            continue
        try:
            if int(day.get("day") or 0) != day_no:
                continue
        except (TypeError, ValueError):
            continue
        local: set[int] = set()
        for key in ("durations", "visit_minutes"):
            for value in day.get(key) or []:
                if isinstance(value, int):
                    local.add(value)
        return local or global_minutes
    return global_minutes


def _travel_time_issues(section: str, whitelist: dict[str, Any]) -> list[str]:
    """「景点建议」里写到的「N 分钟」必须能对上已知耗时。

    只扫「景点建议」：「行程表」由 itinerary_markdown 生成，头部固定含
    「午餐 60 分钟，切换 15 分钟，机动 30 分钟」，那是排程假设，不是景点耗时。
    """
    if not section:
        return []
    issues: list[str] = []
    segments = _day_segments(section)
    scopes = segments if segments else [(None, section)]
    for day_no, chunk in scopes:
        allowed = _allowed_minutes(whitelist, day_no)
        for match in _MINUTES_RE.finditer(chunk):
            minutes = int(match.group(1))
            if minutes in allowed:
                continue
            issue = f"travel_time_overclaim:{minutes}分钟"
            if issue not in issues:
                issues.append(issue)
    return issues


def _status_overclaim(section: str, whitelist: dict[str, Any]) -> bool:
    """有 reference 边时，正文不能把参考耗时写成实测/已核实。

    只在同一行里既出现「已核实/实测」这类断言、又出现耗时口径时才判，
    避免把「已确认门票需预约」这类与耗时无关的句子也算成越界。
    """
    has_reference = any(
        str(leg.get("status") or "") == "reference"
        for day in (whitelist.get("days") or [])
        if isinstance(day, dict)
        for leg in (day.get("legs") or [])
        if isinstance(leg, dict)
    )
    if not has_reference or not section:
        return False
    for line in section.splitlines():
        if not any(word in line for word in _VERIFIED_TIME_WORDS):
            continue
        if _MINUTES_RE.search(line) or any(cue in line for cue in _TIME_CUES):
            return True
    return False


def _date_overclaim(sight_section: str, itinerary_section: str) -> bool:
    """出行日期未知时，景点建议/行程表里不能凭空写具体日期。

    只看这两节：「需求摘要」的「- 日期：…」是确定性生成的，写的是用户给的
    真实日期，扫全篇会把它误判成编造日期。
    """
    target = f"{sight_section or ''}\n{itinerary_section or ''}"
    if not target.strip():
        return False
    if _ISO_DATE_RE.search(target):
        return True
    return bool(_CN_DAY_DATE_RE.search(target))


def poi_gate_issues(answer: str, whitelist: dict[str, Any], *, plan_ready: bool) -> list[str]:
    """景点建议/行程表与结构化行程的一致性校验，返回中文可读的 issue。

    四类检查：
    1. 名称：正文出现的景点级名称必须是行程里排过的；多写候选/未定位/站名 → 报错。
    2. 顺序：同一「第N天」段里景点出现顺序必须是 poi_order 的子序列。
    3. 耗时：「N 分钟」要对得上当天已知的边耗时或游览段时长。
    4. 状态与日期：reference 边不能写成实测；日期未知时不能写具体日期。

    plan_ready=False（没有结构化行程）时没有可比对的依据，名称与顺序无从判断，
    只保留 3、4 两类，并额外要求正文说清「景点待检索」。
    """
    text = answer or ""
    data = whitelist if isinstance(whitelist, dict) else {}
    sight_section = section_body(text, "景点建议")
    itinerary_section = section_body(text, "行程表")
    issues: list[str] = []

    scheduled = set()
    for key in ("sight_names", "lodging_names", "station_names"):
        scheduled.update(str(name) for name in (data.get(key) or set()) if name)

    if plan_ready:
        unscheduled = set(str(name) for name in (data.get("unselected_names") or set()) if name)
        unscheduled |= set(str(name) for name in (data.get("unlocated_names") or set()) if name)
        stations = set(str(name) for name in (data.get("station_names") or set()) if name)
        # 「行程表」是确定性生成的，站名/住宿锚点本来就在里面，不查；
        # 「景点建议」里把站名当景点推荐才越界。
        seen: list[str] = []
        for name in _name_hits_in_section(sight_section, unscheduled, scheduled):
            if name not in seen:
                seen.append(name)
        for name in _name_hits_in_section(sight_section, stations, scheduled, station_visit_only=True):
            if name not in seen:
                seen.append(name)
        for name in _name_hits_in_section(itinerary_section, unscheduled, scheduled):
            if name not in seen:
                seen.append(name)
        issues.extend(f"poi_not_in_itinerary:{name}" for name in seen)
        if _poi_order_violated(sight_section, data):
            issues.append("poi_order_mismatch")

    issues.extend(_travel_time_issues(sight_section, data))
    if _status_overclaim(sight_section, data):
        issues.append("travel_time_status_overclaim")
    if data.get("date_unknown") and _date_overclaim(sight_section, itinerary_section):
        issues.append("date_overclaim")
    if not plan_ready and sight_section and not any(hint in sight_section for hint in ("待", "未", "暂")):
        issues.append("poi_claim_without_plan")
    return issues


def sights_suggestion_markdown(
    itinerary: dict[str, Any] | None,
    *,
    unlocated: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    hotel: Any = None,
) -> str:
    """确定性生成「景点建议」正文：只写排进行程的景点，另如实交代未定位与备选。

    这个函数是闸门失败时的确定性降级产物，所以它的输出必须自己也能过闸：
    未定位/备选单独成句并带「未能定位」「备选」这类字样，不会被当成已排入景点。
    """
    plan = itinerary if isinstance(itinerary, dict) else {}
    days = [day for day in (plan.get("days") or []) if isinstance(day, dict)]
    if not days:
        return _SIGHTS_EMPTY_TEXT
    lines: list[str] = []
    scheduled: set[str] = set()
    for index, day in enumerate(days, start=1):
        try:
            day_no = int(day.get("day") or index)
        except (TypeError, ValueError):
            day_no = index
        date_text = str(day.get("date") or "").strip() or "日期待定"
        window_text = _day_window_text(day.get("window"))
        head = f"- 第{day_no}天 {date_text}"
        if window_text:
            head += f"（{window_text}）"
        visits = [
            item
            for item in (day.get("items") or [])
            if isinstance(item, dict) and str(item.get("role") or "") == "visit" and item.get("name")
        ]
        if not visits:
            lines.append(head + "：当天没有排入景点。")
            continue
        bits: list[str] = []
        for item in visits:
            name = str(item["name"]).strip()
            scheduled.add(name)
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            if start and end:
                bits.append(f"{name} {start}–{end}")
            else:
                bits.append(f"{name}（时刻未核实）")
        lines.append(head + "：" + " → ".join(bits))
        density = day.get("density") or {}
        target_min = density.get("target_count_min")
        target_max = density.get("target_count_max")
        if isinstance(target_min, int) and isinstance(target_max, int):
            actual = len(visits)
            pace = {"relaxed": "轻松", "balanced": "适中", "compact": "紧凑"}.get(str(density.get("pace") or ""), "当前")
            if actual < target_min:
                reason = str(density.get("reason") or "当天可行性条件限制了继续增加景点")
                lines.append(f"  {pace}模式目标 {target_min}–{target_max} 个景点，实际安排 {actual} 个；{reason}。")
            elif actual > target_max:
                lines.append(f"  {pace}模式参考上限 {target_max} 个景点，实际安排 {actual} 个；安排偏紧。")
        # 排程备注里含「午餐 60 分钟」这类假设，属于「行程表」口径，不搬进景点建议。
        notes = [
            str(note).strip()
            for note in (day.get("notes") or [])
            if str(note).strip() and "分钟" not in str(note)
        ]
        if notes:
            lines.append("  " + "；".join(notes))

    unlocated_names = [_entry_name(entry) for entry in (unlocated or []) if _entry_name(entry)]
    if unlocated_names:
        lines.append("未能定位：" + "、".join(unlocated_names) + "（检索未确认位置，未排入行程）")
    candidate_names = [
        _entry_name(entry)
        for entry in (candidates or [])
        if _entry_name(entry) and _entry_name(entry) not in scheduled
    ]
    if candidate_names:
        lines.append("备选（未排入行程）：" + "、".join(candidate_names))
    hotel_name = _entry_name(hotel)
    if hotel_name:
        lines.append(f"住宿参考：{hotel_name}")
    return "\n".join(lines).strip()


def degraded_plan(
    ticket_reason: TicketState,
    issues: list[str],
    *,
    itinerary: dict[str, Any] | None = None,
    unlocated: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> str:
    """校验两次仍失败时的成文。六章节都在，且不写车次号。"""
    ticket_line = {
        "skipped": "本次未查票（无需铁路）。",
        "unavailable": "车票服务不可用，暂无可靠票务数据。",
        "missed": "本应查票但未调度，未查票。",
        "timeout": "查票超时，暂无可靠票务数据。",
        "reused": "车票信息沿用上次查询。",
        "ok": "票务原文未写入成文，此处不转述车次。",
    }.get(ticket_reason, "车票服务不可用，暂无可靠票务数据。")
    why = TRAIN_NUMBER_RE.sub("某车次", "；".join(issues) or "成文未通过校验")
    # 景点建议也走同一个确定性生成器：有结构化行程就如实列出已排景点，
    # 没有就用占位文案。这样降级产物不会比正常成文多出任何景点。
    sights = sights_suggestion_markdown(
        itinerary,
        unlocated=unlocated,
        candidates=candidates,
    )
    return (
        f"## 需求摘要\n汇总未通过校验（{why}），以下为降级说明，不编造车次或景点细节。\n\n"
        f"## 景点建议\n{sights}\n\n"
        f"## 车票建议\n{ticket_line}\n\n"
        "## 预算\n暂无可靠估算。\n\n"
        "## 行程表\n暂无。\n\n"
        "## 注意事项\n本次方案因校验失败已降级，请勿把未出现在检索结果中的车次当作查询结果。\n"
    )


async def stream_summary(
    *,
    llm: Any,
    query: str,
    slots: TravelSlots | None,
    map_context: str,
    ticket_context: str,
    ticket_reason: TicketState,
    map_reason: MapState = "missed",
    plan_ready: bool = False,
    previous_answer: str | None = None,
    revision_forbid_old_tickets: bool = False,
    gate_feedback: str | None = None,
    timeout_seconds: float | None = None,
    whitelist: dict[str, Any] | None = None,
    evidence_links: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式生成最终 Markdown。对外 yield token；最后一帧 _summary_done 仅给 pipeline 收全文。"""
    slots_text = (
        format_slots_for_prompt(slots, rail=ticket_reason != "skipped")
        if slots is not None
        else "（槽位未结构化确认，请根据用户原话理解）"
    )
    map_block = map_summary_block(
        map_reason,
        map_context,
        plan_ready=plan_ready,
        whitelist=whitelist,
        evidence_links=evidence_links,
    )
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
    if gate_feedback:
        parts.append(f"\n【必须改正】\n{gate_feedback}\n")
    user_msg = "\n".join(parts)
    pieces: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    stream = llm.astream(
        [
            SystemMessage(content=SUMMARY_AGENT_PROMPT),
            HumanMessage(content=user_msg),
        ]
    )
    try:
        with usage_call():
            while True:
                try:
                    # 截止覆盖首段和后续段等待；超时上下文不跨越向调用方的 yield。
                    async with asyncio.timeout_at(deadline):
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                note_llm(chunk)
                delta = message_text(getattr(chunk, "content", None))
                if not delta:
                    continue
                pieces.append(delta)
                yield {"type": "token", "content": delta}
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
    full = "".join(pieces).strip() or "本次没有生成最终结果"
    yield {"type": "_summary_done", "content": full}
