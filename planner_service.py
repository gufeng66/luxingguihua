"""
规划核心服务层（整个项目的「大脑中枢」）

app.py（命令行）和 server.py（网页）都调用本模块的 stream_plan()。
实现已拆到 planner/ 子包；本文件保持稳定对外 API，并便于单测 monkeypatch。
"""

from __future__ import annotations

from deepagents import create_deep_agent

# 副作用：编码 / .env / 目录 / LangSmith
import planner.paths  # noqa: F401

from planner.async_utils import watch_aiter
from planner.backends import ReadOnlyBackend, backend
from planner.llm import build_llm, extract_slots
from planner.paths import (
    BASE_DIR,
    CONFIG_DIR,
    MAPS_DIR,
    RESULTS_DIR,
    SKILL_DIR,
    TICKET_CACHE_TTL_SECONDS,
    WEEKDAYS,
    WORKSPACE_DIR,
)
from planner.pipeline import stream_plan
from planner.prompts import (
    MAIN_AGENT_PROMPT,
    MAIN_AGENT_PROMPT_LEGACY,
    SLOT_EXTRACT_PROMPT,
    TICKET_UNAVAILABLE_PATCH,
    build_main_prompt,
)
from planner.summary import stream_summary, ticket_summary_block
from planner.ticket_cache import get_ticket_agent, resolve_ticket_reason

# 兼容旧测试名
_build_main_prompt = build_main_prompt

__all__ = [
    "BASE_DIR",
    "CONFIG_DIR",
    "MAIN_AGENT_PROMPT",
    "MAIN_AGENT_PROMPT_LEGACY",
    "MAPS_DIR",
    "RESULTS_DIR",
    "ReadOnlyBackend",
    "SKILL_DIR",
    "SLOT_EXTRACT_PROMPT",
    "TICKET_CACHE_TTL_SECONDS",
    "TICKET_UNAVAILABLE_PATCH",
    "WEEKDAYS",
    "WORKSPACE_DIR",
    "_build_main_prompt",
    "backend",
    "build_llm",
    "build_main_prompt",
    "create_deep_agent",
    "extract_slots",
    "get_ticket_agent",
    "resolve_ticket_reason",
    "stream_plan",
    "stream_summary",
    "ticket_summary_block",
    "watch_aiter",
]
