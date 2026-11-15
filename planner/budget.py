"""一次规划的执行预算。最后一段只留给收尾，不发给外部调用。"""

from __future__ import annotations

import os
import time
from typing import Callable


def _num(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class RunBudget:
    """工作截止后禁止新的外部请求和模型调用。清理不计入这 240 秒的承诺。"""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.total = _num("PLAN_BUDGET_SECONDS", 240)
        self.closeout = _num("PLAN_CLOSEOUT_SECONDS", 10)
        self.slot = _num("PLAN_SLOT_SECONDS", 20)
        self.agents = _num("PLAN_AGENT_SECONDS", 120)
        self.evidence = _num("PLAN_EVIDENCE_SECONDS", 60)
        self.summary = _num("PLAN_SUMMARY_SECONDS", 40)
        self.model_limit = int(_num("PLAN_MODEL_CALLS", 6))
        self.tool_limit = int(_num("PLAN_TOOL_ATTEMPTS", 80))
        self.token_limit = int(_num("PLAN_TOKEN_BUDGET", 120_000))
        self.tool_attempts = 0
        self.model_calls: dict[str, int] = {}
        self.external_blocked = False
        self.route_reserve = 0

    def work_left(self) -> float:
        return max(0.0, self.started + self.total - self.closeout - self.clock())

    def timeout_for(self, cap: float) -> float:
        return max(0.0, min(cap, self.work_left()))

    def allow_external(self) -> bool:
        return self.work_left() > 0 and self.tool_attempts < self.tool_limit

    def note_attempt(self, protected: bool = False) -> bool:
        """一次实际发出的外部请求。失败和重试也计。缓存读取不要调用这里。

        protected=True 表示这是已经预留出来的必要调用（例如必要路段的交通耗时），
        即使余量已跌进预留区也允许发出。
        """
        if self.work_left() <= 0 or self.tool_attempts >= self.tool_limit:
            self.external_blocked = True
            return False
        if not protected and self.attempts_left() <= self.reserved_attempts():
            # 不是额度耗尽，而是留给必要路段；不标记 external_blocked，避免被误读成「总次数用完」。
            return False
        self.tool_attempts += 1
        return True

    def allow_model(self, name: str, tokens_used: int) -> bool:
        if self.work_left() <= 0 or tokens_used >= self.token_limit:
            return False
        return self.model_calls.get(name, 0) < self.model_limit

    def note_model(self, name: str) -> None:
        self.model_calls[name] = self.model_calls.get(name, 0) + 1

    # —— 预留：先保必要路段，再拿余量做可选的检索 ——
    # 优先级（从高到低）：① 必去景点定位 ② 必要路段首选模式 ③ 必要路段换乘回退
    # ④ 普通候选检索 ⑤ 开放规则取证。预留不改变总时长，只是把余量算清楚。

    def attempts_left(self) -> int:
        """还剩多少次可发出的外部请求。"""
        return max(0, self.tool_limit - self.tool_attempts)

    def reserve_attempts(self, rounds: int, adjacent_pairs: int) -> int:
        """给路线补查留出次数：每轮每个相邻段每种模式各算一次。"""
        rounds = max(0, int(rounds))
        pairs = max(0, int(adjacent_pairs))
        return rounds * pairs * 2

    def reserve_seconds(self, route_pairs: int) -> float:
        """给路线补查留出时间，上限 60 秒。"""
        pairs = max(0, int(route_pairs))
        return min(60.0, 12.0 * pairs) if pairs else 0.0

    def usable_attempts(self, routes: int, pairs: int) -> int:
        """扣掉路线预留后可自由支配的次数。"""
        return max(0, self.attempts_left() - self.reserve_attempts(routes, pairs))

    def usable_seconds(self, pairs: int) -> float:
        """扣掉路线预留后可自由支配的秒数。"""
        return max(0.0, self.work_left() - self.reserve_seconds(pairs))

    # —— 行程装配用的简化接口：按「必要路段条数」一次性登记预留 ——

    def set_reserve(self, route_pairs: int) -> None:
        """登记本请求要留给路线补查的额度（每条路段按首选+回退两种模式算）。"""
        self.route_reserve = max(0, int(route_pairs))

    def reserved_attempts(self) -> int:
        """为必要路段留下的次数。"""
        return min(self.attempts_left(), self.route_reserve * 2)

    def reserved_seconds(self) -> float:
        """为必要路段留下的时间。"""
        return min(self.work_left(), self.reserve_seconds(self.route_reserve))

    def work_left_without_reserve(self) -> float:
        """可选检索能用的剩余工作时间。"""
        return max(0.0, self.work_left() - self.reserved_seconds())
