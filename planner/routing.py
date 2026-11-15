"""
查票 / 出地图 / 修订分流（纯函数，不联网）

【小白怎么理解？】
    不是每次规划都要调 12306。同城、用户说自驾/坐飞机、或明确不要地图时，
    这里用关键词 + 槽位算出「跳过原因」，pipeline 按原因发 step 事件。
    调度结束后再用 resolve_*_state 告诉汇总：ok / skipped / missed…，禁止瞎编车次。
"""

from __future__ import annotations

import re
from typing import Literal

from slots import TravelSlots

# 票务最终态：ok 有结果；reused 修订沿用上版；其余都禁止编造车次
TicketState = Literal["ok", "skipped", "unavailable", "missed", "timeout", "reused"]
# 父快照票务状态的分类（复用时要区分「真的查过」和「上次就失败」）
TicketParentClass = Literal["ok", "skipped", "failed", "unknown"]
# 复用来源：parent_failed 表示父方案本身没查到，正文不能写成「沿用上次结果」
ReuseSource = Literal[
    "parent_ok",
    "parent_skipped",
    "parent_failed",
    "parent_reused",
    "parent_unknown",
]
# 用户改上一版方案时走哪条流水线
RevisionKind = Literal["map_only", "ticket_and_map", "summary_only", "budget_only"]
MapState = Literal["ok", "skipped", "missed", "timeout", "incomplete"]

# 「不要地图产物」只否决地图 HTML，景点检索与路线仍要做。
# 注意：「不要路线图」说的是不要那张图，不等于不要查路线耗时；两者不能混为一谈。
_NO_MAP_ARTIFACT = (
    "不要地图",
    "不用地图",
    "不需要地图",
    "无需地图",
    "不生成地图",
    "不要路线图",
    "不用路线图",
    "不需要路线图",
    "无需路线图",
)
# 「不要查路线 / 不排路线」只否决路线耗时查询：景点候选与结构化行程仍保留，
# 但相邻段一律记 not_queried、耗时未知。刻意不收「不要路线」这种会撞上
# 「不要路线图」的宽泛子串，词表只收明确的「查 / 排 / 查询」动作。
_NO_ROUTE = (
    "不查路线",
    "不查交通",
    "不排路线",
    "不要查路线",
    "不用查路线",
    "不需要查路线",
    "无需查路线",
    "不要路线查询",
    "不用路线查询",
    "不需要路线查询",
    "无需路线查询",
    "不要交通耗时",
    "不查交通耗时",
)
# 「不要找景点」否决景点检索；检索不到景点就没有可信行程，路线与地图一并停。
_NO_POI = (
    "不要找景点",
    "不用找景点",
    "不需要找景点",
    "无需找景点",
    "不要景点",
    "不需要景点",
    "不用搜景点",
    "不搜景点",
)

_RAIL_POS = ("高铁", "动车", "12306", "车票", "坐火车", "乘火车", "火车去")
_DRIVE = ("自驾", "开车", "租车", "不坐火车")
_PLANE = ("飞机", "航班", "机票", "坐飞机", "乘飞机")
_NO_PLANE = ("不坐飞机", "不乘飞机", "不坐航班")

_DATE_CHANGE = (
    "推迟",
    "提前",
    "改期",
    "改天",
    "日期",
    "下周再",
    "改到周",
    "改明天",
    "改后天",
)
_PLACE_CHANGE = ("出发地", "目的地", "改从", "改去", "换个城市", "换成从")
_TRANSPORT_CHANGE = (
    "高铁",
    "火车",
    "动车",
    "自驾",
    "开车",
    "租车",
    "飞机",
    "航班",
    "机票",
    "打车",
    "交通",
    "地铁",
    "更快的交通",
    "换个快点",
)
_ITINERARY = ("景点", "路线", "景区", "不要", "换成", "少走", "充电", "行程", "顺路", "少林", "白马")
_COPY_ONLY = ("注意事项", "措辞", "语气", "写法", "润色")

