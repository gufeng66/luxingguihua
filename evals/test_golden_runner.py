"""黄金集验收本身的离线检查，不创建真实模型或外部连接。"""

from __future__ import annotations

import io
import json
import queue
from datetime import date

import pytest

from evals import run_golden as golden


def _final(**kwargs):
    return {"type": "final", "plan_id": "plan", "timings": {"total_ms": 10},
            "quality": {"status": "partial", "checks": {"ticket": "unverified"}}, **kwargs}


def test_summary_keeps_failures_and_counts_all_rounds():
    rows = [golden._row("c", [_final()], "full"),
            golden._row("c", [{"type": "clarify"}], "clarify"),
            golden._row("c", [{"type": "error", "message": "broken"}], "full"),
            golden._failure({"case_id": "c"}, "full", "deadline", "timeout"),
            golden._row("c", [], "full")]
    summary = golden.summarize({"c": rows})["c"]
    assert summary["runs"] == rows
    assert summary["counts"] == {"attempted_rounds": 5, "executed": 5,
                                 "passed": 2, "failed": 1, "blocked": 0,
                                 "timeout": 1, "no_terminal": 1}
    assert summary["cold"]["n"] == 1


def test_expectations_apply_to_the_correct_round_and_map_artifact():
    case = {"case_id": "c", "expect": {"first": "clarify", "need_ticket": False,
                                         "need_map": True, "ticket_reason": "skipped"}}
    assert golden._row("c", [{"type": "clarify"}], "clarify", case)["expectation_pass"]
    assert not golden._row("c", [_final()], "clarify", case)["expectation_pass"]
    events = [{"type": "step", "id": "ticket", "status": "skipped", "reason": "no_rail_intent"},
              {"type": "step", "id": "map", "status": "active"}, _final(map_url="/map.html")]
    row = golden._row("c", events, "full", case)
    assert row["expectation_pass"] and row["quality_status"] == "partial"
    events[-1].pop("map_url")
    assert "expected map artifact" in golden._row("c", events, "full", case)["expectation_issues"]


def test_ticket_failure_and_second_round_route_are_checked():
    case = {"case_id": "c", "expect": {"ticket_reason": "unavailable"}}
    events = [{"type": "step", "id": "ticket", "status": "skipped", "reason": "mcp_unavailable"}, _final()]
    assert golden._row("c", events, "full", case)["expectation_pass"]
    events[0]["reason"] = "no_rail_intent"
    assert not golden._row("c", events, "full", case)["expectation_pass"]
    case["expect"] = {"first": "clarify", "second_need_ticket": True}
    assert golden._row("c", [{"type": "clarify"}], "clarify", case)["expectation_pass"]
    assert not golden._row("c", events, "full", case)["expectation_pass"]
    case["expect"] = {"ticket_reason": "reused_or_skipped", "need_ticket": False}
    events[0]["reason"] = "route_no_rail"
    assert golden._row("c", events, "full", case)["expectation_pass"]


@pytest.mark.asyncio
async def test_worker_emits_both_rounds_and_reports_failure(monkeypatch):
    emitted = []
    calls = []

    async def run(_query, slots, _parent):
        calls.append(slots)
        return [{"type": "clarify"}] if slots is None else []

    monkeypatch.setenv("GOLDEN_N", "1")
    monkeypatch.setattr(golden, "_run", run)
    monkeypatch.setattr(golden, "_emit", emitted.append)
    assert not await golden._worker_async({"case_id": "c", "query": "q",
                                          "rounds": "clarify_then_slots", "slots": {"days": 1}})
    assert calls == [None, {"days": 1}]
    assert [row["terminal"] for row in emitted] == ["clarified", "no_terminal"]


@pytest.mark.parametrize(("output", "code", "terminal"), [("", 0, "no_terminal"), ("", 9, "error")])
def test_child_without_rows_is_a_failed_sample(monkeypatch, output, code, terminal):
    class Process:
        stdout = io.StringIO(output)

        def wait(self, timeout):
            return code

    monkeypatch.setattr(golden.subprocess, "Popen", lambda *_a, **_k: Process())
    rows = golden._spawn({"case_id": "c"}, [])
    assert len(rows) == 1 and rows[0]["terminal"] == terminal
    assert not rows[0]["expectation_pass"]


def test_child_crash_after_valid_row_is_not_hidden(monkeypatch):
    row = golden._row("c", [_final()], "full")

    class Process:
        stdout = io.StringIO("ROW " + json.dumps(row) + "\n")

        def wait(self, timeout):
            return 9

    monkeypatch.setattr(golden.subprocess, "Popen", lambda *_a, **_k: Process())
    rows = golden._spawn({"case_id": "c"}, [])
    assert len(rows) == 2 and rows[-1]["terminal"] == "error"


