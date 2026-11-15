# 评测说明

## 跑法

默认（纯函数 + mock，不调 LLM，可进 CI）：

```bash
pytest -q -m "not integration"
```

LLM 集成（需 `.env` 中 DeepSeek Key）：

```bash
pytest -m integration evals/test_integration.py -q
```

人读口径与 `test_routing.py` 顶部 `CASES` 同构：见 [`cases.md`](cases.md)。真跑黄金集（需 Key，不进 CI）：`python -m evals.run_golden --as-of 2026-11-14`，再 `python -m evals.judge`。`--as-of`（或 `GOLDEN_AS_OF`）按 `Asia/Shanghai` 生成严格晚于基准日的下一个周六；结果会写入 fixture SHA 与日期上下文，源 `golden.json` 不会被改写。

真实场景的前置配置未满足时结果为 `blocked`，不算通过也不等同于代码失败；完整状态和场景 ID 见 `integration_matrix.json`。POI 闸门真实质量使用 `poi_gate_samples.json` 的 30 条人工标注样本：关键安全类别零误报，总误报/漏报各不超过 10%。

## 分层

| 层 | 文件 | 依赖 |
|----|------|------|
| unit | `test_unit.py` / `test_stream_plan.py` / `test_routing.py` / `test_gate.py` / `checkers.py` | 无网络 |
| integration | `test_integration.py` | API Key / 可 mock |

## 覆盖点

- critical / soft 分级与 `should_clarify`
- 相对日期纯函数（注入 `today=`）
- 路由 `needs_rail_ticket`、同城归一、飞机/高铁意图
- 票务六态（含修订复用）、六章节、车票节禁编造
- 并行同超步、自驾不连 MCP、missed warning
- `watch_aiter` 超时 aclose
- `plan_id` UUID 校验；map_only 修订会抽取必去/不去/交通方式，不连 MCP
- 修订合并后重查必需槽位；本轮交通意图覆盖旧原话
- 成文首段和后续段等待受同一截止时间限制，超时关闭流并降级
- 黄金集保留失败记录、校验 `expect`、失败返回非零（`test_golden_runner.py`）
- 离线回放恢复函数、环境和目录，成功或异常均清理临时产物（`test_replay_isolation.py`）
