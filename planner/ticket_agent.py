"""
车票子智能体（ticket_agent）。

不在 import 时连 12306。planner/ticket_cache.py 需要时才 await build_ticket_agent()。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import find_dotenv, load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(find_dotenv())

DEFAULT_MCP_12306_URL = "https://mcp.api-inference.modelscope.net/4654213017684d/mcp"
MCP_TIMEOUT_SECONDS = 15
MCP_RETRY_TIMES = 2

TICKET_AGENT_PROMPT = """
你是一名12306车票规划助手。
你只负责车次查询、票价分析、直达/中转建议。

规则：
1. 只要任务中已给出出发地、目的地、出行日期，就优先调用12306相关工具查询（日期应为 YYYY-MM-DD）
2. 如果任务仍缺少出行日期：先调用当前日期工具说明情况，并明确写出「待补充出行日期后可精确查票」，不要擅自按「近期出行」编造具体车次
3. 如果缺少出发地，不要直接停止；先给出「待补充出发地后可精确查票」的说明，同时尽量补充目的地车站和交通预算建议
4. 如预算敏感，优先给出低价方案；如时间敏感，优先给出省时方案
5. 不做真实购票，只做查询与建议
6. 输出必须包含：票务状态、推荐方案、预算提示、还需补充的信息
7. 禁止在没有工具返回的情况下编造车次号或票价
"""


def mcp_12306_url() -> str:
    """12306 MCP 地址：.env 的 MCP_12306_URL 优先，否则用仓库默认演示地址。"""
    return (os.getenv("MCP_12306_URL") or "").strip() or DEFAULT_MCP_12306_URL


async def aclose_mcp_client(client: Any) -> None:
    """尽量关闭 MCP 客户端。适配器方法名可能是 aclose 或 close。"""
    if client is None:
        return
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if asyncio.iscoroutine(result):
        await result


async def build_ticket_agent() -> tuple[dict[str, Any], Any]:
    """连接 12306 MCP，返回 (子智能体配置, client)。失败抛给缓存层降级。"""
    last_error: BaseException | None = None
    for _ in range(MCP_RETRY_TIMES):
        client = MultiServerMCPClient(
            {
                "12306-mcp": {
                    "transport": "streamable_http",
                    "url": mcp_12306_url(),
                }
            }
        )
        try:
            ticket_tools = await asyncio.wait_for(
                client.get_tools(),
                timeout=MCP_TIMEOUT_SECONDS,
            )
            spec = {
                "name": "ticket_agent",
                "description": "负责12306车次查询、票价分析、出发时间建议",
                "system_prompt": TICKET_AGENT_PROMPT,
                "tools": ticket_tools,
            }
            return spec, client
        except Exception as exc:
            last_error = exc
            await aclose_mcp_client(client)
    assert last_error is not None
    raise last_error
