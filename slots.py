"""
旅行规划「槽位」模块（纯函数，不联网）

【小白怎么理解「槽位」？】
    把用户一句话拆成表格字段，例如：
        出发地=杭州，目的地=苏州，日期=2026-09-26，天数=1，预算=800…
    这些字段就叫槽位（slots）。

【两类字段】
    critical（关键）：出发地 / 目的地 / 日期 —— 缺了就没法认真规划，要找用户确认（HITL）。
    soft（软字段）：天数 / 预算 / 偏好 / 节奏 —— 缺了可以用默认值先填上。

【本文件特点】
    全是纯函数 + Pydantic 模型，方便单元测试，不依赖大模型、不依赖网络。
"""

from __future__ import annotations

# 正则：识别 YYYY-MM-DD、解析「下周六」等
import re
# 日期计算
from datetime import date, datetime, timedelta
# 任意类型标注
from typing import Any

# Pydantic：自动校验字段类型/范围
from pydantic import BaseModel, Field, field_validator

# 关键字段名元组（缺一不可）
CRITICAL_FIELDS = ("origin", "destination", "date")
# 软字段名元组（可默认）
SOFT_FIELDS = ("days", "budget", "preferences", "pace")

# 软字段默认值字典
SOFT_DEFAULTS: dict[str, Any] = {
    "days": 1,
    "budget": "中等",
    "preferences": "经典景点 + 少折腾",
    "pace": "舒适型节奏",
}

# 严格日期格式：四位年-两位月-两位日
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 中文星期 → Python weekday() 数字（周一=0 … 周日=6）
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _parse_today(today: date | str | None) -> date:
    """把「今天」参数统一成 date 对象；None 则用系统当天。"""
    if today is None:
        return date.today()
    if isinstance(today, date):
        return today
    # 字符串取前 10 位当 ISO 日期
    return date.fromisoformat(str(today)[:10])


def _next_week_weekday(base: date, target: int) -> date:
    """计算「下周的星期 X」。

    例：今天周四，目标周六 → 落到下一周的周六（不是本周六）。
    若今天刚好是目标星期，则再加 7 天。
    """
    # 距离目标星期还差几天（0~6）
    days_until = (target - base.weekday()) % 7
    if days_until == 0:
        # 今天就是该星期 → 「下周」应再等一周
        return base + timedelta(days=7)
    # 先到本周/即将到来的该日，再 +7 变成「下周」
    return base + timedelta(days=days_until + 7)


def resolve_relative_date(text: str, today: date | str | None = None) -> str | None:
    """把「明天 / 下周六」等相对说法换成 YYYY-MM-DD。

    认不出就返回 None；已经是标准日期则原样返回。
    """
    # 去掉空白
    raw = (text or "").strip()
    if not raw:
        return None
    # 已是标准格式
    if _DATE_RE.match(raw):
        return raw

    # 基准「今天」
    base = _parse_today(today)
    # 「星期三」统一成「周三」，减少分支
    s = raw.replace("星期", "周").replace("礼拜", "周")

    # 今天 / 明天 / 后天 / 大后天
    if s in ("今天", "今日"):
        return base.isoformat()
    if s in ("明天", "明日"):
        return (base + timedelta(days=1)).isoformat()
    if s in ("后天",):
        return (base + timedelta(days=2)).isoformat()
    if s in ("大后天",):
        return (base + timedelta(days=3)).isoformat()

    # 「下周X」或「下下周X」（下的个数表示隔几周）
    m = re.search(r"(下+)周([一二三四五六日天])", s)
    if m:
        weeks = len(m.group(1))
        target = _WEEKDAY_CN[m.group(2)]
        first = _next_week_weekday(base, target)
        if weeks <= 1:
            return first.isoformat()
        # 下下周 = 下周再 +7
        return (first + timedelta(days=7 * (weeks - 1))).isoformat()

    # 「本周X / 这周X」：落到本周该日（含今天）
    m = re.search(r"(?:本周|这周)([一二三四五六日天])", s)
    if m:
        target = _WEEKDAY_CN[m.group(1)]
        days_until = (target - base.weekday()) % 7
        return (base + timedelta(days=days_until)).isoformat()

    # 单独「周六」：若今天就是周六则指下周六，否则指即将到来的周六
    m = re.fullmatch(r"周([一二三四五六日天])", s)
    if m:
        target = _WEEKDAY_CN[m.group(1)]
        days_until = (target - base.weekday()) % 7
        if days_until == 0:
            days_until = 7
        return (base + timedelta(days=days_until)).isoformat()

    # 其它说法暂不支持
    return None


