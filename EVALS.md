# 评测

默认不调用模型、不连 12306：

```bash
pytest -q -m "not integration"
```

需要 Key 的抽取集成测在 `evals/test_integration.py`，不进上面这条命令。

## 当前离线复核（2026-11-14，当前）

在当前未提交工作区执行：

```bash
python -X utf8 -m pytest -q -m "not integration"
```

结果：**501 passed, 2 deselected in 17.66s**（`travel-planner` Conda 环境，Python 3.11.16，完整项目依赖）。这是未提交工作区的本地证据，尚未绑定可复查的 commit SHA；整改过程中出现过、现已全部收敛的失败及处理方式见下节。

离线全绿只证明控制层与既定用例，**不证明**：CI 3.11 / 3.12 通过、`deepagents` / MCP 适配器安装后的完整路径可用、真实模型 / 12306 / 高德 / Tavily 可用、黄金集成绩、iframe `srcdoc` 与 `/results/maps` 两分支的真实浏览器渲染、POI 闸门的误报率 / 漏报率。

### 整改期间的失败及其收敛（2026-11-13，历史记录）

整改中期基线为 **418 passed, 4 failed, 2 skipped, 2 deselected**。四条失败全部在 `evals/test_itinerary.py` 与 `evals/test_quality.py`，其中前三条是整改开始前就已存在的未提交 WIP 缺陷（密度与 POI 匹配策略），第四条是旧测试与新票务六态语义冲突：

| 用例 | 当时现象 | 收敛方式 |
|------|------|------|
| `test_pick_matches_official_poi_name` | `_pick` 对「长城（八达岭」返回 `None`（`八达岭长城` 与 `八达岭水关长城` 同分，同分保护直接返回 `None`） | 修 `planner/map_fallback.py` 的同分选取逻辑，不放宽断言 |
| `test_alias_must_is_not_unlocated` / `test_slack_applies_without_lodging` | `schedule_status` 得 `candidate`、期望 `checked_under_assumptions` | 修 `planner/route_plan.py` 的 WIP 判定 |
| `test_fixed_responses_keep_the_same_itinerary` | `place_calls` 得 `('景点','4','110000')`、期望 `('景点','3','110000')` | 确认 `4` 是密度目标推导的正确结果（`candidate_retrieval_target`），把 `page_size` 语义写进 docstring、诊断写进 `constraints.candidate_retrieval`、补纯函数测试后更新期望；连带暴露的下游 `visits` 断言按「未知耗时如实置 `None`」重定基线，并补反编造护栏 |
| `test_reused_ticket_after_parent_failure_keeps_failure_reason` | 旧词表用 `failed` / `empty`（非法六态）并断言「本轮沿用」「余票未刷新」 | 改为合法三失败态 `unavailable` / `missed` / `timeout`，断言必须说「本次未重新查询」「余票未知」且不得声称「余票未刷新」 |

以上收敛均为修实现或对齐新契约，**没有放宽校验、没有删除告警**。

## 当前离线复核（2026-11-04，历史）

在当前未提交工作区，使用现有 `travel-planner` Python 环境执行：

```bash
python -X utf8 -m pytest -q -m "not integration" -p no:cacheprovider --basetemp=.test-tmp-review
```

结果（历史参考 / 当时基线）：**162 passed, 2 deselected in 12.57s**。这两项是未执行的集成测试；本次没有运行真实模型、高德或票务验收。Windows 测试临时目录访问受限时换用允许访问的执行方式，未修改目录权限；测试临时目录在结束后清理。

| 本轮回归 | 证据 | 验证边界 |
|------|------|------|
| 高铁改自驾、修订为多日时重新确认住宿 | `evals/test_revision_timeout.py` | 注入槽位抽取，验证真实流水线分支；不证明真实模型抽取正确率 |
| 首段或后续段输出卡住后超时并关闭流，最终确定性降级 | 同上 | 使用停滞的假模型；不证明远端提供商已经停止计费 |
| 保留错误、超时、无终态；执行黄金集 `expect`；失败返回非零 | `evals/test_golden_runner.py` | 验证评测器本身，不是新的线上成绩 |
| 回放成功和异常后恢复函数、环境、路径并清理临时产物 | `evals/test_replay_isolation.py` | 顺序离线回放；显式传 `output_dir` 时由调用方保管产物 |

黄金集现在分别记录流程终态、期望是否通过和质量状态，`partial` 不会自动算执行失败，符合用例预期的降级也可通过。所有轮次留在 `scenarios.*.runs`，延迟统计另取有耗时的样本。本地追踪配置在测试期间隔离，测试不访问 LangSmith。

**待做线上复核：** 黄金集已在运行时按 `Asia/Shanghai` 的 `--as-of`/`GOLDEN_AS_OF` 生成严格晚于基准日的下一个周六；本次未刷新历史耗时、成本和并行收益数据。

## 现在能证明的

