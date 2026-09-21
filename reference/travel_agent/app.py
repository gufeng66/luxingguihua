import asyncio

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

from core.map_agent import map_agent
from core.summary_agent import summary_agent
from core.ticket_agent import ticket_agent
from utils.llm_utils import get_llm_client

MAIN_AGENT_PROMPT = """
你是一名旅游规划总控智能体。
你的职责是根据用户输入，先抽取关键信息，再调度合适的子智能体完成任务。

规则：
1. 先从用户输入中抽取：出发地、目的地、日期/天数、预算、偏好、出行节奏
2. 如果用户没有明确说明游玩天数，默认按 1 天规划
3. 如果用户没有明确说明预算，默认按中等预算规划
4. 如果用户没有明确说明偏好，默认按“经典景点 + 少折腾”规划
5. 如果用户没有明确说明出行节奏，默认按“舒适型节奏”规划
6. 景点、路线、地图相关问题交给 map_agent
7. 火车票、车次、票价、时间建议交给 ticket_agent
8. 最终结果交给 summary_agent 汇总
9. 输出必须使用中文
10. 不做真实购票，只做规划和建议
"""

# 获取大模型实例
llm = get_llm_client()

# 创建主智能体
main_agent = create_deep_agent(
    name="旅游规划助手",
    model=llm,
    system_prompt=MAIN_AGENT_PROMPT,
    # 配置存储后端
    backend=FilesystemBackend(root_dir=".",virtual_mode=True),
    # 配置长期记忆
    memory=["/memory/AGENTS.md"],
    # 配置子智能体
    subagents=[map_agent,ticket_agent,summary_agent]
)

async def main(query: str):
    # 执行
    result =  main_agent.astream(
        input={
            "messages":[
                {"role": "user", "content": query}
            ]
        }
    )
    # 创建一个保存工具id和名字的字典
    tool_dict = {}
    # 遍历
    async for chunk in result:
        # print(chunk)
        # 遍历获取每一个字典对象
        for key, value in chunk.items():
            # 判断是否是调用的模型
            if key == "model":
                # 获取AIMessage对象
                ai_message = value["messages"][-1]
                # 获取工具调用的列表
                tool_calls = ai_message.tool_calls
                # 判断是让调用工具还是最终输出结果
                if tool_calls:
                    # 遍历得到每一个工具
                    for tool_call in tool_calls:
                        # 获取工具的名称
                        tool_name = tool_call["args"]["subagent_type"]
                        # 获取调用工具时的参数
                        args = tool_call["args"]["description"]
                        # 获取工具的id
                        tool_id = tool_call["id"]
                        # 保存工具id和名字
                        tool_dict[tool_id] = tool_name
                        print(f"调用 【{tool_name}】 子智能体，传入的参数是：{args}")
                else:
                    # 获取最终的输出
                    final_output = ai_message.content
                    print(f"旅游规划助手最终的输出是：{final_output}")
                    return final_output
            elif key == "tools":
                # 获取工具消息对象
                tool_message = value["messages"][-1]
                # 获取工具响应的内容
                content = tool_message.content
                # 获取工具的id
                tool_id = tool_message.tool_call_id
                # 从字典中获取工具的名称
                tool_name = tool_dict[tool_id]
                print(f"子智能体 【{tool_name}】 返回的结果是：{content}")


if __name__ == "__main__":
    asyncio.run(main("帮我做一个北京到天津一日游的旅游规划"))