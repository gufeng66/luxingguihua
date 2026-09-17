"""
地图子智能体配置（map_agent）

【小白怎么理解？】
    主智能体像「项目经理」，地图子智能体像「导游顾问」。
    项目经理不会自己去搜景点，而是发任务给导游顾问。
    导游顾问的能力来自本地文件夹 amap-lbs-skill（高德地图 Skill）。

【本文件只导出一个字典】
    DeepAgents 认这种 dict 配置：name / description / system_prompt / skills。
    真正执行时由 create_deep_agent(..., subagents=[map_agent, ...]) 挂载。
"""

# 从 .env 加载 AMAP_WEBSERVICE_KEY，供 Skill 脚本读环境变量
from dotenv import find_dotenv, load_dotenv

# 尽早加载，避免 Skill 运行时读不到 Key
load_dotenv(find_dotenv())

# 写给「导游顾问」看的系统提示词：约束它只做地图相关事
MAP_AGENT_PROMPT = """
你是一名地图规划助手。
你只负责景点搜索、路线分析、地图生成。

规则：
1. 优先筛选 3 到 6 个最值得推荐的景点
2. 输出时说明推荐理由
3. 如果可以生成高德个人地图，优先生成
4. 不要输出冗长原始 POI 数据
5. 优先使用 amap-lbs-skill 提供的能力（阅读 /workspace/skills/amap-lbs-skill/SKILL.md 并按其指引调用 scripts）
6. 路线地图 HTML 必须写到 /workspace/results/maps/{plan_id}.html，不要在正文里塞地图 URL
7. 不要读取 /workspace/skills 以外的项目文件；不要改写 skill
"""

# DeepAgents 子智能体配置（注意 skills 是虚拟路径，对应 CompositeBackend 挂载）
map_agent = {
    # 调度时用的名字：主智能体 task(subagent_type="map_agent")
    "name": "map_agent",
    # 给主智能体看的简介，帮助它决定何时委派
    "description": "负责景点推荐、路线分析、地图生成",
    # 上面的系统提示
    "system_prompt": MAP_AGENT_PROMPT,
    # 只读挂载的高德 Skill 目录（见 planner_service 里的 backend routes）
    "skills": ["/workspace/skills/amap-lbs-skill"],
}
