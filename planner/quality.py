"""落盘前的质量状态。流程完成和可靠完成分开计数。"""

from __future__ import annotations

from datetime import date
from typing import Any


def _coverage(verified: int, required: int) -> dict[str, Any]:
    if required <= 0:
        return {"verified": 0, "required": 0, "label": "不适用"}
    return {"verified": verified, "required": required, "label": f"{verified}/{required}"}


def _travel_coverage(verified: int, referenced: int | None, required: int) -> dict[str, Any]:
    """交通耗时把「已核实」与「参考（取得结果但未证明适用性）」分开统计。"""
    if required <= 0:
        return {"verified": 0, "referenced": 0, "required": 0, "label": "不适用"}
    ref = verified if referenced is None else referenced
    if verified >= required:
        label = f"已核实 {verified}/{required}"
    else:
        label = f"参考 {ref}/{required}"
    return {"verified": verified, "referenced": ref, "required": required, "label": label}


def build_quality(
    *,
    summary_degraded: bool,
    ticket_applicable: bool,
    ticket_facts: list[dict],
    ticket_reused: bool,
    opening_required: int,
    opening_verified: int,
    legs_required: int,
    legs_verified: int = 0,
    legs_referenced: int | None = None,
    schedule_check: str,
    budget_status: str,
    issues: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> dict[str, Any]:
    checks = {
        "ticket": "not_required" if not ticket_applicable else _ticket_status(ticket_facts, ticket_reused),
        "opening_rules": _ratio_status(opening_verified, opening_required),
        "travel_time": _travel_status(legs_required, legs_verified, legs_referenced),
        "schedule": schedule_check if schedule_check in {"verified", "unverified", "conflict", "not_required"} else "unverified",
        "budget": budget_status if budget_status in {"verified", "unverified", "conflict", "not_required"} else "unverified",
    }
    if not ticket_applicable:
        checks["ticket"] = "not_required"
    found = list(issues or [])
    waiting = list(pending or [])
    if summary_degraded:
        status = "degraded"
    elif any(value in {"unverified", "conflict"} for value in checks.values()):
        status = "partial"
    elif any(value == "reference" for value in checks.values()):
        # 参考耗时只是「取得了结果」，无法证明适用于规划日期/时段，不能算完整。
        status = "partial"
    elif waiting:
        # 有「等用户确认」的项时不能算完整：确认之前方案只是草稿。
        # 但 checks 保持原样，不为了下调状态伪造 unverified。
        status = "partial"
    else:
        status = "complete"
    return {
        "status": status,
        "checks": checks,
        "issues": found,
        "pending": waiting,
        "coverage": {
            "opening_rules": _coverage(opening_verified, opening_required),
            "travel_time": _travel_coverage(legs_verified, legs_referenced, legs_required),
        },
    }


def _ticket_status(facts: list[dict], reused: bool) -> str:
    """只看适用于本方案的事实：解析成功但区间/日期不适用，仍算未核实。"""
    applied = [fact for fact in facts if (fact.get("value") or {}).get("applied", True)]
    if reused or not applied:
        return "unverified"
    if all(fact.get("status") == "verified" and fact.get("source_id") for fact in applied):
        return "verified"
    return "unverified"


def _ratio_status(verified: int, required: int) -> str:
    if required <= 0:
        return "not_required"
    if verified >= required:
        return "verified"
    return "unverified"


def _travel_status(required: int, verified: int, referenced: int | None) -> str:
    """交通耗时的核实程度：verified / reference / unverified / not_required。

    - verified：每一段都有可证实的适用性依据（当前只有复用已验证行程可达）。
    - reference：每段都取得了工具结果，但结果未带规划日期/时段，只能作参考。
    - unverified：存在缺失或失败的路段。
    """
    if required <= 0:
        return "not_required"
    if verified >= required:
        return "verified"
    ref = verified if referenced is None else referenced
    if ref >= required:
        return "reference"
    return "unverified"


UNLOCATED_KINDS = ("sight", "must_visit", "lodging", "station")


def _unlocated_entries(unlocated: list | None) -> list[dict]:
    """未定位项可以是结构化记录，也兼容裸字符串（旧快照与既有调用方）。"""
    entries: list[dict] = []
    for item in unlocated or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "sight").strip() or "sight"
            reason = str(item.get("reason") or "")
            asked = str(item.get("asked_name") or "")
        else:
            name = str(item or "").strip()
            kind, reason, asked = "sight", "", ""
        if not name:
            continue
        entries.append({"name": name, "kind": kind if kind in UNLOCATED_KINDS else "sight", "reason": reason, "asked_name": asked})
    return entries


