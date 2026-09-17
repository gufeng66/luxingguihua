"""路径常量、编码兼容与 LangSmith 开关。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.platform == "win32":
    import locale as _locale

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _locale.getpreferredencoding = lambda do_setlocale=True: "utf-8"  # type: ignore[assignment,misc]
    if hasattr(_locale, "getencoding"):
        _locale.getencoding = lambda: "utf-8"  # type: ignore[method-assign]

load_dotenv(find_dotenv())

BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = BASE_DIR / "workspace"
CONFIG_DIR = WORKSPACE_DIR / "config"
RESULTS_DIR = WORKSPACE_DIR / "results"
MAPS_DIR = RESULTS_DIR / "maps"
SKILL_DIR = BASE_DIR / "amap-lbs-skill"
TICKET_CACHE_TTL_SECONDS = 300
WEEKDAYS = "一二三四五六日"

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
MAPS_DIR.mkdir(parents=True, exist_ok=True)
(CONFIG_DIR / "memory").mkdir(parents=True, exist_ok=True)


def configure_langsmith() -> None:
    """有 LANGSMITH_API_KEY 则打开追踪，否则明确关闭。"""
    key = (os.getenv("LANGSMITH_API_KEY") or "").strip()
    if not key:
        os.environ["LANGSMITH_TRACING"] = "false"
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ.setdefault("LANGSMITH_PROJECT", "travel-planner")


configure_langsmith()
