"""
规划核心服务层（整个项目的「对外窗口」）

【小白怎么理解这个文件？】
    app.py（命令行）和 server.py（网页）都写：from planner_service import stream_plan。
    真正的实现已经拆到 planner/ 子包。本文件只做两件事：
        1. import 时先加载 planner.paths（设编码、读 .env、建结果目录）
        2. 把常用符号再导出一遍，保持旧 import 路径稳定，单测也方便 monkeypatch 本模块

不要在这里写新业务逻辑；改 pipeline / routing / summary 即可。
"""

from __future__ import annotations

from deepagents import create_deep_agent

# 副作用：UTF-8、.env、results 目录、LangSmith 开关
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
    SNAPSHOTS_DIR,
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
from planner.routing import TicketState, needs_rail_ticket, resolve_ticket_state
from planner.summary import stream_summary, ticket_summary_block
from planner.ticket_cache import get_ticket_agent

# 兼容旧测试：曾经 patch planner_service._build_main_prompt
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
    "SNAPSHOTS_DIR",
    "TICKET_CACHE_TTL_SECONDS",
    "TICKET_UNAVAILABLE_PATCH",
    "TicketState",
    "WEEKDAYS",
    "WORKSPACE_DIR",
    "_build_main_prompt",
    "backend",
    "build_llm",
    "build_main_prompt",
    "create_deep_agent",
    "extract_slots",
    "get_ticket_agent",
    "needs_rail_ticket",
    "resolve_ticket_state",
    "stream_plan",
    "stream_summary",
    "ticket_summary_block",
    "watch_aiter",
]
