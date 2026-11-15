"""审查整改回归：成文白名单不泄漏 map_context、复用票务按父态如实措辞。

两个缺陷各有一组离线用例：

1. ``map_summary_block`` 在没有可核验的结构化行程时（``plan_ready=False``）
   或 map agent 降级（timeout/incomplete/missed）时，绝不能把 ``map_context``
   原文交回模型；有结构化行程时只给白名单派生摘要，未定位项/候选不能写成已排入。
2. ``collect_issues`` 的 ``reason=="reused"`` 且 ``facts_count==0`` 分支，
   必须按 ``routing.classify_ticket_parent`` 的 (parent_class, reuse_source)
   分流，而不是旧词表 ``{"failed","empty","timeout"}`` 硬猜。父状态
   unavailable/missed 不能再被写成「沿用上次车次查询 / 余票未刷新」。

并与 ``planner/ticket_facts.py`` 的 ``ticket_markdown`` 对照同一 reuse_source
的核心语义（正文与质量告警并不逐字相同，只对共享措辞做一致性断言）。
"""

from __future__ import annotations

import pytest

from planner.quality import collect_issues
from planner.routing import classify_ticket_parent
from planner.summary import itinerary_whitelist, map_summary_block
from planner.ticket_facts import ticket_markdown


# ---------------------------------------------------------------------------
# 测试用的结构化行程：第1天 故宫 → 天安门（步行 12 分钟），第2天 长城。
# 与 evals/test_poi_gate.py 的辅助数据同构，本文件自带一份，保持自包含。
# ---------------------------------------------------------------------------


def _itinerary() -> dict:
    return {
        "days": [
            {
                "day": 1,
                "date": "2026-11-03",
                "items": [
                    {"poi_id": "gugong", "name": "故宫", "role": "visit", "start": "09:00", "end": "11:00"},
                    {"poi_id": "tiananmen", "name": "天安门", "role": "visit", "start": "11:20", "end": "12:20"},
                ],
                "legs": [
                    {"origin_id": "gugong", "dest_id": "tiananmen", "mode": "walking",
                     "duration_seconds": 720, "confidence": "reference"},
                ],
            },
            {
                "day": 2,
                "date": "2026-11-04",
                "items": [
                    {"poi_id": "changcheng", "name": "长城", "role": "visit", "start": "09:30", "end": "12:30"},
                ],
                "legs": [],
            },
        ],
        "schedule_status": "ok",
    }


def _collect(state: dict) -> list[dict]:
    """按 collect_issues 现有签名调用，不用的参数一律传 None/[]。"""
    return collect_issues(
        checks={"ticket": "unverified"},
        ticket_diagnostics=None,
        summary_degraded=False,
        unlocated=[],
        ticket_applicability=None,
        ticket_state=state,
        itinerary=None,
        schedule_details=[],
        route_states=None,
        lodging_state=None,
        facts=None,
        return_options=None,
        plan_basis=None,
        rail_window=None,
    )


def _reused_state(parent_reason: str) -> dict:
    return {
        "reason": "reused",
        "queried_at": "2026-11-01 10:00",
        "facts_count": 0,
        "applicable_count": 0,
        "parent_reason": parent_reason,
    }


# ---------------------------------------------------------------------------
# 缺陷 1：map_summary_block 四类用例
# ---------------------------------------------------------------------------


def test_map_summary_block_ok_without_plan_ignores_body():
    """① ok + plan_ready=False：地图原文里的景点/耗时/证据链接一律不能当事实。"""
    body = "具体景点：故宫，步行12分钟到天安门，顺序见 https://evidence.example/leak"
    text = map_summary_block("ok", body, plan_ready=False)
    assert "故宫" not in text
    assert "步行12分钟" not in text
    assert "evidence.example" not in text
    # 只返回固定占位说明。
    assert "本轮没有可核验的结构化行程" in text
    assert "不能作为已确认事实" in text


@pytest.mark.parametrize("plan_ready", [False, True])
@pytest.mark.parametrize("map_reason", ["timeout", "incomplete", "missed", "skipped"])
def test_map_summary_block_degraded_never_leaks_body(map_reason, plan_ready):
    """② 降级 reason（含 plan_ready=True）不得把 map agent 原文当事实。

    无论有没有「路线地图已完成」标志，timeout/incomplete/missed/skipped 都只
    能返回固定降级措辞；含具体景点、耗时、链接的 body 一律不进成文上下文。
    """
    body = "具体景点：颐和园、圆明园，步行25分钟，打车18分钟 https://map.example/raw"
    text = map_summary_block(map_reason, body, plan_ready=plan_ready)
    assert "颐和园" not in text
    assert "圆明园" not in text
    assert "步行25分钟" not in text
    assert "打车18分钟" not in text
    assert "map.example/raw" not in text
    # 保留原有降级措辞（且不含 body 的任何具体片段）。
    assert text.startswith("（") or map_reason == "skipped"
    assert "景点建议" in text or "请标注暂缺" in text


