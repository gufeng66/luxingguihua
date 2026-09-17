"""
评测辅助断言（不联网、不调大模型）

【小白怎么理解？】
    规划结果是一篇 Markdown。我们约定必须有六个章节标题，
    并且在「没查到票」时不能瞎编 G1234 这种车次号。
    本文件提供几个小函数，给 pytest 用例直接调用。
"""

from __future__ import annotations

# 用正则找车次号
import re

# 最终方案必须出现的六个二级标题（与 SUMMARY_AGENT_PROMPT 一致）
REQUIRED_SECTIONS = (
    "需求摘要",
    "景点建议",
    "车票建议",
    "预算",
    "行程表",
    "注意事项",
)

# 匹配高铁/动车/城际/普快常见车次前缀 + 3~4 位数字
# (?<![A-Za-z0-9]) 避免把单词中间的字母数字误判成车次
TRAIN_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[GDCKT]\d{3,4}(?!\d)", re.IGNORECASE)

# 文中若出现这些「暂无票务」提示，则不应再出现具体车次
NO_TICKET_HINTS = ("暂无", "不可用", "未查询", "无法提供", "缺少票务", "无可靠票务")


def has_required_sections(text: str) -> bool:
    """最终方案是否包含约定的六个章节标题。"""
    body = text or ""
    return all(section in body for section in REQUIRED_SECTIONS)


def missing_sections(text: str) -> list[str]:
    """返回缺失的章节标题列表（便于断言失败时看缺啥）。"""
    body = text or ""
    return [s for s in REQUIRED_SECTIONS if s not in body]


def find_train_numbers(text: str) -> list[str]:
    """从文本里找出疑似火车车次号。"""
    return TRAIN_NUMBER_RE.findall(text or "")


def assert_no_fabricated_trains(text: str) -> None:
    """无可靠票务时的护栏：不该出现硬编的车次。

    - 文本里没有任何车次 → 通过
    - 有车次但又写「暂无/不可用」→ 自相矛盾，失败
    - 有车次且没有「暂无」说明 → 也视为可能编造，失败
    """
    body = text or ""
    trains = find_train_numbers(body)
    if not trains:
        return
    if any(h in body for h in NO_TICKET_HINTS):
        # 若同时出现车次与「暂无」仍视为失败（自相矛盾）
        raise AssertionError(f"声称暂无票务但仍含车次号: {trains}")
    raise AssertionError(f"疑似编造车次号: {trains}")
