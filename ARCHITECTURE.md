# 架构

一次规划只有一条事件流：`planner/pipeline.py` 的 `stream_plan`。CLI（`app.py`）和 Web（`server.py` 的 `POST /api/plan`）都消费它。

```text
用户原话
  → 抽槽位（json_mode）；缺出发地 / 目的地 / 日期则 clarify 后结束本轮
  → 规则路由：要不要查票、要不要地图（planner/routing.py）
  → 需要票时连接 12306 MCP（5 分钟缓存；失败则 ticket 不可用）
  → main_agent 调度 map_agent / ticket_agent
  → stream_summary 裸 LLM 写成六章节
  → 闸门：缺章节或无票却出现车次号 → 带约束重试一次 → 仍失败则降级成文
  → 落盘 Markdown + 快照，yield final（含耗时与 token）
```

| 组件 | 做什么 | 不做什么 |
|------|--------|----------|
| `main_agent` | 按已确认槽位调度子智能体 | 不写最终长文 |
| `map_agent` | 景点、路线、地图 HTML | 不查 12306 |
| `ticket_agent` | 车次与票价 | 不编造没有工具返回的车次 |
| `stream_summary` | 无工具的一次成文 | 不是可再调工具的 agent |
| 路由 | 关键词 + 槽位，纯函数 | 不用 LLM |

决策记录：

- [ADR-1 为什么 summary 不是 agent](docs/adr/0001-summary-is-not-an-agent.md)
- [ADR-2 为什么路由用规则](docs/adr/0002-routing-is-rules.md)
- [ADR-3 shell 边界](docs/adr/0003-shell-boundary.md)

地图脚本在 `amap-lbs-skill/`，经只读挂载给模型读，执行发生在 `LocalShellBackend`（工作目录是 skill 目录）。方案与快照在 `workspace/results/`。
