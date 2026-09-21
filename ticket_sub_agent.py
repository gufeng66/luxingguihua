"""
车票子智能体（ticket_agent）— 懒加载构建

【小白怎么理解？】
    车票顾问不自己「编」火车时刻表，而是通过 MCP 协议去问远程 12306 服务。
    MCP ≈ 一种让大模型安全调用外部工具的标准接口。
    Streamable HTTP ≈ 用普通网址就能连上远程工具，不必自己装 12306 客户端。

【为什么不能在 import 时就连接？】
    一 import 就联网会让：启动变慢、单测变难、远程挂了整个程序起不来。
    所以只导出 async 函数 build_ticket_agent()，由 planner_service 在需要时 await。
"""

# 现代类型注解
from __future__ import annotations

# 超时控制、判断协程
import asyncio
# 通用类型标注
from typing import Any

import os

from dotenv import find_dotenv, load_dotenv
# LangChain 官方 MCP 适配器：把远程工具变成可调用的 StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(find_dotenv())

DEFAULT_MCP_12306_URL = "https://mcp.api-inference.modelscope.net/4654213017684d/mcp"


def mcp_12306_url() -> str:
    """12306 MCP 地址：.env 的 MCP_12306_URL 优先，否则用仓库默认演示地址。"""
    return (os.getenv("MCP_12306_URL") or "").strip() or DEFAULT_MCP_12306_URL
# 单次拉取工具列表最多等 15 秒，超时算失败
MCP_TIMEOUT_SECONDS = 15
# 最多尝试 2 次（失败再试一次）
MCP_RETRY_TIMES = 2

# 写给「车票顾问」的系统提示
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


async def aclose_mcp_client(client: Any) -> None:
    """尽量关闭 MCP 客户端，释放网络资源。

    不同版本适配器方法名可能是 aclose 或 close；没有就什么都不做。
    """
    # 空客户端直接返回
    if client is None:
        return
    # 优先异步关闭，其次同步 close
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    # 调用关闭方法
    result = closer()
    # 若返回的是协程，需要 await
    if asyncio.iscoroutine(result):
        await result


async def build_ticket_agent() -> tuple[dict[str, Any], Any]:
    """连接 12306 MCP，返回 (子智能体配置字典, MCP client)。

    成功：配置里带 tools 列表，可交给 create_deep_agent。
    失败：重试后仍失败则把异常抛给调用方（planner 会降级为「无票务」）。
    """
    # 记录最后一次异常，方便重试耗尽后抛出
    last_error: BaseException | None = None
    # 循环重试
    for _ in range(MCP_RETRY_TIMES):
        # 创建指向远程 12306 的 MCP 客户端（Streamable HTTP）
        client = MultiServerMCPClient(
            {
                "12306-mcp": {
                    "transport": "streamable_http",
                    "url": mcp_12306_url(),
                }
            }
        )
        try:
            # 带超时地拉取远程工具列表（查票、查站等）
            ticket_tools = await asyncio.wait_for(
                client.get_tools(),
                timeout=MCP_TIMEOUT_SECONDS,
            )
            # 组装 DeepAgents 认的子智能体配置
            spec = {
                "name": "ticket_agent",
                "description": "负责12306车次查询、票价分析、出发时间建议",
                "system_prompt": TICKET_AGENT_PROMPT,
                "tools": ticket_tools,
            }
            # 成功：把配置和 client 一起返回（client 留给缓存/关闭用）
            return spec, client
        except Exception as exc:
            # 记下错误，关掉这次失败的 client，再试
            last_error = exc
            await aclose_mcp_client(client)
    # 理论上 last_error 一定有值
    assert last_error is not None
    # 抛给上层做「车票服务不可用」降级
    raise last_error
