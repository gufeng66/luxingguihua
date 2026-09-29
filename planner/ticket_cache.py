"""
12306 MCP 客户端缓存（进程内）。

成功缓存 5 分钟。失败后 30 秒内直接返回 None，不再握手
（一次失败最坏约 15s × 2 次重试）。asyncio.Lock 把握手串成一次，
避免并发请求各建一个 client，后写入的覆盖先写入的、连接泄漏。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from planner.ticket_agent import aclose_mcp_client, build_ticket_agent

from planner.paths import TICKET_CACHE_TTL_SECONDS

# ponytail: 全局一把锁、失败冷却 30s。多 MCP 地址再拆成按 url 加锁。
FAIL_COOLDOWN_SECONDS = 30

_lock = asyncio.Lock()
_ticket_cache: dict[str, Any] | None = None
_fail_until = 0.0
_last_lookup: dict[str, Any] = {"cache_hit": False, "connect_ms": 0.0}


def last_lookup() -> dict[str, Any]:
    """最近一次 get_ticket_agent 是否命中缓存、握手花了多少毫秒（给 timings）。"""
    return dict(_last_lookup)


async def get_ticket_agent() -> dict[str, Any] | None:
    """带 TTL 的车票子智能体；失败或冷却期内返回 None，由调用方降级。"""
    global _ticket_cache, _fail_until
    async with _lock:
        now = time.monotonic()
        if _ticket_cache is not None and now - float(_ticket_cache["created_at"]) < TICKET_CACHE_TTL_SECONDS:
            _last_lookup["cache_hit"] = True
            _last_lookup["connect_ms"] = 0.0
            return _ticket_cache["agent"]
        if now < _fail_until:
            _last_lookup["cache_hit"] = False
            _last_lookup["connect_ms"] = 0.0
            return None
        old = _ticket_cache
        _ticket_cache = None
        if old is not None:
            await aclose_mcp_client(old.get("client"))
        t0 = time.monotonic()
        try:
            spec, client = await build_ticket_agent()
        except Exception:
            _fail_until = time.monotonic() + FAIL_COOLDOWN_SECONDS
            _last_lookup["cache_hit"] = False
            _last_lookup["connect_ms"] = (time.monotonic() - t0) * 1000
            return None
        _fail_until = 0.0
        _last_lookup["cache_hit"] = False
        _last_lookup["connect_ms"] = (time.monotonic() - t0) * 1000
        _ticket_cache = {"agent": spec, "client": client, "created_at": time.monotonic()}
        return spec
