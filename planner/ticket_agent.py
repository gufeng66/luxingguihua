"""
车票子智能体（ticket_agent）。

不在 import 时连 12306。planner/ticket_cache.py 需要时才 await build_ticket_agent()。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

DEFAULT_MCP_12306_URL = "https://mcp.api-inference.modelscope.net/4654213017684d/mcp"
MCP_TIMEOUT_SECONDS = 15
MCP_RETRY_TIMES = 2


class TicketBackendUnavailable(RuntimeError):
    """缺可选依赖（langchain_mcp_adapters）时抛。

    只表示「本机没装票务适配器」，不是查询失败；调用方据此把票务标为
    unavailable 并保留 dependency_missing 诊断，绝不假装查过票。
    """


# 惰性填充：导入时不去碰可选依赖，首次真正连票时才解析。
# 同时也让测试可以 patch planner.ticket_agent.MultiServerMCPClient。
MultiServerMCPClient: Any = None


def _mcp_client_class() -> Any:
    """惰性取 MCP 客户端类。缺包时抛 TicketBackendUnavailable，不拖垮模块导入。"""
    global MultiServerMCPClient
    if MultiServerMCPClient is not None:
        return MultiServerMCPClient
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient as _client_class
    except ImportError as exc:  # pragma: no cover - 取决于安装环境
        raise TicketBackendUnavailable(
            "langchain_mcp_adapters 未安装，无法连接 12306 MCP"
        ) from exc
    MultiServerMCPClient = _client_class
    return _client_class

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


async def build_ticket_agent(budget: Any = None) -> tuple[dict[str, Any], Any]:
    """连接 12306 MCP，返回 (子智能体配置, client)。每次发现工具都计入外部尝试。

    缺依赖直接抛 TicketBackendUnavailable，不返回半成品 spec。
    """
    client_class = _mcp_client_class()
    last_error: BaseException | None = None
    for _ in range(MCP_RETRY_TIMES):
        if budget is not None and not budget.note_attempt():
            raise TimeoutError("ticket connect budget exhausted")
        client = client_class(
            {
                "12306-mcp": {
                    "transport": "streamable_http",
                    "url": mcp_12306_url(),
                }
            }
        )
        timeout = MCP_TIMEOUT_SECONDS
        if budget is not None:
            timeout = min(timeout, budget.timeout_for(MCP_TIMEOUT_SECONDS))
        try:
            if timeout <= 0:
                raise TimeoutError("ticket connect budget exhausted")
            ticket_tools = await asyncio.wait_for(
                client.get_tools(),
                timeout=timeout,
            )
            spec = {
                "name": "ticket_agent",
                "description": "负责12306车次查询、票价分析、出发时间建议",
                "system_prompt": TICKET_AGENT_PROMPT,
                "tools": ticket_tools,
            }
            return spec, client
        except asyncio.CancelledError:
            await aclose_mcp_client(client)
            raise
        except Exception as exc:
            last_error = exc
            await aclose_mcp_client(client)
    assert last_error is not None
    raise last_error
