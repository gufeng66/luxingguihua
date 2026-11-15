"""请求级运行统计：谁能确定就由谁写，最后统一序列化。

设计动机（对应整改方案 B8）：动手前 `timings["tool_attempts"]=0` 这一种表现可能
同时意味着「没必要调用」「被预算拒绝」「依赖缺失」「用户取消」「路由跳过」，
排查时分不清。这里把「一个动作发生了没有」拆成互斥的状态码，由产生该事实的
那一层写入：

- `ticket_agent` / `ticket_cache`：连接 MCP 的尝试
- `RecordingTool`：具体票务工具查询
- `map_fallback` / `travel_search`：高德 POI、路线、开放规则取证
- `subruns`：子智能体残留
- `server`：SSE 总超时、客户端断开

写入用 `note(key, status)`；不允许调用方「事后推断」别人的状态。
刻意用 ContextVar 而不是逐层加参数：现有 `planner/usage.py` 已是这个模式，
子智能体 task 会继承上下文，写的是同一个 dict，不需要把 RunStats 穿过
`iter_agents` / `bounded_loop` / `assemble_itinerary` 的每一层签名。

序列化只放计数与状态名：**不存**用户原话、原始票务响应、模型输出全文。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Iterable

# 一个动作在本次请求里的最终状态。互斥，同一 key 后写覆盖先写。
STATES = (
    "attempted",
    "blocked_by_budget",
    "timed_out",
    "cancelled",
    "failed",
    "rate_limited",
    "succeeded",
    "not_needed",
    "dependency_missing",
)

_STATE_SET = frozenset(STATES)

# 已知动作键。新增时同步 README 的观测说明，避免出现无文档的口径。
KNOWN_KEYS = (
    "ticket_connect",
    "ticket_query",
    "map_poi",
    "map_route",
    "evidence_search",
    "subagent",
    "sse",
)


class RunStats:
    """一次请求的运行统计。

    `note()` 的 key 用 `<域>.<动作>` 的形式可以由调用方自定义，
    但 `KNOWN_KEYS` 覆盖的是会被前端/文档引用的口径。
    """

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._notes: list[str] = []
        # 本请求的资源清理结果（drain、MCP 关闭）。pipeline 在发 final / error
        # 之前写入；SSE 总超时或断连时由 aclose 触发的 finally 写入，server 再
        # 通过同一个 RunStats 对象读出来补进 error 事件。
        self._cleanup: dict[str, Any] | None = None

    def note(self, key: str, status: str) -> None:
        """记录某个动作的状态。未知状态直接拒绝，避免拼写错误静默丢数据。"""
        name = str(key or "").strip()
        state = str(status or "").strip()
        if not name:
            raise ValueError("run_stats: key 不能为空")
        if state not in _STATE_SET:
            raise ValueError(f"run_stats: 未知状态 {state!r}，可选 {', '.join(STATES)}")
        self._states[name] = state

    def bump(self, key: str, amount: int = 1) -> None:
        """计数型指标（请求次数、限流次数）。与状态互不影响。"""
        name = str(key or "").strip()
        if not name:
            raise ValueError("run_stats: key 不能为空")
        self._counts[name] = self._counts.get(name, 0) + int(amount)

    def note_text(self, text: str) -> None:
        """一条简短的技术说明。只放机制性描述，不放用户输入与模型全文。"""
        clean = str(text or "").strip()
        if clean:
            self._notes.append(clean)

    def state(self, key: str, default: str = "") -> str:
        return self._states.get(key, default)

    def count(self, key: str) -> int:
        return self._counts.get(key, 0)

    def set_cleanup(self, value: dict[str, Any]) -> None:
        """记录本请求的资源清理结果；存拷贝，避免调用方后续改动污染快照。"""
        self._cleanup = dict(value)

    @property
    def cleanup(self) -> dict[str, Any] | None:
        """供 server 在超时/断连后读取；没有清理记录时返回 None。"""
        return dict(self._cleanup) if self._cleanup is not None else None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"states": dict(self._states)}
        if self._counts:
            payload["counts"] = dict(self._counts)
        if self._notes:
            payload["notes"] = list(self._notes)
        if self._cleanup is not None:
            payload["cleanup"] = dict(self._cleanup)
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> "RunStats":
        """从快照或数据库里读回（复用时要在原统计上继续累加）。"""
        stats = cls()
        if not isinstance(raw, dict):
            return stats
        states = raw.get("states")
        if isinstance(states, dict):
            for key, value in states.items():
                name, state = str(key or ""), str(value or "")
                if name and state in _STATE_SET:
                    stats._states[name] = state
        counts = raw.get("counts")
        if isinstance(counts, dict):
            for key, value in counts.items():
                name = str(key or "")
                if not name:
                    continue
                try:
                    stats._counts[name] = int(value)
                except (TypeError, ValueError):
                    continue
        notes = raw.get("notes")
        if isinstance(notes, Iterable) and not isinstance(notes, (str, bytes)):
            for item in notes:
                clean = str(item or "").strip()
                if clean:
                    stats._notes.append(clean)
        cleanup = raw.get("cleanup")
        if isinstance(cleanup, dict):
            stats._cleanup = dict(cleanup)
        return stats


_current: ContextVar["RunStats | None"] = ContextVar("plan_run_stats", default=None)


def begin_run_stats(existing: RunStats | None = None) -> RunStats:
    """开启（或接管）本次请求的统计。返回的对象要一路写到 timings 里。"""
    stats = existing if isinstance(existing, RunStats) else RunStats()
    _current.set(stats)
    return stats


def current_stats() -> RunStats | None:
    """取当前请求的统计；不在规划流程里时为 None。

    刻意不自动创建：后台脚本、单测、`/api/health` 这些路径不该凭空多出一个
    RunStats 对象，调用方用 `if stats is not None` 判断即可。
    """
    return _current.get()


def note(key: str, status: str) -> None:
    """在任意层记一个状态；没有活动请求时静默跳过。"""
    stats = _current.get()
    if stats is None:
        return
    stats.note(key, status)


def bump(key: str, amount: int = 1) -> None:
    stats = _current.get()
    if stats is None:
        return
    stats.bump(key, amount)
