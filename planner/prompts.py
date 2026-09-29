"""
主智能体 / 槽位抽取用的系统提示。

【小白怎么理解？】
    MAIN_AGENT_PROMPT：槽位已确认时用。
    MAIN_AGENT_PROMPT_LEGACY：抽取失败、没有结构化槽位时用（允许模型自己理解）。
    build_main_prompt() 按「要不要票 / 要不要地图 / 是不是只改景点」往模板里填补丁。
"""

from __future__ import annotations

from datetime import datetime

from slots import TravelSlots, format_slots_for_prompt

from planner.paths import WEEKDAYS

_SHARED_RULES = """
可访问路径仅限：
- /workspace/results/（可写，地图 HTML 写到 /workspace/results/maps/{plan_id}.html）
- /workspace/config/（记忆）
- /workspace/skills/amap-lbs-skill/（只读 skill）

最终旅游规划 Markdown 由系统自动保存，不要自己写 md 文件。

规则：
1. {rule_1}
2. {rule_2}
3. {map_rule}
4. {rail_rule}
5. 不要调用 summary_agent；成文由系统完成
6. 调度完所需子智能体后，只输出一句简短状态（例如「已完成查票与景点检索」或「未查票（无需铁路），已完成景点检索」），不要输出完整旅行方案长文
7. 输出必须使用中文
8. 不做真实购票，只做规划和建议
{ticket_patch}
{map_patch}
{slots_block}
""".strip()

MAIN_AGENT_PROMPT = f"""
你是一名旅游规划总控智能体。
你的职责是根据已确认的旅行约束，调度合适的子智能体完成检索任务。

今天是 {{today}}，星期{{weekday}}。
用户说的「明天 / 下周六」等相对日期，必须换算成具体 YYYY-MM-DD 再查票。

{_SHARED_RULES}
""".strip()

MAIN_AGENT_PROMPT_LEGACY = f"""
你是一名旅游规划总控智能体。
你的职责是根据用户输入，先理解关键信息，再调度合适的子智能体完成任务。

今天是 {{today}}，星期{{weekday}}。
用户说的「明天 / 下周六」等相对日期，必须换算成具体 YYYY-MM-DD 再查票。

{_SHARED_RULES}
""".strip()

NEED_MAP_PATCH = """
用户未声明不要地图：必须调用 map_agent，且必须把路线地图 HTML 写到 /workspace/results/maps/{plan_id}.html（不是「可以的话再生成」）。地图 HTML 必须内嵌 <script id="amapTaskData" type="application/json"> 数组，由页面 JS 运行时构造 #amapOnline 链接，不要手写 percent-encoding 的完整 URL。
"""

NO_MAP_PATCH = """
用户明确不要路线地图：不要调用 map_agent，不要生成地图 HTML。
"""

NEED_RAIL_PATCH = """
本次路由判定需要铁路（跨城或用户明确要坐火车/高铁）。已挂载 ticket_agent，必须在同一轮对 map_agent 与 ticket_agent 各发起一次 task（一条消息里两个 tool call），禁止先等一个返回再调另一个。
"""

NO_RAIL_PATCH = """
本次路由判定无需铁路（{reason}）。不要调用 ticket_agent，不要走 12306；只调度 map_agent；禁止编造车次/票价。
"""

TICKET_UNAVAILABLE_PATCH = """
车票服务不可用，不要调度 ticket_agent；后续汇总只整合地图结果，禁止编造车次/票价。
"""

REVISION_MAP_PATCH = """
本次为方案修订：落实用户点名的增删景点与路线调整；未改的约束（排除目的地、预算、天数）保持不变。不要调度 ticket_agent。
"""

SLOT_EXTRACT_PROMPT = """你是旅行需求槽位抽取器。今天是 {today}，星期{weekday}。
从用户输入抽取字段：origin(出发地)、destination(目的地)、date、days、budget、preferences、pace、lodging(住宿地：酒店/民宿/住址，未提及则 null)。
规则：
1. 用户未提及的字段填 null，不要猜测填充
2. date：若是相对日期（明天/下周六等）请换算成 YYYY-MM-DD；已是具体日期则用 YYYY-MM-DD；无法确定则 null
3. days 必须是正整数或 null
4. 只输出结构化字段，不要解释
"""


def _rail_reason_text(skip_reason: str | None) -> str:
    """把内部 reason 码翻成提示词里的人话。"""
    if skip_reason == "intra_city":
        return "同城"
    if skip_reason == "no_rail_intent":
        return "用户明确自驾/租车/航空/不坐火车"
    return "无需铁路"


def build_main_prompt(
    *,
    ticket_available: bool,
    plan_id: str,
    slots: TravelSlots | None = None,
    legacy: bool = False,
    need_ticket: bool = True,
    skip_reason: str | None = None,
    revision_map_only: bool = False,
    need_map: bool = True,
) -> str:
    """按本次路由结果拼主智能体 system prompt（含日期、槽位约束、票/图补丁）。"""
    now = datetime.now()
    if need_map:
        map_patch = NEED_MAP_PATCH.format(plan_id=plan_id)
        map_rule = "必须调度 map_agent，并生成路线地图 HTML。"
    else:
        map_patch = NO_MAP_PATCH
        map_rule = "用户不要路线地图：禁止调用 map_agent。"
    if revision_map_only:
        ticket_patch = REVISION_MAP_PATCH
        rail_rule = "修订模式：只调度 map_agent，禁止查票与编造车次。"
    elif not need_ticket:
        ticket_patch = NO_RAIL_PATCH.format(reason=_rail_reason_text(skip_reason))
        rail_rule = "路由已判定无需铁路，禁止调用 ticket_agent。"
    elif ticket_available:
        if need_map:
            ticket_patch = NEED_RAIL_PATCH
            rail_rule = "路由已判定需要铁路：必须同轮并行调度 map_agent 与 ticket_agent。"
        else:
            ticket_patch = "必须调度 ticket_agent 查票。用户不要路线地图，禁止调用 map_agent。\n"
            rail_rule = "需要铁路查票，但不要调度 map_agent。"
    else:
        ticket_patch = TICKET_UNAVAILABLE_PATCH
        rail_rule = "车票服务不可用：禁止调度 ticket_agent，禁止编造票务。"
    common = {
        "today": now.strftime("%Y-%m-%d"),
        "weekday": WEEKDAYS[now.weekday()],
        "ticket_patch": ticket_patch,
        "map_patch": map_patch,
        "map_rule": map_rule,
        "plan_id": plan_id,
        "rail_rule": rail_rule,
    }
    if legacy or slots is None:
        return MAIN_AGENT_PROMPT_LEGACY.format(
            **common,
            rule_1="从用户输入理解：出发地、目的地、日期/天数、预算、偏好、出行节奏",
            rule_2="未说明天数时按 1 天；未说明预算按中等；未说明偏好按「经典景点 + 少折腾」；未说明节奏按舒适型",
            slots_block="",
        )
    return MAIN_AGENT_PROMPT.format(
        **common,
        rule_1="出发地、目的地、出行日期为关键约束：必须有据可查，禁止臆造",
        rule_2="游玩天数、预算、偏好、出行节奏以【已确认的旅行约束】为准（可含用户确认过的默认值），不要擅自改写",
        slots_block=format_slots_for_prompt(slots, rail=need_ticket),
    )
