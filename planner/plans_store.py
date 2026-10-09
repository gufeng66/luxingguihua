"""一次规划落一行 SQLite，并打一条延迟日志。写库失败不影响规划。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("travel_planner")

_DROP = ("prompt_tokens", "completion_tokens", "cost_usd")
_plans = 0
_failures = 0
_latency_ms = 0


def db_path() -> Path:
    raw = (os.getenv("PLANS_DB") or "").strip()
    if raw:
        return Path(raw)
    from planner.paths import RESULTS_DIR

    return RESULTS_DIR / "plans.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
            plan_id TEXT PRIMARY KEY,
            query TEXT,
            slots TEXT,
            status TEXT,
            timings TEXT,
            artifact TEXT,
            created_at TEXT
        )
        """
    )
    return conn


def latency_timings(timings: dict[str, Any] | None) -> dict[str, Any]:
    if not timings:
        return {}
    return {key: value for key, value in timings.items() if key not in _DROP}


def _note(ok: bool, total_ms: int | None) -> dict[str, int]:
    global _plans, _failures, _latency_ms
    if ok:
        _plans += 1
        if total_ms:
            _latency_ms += int(total_ms)
    else:
        _failures += 1
    avg = int(_latency_ms / _plans) if _plans else 0
    return {"plans": _plans, "failures": _failures, "avg_ms": avg}


def record_plan_sync(
    *,
    plan_id: str,
    query: str,
    slots: dict[str, Any] | None,
    status: str,
    timings: dict[str, Any] | None,
    artifact: str | None,
    ok: bool,
) -> dict[str, Any]:
    clean = latency_timings(timings)
    total = clean.get("total_ms")
    stats = _note(ok, int(total) if isinstance(total, (int, float)) else None)
    try:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO plans
                (plan_id, query, slots, status, timings, artifact, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    query,
                    json.dumps(slots, ensure_ascii=False) if slots is not None else None,
                    status,
                    json.dumps(clean, ensure_ascii=False),
                    artifact,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("plans_db insert failed plan_id=%s", plan_id)
    payload = {"plan_id": plan_id, "status": status, "timings": clean, **stats}
    logger.info("%s", json.dumps(payload, ensure_ascii=False))
    return payload


async def record_plan(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(record_plan_sync, **kwargs)


def list_plans(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    if not db_path().is_file():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT plan_id, query, slots, status, timings, artifact, created_at
            FROM plans ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for plan_id, query, slots, status, timings, artifact, created_at in rows:
        out.append(
            {
                "plan_id": plan_id,
                "query": query,
                "slots": json.loads(slots) if slots else None,
                "status": status,
                "timings": json.loads(timings) if timings else {},
                "artifact": artifact,
                "created_at": created_at,
            }
        )
    return out