class TravelSlots(BaseModel):
    """已经比较干净、可拿去规划的槽位（日期必须是 YYYY-MM-DD）。"""

    origin: str | None = Field(default=None, max_length=64)
    destination: str | None = Field(default=None, max_length=64)
    date: str | None = Field(default=None, description="YYYY-MM-DD")
    days: int | None = Field(default=None, ge=1, le=30)
    budget: str | None = Field(default=None, max_length=64)
    preferences: str | None = Field(default=None, max_length=200)
    pace: str | None = Field(default=None, max_length=64)
    lodging: str | None = Field(default=None, max_length=128)

    @field_validator("origin", "destination", "budget", "preferences", "pace", "lodging", mode="before")
    @classmethod
    def _strip_empty(cls, v: Any) -> Any:
        """空字符串当成 None，避免『填了空白』误判为有值。"""
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s or None
        return v

    @field_validator("date", mode="before")
    @classmethod
    def _normalize_date(cls, v: Any) -> Any:
        """强制日期只能是 YYYY-MM-DD（相对日期要先 resolve 再进来）。"""
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if not _DATE_RE.match(s):
            raise ValueError("date 必须为 YYYY-MM-DD")
        # 再验一次是否为真实日历日（如 2026-02-30 会失败）
        datetime.strptime(s, "%Y-%m-%d")
        return s


class ExtractedSlots(BaseModel):
    """大模型刚抽出来的毛坯槽位：date 允许仍是『下周六』这种相对词。"""

    origin: str | None = None
    destination: str | None = None
    date: str | None = Field(default=None, description="YYYY-MM-DD 或相对日期原文")
    days: int | None = Field(default=None, ge=1, le=30)
    budget: str | None = None
    preferences: str | None = None
    pace: str | None = None
    lodging: str | None = Field(default=None, max_length=128)


def missing_critical(slots: TravelSlots | dict[str, Any] | None) -> list[str]:
    """列出还缺哪些关键字段（返回字段名列表）。"""
    if slots is None:
        return list(CRITICAL_FIELDS)
    # 统一成普通 dict 方便遍历
    data = slots.model_dump() if isinstance(slots, TravelSlots) else dict(slots)
    missing: list[str] = []
    for key in CRITICAL_FIELDS:
        val = data.get(key)
        if val is None or (isinstance(val, str) and not str(val).strip()):
            missing.append(key)
    return missing


def soft_defaults_needed(slots: TravelSlots | dict[str, Any] | None) -> list[str]:
    """列出哪些软字段为空（即将被默认值填上）。"""
    if slots is None:
        return list(SOFT_FIELDS)
    data = slots.model_dump() if isinstance(slots, TravelSlots) else dict(slots)
    needed: list[str] = []
    for key in SOFT_FIELDS:
        val = data.get(key)
        if val is None or (isinstance(val, str) and not str(val).strip()):
            needed.append(key)
    return needed


def apply_soft_defaults(slots: TravelSlots) -> tuple[TravelSlots, list[str]]:
    """给空的软字段填上默认值。

    返回：(填好后的新 TravelSlots, 本次真正被填上的字段名列表)
    """
    data = slots.model_dump()
    applied: list[str] = []
    for key, default in SOFT_DEFAULTS.items():
        val = data.get(key)
        if val is None or (isinstance(val, str) and not str(val).strip()):
            data[key] = default
            applied.append(key)
    return TravelSlots.model_validate(data), applied


def normalize_extracted(
    extracted: ExtractedSlots | TravelSlots | dict[str, Any],
    *,
    today: date | str | None = None,
) -> TravelSlots:
    """把抽取毛坯整理成 TravelSlots：相对日期在这里换算。"""
    if isinstance(extracted, TravelSlots):
        data = extracted.model_dump()
    elif isinstance(extracted, ExtractedSlots):
        data = extracted.model_dump()
    else:
        data = dict(extracted)

    raw_date = data.get("date")
    # 不是标准格式 → 尝试相对日期解析
    if raw_date and not _DATE_RE.match(str(raw_date).strip()):
        data["date"] = resolve_relative_date(str(raw_date), today=today)
    elif raw_date:
        data["date"] = str(raw_date).strip()

    return TravelSlots.model_validate(data)


