"""每个规划自己的 12306 连接。不在进程内共享客户端。"""

from __future__ import annotations

import time
from typing import Any

from planner.run_stats import note as note_stat
from planner.ticket_agent import (
    TicketBackendUnavailable,
    aclose_mcp_client,
    build_ticket_agent,
)
from planner.ticket_facts import select_ticket_tools

# 最近一次连接失败的原因。get_ticket_agent() 的返回契约（成功 dict / 失败 None）
# 被测试白名单依赖，不能改成结构化返回值，所以把原因单独留在这里给 pipeline 取。
_LAST_DIAG: dict[str, Any] = {}


def last_ticket_diag() -> dict[str, Any]:
    """最近一次 get_ticket_agent() 的失败诊断；成功时为 ok 诊断。"""
    return dict(_LAST_DIAG)


def _classify(exc: BaseException) -> str:
    """把连接异常归成稳定取值，供质量与告警使用。"""
    if isinstance(exc, TicketBackendUnavailable):
        return "dependency_missing"
    if isinstance(exc, (TimeoutError,)):
        return "budget_exhausted"
    return "connect_failed"


async def get_ticket_agent(budget: Any = None) -> dict[str, Any] | None:
    """新建连接。失败返回 None，原因留在 last_ticket_diag()。"""
    started = time.monotonic()
    try:
        spec, client = await build_ticket_agent(budget)
    except Exception as exc:
        reason = _classify(exc)
        note_stat("ticket_connect", "dependency_missing" if reason == "dependency_missing" else (
            "timed_out" if reason == "budget_exhausted" else "failed"
        ))
        _LAST_DIAG.clear()
        _LAST_DIAG.update(
            {
                "ok": False,
                "reason": reason,
                "dependency_missing": isinstance(exc, TicketBackendUnavailable),
                "connect_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    spec = dict(spec)
    spec["tools"] = select_ticket_tools(list(spec.get("tools") or []))
    spec["_client"] = client
    spec["diag"] = {
        "ok": True,
        "cache_hit": False,
        "connect_ms": int((time.monotonic() - started) * 1000),
        "error": None,
    }
    note_stat("ticket_connect", "succeeded")
    _LAST_DIAG.clear()
    _LAST_DIAG.update(spec["diag"])
    return spec


async def close_ticket_agent(spec: dict[str, Any] | None) -> dict[str, Any]:
    """关闭本请求的 MCP 连接，返回可观测的关闭结果，不只依赖日志。

    返回 {"closed": bool, "error": str | None}：closed=True 表示连接已释放；
    关闭抛异常时同样置 _client=None 避免二次关闭，但把异常回传给调用方写进
    final/error 的 cleanup 字段，让「资源是否真的释放」在 SSE 层可观察。
    """
    if not spec:
        return {"closed": True, "error": None}
    error: str | None = None
    try:
        await aclose_mcp_client(spec.get("_client"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    spec["_client"] = None
    return {"closed": error is None, "error": error}
