"""
评测辅助断言（不联网、不调大模型）

【小白怎么理解？】
    检查汇总 Markdown 是否含固定六章节，以及「没查票时车票建议里不许出现车次号」。
    evals/test_*.py 调这些函数；CI 默认跑确定性单测。
"""

from __future__ import annotations

import re

from planner.routing import TicketState

REQUIRED_SECTIONS = (
    "需求摘要",
    "景点建议",
    "车票建议",
    "预算",
    "行程表",
    "注意事项",
)

TRAIN_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[GDCKT]\d{3,4}(?!\d)", re.IGNORECASE)


def has_required_sections(text: str) -> bool:
    """六个固定章节标题是否都出现在正文里。"""
    body = text or ""
    return all(section in body for section in REQUIRED_SECTIONS)


def missing_sections(text: str) -> list[str]:
    """列出还缺哪些章节标题（方便断言报错信息）。"""
    body = text or ""
    return [s for s in REQUIRED_SECTIONS if s not in body]


def _section_body(text: str, title: str) -> str:
    """取「## 标题」到下一个 ## 之间的正文；找不到标题则空串。"""
    body = text or ""
    marker = f"## {title}"
    start = body.find(marker)
    if start < 0:
        start = body.find(title)
        if start < 0:
            return ""
    rest = body[start + len(marker) :]
    nxt = re.search(r"\n## ", rest)
    return rest[: nxt.start()] if nxt else rest


def find_train_numbers(text: str) -> list[str]:
    """粗匹配 G/D/C/K/T + 3~4 位数字（评测用，不是完整车次规范）。"""
    return TRAIN_NUMBER_RE.findall(text or "")


def assert_no_train_numbers(text: str, *, section: str = "车票建议") -> None:
    chunk = _section_body(text, section)
    trains = find_train_numbers(chunk)
    if trains:
        raise AssertionError(f"{section}章节出现车次号: {trains}")


def assert_has_train_numbers(text: str) -> None:
    trains = find_train_numbers(text or "")
    if not trains:
        raise AssertionError("期望出现车次号但未找到")


def assert_no_fabricated_trains(text: str) -> None:
    """无票场景：仅检查「车票建议」章节。"""
    assert_no_train_numbers(text, section="车票建议")


def assert_ticket_section_consistent(text: str, ticket_reason: TicketState) -> None:
    """票务态与「车票建议」章节措辞、是否允许车次号必须一致。"""
    if not has_required_sections(text):
        raise AssertionError(f"缺少章节: {missing_sections(text)}")
    chunk = _section_body(text, "车票建议")
    if ticket_reason == "ok":
        return
    if ticket_reason == "reused":
        if "沿用" not in chunk and "上次" not in chunk:
            raise AssertionError("reused 应说明车票沿用上次查询")
        return
    assert_no_train_numbers(text, section="车票建议")
    if ticket_reason == "skipped" and "未查票" not in chunk and "无需铁路" not in chunk:
        raise AssertionError("skipped 应说明未查票/无需铁路")
    if ticket_reason == "unavailable" and "不可用" not in chunk and "暂无" not in chunk:
        raise AssertionError("unavailable 应说明服务不可用")
    if ticket_reason == "missed" and "未调度" not in chunk and "未查票" not in chunk:
        raise AssertionError("missed 应说明未调度")
    if ticket_reason == "timeout" and "超时" not in chunk:
        raise AssertionError("timeout 应说明超时")
