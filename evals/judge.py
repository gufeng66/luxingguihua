"""用当前网关模型给黄金集成文打分。与被评模型同源，有自偏好。不进 CI。

用法：python -m evals.judge
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

METRICS = Path(__file__).with_name("metrics.json")
OUT = Path(__file__).with_name("judge_results.json")


def parse_judge(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("judge 输出里没有 JSON")
    data = json.loads(raw[start : end + 1])
    score = int(data["score"])
    if score < 1 or score > 5:
        raise ValueError(f"score 超出 1-5: {score}")
    reasons = data.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    return {"score": score, "reasons": [str(item) for item in reasons]}


def _prompt(answer: str, snapshot: dict[str, Any]) -> str:
    return f"""你在给一份旅行规划打分。对照工具原文，不要另找外部知识。
维度：六章节是否齐全；车次是否与 ticket_context 一致（原文没有的车次号算编造）；景点是否与 map_context 一致；是否遵守 slots；有无编造。
只输出 JSON：{{"score": 1到5的整数, "reasons": ["扣分理由"]}}

slots:
{json.dumps(snapshot.get("slots"), ensure_ascii=False)}

ticket_context:
{snapshot.get("ticket_context") or ""}

map_context:
{snapshot.get("map_context") or ""}

成文:
{answer}
"""


async def _score_all(plan_ids: list[str]) -> list[dict[str, Any]]:
    from langchain_core.messages import HumanMessage

    from planner.async_utils import message_text
    from planner.llm import build_llm
    from planner.summary import load_snapshot

    llm = build_llm()
    results: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        snapshot = load_snapshot(plan_id)
        if snapshot is None:
            results.append({"plan_id": plan_id, "error": "缺少 snapshot"})
            continue
        message = await llm.ainvoke([HumanMessage(content=_prompt(str(snapshot.get("answer") or ""), snapshot))])
        parsed = parse_judge(message_text(getattr(message, "content", message)))
        parsed["plan_id"] = plan_id
        results.append(parsed)
    return results


def _plan_ids(metrics: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for scenario in (metrics.get("scenarios") or {}).values():
        for bucket in ("clarify",):
            for row in scenario.get(bucket) or []:
                if row.get("plan_id"):
                    found.append(str(row["plan_id"]))
        for name in ("cold", "hot"):
            for row in (scenario.get(name) or {}).get("runs") or []:
                if row.get("plan_id"):
                    found.append(str(row["plan_id"]))
    return found


def main() -> int:
    if not METRICS.is_file():
        print("没有 evals/metrics.json，先跑 python -m evals.run_golden")
        return 2
    metrics = json.loads(METRICS.read_text(encoding="utf-8"))
    plan_ids = _plan_ids(metrics)
    if not plan_ids:
        print("metrics.json 里没有 plan_id")
        return 2
    results = asyncio.run(_score_all(plan_ids))
    scores = [item["score"] for item in results if "score" in item]
    payload = {
        "model": metrics.get("model"),
        "same_model_bias": True,
        "git_sha": metrics.get("git_sha"),
        "n": len(results),
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "results": results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
