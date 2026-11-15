"""
FastAPI 后端入口（给浏览器用的「服务员」）

【小白怎么理解这个文件？】
    浏览器打开网页后，前端通过 HTTP 跟本文件通信。
    本文件负责：
        1. 把 frontend/ 里的 HTML/JS/CSS 发给浏览器
        2. 接收「开始规划」请求，把 planner_service 的事件用 SSE 推回去
        3. 把 results/ 里的方案 md、地图 HTML 挂成静态文件供下载/打开

【SSE 是什么？】
    Server-Sent Events：服务器单向、持续往浏览器推送文本。
    适合「规划要跑很久，边跑边显示进度」的场景。
    每一帧形如：data: {"type":"status","message":"..."}\n\n
"""

# 现代类型注解
from __future__ import annotations

# 异步事件：客户端断开时用来通知规划流水线停下来
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

# 加载 .env 里的密钥（DeepSeek / 高德等）
from dotenv import find_dotenv, load_dotenv
# FastAPI 框架与异常、请求对象
from fastapi import FastAPI, HTTPException, Request
# 跨域中间件：允许指定来源的前端访问 API
from fastapi.middleware.cors import CORSMiddleware
# 返回流式响应
from fastapi.responses import FileResponse, StreamingResponse
# 把某个文件夹挂成 URL 前缀下的静态站点
from fastapi.staticfiles import StaticFiles
# 请求体校验模型
from pydantic import BaseModel, Field

# 规划流水线 + 可取消的异步迭代包装 + 结果目录
from planner.plans_store import list_plans
from planner.preflight import optional_deps
from planner.run_stats import RunStats, begin_run_stats
from planner.ticket_agent import mcp_12306_url
from planner_service import RESULTS_DIR, stream_plan, watch_aiter
# 可选：前端确认卡回传的槽位
from slots import TravelSlots

logger = logging.getLogger("travel_planner")
# ponytail: 单进程内存计数。多 worker 各自一份，挡不住打到别的进程上的请求。
_rate_hits: dict[str, list[float]] = {}
_sessions: dict[str, tuple[float, str]] = {}
_SESSION_COOKIE = "travel_planner_session"
_PLAN_FILE = re.compile(r"^旅游规划-.+-([0-9a-f-]{36})\.md$")


class _Lease:
    def __init__(self, owner: "Inflight") -> None:
        self._owner = owner
        self._open = True

    def release(self) -> None:
        if not self._open:
            return
        self._open = False
        self._owner.release()


class _BoundStream:
    """响应对象被关闭时释放名额，包括生成器还没开始被消费的情况。"""

    def __init__(self, agen, lease: _Lease, cancel_event: asyncio.Event) -> None:
        self._agen = agen
        self._lease = lease
        self._cancel = cancel_event
        self._released = False

    def __aiter__(self) -> "_BoundStream":
        return self

    async def __anext__(self):
        return await self._agen.__anext__()

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        self._cancel.set()
        self._lease.release()
        _inflight.cancels.discard(self._cancel)

    async def aclose(self) -> None:
        self._release()
        aclose = getattr(self._agen, "aclose", None)
        if aclose is not None:
            await aclose()


class Inflight:
    """同时规划名额。占用在返回响应之前完成，释放可重复调用。"""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()
        self.cancels: set[asyncio.Event] = set()
        self.tasks: set[asyncio.Task] = set()

    def try_acquire(self) -> _Lease | None:
        limit = int(os.getenv("PLAN_MAX_INFLIGHT") or 2)
        with self._lock:
            if self._n >= limit:
                return None
            self._n += 1
            return _Lease(self)

    def release(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def count(self) -> int:
        with self._lock:
            return self._n


_inflight = Inflight()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    for event in list(_inflight.cancels):
        event.set()
    pending = [task for task in list(_inflight.tasks) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=float(os.getenv("PLAN_CLEANUP_SECONDS") or 5))

# 启动时加载环境变量
load_dotenv(find_dotenv())

# 项目根目录 = 本文件所在目录
BASE_DIR = Path(__file__).resolve().parent
# Vue 前端静态资源目录
FRONTEND_DIR = BASE_DIR / "frontend"
# 确保结果目录存在（首次克隆仓库时可能只有 .gitkeep）
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# 创建 FastAPI 应用（标题会出现在 /docs 自动文档里）
app = FastAPI(title="旅游规划多智能体 API", version="1.0.0", lifespan=_lifespan)

# 从环境变量读允许的前端来源；默认本机 8000
_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if origin.strip()
]
# 挂上 CORS，避免浏览器拦截跨域 fetch（本项目前后端同端口时其实同域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PlanRequest(BaseModel):
    """浏览器 POST /api/plan 时提交的 JSON 结构。"""

    # 用户一句话需求（限制长度，防止超长刷爆模型）
    query: str = Field(..., min_length=1, max_length=300, description="自然语言旅游需求")
    slots: TravelSlots | None = Field(default=None, description="确认卡回传的已校验槽位")
    plan_id: str | None = Field(default=None, description="修订时传入上一份方案的 UUID")
    # plan=首次规划；revise=按原话改上一版；confirm=用户在补全表单里点了确认
    mode: Literal["plan", "revise", "confirm"] = Field(default="plan", description="本次请求的来源")
    lodging_scope: Literal["exact", "area"] | None = Field(
        default=None, description="住宿口径：exact=已定具体酒店；area=按该区域参考规划"
    )
    party_size: int | None = Field(default=None, ge=1, le=50, description="出行人数")
    budget_scope: Literal["per_person", "total"] | None = Field(
        default=None, description="预算是人均还是总额"
    )
    cleared_fields: list[str] = Field(default_factory=list, description="用户主动撤销的确认项")
    intent_source: Literal["initial", "revision", "background"] = "initial"
    intercity_mode: Literal["rail", "driving", "plane"] | None = None
    return_required: bool | None = None


class SessionRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=512)


@app.get("/api/health")
async def health() -> dict[str, object]:
    """进程存活。不访问外部网络。"""
    return {"status": "ok", "check": "live"}


@app.get("/api/ready")
async def ready() -> dict[str, object]:
    """必需配置缺失则未就绪。MCP 只做短超时 HTTP 探测，不可用时降级而不是整站不可用。"""
    missing = []
    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        missing.append("OPENAI_API_KEY")
    if not ((os.getenv("AMAP_WEBSERVICE_KEY") or os.getenv("AMAP_KEY") or "").strip()):
        missing.append("AMAP_WEBSERVICE_KEY")
    mcp_http = "unreachable"
    try:
        mcp_ok = await asyncio.wait_for(asyncio.to_thread(_probe_mcp, mcp_12306_url()), timeout=2)
        mcp_http = "reachable" if mcp_ok else "unreachable"
    except (asyncio.TimeoutError, OSError):
        mcp_http = "unreachable"
    return {
        "ready": not missing,
        "degraded": not missing and mcp_http != "reachable",
        "check": "ready",
        "config": "missing" if missing else "ok",
        "missing": missing,
        "mcp_http": mcp_http,
        "mcp_tools": "not_checked",
        "optional_deps": optional_deps(),
    }


def _probe_mcp(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return int(resp.status) < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _expected_key() -> str:
    return (os.getenv("PLAN_API_KEY") or "").strip()


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    return bool(origin and origin in _cors_origins)


def _authenticated(request: Request, *, cookie_write: bool = False) -> bool:
    """返回是否由显式请求头认证；头存在时绝不让旧 Cookie 掩盖错误。"""
    expected = (os.getenv("PLAN_API_KEY") or "").strip()
    header = request.headers.get("x-api-key")
    if header is not None:
        if not expected or not hmac.compare_digest(header, expected):
            raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Key")
        return True
    if not expected:
        return False
    token = request.cookies.get(_SESSION_COOKIE, "")
    row = _sessions.get(_key_hash(token)) if token else None
    if row and row[0] > time.time() and hmac.compare_digest(row[1], _key_hash(expected)):
        if cookie_write and not _origin_ok(request):
            raise HTTPException(status_code=403, detail="Cookie 认证的写请求需要受信任来源")
        return False
    raise HTTPException(status_code=401, detail="缺少或错误的访问凭证")


def _issue_session(response) -> None:
    expected = _expected_key()
    if not expected:
        return
    raw = secrets.token_urlsafe(32)
    ttl = max(60, int(os.getenv("PLAN_SESSION_TTL_SECONDS") or 28800))
    _sessions[_key_hash(raw)] = (time.time() + ttl, _key_hash(expected))
    response.set_cookie(
        _SESSION_COOKIE, raw, max_age=ttl, httponly=True, samesite="strict",
        secure=(os.getenv("PLAN_COOKIE_SECURE") or "0") == "1", path="/",
    )


def _reject_bad_api_key(header: str | None) -> None:
    """兼容既有纯函数测试；HTTP 路由统一使用 _authenticated。"""
    expected = _expected_key()
    if expected and header != expected:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Key")


@app.post("/api/session")
async def create_session(body: SessionRequest):
    expected = _expected_key()
    if not expected:
        raise HTTPException(status_code=404, detail="未启用服务访问口令")
    if not hmac.compare_digest(body.api_key, expected):
        raise HTTPException(status_code=401, detail="访问口令错误")
    from fastapi.responses import JSONResponse
    response = JSONResponse({"ok": True})
    _issue_session(response)
    return response


def _private_file(path: Path, media_type: str):
    if not path.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@app.get("/results/maps/{plan_id}.html")
async def result_map(plan_id: str, request: Request):
    _authenticated(request)
    from planner.summary import parse_plan_id
    valid = parse_plan_id(plan_id)
    if valid is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    return _private_file(RESULTS_DIR / "maps" / f"{valid}.html", "text/html; charset=utf-8")


@app.get("/results/{filename}")
async def result_plan(filename: str, request: Request):
    _authenticated(request)
    from planner.summary import parse_plan_id
    match = _PLAN_FILE.fullmatch(filename)
    if match is None or parse_plan_id(match.group(1)) is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    return _private_file(RESULTS_DIR / filename, "text/markdown; charset=utf-8")


def _allow_plan(client: str) -> bool:
    limit = int(os.getenv("PLAN_RATE_PER_MINUTE") or 8)
    now = time.monotonic()
    bucket = [t for t in _rate_hits.get(client, []) if now - t < 60]
    if len(bucket) >= limit:
        _rate_hits[client] = bucket
        return False
    bucket.append(now)
    _rate_hits[client] = bucket
    return True


@app.post("/api/plan")
async def plan(req: PlanRequest, request: Request) -> StreamingResponse:
    """规划主接口：以 SSE 流式返回多智能体过程事件。

    若浏览器关掉标签页，会通过 cancel_event 停止后续 LLM 调用，节省费用。
    """
    # 去掉首尾空格
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    header_authenticated = _authenticated(request, cookie_write=True)
    client = request.client.host if request.client else "local"
    if not _allow_plan(client):
        raise HTTPException(status_code=429, detail="规划太频繁，请一分钟后再试")
    lease = _inflight.try_acquire()
    if lease is None:
        raise HTTPException(status_code=429, detail="同时规划已达上限")
    logger.info("plan_request client=%s bytes=%s", client, len(query))

    cancel_event = asyncio.Event()
    _inflight.cancels.add(cancel_event)
    # server 自己持有 RunStats：总超时、断连由这一层判定，pipeline 结束时再序列化。
    request_stats = RunStats()

    async def on_idle() -> None:
        if await request.is_disconnected():
            request_stats.note("sse", "cancelled")
            cancel_event.set()

    async def event_source():
        task = asyncio.current_task()
        if task is not None:
            _inflight.tasks.add(task)
        # 关键：watch_aiter 用 create_task 驱动每一帧，会复制当前 context。
        # 必须在这里（生成器的 context）预设同一个 RunStats，否则 pipeline 首帧里
        # 设置的 ContextVar 只留在那一帧的 task，后续帧与深层子任务全部读到 None。
        begin_run_stats(request_stats)
        agen = stream_plan(
            query,
            cancel_event=cancel_event,
            slots=req.slots,
            parent_plan_id=req.plan_id,
            mode=req.mode,
            lodging_scope=req.lodging_scope,
            party_size=req.party_size,
            budget_scope=req.budget_scope,
            cleared_fields=req.cleared_fields,
            intent_source=req.intent_source,
            intercity_mode=req.intercity_mode,
            return_required=req.return_required,
            run_stats=request_stats,
        )
        # SSE 总时限：默认 = 工作预算 + 收尾预留 + 30 秒兜底，覆盖「核心已停但流没关」的情况。
        total_timeout = float(
            os.getenv("SSE_TOTAL_TIMEOUT_SECONDS")
            or (
                float(os.getenv("PLAN_BUDGET_SECONDS") or 240)
                + float(os.getenv("PLAN_CLOSEOUT_SECONDS") or 10)
                + 30
            )
        )
        try:
            heartbeat = float(os.getenv("SSE_HEARTBEAT_SECONDS") or 15)
            async for event in watch_aiter(
                agen,
                cancel_event=cancel_event,
                on_idle=on_idle,
                timeout_seconds=total_timeout,
                heartbeat_seconds=heartbeat,
            ):
                if event.get("type") == "_heartbeat":
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except asyncio.TimeoutError:
            # watch_aiter 已先关闭上游生成器再抛超时；这里只补发一条终止事件并立刻结束流，
            # 保证 error 之后不会再有 final。子任务是否真的清理干净，另行由 cleanup 事件核实。
            request_stats.note("sse", "timed_out")
            logger.warning("plan_sse_timeout timeout=%.1fs", total_timeout)
            timeout_event = {
                "type": "error",
                "message": "规划超时，已中止本次请求",
                "code": "sse_timeout",
            }
            # watch_aiter 的 _close() 会触发上游生成器 finally；pipeline 把清理结果
            # 写进与 request_stats 同一个 RunStats 对象，这里补进终止事件，让客户端
            # 与排障日志都能看到子任务残留和 MCP 是否真的关掉。
            cleanup = request_stats.cleanup
            if cleanup is not None:
                timeout_event["cleanup"] = cleanup
            yield f"data: {json.dumps(timeout_event, ensure_ascii=False)}\n\n"
        finally:
            cancel_event.set()
            lease.release()
            _inflight.cancels.discard(cancel_event)
            if task is not None:
                _inflight.tasks.discard(task)

    try:
        response = StreamingResponse(
            _BoundStream(event_source(), lease, cancel_event),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        if header_authenticated:
            _issue_session(response)
        return response
    except Exception:
        cancel_event.set()
        lease.release()
        _inflight.cancels.discard(cancel_event)
        raise


@app.get("/api/plans")
async def plans(request: Request, limit: int = 20) -> dict[str, object]:
    """最近规划记录。库在 workspace/results/plans.db，不进 git。"""
    _authenticated(request)
    rows = await asyncio.to_thread(list_plans, limit)
    return {"plans": rows}


# 必须在全部 API 与 /results 之后注册，避免吃掉 /api/*
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
