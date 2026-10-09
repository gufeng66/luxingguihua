# 评测

默认不调用模型、不连 12306：

```bash
pytest -q -m "not integration"
```

需要 Key 的抽取集成测在 `evals/test_integration.py`，不进上面这条命令。

## 现在能证明的

| 检查 | 位置 | 说明 |
|------|------|------|
| 六章节 + 无票不写车次 | `evals/checkers.py`，运行时 `summary_gate_issues` | 失败重试一次，仍失败则降级落盘 |
| 闸门真的丢掉编造车次 | `evals/test_gate.py::test_fabricated_train_is_retried_then_dropped` | 确定性护栏测试：mock 成文含 `G1234`，`final.content` 不含该车次，`summary_gate=degraded`。没有关掉闸门的对照输出 |
| MCP 连不上 | 同上 | `get_ticket_agent` 返回空，ticket 步骤 `mcp_unavailable` |
| 没调度 map | 同上 | `map_not_dispatched`，且没有 `map_url` |
| 路由与五态 | `evals/test_routing.py`、`evals/test_unit.py` | 离线 |
| token | `final.timings` 的 `prompt_tokens` / `completion_tokens` / `cost_usd` | `cost_usd` 在设置 `TOKEN_USD_PER_M_IN`、`TOKEN_USD_PER_M_OUT` 之前为 0 |

## 黄金集

[evals/golden.json](evals/golden.json) 有 5 条真跑和 5 条离线。离线条目只存 pytest node id，期望值仍在现有测试里。

```bash
python -m evals.run_golden
python -m evals.judge
```

需要可用的 `OPENAI_API_KEY`（DeepSeek 官方 `https://api.deepseek.com`，模型 `deepseek-flash`）和 `AMAP_WEBSERVICE_KEY`。跑之前在 `.env` 设置 `TOKEN_USD_PER_M_IN` / `TOKEN_USD_PER_M_OUT`，否则 `cost_usd` 为 0。本次单价用 DeepSeek Flash 官方面价的 off-peak、cache-miss：$0.15 / $0.60（每百万 token）。未拆 prompt cache，高峰会翻倍。下面表格是当时的实测；`metrics.json` 里的模型名是那次请求记下的旧网关模型。MCP 失败场景用 `http://127.0.0.1:1/mcp`。修订接在自驾那次的 `plan_id` 后面。

2026-10-07 跑了 n=3，每个 attempt 新进程，所以 `ticket_cache_hit` 全是 false。热缓存不在这张表里。Judge 读 snapshot，与被评模型同源，有自偏好。18 份分数 2–5（12 个 5、3 个 4、1 个 3、2 个 2）。低分集中在自驾和 MCP 不可用：`map_context` 往往只剩 HTML 路径，成文仍写出工具原文里没有的景点、里程或票价区间。全部 `summary_gate=pass`，闸门没有拦截这些空上下文下的发挥。

| 场景 | 冷启动中位 / 最大 total_ms | 命中缓存 |
|------|-----------------------------|---------|
| 跨城高铁 | 1279062 / 1451532 | 全量规划长于 TTL，见下方探针 |
| 自驾洛阳 | 1104092 / 1504436 | 同上 |
| HITL 第二轮 | 1038250 / 1053405 | 同上 |
| 修订 map_only | 1647202 / 1897016 | 同上 |
| MCP 不可用 | 918766 / 2362077 | 同上 |

HITL 第一轮「周末去郑州玩」3 次都没有停在确认卡（`hit_clarify=false`），直接出了完整方案。上表只用 `round=full` 的第二轮。有两次 `map_ms` 只有几十毫秒（自驾一次、MCP 不可用一次），对应 judge 的两个 2 分。

### 成本

跨城高铁一次（中位）：prompt 665749、completion 29091、约 $0.12。三次范围 $0.057–$0.218。自驾中位约 $0.025，MCP 不可用中位约 $0.024。修订有一次 prompt 约 468 万、约 $0.73，中位约 $0.18。

### 缓存探针

`python -m evals.cache_probe`（2026-10-07，ModelScope 可达）：

- 冷：agent 非空，`cache_hit=false`，`connect_ms=8329`
- 热：300 秒内第二次，`cache_hit=true`，`connect_ms=0`

跨城三次的冷启动 `ticket_mcp_ms` 中位 8532，和探针的冷握手同一量级。热握手接近 0，不改变 `total_ms`（地图占绝大部分）。

### 并行对照

2026-10-08 跑了 `python -m evals.run_golden --serial`（每种模式 5 次，每次新进程）。原始行在 `evals/serial.json`。`parallel_dispatch` 不用：ticket 比 map 早结束，结果不在同一条工具消息里，这个布尔值几乎恒为 false。

| 模式 | `start_delta_ms` ≈ 0 | delta 中位 | `dispatch_ms` 中位 |
|------|----------------------|------------|-------------------|
| parallel | 4 / 5（另 1 次约 1089 秒） | 0 | 1361483 |
| serial（`FORCE_SERIAL=1`） | 0 / 5（最小约 371 秒） | 719907 | 1431546 |

提示词能把开始时间拉开：serial 五次都不是同 turn。墙钟没有省出一截。parallel 五次里只有 1 次 `dispatch_ms` 低于当次 `map_ms + ticket_ms`，而且只少约 11 秒；另外 4 次调度墙钟高于这个下界，因为主智能体在两次工具之外还有耗时。两种模式的 `dispatch_ms` 中位只差约 1 分钟，小于单次之间的波动。
