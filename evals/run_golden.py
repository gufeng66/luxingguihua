"""黄金集真跑。不进 CI。缺 Key 时退出，不写假数字。

用法：python -m evals.run_golden
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN = Path(__file__).with_name("golden.json")
METRICS = Path(__file__).with_name("metrics.json")
MODEL = "deepseek-flash"
N = 3
_TIMING_KEYS = (
    "total_ms",
    "map_ms",
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
    payload = {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "model": MODEL,
        "python": platform.python_version(),
        "node": _node_version(),
        "n": N,
        "process_per_attempt": True,
        "langsmith": False,
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
            "clarify": clarify,
            "cold": _pack_bucket(cold),
            "hot": _pack_bucket(hot),
        }
    return scenarios


def _row(case_id: str, events: list[dict[str, Any]], round_name: str) -> dict[str, Any]:
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
    return row


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
    import planner.ticket_cache as tc

    tc._fail_until = 0.0


async def _drop_ticket_cache() -> None:
    import planner.ticket_cache as tc
    from planner.ticket_agent import aclose_mcp_client

    await _reset_fail()
    async with tc._lock:
        old = tc._ticket_cache
        tc._ticket_cache = None
    if old:
        await aclose_mcp_client(old.get("client"))


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


async def _worker_async(case: dict[str, Any]) -> None:
    repeats = int(os.getenv("GOLDEN_N") or N)
    parents = [item for item in (os.getenv("GOLDEN_PARENTS") or "").split(",") if item]
    case_id = case["case_id"]
    for index in range(repeats):
        await _reset_fail()
        print(f"run {case_id} {index + 1}/{repeats}", flush=True)
        parent = None
        if case.get("after"):
            parent = parents[index] if index < len(parents) else (parents[-1] if parents else None)
            if parent is None:
                _emit({"case_id": case_id, "round": "full", "error": "缺少上一份方案", "hit_clarify": False})
                continue
        if case.get("rounds") == "clarify_then_slots":
            _emit(_row(case_id, await _run(case["query"], None, None), "clarify"))
            _emit(_row(case_id, await _run(case["query"], case.get("slots"), None), "full"))
        else:
            _emit(_row(case_id, await _run(case["query"], case.get("slots"), parent), "full"))


def _spawn(case: dict[str, Any], parents: list[str]) -> list[dict[str, Any]]:
    """一个场景一个进程。同一进程里的重复次数仍能命中车票缓存。卡住就杀掉进程。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # 追踪上传卡住时整次规划停在 HTTPS 上。测延迟时关掉。
    env.pop("LANGSMITH_API_KEY", None)
    env["LANGSMITH_TRACING"] = "false"
    if case.get("mcp_url"):
        env["MCP_12306_URL"] = str(case["mcp_url"])
    if parents:
        env["GOLDEN_PARENTS"] = ",".join(parents)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "evals.run_golden", "--worker", case["case_id"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=ROOT,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=_read, daemon=True).start()
    rows: list[dict[str, Any]] = []
    while True:
        try:
            line = lines.get(timeout=_RUN_TIMEOUT_S)
        except queue.Empty:
            proc.kill()
            print(f"killed {case['case_id']} after {_RUN_TIMEOUT_S}s", flush=True)
            rows.append(
                {
                    "case_id": case["case_id"],
                    "round": "full",
                    "error": f"killed after {_RUN_TIMEOUT_S}s",
                    "hit_clarify": False,
                }
            )
            break
        if line is None:
            break
        text = line.strip()
        if text:
            print(text, flush=True)
        if text.startswith("ROW "):
            rows.append(json.loads(text[4:]))
    proc.wait(timeout=30)
    return rows


def _run_cases(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    parents: dict[str, list[str]] = {}
    os.environ["GOLDEN_N"] = "1"
    for case in live_order(cases):
        found = parents.get(str(case.get("after"))) or []
        rows: list[dict[str, Any]] = []
        for index in range(N):
            one = [found[index]] if index < len(found) else ([found[-1]] if found else [])
            rows.extend(_spawn(case, one if case.get("after") else []))
        by_case[case["case_id"]] = rows
        _write_metrics(by_case)
        if not case.get("after"):
            parents[case["case_id"]] = [str(row["plan_id"]) for row in rows if row.get("plan_id")]
    return by_case


SERIAL = Path(__file__).with_name("serial.json")
_CROSS = "这周六从新乡坐高铁去郑州一日游，预算 500，少折腾"


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
    }


def _serial_compare() -> dict[str, Any]:
    """每次新进程，车票缓存都是冷的，避免和串行/并行混在一起。"""
    case = {"case_id": "cross_city_hsr", "mode": "live", "query": _CROSS}
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


def main() -> int:
    missing = _keys_missing()
    if missing:
        print(missing + "。黄金集未跑，不写 metrics.json。")
        return 2
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if "--worker" in sys.argv:
        case_id = sys.argv[sys.argv.index("--worker") + 1]
        case = next(item for item in doc["cases"] if item["case_id"] == case_id)
        asyncio.run(_worker_async(case))
        return 0
    if "--serial" in sys.argv:
        payload = _serial_compare()
        SERIAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {SERIAL}")
        return 0
    (ROOT / "evals" / "rows.jsonl").write_text("", encoding="utf-8")
    by_case = _run_cases(doc["cases"])
    _write_metrics(by_case)
    print(f"wrote {METRICS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