# 用户明确要求「重新取数」的动作。槽位没变也要真的再查一次，不能只重写正文。
_TICKET_REFRESH_HINTS = ("刷新余票", "更新余票", "余票刷新", "重新订票", "重新买票", "再看余票")
_TICKET_REFRESH_VERBS = ("重新查", "再查", "重查", "重新查询", "再查询")
# 用户明确要求「重排行程/路线顺序」的动作：要重算行程，但不重新查票。
_MAP_RESHUFFLE_HINTS = (
    "重排行程",
    "重新排行程",
    "重排景点",
    "重新安排顺序",
    "重新规划路线",
    "重算路线",
    "换个路线顺序",
    "调换顺序",
    "调整景点顺序",
    "调整行程顺序",
)


def _explicit_ticket_refresh(instruction: str) -> bool:
    """是否明确要求重新查询车票/刷新余票。"""
    text = instruction or ""
    if not text:
        return False
    if _has_any(text, _TICKET_REFRESH_HINTS):
        return True
    # 「重新查一下余票」这类要动词 + 票/余票同时出现，避免「重新查看注意事项」被误升。
    return _has_any(text, _TICKET_REFRESH_VERBS) and ("票" in text)


def _explicit_map_reshuffle(instruction: str) -> bool:
    """是否明确要求调整/重排行程或景点顺序。"""
    text = instruction or ""
    if not text:
        return False
    if "顺序" in text and _has_any(text, ("调整", "改", "换", "重排", "重新排")):
        return True
    return _has_any(text, _MAP_RESHUFFLE_HINTS)


# 明确要求「重新定位住宿区域/参考地点」：这类原话不带来任何槽位变化，
# 却必须真的重跑一次高德定位、重建住宿往返接驳。少了这条判据，纯原话
# 修订会被 resolve_revision_kind 压成 summary_only，页面上的「重新定位」
# 按钮点了也不会重新查。
_LODGING_RELOCATE_HINTS = (
    "重新定位住宿",
    "再定位住宿",
    "住宿重新定位",
    "重新查住宿",
    "重新找住宿",
    "重新核实住宿",
    "重新定位参考地点",
    "重新定位区域",
)


def _explicit_lodging_relocate(instruction: str) -> bool:
    """是否明确要求重新定位住宿区域或参考地点。"""
    text = (instruction or "").strip()
    if not text:
        return False
    return _has_any(text, _LODGING_RELOCATE_HINTS)


# 原话里的车次号：允许 G/D/C/Z/T/K 开头，后面 1-4 位数字。
# 与 planner/ticket_facts.py 的 _PROSE_TRAIN 保持同一口径，但本模块是纯规则层，
# 不反向依赖票务解析模块（routing 要能被单独导入与测试）。
_PROSE_TRAIN = re.compile(r"(?<![A-Za-z0-9])([GDCZTK]\d{1,4})(?!\d)")
# 选车/改车次类动词：「返程坐 G3252」「改成 G1952」「换一趟下午的车」。
_SELECT_VERBS = ("坐", "选", "改", "换", "订", "就")
# 返程上下文词表，与 planner/ticket_facts.py 的 _RETURN_CTX 一致。
_RETURN_CTX = (
    "返程",
    "回程",
    "返回",
    "回来",
    "回去",
    "当天回",
    "当天返回",
    "往返",
    "来回",
)
# 返程时段偏好词，与 planner/ticket_facts.py 的 _RETURN_HINT 键一致。
_PERIOD_WORDS = ("下午", "晚上", "傍晚", "中午")


def _explicit_train_selection(instruction: str) -> bool:
    """是否明确指定/更换某一趟车或返程时段。

    命中任一即 True：
    - 出现车次号且出现选择/修改类动词（「返程坐 G3252」「改成 G1952 吧」）；
    - 出现返程上下文且出现时段词（「返程改为下午」「回程想傍晚走」）。

    这类动作在已有适用事实里就能定下依据，只需重算排程，不需要重查票，
    因此调用方把它映射成 map_only；明确要求刷新余票仍由
    _explicit_ticket_refresh 先判成 ticket_and_map。
    """
    text = (instruction or "").strip()
    if not text:
        return False
    if _PROSE_TRAIN.search(text) and _has_any(text, _SELECT_VERBS):
        return True
    return _has_any(text, _RETURN_CTX) and _has_any(text, _PERIOD_WORDS)


