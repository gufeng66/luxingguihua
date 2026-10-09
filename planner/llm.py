"""
大模型：建客户端 + 把用户一句话抽成 TravelSlots。

【小白怎么理解？】
    密钥走 OpenAI 兼容协议（.env 的 OPENAI_BASE_URL）。
    extract_slots 失败不要在这里吞掉：pipeline 捕获后降级成「原模式规划」。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from slots import ExtractedSlots, TravelSlots, _parse_today, normalize_extracted

from planner.paths import WEEKDAYS
from planner.prompts import SLOT_EXTRACT_PROMPT
from planner.usage import note_llm

logger = logging.getLogger("travel_planner")

_COORD = re.compile(r"\d{2,3}\.\d+\s*[,，]\s*\d{2,3}\.\d+")
_TOOL_STUB = "工具结果已省略。请按任务书用参考坐标写完地图 HTML，不要再查询。"


def _is_content_risk(exc: BaseException) -> bool:
    return "Content Exists Risk" in str(exc)


def _keep_tool_text(text: str, limit: int) -> str:
    """审核重试只留坐标行。整段换成「已省略」会让 map_agent 拒绝写 HTML。"""
    picked = [line.strip()[:240] for line in text.splitlines() if line.strip() and (_COORD.search(line) or "坐标" in line)]
    body = "\n".join(picked) if picked else text
    if len(body) > limit:
        return body[:limit] + "\n[内容过长已截断]"
    return body


def shrink_for_content_filter(messages: list, *, drop_tools: bool = False) -> tuple[list, bool]:
    """丢掉区域旗帜符号；工具输出过长时截断，仍保留坐标，避免整段请求被审核拒绝。"""
    out = []
    changed = False
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(message, ToolMessage) and drop_tools:
            if content != _TOOL_STUB:
                changed = True
                message = message.model_copy(update={"content": _TOOL_STUB})
            out.append(message)
            continue
        if not isinstance(content, str):
            out.append(message)
            continue
        new = "".join(ch for ch in content if not (0x1F1E6 <= ord(ch) <= 0x1F1FF))
        if isinstance(message, ToolMessage) and len(new) > 2000:
            new = _keep_tool_text(new, 2000)
        if new != content:
            changed = True
            message = message.model_copy(update={"content": new})
        out.append(message)
    return out, changed


def _install_content_risk_retry(llm):
    original = llm._agenerate

    async def _agenerate(messages, stop=None, run_manager=None, **kwargs):
        try:
            return await original(messages, stop, run_manager, **kwargs)
        except Exception as exc:
            if not _is_content_risk(exc):
                raise
            last = exc
            for drop in (False, True):
                trimmed, changed = shrink_for_content_filter(messages, drop_tools=drop)
                if not changed:
                    continue
                try:
                    return await original(trimmed, stop, run_manager, **kwargs)
                except Exception as again:
                    last = again
                    if not _is_content_risk(again):
                        raise
        logger.warning("content_risk exhausted: %s", last)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="内容审核拦截了本轮模型调用。请立刻结束，不要再调用工具。")
                )
            ]
        )

    llm._agenerate = _agenerate
    return llm


def build_llm():
    """初始化 DeepSeek 官方接口上的 deepseek-flash。"""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY。请复制 .env.example 为 .env 并填写 Key。")
    llm = init_chat_model(
        model="deepseek-flash",
        model_provider="openai",
        api_key=api_key,
        base_url=base_url,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return _install_content_risk_retry(llm)


async def extract_slots(
    query: str,
    llm: Any,
    *,
    today: date | str | None = None,
) -> TravelSlots:
    """结构化抽取旅行槽位；失败由调用方捕获并降级。"""
    base = _parse_today(today)
    prompt = SLOT_EXTRACT_PROMPT.format(
        today=base.isoformat(),
        weekday=WEEKDAYS[base.weekday()],
    )
    structured = llm.with_structured_output(ExtractedSlots, method="json_mode", include_raw=True)
    packed = await structured.ainvoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=query),
        ]
    )
    if isinstance(packed, dict) and "parsed" in packed:
        note_llm(packed.get("raw"))
        result = packed.get("parsed")
    else:
        result = packed
    if not isinstance(result, (ExtractedSlots, dict)):
        result = ExtractedSlots.model_validate(result)
    return normalize_extracted(result, today=base)
