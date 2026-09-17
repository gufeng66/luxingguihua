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
# 把 Python 字典编成 JSON 字符串塞进 SSE
import json
# 读 CORS 等环境变量
import os
# 定位 frontend、results 目录
from pathlib import Path

# 加载 .env 里的密钥（DeepSeek / 高德等）
from dotenv import find_dotenv, load_dotenv
# FastAPI 框架与异常、请求对象
from fastapi import FastAPI, HTTPException, Request
# 跨域中间件：允许指定来源的前端访问 API
from fastapi.middleware.cors import CORSMiddleware
# 返回文件 / 返回流式响应
from fastapi.responses import FileResponse, StreamingResponse
# 把某个文件夹挂成 URL 前缀下的静态站点
from fastapi.staticfiles import StaticFiles
# 请求体校验模型
from pydantic import BaseModel, Field

# 规划流水线 + 可取消的异步迭代包装 + 结果目录
from planner_service import RESULTS_DIR, stream_plan, watch_aiter
# 可选：前端确认卡回传的槽位
from slots import TravelSlots

# 启动时加载环境变量
load_dotenv(find_dotenv())

# 项目根目录 = 本文件所在目录
BASE_DIR = Path(__file__).resolve().parent
# Vue 前端静态资源目录
FRONTEND_DIR = BASE_DIR / "frontend"
# 确保结果目录存在（首次克隆仓库时可能只有 .gitkeep）
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# 创建 FastAPI 应用（标题会出现在 /docs 自动文档里）
app = FastAPI(title="旅游规划多智能体 API", version="1.0.0")

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
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /results/xxx → 磁盘上 RESULTS_DIR/xxx（方案 md、地图 html）
app.mount("/results", StaticFiles(directory=str(RESULTS_DIR)), name="results")


class PlanRequest(BaseModel):
    """浏览器 POST /api/plan 时提交的 JSON 结构。"""

    # 用户一句话需求（限制长度，防止超长刷爆模型）
    query: str = Field(..., min_length=1, max_length=300, description="自然语言旅游需求")
    # 可选：用户在确认卡里改完后的槽位；有则跳过再次澄清
    slots: TravelSlots | None = Field(default=None, description="确认卡回传的已校验槽位")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """健康检查：运维/脚本用来确认服务是否活着。"""
    return {"status": "ok"}


@app.post("/api/plan")
async def plan(req: PlanRequest, request: Request) -> StreamingResponse:
    """规划主接口：以 SSE 流式返回多智能体过程事件。

    若浏览器关掉标签页，会通过 cancel_event 停止后续 LLM 调用，节省费用。
    """
    # 去掉首尾空格
    query = req.query.strip()
    # 防御空字符串（Field 已限制，这里再保险一次）
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")

    # 取消开关：set() 之后 stream_plan / watch_aiter 会尽快停
    cancel_event = asyncio.Event()

    async def on_idle() -> None:
        """每隔一小段空闲时间检查浏览器是否已断开。"""
        if await request.is_disconnected():
            # 客户端走了 → 通知规划停
            cancel_event.set()

    async def event_source():
        """SSE 生成器：把业务事件编成 data: ... 帧。"""
        # 启动规划异步生成器（可带已确认 slots）
        agen = stream_plan(query, cancel_event=cancel_event, slots=req.slots)
        try:
            # watch_aiter：边取事件边检查取消/断连
            async for event in watch_aiter(agen, cancel_event=cancel_event, on_idle=on_idle):
                # ensure_ascii=False 保留中文，方便前端直接显示
                payload = json.dumps(event, ensure_ascii=False)
                # SSE 规范：以 data: 开头，空行结束一帧
                yield f"data: {payload}\n\n"
        finally:
            # 无论正常结束还是异常，都标取消，避免后台残留任务
            cancel_event.set()

    # 告诉浏览器这是事件流，并禁止代理缓冲
    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def index() -> FileResponse:
    """打开网站首页 = 返回 frontend/index.html。"""
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html 不存在")
    return FileResponse(index_path)


@app.get("/app.js")
async def frontend_js() -> FileResponse:
    """前端逻辑脚本。"""
    js_path = FRONTEND_DIR / "app.js"
    if not js_path.exists():
        raise HTTPException(status_code=404, detail="frontend/app.js 不存在")
    return FileResponse(js_path, media_type="application/javascript")


@app.get("/style.css")
async def frontend_css() -> FileResponse:
    """前端样式表。"""
    css_path = FRONTEND_DIR / "style.css"
    if not css_path.exists():
        raise HTTPException(status_code=404, detail="frontend/style.css 不存在")
    return FileResponse(css_path, media_type="text/css")