def normalize_place(name: str | None) -> str:
    """去掉「省/市/区」便于模糊同城比较。空串表示没法比。"""
    raw = (name or "").strip().replace(" ", "")
    if not raw:
        return ""
    raw = re.sub(r"^[\u4e00-\u9fff]{1,8}省", "", raw)
    return raw.replace("市", "").replace("区", "")


def is_same_city(origin: str | None, destination: str | None) -> bool:
    """判不出则 False（视为跨城 / 需要查票）。新乡 vs 新乡县视为不同城。"""
    a = (origin or "").strip()
    b = (destination or "").strip()
    if not a or not b:
        return False
    if ("县" in a) != ("县" in b):
        return False
    fa, fb = normalize_place(a), normalize_place(b)
    if not fa or not fb:
        return False
    return fa == fb or fa in fb or fb in fa


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    """原文是否包含任一关键词（子串匹配，够用且好测）。"""
    return any(n in text for n in needles)


def rail_intent_flags(query: str) -> dict[str, bool]:
    """从用户原话抽出铁路 / 自驾 / 飞机意图。坐高铁优先于自驾、飞机。"""
    q = query or ""
    no_plane = _has_any(q, _NO_PLANE)
    plane = (not no_plane) and _has_any(q, _PLANE)
    drive = _has_any(q, _DRIVE)
    rail = (not drive) and (
        _has_any(q, _RAIL_POS) or ("火车" in q and "不坐火车" not in q)
    )
    if "坐高铁" in q or "乘高铁" in q or "高铁去" in q:
        rail = True
        drive = False
        plane = False
    return {"rail": rail, "drive": drive, "plane": plane, "no_plane": no_plane}


def effective_intercity_intent(
    query: str,
    *,
    request_ctx: dict | None = None,
    slots: TravelSlots | None = None,
    previous_query: str = "",
    query_is_current: bool = True,
) -> dict[str, bool]:
    """跨城交通按三层优先级判定：本轮原话 > 已确认/合并的交通方式 > 历史原话。

    只有 transport_mode == "driving" 参与否定铁路：该字段默认值就是
    public_transit，无法区分「用户显式选了公交」与「本轮压根没提交通」，
    把它映射成 rail 会让每个只改景点的修订轮都自称坐高铁。
    """
    flags = rail_intent_flags(query) if query_is_current else {"rail": False, "drive": False, "plane": False, "no_plane": False}
    ctx = request_ctx or {}
    if not any(flags.values()):
        mode = str(ctx.get("intercity_mode") or "")
        flags = {
            "rail": mode == "rail",
            "drive": mode == "driving",
            "plane": mode == "plane",
            "no_plane": False,
        }
    if not any(flags.values()):
        # 合并后的槽位：修订轮「推迟一天」本身不含交通词，但上一版定下的
        # 自驾仍然是这份行程的约束，不能被更早的原话（坐高铁）覆盖。
        if str(getattr(slots, "transport_mode", "") or "") == "driving":
            flags = {"rail": False, "drive": True, "plane": False, "no_plane": False}
    if not any(flags.values()):
        flags = rail_intent_flags(previous_query)
    return flags


def skip_ticket_reason(
    slots: TravelSlots | None, query: str, *, previous_query: str = "", request_ctx: dict | None = None,
    query_is_current: bool = True,
) -> str | None:
    """铁路查询与抵离窗口使用同一份跨城意图，不能把市内自驾当作跨城自驾。"""
    flags = effective_intercity_intent(
        query, request_ctx=request_ctx, slots=slots, previous_query=previous_query, query_is_current=query_is_current
    )
    if flags["rail"] or flags["no_plane"]:
        return None
    if flags["drive"] or flags["plane"]:
        return "no_rail_intent"
    if slots is not None and is_same_city(slots.origin, slots.destination):
        return "intra_city"
    return None


def needs_rail_ticket(slots: TravelSlots | None, query: str) -> bool:
    """本次是否需要铁路查票。仅依据本次 query 与槽位，不用默认画像。"""
    return skip_ticket_reason(slots, query) is None