_DEST = re.compile(r"去([\u4e00-\u9fff]{2,6}?)(?:[0-9一二两三四五六七八九十]|日|天|游|玩)")
_DAYS_DIGIT = re.compile(r"(\d+)\s*日")
_DAYS_CN = re.compile(r"([一二两三四五六七八九十])\s*日")
_CN_DAYS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_FROM = re.compile(r"从([\u4e00-\u9fff]{2,8}?)(?:坐|乘|出发|去|到)")
_STAY = re.compile(r"住在([^，。,；;\n]{2,20})")
_BUDGET = re.compile(r"预算\s*([0-9]+)")
_WHEN = re.compile(r"(?:下下周|下周|这周|本周)[一二三四五六日天]|大后天|后天|明天|今天")


def destination_from_query(query: str) -> str:
    match = _DEST.search(query or "")
    return match.group(1) if match else ""


def days_from_query(query: str) -> int:
    match = _DAYS_DIGIT.search(query or "")
    if match:
        return max(1, min(30, int(match.group(1))))
    match = _DAYS_CN.search(query or "")
    if match:
        return _CN_DAYS[match.group(1)]
    return 1


def lodging_from_query(query: str) -> str | None:
    match = _STAY.search(query or "")
    if not match:
        return None
    text = match.group(1).strip()
    return text or None


def slots_from_query(query: str, *, today: date | str | None = None) -> TravelSlots | None:
    """模型抽槽失败时，从原话里取出能确定的出发地、目的地、住宿地。"""
    destination = destination_from_query(query)
    if not destination:
        return None
    origin = _FROM.search(query or "")
    budget = _BUDGET.search(query or "")
    when = _WHEN.search(query or "")
    return TravelSlots(
        origin=origin.group(1) if origin else None,
        destination=destination,
        date=resolve_relative_date(when.group(0), today=today) if when else None,
        days=days_from_query(query),
        budget=budget.group(1) if budget else None,
        preferences="少折腾" if "少折腾" in (query or "") else None,
        lodging=lodging_from_query(query),
    )


def lodging_route_rules(lodging: str | None, destination: str | None, rail: bool) -> str:
    """有住宿地时生成闭环路线文案；空住宿地返回空串。"""
    if not lodging or not str(lodging).strip():
        return ""
    lines = [
        f"- 住宿地：{lodging}",
        "【住宿地闭环路线规则】",
        "1. 先用 amap poi-search 定位住宿地；每天都有一个住宿 POI（同一坐标、day 不同）",
        "2. 每天路线从住宿地出发，最后一个景点之后回到住宿地",
        "3. 每个完整游玩日安排 3 到 4 个景点。少折腾指少换乘、顺路，不是少排景点",
    ]
    if rail:
        lines += [
            f"4. 第一天第一段：{destination}的车站 → 住宿地（先放行李）；车站优先用户点名的站，否则搜{destination}高铁站，不等查票结果",
            "5. 最后一天：回到住宿地之后，再加一段 住宿地 → 车站",
        ]
    return "\n".join(lines)


def format_slots_for_prompt(slots: TravelSlots, *, rail: bool = False) -> str:
    """把已确认槽位写成主智能体容易遵守的约束段落。"""
    lines = [
        "【已确认的旅行约束，请严格遵守】",
        f"- 出发地：{slots.origin}",
        f"- 目的地：{slots.destination}",
        f"- 出行日期：{slots.date}",
        f"- 游玩天数：{slots.days}",
        f"- 预算：{slots.budget}",
        f"- 偏好：{slots.preferences}",
        f"- 出行节奏：{slots.pace}",
    ]
    rules = lodging_route_rules(slots.lodging, slots.destination, rail)
    if rules:
        lines.append(rules)
    return "\n".join(lines)


def clarify_missing(slots: TravelSlots | None) -> list[str]:
    """缺出发地/目的地/日期要问；多日行程没说住宿地也要问，不能编一个酒店。"""
    missing = list(missing_critical(slots))
    if slots is not None and (slots.days or 1) > 1 and not slots.lodging:
        missing.append("lodging")
    return missing


def should_clarify(slots: TravelSlots | None, *, slots_from_client: bool) -> bool:
    """要不要弹出确认卡？

    - 前端已经回传过 slots，且关键字段齐全 → 不再问
    - 否则只要还缺 critical，或多日行程缺住宿地 → 要问
    """
    if slots_from_client and slots is not None and not clarify_missing(slots):
        return False
    return bool(clarify_missing(slots))
