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

人读口径与 `test_routing.py` 顶部 `CASES` 同构：见 [`cases.md`](cases.md)。真跑黄金集（需 Key，不进 CI）：`python -m evals.run_golden`，再 `python -m evals.judge`。

## 分层

| 层 | 文件 | 依赖 |
|----|------|------|
| unit | `test_unit.py` / `test_stream_plan.py` / `test_routing.py` / `test_gate.py` / `checkers.py` | 无网络 |
| integration | `test_integration.py` | API Key / 可 mock |

## 覆盖点

- critical / soft 分级与 `should_clarify`
- 相对日期纯函数（注入 `today=`）
- 路由 `needs_rail_ticket`、同城归一、飞机/高铁意图
- 五态优先级、六章节、车票节禁编造
- 并行同超步、自驾不连 MCP、missed warning
- `watch_aiter` 超时 aclose
- `plan_id` UUID 校验；map_only 修订不抽槽不连 MCP