def unlocated_names(unlocated: list | None) -> list[str]:
    """只供展示与旧接口：语义判断请用 unlocated_by_kind。"""
    return [entry["name"] for entry in _unlocated_entries(unlocated)]


def unlocated_by_kind(unlocated: list | None, kind: str) -> list[dict]:
    return [entry for entry in _unlocated_entries(unlocated) if entry["kind"] == kind]


def opening_units(itinerary: dict | None, facts: list[dict] | None = None, unlocated: list | None = None) -> list[tuple[str, date | None]]:
    """统计单位是「景点 + 实际游览日」。未定位和未能排入的必去项日期为 None，但留在分母里。

    这是 `opening_counts` 与 `schedule_details` 的唯一口径来源：
    覆盖率与可展开明细必须用同一份单元，不能各自从总数反推。
    `facts` 参数只为兼容既有调用签名，取单元本身不需要它。
    """
    units: list[tuple[str, str | None]] = []
    for day in (itinerary or {}).get("days") or []:
        when = str(day.get("date") or "")
        for item in day.get("items") or []:
            if item.get("role") == "visit":
                units.append((str(item.get("poi_id") or item.get("name") or ""), when))
    # 住宿与接驳端点不是游览项，不进开放规则分母。
    for entry in _unlocated_entries(unlocated):
        if entry["kind"] in {"sight", "must_visit"}:
            units.append((entry["name"], None))
    for item in (itinerary or {}).get("conflicts") or []:
        units.append((str(item.get("name") or ""), None))
    resolved: list[tuple[str, date | None]] = []
    for poi_id, when in dict.fromkeys(units):
        if not poi_id:
            continue
        resolved.append((poi_id, date.fromisoformat(when) if when else None))
    return resolved


def opening_counts(itinerary: dict | None, facts: list[dict] | None, unlocated: list | None) -> tuple[int, int]:
    """已验证数 / 单元总数。口径与 `opening_units` 完全一致。"""
    from planner.route_plan import _fact_applies

    units = opening_units(itinerary, facts, unlocated)
    if not units:
        return 0, 0
    verified = 0
    for poi_id, day in units:
        if day is None:
            continue
        if any(_opening_fact(fact, poi_id, day, _fact_applies) for fact in facts or []):
            verified += 1
    return verified, len(units)


def _opening_fact(fact: dict, poi_id: str, day: Any, applies) -> bool:
    if fact.get("status") != "hard" or fact.get("kind") not in {"opening_hours", "last_entry"}:
        return False
    if str(fact.get("poi_id") or "") != poi_id:
        return False
    return bool(applies(fact, day))


def leg_counts(itinerary: dict | None) -> tuple[int, int, int]:
    """返回 (referenced, verified, required)。

    - referenced：取得了可用工具结果（result_state == "ok" 且 duration_seconds 为正）的路段数，
      只代表「有参考耗时」，不代表已核实。
    - verified：只有带完整适用性依据（confidence == "verified"）的路段才计入，
      与取得结果无关；当前路线接口不带日期/时段，正常查询结果一律 reference。
    - required：所有真实路段（origin_id != dest_id 的占位路段不计）的总数。
    """
    required = 0
    referenced = 0
    verified = 0
    for day in (itinerary or {}).get("days") or []:
        for leg in day.get("legs") or []:
            if str(leg.get("origin_id") or "") == str(leg.get("dest_id") or ""):
                # 同一地点间的空跑是占位，不参与覆盖率统计。
                continue
            required += 1
            seconds = leg.get("duration_seconds")
            if isinstance(seconds, int) and seconds > 0:
                referenced += 1
            if leg.get("confidence") == "verified" and isinstance(seconds, int) and seconds > 0:
                verified += 1
    return referenced, verified, required


def schedule_check(itinerary: dict | None, unlocated: list | None) -> str:
    # 只有必去项没定位才影响排程结论；普通景点或住宿未定位不算排程冲突。
    if unlocated_by_kind(unlocated, "must_visit") or (itinerary or {}).get("conflicts"):
        return "conflict"
    if not itinerary:
        return "unverified"
    if str(itinerary.get("schedule_status") or "") != "checked_under_assumptions":
        return "unverified"
    if any(day.get("unverified") for day in itinerary.get("days") or []):
        return "unverified"
    return "verified"