_ROUND_TRIP = ("往返", "当天回", "当天返回", "来回", "返程", "回程", "往返票")


def rail_required(
    slots: TravelSlots | None, query: str, *, previous_query: str = "", request_ctx: dict | None = None,
    query_is_current: bool = True,
) -> dict[str, bool]:
    """当前行程是否依赖铁路。与「本轮是否重新查票」无关。

    返回 {"outbound": bool, "inbound": bool}：
    - outbound 决定首日最早开始；inbound 决定末日最晚结束。
    - 明确自驾 / 飞机 / 同城时都不依赖铁路。
    - 多日行程默认需要返程；单日只在原话有往返意图时才需要返程约束。
    修订只改景点、复用旧票时（need_ticket=False），这里仍返回 True，
    抵离窗口不会被误判成「无需铁路」而回退到 09:00。
    """
    flags = effective_intercity_intent(
        query, request_ctx=request_ctx, slots=slots, previous_query=previous_query, query_is_current=query_is_current
    )
    if flags["drive"] or flags["plane"]:
        return {"outbound": False, "inbound": False}
    if slots is not None and is_same_city(slots.origin, slots.destination):
        return {"outbound": False, "inbound": False}
    days = (slots.days if slots is not None and slots.days else None) or 1
    # 往返意图要看本轮原话与历史原话两侧：本轮只写「把景点换成外滩」时，
    # 历史里那句「当天往返」仍然是这份行程的约束，不能被本轮文本遮住。
    ctx_return = (request_ctx or {}).get("return_required")
    inbound = bool(ctx_return) if isinstance(ctx_return, bool) else (
        days > 1 or _has_any(query or "", _ROUND_TRIP) or _has_any(previous_query or "", _ROUND_TRIP)
    )
    return {"outbound": True, "inbound": bool(inbound)}


def map_scope(query: str) -> dict:
    """三层地图语义：要不要检索景点、要不要路线、要不要出地图 HTML。

    「不要地图 / 不要路线图」只是否决地图 HTML 产物，景点和路线仍是正文内容：
    路线图是「一张图」，不是「路线耗时查询」，两者不能因为都带「路线」就混为一谈。
    只有明确说「不查路线 / 不排路线 / 不查交通耗时」才关掉路线耗时查询；此时景点
    候选与结构化行程照旧保留，但所有相邻段标记为 not_queried、耗时未知。
    「不要找景点」连景点都不检索，也就没有可信行程可以排，路线与地图一并停。
    reason 说清是哪一层被用户否决，pipeline 据此发 step 与告警。
    """
    text = query or ""
    if _has_any(text, _NO_POI):
        # 强否定优先：连景点都不要时，另外两层没有必要再区分。
        return {
            "need_poi_search": False,
            "need_route": False,
            "need_map_artifact": False,
            "reason": "user_no_poi",
        }
    no_artifact = _has_any(text, _NO_MAP_ARTIFACT)
    no_route = _has_any(text, _NO_ROUTE)
    # 两种否决可以并存：reason 取更直观的「不要地图产物」，need_route 仍如实为 False。
    reason = "user_no_map" if no_artifact else ("user_no_route" if no_route else "")
    return {
        "need_poi_search": True,
        "need_route": not no_route,
        "need_map_artifact": not no_artifact,
        "reason": reason,
    }


def skip_map_reason(query: str) -> str | None:
    """map_scope 的薄封装：只回答「地图为什么被跳过」，未说明返回 None。"""
    reason = map_scope(query)["reason"]
    return reason or None


def classify_ticket_parent(parent_reason: str) -> tuple[TicketParentClass, ReuseSource]:
    """把父快照的 ticket_reason 分成「父状态分类」与「复用来源」两件事。

    父快照的原始值必须原样保留（它是事实来源），这里只做分类：正文据此决定
    能不能写「沿用上次查询结果」。父方案本身就是 unavailable/missed/timeout 时，
    复用它只能是「上次没查到，这次也没重查」，不能伪装成有余票依据。
    未知值和空值都归 unknown，避免旧快照缺字段时静默当成 ok。
    """
    reason = str(parent_reason or "").strip()
    if reason == "ok":
        return "ok", "parent_ok"
    if reason == "skipped":
        return "skipped", "parent_skipped"
    if reason in ("unavailable", "missed", "timeout"):
        return "failed", "parent_failed"
    if reason == "reused":
        return "ok", "parent_reused"
    return "unknown", "parent_unknown"