def test_child_timeout_preserves_each_missing_round(monkeypatch):
    killed = []

    class Process:
        stdout = io.StringIO("")

        def kill(self):
            killed.append(True)

        def wait(self, timeout):
            return -9

    class IdleQueue:
        def get(self, timeout):
            raise queue.Empty

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(golden.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(golden.queue, "Queue", IdleQueue)
    monkeypatch.setattr(golden.threading, "Thread", Thread)
    rows = golden._spawn({"case_id": "c", "rounds": "clarify_then_slots"}, [])
    assert killed == [True]
    assert [row["round"] for row in rows] == ["clarify", "full"]
    assert all(row["terminal"] == "timeout" and not row["expectation_pass"] for row in rows)


def test_main_returns_nonzero_for_failed_batch(monkeypatch, tmp_path):
    (tmp_path / "evals").mkdir()
    config = tmp_path / "golden.json"
    config.write_text('{"cases": []}', encoding="utf-8")
    monkeypatch.setattr(golden, "ROOT", tmp_path)
    monkeypatch.setattr(golden, "GOLDEN", config)
    monkeypatch.setattr(golden, "_keys_missing", lambda: None)
    monkeypatch.setattr(golden.sys, "argv", ["run_golden"])
    monkeypatch.setattr(golden, "_write_metrics", lambda _: None)
    monkeypatch.setattr(golden, "_run_cases", lambda _: {"c": [golden._row("c", [], "full")]})
    assert golden.main() == 1


def test_prepare_cases_is_reproducible_and_preserves_source_fixture():
    source = b'{"cases":[{"case_id":"c","mode":"live","date_expression":"next_saturday",' \
             b'"query_template":"{trip_date}","slots":{"days":2}}]}'
    doc = json.loads(source)
    first = golden.prepare_cases(doc, date(2026, 11, 14), source)
    second = golden.prepare_cases(doc, date(2026, 11, 14), source)
    later = golden.prepare_cases(doc, date(2026, 11, 22), source)
    assert first == second
    assert first[0]["query"] == "2026-11-21"
    assert first[0]["slots"]["date"] == "2026-11-21"
    assert first[0]["_fixture_context"]["return_date"] == "2026-11-22"
    assert later[0]["_fixture_context"]["trip_date"] == "2026-11-28"
    assert doc["cases"][0]["slots"] == {"days": 2}


def test_blocked_rows_are_not_counted_as_passed():
    case = {"case_id": "c"}
    row = golden._blocked(case, "full", "blocked_missing_prerequisite: key")
    summary = golden.summarize({"c": [row]})["c"]
    assert row["result_status"] == "blocked" and row["executed"] is False
    assert summary["counts"] == {"attempted_rounds": 1, "executed": 0,
                                 "passed": 0, "failed": 0, "blocked": 1,
                                 "timeout": 0, "no_terminal": 0}


def test_live_golden_requires_explicit_approval_and_cost_limit(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "key")
    monkeypatch.delenv("GOLDEN_LIVE_APPROVED", raising=False)
    monkeypatch.delenv("GOLDEN_COST_LIMIT_USD", raising=False)
    assert golden._keys_missing() == "未设置 GOLDEN_LIVE_APPROVED=1"
    monkeypatch.setenv("GOLDEN_LIVE_APPROVED", "1")
    assert golden._keys_missing() == "缺少正数 GOLDEN_COST_LIMIT_USD"
    monkeypatch.setenv("GOLDEN_COST_LIMIT_USD", "1.5")
    assert golden._keys_missing() is None


def test_serial_compare_receives_resolved_cross_city_case(monkeypatch, tmp_path):
    config = tmp_path / "golden.json"
    config.write_text(
        '{"cases":[{"case_id":"cross_city_hsr","mode":"live","date_expression":"next_saturday",'
        '"query_template":"{trip_date}"}]}',
        encoding="utf-8",
    )
    seen = {}
    monkeypatch.setattr(golden, "GOLDEN", config)
    monkeypatch.setattr(golden, "_keys_missing", lambda: None)
    monkeypatch.setattr(golden, "_serial_compare", lambda case: seen.update(case) or {"modes": {}})
    monkeypatch.setattr(golden, "SERIAL", tmp_path / "serial.json")
    monkeypatch.setattr(golden.sys, "argv", ["run_golden", "--as-of", "2026-11-14", "--serial"])
    assert golden.main() == 0
    assert seen["query"] == "2026-11-21"

