"""B3 票务复用三元状态 + B4 三层地图语义的纯函数回归，不联网。

这里只验证规则层与文案层：父状态怎么分类、复用来源怎么影响正文首句、
「不要地图」和「不要找景点」是不是两种不同的否决层级。
pipeline 的接线由 test_stream_plan / test_legacy_reuse 覆盖，本文件不碰落盘。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner.routing import classify_ticket_parent, map_scope, skip_map_reason  # noqa: E402
from planner.ticket_facts import ticket_markdown  # noqa: E402

# 一条最小可用车次事实：正文表格渲染分支要能跑通。
FACT = {
    "kind": "train",
    "status": "verified",
    "source_id": "get-tickets",
    "value": {
        "train_no": "G1234",
        "date": "2026-11-14",
        "origin": "新乡东",
        "destination": "郑州东",
        "seat": "二等座",
        "price": 96.5,
        "availability": "有票",
    },
}
QUERIED_AT = "2026-11-13T09:00:00+08:00"


# ---------------------------------------------------------------- B3 父状态分类


@pytest.mark.parametrize(
    ("parent_reason", "parent_class", "reuse_source"),
    [
        ("ok", "ok", "parent_ok"),
        ("skipped", "skipped", "parent_skipped"),
        ("unavailable", "failed", "parent_failed"),
        ("missed", "failed", "parent_failed"),
        ("timeout", "failed", "parent_failed"),
        ("reused", "ok", "parent_reused"),
        ("", "unknown", "parent_unknown"),
        (None, "unknown", "parent_unknown"),
        ("something_else", "unknown", "parent_unknown"),
    ],
    ids=[
        "ok",
        "skipped",
        "unavailable",
        "missed",
        "timeout",
        "reused",
        "empty",
        "none",
        "unknown_value",
    ],
)
def test_classify_ticket_parent(parent_reason, parent_class, reuse_source) -> None:
    assert classify_ticket_parent(parent_reason) == (parent_class, reuse_source)


def test_classify_ticket_parent_keeps_six_states_distinct() -> None:
    """六个终态不能被粗分类抹平：ok 与 reused 同属 ok 类但来源不同，
    三种失败同属 failed 类且来源一致，skipped 自成一路。"""
    classified = {
        reason: classify_ticket_parent(reason)
        for reason in ["ok", "skipped", "unavailable", "missed", "timeout", "reused"]
    }
    assert classified["ok"] == ("ok", "parent_ok")
    assert classified["reused"] == ("ok", "parent_reused")
    assert classified["skipped"] == ("skipped", "parent_skipped")
    for reason in ("unavailable", "missed", "timeout"):
        assert classified[reason] == ("failed", "parent_failed")
    # 4 个来源：ok / skipped / failed / reused，三种失败共享同一个来源。
    assert len({source for _, source in classified.values()}) == 4


# ------------------------------------------------------------------ B4 三层语义


@pytest.mark.parametrize(
    ("query", "need_poi_search", "need_route", "need_map_artifact", "reason"),
    [
        ("郑州玩，不要地图", True, True, False, "user_no_map"),
        # 「不要路线图」说的是不要那张 HTML 图，不等于不要查路线耗时。
        ("郑州玩，不要路线图", True, True, False, "user_no_map"),
        ("郑州玩，不需要地图", True, True, False, "user_no_map"),
        # 明确不查/不排路线才关路线耗时查询：景点候选与地图 HTML 仍保留。
        ("郑州玩，不查路线", True, False, True, "user_no_route"),
        ("郑州玩，不排路线", True, False, True, "user_no_route"),
        ("郑州玩，不查交通耗时", True, False, True, "user_no_route"),
        # 不检索景点就没有可信行程，路线与地图一并停，reason 必须是 user_no_poi。
        ("郑州玩，不要找景点", False, False, False, "user_no_poi"),
        ("郑州玩，不要景点", False, False, False, "user_no_poi"),
        ("郑州玩，不搜景点", False, False, False, "user_no_poi"),
        ("郑州玩，不需要找景点", False, False, False, "user_no_poi"),
        ("这周六从新乡坐高铁去郑州一日游", True, True, True, ""),
        ("", True, True, True, ""),
    ],
    ids=[
        "no_map",
        "no_route_map",
        "no_map_need",
        "no_route",
        "no_route_plan",
        "no_route_duration",
        "no_poi",
        "no_sight",
        "no_sight_search",
        "no_poi_need",
        "unspecified",
        "empty",
    ],
)
def test_map_scope(query, need_poi_search, need_route, need_map_artifact, reason) -> None:
    assert map_scope(query) == {
        "need_poi_search": need_poi_search,
        "need_route": need_route,
        "need_map_artifact": need_map_artifact,
        "reason": reason,
    }


def test_map_scope_no_poi_wins_when_both_present() -> None:
    """同时说「不要地图」和「不要找景点」时按更强的否决走，不能只报不要地图。"""
    scope = map_scope("不要地图也不要找景点")
    assert scope["reason"] == "user_no_poi"
    assert scope["need_poi_search"] is False


def test_map_scope_no_route_and_no_map_artifact_coexist() -> None:
    """同时否决地图产物与路线查询：reason 取更直观的 user_no_map，但 need_route 仍如实为 False。

    景点候选与结构化行程照旧保留，地图 HTML 不写、路线耗时也不查，三者互不掩盖。
    """
    scope = map_scope("郑州玩，不要地图，也不查路线")
    assert scope == {
        "need_poi_search": True,
        "need_route": False,
        "need_map_artifact": False,
        "reason": "user_no_map",
    }


def test_map_scope_no_route_does_not_match_route_map() -> None:
    """「不要路线图」只是一张图，不能被子串误判成「不查路线」。"""
    scope = map_scope("郑州玩，不要路线图")
    assert scope["need_route"] is True
    assert scope["need_map_artifact"] is False
    assert scope["reason"] == "user_no_map"


def test_skip_map_reason_is_thin_wrapper() -> None:
    """现有调用方（pipeline）只读返回值，薄封装必须与 map_scope 的 reason 一致。"""
    for query in ("郑州玩，不要地图", "郑州玩，不要找景点", "郑州一日游"):
        assert skip_map_reason(query) == (map_scope(query)["reason"] or None)


# ------------------------------------------------------- B3 正文文案优先级


def test_head_skipped_reason_wins_over_reuse() -> None:
    """本次就判定无需铁路时，即使带着复用来源也先说「本次未查票」。"""
    text = ticket_markdown(
        [FACT],
        reused=True,
        queried_at=QUERIED_AT,
        reason="skipped",
        reuse_source="parent_ok",
        parent_reason="ok",
    )
    assert text.splitlines()[0] == "本次未查票（无需铁路）。"


@pytest.mark.parametrize(
    ("parent_reason", "cause"),
    [
        ("unavailable", "服务不可用或依赖缺失"),
        ("missed", "未调度或空结果"),
        ("timeout", "超时"),
    ],
)
def test_head_parent_failed_never_claims_reuse(parent_reason, cause) -> None:
    """父方案没查到票：必须写明失败原因，且不能出现「沿用上次查询结果」。"""
    head = ticket_markdown(
        [],
        reused=True,
        queried_at=QUERIED_AT,
        reuse_source="parent_failed",
        parent_reason=parent_reason,
    ).splitlines()[0]
    assert cause in head
    assert "本次未重新查询，余票未知" in head
    assert "上次查询结果" not in head
    assert "沿用更早一次查询" not in head


def test_head_parent_reused_points_to_earlier_query() -> None:
    head = ticket_markdown(
        [FACT],
        reused=True,
        queried_at=QUERIED_AT,
        reuse_source="parent_reused",
        parent_reason="reused",
    ).splitlines()[0]
    assert "沿用更早一次查询" in head
    assert QUERIED_AT in head
    assert "余票未刷新" in head
    assert "上次查询结果" not in head


def test_head_parent_ok_keeps_stale_notice() -> None:
    head = ticket_markdown(
        [FACT],
        reused=True,
        queried_at=QUERIED_AT,
        reuse_source="parent_ok",
        parent_reason="ok",
    ).splitlines()[0]
    assert head == f"上次查询结果，未刷新（查询时间 {QUERIED_AT}）。余票不是当前余票。"


def test_head_parent_skipped_says_no_ticket_needed() -> None:
    head = ticket_markdown(
        [],
        reused=True,
        queried_at=QUERIED_AT,
        reuse_source="parent_skipped",
        parent_reason="skipped",
    ).splitlines()[0]
    assert head == "本次未查票（无需铁路）。"


def test_head_parent_unknown_does_not_invent_history() -> None:
    """旧快照缺 ticket_reason 时既不能说「沿用上次结果」，也不能说「已核实」。"""
    head = ticket_markdown(
        [],
        reused=True,
        queried_at=QUERIED_AT,
        reuse_source="parent_unknown",
        parent_reason="",
    ).splitlines()[0]
    assert "本次未重新查询，余票状态未知" in head
    assert "上次查询结果" not in head


def test_head_legacy_call_without_reuse_source_is_backward_compatible() -> None:
    """pipeline 尚未接线时只传 reused=True：文案必须与旧行为完全一致。"""
    text = ticket_markdown([FACT], reused=True, queried_at=QUERIED_AT)
    assert text.splitlines()[0] == f"上次查询结果，未刷新（查询时间 {QUERIED_AT}）。余票不是当前余票。"


def test_head_fallback_branches_without_reuse() -> None:
    assert "没有可解析的车次事实" in ticket_markdown([], reused=False, queried_at=None)
    assert "只来自本次工具原始响应" in ticket_markdown([FACT], reused=False, queried_at=None)


def test_fact_table_still_renders_under_every_head() -> None:
    """只改首句文案，车次表格与来源标识必须照旧渲染。"""
    for kwargs in (
        {"reuse_source": "parent_failed", "parent_reason": "timeout"},
        {"reuse_source": "parent_reused", "parent_reason": "reused"},
        {"reuse_source": "parent_ok", "parent_reason": "ok"},
        {"reason": "skipped"},
        {},
    ):
        text = ticket_markdown([FACT], reused=True, queried_at=QUERIED_AT, **kwargs)
        assert "G1234" in text
        assert "新乡东→郑州东" in text
        assert "二等座" in text
        assert "96.5" in text
        assert "（来源 get-tickets）" in text