def resolve_map_state(
    *,
    need_map: bool,
    dispatched: bool,
    has_result: bool,
    timed_out: bool,
) -> MapState:
    """ok > skipped > missed（未发 task）> timeout（已调度后超时）> incomplete（已调度但没有地图 HTML）。has_result 只认 HTML 落盘。"""
    if has_result:
        return "ok"
    if not need_map:
        return "skipped"
    if not dispatched:
        return "missed"
    if timed_out:
        return "timeout"
    return "incomplete"


def resolve_ticket_state(
    *,
    need_ticket: bool,
    ticket_mounted: bool,
    has_ticket_result: bool,
    timed_out: bool,
) -> TicketState:
    """优先级固定：ok > skipped > unavailable > timeout > missed。"""
    if has_ticket_result:
        return "ok"
    if not need_ticket:
        return "skipped"
    if not ticket_mounted:
        return "unavailable"
    if timed_out:
        return "timeout"
    return "missed"


def classify_revision(
    instruction: str,
    *,
    previous_ticket_state: TicketState | None = None,
) -> RevisionKind:
    """是否改变 (date, origin, destination, 出行方式)？是 → ticket_and_map。"""
    text = instruction or ""
    four_changed = (
        _has_any(text, _DATE_CHANGE)
        or _has_any(text, _PLACE_CHANGE)
        or _has_any(text, _TRANSPORT_CHANGE)
    )
    if four_changed:
        return "ticket_and_map"
    if previous_ticket_state == "ok" and _has_any(text, _TRANSPORT_CHANGE):
        return "ticket_and_map"
    copy_only = _has_any(text, _COPY_ONLY) and not _has_any(text, _ITINERARY)
    if copy_only:
        return "summary_only"
    return "map_only"


def merge_slots(base: TravelSlots, overlay: TravelSlots) -> TravelSlots:
    """只覆盖本轮明确给出的字段。null 不覆盖；空列表表示明确清空。"""
    data = base.model_dump()
    provided = overlay.model_dump(exclude_unset=True)
    for key, val in provided.items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        data[key] = val
    return TravelSlots.model_validate(data)


def diff_fields(base: TravelSlots, merged: TravelSlots) -> list[str]:
    """本轮补充/修改实际动了哪些槽位字段（纯比较，不判断意图）。"""
    if merged is None:
        return []
    old = base.model_dump() if base is not None else {}
    new = merged.model_dump()
    changed: list[str] = []
    for key, val in new.items():
        if old.get(key) != val:
            changed.append(key)
    return changed


# 改这些字段意味着票务区间变了，不能沿用旧票。transport_mode 是市内游览
# 交通，不能再被误当成跨城铁路意图；跨城方式由 request_context 单独保存。
TICKET_FIELDS = ("date", "origin", "destination")
# 只改这些字段时不需要重查车票。
MAP_FIELDS = ("must_visit", "avoid", "preferences", "pace", "lodging")
# 只改这些字段时连地图都不用重做。
BUDGET_FIELDS = ("budget",)
# 请求上下文里「只影响预算/成文」的字段。
CONTEXT_BUDGET_FIELDS = ("party_size", "budget_scope")
# 请求上下文里「影响住宿解析与路线」的字段。
CONTEXT_MAP_FIELDS = ("lodging_scope", "lodging_kind", "intercity_mode", "return_required")
CONTEXT_REVISION_FIELDS = CONTEXT_BUDGET_FIELDS + CONTEXT_MAP_FIELDS


