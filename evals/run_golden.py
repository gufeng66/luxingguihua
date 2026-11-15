"""黄金集真跑。不进 CI。缺 Key 时退出，不写假数字。

用法：python -m evals.run_golden
"""

from __future__ import annotations

import asyncio
import argparse
import copy
import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN = Path(__file__).with_name("golden.json")
METRICS = Path(__file__).with_name("metrics.json")
MODEL = "deepseek-flash"
N = 3
TIMEZONE = "Asia/Shanghai"
FIXTURE_VERSION = "golden-v2"
_TIMING_KEYS = (
    "total_ms",
    "map_ms",
    "assemble_ms",
    "poi_verify_ms",
    "facts_ms",
    "route_ms",
    "schedule_ms",
    "map_model_calls",
    "poi_requests",
    "route_requests",
    "amap_rate_limits",
    "ticket_ms",
    "dispatch_ms",
    "map_ticket_active_delta_ms",
    "ticket_cache_hit",
    "ticket_mcp_ms",
    "summary_gate",
    "parallel_dispatch",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "tool_attempts",
    "outcome",
)


def live_order(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """查票场景在前，修订接在其父场景之后，MCP 失败最后。"""

    def rank(case: dict[str, Any]) -> tuple[int, str]:
        if case.get("mcp_url"):
            return (2, case["case_id"])
        if case.get("after"):
            return (1, case["case_id"])
        return (0, case["case_id"])

    return sorted((case for case in cases if case.get("mode") == "live"), key=rank)


def median(values: list[int]) -> int | None:
    if not values:
        return None
    xs = sorted(values)
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) // 2


