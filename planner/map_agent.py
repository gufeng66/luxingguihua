"""
地图子智能体配置（map_agent）。

主智能体只发任务。本配置只让子智能体用高德 skill 找候选，
不写最终 HTML，不猜坐标。排程和地图由编排层生成。
"""

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

MAP_AGENT_PROMPT = """
你是一名地图规划助手。
你只能调用 amap_poi_search。参数只有 city 和 keywords。

规则：
1. 不要自己排每天去几个，不要编造坐标或 poi_id，不要写地图 HTML
2. 用户没点名必去时，must_visit 必须是 false
3. 住宿地没说清楚就不要编造酒店
4. 结束时只输出一个 JSON 数组，可以用一层 markdown 代码围栏。元素字段：
   name, city, place_type, must_visit, reason, visit_minutes, visit_duration_source
   只有用户明确说了某个景点停留多久时，才填写 visit_minutes（分钟）与
   visit_duration_source="user"；不能为凑行程自行猜测时长。
   坐标和 poi_id 由系统向高德复核，你写了也不会被采用
"""

map_agent = {
    "name": "map_agent",
    "description": "负责景点候选检索",
    "system_prompt": MAP_AGENT_PROMPT,
    "skills": ["/workspace/skills/amap-lbs-skill"],
}
