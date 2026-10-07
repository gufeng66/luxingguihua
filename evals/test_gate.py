"""成文闸门、shell 环境、限流、失败注入。不调真实模型、不连 MCP。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.checkers import find_train_numbers, has_required_sections  # noqa: E402
from planner.summary import degraded_plan, summary_gate_issues  # noqa: E402
from slots import TravelSlots  # noqa: E402

_SECTIONS = """## 需求摘要
新乡到郑州一日游
## 景点建议
二七塔
## 车票建议
{ticket}
## 预算
500
## 行程表
上午市区
## 注意事项
以检索结果为准
"""


def _ok_text() -> str:
    return _SECTIONS.format(ticket="本次未查票（无需铁路）")


def _fake_train_text() -> str:
    return _SECTIONS.format(ticket="建议乘坐 G1234，票价 50 元")


def test_gate_flags_missing_sections_and_trains() -> None:
    assert summary_gate_issues("只有一句", "skipped")
    issues = summary_gate_issues(_fake_train_text(), "unavailable")
    assert any("车次" in item for item in issues)
    assert summary_gate_issues(_fake_train_text(), "ok") == []
    assert summary_gate_issues(_ok_text(), "skipped") == []


def test_degraded_plan_passes_gate() -> None:
    for reason in ("skipped", "unavailable", "missed", "timeout", "reused", "ok"):
        text = degraded_plan(reason, ["缺少章节: 预算"])
        assert summary_gate_issues(text, reason) == []
        assert has_required_sections(text)
        assert find_train_numbers(text) == []


def test_shell_env_hides_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-secret")
    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "amap-test")
    monkeypatch.delenv("AMAP_KEY", raising=False)
    from planner.backends import backend, shell_env

    env = shell_env()
    assert env["AMAP_KEY"] == "amap-test"
    assert "OPENAI_API_KEY" not in env
    assert "LANGSMITH_API_KEY" not in env
    live = backend.default._env
    assert "OPENAI_API_KEY" not in live
    assert "LANGSMITH_API_KEY" not in live


def test_usage_counts_stream_total_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_USD_PER_M_IN", "1")
    monkeypatch.setenv("TOKEN_USD_PER_M_OUT", "2")
    from planner.usage import begin_usage, current_usage, note_llm, usage_call

    begin_usage()

    class Msg:
        def __init__(self, prompt: int, completion: int) -> None:
            self.usage_metadata = {"input_tokens": prompt, "output_tokens": completion}

    with usage_call():
        note_llm(Msg(10, 1))
        note_llm(Msg(10, 4))
    note_llm(Msg(3, 1))
    usage = current_usage().as_dict()
    assert usage["prompt_tokens"] == 13
    assert usage["completion_tokens"] == 5
    assert usage["cost_usd"] == round((13 + 10) / 1_000_000, 6)


def test_rate_limit_and_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import server
    from fastapi import HTTPException

    monkeypatch.setenv("PLAN_RATE_PER_MINUTE", "1")
    server._rate_hits.clear()
    assert server._allow_plan("127.0.0.1") is True
    assert server._allow_plan("127.0.0.1") is False

    monkeypatch.setenv("PLAN_API_KEY", "secret")
    with pytest.raises(HTTPException) as exc:
        server._reject_bad_api_key(None)
    assert exc.value.status_code == 401
    server._reject_bad_api_key("secret")
    monkeypatch.delenv("PLAN_API_KEY")
    server._reject_bad_api_key(None)


@pytest.mark.asyncio
async def test_health_uses_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    monkeypatch.setattr(server, "_probe_mcp", lambda _url: False)
    monkeypatch.setattr(server.shutil, "which", lambda _name: r"C:\node.exe")
    body = await server.health()
    assert body["status"] == "ok"
    assert body["node"] is True
    assert body["skill"] is True
    assert body["mcp_reachable"] is False


def _slots() -> TravelSlots:
    return TravelSlots(
        origin="新乡",
        destination="郑州",
        date="2026-10-10",
        days=1,
        budget="500",
        preferences="少折腾",
        pace="舒适型节奏",
    )


async def _run_plan(monkeypatch: pytest.MonkeyPatch, summary_text: str, *, mount_ticket: bool, dispatch_map: bool):
    import planner_service as ps

    calls: list[str | None] = []
    monkeypatch.setattr(ps, "extract_slots", AsyncMock(return_value=_slots()))
    monkeypatch.setattr(ps, "build_llm", lambda: MagicMock())
    if mount_ticket:
        monkeypatch.setattr(
            ps, "get_ticket_agent", AsyncMock(return_value={"name": "ticket_agent", "tools": [object()]})
        )
    else:
        monkeypatch.setattr(ps, "get_ticket_agent", AsyncMock(return_value=None))

    async def fake_astream(*_a, **_k):
        if not dispatch_map:
            yield {"model": {"messages": [_ai(content="未调度")]}}
            return
        yield {
            "model": {
                "messages": [
                    _ai(tool_calls=[{"name": "task", "id": "m1", "args": {"subagent_type": "map_agent"}}])
                ]
            }
        }
        yield {"tools": {"messages": [_tool("二七塔", "m1")]}}

    agent = MagicMock()
    agent.astream = fake_astream
    monkeypatch.setattr(ps, "create_deep_agent", lambda **_k: agent)

    async def fake_summary(**kwargs):
        calls.append(kwargs.get("gate_feedback"))
        yield {"type": "token", "content": summary_text}
        yield {"type": "_summary_done", "content": summary_text}

    monkeypatch.setattr(ps, "stream_summary", fake_summary)
    events = [ev async for ev in ps.stream_plan("这周六从新乡坐高铁去郑州一日游")]
    return events, calls


def _ai(*, content: str = "", tool_calls: list | None = None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.tool_call_id = None
    return msg


def _tool(content: str, tool_call_id: str):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    msg.tool_call_id = tool_call_id
    return msg


@pytest.mark.asyncio
async def test_fabricated_train_is_retried_then_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    events, calls = await _run_plan(monkeypatch, _fake_train_text(), mount_ticket=False, dispatch_map=True)
    final = next(e for e in events if e.get("type") == "final")
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1]
    assert "G1234" not in final["content"]
    assert has_required_sections(final["content"])
    assert final["timings"]["summary_gate"] == "degraded"
    assert "prompt_tokens" in final["timings"]
    assert any(e.get("code") == "summary_degraded" for e in events)
    assert any(e.get("reason") == "mcp_unavailable" for e in events if e.get("id") == "ticket")


@pytest.mark.asyncio
async def test_valid_summary_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    events, calls = await _run_plan(monkeypatch, _ok_text(), mount_ticket=False, dispatch_map=True)
    final = next(e for e in events if e.get("type") == "final")
    assert calls == [None]
    assert final["timings"]["summary_gate"] == "pass"
    assert "未查票" in final["content"]
    assert not any(e.get("code") == "summary_degraded" for e in events)


@pytest.mark.asyncio
async def test_skill_not_dispatched_warns_and_blocks_trains(monkeypatch: pytest.MonkeyPatch) -> None:
    events, _calls = await _run_plan(monkeypatch, _fake_train_text(), mount_ticket=False, dispatch_map=False)
    assert any(e.get("code") == "map_not_dispatched" for e in events)
    final = next(e for e in events if e.get("type") == "final")
    assert find_train_numbers(final["content"]) == []
    assert "map_url" not in final
