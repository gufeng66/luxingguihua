"""异步流工具：消息文本处理与可取消迭代。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


def truncate(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(已截断)"


def message_text(content: Any) -> str:
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
) -> AsyncIterator[Any]:
    """带取消检查的异步迭代：0.5s 空闲时跑 on_idle（如检测 HTTP 断连）。"""
    nxt = asyncio.create_task(agen.__anext__())
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                nxt.cancel()
                await agen.aclose()
                return
            done, _ = await asyncio.wait({nxt}, timeout=0.5)
            if not done:
                if on_idle is not None:
                    await on_idle()
                continue
            try:
                item = nxt.result()
            except StopAsyncIteration:
                return
            yield item
            nxt = asyncio.create_task(agen.__anext__())
    finally:
        if not nxt.done():
            nxt.cancel()
            try:
                await nxt
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass
