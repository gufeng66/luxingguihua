"""离线回放。冻结工具响应和假模型，不调用付费接口。"""

from __future__ import annotations

import asyncio
from typing import Any

from planner.ticket_facts import parse_ticket_payload


FROZEN_TICKET = {
    "schema": "ticket-query-v1",
    "trains": [
        {
            "train_no": "G7586",
            "date": "2026-09-26",
            "origin": "杭州东",
            "destination": "苏州",
            "seat": "二等座",
            "price": 48,
            "availability": "有票",
        }
    ],
}


class _FrozenTickets:
    name = "get-tickets"
    description = "离线回放的冻结车票响应"
    args_schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "fromStation": {"type": "string"},
            "toStation": {"type": "string"},
        },
        "required": ["date", "fromStation", "toStation"],
    }

    async def ainvoke(self, _args: dict) -> dict:
        return FROZEN_TICKET


class _ReplayLLM:
    _tools: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> "_ReplayLLM":
        bound = _ReplayLLM()
        bound._tools = list(tools)
        return bound

    async def ainvoke(self, messages: list[Any], **_kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage

        names = {getattr(tool, "name", "") for tool in self._tools}
        seen_tool = any(getattr(message, "type", "") == "tool" for message in messages)
        if "get-tickets" in names and not seen_tool:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get-tickets",
                        "id": "replay-1",
                        "args": {"date": "2026-09-26", "fromStation": "杭州", "toStation": "苏州"},
                    }
                ],
            )
        return AIMessage(content="离线回放。解释只引用结构化章节，不另报车次。")


def frozen_facts() -> list[dict]:
    facts, diag = parse_ticket_payload(
        FROZEN_TICKET,
        source_id="raw-1",
        queried_at="2026-09-01T00:00:00+00:00",
        expect_date="2026-09-26",
        origin="杭州",
        destination="苏州",
    )
    assert diag is None and facts
    return facts


def _assemble(kind: str):
    async def fake(**kwargs: Any) -> dict[str, Any]:
        from planner.route_plan import schedule

        poi = {"poi_id": "p1", "name": "拙政园", "lng": 120.63, "lat": 31.32, "visit_minutes": 90}
        lodging = {"poi_id": "stay", "name": "住宿", "lng": 120.62, "lat": 31.31, "visit_minutes": 0}
        seconds = None if kind == "hours_unknown" else 600
        constraints = {
            "date": kwargs.get("date"),
            "days": kwargs.get("days") or 1,
            "lodging_poi": lodging,
            "transport_mode": kwargs.get("transport_mode") or "public_transit",
        }
        plan = schedule([poi], constraints, lambda *_args: seconds)
        from planner.route_plan import days_to_tasks

        facts = [] if kind == "hours_unknown" else [
            {
                "poi_id": "p1",
                "status": "hard",
                "kind": "opening_hours",
                "applies_on": [str(kwargs.get("date") or "")],
                "source_url": "https://www.dpm.org.cn/visit",
                "queried_at": "2026-09-01T00:00:00+00:00",
            }
        ]
        return {
            "plan": plan,
            "tasks": days_to_tasks(plan or {}, str(kwargs.get("city") or "")),
            "unlocated": [],
            "facts_status": "unavailable" if kind == "hours_unknown" else "ok",
            "facts": facts,
            "search_attempts": 0,
            "search_error": None,
            "search_usage": {},
            "route_error": None,
            "candidates": [poi],
            "constraints": constraints,
            "context": "离线回放",
        }

    return fake


async def replay(kind: str, *, output_dir: str | None = None) -> dict[str, Any]:
    """顺序离线回放。默认清理产物；传 output_dir 则由调用方保管地图和快照。"""
    import importlib
    import os
    import tempfile
    from contextlib import ExitStack
    from pathlib import Path
    from unittest.mock import patch

    import planner.pipeline as pipeline
    import planner_service as ps
    from slots import TravelSlots

    slots = {
        "origin": "杭州",
        "destination": "苏州",
        "date": "2026-09-26",
        "days": 1,
        "budget": "800",
        "preferences": "园林",
        "pace": "舒适型节奏",
        "transport_mode": "public_transit",
    }
    if kind == "hours_unknown":
        slots["origin"] = "杭州"
        slots["destination"] = "杭州"
        slots["transport_mode"] = "driving"
    confirmed = TravelSlots.model_validate(slots)
    if kind == "ticket_down":
        async def tickets(*_args, **_kwargs):
            return None
    else:
        async def tickets(*_args, **_kwargs):
            return {"name": "ticket_agent", "tools": [_FrozenTickets()], "diag": {"ok": True, "cache_hit": False}}

    async def summary(**_kwargs):
        yield {
            "type": "_summary_done",
            "content": "离线回放\n## 需求摘要\n回放\n## 景点建议\n拙政园\n## 车票建议\n模型写成 G1234\n## 预算\n未知\n## 行程表\n未知\n## 注意事项\n离线回放",
        }

    # ponytail: 局部补丁修改进程全局；回放保持顺序运行，需要并发时改为子进程。
    with ExitStack() as stack:
        root = Path(output_dir) if output_dir is not None else Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="replay-"))
        )
        (root / "maps").mkdir(parents=True, exist_ok=True)
        (root / "snapshots").mkdir(exist_ok=True)
        stack.enter_context(patch.dict(os.environ, {
            "PLANS_DB": str(root / "plans.db"),
            "TRAVEL_MAP_FALLBACK": "1",
            "LANGSMITH_API_KEY": "",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        }))
        for modname in ("planner.paths", "planner.summary", "planner.pipeline", "planner.map_fallback"):
            mod = importlib.import_module(modname)
            for name, path in (
                ("RESULTS_DIR", root),
                ("MAPS_DIR", root / "maps"),
                ("SNAPSHOTS_DIR", root / "snapshots"),
            ):
                if hasattr(mod, name):
                    stack.enter_context(patch.object(mod, name, path))
        stack.enter_context(patch.object(ps, "build_llm", lambda: _ReplayLLM()))
        stack.enter_context(patch.object(ps, "get_ticket_agent", tickets))
        stack.enter_context(patch.object(ps, "stream_summary", summary))
        stack.enter_context(patch.object(pipeline, "assemble_itinerary", _assemble(kind)))
        events = [event async for event in ps.stream_plan("离线回放", slots=confirmed)]
        final = next(event for event in events if event.get("type") == "final")
        final["replay"] = "离线回放"
        final["scenario"] = kind
        return final


def main() -> None:
    import sys

    kind = sys.argv[1] if len(sys.argv) > 1 else "happy"
    final = asyncio.run(replay(kind))
    print(final.get("replay"), kind, final.get("quality", {}).get("status"))
    content = final.get("content") or ""
    print("G1234" in content, "G7586" in content)


if __name__ == "__main__":
    main()