def split_cache(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cold = [row for row in rows if not row.get("ticket_cache_hit")]
    hot = [row for row in rows if row.get("ticket_cache_hit")]
    return cold, hot


def _pack_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = [int(row["total_ms"]) for row in rows if isinstance(row.get("total_ms"), int)]
    return {
        "n": len(rows),
        "median_total_ms": median(totals),
        "max_total_ms": max(totals) if totals else None,
        "runs": rows,
    }


def _write_metrics(by_case: dict[str, list[dict[str, Any]]]) -> None:
    context = next(
        (row.get("fixture_context") for rows in by_case.values() for row in rows
         if isinstance(row.get("fixture_context"), dict)),
        {},
    )
    payload = {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "model": MODEL,
        "python": platform.python_version(),
        "node": _node_version(),
        "n": N,
        "process_per_attempt": True,
        "langsmith": False,
        "fixture_context": context,
        "scenarios": summarize(by_case),
    }
    METRICS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize(by_case: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    scenarios: dict[str, Any] = {}
    for case_id, runs in by_case.items():
        clarify = [row for row in runs if row.get("hit_clarify")]
        timed = [row for row in runs if isinstance(row.get("total_ms"), int)]
        cold, hot = split_cache(timed)
        scenarios[case_id] = {
            "runs": runs,
            "counts": {
                "attempted_rounds": len(runs),
                "executed": sum(bool(row.get("executed", True)) for row in runs),
                **{name: sum(row.get("result_status") == name for row in runs)
                   for name in ("passed", "failed", "blocked", "timeout", "no_terminal")},
            },
            "clarify": clarify,
            "cold": _pack_bucket(cold),
            "hot": _pack_bucket(hot),
        }
    return scenarios


def _check(row: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """澄清轮只验澄清；完整轮验产物和路由，不把 partial 当执行错误。"""
    expect = case.get("expect") or {}
    issues = []
    desired = "clarified" if row["round"] == "clarify" else "finished"
    if row.get("terminal") != desired:
        issues.append(f"terminal: expected {desired}, got {row.get('terminal')}")
    if row["round"] == "clarify":
        if expect.get("first", "clarify") != "clarify":
            issues.append("unsupported first expectation")
    else:
        for key in ("need_ticket", "need_map"):
            expected = expect.get("second_need_ticket") if key == "need_ticket" and "second_need_ticket" in expect else expect.get(key)
            if expected is not None and row.get(key) != expected:
                issues.append(f"{key}: expected {expected}, got {row.get(key)}")
        if expect.get("need_map") is True and not row.get("has_map"):
            issues.append("expected map artifact")
        if expect.get("need_ticket") is False and row.get("ticket_dispatched"):
            issues.append("ticket agent dispatched despite no-ticket expectation")
        reason = expect.get("ticket_reason")
        allowed = {"reused", "skipped"} if reason == "reused_or_skipped" else {reason}
        if reason is not None and row.get("ticket_reason") not in allowed:
            issues.append(f"ticket_reason: expected {reason}, got {row.get('ticket_reason')}")
    row["expectation_issues"] = issues
    row["expectation_pass"] = not issues
    if row.get("result_status") is None:
        terminal = row.get("terminal")
        row["result_status"] = (
            "passed" if not issues else
            "timeout" if terminal == "timeout" else
            "no_terminal" if terminal == "no_terminal" else
            "failed"
        )
    row.setdefault("executed", row["result_status"] != "blocked")
    context = case.get("_fixture_context")
    if isinstance(context, dict):
        row["fixture_context"] = context
    return row


def _failure(case: dict[str, Any], round_name: str, message: str, terminal: str = "error") -> dict[str, Any]:
    return _check({"case_id": case["case_id"], "round": round_name,
                   "terminal": terminal, "error": message, "hit_clarify": False}, case)


def _blocked(case: dict[str, Any], round_name: str, reason: str) -> dict[str, Any]:
    return _check({"case_id": case["case_id"], "round": round_name,
                   "terminal": "blocked", "error": reason, "hit_clarify": False,
                   "result_status": "blocked", "executed": False}, case)


def _rounds(case: dict[str, Any]) -> tuple[str, ...]:
    return ("clarify", "full") if case.get("rounds") == "clarify_then_slots" else ("full",)


def _parse_as_of(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _next_saturday(as_of: date) -> date:
    delta = (5 - as_of.weekday()) % 7
    return as_of + timedelta(days=delta or 7)


def _fixture_context(as_of: date, source_bytes: bytes, parent_case_id: str | None = None) -> dict[str, Any]:
    trip_date = _next_saturday(as_of)
    return {
        "fixture_version": FIXTURE_VERSION,
        "fixture_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "as_of": as_of.isoformat(),
        "timezone": TIMEZONE,
        "date_algorithm": "strict_next_saturday_v1",
        "trip_date": trip_date.isoformat(),
        "return_date": (trip_date + timedelta(days=1)).isoformat(),
        "parent_case_id": parent_case_id,
    }


def prepare_cases(doc: dict[str, Any], as_of: date, source_bytes: bytes) -> list[dict[str, Any]]:
    """为一次真跑生成临时案例，绝不写回 golden.json。"""
    resolved: list[dict[str, Any]] = []
    for source in doc.get("cases", []):
        case = copy.deepcopy(source)
        context = _fixture_context(as_of, source_bytes, case.get("after"))
        case["_fixture_context"] = context
        if case.get("mode") != "live":
            resolved.append(case)
            continue
        trip_date = context["trip_date"]
        if case.get("query_template"):
            case["query"] = str(case["query_template"]).format(
                trip_date=trip_date, return_date=context["return_date"],
            )
        slots = case.get("slots")
        if isinstance(slots, dict) and case.get("date_expression"):
            slots["date"] = trip_date
        resolved.append(case)
    return resolved


def _row(case_id: str, events: list[dict[str, Any]], round_name: str,
         case: dict[str, Any] | None = None) -> dict[str, Any]:
    final = next((event for event in events if event.get("type") == "final"), None)
    clarify = next((event for event in events if event.get("type") == "clarify"), None)
    error = next((event for event in events if event.get("type") == "error"), None)
    row: dict[str, Any] = {
        "case_id": case_id,
        "round": round_name,
        "hit_clarify": bool(clarify) and final is None,
        "missing": None if clarify is None else clarify.get("missing"),
        "error": None if error is None else error.get("message"),
        "plan_id": None if final is None else final.get("plan_id"),
    }
    timings = {} if final is None else (final.get("timings") or {})
    for key in _TIMING_KEYS:
        if key in timings:
            row[key] = timings[key]
    quality = {} if final is None else (final.get("quality") or {})
    if quality:
        row["quality_status"] = quality.get("status")
        row["coverage"] = quality.get("coverage")
        row["issues"] = [item.get("code") for item in quality.get("issues") or []]
    row["terminal"] = "error" if error else "finished" if final else "clarified" if clarify else "no_terminal"
    steps = [event for event in events if event.get("type") == "step"]
    ticket_steps = [event for event in steps if event.get("id") == "ticket"]
    map_steps = [event for event in steps if event.get("id") == "map"]
    ticket_skip = next((event.get("reason") for event in reversed(ticket_steps) if event.get("status") == "skipped"), None)
    no_ticket = {"route_no_rail", "no_rail_intent", "intra_city"}
    row["need_ticket"] = False if ticket_skip in no_ticket else True if ticket_steps else None
    # user_no_poi 也是「不需要地图」的一种：不检索景点就没有可信地图可出。
    # reuse_parent 是复用轮的地图 step 码：这一轮既没重新检索也没重新出图，
    # 不能因为没有 route_no_map/user_no_map 就把它记成「本轮需要地图」。
    row["need_map"] = (
        not any(
            event.get("reason") in {"route_no_map", "user_no_map", "user_no_poi", "reuse_parent"}
            for event in map_steps
        )
    ) if map_steps else None
    row["has_map"] = bool(final and final.get("map_url"))
    row["ticket_dispatched"] = any(event.get("type") == "subagent" and event.get("name") == "ticket_agent" for event in events)
    row["ticket_reason"] = (
        "skipped" if ticket_skip in no_ticket else
        # reuse_parent 是复用分支的 step 码：这一轮没连票务，不能因为
        # checks.ticket 是 verified 就把它记成本轮的 ok。
        "reused" if ticket_skip == "reuse_parent" else
        {"mcp_unavailable": "unavailable", "dispatch_timeout": "timeout", "not_dispatched": "missed", "empty": "missed"}.get(ticket_skip)
    )
    if row["ticket_reason"] is None and quality.get("checks", {}).get("ticket") == "verified":
        row["ticket_reason"] = "ok"
    return _check(row, case or {"case_id": case_id})


def _keys_missing() -> str | None:
    from dotenv import load_dotenv

    from planner.paths import BASE_DIR

    load_dotenv(BASE_DIR / ".env")
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    amap = (os.getenv("AMAP_WEBSERVICE_KEY") or os.getenv("AMAP_KEY") or "").strip()
    if not key or key.startswith("sk-your"):
        return "缺少可用的 OPENAI_API_KEY"
    if not amap or amap.startswith("your_"):
        return "缺少可用的 AMAP_WEBSERVICE_KEY"
    if (os.getenv("GOLDEN_LIVE_APPROVED") or "").strip() != "1":
        return "未设置 GOLDEN_LIVE_APPROVED=1"
    try:
        cost_limit = float((os.getenv("GOLDEN_COST_LIMIT_USD") or "").strip())
    except ValueError:
        cost_limit = 0
    if cost_limit <= 0:
        return "缺少正数 GOLDEN_COST_LIMIT_USD"
    return None


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def _node_version() -> str:
    node = shutil.which("node")
    if not node:
        return "missing"
    try:
        return subprocess.check_output([node, "-v"], text=True).strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


async def _reset_fail() -> None:
    return None


async def _drop_ticket_cache() -> None:
    """连接按请求创建，这里不再有进程内缓存可清。"""
    return None


_RUN_TIMEOUT_S = 2700  # 跨城实测约 1488s，1500 会杀在汇总前


async def _run(query: str, slots: dict[str, Any] | None, parent: str | None) -> list[dict[str, Any]]:
    from planner_service import stream_plan
    from slots import TravelSlots

    confirmed = TravelSlots.model_validate(slots) if slots else None
    events: list[dict[str, Any]] = []
    async for event in stream_plan(query, slots=confirmed, parent_plan_id=parent):
        events.append(event)
    return events


def _emit(row: dict[str, Any]) -> None:
    line = json.dumps(row, ensure_ascii=False)
    with (ROOT / "evals" / "rows.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print("ROW " + line, flush=True)
    print(f"done {row.get('case_id')} total_ms={row.get('total_ms')} err={row.get('error')}", flush=True)


async def _worker_async(case: dict[str, Any]) -> bool:
    repeats = int(os.getenv("GOLDEN_N") or N)
    parents = [item for item in (os.getenv("GOLDEN_PARENTS") or "").split(",") if item]
    case_id = case["case_id"]
    passed = True
    for index in range(repeats):
        await _reset_fail()
        print(f"run {case_id} {index + 1}/{repeats}", flush=True)
        parent = None
        if case.get("after"):
            parent = parents[index] if index < len(parents) else (parents[-1] if parents else None)
            if parent is None:
                _emit(_failure(case, "full", "缺少上一份方案"))
                passed = False
                continue
        for round_name in _rounds(case):
            try:
                events = await _run(case["query"], None if round_name == "clarify" else case.get("slots"), parent)
                row = _row(case_id, events, round_name, case)
            except Exception as exc:
                row = _failure(case, round_name, f"{type(exc).__name__}: {exc}")
            _emit(row)
            passed = passed and row["expectation_pass"]
    return passed


def _spawn(case: dict[str, Any], parents: list[str]) -> list[dict[str, Any]]:
    """每次尝试新进程；缺失轮次和进程异常也必须留在结果中。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # 追踪上传卡住时整次规划停在 HTTPS 上。测延迟时关掉。
    env.pop("LANGSMITH_API_KEY", None)
    env["LANGSMITH_TRACING"] = "false"
    if case.get("mcp_url"):
        env["MCP_12306_URL"] = str(case["mcp_url"])
    if parents:
        env["GOLDEN_PARENTS"] = ",".join(parents)
    env["GOLDEN_CASE_JSON"] = json.dumps(case, ensure_ascii=False)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "evals.run_golden", "--worker", case["case_id"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=ROOT,
        )
    except OSError as exc:
        return [_failure(case, "full", f"process start: {exc}")]
    lines: queue.Queue[str | None] = queue.Queue()

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=_read, daemon=True).start()
    rows: list[dict[str, Any]] = []
    timed_out = False
    while True:
        try:
            line = lines.get(timeout=_RUN_TIMEOUT_S)
        except queue.Empty:
            proc.kill()
            print(f"killed {case['case_id']} after {_RUN_TIMEOUT_S}s", flush=True)
            timed_out = True
            break
        if line is None:
            break
        text = line.strip()
        if text:
            print(text, flush=True)
        if text.startswith("ROW "):
            try:
                rows.append(_check(json.loads(text[4:]), case))
            except (ValueError, TypeError, KeyError):
                rows.append(_failure(case, "process", "invalid ROW output"))
    code = proc.wait(timeout=30)
    for round_name in _rounds(case):
        if not any(row["round"] == round_name for row in rows):
            terminal = "timeout" if timed_out else "error" if code else "no_terminal"
            rows.append(_failure(case, round_name, f"missing ROW; exit={code}; timeout={timed_out}", terminal))
    if (code or timed_out) and all(row["expectation_pass"] for row in rows):
        rows.append(_failure(case, "process", f"process exit={code}; timeout={timed_out}", "timeout" if timed_out else "error"))
    return rows


def _run_cases(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    parents: dict[str, list[str]] = {}
    os.environ["GOLDEN_N"] = "1"
    blocked_reason: str | None = None
    for case in live_order(cases):
        if blocked_reason:
            rows = [_blocked(case, round_name, blocked_reason) for round_name in _rounds(case)]
            by_case[case["case_id"]] = rows
            _write_metrics(by_case)
            continue
        found = parents.get(str(case.get("after"))) or []
        rows: list[dict[str, Any]] = []
        attempt_parents: list[str] = []
        for index in range(N):
            one = [found[index]] if index < len(found) and found[index] else []
            attempt_rows = _spawn(case, one if case.get("after") else [])
            rows.extend(attempt_rows)
            attempt_parents.append(next((str(row["plan_id"]) for row in attempt_rows
                                         if row.get("round") == "full" and row.get("plan_id")
                                         and row.get("expectation_pass")), ""))
        by_case[case["case_id"]] = rows
        _write_metrics(by_case)
        if not case.get("after"):
            parents[case["case_id"]] = attempt_parents
        if case.get("case_id") == "cross_city_hsr" and any(
            row.get("result_status") != "passed" for row in rows
        ):
            blocked_reason = "blocked_by_previous_failure: cross_city_hsr"
    return by_case


SERIAL = Path(__file__).with_name("serial.json")


def _overlap(row: dict[str, Any]) -> dict[str, Any]:
    map_ms = row.get("map_ms")
    ticket_ms = row.get("ticket_ms")
    serial_lower = None
    if isinstance(map_ms, int) and isinstance(ticket_ms, int):
        serial_lower = map_ms + ticket_ms
    return {
        "serial_lower_ms": serial_lower,
        "dispatch_ms": row.get("dispatch_ms"),
        "start_delta_ms": row.get("map_ticket_active_delta_ms"),
        "parallel_dispatch": row.get("parallel_dispatch"),
        "total_ms": row.get("total_ms"),
        "plan_id": row.get("plan_id"),
        "error": row.get("error"),
        "expectation_pass": row.get("expectation_pass", False),
        "expectation_issues": row.get("expectation_issues", []),
    }


def _serial_compare(case: dict[str, Any]) -> dict[str, Any]:
    """每次新进程，车票缓存都是冷的，避免和串行/并行混在一起。"""
    modes: dict[str, list[dict[str, Any]]] = {}
    saved_serial = os.environ.get("FORCE_SERIAL")
    saved_n = os.environ.get("GOLDEN_N")
    try:
        os.environ["GOLDEN_N"] = "1"
        for name, flag in (("parallel", None), ("serial", "1")):
            if flag:
                os.environ["FORCE_SERIAL"] = flag
            else:
                os.environ.pop("FORCE_SERIAL", None)
            runs: list[dict[str, Any]] = []
            for index in range(5):
                print(f"serial-compare {name} {index + 1}/5", flush=True)
                rows = _spawn(case, [])
                runs.append(_overlap(rows[-1] if rows else {"error": "no row"}))
            modes[name] = runs
    finally:
        if saved_serial is None:
            os.environ.pop("FORCE_SERIAL", None)
        else:
            os.environ["FORCE_SERIAL"] = saved_serial
        if saved_n is None:
            os.environ.pop("GOLDEN_N", None)
        else:
            os.environ["GOLDEN_N"] = saved_n
    serial_parallel_flags = [bool(row.get("parallel_dispatch")) for row in modes["serial"]]
    return {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "model": MODEL,
        "python": platform.python_version(),
        "node": _node_version(),
        "n": 5,
        "fresh_process_each_run": True,
        "prompt_forced_serial_but_still_parallel": all(serial_parallel_flags) and bool(serial_parallel_flags),
        "modes": modes,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行旅行规划真实黄金集")
    parser.add_argument("--as-of", help="以 Asia/Shanghai 的 YYYY-MM-DD 解析相对日期")
    parser.add_argument("--worker")
    parser.add_argument("--serial", action="store_true")
    return parser.parse_args()


def _blocked_batch(cases: list[dict[str, Any]], reason: str) -> dict[str, list[dict[str, Any]]]:
    return {
        case["case_id"]: [_blocked(case, round_name, reason) for round_name in _rounds(case)]
        for case in live_order(cases)
    }


def main() -> int:
    args = _arguments()
    source_bytes = GOLDEN.read_bytes()
    doc = json.loads(source_bytes)
    cases = prepare_cases(doc, _parse_as_of(args.as_of or os.getenv("GOLDEN_AS_OF")), source_bytes)
    if args.worker:
        raw_case = os.getenv("GOLDEN_CASE_JSON")
        case = json.loads(raw_case) if raw_case else next(item for item in cases if item["case_id"] == args.worker)
        return 0 if asyncio.run(_worker_async(case)) else 1
    missing = _keys_missing()
    if missing:
        blocked = _blocked_batch(cases, f"blocked_missing_prerequisite: {missing}")
        _write_metrics(blocked)
        print(missing + "。黄金集已记录为 blocked，未执行真实调用。")
        return 2
    if args.serial:
        cross_city = next(case for case in cases if case.get("case_id") == "cross_city_hsr")
        payload = _serial_compare(cross_city)
        SERIAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {SERIAL}")
        return int(any(not row.get("expectation_pass") for rows in payload["modes"].values() for row in rows))
    (ROOT / "evals" / "rows.jsonl").write_text("", encoding="utf-8")
    by_case = _run_cases(cases)
    _write_metrics(by_case)
    print(f"wrote {METRICS}")
    return int(not by_case or any(not row.get("expectation_pass") for rows in by_case.values() for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
