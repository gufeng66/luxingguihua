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
| 闸门真的丢掉编造车次 | `evals/test_gate.py` | mock 汇总写出 `G1234`，`final.content` 不含该车次 |
| MCP 连不上 | 同上 | `get_ticket_agent` 返回空，ticket 步骤 `mcp_unavailable` |
| 没调度 map | 同上 | `map_not_dispatched`，且没有 `map_url` |
| 路由与五态 | `evals/test_routing.py`、`evals/test_unit.py` | 离线 |
| token | `final.timings` 的 `prompt_tokens` / `completion_tokens` / `cost_usd` | `cost_usd` 在设置 `TOKEN_USD_PER_M_IN`、`TOKEN_USD_PER_M_OUT` 之前为 0 |

质量分、单次费用、P50/P95 需要一次真实跨城运行的快照。没有这次记录之前不填表。离线回放（VCR）和 LLM-as-judge 同样押后。