def derive_revision_from_changes(changed: list[str] | None) -> RevisionKind | None:
    """确认卡提交后按「改了什么」重判走哪条流水线。

    返回 None 表示没有可识别的变更，调用方自己决定怎么提示。
    """
    fields = [str(item) for item in (changed or [])]
    if not fields:
        return None
    if any(item in TICKET_FIELDS for item in fields) or "days" in fields:
        # 天数变了返程日期也变，票务区间与排程都受影响。
        return "ticket_and_map"
    if any(item in MAP_FIELDS for item in fields):
        return "map_only"
    if any(item in BUDGET_FIELDS for item in fields):
        # 只改预算/人数/口径：不重查、不重排，只重算预算与成文。
        return "budget_only"
    return None


def resolve_revision_kind(
    structured_changed: list[str] | None,
    context_changed: list[str] | None,
    instruction: str,
    *,
    previous_ticket_state: TicketState | None = None,
) -> RevisionKind:
    """按「实际变化 + 明确动作」决定流水线，不用普通关键词命中当升级依据。

    顺序：
    1. 用户明确要求重新查票/刷新余票 → ticket_and_map（即使槽位没变、即使同时改了预算）。
    2. 日期/区间/交通意图/天数变化 → ticket_and_map。
    3. 用户明确点名车次或指定返程时段 → map_only（在已有适用事实里选车只重算排程，不重查票）。
    4. 用户明确要求重排行程/景点顺序 → map_only（要重算行程，不重查票）。
    5. 用户明确要求重新定位住宿区域/参考地点 → map_only（重跑定位与住宿接驳，
       但原话本身不带来任何槽位变化，必须单独识别，否则会被压成 summary_only）。
    6. 景点/住宿/路线条件变化（含住宿确认范围）→ map_only。
    7. 只有预算/人数/预算口径变化 → budget_only。
    8. 没有任何结构化/上下文变化、也没有明确动作 → summary_only 复用上一版。
       classify_revision 的默认 map_only 与普通关键词匹配（如「自驾」命中交通）
       都不能当作升级依据——它会把一次毫无变化的重述误判成重跑地图或重查票。

    第 1 条必须排在第 2、3 条前面：「返程改为下午，顺便重新查询余票」既命中选车
    也命中刷新，用户要的是新票，不能只重算排程。

    第 2 条必须排在第 3 条前面：「改成 4 日游，返程坐 G3252」同时命中天数变化与
    选车。天数变了返程日期就变了，父方案的旧票按旧日期算的适用性已经不成立；
    若被选车先判成 map_only，就会出现「行程改成 4 天、票还是 11-09 那张、
    而且不重查」的矛盾结果。选车只在「票务区间没变」时才等于只重算排程。
    """
    fields = [str(item) for item in (structured_changed or [])]
    ctx_fields = [str(item) for item in (context_changed or [])]
    if _explicit_ticket_refresh(instruction):
        return "ticket_and_map"
    if "intercity_mode" in ctx_fields:
        return "ticket_and_map"
    if "return_required" in ctx_fields:
        # 单程转往返需要核实返程事实；保守重查能避免没有返程事实却继续沿用。
        # 往返转单程虽不需要票务连接，但该分支会由 skip_ticket_reason 决定是否跳过。
        return "ticket_and_map"
    if any(item in TICKET_FIELDS for item in fields) or "days" in fields:
        # 天数变了返程日期也变，票务区间与排程都受影响：必须重查，不能只重排。
        return "ticket_and_map"
    if _explicit_train_selection(instruction):
        # 从已有适用事实里选车/换时段：重算 rail_window 与排程，但不重查票。
        return "map_only"
    if _explicit_map_reshuffle(instruction):
        return "map_only"
    if _explicit_lodging_relocate(instruction):
        # 纯原话、无槽位变化时也要真的重跑住宿定位，不能压成 summary_only。
        return "map_only"
    if any(item in MAP_FIELDS for item in fields) or any(
        item in CONTEXT_MAP_FIELDS for item in ctx_fields
    ):
        return "map_only"
    if any(item in BUDGET_FIELDS for item in fields) or any(
        item in CONTEXT_BUDGET_FIELDS for item in ctx_fields
    ):
        return "budget_only"
    if fields or ctx_fields:
        # 有变化但不在已知分类里，保守地重算路线（不重查票）。
        return "map_only"
    return "summary_only"


