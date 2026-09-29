"""
异步流工具：模型消息抽文本、可取消/可超时的 async for。

【小白怎么理解？】
    浏览器关页时 server 会 set cancel_event；watch_aiter 最多 0.5s 醒来一次检查。
    调度超时则 aclose 生成器并 raise TimeoutError，pipeline 据此降级汇总。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


def ms_since(t0: float) -> int:
    """monotonic 起点到现在的毫秒数。"""
    return int((time.monotonic() - t0) * 1000)


def truncate(text: str, limit: int = 2000) -> str:
    """把工具返回截短再推给前端，避免 SSE 被几万字 POI JSON 撑爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(已截断)"


def message_text(content: Any) -> str:
    """LangChain 消息 content 可能是 str 或 [{type:text}] 列表，统一抽成一段文字。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


async def watch_aiter(
    agen: AsyncIterator[Any],
    cancel_event: asyncio.Event | None = None,
    on_idle: Callable[[], Awaitable[None]] | None = None,
    timeout_seconds: float | None = None,
) -> AsyncIterator[Any]:
    """带取消与可选总超时的异步迭代；超时 raise TimeoutError 并 aclose 生成器。"""
    loop = asyncio.get_running_loop()
    started = loop.time()
    nxt = asyncio.create_task(agen.__anext__())
    closed = False

    async def _close() -> None:
        nonlocal closed, nxt
        if closed:
            return
        closed = True
        if not nxt.done():
            nxt.cancel()
            try:
                await nxt
            except (asyncio.CancelledError, Exception):
                pass
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                await _close()
                return
            wait_s = 0.5
            if timeout_seconds is not None:
                remaining = timeout_seconds - (loop.time() - started)
                if remaining <= 0:
                    await _close()
                    raise asyncio.TimeoutError()
                wait_s = min(0.5, remaining)
            done, _ = await asyncio.wait({nxt}, timeout=wait_s)
            if not done:
                if timeout_seconds is not None and (loop.time() - started) >= timeout_seconds:
                    await _close()
                    raise asyncio.TimeoutError()
                if on_idle is not None:
                    await on_idle()
                continue
            try:
                item = nxt.result()
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                await _close()
                return
            yield item
            nxt = asyncio.create_task(agen.__anext__())
    finally:
        if not closed:
            if not nxt.done():
                nxt.cancel()
                try:
                    await nxt
                except (asyncio.CancelledError, Exception):
                    pass
