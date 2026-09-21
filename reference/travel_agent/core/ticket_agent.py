import asyncio

from utils.mcp_utils import get_mcp_client

TICKET_AGENT_PROMPT = """
你是一名12306车票规划助手。
你只负责车次查询、票价分析、直达/中转建议。

规则：
1. 只要用户已经给出出发地、目的地、出行日期中的关键信息，就优先调用12306相关工具查询
2. 如果缺少出行日期，先调用当前日期工具，再按“近期出行”给出默认规划
3. 如果缺少出发地，不要直接停止；先给出“待补充出发地后可精确查票”的说明，同时尽量补充目的地车站和交通预算建议
4. 如预算敏感，优先给出低价方案；如时间敏感，优先给出省时方案
5. 不做真实购票，只做查询与建议
6. 输出必须包含：票务状态、推荐方案、预算提示、还需补充的信息
"""


async def get_ticket_agent():
    # 获取mcp客户端
    mcp_client = get_mcp_client()
    # 获取工具列表
    tools = await mcp_client.get_tools()
    # 创建子智能体
    ticket_agent = {
        "name": "车票子智能体",
        "description": "负责12306车次查询、票价分析、出发时间建议",
        "system_prompt": TICKET_AGENT_PROMPT,
        # 配置工具
        "tools":tools
    }
    return ticket_agent

# 调用函数获取返回的结果
ticket_agent = asyncio.run(get_ticket_agent())