def context_revision_diff(old_ctx: dict | None, new_ctx: dict | None, *, cleared: list[str] | None = None) -> list[str]:
    """比较两版请求上下文里「会改变处理路径」的字段。

    只把「本轮明确提供了新值且与旧值不同」算作变化；本轮没提供的字段（None）
    不算变化，清空动作走 cleared_fields。lodging_kind 是地图解析派生的，不会由
    请求直接提供，因此这里自然跳过，避免把「未重新提供」误判成「已清空」。
    """
    old = old_ctx or {}
    new = new_ctx or {}
    changed: list[str] = []
    for key in CONTEXT_REVISION_FIELDS:
        new_val = new.get(key)
        if new_val is None:
            continue
        # False 是“明确单程”，不能被 `or None` 吞掉，否则同样的单程确认也会
        # 被误判成一次跨城意图变化。
        if old.get(key) != new_val:
            changed.append(key)
    for key in cleared or []:
        if key in CONTEXT_REVISION_FIELDS and key not in changed:
            changed.append(key)
    return changed


# 请求上下文里持久保存的键；数组顺序就是快照里的字段顺序。
REQUEST_CONTEXT_KEYS = (
    "party_size",
    "budget_scope",
    "lodging_scope",
    "lodging_kind",
    "lodging_text",
    "intercity_mode",
    "return_required",
    "plan_basis",
    "selected_train_intent",
    "confirmed_pending",
    "cleared",
    "updated_at",
)


def merge_request_context(
    snapshot_ctx: dict | None,
    current: dict | None = None,
    changed: list[str] | None = None,
    cleared: list[str] | None = None,
) -> dict:
    """合并「上次确认过的输入状态」。

    规则：本轮传了非空值就覆盖；没传就继承；要清空必须显式写进 cleared。
    字段本身变了（住宿文本、出行日期等）就把它派生出来的确认状态作废，
    避免第二次修订还拿着上一次的酒店或人数。
    """
    base = dict(snapshot_ctx or {})
    cur = dict(current or {})
    done = [str(item) for item in (cleared or [])]
    carried_cleared = {str(item) for item in (base.get("cleared") or [])}
    result = {key: base.get(key) for key in REQUEST_CONTEXT_KEYS}

    fields = [str(item) for item in (changed or [])]
    if any(item in TICKET_FIELDS for item in fields):
        result["plan_basis"] = None
    elif "days" in fields and isinstance(result.get("plan_basis"), dict):
        # 天数变了只知道返程日期变了，去程依据还能用。
        basis = dict(result["plan_basis"])
        basis["inbound"] = None
        result["plan_basis"] = basis
    if "lodging" in fields:
        # 住宿文本变了，上一次的确认失效；但本轮同一次提交带上来的确认要保留，
        # 所以先作废、再覆盖（顺序反了会把用户刚点的「按该区域参考」又清掉）。
        result["lodging_scope"] = None
        result["lodging_kind"] = None

    for key in ("party_size", "budget_scope", "lodging_scope", "lodging_kind", "lodging_text", "intercity_mode", "return_required"):
        val = cur.get(key)
        if val is not None and (not isinstance(val, str) or val.strip()):
            result[key] = val
            carried_cleared.discard(key)
    if cur.get("plan_basis") is not None:
        result["plan_basis"] = cur.get("plan_basis")
    if cur.get("selected_train_intent") is not None:
        result["selected_train_intent"] = dict(cur.get("selected_train_intent") or {})
    if cur.get("confirmed_pending") is not None:
        result["confirmed_pending"] = list(cur.get("confirmed_pending") or [])
    if cur.get("updated_at"):
        result["updated_at"] = cur.get("updated_at")

    if "budget" in fields:
        # 金额变了不影响人数口径，保留 party_size。
        pass

    if "lodging_scope" in done:
        result["lodging_scope"] = None
        result["lodging_kind"] = None
    if "lodging_kind" in done:
        result["lodging_kind"] = None
    if "party_size" in done:
        result["party_size"] = None
    if "budget_scope" in done:
        result["budget_scope"] = None

    carried_cleared.update(done)
    result["cleared"] = sorted(carried_cleared)
    return result
