"""invoke_tool_with_timeout 的离线回归（纯 mock，不联网、不调用真实模型/工具）。

对应审查意见 P1「给所有工具调用增加统一超时和取消包装」。这层包装是「模型→工具」
循环里每单次工具调用的独立墙钟边界：底层 HTTP timeout 管不了工具内部的同步阻塞或
解析卡死，若没有它，一次卡住的工具会拖死整个子任务，甚至把「未获得结果」当成空结果
继续喂给模型、被编造成已核实数据。

本文件按 planner/subruns.py 的真实契约做行为断言（返回的是字符串 payload，不是 dict）：
  1. 成功：原样返回 ainvoke 结果，不截断、不改类型；
  2. 超时：返回含「超时/timeout」的明确失败 payload，不抛异常、不编造业务数据；
  3. 取消：cancel_event 预先 set 抛 asyncio.CancelledError，且 ainvoke 绝不执行；
  4. 其它异常：返回含「失败/failed/不可用」与异常类型名的 payload，不泄露原始消息、
     不把「工具结果不可用」包装成成功结果；
  5. 非法 timeout（0 / 负数 / None / inf）：抛 ValueError，消息含「有限正数 timeout」；
  6. 失败/超时 payload 与真实业务数据严格区分：工具超时后下游拿到的是「未获得结果」，
     绝不能被当成已核实数据（验证真实业务行为，而非线程 ID / 内部调用次数等实现细节）。

项目已安装 pytest-asyncio==1.2.0 且 pytest.ini 配置 asyncio_mode = auto，因此直接用
async 测试函数，不引入任何新依赖；取消路径通过 asyncio.run 包装同步函数以干净地断言
CancelledError 的传播。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner.subruns import invoke_tool_with_timeout  # noqa: E402

# 源码里的精确字面量（planner/subruns.py:104-112），改这里就能立刻发现契约漂移。
TIMEOUT_TEXT = "工具调用超时（timeout），未获得工具结果"
FAILED_PREFIX = "工具调用失败（failed）："
FAILED_SUFFIX = "，工具结果不可用"
INVALID_TIMEOUT_MSG = "invoke_tool_with_timeout 需要有限正数 timeout"

# 真实业务数据标记：用不可能由工具超时/失败凭空产生的内容，验证 payload 没有编造数据。
VERIFIED_MARKER = "VERIFIED_REAL_BUSINESS_DATA"
REAL_DATA_NOTE = "已核实车次 G1234 二等座余票 5 张"


@dataclass
class FakeTool:
    """带 .ainvoke(args) 的假工具，按 kind 模拟成功/挂起/抛异常，记录收到的 args。"""

    name: str = "fake_tool"
    kind: str = "success"  # success | hang | raise | cancelled
    result: Any = None
    exc: BaseException | None = None
    sleep_seconds: float = 3600.0
    received_args: list[Any] = field(default_factory=list)
    invoked: int = 0

    async def ainvoke(self, args: Any) -> Any:
        self.invoked += 1
        self.received_args.append(args)
        if self.kind == "hang":
            await asyncio.sleep(self.sleep_seconds)
            return self.result
        if self.kind == "raise":
            assert self.exc is not None
            raise self.exc
        if self.kind == "cancelled":
            raise asyncio.CancelledError()
        return self.result


def _new_event() -> asyncio.Event:
    return asyncio.Event()


# ---------------------------------------------------------------------------
# ① 成功路径：原样返回，不截断、不改类型
# ---------------------------------------------------------------------------
async def test_success_returns_raw_result_unchanged() -> None:
    """成功时原样返回 ainvoke 结果，类型、长度、内容都不动。"""
    payload = {"rows": [{"train": "G1", "price": "￥553"}], "count": 1}
    tool = FakeTool(kind="success", result=payload)

    got = await invoke_tool_with_timeout(
        tool, {"city": "郑州"}, timeout=5.0, cancel_event=_new_event(), who="map"
    )

    assert got is payload, "应返回同一个对象，不做拷贝/截断/改类型"
    assert got == payload
    assert tool.invoked == 1
    assert tool.received_args == [{"city": "郑州"}]


async def test_success_does_not_truncate_long_string() -> None:
    """长字符串结果不被截断——截断是 bounded_loop 的职责，不在本包装里做。"""
    long_text = "已核实车次数据：" + "G1234·" * 3000
    tool = FakeTool(kind="success", result=long_text)

    got = await invoke_tool_with_timeout(tool, {}, timeout=5.0, cancel_event=_new_event())

    assert isinstance(got, str)
    assert got == long_text
    assert len(got) == len(long_text) > 4000


# ---------------------------------------------------------------------------
# ② 超时：返回超时语义 payload，不抛异常、不含真实业务数据
# ---------------------------------------------------------------------------
async def test_timeout_returns_timeout_payload_not_exception() -> None:
    """工具挂起时，极小 timeout 触发 asyncio.wait_for，返回明确超时 payload 而非抛异常。"""
    tool = FakeTool(kind="hang", result=VERIFIED_MARKER)

    got = await invoke_tool_with_timeout(
        tool, {"keywords": "景点"}, timeout=0.01, cancel_event=_new_event(), who="map"
    )

    assert isinstance(got, str), "超时 payload 是字符串，不是 dict"
    assert "超时" in got and "timeout" in got
    assert "未获得工具结果" in got
    assert got == TIMEOUT_TEXT
    # 关键业务断言：工具其实「持有」真实数据，但超时后绝不能把它当成已取得的结果。
    assert VERIFIED_MARKER not in got
    assert REAL_DATA_NOTE not in got
    assert "失败" not in got and "failed" not in got  # 超时与失败是不同语义
    assert tool.invoked == 1


# ---------------------------------------------------------------------------
# ③ cancel_event 预先 set：抛 CancelledError，不执行 ainvoke
# ---------------------------------------------------------------------------
async def test_preset_cancel_event_raises_cancelled_error() -> None:
    """调用前 cancel_event 已 set，直接抛 asyncio.CancelledError，工具不被调用。"""
    cancel_event = _new_event()
    cancel_event.set()
    tool = FakeTool(kind="success", result=VERIFIED_MARKER)

    with pytest.raises(asyncio.CancelledError):
        await invoke_tool_with_timeout(
            tool, {"any": "args"}, timeout=5.0, cancel_event=cancel_event, who="ticket"
        )

    assert tool.invoked == 0, "取消后不应启动工具调用"
    assert tool.received_args == []


async def test_cancel_event_none_is_accepted() -> None:
    """cancel_event 允许 None（签名是 asyncio.Event | None），未 set 时正常走成功路径。"""
    payload = [{"poi": "少林寺", "lng": 113.0, "lat": 34.5}]
    tool = FakeTool(kind="success", result=payload)

    got = await invoke_tool_with_timeout(tool, {}, timeout=5.0, cancel_event=None)

    assert got is payload
    assert tool.invoked == 1


async def test_cancelled_error_from_tool_is_reraised_not_swallowed() -> None:
    """ainvoke 抛 CancelledError 时原样 re-raise，绝不被包成失败/超时 payload。"""
    tool = FakeTool(kind="cancelled")

    with pytest.raises(asyncio.CancelledError):
        await invoke_tool_with_timeout(tool, {}, timeout=5.0, cancel_event=_new_event())

    assert tool.invoked == 1


async def test_outer_task_cancel_is_not_converted_to_failure_payload() -> None:
    """运行中被上层取消（wait_for 内部抛 CancelledError）同样原样传播，不变成「工具失败」。"""
    tool = FakeTool(kind="hang", result=VERIFIED_MARKER)

    async def run() -> Any:
        task = asyncio.create_task(
            invoke_tool_with_timeout(tool, {}, timeout=30.0, cancel_event=_new_event())
        )
        await asyncio.sleep(0)
        task.cancel()
        return await task

    with pytest.raises(asyncio.CancelledError):
        await run()


def test_preset_cancel_propagates_through_asyncio_run() -> None:
    """用 asyncio.run 包装同步测试：CancelledError 能一路传到调用方（取消不被吞掉）。"""
    cancel_event = _new_event()
    cancel_event.set()
    tool = FakeTool(kind="success", result=VERIFIED_MARKER)

    async def run() -> Any:
        return await invoke_tool_with_timeout(tool, {}, timeout=5.0, cancel_event=cancel_event)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert tool.invoked == 0


# ---------------------------------------------------------------------------
# ④ 其它异常：失败 payload，含异常类型名，不含原始数据/业务数据
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc_type", [RuntimeError, ValueError, TypeError])
async def test_other_exception_returns_failed_payload_with_type_name(exc_type) -> None:
    """RuntimeError/ValueError/TypeError 都返回失败 payload，含类型名，不抛到循环外。"""
    tool = FakeTool(kind="raise", exc=exc_type("内部原始错误消息 SECRET"))

    got = await invoke_tool_with_timeout(
        tool, {"q": "票"}, timeout=5.0, cancel_event=_new_event(), who="ticket"
    )

    assert isinstance(got, str), "失败 payload 是字符串，不是 dict"
    assert "失败" in got and "failed" in got
    assert "工具结果不可用" in got
    assert exc_type.__name__ in got
    assert got == f"{FAILED_PREFIX}{exc_type.__name__}{FAILED_SUFFIX}"
    # 不泄露异常原始消息，也不把「不可用」包装成真实业务数据。
    assert "内部原始错误消息" not in got
    assert "SECRET" not in got
    assert VERIFIED_MARKER not in got
    assert REAL_DATA_NOTE not in got
    assert "超时" not in got and "timeout" not in got  # 失败与超时是不同语义
    assert tool.invoked == 1


async def test_failed_payload_is_not_a_success_result() -> None:
    """失败 payload 不能被当成成功结果：没有业务数据、带明确失败标记、不是原结果对象。"""
    real = {"trains": ["G1", "G2"], "verified": True}
    tool = FakeTool(kind="raise", exc=RuntimeError("db down"), result=real)

    got = await invoke_tool_with_timeout(tool, {}, timeout=5.0, cancel_event=_new_event())

    assert got is not real
    assert not isinstance(got, (dict, list)), "失败时不得返回结构化的「成功数据」"
    assert "失败" in got and "工具结果不可用" in got
    assert "G1" not in got and "verified" not in got and "True" not in got


# ---------------------------------------------------------------------------
# ⑤ 非法 timeout：抛 ValueError，消息含「有限正数 timeout」
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_timeout",
    [0, -1, -0.001, None, float("inf"), float("nan"), "5"],
)
async def test_invalid_timeout_raises_value_error(bad_timeout) -> None:
    """0 / 负数 / None / inf / NaN / 非数值都抛 ValueError，消息含「有限正数 timeout」。

    注意：bool 是 int 子类，源码的 isinstance 校验会把 True 当成 1.0 放行，故不列入。
    """
    tool = FakeTool(kind="success", result=VERIFIED_MARKER)

    with pytest.raises(ValueError) as exc_info:
        await invoke_tool_with_timeout(
            tool, {}, timeout=bad_timeout, cancel_event=_new_event()
        )

    assert "有限正数 timeout" in str(exc_info.value)
    assert str(exc_info.value) == INVALID_TIMEOUT_MSG
    assert tool.invoked == 0, "校验应在调用 ainvoke 之前，非法 timeout 不应启动工具"


def test_invalid_timeout_propagates_through_asyncio_run() -> None:
    """同步包装验证：非法 timeout 的 ValueError 能传到调用方，且不被当成工具失败 payload。"""
    tool = FakeTool(kind="success", result=VERIFIED_MARKER)

    async def run() -> Any:
        return await invoke_tool_with_timeout(tool, {}, timeout=0, cancel_event=_new_event())

    with pytest.raises(ValueError, match="有限正数 timeout"):
        asyncio.run(run())
    assert tool.invoked == 0


# ---------------------------------------------------------------------------
# ⑥ 业务行为：超时/失败 payload 与真实数据严格区分，下游不会误判为已核实
# ---------------------------------------------------------------------------
async def test_unavailable_payloads_never_claim_verified_business_data() -> None:
    """模拟一个消费侧判定：只有拿到真实结构化结果才算「已核实」，超时/失败都不算。"""
    real = {"rows": [{"train": "G1234", "left_tickets": 5}], "source": "12306"}
    markers = ("G1234", "left_tickets", "12306", REAL_DATA_NOTE, VERIFIED_MARKER)

    # 成功：下游能拿到真实业务数据。
    ok_tool = FakeTool(kind="success", result=real)
    ok = await invoke_tool_with_timeout(ok_tool, {}, timeout=5.0, cancel_event=_new_event())
    assert isinstance(ok, dict)
    assert "rows" in ok and ok["rows"][0]["train"] == "G1234"

    # 超时：payload 是明确的「未获得工具结果」，不含任何业务数据标记。
    slow_tool = FakeTool(kind="hang", result=real, sleep_seconds=3600)
    timed_out = await invoke_tool_with_timeout(
        slow_tool, {}, timeout=0.01, cancel_event=_new_event()
    )
    assert not isinstance(timed_out, (dict, list))
    assert "未获得工具结果" in timed_out
    for marker in markers:
        assert marker not in str(timed_out), f"超时 payload 混入了业务数据: {marker}"

    # 失败：payload 是明确的「工具结果不可用」，同样不含业务数据标记。
    broken_tool = FakeTool(kind="raise", exc=RuntimeError("upstream 500"), result=real)
    failed = await invoke_tool_with_timeout(broken_tool, {}, timeout=5.0, cancel_event=_new_event())
    assert not isinstance(failed, (dict, list))
    assert "工具结果不可用" in failed
    for marker in markers:
        assert marker not in str(failed), f"失败 payload 混入了业务数据: {marker}"

    # 一个朴素的消费侧判定：把这三类 payload 分别喂进去，只有成功被当作已核实数据。
    def is_verified_business_data(payload: Any) -> bool:
        return isinstance(payload, dict) and bool(payload.get("rows"))

    assert is_verified_business_data(ok) is True
    assert is_verified_business_data(timed_out) is False
    assert is_verified_business_data(failed) is False