def test_map_summary_block_plan_ready_uses_whitelist_not_raw_body():
    """③ plan_ready=True：结果是白名单派生内容，而非原始 map_context。"""
    raw = "原文泄漏检测词 XYZLEAK，步行99分钟，https://evil.example/leak"
    whitelist = itinerary_whitelist(
        _itinerary(),
        unlocated=[{"name": "甲博物馆", "kind": "sight"}],
        candidates=[{"name": "天坛"}],
    )
    text = map_summary_block("ok", raw, plan_ready=True, whitelist=whitelist)
    # 白名单派生内容。
    assert "第1天" in text
    assert "故宫 → 天安门" in text
    assert "未能定位：甲博物馆" in text
    assert "不得新增未列出的景点" in text
    # 原始 body 一点都不留下。
    assert "XYZLEAK" not in text
    assert "步行99分钟" not in text
    assert "evil.example" not in text

    # 不传 whitelist、改传 itinerary 时也要走结构化派生，不能回落到原文。
    text2 = map_summary_block("ok", raw, plan_ready=True, itinerary=_itinerary())
    assert "故宫 → 天安门" in text2
    assert "XYZLEAK" not in text2
    assert "步行99分钟" not in text2

    # 严格读法：即使 map_context 为空，只要给了白名单也必须走 whitelist_summary_block，
    # 不能落到「HTML 已落盘，可依据文件」这种让模型自由参考原文的说明。
    text3 = map_summary_block("ok", "", plan_ready=True, whitelist=whitelist)
    assert "故宫 → 天安门" in text3
    assert "不得新增未列出的景点" in text3
    assert "HTML 已落盘" not in text3

    # 连白名单都没有时，仍由 whitelist_summary_block 给保守说明，绝不返回 body。
    text4 = map_summary_block("ok", "原文泄漏检测词 XYZLEAK", plan_ready=True)
    assert "未提供结构化行程清单" in text4
    assert "XYZLEAK" not in text4


@pytest.mark.parametrize("via_itinerary", [False, True])
def test_map_summary_block_unlocated_candidate_not_written_as_scheduled(via_itinerary):
    """④ 有行程但带未定位/仅候选地点时，候选名不得被写成已排入景点。"""
    unlocated = [{"name": "未名湖", "kind": "sight"}]
    candidates = [{"name": "天坛"}]
    if via_itinerary:
        text = map_summary_block(
            "ok", "原文", plan_ready=True,
            itinerary=_itinerary(), unlocated=unlocated, candidates=candidates,
        )
    else:
        whitelist = itinerary_whitelist(_itinerary(), unlocated=unlocated, candidates=candidates)
        text = map_summary_block("ok", "原文", plan_ready=True, whitelist=whitelist)

    lines = text.splitlines()
    poi_lines = [line for line in lines if line.startswith("第")]
    assert poi_lines, "白名单摘要必须给出按天排入的景点行"
    # 「第N天 …：」行只允许出现真正排入的景点。
    for line in poi_lines:
        assert "天坛" not in line
        assert "未名湖" not in line
    # 候选/未定位只能出现在各自标注的段落里。
    assert any("仅候选、未排入行程" in line and "天坛" in line for line in lines)
    assert any("未能定位" in line and "未名湖" in line for line in lines)
    assert "不得把未定位或仅候选的地点写成已排入行程" in text


# ---------------------------------------------------------------------------
# 缺陷 2：复用票务按父态分流
# ---------------------------------------------------------------------------


def test_classify_ticket_parent_mapping():
    """六态分类口径（质量分支依赖此纯函数，先锁定映射本身）。"""
    assert classify_ticket_parent("ok") == ("ok", "parent_ok")
    assert classify_ticket_parent("skipped") == ("skipped", "parent_skipped")
    for reason in ("unavailable", "missed", "timeout"):
        assert classify_ticket_parent(reason) == ("failed", "parent_failed")
    assert classify_ticket_parent("reused") == ("ok", "parent_reused")
    assert classify_ticket_parent("") == ("unknown", "parent_unknown")


