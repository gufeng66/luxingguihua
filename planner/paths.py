"""
路径常量、Windows 控制台编码、.env 与 LangSmith 开关。

【小白怎么理解？】
    智能体「以为」文件在 /workspace/results、/workspace/skills/…，
    那是虚拟路径（见 backends.py）。磁盘上真实目录由本文件算出。
    任何模块 import planner.paths 都会执行这里的副作用：修编码、读密钥、建空目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Windows 终端默认 GBK，打印中文/emoji 会乱码或抛错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.platform == "win32":
    import locale as _locale

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # open() 未指定 encoding 时跟 locale 走；强制报 utf-8 减少中文路径踩坑
    _locale.getpreferredencoding = lambda do_setlocale=True: "utf-8"  # type: ignore[assignment,misc]
    if hasattr(_locale, "getencoding"):
        _locale.getencoding = lambda: "utf-8"  # type: ignore[method-assign]

load_dotenv(find_dotenv())

BASE_DIR = Path(__file__).resolve().parent.parent  # 仓库根目录
WORKSPACE_DIR = BASE_DIR / "workspace"
CONFIG_DIR = WORKSPACE_DIR / "config"  # 虚拟 /workspace/config
RESULTS_DIR = WORKSPACE_DIR / "results"  # 方案 md、地图 html
MAPS_DIR = RESULTS_DIR / "maps"
SKILL_DIR = BASE_DIR / "amap-lbs-skill"  # 高德 Skill 源码（只读挂载）
TICKET_CACHE_TTL_SECONDS = 300  # MCP 连接缓存 5 分钟
SNAPSHOTS_DIR = RESULTS_DIR / "snapshots"  # 修订用的上一版上下文
WEEKDAYS = "一二三四五六日"  # datetime.weekday() 0=周一 → 索引进本串


def dispatch_timeout_seconds() -> float:
    """主智能体调度 map/ticket 的总超时（秒）。环境变量坏值则退回 180。"""
    raw = (os.getenv("DISPATCH_TIMEOUT_SECONDS") or "180").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 180.0

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
MAPS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
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
