"""一次规划里收集到的 token。价格用环境变量，不写死厂商价目。"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

_current: ContextVar["TokenUsage | None"] = ContextVar("plan_token_usage", default=None)
_call: ContextVar[list[int] | None] = ContextVar("plan_usage_call", default=None)


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pair(msg: Any) -> tuple[int, int]:
    meta = getattr(msg, "usage_metadata", None)
    if not isinstance(meta, dict):
        resp = getattr(msg, "response_metadata", None)
        meta = {}
        if isinstance(resp, dict):
            raw = resp.get("token_usage") or resp.get("usage") or {}
            if isinstance(raw, dict):
                meta = raw
    prompt = _as_int(meta.get("input_tokens") or meta.get("prompt_tokens"))
    completion = _as_int(meta.get("output_tokens") or meta.get("completion_tokens"))
    return prompt, completion


class TokenUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def add_call(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion

    def as_dict(self) -> dict[str, int | float]:
        inn = float(os.getenv("TOKEN_USD_PER_M_IN") or 0)
        out = float(os.getenv("TOKEN_USD_PER_M_OUT") or 0)
        cost = (self.prompt_tokens * inn + self.completion_tokens * out) / 1_000_000
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(cost, 6),
        }


def begin_usage() -> TokenUsage:
    usage = TokenUsage()
    _current.set(usage)
    return usage


def current_usage() -> TokenUsage:
    usage = _current.get()
    if usage is None:
        usage = begin_usage()
    return usage


def note_llm(msg: Any) -> None:
    """流式最后一帧通常带本次调用的累计 usage；在 usage_call 里取最大值，结束时加一次。"""
    prompt, completion = _pair(msg)
    if prompt == 0 and completion == 0:
        return
    holder = _call.get()
    if holder is None:
        current_usage().add_call(prompt, completion)
        return
    holder[0] = max(holder[0], prompt)
    holder[1] = max(holder[1], completion)


class usage_call:
    def __enter__(self) -> "usage_call":
        self._token = _call.set([0, 0])
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            holder = _call.get()
        except LookupError:
            holder = None
        try:
            _call.reset(self._token)
        except ValueError:
            # 热重载掐断 SSE 时，退出发生在另一个 context，reset 会抛。
            _call.set(None)
        if holder and (holder[0] or holder[1]):
            current_usage().add_call(holder[0], holder[1])