_PARENT_CASES = [
    # parent_reason, 期望 code, 必须出现, 必须不出现
    ("ok", "ticket_reused", ["上次查询结果，未刷新", "余票不是当前余票"], ["余票未知"]),
    ("skipped", None, [], ["沿用上次车次查询", "余票未刷新", "余票未知"]),
    ("unavailable", "ticket_unverified",
     ["上次票务服务不可用或依赖缺失", "本次未重新查询", "余票未知"],
     ["沿用上次车次查询", "余票未刷新"]),
    ("missed", "ticket_unverified",
     ["上次未调度或返回空结果", "本次未重新查询", "余票未知"],
     ["沿用上次车次查询", "余票未刷新"]),
    ("timeout", "ticket_unverified",
     ["上次查票超时", "本次未重新查询", "余票未知"],
     ["沿用上次车次查询", "余票未刷新"]),
    ("reused", "ticket_reused", ["沿用更早一次查询", "余票未刷新"], ["余票未知"]),
]


@pytest.mark.parametrize("parent_reason,expected_code,must,must_not", _PARENT_CASES)
def test_ticket_parent_states_via_collect_issues(parent_reason, expected_code, must, must_not):
    """六态覆盖：reason=reused 且 facts_count=0 时，按父态给对应文案。"""
    issues = _collect(_reused_state(parent_reason))
    if expected_code is None:
        # 父方案无需铁路：复用不产生任何票务 issue，与 collect_issues 的 skipped 一致。
        assert issues == []
        return
    assert len(issues) == 1
    issue = issues[0]
    assert issue["code"] == expected_code
    message = str(issue["message"])
    for word in must:
        assert word in message
    for word in must_not:
        assert word not in message


@pytest.mark.parametrize("parent_reason", ["unavailable", "missed"])
def test_unavailable_missed_never_claim_reused_or_unrefreshed(parent_reason):
    """审查硬要求：父失败两态不得说「沿用上次车次查询」「余票未刷新」，必须说「余票未知」。"""
    issues = _collect(_reused_state(parent_reason))
    assert len(issues) == 1
    message = str(issues[0]["message"])
    assert "沿用上次车次查询" not in message
    assert "余票未刷新" not in message
    assert "余票未知" in message
    assert issues[0]["code"] == "ticket_unverified"


def test_prefilled_parent_class_and_reuse_source_are_honored():
    """pipeline 已塞好 parent_class/reuse_source 时优先复用，不再按 parent_reason 现算。

    parent_reason 为空（现算会得到 parent_unknown →「余票状态未知」），但预置的
    parent_skipped 必须让复用分支返回 []，借此证明预置字段确实被读到。
    """
    state = _reused_state("")
    state["parent_class"] = "skipped"
    state["reuse_source"] = "parent_skipped"
    assert _collect(state) == []


# ---------------------------------------------------------------------------
# ticket_markdown 与 quality 对同一 reuse_source 的核心语义一致（只读 ticket_facts.py）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reuse_source,parent_reason,shared", [
    ("parent_ok", "ok", ["上次查询结果，未刷新", "余票不是当前余票"]),
    ("parent_reused", "reused", ["沿用更早一次查询", "余票未刷新"]),
    ("parent_failed", "unavailable", ["本次未重新查询", "余票未知", "服务不可用或依赖缺失"]),
    ("parent_unknown", "", ["本次未重新查询", "余票状态未知"]),
])
def test_ticket_markdown_and_quality_share_wording(reuse_source, parent_reason, shared):
    """同一 reuse_source 下，票务正文首句与质量告警必须共享核心措辞。

    二者不逐字全等：正文会多带「上次查询失败（原因）」与「余票不是当前余票」等
    后缀，质量侧按审查要求给更细的失败文案。这里只断言共享语义，不放宽成全等。
    """
    queried_at = "2026-11-01 10:00"
    head = ticket_markdown(
        [], reused=True, queried_at=queried_at, reason="reused",
        reuse_source=reuse_source, parent_reason=parent_reason,
    ).splitlines()[0]
    issues = _collect(_reused_state(parent_reason))
    assert len(issues) == 1
    message = str(issues[0]["message"])
    for word in shared:
        assert word in head, f"ticket_markdown 缺少共享措辞：{word}"
        assert word in message, f"quality 告警缺少共享措辞：{word}"


def test_parent_skipped_no_ticket_issue_and_markdown_says_no_rail():
    """parent_skipped：质量侧零 issue，正文侧「无需铁路」，都不伪造余票依据。"""
    assert _collect(_reused_state("skipped")) == []
    head = ticket_markdown(
        [], reused=True, queried_at="2026-11-01 10:00", reason="reused",
        reuse_source="parent_skipped", parent_reason="skipped",
    ).splitlines()[0]
    assert "无需铁路" in head
    assert "余票未刷新" not in head
    assert "沿用上次车次查询" not in head
