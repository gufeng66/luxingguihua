"""SSE 生命周期：总超时、断连释放名额、error 之后不再发 final、子任务残留记录。

全部离线：`server.stream_plan`、`planner.subruns.run_map_agent` 等一律替换为假实现，
不访问模型、MCP 或高德。刻意直接 `await server.plan(...)` 拿 `StreamingResponse`
再手动消费 `body_iterator`，而不是用 `TestClient`：本用例关心的是「流被消费 / 被关闭
时名额与清理的真实行为」，TestClient 会把响应体整体读走，反而掩盖了逐帧结束的时序。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeClient:
    host = "127.0.0.1"


class FakeRequest:
    """`server.plan` 只用到 headers / cookies / client / is_disconnected。"""

    def __init__(self, disconnected: bool = False) -> None:
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.client = FakeClient()
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _decode(chunk: str) -> dict:
    assert chunk.startswith("data: "), chunk
    return json.loads(chunk[len("data: ") :].strip())


async def _drain_stream(response) -> list[dict]:
    """把 SSE 响应体读完，返回解析后的事件（心跳行丢弃）。"""
    events: list[dict] = []
    async for chunk in response.body_iterator:
        if chunk.startswith(":"):
            continue
        events.append(_decode(chunk))
    return events


def _forever(*_args, **_kwargs):
    """永不结束的 stream_plan：用于触发 SSE 总超时。"""

    async def gen():
        yield {"type": "status", "message": "开始规划"}
        await asyncio.sleep(3600)
        yield {"type": "final", "content": "超时之后绝不能出现"}

    return gen()


def _forever_after_token(*_args, **_kwargs):
    async def gen():
        yield {"type": "token", "text": "正在成文"}
        await asyncio.sleep(3600)

    return gen()


def _forever_with_cleanup(*_args, **kwargs):
    """模拟真实 pipeline：总超时被 aclose 时，finally 把清理结果写进共享 RunStats。

    server 与 stream_plan 持有同一个 request_stats 对象，server 的 timeout 分支
    才能从 request_stats.cleanup 读到并补进 error 事件（m02971 P1-⑥）。
    """
    run_stats = kwargs.get("run_stats")

    async def gen():
        try:
            yield {"type": "status", "message": "开始规划"}
            await asyncio.sleep(3600)
            yield {"type": "final", "content": "超时之后绝不能出现"}
        finally:
            if run_stats is not None:
                run_stats.set_cleanup({
                    "drain_started": True,
                    "drain_completed": True,
                    "subagent_leak": ["map"],
                    "mcp_close_started": True,
                    "mcp_close_completed": True,
                    "mcp_close_error": None,
                    "mcp_closed_after_timeout": True,
                })

    return gen()


def _silent_forever(*_args, **_kwargs):
    """先不产出任何事件，让 watch_aiter 停在空闲等待上，on_idle 才有机会检测断连。"""

    async def gen():
        await asyncio.sleep(3600)
        yield {"type": "final", "content": "断连之后绝不能出现"}

    return gen()


@pytest.fixture()
def clean_inflight(monkeypatch: pytest.MonkeyPatch):
    """每个用例前后清空名额/限流/认证状态，避免互相污染。"""
    import server

    def reset() -> None:
        server._inflight._n = 0
        server._inflight.cancels.clear()
        server._inflight.tasks.clear()
        server._rate_hits.clear()

    monkeypatch.delenv("PLAN_API_KEY", raising=False)
    monkeypatch.setenv("PLAN_RATE_PER_MINUTE", "100")
    reset()
    try:
        yield server
    finally:
        reset()


async def _open_stream(server, monkeypatch, maker, **env: str):
    monkeypatch.setattr(server, "stream_plan", maker)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return await server.plan(server.PlanRequest(query="郑州一日游，预算 500"), FakeRequest())


@pytest.mark.asyncio
async def test_sse_total_timeout_releases_inflight(clean_inflight, monkeypatch: pytest.MonkeyPatch) -> None:
    """总超时触发后：名额归零，且流以 error(code=sse_timeout) 结束。"""
    server = clean_inflight
    monkeypatch.setenv("SSE_TOTAL_TIMEOUT_SECONDS", "0.1")
    response = await _open_stream(server, monkeypatch, _forever, SSE_TOTAL_TIMEOUT_SECONDS="0.1")
    events = await _drain_stream(response)
    await response.body_iterator.aclose()

    assert [event["type"] for event in events][-1] == "error"
    assert events[-1]["code"] == "sse_timeout"
    assert events[-1]["message"] == "规划超时，已中止本次请求"
    assert server._inflight.count == 0
    assert not server._inflight.cancels
    assert not server._inflight.tasks


@pytest.mark.asyncio
async def test_sse_timeout_error_carries_pipeline_cleanup(clean_inflight, monkeypatch: pytest.MonkeyPatch) -> None:
    """总超时终止事件要带上 generator finally 写入的清理结果，不只靠日志。

    m02971 P1-⑥：客户端与排障都要能看到子任务残留和 MCP 是否真的关掉。
    """
    server = clean_inflight
    response = await _open_stream(
        server, monkeypatch, _forever_with_cleanup, SSE_TOTAL_TIMEOUT_SECONDS="0.1"
    )
    events = await _drain_stream(response)
    await response.body_iterator.aclose()

    error = events[-1]
    assert error["type"] == "error"
    assert error["code"] == "sse_timeout"
    cleanup = error.get("cleanup")
    assert cleanup is not None
    assert cleanup["subagent_leak"] == ["map"]
    assert cleanup["mcp_closed_after_timeout"] is True
    assert cleanup["mcp_close_completed"] is True
    assert "final" not in [event["type"] for event in events]


@pytest.mark.asyncio
async def test_sse_client_disconnect_releases_inflight(clean_inflight, monkeypatch: pytest.MonkeyPatch) -> None:
    """浏览器断开：on_idle 置 cancel_event，流自行结束并释放名额。"""
    server = clean_inflight
    monkeypatch.setattr(server, "stream_plan", _silent_forever)
    response = await server.plan(
        server.PlanRequest(query="郑州一日游，预算 500"), FakeRequest(disconnected=True)
    )
    events = await _drain_stream(response)
    await response.body_iterator.aclose()

    # 断连走的是 cancel 分支：既不报超时，也不会有 final。
    kinds = [event["type"] for event in events]
    assert "final" not in kinds
    assert "error" not in kinds
    assert server._inflight.count == 0
    assert not server._inflight.cancels


@pytest.mark.asyncio
async def test_error_then_no_final(clean_inflight, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时的 error 之后不得再出现 final（客户端不能把它当成可用产物）。"""
    server = clean_inflight
    response = await _open_stream(server, monkeypatch, _forever_after_token, SSE_TOTAL_TIMEOUT_SECONDS="0.1")
    events = await _drain_stream(response)
    await response.body_iterator.aclose()

    kinds = [event["type"] for event in events]
    assert "error" in kinds
    error_at = kinds.index("error")
    assert "final" not in kinds
    assert kinds.count("error") == 1
    # error 必须是最后一帧，后面什么都没有。
    assert error_at == len(kinds) - 1
    assert server._inflight.count == 0


