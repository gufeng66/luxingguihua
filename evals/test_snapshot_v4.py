"""旧快照归一化：v3 及更早缺抵离时刻、request_context、适用性标记。"""

from __future__ import annotations

from uuid import uuid4

from planner.summary import SNAPSHOT_VERSION, degraded_plan, load_snapshot, load_snapshot_v4, save_snapshot


def _raw_text() -> list[dict]:
    return [
        {
            "type": "text",
            "text": "\n".join(
                [
                    "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                    "G1813 新乡东(telecode:EGF) -> 上海虹桥(telecode:AOH) 09:35 -> 15:27 历时：05:52",
                    "- 二等座: 有票 493元",
                ]
            ),
        }
    ]


def _legacy_snapshot(**changes) -> dict:
    payload = {
        "plan_id": str(uuid4()),
        "parent_plan_id": None,
        "query": "从新乡坐高铁去上海",
        "schema_version": 3,
        "slots": {"origin": "新乡", "destination": "上海", "date": "2026-11-07", "days": 1},
        "ticket_reason": "ok",
        "answer": degraded_plan("skipped", []),
        "ticket_raw": [
            {
                "id": "raw-3",
                "tool": "get-tickets",
                "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "上海"},
                "payload": _raw_text(),
                "queried_at": "2026-11-03T00:00:00+00:00",
            }
        ],
        # 旧版本解析器丢了 depart/arrive，也没有 applied。
        "ticket_facts": [
            {
                "kind": "train",
                "status": "verified",
                "source_id": "raw-3",
                "value": {
                    "train_no": "G1813",
                    "date": "2026-11-07",
                    "origin": "新乡东",
                    "destination": "上海虹桥",
                    "seat": "二等座",
                    "price": 493.0,
                    "availability": "有票",
                },
            }
        ],
        "itinerary": {"days": [{"day": 1, "date": "2026-11-07", "items": [{"name": "外滩", "role": "visit"}], "legs": []}]},
    }
    payload.update(changes)
    return payload


def test_legacy_snapshot_refills_times_from_raw():
    """原文还在，就把丢掉的抵离时刻补回来；补不出来保持安静，不猜。"""
    snapshot = load_snapshot_v4(_legacy_snapshot())
    assert snapshot is not None
    assert snapshot["schema_version"] == SNAPSHOT_VERSION
    assert snapshot["legacy"] is True
    assert snapshot["legacy_filled"] == 1
    assert snapshot["legacy_unresolved"] == 0
    value = snapshot["ticket_facts"][0]["value"]
    assert value["depart"] == "09:35"
    assert value["arrive"] == "15:27"
    # 旧快照没有适用性判定，只能标 None（未知），不能当成已核实。
    assert value["applied"] is None
    assert snapshot["request_context"] == {}


def test_legacy_snapshot_keeps_unknown_when_raw_is_gone():
    snapshot = load_snapshot_v4(_legacy_snapshot(ticket_raw=[]))
    assert snapshot is not None
    assert snapshot["legacy_filled"] == 0
    assert snapshot["legacy_unresolved"] == 1
    value = snapshot["ticket_facts"][0]["value"]
    assert "depart" not in value


def test_current_snapshot_is_not_marked_legacy():
    raw = _legacy_snapshot(schema_version=4, request_context={"party_size": 2})
    snapshot = load_snapshot_v4(raw)
    assert snapshot is not None
    assert snapshot["legacy"] is False
    assert snapshot["request_context"] == {"party_size": 2}
    # v4 快照原样返回，不做任何回填。
    assert snapshot["ticket_facts"] == raw["ticket_facts"]


def test_load_snapshot_normalizes_through_v4():
    payload = _legacy_snapshot()
    save_snapshot(payload)
    loaded = load_snapshot(payload["plan_id"])
    assert loaded is not None
    assert loaded["legacy"] is True
    assert loaded["ticket_facts"][0]["value"]["arrive"] == "15:27"


def _train_text(train: str, origin: str, dest: str, depart: str, arrive: str) -> list[dict]:
    return [
        {
            "type": "text",
            "text": "\n".join(
                [
                    "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时",
                    f"{train} {origin}(telecode:AAA) -> {dest}(telecode:BBB) {depart} -> {arrive} 历时：01:10",
                    "- 二等座: 有票 120元",
                ]
            ),
        }
    ]


def test_clock_index_does_not_mix_stations_of_same_train():
    """同一趟车在不同站发车时刻不同：只按车次+座席匹配会把两站的时刻串起来。"""
    from planner.summary import _clock_for, _clock_index

    records = [
        {
            "id": "raw-a",
            "tool": "get-tickets",
            "arguments": {"date": "2026-11-07", "fromStation": "新乡南", "toStation": "郑州东"},
            "payload": _train_text("G3251", "新乡南", "郑州东", "11:04", "12:14"),
            "queried_at": "2026-11-03T00:00:00+00:00",
        },
        {
            "id": "raw-b",
            "tool": "get-tickets",
            "arguments": {"date": "2026-11-07", "fromStation": "新乡东", "toStation": "郑州东"},
            "payload": _train_text("G3251", "新乡东", "郑州东", "10:46", "11:56"),
            "queried_at": "2026-11-03T00:00:00+00:00",
        },
    ]
    index = _clock_index(records)

    south = _clock_for(
        index,
        {"train_no": "G3251", "date": "2026-11-07", "seat": "二等座",
         "origin": "新乡南", "destination": "郑州东"},
    )
    east = _clock_for(
        index,
        {"train_no": "G3251", "date": "2026-11-07", "seat": "二等座",
         "origin": "新乡东", "destination": "郑州东"},
    )
    assert south is not None and south["depart"] == "11:04"
    assert east is not None and east["depart"] == "10:46"

    # 事实带站点但原文里没有这趟车这个区间：宁可不补，也不拿别的站的时刻顶替。
    missing = _clock_for(
        index,
        {"train_no": "G3251", "date": "2026-11-07", "seat": "二等座",
         "origin": "安阳东", "destination": "郑州东"},
    )
    assert missing is None