| 检查 | 位置 | 说明 |
|------|------|------|
| 六章节 + 无票不写车次 | `evals/checkers.py`，运行时 `summary_gate_issues` | 失败重试一次，仍失败则降级落盘 |
| 景点名/耗时/顺序不得越界 | `evals/test_poi_gate.py` | 拿 `itinerary` 生成的白名单逐项比对；含「行程外景点」「未核实耗时」「reference 写成已实测」「无结构化行程却断言景点」四类 |
| 闸门真的丢掉编造车次 | `evals/test_gate.py::test_fabricated_train_is_retried_then_dropped` | 确定性护栏测试：mock 成文含 `G1234`，`final.content` 不含该车次，`summary_gate=degraded`。没有关掉闸门的对照输出 |
| MCP 连不上 | 同上 | `get_ticket_agent` 返回空，ticket 步骤 `mcp_unavailable` |
| 可选依赖边界 | `evals/test_contracts.py`、`evals/test_quality.py` | 依赖存在时走完整导入；缺依赖的兼容路径仍有 mock 回归，票务会落 `unavailable` 并保留 `dependency_missing` |
| 复用不说错话 | `evals/test_ticket_reuse_state.py` | 父 `ok` / `skipped` / `unavailable` / `missed` / `timeout` / `reused` 六种来源各自出对应文案，三元组不互相覆盖 |
| SSE 超时与断开释放名额 | `evals/test_server_lifecycle.py` | 全 mock，不触网；断言超时后发 `error(code=sse_timeout)`、之后无 `final`、inflight 名额已释放 |
| URL scheme 与白名单 | `evals/test_urls.py` | 危险 scheme / userinfo / 非默认端口 / 私网地址 / 非白名单主机全部拒绝 |
| 前端请求竞态与 URL 出口 | `evals/frontend_race_check.mjs`、`evals/frontend_url_check.mjs` | node 脚本，vm 沙箱加载 `frontend/app.js`；旧请求事件不得改新请求状态，所有 `:href` 必须走 `safeHref` |
| 没调度 map | 同上 | `map_not_dispatched`，且没有 `map_url` |
| 路由与票务六态 | `evals/test_routing.py`、`evals/test_unit.py` | 离线；`reused` 由修订分支赋值 |
| 闭馆改路线、地图与行程表一致 | `evals/test_itinerary.py` | 注入证据和耗时，不访问 Tavily / 高德 |
| 出行日期未知不伪造首日 | `evals/test_itinerary.py`、`evals/test_density.py` | `day_window` 返回 `unknown`、`schedule_status=date_unknown`、候选缺口为未知 |
| token | `final.timings` 的 `prompt_tokens` / `completion_tokens` / `cost_usd` | 未配置有效单价时 `cost_usd=null`，显示「成本未知」 |
| 请求级运行统计 | `final.timings.run_stats` | 九档状态区分「没调用 / 预算拒绝 / 依赖缺失 / 取消 / 路由跳过」，不含原文数据 |

## 黄金集

[evals/golden.json](evals/golden.json) 有 5 条真跑和 5 条离线。离线条目只存 pytest node id，期望值仍在现有测试里。live 案例不保存过期的固定旅行日期；运行器将 `date_expression` 解析为临时上下文，并将 `as_of`、时区、算法版本、旅行日期、父案例和 fixture SHA 写入结果。

```bash
python -m evals.run_golden --as-of 2026-11-14
python -m evals.judge
```

未提供必要密钥、服务授权或费用上限时，真跑案例会记录为 `blocked`，不计入 `passed`，命令返回非零。报告分别统计 `executed`、`passed`、`failed`、`blocked`、`timeout` 和 `no_terminal`；跨城首场失败后，后续案例标为 `blocked_by_previous_failure`。场景定义在 `evals/integration_matrix.json`，B5 的 30 条人工标注样本与阈值在 `evals/poi_gate_samples.json`。

浏览器真机验收不调用真实外部服务，使用 `evals/fixtures/browser-map.html` 和 `evals/MANUAL_BROWSER_ACCEPTANCE.md`。该证据只覆盖记录中的浏览器版本，不替代跨浏览器、移动端、HTTPS 或反向代理验证。

需要可用的 `OPENAI_API_KEY`（DeepSeek 官方 `https://api.deepseek.com`，模型 `deepseek-flash`）和 `AMAP_WEBSERVICE_KEY`。跑之前在 `.env` 设置 `TOKEN_USD_PER_M_IN` / `TOKEN_USD_PER_M_OUT`，否则当前实现会将成本标为未知。以下价格口径和数字均来自历史跑批，不能当作当前价格或性能：当时使用 off-peak、cache-miss $0.15 / $0.60（每百万 token），未拆 prompt cache。`metrics.json` 里的模型名是那次请求记下的旧网关模型。MCP 失败场景用 `http://127.0.0.1:1/mcp`。修订接在自驾那次的 `plan_id` 后面。

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

## 复核（2026-10-12）

上面两条结论在当前代码上已不成立，跑批早于 `cda0fb1`（2026-10-09「更新规划流水线、评测与前端」）引入的槽位澄清机制。

1. **HITL 第一轮会停在确认卡**。「缺出发地 / 目的地 / 日期，或多日行程缺住宿地就要求确认」是 `clarify_missing()`（`slots.py`）在 `cda0fb1` 里才加的。现在端到端实跑 `周末去郑州玩` 拿到 `clarify`（`missing=[origin, date]`）且无 `final`。
2. **耗时、成本与 judge 评分都基于旧流水线**。10-09 起住宿地成了多日行程的必答项，两日用例首轮不再直接出方案，旧数字不能当基线对照。

`evals/golden.json` 的 `xinxiang_drive_luoyang` 已改成两轮（先 `clarify`，再带 `slots` 跑满），`README.md` 验收场景 2 同步。要新数字得重跑：

```bash
python -m evals.run_golden
```
