# 评测说明

## 跑法

默认（纯函数，不调 LLM，可进 CI）：

```bash
pytest evals/test_unit.py -q
```

LLM 集成（需 `.env` 中 DeepSeek Key）：

```bash
pytest -m integration evals/test_integration.py -q
```

## 分层

| 层 | 文件 | 依赖 |
|----|------|------|
| unit | `test_unit.py` + `checkers.py` | 无网络 |
| integration | `test_integration.py` | API Key / 可 mock |

## 覆盖点

- critical / soft 分级与 `should_clarify`
- 相对日期纯函数（注入 `today=`）
- `TravelSlots` 校验
- 六章节、`[GDCKT]` 车次禁编造
- 票务三态与并行调度（mock）
- 抽取失败降级（integration + mock）
