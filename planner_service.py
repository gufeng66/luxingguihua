"""
CLI、Web、测试的导入入口。

实现在 planner/。这里只做两件事：先加载 planner.paths（编码、.env、目录），
再导出调用方和单测 monkeypatch 实际用到的符号。
"""

from __future__ import annotations

from deepagents import create_deep_agent

import planner.paths  # noqa: F401

from planner.async_utils import watch_aiter
from planner.llm import build_llm, extract_slots
from planner.paths import RESULTS_DIR
from planner.pipeline import stream_plan
from planner.prompts import build_main_prompt
from planner.routing import resolve_ticket_state
from planner.summary import stream_summary, ticket_summary_block
from planner.ticket_cache import get_ticket_agent

# 单测仍 patch 这个名字
_build_main_prompt = build_main_prompt

__all__ = [
    "RESULTS_DIR",
    "_build_main_prompt",
    "build_llm",
    "create_deep_agent",
    "extract_slots",
    "get_ticket_agent",
    "resolve_ticket_state",
    "stream_plan",
    "stream_summary",
    "ticket_summary_block",
    "watch_aiter",
]
