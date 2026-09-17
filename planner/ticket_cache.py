"""车票子智能体 TTL 缓存与票务三态判断。"""

from __future__ import annotations

import time
from typing import Any, Literal

from ticket_sub_agent import aclose_mcp_client, build_ticket_agent

from planner.paths import TICKET_CACHE_TTL_SECONDS

_ticket_cache: dict[str, Any] | None = None


async def get_ticket_agent() -> dict[str, Any] | None:
    """带 TTL 的车票子智能体；失败返回 None，由调用方降级。"""
    global _ticket_cache
    now = time.monotonic()
    if _ticket_cache is not None and now - float(_ticket_cache["created_at"]) < TICKET_CACHE_TTL_SECONDS:
        return _ticket_cache["agent"]
    old = _ticket_cache
    _ticket_cache = None
    if old is not None:
        await aclose_mcp_client(old.get("client"))
    try:
        spec, client = await build_ticket_agent()
    except Exception:
        return None
    _ticket_cache = {"agent": spec, "client": client, "created_at": time.monotonic()}
    return spec


def resolve_ticket_reason(
    *,
    ticket_mounted: bool,
    has_ticket_result: bool,
) -> Literal["unavailable", "not_needed", "ok"]:
    """unavailable / not_needed / ok 三态。"""
    if not ticket_mounted:
        return "unavailable"
    if has_ticket_result:
        return "ok"
    return "not_needed"
