"""
CLI、Web、测试的导入入口。

实现在 planner/。这里只做两件事：先加载 planner.paths（编码、.env、目录），
再导出调用方和单测 monkeypatch 实际用到的符号。
"""

from __future__ import annotations

try:  # 历史主路径已不用 deepagents；缺包只影响该符号，不阻断导入
    from deepagents import create_deep_agent as _real_create_deep_agent
except ImportError:  # pragma: no cover - 取决于安装环境
    _real_create_deep_agent = None  # type: ignore[assignment]


def create_deep_agent(*args, **kwargs):
    """历史符号/测试用，不是当前主路径；缺 deepagents 时调用给出有诊断意义的报错。"""
    if _real_create_deep_agent is None:
        raise RuntimeError("deepagents 未安装，当前主路径不使用 create_deep_agent")
    return _real_create_deep_agent(*args, **kwargs)


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
