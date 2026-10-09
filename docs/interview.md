# 面试叙述

数字来自 2026-10-07 的黄金集（n=3）、`evals/judge_results.json`、`evals/cache_probe.py`，以及 `test_fabricated_train_is_retried_then_dropped`。单价是 DeepSeek Flash 官方面价 off-peak cache-miss（$0.15 / $0.60 每百万 token）。

## 简历句

- 用规则路由、六章节闸门和票务五态降级，把 mock 成文里的编造车次 `G1234` 挡在落盘之外（确定性单测，`summary_gate=degraded`）。真跑 18 次成文闸门均为 pass；同源 judge 18 份里 15 份 ≥4 分，低分来自工具原文为空时模型仍写细节。
- 跨城规划中位 21.3 分钟、约 67 万 prompt token / $0.12（三次 $0.057–$0.218）。车票 MCP 冷握手约 8.5 秒，同进程 5 分钟内第二次握手为 0。
- map 与 ticket 分工具面挂载：地图只有只读高德 Skill 和 shell，车票只有 MCP，成文是无工具调用。shell 能跑 `node`，不是容器。

## 1. 闸门丢掉编造车次

- 情境：无票时模型仍会写出 `G1234`。
- 动作：成文后、落盘前检查六章节；票务不是可靠态时丢掉车次号，失败重试一次，仍失败则降级。锚点 `evals/test_gate.py`。
- 结果：该测试里 `final.content` 不含 `G1234`，`summary_gate=degraded`。这是护栏测试，没有「关掉闸门」的对照输出。真跑 18 次闸门都是 pass，挡不住的是另一种情况：MCP 不可用或自驾时工具原文很空，模型仍写里程和票价区间，judge 给了 2 分和 3 分。

## 2. 路由用规则

- 情境：要不要连 12306、要不要跑地图，会花钱，也会打外部服务。交给模型就无法离线锁定同一句话走同一条路。
- 动作：`planner/routing.py` 用槽位和关键词决定跳过原因。见 ADR-2。
- 结果：离线测试能断言查票和出图。代价是新说法要加关键词。黄金集里「周末去郑州玩」3 次都没有停在确认卡，说明缺槽位的拦截也不该只靠模型自觉。

## 3. shell 能跑脚本，但不是沙箱

- 情境：`StateBackend` 没有 `execute`，地图子智能体跑不了 `node`，调度会一直拖到超时。换成宿主机 shell 之后，中文 Windows 上 `text=True` 再叠 UTF-8 locale，cmd 的 GBK 会让读线程崩掉。
- 动作：工作目录固定在 skill 目录，`inherit_env=False`，只放行 node 启动变量和 `AMAP_KEY`。见 `planner/backends.py` 和 ADR-3。
- 结果：密钥不再从环境继承。磁盘上的 `.env` 和任意出网仍然没挡住，ADR 里这两条仍是未缓解。

## 4. 并行要看开始时间，不看一个布尔值

- 情境：提示词要求同一条消息里同时调度 map 和 ticket。`parallel_dispatch` 还要求同一次工具消息里两边结果都到。ticket 大约 45 秒结束，map 要十几分钟，结果必然分两次回来，这个布尔值几乎恒为 false。
- 动作：用 `map_ticket_active_delta_ms` 看是否同 turn 发出，用 `dispatch_ms` 对比 `map_ms + ticket_ms`。`FORCE_SERIAL=1` 只改提示词。对照在 `evals/serial.json`（每种模式 5 次）。
- 结果：parallel 模式 4/5 次 delta 为 0，1 次相差约 18 分钟。serial 模式 5/5 次都拉开了，最小约 6 分钟，中位约 12 分钟。墙钟没有省 30%：parallel 的 `dispatch_ms` 中位约 1361 秒，只有 1 次低于 `map_ms + ticket_ms`，而且只少约 11 秒。serial 的 `dispatch_ms` 中位约 1432 秒，和 parallel 的差距小于单次波动。

## 5. 成文不是智能体

- 情境：再包一层带工具的 summary，会在成文阶段继续查票、改文件，闸门也说不清。
- 动作：`stream_summary` 是编排层一次无工具调用。map 只有 skill 和 shell，ticket 只有 MCP 工具。见 ADR-1、ADR-4。
- 结果：跨城中位约 67 万 prompt token、2.9 万 completion token、约 $0.12。修订有一次冲到约 468 万 prompt token、$0.73，说明成本主要在地图子智能体的多轮工具，不在最后那次成文。
