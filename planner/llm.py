"""
大模型：建客户端 + 把用户一句话抽成 TravelSlots。

【小白怎么理解？】
    密钥走 OpenAI 兼容协议（DeepSeek 的 OPENAI_BASE_URL）。
    extract_slots 失败不要在这里吞掉：pipeline 捕获后降级成「原模式规划」。
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from slots import ExtractedSlots, TravelSlots, normalize_extracted

from planner.paths import WEEKDAYS
from planner.prompts import SLOT_EXTRACT_PROMPT


def build_llm():
    """初始化 DeepSeek-V4.1-Flash（API 模型名：deepseek-flash）。"""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY。请复制 .env.example 为 .env 并填写 DeepSeek Key。")
    return init_chat_model(
        model="deepseek-flash",
        model_provider="openai",
        api_key=api_key,
        base_url=base_url,
    )


async def extract_slots(
    query: str,
    llm: Any,
    *,
    today: date | str | None = None,
) -> TravelSlots:
    """结构化抽取旅行槽位；失败由调用方捕获并降级。"""
    base = date.today() if today is None else (
        today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
    )
    prompt = SLOT_EXTRACT_PROMPT.format(
        today=base.isoformat(),
        weekday=WEEKDAYS[base.weekday()],
    )
    structured = llm.with_structured_output(ExtractedSlots)
    result = await structured.ainvoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=query),
        ]
    )
    if isinstance(result, ExtractedSlots):
        return normalize_extracted(result, today=base)
    if isinstance(result, dict):
        return normalize_extracted(result, today=base)
    return normalize_extracted(ExtractedSlots.model_validate(result), today=base)