_ISSUE_TEXT = {
    "unrecognized_ticket_payload": "票务响应无法识别，未生成车次事实",
    "ticket_empty": "本次查询条件下未找到车次",
    "ticket_mismatch": "票务记录与出行日期或区间不一致",
    "ticket_not_applicable": "本次查到了车次，但区间或日期不适用于本方案",
    "summary_degraded": "成文未通过校验，已改为确定性说明",
    "opening_unverified": "有景点在实际游览日没有适用的开放规则",
    "opening_missing": "尚无适用于游览日期的开放规则",
    "travel_unverified": "有路段没有与所选交通方式匹配的耗时",
    "travel_reference": "交通耗时来自路线查询，未按时段核验（参考）",
    # 明细文案会按实际交通方式替换成「步行/驾车/换乘…」，这里是兜底。
    "travel_leg_unknown": "耗时未取得",
    "schedule_conflict": "必去项未排入，或与已知闭馆冲突",
    "schedule_unverified": "排程仍有未知条件",
    "rail_window_unknown": "抵离时刻未确定，首末两天的可用时间窗口无法核实",
    "budget_unverified": "人数与口径已明确，但市内交通、门票等费用仍未知，不能宣称预算达标",
    "budget_conflict": "已核实费用超过预算",
    "ticket_unverified": "方案依赖铁路，但没有可引用的有效车次",
    "ticket_reused": "沿用的车次查询结果余票未刷新，不是当前余票",
    "candidate_unlocated": "有景点未能在高德定位，没有进入候选池",
    "candidate_shortage": "当前候选池不足以补足部分完整游览日",
    "schedule_sparse": "当天游览活动偏少，建议补充同片区活动",
    "lodging_area_pending": "住宿只给到区域，需要你选：填具体酒店，或按该区域参考规划",
    "lodging_unresolved": "没能在高德匹配到你给的住宿，出发与返回点按目的地中心暂代",
    "lodging_anchor_missing": "住宿区域参考点尚未建立，住宿往返接驳没有纳入排程",
    "return_basis_pending": "已查到返程候选车次，请选择返程车次或返程时段，以确定末日最晚结束时间",
}

_ISSUE_KIND = {
    "unrecognized_ticket_payload": "warning",
    "ticket_empty": "warning",
    "ticket_mismatch": "warning",
    "ticket_not_applicable": "warning",
    "ticket_unverified": "warning",
    "ticket_reused": "info",
    "summary_degraded": "warning",
    "travel_unverified": "warning",
    "travel_reference": "info",
    "travel_leg_unknown": "warning",
    "schedule_conflict": "warning",
    "schedule_unverified": "info",
    # 抵离时刻未知会直接压缩首末两天的可排时间，属于需要重查车次才能解决的系统项。
    "rail_window_unknown": "warning",
    "budget_conflict": "warning",
    "opening_unverified": "info",
    "opening_missing": "info",
    "candidate_unlocated": "info",
    "candidate_shortage": "warning",
    "schedule_sparse": "info",
    "opening_rules": "info",
    "lodging_area_pending": "pending",
    "lodging_unresolved": "pending",
    # 费用未知属于系统待核实，不是用户待确认：人数/预算口径缺失由
    # slots.draft_pendings() 的 party_unknown / budget_scope_unknown（带 action、
    # 进 quality.pending）承担。「市内交通、门票等费用未知」用户无法回答。
    "budget_unverified": "info",
    "return_basis_pending": "info",
    "lodging_anchor_missing": "info",
}

_ISSUE_ACTION = {
    "lodging_area_pending": "confirm_lodging",
    "lodging_unresolved": "confirm_lodging",
    "ticket_reused": "recheck_tickets",
    "travel_leg_unknown": "rerun_route",
    "opening_missing": "view_opening",
    "return_basis_pending": "select_return",
    "lodging_anchor_missing": "retry_lodging",
}

# 责任方与提示级别正交：kind 说「多严重」，owner 说「谁来处理」。
# 用户能回答的才归 user；其余一律 system，页面据此分「需要你补充 / 系统待核实」。
_ISSUE_OWNER = {
    "lodging_area_pending": "user",
    "lodging_unresolved": "user",
    # 住宿只给到区域、没有参考点时，补充更具体的住宿信息是用户能做的事，
    # 因此与上面两项一样归 user（task-2 第 5 条的明确要求）。
    "lodging_anchor_missing": "user",
    "return_basis_pending": "user",
    "budget_unknown_input": "user",
}


def _issue(code: str, *, ref: Any = None, **extra: Any) -> dict:
    return {
        "code": code,
        "message": _ISSUE_TEXT.get(code, code),
        "ref": ref,
        "kind": _ISSUE_KIND.get(code, "info"),
        "action": _ISSUE_ACTION.get(code),
        "owner": _ISSUE_OWNER.get(code, "system"),
        **extra,
    }


