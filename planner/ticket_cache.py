"""
12306 MCP 客户端缓存（进程内、5 分钟 TTL）

【小白怎么理解？】
    每次规划都握手 MCP 很慢。成功则缓存 agent 配置 + client；
    失败返回 None，pipeline 发「车票服务不可用」，禁止编票。
    票务态怎么判定不在这里，见 planner.routing。
"""

from __future__ import annotations

import time
from typing import Any

from ticket_sub_agent import aclose_mcp_client, build_ticket_agent

from planner.paths import TICKET_CACHE_TTL_SECONDS

_ticket_cache: dict[str, Any] | None = None
_last_lookup: dict[str, Any] = {"cache_hit": False, "connect_ms": 0.0}


def last_lookup() -> dict[str, Any]:
    """最近一次 get_ticket_agent 是否命中缓存、握手花了多少毫秒（给 timings）。"""
    return dict(_last_lookup)


async def get_ticket_agent() -> dict[str, Any] | None:
    """带 TTL 的车票子智能体；失败返回 None，由调用方降级。"""
    global _ticket_cache
    now = time.monotonic()
    if _ticket_cache is not None and now - float(_ticket_cache["created_at"]) < TICKET_CACHE_TTL_SECONDS:
        _last_lookup["cache_hit"] = True
        _last_lookup["connect_ms"] = 0.0
        return _ticket_cache["agent"]
    old = _ticket_cache
    _ticket_cache = None
    if old is not None:
        await aclose_mcp_client(old.get("client"))
    t0 = time.monotonic()
    try:
        spec, client = await build_ticket_agent()
    except Exception:
        _last_lookup["cache_hit"] = False
        _last_lookup["connect_ms"] = (time.monotonic() - t0) * 1000
        return None
    _last_lookup["cache_hit"] = False
    _last_lookup["connect_ms"] = (time.monotonic() - t0) * 1000
    _ticket_cache = {"agent": spec, "client": client, "created_at": time.monotonic()}
    return spec