def _fake_budget():
    return SimpleNamespace(agents=1, work_left=lambda: 100.0, timeout_for=lambda _n: 5.0)


def _stubborn_factory(stop: asyncio.Event):
    """吞掉取消、永不结束的子任务，用来制造清理窗口内的残留。"""

    async def stubborn(**_kwargs):
        while not stop.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                continue
        return {"started": True, "summary": "", "text": "", "timed_out": False}

    return stubborn


@pytest.mark.asyncio
async def test_subagent_leak_recorded_when_drain_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理窗口内子任务不结束：记进 out["subagent_leak"] 并发出内部 cleanup 事件。"""
    from planner import subruns

    stop = asyncio.Event()
    monkeypatch.setenv("PLAN_CLEANUP_SECONDS", "0.01")
    monkeypatch.setattr(subruns, "run_map_agent", _stubborn_factory(stop))

    out: dict = {"records": []}
    cancel = asyncio.Event()
    events: list[dict] = []
    try:
        agen = subruns.iter_agents(
            out,
            need_map=True,
            need_ticket=False,
            ticket_tools=[],
            llm=None,
            query="郑州一日游",
            budget=_fake_budget(),
            cancel_event=cancel,
            city="郑州",
        )
        events.append(await agen.__anext__())
        events.append(await agen.__anext__())
        # 再推进一次：生成器此时才创建 guard 任务并停在队列等待上，
        # 之后取消才有「已启动、吞掉取消」的子任务可被判定为残留。
        nxt = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.02)
        cancel.set()
        events.append(await asyncio.wait_for(nxt, timeout=3))
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
    finally:
        stop.set()
        await asyncio.sleep(0.05)

    kinds = [event["type"] for event in events]
    assert "cleanup" in kinds
    cleanup = events[kinds.index("cleanup")]
    assert cleanup == {"type": "cleanup", "subagent_leak": ["map"]}
    assert out["subagent_leak"] == ["map"]
    assert out["cancelled"] is True
    # cleanup 不是 SSE 契约事件：不会顶替任何既有事件。
    assert cleanup["type"] not in {
        "status", "slots", "clarify", "step", "warning", "token", "final", "error",
    }


@pytest.mark.asyncio
async def test_shielded_drain_survives_second_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理期间再被取消一次：drain 不跳过清理，残留照样记录。"""
    from planner import subruns

    stop = asyncio.Event()
    monkeypatch.setenv("PLAN_CLEANUP_SECONDS", "0.2")
    monkeypatch.setattr(subruns, "run_map_agent", _stubborn_factory(stop))

    out: dict = {"records": []}
    agen = subruns.iter_agents(
        out,
        need_map=True,
        need_ticket=False,
        ticket_tools=[],
        llm=None,
        query="郑州一日游",
        budget=_fake_budget(),
        cancel_event=asyncio.Event(),
        city="郑州",
    )
    # 先取出两侧已开始的两条事件。
    assert (await agen.__anext__())["id"] == "map"
    assert (await agen.__anext__())["type"] == "subagent"
    # 第三次推进让生成器真正创建 guard 任务并停在队列等待上（取消要打在 await 上）。
    pending_call = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.03)

    started = asyncio.get_running_loop().time()
    pending_call.cancel()  # 第一次取消：CancelledError 打进 await，触发 finally → drain
    await asyncio.sleep(0.02)
    assert not pending_call.done(), "drain 应该还在清理窗口里等待"
    pending_call.cancel()  # 清理期间二次取消
    try:
        await pending_call
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        await asyncio.sleep(0.05)

    elapsed = asyncio.get_running_loop().time() - started
    assert out.get("subagent_leak") == ["map"]
    # 没有跳过清理：耗时覆盖清理窗口，且没有被二次取消立刻打断。
    assert elapsed >= 0.15
    assert elapsed < 2.0