def _day_label(when: date | None) -> str:
    """中文可读日期，例如「11 月 8 日」。"""
    if when is None:
        return ""
    return f"{when.month} 月 {when.day} 日"


_ROUTE_REASON_TEXT = {
    "budget": "路线查询预算用尽",
    "rate_limit": "路线接口限流",
    "request_failed": "路线请求失败",
    "no_route": "没有可用路线",
    "not_queried": "尚未查询",
}


def _route_index(route_states: dict | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    """把 route_states 归一成 {(起点, 终点, 日期): {模式: 状态值}}。

    内存里键是 `route_cache_key` 元组，落盘的快照是 `encode_route_key` 字符串，
    两种都要认，否则复用旧快照时明细会全部丢掉。
    """
    from planner.route_plan import decode_route_key

    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, value in (route_states or {}).items():
        parts: tuple | None
        if isinstance(key, (tuple, list)) and len(key) >= 5:
            parts = tuple(key)[:5]
        elif isinstance(key, str):
            parts = decode_route_key(key)
        else:
            parts = None
        if not parts:
            continue
        origin_id, dest_id, mode, day, _hour = (str(part) for part in parts)
        index.setdefault((origin_id, dest_id, day), {})[mode] = value
    return index


def _route_state_value(leg: dict, day: str, index: dict) -> Any:
    """按「本段自己的模式优先」取该路段的查询状态，取不到再看其它模式。"""
    by_mode = index.get((str(leg.get("origin_id") or ""), str(leg.get("dest_id") or ""), day)) or {}
    if not by_mode:
        return None
    mode = str(leg.get("mode") or "")
    order = [mode, "transfer", "walking", "driving"]
    for name in order:
        if name and name in by_mode:
            return by_mode[name]
    return next(iter(by_mode.values()))


def _route_state_text(state: Any) -> str:
    """状态值的原始文本，只用于技术明细展示。"""
    if isinstance(state, dict):
        return str(state.get("state") or "")
    if isinstance(state, str):
        return state
    return ""


def _route_reason(state: Any) -> str:
    """把状态值翻成失败原因。只写进 detail，不改变聚合告警的文案。"""
    if isinstance(state, dict):
        for key in ("reason", "error", "state"):
            text = str(state.get(key) or "")
            if text:
                return text
        return ""
    text = str(state or "")
    if text == "failed":
        # states 只记 failed，具体是限流还是请求失败由 route_error 汇总；
        # 路段级只能如实写作请求失败。
        return "request_failed"
    if text in {"no_route", "not_queried", "budget", "rate_limit"}:
        return text
    return ""


def _leg_seconds_ok(seconds: Any) -> bool:
    return isinstance(seconds, int) and seconds > 0


# 交通方式的中文说法，只影响文案；缺省用「交通」兜底，不猜具体方式。
_MODE_TEXT = {
    "walking": "步行",
    "driving": "驾车",
    "public_transit": "公共交通",
    "transit": "公共交通",
    "transfer": "换乘",
}


def _mode_text(mode: Any) -> str:
    return _MODE_TEXT.get(str(mode or "").strip().lower(), "交通")


# 明细覆盖哪一类聚合告警：每个明细只压掉自己解释得了的那一类。
# travel_leg_unknown 能解释「交通耗时未核实」，但不能替开放规则或排程结论说话。
_DETAIL_COVERS = {
    "travel_leg_unknown": ("travel_time",),
    "opening_missing": ("opening_rules",),
    "rail_window_unknown": ("schedule",),
    "candidate_shortage": ("schedule",),
    "schedule_sparse": ("schedule",),
}

# 聚合 code → 它所属的类别。只有该类别已有明细时才省略聚合项。
_AGGREGATE_CATEGORY = {
    "opening_unverified": "opening_rules",
    "travel_unverified": "travel_time",
    "schedule_unverified": "schedule",
}


def _rail_window_details(
    rail_window: dict | None,
    days: list[dict],
    return_options: list | None = None,
) -> list[dict]:
    """抵离时刻未知时给出明细：首日最早开始 / 末日最晚结束定不下来。

    `rail_window` 来自 `planner.ticket_facts.rail_window`，每个方向可能是
    `known` / `unknown` / `not_constrained`。只有 `unknown` 才是「该知道却没拿到」，
    这时行程可能排不下，必须单独说明，不能只留一句「排程仍有未知条件」。
    没有传 `rail_window` 时退回看每天自己的 window 状态，避免旧调用方丢失明细。
    """
    details: list[dict] = []
    if isinstance(rail_window, dict):
        for direction in ("outbound", "inbound"):
            item = rail_window.get(direction)
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") != "unknown":
                continue
            basis = item.get("basis")
            basis = basis if isinstance(basis, dict) else {}
            train_no = str(basis.get("train_no") or basis.get("train") or "")
            first = direction == "outbound"
            if train_no and first:
                message = f"已选去程车次 {train_no} 缺少可用的抵达时刻：首日最早开始时间未知。"
            elif train_no:
                message = f"已选返程车次 {train_no} 缺少可用的出发时刻：末日最晚结束时间未知。"
            elif first:
                message = "去程车次尚未确定：首日最早开始时间未知，首日行程可能排不下。"
            elif return_options:
                # 候选已经查到但用户没选：这一步该由用户回答，另有 return_basis_pending。
                message = "返程车次尚未选定：末日最晚结束时间未知，末日行程可能排不下。"
            else:
                message = "返程车次尚未确定：末日最晚结束时间未知，末日行程可能排不下。"
            details.append(
                _issue(
                    "rail_window_unknown",
                    ref=direction,
                    message=message,
                    direction=direction,
                    date=str(item.get("arrive_date") or item.get("depart_date") or ""),
                    detail={
                        "state": "unknown",
                        "reason": str(item.get("reason") or ""),
                        "train_no": train_no,
                        "first_day": str(rail_window.get("first_day") or ""),
                        "last_day": str(rail_window.get("last_day") or ""),
                    },
                )
            )
        # 拿到了窗口依据就以它为准：两个方向都不是 unknown 时不该再按天兜底报错。
        return details

    # 没有窗口依据时，按天的 window 状态兜底，至少能指出是哪一天。
    for day in days:
        window = day.get("window") or {}
        if not isinstance(window, dict) or str(window.get("state") or "") != "unknown":
            continue
        when = str(day.get("date") or "")
        label = _day_label(date.fromisoformat(when)) if when else ""
        details.append(
            _issue(
                "rail_window_unknown",
                ref=f"window@{when or 'unknown'}",
                message=f"{label}：当天可用时间窗口未知（抵离时刻未确定），行程可能排不下。",
                date=when,
                detail={"state": "unknown", "date": when},
            )
        )
    return details


def schedule_details(
    *,
    itinerary: dict | None,
    unlocated: list | None = None,
    route_states: dict | None = None,
    rail_window: dict | None = None,
    lodging_state: dict | None = None,
    facts: list[dict] | None = None,
    return_options: list | None = None,
    plan_basis: dict | None = None,
) -> list[dict]:
    """把「排程仍有未知条件」拆成可展开的明细。

    每条明细都带责任方（owner）与可执行动作（action），供页面分「需要你补充」
    与「系统待核实」。只有真的无法展开时才由 `collect_issues` 退回聚合项。

    `rail_window` 里 `outbound`/`inbound` 的 state 为 `unknown` 时说明抵离时刻
    没拿到：首日最早开始时间或末日最晚结束时间未知，行程可能排不下，这里单独
    列成 `rail_window_unknown`。为 None 时退回看每天 `window.state`。
    """
    from planner.route_plan import _fact_applies

    details: list[dict] = []
    days = (itinerary or {}).get("days") or []
    # 路段与开放规则只有 id：先把 poi_id → 名称建好，正文才不至于露出编号。
    # 未定位项也在里面，否则它们的提示会只剩一个查不到的 id。
    names: dict[str, str] = {}
    for day in days:
        for item in day.get("items") or []:
            poi_id = str(item.get("poi_id") or "")
            if poi_id:
                names.setdefault(poi_id, str(item.get("name") or poi_id))
    for entry in _unlocated_entries(unlocated):
        if entry["kind"] in {"sight", "must_visit"}:
            names.setdefault(entry["name"], entry["name"])
    index = _route_index(route_states)

    for day in days:
        when = str(day.get("date") or "")
        label = _day_label(date.fromisoformat(when)) if when else ""
        for leg in day.get("legs") or []:
            origin_id = str(leg.get("origin_id") or "")
            dest_id = str(leg.get("dest_id") or "")
            if origin_id == dest_id:
                # 同一地点间的空跑是占位，不参与覆盖率统计，也不报未知耗时。
                continue
            if _leg_seconds_ok(leg.get("duration_seconds")):
                continue
            state = _route_state_value(leg, when, index)
            reason = _route_reason(state)
            origin = names.get(origin_id, origin_id)
            dest = names.get(dest_id, dest_id)
            mode = str(leg.get("mode") or "")
            message = f"{label}：{origin} → {dest}，{_mode_text(mode)}耗时未取得"
            detail: dict[str, Any] = {
                "mode": mode,
                "depart": str(leg.get("depart") or ""),
                "state": _route_state_text(state),
                "reason": reason,
                "reason_text": _ROUTE_REASON_TEXT.get(reason, ""),
            }
            details.append(
                _issue(
                    "travel_leg_unknown",
                    ref=f"{origin_id}→{dest_id}@{when}",
                    message=message,
                    date=when,
                    leg={
                        "origin_id": origin_id,
                        "origin": origin,
                        "dest_id": dest_id,
                        "dest": dest,
                        "mode": mode,
                        "depart": str(leg.get("depart") or ""),
                    },
                    detail=detail,
                )
            )

    # 开放规则：与 opening_counts 用同一份单元，不从「0/8」反推。
    for poi_id, when in opening_units(itinerary, facts, unlocated):
        if when is not None and any(_opening_fact(fact, poi_id, when, _fact_applies) for fact in facts or []):
            continue
        name = names.get(poi_id, poi_id)
        if when is None:
            message = f"{name}：未能定位或未能排入行程，开放规则无法核实"
        else:
            message = f"{_day_label(when)} {name}：尚无适用于当天的开放规则"
        details.append(
            _issue(
                "opening_missing",
                ref=f"{poi_id}@{when.isoformat() if when else 'unplaced'}",
                message=message,
                poi_id=poi_id,
                date=when.isoformat() if when else "",
                date_label=_day_label(when),
            )
        )

    # 抵离时刻未知：首末两天的可用窗口定不下来，行程可能排不下。
    details.extend(_rail_window_details(rail_window, days, return_options))

    supply = (itinerary or {}).get("candidate_supply") or {}
    gaps = list(supply.get("final_gap") or []) if isinstance(supply, dict) else []
    if supply.get("shortage"):
        details.append(
            _issue(
                "candidate_shortage",
                ref="candidate_supply",
                detail={"gaps": gaps, "supplement": supply.get("supplement") or {}},
            )
        )
    else:
        for day in days:
            density = day.get("density") or {}
            if density.get("status") != "sparse":
                continue
            details.append(
                _issue(
                    "schedule_sparse",
                    ref=f"density@{day.get('date') or day.get('day')}",
                    date=str(day.get("date") or ""),
                    detail=dict(density),
                    message=f"{day.get('label') or '当天'}：{density.get('reason') or _ISSUE_TEXT['schedule_sparse']}",
                )
            )

    # 返程依据：后端已经查到候选，但用户还没选，末日最晚结束时间无法定。
    basis = plan_basis or {}
    inbound = basis.get("inbound")
    if inbound is None and return_options:
        details.append(_issue("return_basis_pending", ref="inbound", options=list(return_options)))

    # 住宿只到区域：往返接驳没有可靠锚点，不能假装已纳入排程。
    state = lodging_state or {}
    if str(state.get("scope") or "") == "area" and not str(state.get("poi_id") or ""):
        text = str(state.get("text") or state.get("name") or state.get("asked_name") or "")
        details.append(
            _issue(
                "lodging_anchor_missing",
                ref=text or "lodging",
                detail={"scope": "area", "text": text, "reason": "not_located"},
            )
        )
    return details


# collect_issues 有个同名关键字参数，这里留个别名避免参数遮蔽函数。
_build_schedule_details = schedule_details


def _ticket_issues(ticket_state: dict | None, ticket_applicability: dict | None) -> list[dict] | None:
    """票务告警按真实状态分流。返回 None 表示没有状态信息，沿用旧聚合逻辑。

    `checks.ticket` 仍是 unverified（复用不等于已核实），但文案要说清是
    「复用未刷新」「查到但不适用」还是「查询失败/无结果/超时」。
    """
    if ticket_state is None:
        if ticket_applicability:
            alt = str(ticket_applicability.get("code") or "ticket_not_applicable")
            return [
                _issue(
                    alt,
                    ref=ticket_applicability.get("ref"),
                    reasons=list(ticket_applicability.get("reasons") or []),
                )
            ]
        return None
    reason = str(ticket_state.get("reason") or "")
    queried_at = str(ticket_state.get("queried_at") or "")
    facts_count = int(ticket_state.get("facts_count") or 0)
    applicable = int(ticket_state.get("applicable_count") or 0)
    diagnostics = [item for item in (ticket_state.get("diagnostics") or []) if isinstance(item, dict)]
    if reason == "skipped":
        # 本方案不依赖铁路，checks.ticket 本来就是 not_required。
        return []
    if reason == "ok" and applicable > 0:
        return []
    if reason == "reused":
        # 复用要按「上次结果本身是否可用」分流：能用的说「未刷新」，
        # 不适用的说「不适用于本方案」，上次就没查出结果的如实说明失败原因。
        if facts_count > 0 and applicable == 0:
            reasons = list((ticket_applicability or {}).get("reasons") or [])
            issue = _issue(
                "ticket_not_applicable",
                ref=(ticket_applicability or {}).get("ref") or queried_at or None,
                message="上次查到的车次，区间或日期都不适用于本方案",
                reasons=reasons,
                detail="本轮沿用上次结果，余票未刷新",
            )
            return [issue]
        if facts_count == 0:
            # 复用父方案的票务结论时，不能再用旧的 {"failed","empty","timeout"}
            # 词表硬猜：那会把 unavailable/missed 误判成「沿用上次车次查询」。
            # 统一走 routing.classify_ticket_parent，按 parent_reason 现算
            # (parent_class, reuse_source)；pipeline 已塞好的字段优先复用，缺失时补算。
            # 延迟导入避免与 routing 形成模块顶层循环依赖。
            from planner.routing import classify_ticket_parent

            parent = str(ticket_state.get("parent_reason") or "")
            parent_class = str(ticket_state.get("parent_class") or "")
            reuse_source = str(ticket_state.get("reuse_source") or "")
            if not parent_class or not reuse_source:
                parent_class, reuse_source = classify_ticket_parent(parent)
            parent_notes = [
                str(item.get("message") or item.get("code") or "")
                for item in (ticket_state.get("parent_diagnostics") or [])
                if isinstance(item, dict)
            ]
            parent_notes = [text for text in parent_notes if text]
            if reuse_source == "parent_ok":
                message = f"上次查询结果，未刷新（查询时间 {queried_at}）。余票不是当前余票。" if queried_at else "上次查询结果，未刷新。余票不是当前余票。"
                return [_issue("ticket_reused", ref=queried_at or None, message=message)]
            if reuse_source == "parent_reused":
                message = f"沿用更早一次查询（查询时间 {queried_at}），余票未刷新。" if queried_at else "沿用更早一次查询，余票未刷新。"
                return [_issue("ticket_reused", ref=queried_at or None, message=message)]
            if reuse_source == "parent_skipped":
                # 父方案无需铁路，复用不应产生票务 issue，与 collect_issues 的 skipped 分支一致。
                return []
            if parent_class == "failed":
                cause = {
                    "unavailable": "上次票务服务不可用或依赖缺失，本次未重新查询，余票未知。",
                    "missed": "上次未调度或返回空结果，本次未重新查询，余票未知。",
                    "timeout": "上次查票超时，本次未重新查询，余票未知。",
                }.get(parent)
                if cause is None:
                    # 失败分类但没有具体父原因（或未知原因）：保留如实说明，不包装成有结果。
                    if parent_notes:
                        cause = f"上次车次查询未成功（{'；'.join(parent_notes[:3])}），本次未重新查询，余票未知。"
                    else:
                        cause = "本次未重新查询，余票状态未知。"
                return [
                    _issue(
                        "ticket_unverified",
                        ref=queried_at or None,
                        message=cause,
                    )
                ]
            # parent_unknown / 其它：父方案没说清查没查到，不能声称有上次结果。
            if parent_notes:
                return [
                    _issue(
                        "ticket_unverified",
                        ref=queried_at or None,
                        message=f"上次车次查询未成功（{'；'.join(parent_notes[:3])}），本次未重新查询，余票状态未知。",
                    )
                ]
            return [
                _issue(
                    "ticket_unverified",
                    ref=queried_at or None,
                    message="本次未重新查询，余票状态未知。",
                )
            ]
        # 有事实但 applicable>0：沿用成功结果，统一口径说未刷新（不再是「沿用上次车次查询」）。
        message = f"上次查询结果，未刷新（查询时间 {queried_at}）。余票不是当前余票。" if queried_at else "上次查询结果，未刷新。余票不是当前余票。"
        return [_issue("ticket_reused", ref=queried_at or None, message=message)]
    if facts_count > 0 and applicable == 0:
        # 查到了车次但都不适用于本方案：原因由 applicability_note 给出。
        if ticket_applicability:
            alt = str(ticket_applicability.get("code") or "ticket_not_applicable")
            return [
                _issue(
                    alt,
                    ref=ticket_applicability.get("ref") or queried_at or None,
                    reasons=list(ticket_applicability.get("reasons") or []),
                )
            ]
        return [
            _issue(
                "ticket_unverified",
                ref=queried_at or None,
                message="查到了车次，但区间或日期都不适用于本方案",
            )
        ]
    base = {
        "failed": "票务查询失败，没有可引用的车次",
        "empty": "票务查询没有返回车次",
        "timeout": "票务查询超时，没有可引用的车次",
    }.get(reason, "方案依赖铁路，但没有可引用的有效车次")
    messages = [str(item.get("message") or item.get("code") or "") for item in diagnostics]
    messages = [text for text in messages if text]
    if messages:
        base = f"{base}（{'；'.join(messages[:3])}）"
    return [_issue("ticket_unverified", ref=queried_at or None, message=base)]


def collect_issues(
    *,
    checks: dict[str, str],
    ticket_diagnostics: list[dict] | None = None,
    summary_degraded: bool = False,
    unlocated: list | None = None,
    ticket_applicability: dict | None = None,
    ticket_state: dict | None = None,
    itinerary: dict | None = None,
    schedule_details: list[dict] | None = None,
    route_states: dict | None = None,
    lodging_state: dict | None = None,
    facts: list[dict] | None = None,
    return_options: list | None = None,
    plan_basis: dict | None = None,
    rail_window: dict | None = None,
) -> list[dict]:
    issues: list[dict] = []
    # 同一条问题可能被两个阶段各报一次（例如同一住宿既没定位又只到区域）。
    # 按 code + ref + kind 去重：页面上一条就够，重复只会让人以为问题更多。
    seen: set[tuple] = set()

    def add(issue: dict) -> None:
        key = (issue["code"], str(issue.get("ref")), issue.get("kind"))
        if key in seen:
            return
        seen.add(key)
        issues.append(issue)

    for item in ticket_diagnostics or []:
        code = str(item.get("code") or "unrecognized_ticket_payload")
        add(_issue(code, ref=item.get("ref") or item.get("source_id"), tool=item.get("tool"),
                   **({"message": item["message"]} if code == "ticket_empty" and item.get("message") else {})))
    # 未定位项按对象类型分开：只有景点与必去项影响开放规则分母与日程结论。
    entries = _unlocated_entries(unlocated)
    sights = [entry for entry in entries if entry["kind"] in {"sight", "must_visit"}]
    if sights:
        add(_issue("candidate_unlocated", ref="、".join(entry["name"] for entry in sights), items=sights))
    for entry in entries:
        if entry["kind"] != "lodging":
            continue
        code = "lodging_area_pending" if entry.get("reason") != "unresolved" else "lodging_unresolved"
        add(_issue(code, ref=entry["name"], items=[entry]))
    # 排程明细：能展开就逐条列，页面才能给出「哪一段、哪一天、为什么」。
    details = list(schedule_details or [])
    if itinerary and not details:
        details = _build_schedule_details(
            itinerary=itinerary,
            unlocated=unlocated,
            route_states=route_states,
            lodging_state=lodging_state,
            facts=facts,
            return_options=return_options,
            plan_basis=plan_basis,
            rail_window=rail_window,
        )
    # 明细各自只压掉自己解释得了的那一类聚合项：交通明细能说明「耗时未核实」，
    # 但不能替开放规则或排程结论说话，否则别的问题会被悄悄吞掉。
    covered: set[str] = set()
    for item in details:
        for category in item.pop("covers", ()) or ():
            covered.add(str(category))
        for category in _DETAIL_COVERS.get(str(item.get("code") or ""), ()):
            covered.add(str(category))
        add(item)
    pairs = (
        ("opening_rules", "unverified", "opening_unverified"),
        ("travel_time", "reference", "travel_reference"),
        ("travel_time", "unverified", "travel_unverified"),
        ("schedule", "conflict", "schedule_conflict"),
        ("schedule", "unverified", "schedule_unverified"),
        ("budget", "unverified", "budget_unverified"),
        ("budget", "conflict", "budget_conflict"),
        ("ticket", "unverified", "ticket_unverified"),
    )
    for check, status, code in pairs:
        if checks.get(check) != status:
            continue
        if check == "ticket":
            routed = _ticket_issues(ticket_state, ticket_applicability)
            if routed is None:
                add(_issue("ticket_unverified"))
            else:
                for item in routed:
                    add(item)
            continue
        # 该类别已经有可展开明细时不再叠一条聚合告警：同一件事说两遍只会让人以为问题更多。
        if _AGGREGATE_CATEGORY.get(code) in covered:
            continue
        add(_issue(code))
    if summary_degraded:
        add(_issue("summary_degraded"))
    return issues
