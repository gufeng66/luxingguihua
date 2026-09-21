"""
规划核心子包（真正干活的代码都在这里）

【小白怎么理解？】
    根目录的 planner_service.py 只是「对外窗口」：把本包里的函数再导出一遍，
    方便 app.py / server.py / 单测继续写 from planner_service import stream_plan。
    改逻辑请改本目录，不要在 planner_service.py 里堆实现。

文件职责：
    pipeline.py     一次规划的事件流（stream_plan）
    dispatch.py     解析主智能体 astream（map/ticket 进度与原文）
    routing.py      要不要查票/出地图、修订走哪条路（纯函数）
    llm.py          建大模型、抽槽位
    prompts.py      主智能体系统提示
    summary.py      流式成文 + 落盘 + 快照
    ticket_cache.py 12306 MCP 连接缓存
    backends.py     智能体看到的虚拟文件系统
    async_utils.py  可取消、可超时的异步迭代
    paths.py        目录常量、Windows 编码、LangSmith 开关
"""
