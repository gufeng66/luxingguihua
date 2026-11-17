"""
一次旅游规划的事件流（CLI 与 Web 的唯一核心入口）。

【小白怎么理解 stream_plan？】
    它是 async 生成器：一边跑一边 yield 字典事件（status / step / clarify / token / final…）。
    网页用 SSE 转发这些事件；命令行则 print。

【正常顺序】
    1. 理解需求：抽槽位；缺出发地/目的地/日期 → yield clarify 后 return
    2. 路由：要不要查票、要不要地图
    3. 连 12306 MCP（需要票时）；失败则降级
    4. 代码按规则并发启动地图 / 票务工具循环
    5. stream_summary 流式成文 → 落盘 md + 快照 JSON → yield final

【修订（带 parent_plan_id）】
    map_only        调整景点与天数等；票务字段未变时沿用快照
    ticket_and_map  改了日期/地点/出行方式，重新决定是否查票
    summary_only    只改措辞，跳过检索
    budget_only     只改预算/人数/预算口径，重算成文，不查票不查地图
    所有修订在合并槽位后检查关键字段，多日缺住宿地仍需澄清
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from planner.map_agent import map_agent
from planner.route_plan import encode_route_key, itinerary_markdown
from planner.map_fallback import (
    assemble_itinerary,
    days_from_query,
    destination_from_query,
    parse_candidates,
    render_map_html,
)
from slots import (
    TravelSlots,
    apply_soft_defaults,
    draft_pendings,
    format_slots_for_prompt,
    clarify_missing,
    input_state,
    lodging_from_query,
    lodging_route_rules,
    must_pause,
    pending_confirmations,
    should_clarify,
    slots_for_plan,
    slots_from_query,
)

from planner.async_utils import ms_since
from planner.budget import RunBudget
from planner.paths import MAPS_DIR
from planner.quality import build_quality, collect_issues, leg_counts, opening_counts, schedule_check
from planner.routing import (
    TICKET_FIELDS,
    _explicit_lodging_relocate,
    _explicit_train_selection,
    classify_revision,
    classify_ticket_parent,
    context_revision_diff,
    derive_revision_from_changes,
    diff_fields,
    map_scope,
    merge_request_context,
    merge_slots,
    resolve_map_state,
    resolve_revision_kind,
    resolve_ticket_state,
    effective_intercity_intent,
    rail_required,
    skip_ticket_reason,
)
from planner.travel_search import ALLOWED_HOSTS
from planner.urls import safe_evidence_url
from planner.summary import (
    build_trace_url,
    cautions_markdown,
    degraded_note_lines,
    degraded_plan,
    demand_summary_markdown,
    load_snapshot,
    parse_plan_id,
    replace_itinerary_section,
    replace_named_section,
    reusable_itinerary,
    rewrite_plan_ids,
    save_plan,
    save_snapshot,
    section_body,
    sights_suggestion_markdown,
    state_consistency_issues,
    summary_gate_issues,
    itinerary_whitelist,
    poi_gate_issues,
)
from planner.plans_store import record_plan
from planner.run_stats import RunStats, begin_run_stats
from planner.subruns import iter_agents
from planner.ticket_cache import close_ticket_agent
from planner.ticket_facts import (
    INBOUND,
    OUTBOUND,
    allowed_train_numbers,
    applicability_note,
    applicable_facts,
    budget_markdown,
    choose_plan_basis,
    facts_from_records,
    party_size,
    rail_window,
    revalidate_facts,
    strip_unlisted_trains,
    ticket_markdown,
    unparsed_tool_notes,
)
from planner.usage import begin_usage, current_usage

logger = logging.getLogger("travel_planner")


def _return_date(
    confirmed: TravelSlots | None,
    query: str,
    *,
    previous_query: str = "",
    expect_date: str | None = None,
    request_ctx: dict | None = None,
    query_is_current: bool = True,
) -> str | None:
    """本轮行程的返程日期；不需要返程铁路或日期不可解析时返回 None。

    返程日期不是槽位字段，而是「出发日 + 天数 - 1」派生出来的。它同时决定
    返程车次事实是否适用、票务区间是否与父方案一致，因此必须只有一处算法：
    以前这段逻辑内联在事实解析里，天数改了但复用判断仍按旧字段比对，就会
    出现「行程改成 4 天、票还是 11-09 那张」的矛盾。
    """
    if confirmed is None or not expect_date:
        return None
    required = rail_required(
        confirmed,
        query,
        previous_query=previous_query,
        request_ctx=request_ctx,
        query_is_current=query_is_current,
    )
    if not required.get("inbound"):
        return None
    span = int(confirmed.days) - 1 if (confirmed.days or 1) > 1 else 0
    try:
        return (date.fromisoformat(str(expect_date)) + timedelta(days=span)).isoformat()
    except ValueError:
        return None


def _ticket_fields_match(
    snapshot: dict[str, Any] | None,
    confirmed: TravelSlots | None,
    *,
    query: str | None = None,
    previous_query: str = "",
    request_ctx: dict | None = None,
    query_is_current: bool = True,
) -> bool:
    """本轮票务区间是否与父方案完全一致（一致才允许沿用旧票）。

    除了出发地/目的地/日期/交通方式，天数也必须相等：天数变了返程日期就变，
    旧票按旧返程日期算出的适用性已经不成立。调用方给了本轮原话时，再比一次
    两侧派生出的返程日期，把「天数没变但返程意图变了」也挡住。
    """
    old = (snapshot or {}).get("slots") or {}
    if confirmed is None:
        return False
    if not all(
        old.get(key) == getattr(confirmed, key)
        for key in ("date", "origin", "destination")
    ):
        return False
    old_days = old.get("days")
    if old_days is not None and int(old_days) != int(confirmed.days or 1):
        return False
    if query is None:
        # 调用方没给本轮原话就只按槽位比：拿不到本轮返程意图，不能替它判返程日期。
        return True
    old_ctx = (snapshot or {}).get("request_context") or {}
    for key in ("intercity_mode", "return_required"):
        if old_ctx.get(key) != (request_ctx or {}).get(key):
            return False
    old_slots = _snapshot_slots(snapshot)
    if old_slots is None:
        return False
    old_return = _return_date(
        old_slots,
        str((snapshot or {}).get("query") or ""),
        expect_date=old_slots.date,
        request_ctx=old_ctx,
    )
    new_return = _return_date(
        confirmed,
        query,
        previous_query=previous_query,
        expect_date=confirmed.date,
        request_ctx=request_ctx,
        query_is_current=query_is_current,
    )
    return old_return == new_return


def _snapshot_slots(snapshot: dict[str, Any] | None) -> TravelSlots | None:
    """读回上一版确认过的槽位；快照缺失或字段不合法时返回 None。"""
    raw = (snapshot or {}).get("slots")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return TravelSlots.model_validate(raw)
    except Exception:
        logger.exception("snapshot_slots_invalid")
        return None


async def _extract(svc: Any, query: str, llm: Any, budget: RunBudget) -> Any:
    timeout = budget.timeout_for(budget.slot)
    if timeout <= 0:
        raise TimeoutError("slot budget exhausted")
    return await asyncio.wait_for(svc.extract_slots(query, llm), timeout=timeout)


def _ticket_queried_at(records: list[dict]) -> str:
    if not records:
        return ""
    return str(records[-1].get("queried_at") or "")


def _plan_basis(
    ticket_facts: list[dict],
    query: str,
    *,
    saved: dict | None = None,
    allow_stale: bool = False,
) -> dict:
    """把适用事实收敛成排程依据。没有可用车次时如实返回 None，不拿查询事实顶替。

    saved 是上一版快照里已定的排程依据。四级优先级，按「本轮说了什么」而不是
    只比 origin 字段决定谁胜出：

    1. 本轮点了名次且该车次在当前适用事实里 → 用本轮的（origin=user_selected）。
    2. 本轮给了明确的返程时段偏好（choose_plan_basis(INBOUND) 返回 dict，
       它内部已要求返程语境 + 时段词）→ 本轮结论胜出，即使父方案已经选了返程。
    3. 本轮完全没提该方向 → 父方案已选且该车次仍在适用事实里 → 沿用父方案。
    4. 其余（含没有可用车次）→ 用本轮结论；本轮没有就是 None。

    只改景点/只改预算时本轮原话里没有点名车次，若还按「当日最早抵达」重挑，
    会把父方案已经选定的车次换掉，正文与地图跟着一起变。

    allow_stale：本轮是复用（没重查票）且父方案根本没留下票务事实时，仍按父方案
    标好的依据走。否则「上次依据 G1813、事实为空」的快照一旦复用，
    plan_basis 与 rail_window 会双双变空，行程与正文就丢掉了抵离约束。
    只有复用分支该传 True；重建分支必须让旧依据彻底失效。
    """
    applicable = applicable_facts(ticket_facts)
    if not applicable:
        if allow_stale and isinstance(saved, dict):
            stale = {
                direction: (saved.get(direction) if isinstance(saved.get(direction), dict) else None)
                for direction in (OUTBOUND, INBOUND)
            }
            if any(stale.values()):
                return stale
        return {OUTBOUND: None, INBOUND: None}
    fresh = {
        OUTBOUND: choose_plan_basis(applicable, query=query, direction=OUTBOUND),
        INBOUND: choose_plan_basis(applicable, query=query, direction=INBOUND),
    }
    keep = saved if isinstance(saved, dict) else {}
    merged: dict = {}
    for direction in (OUTBOUND, INBOUND):
        basis = fresh.get(direction)
        previous = keep.get(direction)
        previous_ok = False
        refreshed_previous = None
        if isinstance(previous, dict):
            def _same_fact(field: str, saved_value: object, fact_value: object) -> bool:
                # 旧快照未保存站点/日期时只用已知字段匹配，兼容历史依据；一旦保存过
                # 就必须完整相等，不能把同车次的不同区间当作仍然适用。
                return not str(saved_value or "") or str(fact_value or "") == str(saved_value)

            matched = [
                fact for fact in applicable
                if (fact.get("value") or {}).get("direction") == direction
                and _same_fact("train_no", previous.get("train_no"), (fact.get("value") or {}).get("train_no"))
                and _same_fact("depart_date", previous.get("depart_date"), (fact.get("value") or {}).get("depart_date") or (fact.get("value") or {}).get("date"))
                and _same_fact("origin", previous.get("from_station"), (fact.get("value") or {}).get("origin"))
                and _same_fact("destination", previous.get("to_station"), (fact.get("value") or {}).get("destination"))
            ]
            refreshed_previous = choose_plan_basis(matched, query=str(previous.get("train_no") or ""), direction=direction)
            previous_ok = isinstance(refreshed_previous, dict)
        if isinstance(basis, dict) and basis.get("origin") == "user_selected":
            # 1. 本轮点名车次，且该车次就在当前适用事实里。
            merged[direction] = basis
        elif direction == INBOUND and isinstance(basis, dict):
            # 2. 本轮明确给了返程时段偏好：这是新意图，必须盖住父方案的旧返程。
            merged[direction] = basis
        elif previous_ok:
            # 3. 本轮没提这个方向：父方案的车次仍适用就沿用，不重挑。
            refreshed_previous["origin"] = previous.get("origin") or "reused_selection"
            refreshed_previous["reason"] = "沿用已选车次，时刻与票价已按本轮查询更新"
            merged[direction] = refreshed_previous
        elif isinstance(previous, dict) and previous.get("origin") == "user_selected":
            # 用户自己选的车在刷新结果中已无票或不再适用，不能悄悄换成系统推荐；
            # 选择意图仍留在父快照，当前排程依据则明确降级为空。
            merged[direction] = None
        else:
            # 4. 没有可沿用的旧依据，用本轮结论（可能仍是 None）。
            merged[direction] = basis
    return merged


def _rail_window(plan_basis: dict, travel_date: str | None, days: int | None, required: dict[str, bool] | None = None) -> dict | None:
    # 只看两个方向键：plan_basis 里可能还有 basis_source 之类的标注，不能被当成「有依据」。
    has_basis = any((plan_basis or {}).get(direction) for direction in (OUTBOUND, INBOUND))
    if not has_basis and not (required or {}).get("outbound", False) and not (required or {}).get("inbound", False):
        return None
    try:
        first = date.fromisoformat(str(travel_date or ""))
    except ValueError:
        return None
    last = first + timedelta(days=max(1, int(days or 1)) - 1)
    return rail_window(plan_basis, first_day=first, last_day=last, required=required)


def _changed_slot_fields(snapshot: dict | None, confirmed: TravelSlots | None) -> list[str]:
    """本轮相对父方案实际改了哪些槽位字段。父方案没槽位（旧快照）时返回空列表。"""
    base = _snapshot_slots(snapshot)
    if base is None or confirmed is None:
        return []
    return diff_fields(base, confirmed)


def _saved_basis(snapshot: dict | None) -> tuple[dict, str | None]:
    """读父方案的规划依据，顺带兼容「依据只写在 request_context / constraints 里」的旧快照。

    只做回填，不凭空生成依据：顶层 plan_basis 为空时才依次看 request_context.plan_basis
    与 constraints.plan_basis；后者是旧快照兼容路径，标记 basis_source 便于排查来源。
    """
    snap = snapshot or {}
    basis = snap.get("plan_basis")
    saved = dict(basis) if isinstance(basis, dict) else {}
    if any(saved.get(direction) for direction in (OUTBOUND, INBOUND)):
        return saved, None
    context = snap.get("request_context")
    context_basis = context.get("plan_basis") if isinstance(context, dict) else None
    if isinstance(context_basis, dict) and any(
        context_basis.get(direction) for direction in (OUTBOUND, INBOUND)
    ):
        # v4 快照把依据同时写在顶层与 request_context，两处不一致时以这里为准读一次。
        return dict(context_basis), None
    selected = context.get("selected_train_intent") if isinstance(context, dict) else None
    if isinstance(selected, dict) and any(selected.get(direction) for direction in (OUTBOUND, INBOUND)):
        # 已点名但本轮没有有效依据时仍保留“想坐哪趟”的用户意图；不能把它伪装成
        # 可用于排程的旧依据，_plan_basis 会要求在当前适用且可售事实中重新命中。
        return {
            direction: (
                {"train_no": selected[direction], "origin": "user_selected"}
                if selected.get(direction) else None
            )
            for direction in (OUTBOUND, INBOUND)
        }, "selected_train_intent"
    constraints = snap.get("constraints")
    inherited = (constraints or {}).get("plan_basis") if isinstance(constraints, dict) else None
    if isinstance(inherited, dict) and any(
        inherited.get(direction) for direction in (OUTBOUND, INBOUND)
    ):
        return dict(inherited), "inherited_normalized"
    return saved, None


def _resolve_plan_basis(
    snapshot: dict | None,
    confirmed: TravelSlots | None,
    query: str,
    ticket_facts: list[dict],
    *,
    day_count: int | None = None,
    travel_date: str | None = None,
    required: dict | None = None,
    reused: bool = False,
    request_ctx: dict | None = None,
) -> tuple[dict, dict | None]:
    """返回 (plan_basis, rail_window)。全流程只调用一次，保证各处依据同源。

    顺序固定：
    1. 归一化旧快照：顶层依据为空时才从 constraints.plan_basis 回填（旧快照兼容）。
    2. 本轮票务相关字段或天数变了，两个方向一起失效——旧依据不再对应本次行程。
    3. 应用本轮选车（四级优先级在 _plan_basis 里实现）。
    4. 由依据重新计算 rail_window；窗口是派生结果，依据变了必须重算，不能照抄旧窗口。

    reused=True 表示本轮沿用了父方案的票务事实（没有重查）。此时若父方案只留了
    依据、没留票务事实，仍按父方案依据走；票务区间一旦变了（第 2 步）就照常清空。
    """
    saved, source = _saved_basis(snapshot)
    changed = _changed_slot_fields(snapshot, confirmed)
    old_ctx = (snapshot or {}).get("request_context") or {}
    intent_changed = any(
        old_ctx.get(key) != (request_ctx or {}).get(key)
        for key in ("intercity_mode", "return_required")
    )
    if any(item in TICKET_FIELDS for item in changed) or "days" in changed or intent_changed:
        # 日期/区间/交通方式/天数变了：去程与返程依据都不再成立，两个方向一起置空。
        saved = {OUTBOUND: None, INBOUND: None}
    basis = _plan_basis(ticket_facts, query, saved=saved, allow_stale=reused)
    if source and any(
        isinstance(basis.get(direction), dict)
        and saved.get(direction)
        and str((basis.get(direction) or {}).get("train_no") or "")
        == str((saved.get(direction) or {}).get("train_no") or "")
        for direction in (OUTBOUND, INBOUND)
    ):
        basis["basis_source"] = source
    window = _rail_window(basis, travel_date, day_count, required=required)
    return basis, window


def _return_candidates(ticket_facts: list[dict], *, limit: int = 8) -> list[dict]:
    """返程可选车次：只从适用事实里取，同一车次只留一条，最多 limit 条。

    只在「本轮没有选定返程」时用于提示「返程依据待定」，以及快照里的
    return_basis 选项；它不是规划依据，不能当成已经挑了车。
    """
    options: list[dict] = []
    seen: set[str] = set()
    for fact in applicable_facts(ticket_facts):
        value = fact.get("value") or {}
        if value.get("direction") != INBOUND:
            continue
        train_no = str(value.get("train_no") or "")
        if not train_no or train_no in seen:
            continue
        seen.add(train_no)
        options.append(
            {
                "train_no": train_no,
                "depart": value.get("depart"),
                "arrive": value.get("arrive"),
                "from_station": value.get("origin"),
                "to_station": value.get("destination"),
                "source_id": fact.get("source_id"),
            }
        )
        if len(options) >= limit:
            break
    return options


# 复用分支要从父方案恢复的全部字段。缺哪个就把哪个记进 inherited_unknown_fields，
# 让「父方案本来就没有」和「我们漏了没读」能区分开。
_INHERIT_ALL_FIELDS = (
    "ticket_facts",
    "ticket_raw",
    "ticket_reason",
    "ticket_queried_at",
    "map_context",
    "itinerary",
    "itinerary_needs_recheck",
    "facts_status",
    "candidates",
    "constraints",
    "facts",
    "search_usage",
    "route_states",
    "route_error",
    "search_error",
    "unlocated",
    "ticket_diagnostics",
    "ticket_notes",
    "plan_basis",
    "rail_window",
)
# map_only 只沿用「票务区间没变」这部分事实；路线、定位、窗口都要按本次重建结果覆盖。
_INHERIT_TICKET_FIELDS = ("ticket_facts", "ticket_raw", "ticket_reason", "ticket_queried_at")


def _inherit_defaults() -> dict[str, Any]:
    """父方案缺字段时的安全默认：宁可留空标未知，也不要凭默认值伪造已核实。

    注意 `ticket_reason` 这里的 "skipped" 只是「父快照没写这个字段」的占位，
    供 `inherited_unknown_fields` 审计用；复用分支判断能不能说「沿用上次查询」
    一律走 `_reuse_state()`，它会读到空值并归成 parent_unknown，不会被这个
    默认值说成「本次未查票（无需铁路）」。
    """
    return {
        "ticket_facts": [],
        "ticket_raw": [],
        "ticket_reason": "skipped",
        "ticket_queried_at": "",
        "map_context": "",
        "itinerary": None,
        "itinerary_needs_recheck": False,
        "facts_status": "unavailable",
        "candidates": None,
        "constraints": None,
        "facts": None,
        "search_usage": None,
        "route_states": {},
        "route_error": None,
        "search_error": None,
        "unlocated": [],
        "ticket_diagnostics": [],
        "ticket_notes": [],
        "plan_basis": {OUTBOUND: None, INBOUND: None},
        "rail_window": None,
        "inherited_unknown_fields": [],
    }


def _reuse_state(snapshot: dict | None) -> dict[str, str]:
    """父快照的票务复用三元状态：父原值 / 分类 / 面向正文的复用来源。

    三者并存而不是互相覆盖：`parent_reason` 是事实来源（快照里本来就写着
    ok/unavailable/missed/timeout），`parent_class` 给质量侧做聚合（失败三态
    都算 failed），`reuse_source` 给正文与前端决定「能不能说沿用上次查询」。
    父方案自己就是 reused 时，原始值仍然是 reused，不能追写成更早那次的结论。

    快照带 `ticket_reuse` 时优先用它：父方案本身也是复用来的，它的
    ticket_reason 已被压成 reused，只有存下来的 parent_reason 还留着最初那次
    查询的真实结论；丢了这层，孙代就会把「最初查失败」说成「沿用上次结果」。
    """
    snap = snapshot or {}
    stored = snap.get("ticket_reuse")
    if isinstance(stored, dict):
        parent_reason = str(stored.get("parent_reason") or "")
        parent_class = str(stored.get("parent_class") or "")
        reuse_source = str(stored.get("reuse_source") or "")
        if parent_reason and parent_class and reuse_source:
            return {
                "parent_reason": parent_reason,
                "parent_class": parent_class,
                "reuse_source": reuse_source,
            }
    reason = str(snap.get("ticket_reason") or "")
    parent_class, reuse_source = classify_ticket_parent(reason)
    return {
        "parent_reason": reason,
        "parent_class": parent_class,
        "reuse_source": reuse_source,
    }


def _inherit_from_parent(
    snapshot: dict | None,
    revision_kind: str,
    *,
    changed: list[str] | None = None,
) -> dict:
    """按修订分支返回可继承的父方案状态，缺失字段用安全默认并记入 inherited_unknown_fields。

    分支矩阵：
    - summary_only / budget_only：票务、行程、地图、约束、候选、路线状态、定位、诊断、
      依据与抵离窗口全部继承（这两条分支本来就不重跑任何子任务）。
    - map_only：只继承票务事实与未改动方向的依据；路线状态、错误、未定位项与抵离窗口
      一律用本次重建结果，避免拿旧窗口配新路线。
    - ticket_and_map：什么都不继承，全部重建。

    质量（pending/issues/sources）任何分支都不继承：每次请求都要按当前数据重算。
    """
    state = _inherit_defaults()
    snap = snapshot or {}
    if revision_kind == "ticket_and_map" or not snap:
        # 票务区间变了或根本没有父方案：全部重建，没有可继承的东西。
        state["inherited_unknown_fields"] = list(_INHERIT_ALL_FIELDS)
        return state
    if revision_kind in {"summary_only", "budget_only"}:
        keys = _INHERIT_ALL_FIELDS
    else:
        keys = _INHERIT_TICKET_FIELDS
    unknown: list[str] = []
    for key in keys:
        value = snap.get(key)
        if value is None:
            unknown.append(key)
            continue
        if isinstance(value, list):
            state[key] = list(value)
        elif isinstance(value, dict):
            state[key] = dict(value)
        else:
            state[key] = value
    if revision_kind == "map_only":
        # 只有行程/景点类修订才走这里：票务字段没变，但天数或区间可能变了。
        # 依据要单独读：_INHERIT_TICKET_FIELDS 只含票务四字段，不包含 plan_basis。
        changed_fields = [str(item) for item in (changed or [])]
        parent_basis = snap.get("plan_basis")
        inherited = dict(parent_basis) if isinstance(parent_basis, dict) else {}
        basis = {OUTBOUND: inherited.get(OUTBOUND), INBOUND: inherited.get(INBOUND)}
        if any(item in TICKET_FIELDS for item in changed_fields):
            # 日期/区间/交通方式变了：两个方向的旧依据都不再成立。
            basis = {OUTBOUND: None, INBOUND: None}
        elif "days" in changed_fields:
            # 天数变了返程日期跟着变，去程依据仍可用。
            basis[INBOUND] = None
        for direction in (OUTBOUND, INBOUND):
            if basis.get(direction) is None:
                unknown.append(f"plan_basis.{direction}")
        state["plan_basis"] = basis
        # 这三类是本轮重建的产物，即使父方案有值也不能沿用来覆盖新结果。
        for key in ("route_states", "rail_window", "unlocated"):
            if key in state and state[key] not in (None, [], {}):
                state[key] = _inherit_defaults()[key]
    state["inherited_unknown_fields"] = unknown
    return state


def _error_reason(error: str | None) -> str:
    """把内部错误码翻成一句人话。没有原因就不加后缀。"""
    text = str(error or "").strip()
    if not text:
        return ""
    labels = {
        "budget": "（本次查询额度或时间已用完）",
        "rate_limit": "（高德返回限流，未继续重试）",
        "request_failed": "（请求失败，已保留未知状态）",
        "no_route": "（高德没有返回可用路线）",
        "no_key": "（未配置高德 Web 服务密钥）",
        "not_queried": "（用户明确不查路线，未查询交通耗时）",
    }
    return labels.get(text, "")


# 复用分支带回的失败原因：措辞必须点明「沿用上一次结果」，不能让人以为本次也查过。
_REUSED_ERROR_LABELS = {
    "budget": "因查询额度或时间已用完",
    "rate_limit": "因高德返回限流",
    "request_failed": "因请求失败",
    "no_route": "因没有可用路线",
    "no_key": "因未配置高德 Web 服务密钥",
    "not_queried": "因用户明确不查路线",
}


def _reused_error_reason(error: str | None) -> str:
    """复用父方案带回的失败。与 _error_reason 同源取值，但换成「沿用」口径。"""
    reason = _REUSED_ERROR_LABELS.get(str(error or "").strip(), "")
    if reason:
        return f"（原路线查询{reason}未完成，本次沿用该结果）"
    return "（原路线查询未完成，本次沿用该结果）"


def _sight_entries(unlocated: list | None) -> list[dict]:
    """未能定位的景点与必去项。住宿与接驳端点另有独立状态，不算「景点未定位」。"""
    entries: list[dict] = []
    for item in unlocated or []:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "sight")
            name = str(item.get("name") or "").strip()
        else:
            kind, name = "sight", str(item or "").strip()
        if name and kind in {"sight", "must_visit"}:
            entries.append({"name": name, "kind": kind})
    return entries


def _lodging_warnings(unlocated: list | None) -> list[dict]:
    """住宿三态：只给区域、未匹配到具体地点，都如实说清，不包装成已确认酒店。"""
    warnings: list[dict] = []
    for item in unlocated or []:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "lodging":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if str(item.get("reason") or "") == "area":
            warnings.append(
                {
                    "code": "lodging_area_pending",
                    "message": f"{name}只是住宿区域，出发与返回点按该区域参考，未确认具体酒店",
                }
            )
        else:
            warnings.append(
                {
                    "code": "lodging_unresolved",
                    "message": f"没能在高德匹配到{name}，出发与返回点按目的地中心暂代",
                }
            )
    return warnings


def _source_label(tool: str, args: dict) -> str:
    """一次查询的人话标签：工具名 + 区间 + 日期。缺什么就不写，不猜。"""
    parts = [tool or "票务查询"]
    origin = args.get("fromStation") or args.get("from_station") or args.get("from")
    dest = args.get("toStation") or args.get("to_station") or args.get("to")
    if origin and dest:
        parts.append(f"{origin}→{dest}")
    day = args.get("date") or args.get("departDate") or args.get("start_date")
    if day:
        parts.append(str(day))
    city = args.get("city") or args.get("citys")
    if city:
        parts.append(str(city))
    return " ".join(parts)


def _group_sources(ticket_records: list[dict] | None, facts: list[dict] | None) -> list[dict]:
    """把「一条事实一行」折成「一次查询一行」。

    一次 get-tickets 返回 20 条座席价格，页面上不该显示 20 条同源依据；
    反过来，同样的参数查了两次也不能折叠成一次 —— 那是两次真实请求、两份时间。
    所以每行同时给两个数：`record_count` 是查询次数，`fact_count` 是记录条数。
    """
    fact_counts: dict[str, int] = {}
    for fact in facts or []:
        if isinstance(fact, dict):
            source_id = str(fact.get("source_id") or "").strip()
            if source_id:
                fact_counts[source_id] = fact_counts.get(source_id, 0) + 1

    # 先按记录 id 收齐「这一行引用了哪些查询」，再按 (tool, 参数) 折。
    by_id: dict[str, dict] = {}
    for record in ticket_records or []:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("id") or "").strip()
        if record_id:
            by_id[record_id] = record

    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for record_id, record in by_id.items():
        if not fact_counts.get(record_id):
            continue
        tool = str(record.get("tool") or "").strip()
        args = record.get("arguments")
        args = args if isinstance(args, dict) else {}
        try:
            key = (tool, json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))
        except (TypeError, ValueError):
            key = (tool, str(record_id))
        hit = groups.get(key)
        if hit is None:
            groups[key] = {
                "url": None,
                "queried_at": record.get("queried_at"),
                "ref": record_id,
                "label": _source_label(tool, args),
                "record_count": 1,
                "fact_count": fact_counts[record_id],
                "tool": tool or None,
                "records": [{"id": record_id, "queried_at": record.get("queried_at")}],
            }
            order.append(key)
            continue
        hit["record_count"] += 1
        hit["fact_count"] += fact_counts[record_id]
        hit["records"].append({"id": record_id, "queried_at": record.get("queried_at")})

    rows = [groups[key] for key in order]
    for row in rows:
        if row["record_count"] > 1:
            row["label"] = f"{row['label']}（{row['record_count']} 次相同参数查询）"

    # 网页取证不是票务工具调用，按来源 URL 聚合，时间用自己的抓取时间。
    # 但同一 URL 往往同时支撑多个景点（开放时间、门票），只留 URL 会让前端无法
    # 把「某景点的依据」和「这一行来源」对上：前端手里只有 poi_id，而这一行
    # 只有 URL，两边永远匹配不上。所以行内额外带 poi_ids 与逐条事实摘要。
    web: dict[str, dict] = {}
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        # 这一行的 url 最终会变成前端 <a href>，必须再过一次证据白名单。
        # 上游 parse_evidence 已经过滤，这里兜住"别处构造的事实"。
        url = safe_evidence_url(fact.get("source_url"), ALLOWED_HOSTS) or ""
        if not url:
            continue
        poi_id = str(fact.get("poi_id") or "").strip()
        brief = {
            "poi_id": poi_id or None,
            "kind": fact.get("kind"),
            "quote": fact.get("quote"),
            "applies_on": fact.get("applies_on"),
            "weekdays": fact.get("weekdays"),
            "status": fact.get("status"),
        }
        hit = web.get(url)
        if hit is None:
            web[url] = {
                "url": url,
                "queried_at": fact.get("queried_at"),
                "ref": fact.get("source_id") or url,
                "label": fact.get("source_title") or fact.get("subject") or "网页取证",
                "record_count": 1,
                "fact_count": 1,
                "tool": None,
                "records": [{"id": fact.get("source_id") or url, "queried_at": fact.get("queried_at")}],
                "poi_ids": [poi_id] if poi_id else [],
                "facts": [brief],
            }
        else:
            hit["fact_count"] += 1
            hit["facts"].append(brief)
            if poi_id and poi_id not in hit["poi_ids"]:
                hit["poi_ids"].append(poi_id)
            if str(fact.get("source_id") or "") not in {str(item.get("id") or "") for item in hit["records"]}:
                hit["record_count"] += 1

    rows.extend(web.values())
    return rows


def resolve_party(
    confirmed: TravelSlots | None,
    query: str,
    *,
    request_ctx: dict | None = None,
    explicit: int | None = None,
) -> int | None:
    """人数口径：本轮显式传的 > 本轮原话里抽到的 > 上次确认过的。

    快照里记住人数，第二次修订才不用再问一遍，预算也不会退回「人数未知」。
    """
    cleared = {str(item) for item in ((request_ctx or {}).get("cleared") or [])}
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    if "party_size" in cleared:
        return None
    said = party_size(query, confirmed.preferences if confirmed is not None else None)
    if said:
        return said
    saved = (request_ctx or {}).get("party_size")
    if isinstance(saved, int) and saved > 0:
        return saved
    return None


def _explicit_party(query: str, explicit: int | None) -> int | None:
    """本轮「明确给出」的人数：显式传参优先，其次本轮原话里抽到的。

    与 resolve_party 的区别：这里不看历史快照。人数要参与修订分支判定并写回
    request_context，所以必须在决定分支之前就拿到「本轮明确值」；拿历史值当
    变化会让一次没改人数的修订也被判成 budget_only。
    """
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    return party_size(query)


def _apply_structured(
    answer: str,
    *,
    itinerary: dict | None,
    ticket_facts: list[dict],
    ticket_reason: str,
    ticket_reused: bool,
    queried_at: str,
    confirmed: TravelSlots | None,
    need_ticket: bool,
    query: str,
    request_ctx: dict | None = None,
    party_explicit: int | None = None,
    pending_items: list[dict] | None = None,
    plan_basis: dict | None = None,
    rail_window: dict | None = None,
    lodging_state: dict | None = None,
    party: int | None = None,
    return_pending: list[dict] | None = None,
    rail_state: dict | None = None,
    reuse_source: str = "",
    parent_reason: str = "",
    unlocated: list[dict] | None = None,
    candidates: list[dict] | None = None,
) -> str:
    """用结构化状态替换正文里会被模型写飘的章节。

    行程表、车票建议、预算原本就是程序生成的；需求摘要与注意事项也从模型原文
    换成确定性内容：人数、住宿口径、返程是否已定这些信息只有结构化状态算数，
    模型很容易把「候选车次」写成「已锁定」，或者在人数已知时仍写「按 1 人估算」。

    这里只吃传进来的结构化状态，不读 quality——quality 在成文之后才生成。
    rail_state 区分「本轮不重查（沿用旧票）」与「这趟行程不需要铁路」，
    不传时 demand_summary_markdown 退回按 need_ticket 判断的旧两态。
    """
    if itinerary is not None:
        answer = replace_itinerary_section(answer, itinerary_markdown(itinerary))
        # 「景点建议」也不再交给模型自由发挥：有结构化行程时整节换成由 days 生成的
        # 确定性内容，模型写的景点名、游玩耗时、先后顺序都随之作废。模型仍负责解释性
        # 文字（需求摘要里的取舍说明、注意事项），但那两节本来也会被下面的替换覆盖。
        answer = replace_named_section(
            answer,
            "景点建议",
            sights_suggestion_markdown(itinerary, unlocated=unlocated, candidates=candidates),
        )
    visible = applicable_facts(ticket_facts) if ticket_reason in {"ok", "reused"} else []
    answer = replace_named_section(
        answer,
        "车票建议",
        ticket_markdown(
            visible,
            reused=ticket_reused,
            queried_at=queried_at,
            reason=ticket_reason,
            reuse_source=reuse_source,
            parent_reason=parent_reason,
        ),
    )
    count = resolve_party(confirmed, query, request_ctx=request_ctx, explicit=party_explicit)
    budget_text, _status = budget_markdown(
        visible,
        user_budget=confirmed.budget if confirmed is not None else None,
        days=confirmed.days if confirmed is not None else None,
        need_ticket=ticket_reason != "skipped",
        party_known=count is not None,
        party=count,
        budget_scope=(request_ctx or {}).get("budget_scope"),
    )
    answer = replace_named_section(answer, "预算", budget_text)
    resolved_party = party if isinstance(party, int) and party > 0 else count
    origin_body = section_body(answer, "需求摘要")
    answer = replace_named_section(
        answer,
        "需求摘要",
        demand_summary_markdown(
            origin=confirmed.origin if confirmed is not None else None,
            destination=confirmed.destination if confirmed is not None else None,
            date_text=confirmed.date if confirmed is not None else None,
            days=confirmed.days if confirmed is not None else None,
            party=resolved_party,
            lodging_state=lodging_state,
            need_ticket=need_ticket,
            # 降级/超时/未通过校验的说明必须留在正文里，否则读者不知道这版为何降级。
            extra_notes=degraded_note_lines(origin_body),
            rail_state=rail_state,
        ),
    )
    answer = replace_named_section(
        answer,
        "注意事项",
        cautions_markdown(
            section_body(answer, "注意事项"),
            pending_items=pending_items,
            plan_basis=plan_basis,
            rail_window=rail_window,
            lodging_state=lodging_state,
            return_pending=return_pending,
            rail_state=rail_state,
        ),
    )
    return strip_unlisted_trains(answer, allowed_train_numbers(visible))


def _svc() -> Any:
    """延迟 import 根模块，避免 pipeline ↔ planner_service 循环导入。"""
    import planner_service as svc

    return svc


def _copy_map(parent_id: str, plan_id: str) -> None:
    src = MAPS_DIR / f"{parent_id}.html"
    dst = MAPS_DIR / f"{plan_id}.html"
    if parent_id and src.is_file():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _input_state(
    slots: TravelSlots | None,
    request_ctx: dict | None,
    *,
    lodging_kind: str | None = None,
    party: int | None = None,
) -> dict[str, Any]:
    """输入状态的唯一入口：暂停判定、草稿 pending、确认项都从这里取。"""
    ctx = request_ctx or {}
    return input_state(
        slots,
        lodging_scope=ctx.get("lodging_scope"),
        lodging_kind=lodging_kind if lodging_kind is not None else ctx.get("lodging_kind"),
        party_known=bool(party) or bool(ctx.get("party_size")),
        budget_scope=ctx.get("budget_scope"),
    )


def _resolve_input_state(
    slots: TravelSlots | None,
    request_ctx: dict | None,
    party_explicit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """算一次输入状态，并给出「不拦路但要写进 quality.pending」的清单。"""
    party = resolve_party(slots, "", request_ctx=request_ctx, explicit=party_explicit)
    state = _input_state(
        slots,
        request_ctx,
        lodging_kind=(request_ctx or {}).get("lodging_kind"),
        party=party,
    )
    pending: list[dict[str, Any]] = []
    if must_pause(state):
        return pending, state
    for item in draft_pendings(state):
        pending.append(item)
    if state.get("lodging_pending"):
        pending.append(
            {
                "code": "lodging_area_pending",
                "message": "住宿只给到区域，出发与返回点按该区域参考，未确认具体酒店",
                "action": "confirm_lodging",
            }
        )
    return pending, state


def _clarify_event(
    slots: TravelSlots,
    state: dict[str, Any] | None = None,
    *,
    request_ctx: dict | None = None,
    party_explicit: int | None = None,
) -> dict[str, Any]:
    preview, defaults_applied = apply_soft_defaults(slots)
    missing = clarify_missing(slots)
    event = {
        "type": "clarify",
        "slots": preview.model_dump(),
        "missing": missing,
        "defaults_applied": defaults_applied,
        "message": (
            "多日行程请补充住宿地。每天从住宿地出发，最后一个景点结束后回到住宿地。"
            if "lodging" in missing
            else "请确认或补全关键信息后继续"
        ),
        "request_context": {
            "intercity_mode": (request_ctx or {}).get("intercity_mode"),
            "return_required": (request_ctx or {}).get("return_required"),
        },
    }
    if state is None:
        _pending, state = _resolve_input_state(slots, request_ctx, party_explicit)
    pending = pending_confirmations(state) if state else []
    if pending:
        # 需要用户点一下的项（住宿区域、人数、预算口径）走同一个确认卡。
        event["pending"] = pending
    return event


async def stream_plan(
    query: str,
    cancel_event: asyncio.Event | None = None,
    slots: TravelSlots | None = None,
    parent_plan_id: str | None = None,
    *,
    mode: str = "plan",
    lodging_scope: str | None = None,
    party_size: int | None = None,
    budget_scope: str | None = None,
    cleared_fields: list[str] | None = None,
    intent_source: str = "initial",
    intercity_mode: str | None = None,
    return_required: bool | None = None,
    run_stats: RunStats | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """一次完整旅游规划的异步事件流（CLI 与 Web 共用）。

    slots: 确认卡回传时带上，跳过「再弹确认」（关键字段仍缺会再澄清）。
    parent_plan_id: 修订上一份方案时传入其 UUID。
    cancel_event: 浏览器断开时 set，尽快停 LLM。
    mode: `plan` 首次规划 / `revise` 改主意 / `confirm` 补充确认卡提交。
    lodging_scope: `exact` 具体酒店 / `area` 按区域参考规划。
    party_size: 几人同行；`budget_scope`: `per_person` 人均 / `total` 总额。
    cleared_fields: 显式撤销的确认状态字段名。
    run_stats: 调用方（如 server 层）持有的请求级统计；不传时本函数自己开一个。
        各层通过 planner.run_stats.note() 写自己拥有的字段，最后由 timings 带出。
    """
    svc = _svc()
    begin_usage()
    stats = begin_run_stats(run_stats)
    budget = RunBudget()
    plan_uuid = uuid4()
    plan_id = str(plan_uuid)
    cancelled = False
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
    # 结构化行程的来源：built=本次真排程；reused=沿用父方案；none=没有可信行程。
    # 正文与行程表据此决定「能不能写具体景点与时刻」，不能靠模型自由生成补空。
    route_source = "none"
    skip_map: str | None = None
    timed_out = False
    ticket_agent: dict[str, Any] | None = None
    map_dispatched = False
    map_reason = "missed"
    map_context = ""
    ticket_reason = "skipped"
    ticket_context = ""
    ticket_facts: list[dict] = []
    ticket_records: list[dict] = []
    ticket_diagnostics: list[dict] = []
    ticket_notes: list[dict] = []
    ticket_reused = False
    # 复用父方案时的票务三元状态：父原值 / 父分类 / 复用来源。三者并存，
    # 正文、质量与前端各自读自己那一份，任何一方都不改写另外两方。
    ticket_reuse: dict[str, str] = {"parent_reason": "", "parent_class": "", "reuse_source": ""}
    itinerary: dict[str, Any] | None = None
    # 行程是否沿用旧口径、需按当前规则重建后才能解除复核标记。与「本轮是否重排」
    # 无关：真正重建时置 False，复用父方案时继承其标记。
    itinerary_needs_recheck = False
    unlocated: list[dict] = []
    # 本轮路线状态：内存里是元组键，落盘前转 saved_route_states；两条路径都从这里读。
    route_states: dict = {}
    saved_route_states: dict = {}
    facts_status = "unavailable"
    search_error = None
    route_error = None
    saved_candidates = None
    saved_constraints = None
    saved_facts = None
    saved_search_usage = None
    # 返程候选：没有明确选择时给出可选车次，供质量诊断提示「返程依据待定」。
    # 它不是规划依据，只在本轮未选定返程时用于提示与快照 return_basis。
    return_candidates: list[dict] | None = None
    amap_stats = {"poi_requests": 0, "route_requests": 0, "amap_rate_limits": 0}
    assemble_stats: dict[str, Any] | None = None
    request_ctx: dict[str, Any] = merge_request_context(None, {
        "party_size": party_size,
        "budget_scope": budget_scope,
        "lodging_scope": lodging_scope,
        "intercity_mode": intercity_mode,
        "return_required": return_required,
    })
    initial_flags = effective_intercity_intent(query, query_is_current=intent_source != "background")
    detected_intercity_mode: str | None = None
    if request_ctx.get("intercity_mode") is None:
        if initial_flags["rail"]:
            detected_intercity_mode = "rail"
        elif initial_flags["drive"]:
            detected_intercity_mode = "driving"
        elif initial_flags["plane"]:
            detected_intercity_mode = "plane"
        if detected_intercity_mode:
            request_ctx["intercity_mode"] = detected_intercity_mode
    detected_return_required: bool | None = None
    if intent_source != "background":
        if any(word in query for word in ("单程", "不往返", "不用返回", "不回程")):
            detected_return_required = False
        elif any(word in query for word in ("往返", "当天回", "当天返回", "来回", "返程", "回程")):
            detected_return_required = True
    if request_ctx.get("return_required") is None and detected_return_required is not None:
        request_ctx["return_required"] = detected_return_required
    input_pending: list[dict[str, Any]] = []
    plan_basis: dict[str, Any] = {}
    window: dict[str, Any] | None = None

    agent_out: dict[str, Any] = {"records": []}
    # 资源清理结果（B8 / m02971 P1）：正常路径发 final 前生成，取消/超时/断连时
    # 由 aclose 触发的 finally 兜底生成。用非空快照保证幂等，避免同一 MCP 连接
    # 被关闭两次；server 与 pipeline 持有的是同一个 RunStats 对象。
    cleanup_snapshot: dict[str, Any] | None = None

    async def _finalize_cleanup() -> dict[str, Any]:
        """登记子任务残留并关闭本请求 MCP，返回可观测的清理结果。

        结果同时写进 RunStats：SSE 总超时或断连时 server 来不及收 final，
        可从 request_stats.cleanup 读出补进 error 事件，不必只依赖日志。
        """
        nonlocal cleanup_snapshot
        if cleanup_snapshot is not None:
            return cleanup_snapshot
        leaked = list(dict.fromkeys(agent_out.get("subagent_leak") or []))
        had_mcp = isinstance(ticket_agent, dict)
        close_result = await close_ticket_agent(ticket_agent if had_mcp else None) or {}
        cleanup_snapshot = {
            "drain_started": True,
            "drain_completed": True,
            "subagent_leak": leaked,
            "mcp_close_started": had_mcp,
            "mcp_close_completed": bool(close_result.get("closed")),
            "mcp_close_error": close_result.get("error"),
            # 「关过 MCP」不等于「资源已完全释放」：有残留任务时如实标出来，
            # 便于排查卡在一次 MCP 请求上的子任务。
            "mcp_closed_after_timeout": bool(leaked and had_mcp),
        }
        if leaked:
            stats.note_text("subagent_leak:" + ",".join(leaked))
            if had_mcp:
                stats.note_text("mcp_closed_after_timeout")
                logger.warning(
                    "plan_id=%s subagent_leak=%s mcp_closed_after_timeout=True",
                    plan_id, ",".join(leaked),
                )
        stats.set_cleanup(cleanup_snapshot)
        return cleanup_snapshot

    try:
        # —— 修订/确认：读上一版快照，本轮槽位合并求差后按变化失效并决定路由 ——
        base_slots: TravelSlots | None = None
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
            base_slots = _snapshot_slots(snapshot)
            previous_answer = rewrite_plan_ids(str(snapshot.get("answer") or ""), parent_id, plan_id)
            reused_ticket_context = rewrite_plan_ids(
                str(snapshot.get("ticket_context") or ""), parent_id, plan_id
            )
            # 初始判断只用于状态文案；真正分支在下面「合并→求差」后重算。
            revision_kind = classify_revision(query, previous_ticket_state=prev_state)
            # revise 的 query 就是本轮意见，必须覆盖父方案的跨城意图；confirm 的
            # query 是随表单回传的历史背景，只接受显式表单字段。
            current_intercity_mode = intercity_mode
            current_return_required = return_required
            if intent_source != "background":
                if initial_flags["rail"]:
                    current_intercity_mode = "rail"
                elif initial_flags["drive"]:
                    current_intercity_mode = "driving"
                elif initial_flags["plane"]:
                    current_intercity_mode = "plane"
                if detected_return_required is not None:
                    current_return_required = detected_return_required
            request_ctx = merge_request_context(
                snapshot.get("request_context"),
                {
                    "party_size": party_size,
                    "budget_scope": budget_scope,
                    "lodging_scope": lodging_scope,
                    "intercity_mode": current_intercity_mode,
                    "return_required": current_return_required,
                },
                cleared=cleared_fields,
            )

        # —— 理解需求：抽槽位或合并本轮槽位；缺关键字段则 clarify 后结束本轮 ——
        yield {
            "type": "step",
            "id": "understand",
            "status": "active",
            **({"label": "修订需求"} if revision_kind else {}),
        }
        yield {"type": "status", "message": "正在理解需求…" if not revision_kind else "正在理解修改意见…"}
        t_understand = time.monotonic()

        llm = None

        def ensure_llm() -> Any:
            nonlocal llm
            if llm is None:
                llm = svc.build_llm()
            return llm

        if snapshot is not None:
            # 修订/确认统一顺序：合并 → 求差 → 按变化失效 → 应用本轮确认 → 决定路由。
            if confirmed is None and revision_kind != "summary_only":
                try:
                    overlay = slots_for_plan(await _extract(svc, query, ensure_llm(), budget))
                except Exception:
                    logger.exception("revision_extract_failed")
                    yield {"type": "error", "message": "没能从修改意见里解析出可执行的变化，请换个说法再试"}
                    yield {"type": "step", "id": "understand", "status": "done"}
                    return
                confirmed = merge_slots(base_slots, overlay) if base_slots is not None else overlay
            elif confirmed is not None:
                confirmed = merge_slots(base_slots, confirmed) if base_slots is not None else confirmed
            else:
                confirmed = base_slots

            if confirmed is not None:
                confirm_changed = diff_fields(base_slots, confirmed) if base_slots is not None else []
                # 本轮原话里说出的人数（如「改成三个人」）也要算作上下文变化并写回快照，
                # 否则本轮预算按 3 人算、快照里还留着 2 人，下一轮又会退回旧口径。
                said_party = _explicit_party(query, party_size) if intent_source != "background" else party_size
                context_changed = context_revision_diff(
                    snapshot.get("request_context"),
                    {
                        "party_size": said_party,
                        "budget_scope": budget_scope,
                        "lodging_scope": lodging_scope,
                        "intercity_mode": current_intercity_mode,
                        "return_required": current_return_required,
                    },
                    cleared=cleared_fields,
                )
                # 选车与重新定位住宿都是「纯原话动作」：没有槽位差异也要重算，
                # 不能在 confirm 分支里被提前压成 summary_only。
                explicit_action = _explicit_train_selection(query) or _explicit_lodging_relocate(query)
                if mode == "confirm" and not confirm_changed and not context_changed and not explicit_action:
                    # 什么都没改、也没点名车次/返程时段/重定位：不重查、不重排，
                    # 只按已有数据重出一版正文。
                    revision_kind = "summary_only"
                else:
                    revision_kind = resolve_revision_kind(
                        confirm_changed, context_changed, query, previous_ticket_state=prev_state
                    )
                request_ctx = merge_request_context(
                    snapshot.get("request_context"),
                    {
                        "party_size": said_party,
                        "budget_scope": budget_scope,
                        "lodging_scope": lodging_scope,
                        "lodging_text": confirmed.lodging,
                        "intercity_mode": current_intercity_mode,
                        "return_required": current_return_required,
                    },
                    changed=confirm_changed,
                    cleared=cleared_fields,
                )
                confirmed, _ = apply_soft_defaults(confirmed)
                yield {"type": "slots", "slots": confirmed.model_dump()}
                if should_clarify(confirmed, slots_from_client=True):
                    yield _clarify_event(confirmed, request_ctx=request_ctx)
                    yield {"type": "step", "id": "understand", "status": "done"}
                    return
        elif confirmed is not None:
            if should_clarify(confirmed, slots_from_client=True):
                yield _clarify_event(confirmed, request_ctx=request_ctx)
                yield {"type": "step", "id": "understand", "status": "done"}
                return
            confirmed, _ = apply_soft_defaults(confirmed)
            yield {"type": "slots", "slots": confirmed.model_dump()}
        else:
            try:
                extracted = slots_for_plan(await _extract(svc, query, ensure_llm(), budget))
                yield {"type": "slots", "slots": extracted.model_dump()}
                if should_clarify(extracted, slots_from_client=False):
                    yield _clarify_event(extracted, request_ctx=request_ctx)
                    yield {"type": "step", "id": "understand", "status": "done"}
                    return
                confirmed, _ = apply_soft_defaults(extracted)
            except Exception:
                logger.exception("slot_extract_failed")
                guessed = slots_from_query(query)
                if guessed is not None and not should_clarify(guessed, slots_from_client=False):
                    confirmed, _ = apply_soft_defaults(guessed)
                    yield {"type": "slots", "slots": confirmed.model_dump()}
                    yield {"type": "status", "message": "参数解析失败，已按原话中的出发地、目的地和住宿地继续"}
                elif guessed is not None:
                    yield _clarify_event(guessed, request_ctx=request_ctx)
                    yield {"type": "step", "id": "understand", "status": "done"}
                    return
                else:
                    confirmed = TravelSlots()
                    yield _clarify_event(confirmed, request_ctx=request_ctx)
                    yield {"type": "step", "id": "understand", "status": "done"}
                    return

        # —— 输入状态：只记录「还缺什么」，是否暂停由 must_pause 决定 ——
        input_pending, state = _resolve_input_state(confirmed, request_ctx, party_size)
        if state.get("lodging_pending") and not must_pause(state):
            yield {
                "type": "warning",
                "code": "lodging_area_pending",
                "message": "住宿只给到区域，先按该区域参考规划；想改成具体酒店可以点「补充与确认」",
            }

        if cancel_event is not None and cancel_event.is_set():
            return

        understand_ms = ms_since(t_understand)
        yield {"type": "step", "id": "understand", "status": "done", "elapsed_ms": understand_ms}

        # —— 路由：要不要铁路、要不要路线地图 ——
        if revision_kind in {"map_only", "summary_only", "budget_only"} and _ticket_fields_match(
            snapshot,
            confirmed,
            query=query,
            previous_query=str((snapshot or {}).get("query") or ""),
            request_ctx=request_ctx,
            query_is_current=intent_source != "background",
        ):
            need_ticket = False
            skip_reason = "route_no_rail"
        elif revision_kind == "summary_only":
            need_ticket = False
            skip_reason = "route_no_rail"
        else:
            skip_reason = skip_ticket_reason(
                confirmed, query, previous_query=str((snapshot or {}).get("query") or ""), request_ctx=request_ctx,
                query_is_current=intent_source != "background",
            )
            need_ticket = skip_reason is None

        # —— 地图语义分三层：检索景点 / 排路线 / 出地图 HTML ——
        # 用户说「不要地图」只是不要地图产物，景点与路线仍该给；说「不要找景点」
        # 才是连检索都停。合成一个布尔会把前者当成后者，结构化行程直接丢失。
        scope = map_scope(query)
        need_poi_search = bool(scope["need_poi_search"])
        need_route = bool(scope["need_route"])
        need_map_artifact = bool(scope["need_map_artifact"])
        skip_map = scope["reason"] or None
        # need_map 保持「要不要调度地图子任务（POI 检索）」这一旧含义，
        # 下游 iter_agents 与告警全部按它判断。
        need_map = need_poi_search
        if revision_kind in {"summary_only", "budget_only"}:
            # 纯改措辞/预算：不重跑任何地图相关子任务，行程与地图沿用父方案。
            need_map = False
            need_route = False
            need_map_artifact = False
            skip_map = skip_map or "route_no_map"

        logger.info("plan_id=%s need_ticket=%s skip_reason=%s need_map=%s revision=%s", plan_id, need_ticket, skip_reason, need_map, revision_kind)

        map_result: dict[str, Any] = {}
        ticket_result: dict[str, Any] = {}
        # 行程排程与正文都只认这一份依据：票务事实定下来之后立刻算一次，两条路径共用。
        map_file_now = MAPS_DIR / f"{plan_id}.html"
        dest = (confirmed.destination if confirmed is not None else "") or destination_from_query(query)
        stay = (confirmed.lodging if confirmed is not None else None) or lodging_from_query(query)
        day_count = confirmed.days if confirmed is not None and confirmed.days else days_from_query(query)
        travel_date = confirmed.date if confirmed is not None else None
        must_visit = list(confirmed.must_visit) if confirmed is not None else []
        avoid = list(confirmed.avoid) if confirmed is not None else []
        transport_mode = confirmed.transport_mode if confirmed is not None else "public_transit"
        # 铁路需求（去程/返程要不要票）由 rail_required 算一次，供依据、正文与质量共用；
        # 重复计算的风险是两处口径漂移（正文说需要返程、依据却没按返程算窗口）。
        rail_req: dict[str, bool] = {}

        def _apply_basis() -> tuple[dict, dict[str, Any] | None]:
            """票务事实已定：算本轮唯一依据，并同源写入请求上下文与父约束副本。

            复用分支与重建分支各调一次，但每次请求只算一次；窗口由依据派生，
            依据变了必须重算，不能照抄父方案的旧窗口。
            """
            nonlocal request_ctx, return_candidates, saved_constraints, rail_req
            rail_req = rail_required(
                confirmed, query, previous_query=str((snapshot or {}).get("query") or ""), request_ctx=request_ctx,
                query_is_current=intent_source != "background",
            )
            basis, win = _resolve_plan_basis(
                snapshot,
                confirmed,
                query,
                ticket_facts,
                day_count=day_count,
                travel_date=travel_date,
                required=rail_req,
                reused=ticket_reused,
                request_ctx=request_ctx,
            )
            selected_intent = dict(request_ctx.get("selected_train_intent") or {})
            for direction in (OUTBOUND, INBOUND):
                chosen = (basis or {}).get(direction)
                if isinstance(chosen, dict) and chosen.get("origin") == "user_selected":
                    selected_intent[direction] = chosen.get("train_no")
            request_ctx = merge_request_context(
                request_ctx,
                {
                    "plan_basis": basis,
                    "selected_train_intent": selected_intent,
                    "party_size": resolve_party(
                        confirmed, query, request_ctx=request_ctx, explicit=party_size
                    ),
                    "lodging_text": stay,
                },
            )
            # 本轮没有选定返程车次时，把适用事实里的返程车次列成候选，供质量诊断提示
            # 「返程依据待定」并写进快照；候选不是依据，不能当成已经挑了车。
            if (basis or {}).get(INBOUND) is None:
                return_candidates = _return_candidates(ticket_facts)
            if isinstance(saved_constraints, dict):
                # 复用分支的约束来自父方案，里面可能还写着上一版的依据与窗口。
                # 依据和窗口都是本次同源算出来的，这里必须覆盖，
                # 否则快照里会同时存在「本轮依据」和「上一版窗口」两套互相矛盾的记录。
                saved_constraints = {
                    **saved_constraints,
                    "plan_basis": dict(basis or {}),
                    "rail_window": win,
                }
            return basis, win

        if revision_kind in {"summary_only", "budget_only"}:
            # 只改预算/人数/口径，或只是重写正文：票、行程、地图、定位约束都沿用上一版。
            # budget_only 也必须走完整复用，否则行程变 null、地图不复制、待复核标记还会
            # 被错误地当成「已按当前规则重建」清零。
            inherited = _inherit_from_parent(snapshot, revision_kind, changed=confirm_changed)
            # 父方案的事实是上一版行程算的适用性。这一版虽然只有措辞/预算在变，
            # 仍要按当前行程重验一遍：直接把上次结论当本次结论，等于用旧行程的
            # 日期和方向去判断这版票还成不成立。
            ticket_facts = revalidate_facts(
                inherited["ticket_facts"],
                expect_date=confirmed.date if confirmed is not None else None,
                origin=confirmed.origin if confirmed is not None else None,
                destination=confirmed.destination if confirmed is not None else None,
                return_date=_return_date(
                    confirmed,
                    query,
                    previous_query=str((snapshot or {}).get("query") or ""),
                    expect_date=confirmed.date if confirmed is not None else None,
                    request_ctx=request_ctx,
                    query_is_current=intent_source != "background",
                ),
            )
            ticket_records = inherited["ticket_raw"]
            ticket_diagnostics = list(inherited["ticket_diagnostics"])
            ticket_notes = list(inherited["ticket_notes"])
            ticket_reused = True
            ticket_reuse = _reuse_state(snapshot)
            ticket_reason = (
                "skipped" if ticket_reuse["parent_reason"] == "skipped" else "reused"
            )
            ticket_context = ticket_markdown(
                ticket_facts,
                reused=True,
                queried_at=str(inherited["ticket_queried_at"] or ""),
                reason=ticket_reason,
                reuse_source=ticket_reuse["reuse_source"],
                parent_reason=ticket_reuse["parent_reason"],
            )
            # 父链的错误状态也要带过来：否则复用后 quality.route_error 变 null，
            # 「上次查询因预算没完成」这件事在正文、快照、前端全都消失。
            route_error = inherited["route_error"]
            search_error = inherited["search_error"]
            unlocated = list(inherited["unlocated"])
            saved_route_states = dict(inherited["route_states"] or {})
            route_states = dict(inherited["route_states"] or {})
            # 候选、约束、开放规则事实与检索用量都属于「上一版已经查过的东西」，
            # 与行程能否复用无关：即使这一版行程取不回来，这些结构化结果照样要留给
            # 正文、质量与快照，否则复用一次就把它们整体清空了。
            saved_candidates = inherited["candidates"]
            saved_constraints = inherited["constraints"]
            saved_facts = inherited["facts"]
            saved_search_usage = inherited["search_usage"]
            map_context = rewrite_plan_ids(str(inherited["map_context"] or ""), parent_id or "", plan_id)
            saved_plan = reusable_itinerary(snapshot)
            if saved_plan:
                itinerary = saved_plan
                route_source = "reused"
                # 复用父方案行程，继承其「待复核」标记；只有按当前规则真正重建才能解除。
                itinerary_needs_recheck = bool(inherited["itinerary_needs_recheck"])
                facts_status = str(inherited["facts_status"] or "unavailable")
                _copy_map(parent_id or "", plan_id)
                map_reason = "ok" if (MAPS_DIR / f"{plan_id}.html").is_file() else "skipped"
            else:
                map_reason = "ok" if str(inherited["map_context"] or "").strip() else "skipped"
            # 复用分支同样要按本次请求重算依据：父方案记的是上一版的依据与窗口，
            # 直接照抄会让快照里出现两套互相矛盾的记录（依据一套、窗口一套）。
            plan_basis, window = _apply_basis()
            # 复用分支这一轮既没连票务也没跑地图，两处都如实标「沿用上一版」：
            # 写成 route_no_rail / route_no_map 会让前端和黄金集把「复用」读成
            # 「本轮判定不需要」，那是另一件事。
            # RunStats（B8）：本轮没有重新发起连接与 POI 检索，记 not_needed，
            # 不能把「复用」和「动作根本没发生」混成同一句空统计。
            stats.note("ticket_connect", "not_needed")
            stats.note("map_poi", "not_needed")
            yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "reuse_parent"}
            yield {"type": "step", "id": "map", "status": "skipped", "reason": "reuse_parent"}
        else:
            if not need_ticket:
                ticket_agent = None
                # B8：纯规则判定无需铁路，连接动作是「没必要发生」，不是失败或依赖缺失。
                stats.note("ticket_connect", "not_needed")
                reason = skip_reason or "route_no_rail"
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": reason}
                yield {"type": "status", "message": "未查票（无需铁路）"}
            else:
                yield {"type": "status", "message": "正在连接 12306 MCP 并加载车票工具..."}
                t_mcp = time.monotonic()
                ticket_agent = await svc.get_ticket_agent(budget)
                lookup = (ticket_agent or {}).get("diag") or {}
                ticket_cache_hit = bool(lookup.get("cache_hit"))
                ticket_mcp_ms = int(lookup.get("connect_ms") or ms_since(t_mcp))
                if cancel_event is not None and cancel_event.is_set():
                    return
                if ticket_agent is None:
                    # 连接失败的真实分类（dependency_missing / timed_out / failed）
                    # 由 ticket_cache.get_ticket_agent 写；编排层不事后覆盖，避免把
                    # 「超时」误记成「依赖缺失」。
                    yield {"type": "status", "message": "车票服务暂不可用，本次仅生成景点方案"}
                    yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "mcp_unavailable"}
                else:
                    tool_count = len(ticket_agent.get("tools") or [])
                    yield {"type": "status", "message": f"车票工具已加载，共 {tool_count} 个。"}

            if not need_map:
                # B8：这一层决定了不调度地图子任务 / 不跑 POI 检索，动作「没必要发生」。
                stats.note("map_poi", "not_needed")
                yield {"type": "step", "id": "map", "status": "skipped", "reason": skip_map or "user_no_map"}
                # 两种否决的原因不同：不要地图只是不要产物，景点与路线照样给；
                # 不要找景点才是连检索都停，正文不能再说「景点方案已生成」。
                if skip_map == "user_no_poi":
                    yield {"type": "status", "message": "未检索景点（用户明确不要找景点）"}
                else:
                    yield {"type": "status", "message": "未生成路线地图（用户明确不要地图）"}

            # —— 规则决定后由代码拉起子任务，不再经主智能体转发 ——
            yield {"type": "status", "message": "开始规划"}
            user_content = query
            if confirmed is not None:
                user_content = f"{query}\n\n{format_slots_for_prompt(confirmed, rail=need_ticket)}"
            if revision_kind == "map_only":
                user_content = f"修订意见：{query}\n请调整景点与路线。\n\n{format_slots_for_prompt(confirmed, rail=need_ticket)}"

            agent_out = {"records": []}
            t_dispatch = time.monotonic()
            if need_map or ticket_agent is not None:
                async for event in iter_agents(
                    agent_out,
                    need_map=need_map,
                    need_ticket=ticket_agent is not None,
                    ticket_tools=list((ticket_agent or {}).get("tools") or []),
                    llm=ensure_llm(),
                    query=user_content,
                    budget=budget,
                    cancel_event=cancel_event,
                    city=(confirmed.destination if confirmed is not None else "") or destination_from_query(query),
                    stats=amap_stats,
                ):
                    # cleanup 是 iter_agents 的内部事件，不属于 SSE 契约（status/slots/
                    # clarify/step/warning/token/final/error）。直通出去前端会收到一个
                    # 不认识的事件名；残留本身由 subagent_leak 统计与告警表达。
                    if str(event.get("type") or "") == "cleanup":
                        continue
                    yield event
            dispatch_ms = ms_since(t_dispatch)
            cancelled = bool(agent_out.get("cancelled")) or (cancel_event is not None and cancel_event.is_set())
            timed_out = bool(agent_out.get("timed_out"))
            map_result = agent_out.get("map") or {}
            ticket_result = agent_out.get("ticket") or {}
            map_dispatched = bool(need_map and map_result.get("started"))
            parallel_tasks = bool(agent_out.get("parallel"))
            parallel_results = parallel_tasks and not timed_out and bool(map_result) and bool(ticket_result)
            map_ms = map_result.get("elapsed_ms") if need_map else None
            ticket_ms = ticket_result.get("elapsed_ms") if ticket_agent is not None else None
            map_active_at = map_result.get("started_at") if need_map else None
            ticket_active_at = ticket_result.get("started_at") if ticket_agent is not None else None
            if cancelled:
                return

            map_context = str(map_result.get("text") or "")
            ticket_records = list(agent_out.get("records") or [])
            expect_date = confirmed.date if confirmed is not None else None
            origin = confirmed.origin if confirmed is not None else None
            destination = confirmed.destination if confirmed is not None else None
            previous_query = str((snapshot or {}).get("query") or "")
            if revision_kind == "map_only" and _ticket_fields_match(
                snapshot,
                confirmed,
                query=query,
                previous_query=previous_query,
                request_ctx=request_ctx,
                query_is_current=intent_source != "background",
            ):
                # 只改景点/顺序（含「返程坐 G3252」这类选车）：票务区间没变，沿用父方案票务事实，
                # 但路线状态、未定位项与抵离窗口由本次重新排程产出，不在这里继承。
                inherited = _inherit_from_parent(snapshot, "map_only", changed=confirm_changed)
                ticket_reused = True
                # 事实里的 applied 是上一版行程算的。即便票务区间一致，天数或返程意图
                # 也可能变过（例如刚改成 4 日游），必须按本轮行程重验一遍再拿去定依据。
                ticket_facts = revalidate_facts(
                    inherited["ticket_facts"],
                    expect_date=expect_date,
                    origin=origin,
                    destination=destination,
                    return_date=_return_date(
                        confirmed,
                        query,
                        previous_query=previous_query,
                        expect_date=expect_date,
                        request_ctx=request_ctx,
                        query_is_current=intent_source != "background",
                    ),
                )
                ticket_records = inherited["ticket_raw"]
                ticket_diagnostics = list(inherited["ticket_diagnostics"])
                ticket_notes = list(inherited["ticket_notes"])
                queried = str(inherited["ticket_queried_at"] or "")
                ticket_reuse = _reuse_state(snapshot)
                ticket_reason = (
                    "skipped" if ticket_reuse["parent_reason"] == "skipped" else "reused"
                )
                ticket_context = ticket_markdown(
                    ticket_facts,
                    reused=True,
                    queried_at=queried,
                    reason=ticket_reason,
                    reuse_source=ticket_reuse["reuse_source"],
                    parent_reason=ticket_reuse["parent_reason"],
                )
                # 路线状态、未定位项与失败原因都不继承：本次重排会产出新结果，
                # 沿用旧值会让告警指向已经不存在的问题。
            elif revision_kind in {"summary_only", "budget_only"}:
                inherited = _inherit_from_parent(snapshot, revision_kind, changed=confirm_changed)
                ticket_reused = True
                # 纯改措辞不需要重排，但依据仍要按本轮行程校验：父方案的票在这一版
                # 还适不适用，不能靠「上次适用过」推断。
                ticket_facts = revalidate_facts(
                    inherited["ticket_facts"],
                    expect_date=expect_date,
                    origin=origin,
                    destination=destination,
                    return_date=_return_date(
                        confirmed,
                        query,
                        previous_query=previous_query,
                        expect_date=expect_date,
                        request_ctx=request_ctx,
                        query_is_current=intent_source != "background",
                    ),
                )
                ticket_records = inherited["ticket_raw"]
                ticket_diagnostics = list(inherited["ticket_diagnostics"])
                ticket_notes = list(inherited["ticket_notes"])
                ticket_reuse = _reuse_state(snapshot)
                ticket_context = ticket_markdown(
                    ticket_facts,
                    reused=True,
                    queried_at=str(inherited["ticket_queried_at"] or ""),
                    reuse_source=ticket_reuse["reuse_source"],
                    parent_reason=ticket_reuse["parent_reason"],
                )
                ticket_reason = (
                    "skipped" if ticket_reuse["parent_reason"] == "skipped" else "reused"
                )
                route_error = inherited["route_error"]
                search_error = inherited["search_error"]
            else:
                # 本轮重新查票：不继承任何旧状态，错误字段只由本次查询结果决定。
                # 返程日期与复用判断用同一个 _return_date 算法，避免「判定说区间没变、
                # 解析却按另一个返程日期算适用性」。
                ticket_facts, ticket_diagnostics = facts_from_records(
                    ticket_records,
                    expect_date=expect_date,
                    origin=origin,
                    destination=destination,
                    return_date=_return_date(
                        confirmed,
                        query,
                        previous_query=previous_query,
                        expect_date=expect_date,
                        request_ctx=request_ctx,
                        query_is_current=intent_source != "background",
                    ),
                )
                ticket_notes = unparsed_tool_notes(ticket_records)

                ticket_context = str(ticket_result.get("summary") or "")
                ticket_reason = resolve_ticket_state(
                    need_ticket=need_ticket,
                    ticket_mounted=ticket_agent is not None and bool((ticket_agent or {}).get("tools")),
                    has_ticket_result=bool(applicable_facts(ticket_facts)),
                    timed_out=bool(timed_out or ticket_result.get("timed_out")),
                )

            hints = parse_candidates(map_context) if need_map else []
            # 依据只算这一次：票务事实已定，旧依据归一化、本轮失效判断、本轮选车
            # 与窗口派生都在 _resolve_plan_basis 里完成，后面各处（行程、正文、快照、
            # 质量）都引用这同一份结果，避免「地图已按新车次重排、正文还写旧车次」。
            plan_basis, window = _apply_basis()
            if need_map and (hints or os.getenv("TRAVEL_MAP_FALLBACK") != "0"):
                yield {"type": "step", "id": "facts", "status": "active"}
                built = await assemble_itinerary(
                    city=dest or "",
                    days=day_count,
                    date=travel_date,
                    lodging=stay,
                    transport_mode=transport_mode,
                    must_visit=must_visit,
                    avoid=avoid,
                    hints=hints,
                    query_text=query,
                    cancel_event=cancel_event,
                    attempt_budget=budget,
                    stats=amap_stats,
                    plan_basis=plan_basis,
                    rail_window=window,
                    lodging_scope=request_ctx.get("lodging_scope"),
                    pace=confirmed.pace if confirmed is not None else None,
                    preferences=confirmed.preferences if confirmed is not None else None,
                    need_route=need_route,
                )
                yield {"type": "step", "id": "facts", "status": "done"}
                yield {"type": "step", "id": "schedule", "status": "active"}
                itinerary = built["plan"]
                # 行程按当前规则真正重建，解除旧口径「待复核」标记。
                itinerary_needs_recheck = False
                facts_status = built["facts_status"]
                search_error = built["search_error"]
                route_error = built["route_error"]
                saved_candidates = built["candidates"]
                saved_constraints = built["constraints"]
                saved_facts = built["facts"]
                unlocated = list(built.get("unlocated") or [])
                saved_search_usage = built["search_usage"]
                assemble_stats = built.get("stats")
                # 「不要地图」只否决产物，不否决结构化行程：地图任务数据照旧用于
                # 行程表与正文，只是不写 HTML、也不再声称已生成地图。
                if built["tasks"] and need_map_artifact:
                    map_file_now.write_text(render_map_html(built["tasks"]), encoding="utf-8")
                map_context = built["context"]
                route_source = "built"
                if not hints:
                    logger.warning("map_fallback plan_id=%s", plan_id)
                    yield {"type": "status", "message": "地图子智能体未写出文件，已用高德检索补写路线地图"}
                sights_missing = _sight_entries(built["unlocated"])
                if sights_missing:
                    yield {
                        "type": "warning",
                        "code": "candidate_unlocated",
                        "message": "有景点未能在高德定位：" + "、".join(item["name"] for item in sights_missing),
                    }
                for note in _lodging_warnings(built["unlocated"]):
                    yield {"type": "warning", **note}
                dropped_hints = list(built.get("dropped_hints") or [])
                if dropped_hints:
                    yield {
                        "type": "warning",
                        "code": "candidates_dropped",
                        "message": "候选景点过多，只检索了前"
                        + f"{len(built.get('candidates') or [])} 个，其余 {len(dropped_hints)} 个没有检索",
                        "kind": "info",
                        "reason": "candidate_quota",
                    }
                route_states = dict(built.get("route_states") or {})
                if route_states:
                    # 键是 (起点, 终点, 模式, 日期, 出发分钟) 的元组，落盘前用统一编码器转成可序列化字符串。
                    saved_route_states = {encode_route_key(key): value for key, value in route_states.items()}
                constraints_now = built.get("constraints") or {}
                request_ctx = merge_request_context(
                    request_ctx,
                    {"lodging_kind": constraints_now.get("lodging_kind")},
                )
                input_pending, _state = _resolve_input_state(confirmed, request_ctx, party_size)
            if itinerary and itinerary.get("conflicts"):
                yield {
                    "type": "warning",
                    "code": "schedule_conflict",
                    "message": "必去项无法排入行程",
                }
            if itinerary is not None:
                yield {"type": "step", "id": "schedule", "status": "done"}
            map_reason = resolve_map_state(
                need_map=need_map,
                dispatched=map_dispatched,
                has_result=map_file_now.is_file(),
                timed_out=timed_out,
            )
            if not need_map_artifact and map_reason == "ok":
                # 用户只要行程不要地图产物：这一步在用户口径里就是「跳过」，
                # 不能因为 map_agent 跑了、POI 也拿到了就报 ok（前端会显示已出图）。
                map_reason = "skipped"
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
                    "message": "已调度 map_agent，但没有生成路线地图",
                }

            if ticket_reason == "missed":
                started = bool(ticket_result.get("started"))
                logger.warning("ticket_missed plan_id=%s started=%s", plan_id, started)
                if started:
                    yield {
                        "type": "warning",
                        "code": "ticket_empty",
                        "message": "已查票，但原始响应里没有可解析车次，不生成确定性票务事实",
                    }
                else:
                    yield {
                        "type": "warning",
                        "code": "ticket_not_dispatched",
                        "message": "本应查票但未调度 ticket_agent，汇总将禁止编造车次",
                    }
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "not_dispatched" if not started else "empty"}
            elif ticket_reason == "timeout":
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "dispatch_timeout"}
            elif ticket_reason == "unavailable":
                yield {"type": "step", "id": "ticket", "status": "skipped", "reason": "mcp_unavailable"}

        # —— 查询失败告警：两条路径共用 ——
        # 本轮真查失败沿用原来的短句；复用父方案带回的失败必须说明是「沿用」，
        # 否则读者会以为这次也查了、也失败了，与实际状态不符。
        if search_error:
            detail = _reused_error_reason(search_error) if ticket_reused else _error_reason(search_error)
            yield {
                "type": "warning",
                "code": "search_unavailable",
                "message": "开放规则未核实" + detail,
                "kind": "info",
                "reason": str(search_error),
            }
        if route_error:
            detail = _reused_error_reason(route_error) if ticket_reused else _error_reason(route_error)
            yield {
                "type": "warning",
                "code": "route_unverified",
                "message": "部分交通耗时未核实" + detail,
                "kind": "info",
                "reason": str(route_error),
            }

        if cancelled:
            return

        # —— 成文要用的结构化状态：一次算好，四处 _apply_structured 调用共用 ——
        # 正文里的「人数 / 住宿口径 / 返程是否已定」只认这些结构化状态：
        # 模型原文常把候选车次写成已锁定，或在人数已知时仍按 1 人估算。
        ticket_queried_at = _ticket_queried_at(ticket_records)
        if not ticket_queried_at and ticket_reused:
            # 父方案的原始响应可能没留下 queried_at，但快照上记着查询时间；
            # 复用时要如实带上，否则「沿用上次查询」这句话就成了无凭据的断言。
            ticket_queried_at = str((snapshot or {}).get("ticket_queried_at") or "")
        # 住宿锚点状态：只取本次行程产出的约束（复用分支就是父方案那份），
        # 没有 poi_id 时质量侧才能如实报「按区域规划、未确认酒店」。
        lodging_anchor = (saved_constraints or {}).get("lodging_poi") or {}
        lodging_state = {
            # 约束里有就以行程实际用到的为准；没有（例如本轮没出地图）再退回请求上下文。
            "scope": (saved_constraints or {}).get("lodging_scope") or request_ctx.get("lodging_scope"),
            "kind": (saved_constraints or {}).get("lodging_kind"),
            "precision": (saved_constraints or {}).get("lodging_precision"),
            "poi_id": lodging_anchor.get("poi_id"),
            "text": request_ctx.get("lodging_text"),
        }
        # 返程依据状态：已选车次 + 候选，落盘后下一轮能知道「当时可选哪些车」。
        return_basis = {
            "selected": (plan_basis or {}).get(INBOUND) is not None,
            "options": list(return_candidates or []),
        }
        party_count = resolve_party(confirmed, query, request_ctx=request_ctx, explicit=party_size)
        # 铁路口径：rail_required 说的是「这趟行程要不要铁路」，与调度标志 need_ticket
        # （这一轮连不连票务）不是一回事。复用分支根本不连票务，早期写法于是把
        # 「沿用旧票」写成了「本次不需要（未查票）」，读者会以为这趟没有铁路段。
        rail_state = {
            "required": bool(rail_req.get("outbound") or rail_req.get("inbound")),
            "outbound": bool(rail_req.get("outbound")),
            "inbound": bool(rail_req.get("inbound")),
            "queried": bool(need_ticket and not ticket_reused),
            "reused": bool(ticket_reused),
        }
        # 成文校验要用同一份结构化状态：正文里的人数和住宿、返程口径不能和事实打架。
        # 这里不读 quality——quality 要等成文之后才生成。
        consistency_state = {
            "party": party_count,
            "lodging_state": lodging_state,
            "plan_basis": plan_basis,
            "need_ticket": need_ticket,
            "date": confirmed.date if confirmed is not None else None,
            "days": confirmed.days if confirmed is not None else None,
            "rail_state": rail_state,
        }

        def _write_structured(text: str) -> str:
            """把正文里会写飘的章节换成结构化内容；只换来源，不改校验判据。"""
            return _apply_structured(
                text,
                itinerary=itinerary,
                ticket_facts=ticket_facts,
                ticket_reason=ticket_reason,
                ticket_reused=ticket_reused,
                queried_at=ticket_queried_at,
                confirmed=confirmed,
                need_ticket=need_ticket,
                query=query,
                request_ctx=request_ctx,
                party_explicit=party_size,
                pending_items=input_pending,
                plan_basis=plan_basis,
                rail_window=window,
                lodging_state=lodging_state,
                party=party_count,
                return_pending=return_candidates,
                rail_state=rail_state,
                reuse_source=ticket_reuse["reuse_source"],
                parent_reason=ticket_reuse["parent_reason"],
                unlocated=unlocated,
                candidates=saved_candidates,
            )

        # —— 流式汇总成文（主智能体不再写长方案）——
        yield {"type": "step", "id": "summary", "status": "active"}
        yield {"type": "status", "message": "正在汇总最终方案…"}
        t_summary = time.monotonic()

        answer = "本次没有生成最终结果"
        gate = "pass"
        summary_deadline = time.monotonic() + budget.timeout_for(budget.summary)
        # 成文用的白名单和闸门必须来自同一份结构化行程，否则「模型允许写什么」与
        # 「事后检查写了什么」会各算一套。candidates 用本轮最终候选（复用轮继承而来）。
        poi_whitelist = itinerary_whitelist(itinerary, unlocated=unlocated, candidates=saved_candidates)
        summary_kwargs = dict(
            llm=ensure_llm(),
            query=query,
            slots=confirmed,
            map_context=map_context,
            ticket_context=ticket_context,
            ticket_reason=ticket_reason,
            map_reason=map_reason,
            plan_ready=itinerary is not None,
            previous_answer=previous_answer or None,
            revision_forbid_old_tickets=revision_kind == "ticket_and_map",
            whitelist=poi_whitelist,
        )
        issues: list[str] = []
        can_write = budget.work_left() > 0 and (summary_deadline - time.monotonic()) > 0 and budget.allow_model(
            "summary", current_usage().total_tokens()
        )
        if not can_write:
            answer = _write_structured(
                degraded_plan(
                    ticket_reason,
                    ["工作时间已用完，未再调用模型成文"],
                    itinerary=itinerary,
                    unlocated=unlocated,
                    candidates=saved_candidates,
                )
            )
            gate = "degraded"
            yield {
                "type": "warning",
                "code": "summary_degraded",
                "message": "工作时间已用完，未再调用模型，已用已有数据收尾",
            }
        else:
            for attempt in (1, 2):
                left = summary_deadline - time.monotonic()
                if left <= 0 or not budget.allow_model("summary", current_usage().total_tokens()):
                    answer = _write_structured(
                        answer if attempt == 2 else degraded_plan(
                            ticket_reason,
                            ["成文阶段时间已用完"],
                            itinerary=itinerary,
                            unlocated=unlocated,
                            candidates=saved_candidates,
                        )
                    )
                    gate = "degraded"
                    break
                if attempt == 2:
                    yield {"type": "status", "message": "成文未通过校验，带约束重试一次"}
                answer = "本次没有生成最终结果"
                feedback = None
                if attempt == 2:
                    feedback = (
                        "上次成文未通过校验："
                        + "；".join(issues)
                        + "。必须输出六个 Markdown 二级标题（需求摘要、景点建议、车票建议、预算、行程表、注意事项）。"
                        "无可靠票务时，车票建议中禁止出现车次号。"
                    )
                budget.note_model("summary")
                try:
                    async for event in svc.stream_summary(**summary_kwargs, gate_feedback=feedback, timeout_seconds=left):
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            break
                        if event.get("type") == "_summary_done":
                            answer = str(event.get("content") or answer)
                        else:
                            yield event
                except TimeoutError:
                    answer = degraded_plan(
                        ticket_reason,
                        ["成文阶段超时，已停止等待模型输出"],
                        itinerary=itinerary,
                        unlocated=unlocated,
                        candidates=saved_candidates,
                    )
                    gate = "degraded"
                    yield {
                        "type": "warning",
                        "code": "summary_degraded",
                        "message": "成文阶段超时，已用已有数据收尾",
                    }
                if cancelled:
                    return
                answer = _write_structured(answer)
                if gate == "degraded":
                    break
                issues = summary_gate_issues(answer, ticket_reason)
                # 景点这一节的事实约束单独一道闸门：车次号能被正则挡住，景点名、
                # 游玩耗时和先后顺序却不能，只能拿结构化行程当白名单逐项比。
                issues += poi_gate_issues(answer, poi_whitelist, plan_ready=itinerary is not None)
                # 同一篇正文里的人数和住宿/返程口径也要对得上：结构化状态说 2 人，
                # 正文不能还写「按 1 人估算」；返程没选定，不能写成「已锁定」。
                issues += state_consistency_issues(answer, consistency_state)
                if not issues:
                    gate = "retry_pass" if attempt == 2 else "pass"
                    break
            else:
                answer = _write_structured(degraded_plan(
                    ticket_reason,
                    issues,
                    itinerary=itinerary,
                    unlocated=unlocated,
                    candidates=saved_candidates,
                ))
                gate = "degraded"
                yield {
                    "type": "warning",
                    "code": "summary_degraded",
                    "message": "成文两次未通过校验，已降级为不含编造车次的说明",
                }

        summary_ms = ms_since(t_summary)
        yield {"type": "step", "id": "summary", "status": "done", "elapsed_ms": summary_ms}
        visible_facts = applicable_facts(ticket_facts) if ticket_reason in {"ok", "reused"} else []
        count = resolve_party(confirmed, query, request_ctx=request_ctx, explicit=party_size)
        _budget_text, budget_status = budget_markdown(
            visible_facts,
            user_budget=confirmed.budget if confirmed is not None else None,
            days=confirmed.days if confirmed is not None else None,
            need_ticket=ticket_reason != "skipped",
            party_known=count is not None,
            party=count,
            budget_scope=request_ctx.get("budget_scope"),
        )
        opening_verified, opening_required = opening_counts(
            itinerary,
            saved_facts if isinstance(saved_facts, list) else [],
            unlocated,
        )
        legs_referenced, legs_verified, legs_required = leg_counts(itinerary)
        schedule_state = schedule_check(itinerary, unlocated)
        # 行程沿用旧口径（未按当前规则重建）时，时刻与耗时只能当「未核实」，连参考都算不上：
        # 旧口径的耗时不是本轮取得的、也无法证明适用于当前日期/时段。
        reused_legacy = bool(itinerary_needs_recheck)
        if reused_legacy:
            legs_referenced = 0
            legs_verified = 0
            schedule_state = "unverified"
        quality = build_quality(
            summary_degraded=gate == "degraded",
            ticket_applicable=ticket_reason != "skipped",
            ticket_facts=visible_facts,
            ticket_reused=ticket_reused,
            opening_required=opening_required,
            opening_verified=opening_verified,
            legs_required=legs_required,
            legs_verified=legs_verified,
            legs_referenced=legs_referenced,
            schedule_check=schedule_state,
            budget_status=budget_status,
            pending=input_pending,
        )
        # 复用父方案时的「上一版票务结论」：只说「余票未刷新」会把「上次其实查失败
        # 或一条都不适用」也盖成同一句话。父方案的状态与失败说明原样带上，交给质量侧
        # 区分「沿用成功结果」与「沿用失败结果」。
        parent_reason = ticket_reuse["parent_reason"] if ticket_reused else ""
        parent_diagnostics: list[dict] = []
        if ticket_reused:
            for item in (snapshot or {}).get("ticket_diagnostics") or []:
                if isinstance(item, dict):
                    parent_diagnostics.append(dict(item))
            if not parent_diagnostics:
                for item in ((snapshot or {}).get("quality") or {}).get("notes") or []:
                    if isinstance(item, dict) and item.get("message"):
                        parent_diagnostics.append(dict(item))
        quality["issues"] = collect_issues(
            checks=quality["checks"],
            ticket_diagnostics=ticket_diagnostics,
            summary_degraded=gate == "degraded",
            unlocated=unlocated,
            ticket_applicability=applicability_note(ticket_facts),
            # reason 必须是 resolve_ticket_state 的语义值（ok/reused/skipped/...），
            # 不是 checks.ticket：前者决定「沿用」与「未查」的区分，后者只是校验结果。
            ticket_state={
                "reason": ticket_reason,
                "queried_at": ticket_queried_at if ticket_reused else "",
                "facts_count": len(ticket_facts or []),
                "applicable_count": len(applicable_facts(ticket_facts)),
                "diagnostics": list(ticket_diagnostics),
                "parent_reason": parent_reason,
                "parent_class": ticket_reuse["parent_class"] if ticket_reused else "",
                "reuse_source": ticket_reuse["reuse_source"] if ticket_reused else "",
                "parent_diagnostics": parent_diagnostics,
            },
            itinerary=itinerary,
            route_states=route_states,
            lodging_state=lodging_state,
            facts=saved_facts if isinstance(saved_facts, list) else [],
            return_options=return_candidates,
            plan_basis=plan_basis,
            rail_window=window,
        )
        notes = list(ticket_notes)
        if reused_legacy:
            notes.append(
                {
                    "code": "reused_legacy_snapshot",
                    "message": "沿用旧方案，时刻与耗时未按新口径复核",
                }
            )
        quality["notes"] = notes
        quality["route_error"] = route_error
        quality["search_error"] = search_error
        quality["ticket_applicability"] = applicability_note(ticket_facts)
        quality["schedule_density"] = [
            {"day": day.get("day"), "date": day.get("date"), "label": day.get("label"), "density": day.get("density")}
            for day in (itinerary or {}).get("days") or []
            if isinstance(day.get("density"), dict)
        ]
        quality["candidate_supply"] = dict((itinerary or {}).get("candidate_supply") or {})
        quality["pending"] = list(input_pending)
        quality["sources"] = _group_sources(
            ticket_records,
            [*(saved_facts if isinstance(saved_facts, list) else []), *visible_facts],
        )

        # —— 落盘 Markdown + 修订快照，带上耗时与地图 URL ——
        _saved_path, saved_url = save_plan(plan_id, query, answer)
        save_snapshot(
            {
                "plan_id": plan_id,
                "parent_plan_id": parent_id,
                "query": query,
                "slots": confirmed.model_dump() if confirmed is not None else None,
                "ticket_reason": ticket_reason,
                # 复用三元状态一并落盘：下一轮再复用时，父方案的 ticket_reason
                # 已经是 reused，只有这里存着的原始值还能追到最初那次查询的结论。
                "ticket_reuse": dict(ticket_reuse),
                "map_context": map_context,
                "ticket_context": ticket_context,
                "answer": answer,
                "usage": current_usage().as_dict(),
                "schema_version": 4,
                "quality": quality,
                "ticket_facts": ticket_facts,
                "plan_basis": plan_basis,
                "rail_window": window,
                "ticket_raw": ticket_records,
                "ticket_summary": ticket_context,
                "ticket_queried_at": ticket_queried_at,
                "itinerary": itinerary,
                "itinerary_needs_recheck": itinerary_needs_recheck,
                "facts_status": facts_status,
                "schedule_status": (itinerary or {}).get("schedule_status"),
                "search_error": search_error,
                "route_error": route_error,
                "candidates": saved_candidates,
                "constraints": saved_constraints,
                "facts": saved_facts,
                "search_usage": saved_search_usage,
                "route_states": saved_route_states,
                # 未定位项与票务诊断、说明：下一版本轮复用时要能原样带回，
                # 否则复用后这些状态会凭空消失（地图、正文、质量会自相矛盾）。
                "unlocated": unlocated,
                "ticket_diagnostics": ticket_diagnostics,
                "ticket_notes": ticket_notes,
                # 住宿锚点状态与返程候选：供下一轮复用与「返程依据待定」提示。
                "lodging_state": lodging_state,
                "return_basis": return_basis,
                "request_context": request_ctx,
                "input_pending": input_pending,
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
            "total_ms": ms_since(t_total),
            "ticket_cache_hit": ticket_cache_hit,
            "parallel_dispatch": bool(parallel_tasks and parallel_results),
            "summary_gate": gate,
            "outcome": "finished",
            "tool_attempts": budget.tool_attempts,
            "run_stats": stats.as_dict(),
            **current_usage().as_dict(),
        }
        if map_ms is not None:
            timings["map_ms"] = map_ms
        if ticket_ms is not None:
            timings["ticket_ms"] = ticket_ms
        if delta_ms is not None:
            timings["map_ticket_active_delta_ms"] = delta_ms
        if need_map:
            timings["map_model_calls"] = budget.model_calls.get("map", 0)
            timings["poi_requests"] = amap_stats["poi_requests"]
            timings["route_requests"] = amap_stats["route_requests"]
            timings["amap_rate_limits"] = amap_stats["amap_rate_limits"]
        if assemble_stats:
            for key in ("assemble_ms", "poi_verify_ms", "facts_ms", "route_ms", "schedule_ms"):
                timings[key] = assemble_stats[key]
        final_event: dict[str, Any] = {
            "type": "final",
            "content": answer,
            "saved_url": saved_url,
            "plan_id": plan_id,
            "timings": timings,
            "quality": quality,
            # 口径字段不在 TravelSlots 里，前端重开补充表单时需要用它还原当前方案的真实口径。
            "request_context": {
                "party_size": request_ctx.get("party_size"),
                "budget_scope": request_ctx.get("budget_scope"),
                "lodging_scope": request_ctx.get("lodging_scope"),
                "intercity_mode": request_ctx.get("intercity_mode"),
                "return_required": request_ctx.get("return_required"),
            },
        }
        if map_file.is_file():
            final_event["map_url"] = f"/results/maps/{plan_id}.html"
        trace_url = None
        if (os.getenv("LANGSMITH_API_KEY") or "").strip():
            try:
                trace_url = await asyncio.wait_for(asyncio.to_thread(build_trace_url, plan_id), timeout=2)
            except TimeoutError:
                trace_url = None
        if trace_url:
            final_event["trace_url"] = trace_url
        # 先收尾再发 final：清理结果必须随 final 与 SQLite 规划记录一起落地，
        # 不能留到 finally 里只写日志/notes（timings.run_stats 是 as_dict() 拷贝）。
        # 收尾后重新取一次 run_stats，让子任务残留等 notes 也进入已发出的 final。
        cleanup = await _finalize_cleanup()
        timings["cleanup"] = cleanup
        timings["run_stats"] = stats.as_dict()
        final_event["cleanup"] = cleanup
        await record_plan(
            plan_id=plan_id,
            query=query,
            slots=confirmed.model_dump() if confirmed is not None else None,
            status=str(quality["status"]),
            timings=timings,
            artifact=saved_url,
            ok=quality["status"] == "complete",
            outcome="finished",
            quality=quality,
            usage=current_usage().as_dict(),
        )
        yield final_event
    except asyncio.CancelledError:
        if cancel_event is not None and cancel_event.is_set():
            # 客户端断开/取消：把「取消」写成显式状态，别让 SSE 观测里只剩空统计。
            stats.note("sse", "cancelled")
            return
        raise
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            return
        logger.error("plan_id=%s phase=failed error=%s", plan_id, type(exc).__name__, exc_info=True)
        # 失败路径也要带 run_stats 与 cleanup：否则排查时看不出零调用到底是没发生
        # 还是时序断了，也看不出子任务/MCP 是否真的收干净了。
        cleanup = await _finalize_cleanup()
        await record_plan(
            plan_id=plan_id,
            query=query,
            slots=confirmed.model_dump() if confirmed is not None else None,
            status="error",
            timings={
                "total_ms": ms_since(t_total),
                "tool_attempts": budget.tool_attempts,
                "run_stats": stats.as_dict(),
                "cleanup": cleanup,
                **current_usage().as_dict(),
            },
            artifact=None,
            ok=False,
            outcome="error",
            usage=current_usage().as_dict(),
        )
        yield {"type": "error", "message": str(exc), "cleanup": cleanup}
    finally:
        # 幂等兜底：正常路径已在发 final 前收尾；SSE 总超时或断连时由
        # watch_aiter 的 agen.aclose() 触发这里，把清理结果写进共享 RunStats，
        # 供 server 的 timeout 分支补进 error 事件。不在这里裸调
        # close_ticket_agent，避免同一连接被关闭两次。
        await _finalize_cleanup()
