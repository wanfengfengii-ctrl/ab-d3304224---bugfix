"""存储层单元测试：修订号快照与条件保存（并发改稿竞争的根基）。"""

from __future__ import annotations

from app.schemas import NetworkPayload
from app.storage import DraftStore


def make_payload(name: str = "存储测试网") -> NetworkPayload:
    return NetworkPayload(
        **{
            "name": name,
            "nodes": ["S", "A", "B", "C", "D"],
            "source": "S",
            "cables": [
                {"name": "C1", "u": "S", "v": "A", "repair_risk": 3},
                {"name": "C2", "u": "A", "v": "B", "repair_risk": 2},
                {"name": "C3", "u": "B", "v": "C", "repair_risk": 1},
                {"name": "C4", "u": "C", "v": "D", "repair_risk": 4},
                {"name": "C5", "u": "D", "v": "S", "repair_risk": 5},
                {"name": "C6", "u": "S", "v": "B", "repair_risk": 2},
            ],
            "rounds": [
                {"closed": ["C1", "C2", "C3"],
                 "readings": {"S": True, "A": True, "B": True,
                              "C": False, "D": False}},
                {"closed": ["C1", "C2", "C3", "C4", "C5", "C6"],
                 "readings": {"S": True, "A": True, "B": True,
                              "C": True, "D": True}},
            ],
        }
    )


def test_snapshot_is_consistent_and_immune_to_later_update():
    s = DraftStore()
    rec = s.create(make_payload())

    payload, revision = s.get_snapshot(rec.id)
    assert revision == 1

    # 改稿不影响此前取得的快照
    s.update(rec.id, make_payload("改稿后"))
    assert (payload, revision) == (payload, 1)
    assert s.get_snapshot(rec.id)[1] == 2


def test_attach_diagnosis_rejected_after_revision_bump():
    """修订号变化后，旧修订的在途结论不得保存为新修订的结论。"""
    s = DraftStore()
    rec = s.create(make_payload())

    # 当前修订的归因可正常保存
    assert s.attach_diagnosis(rec.id, {"revision": 1}, revision=1) is not None
    assert s.get(rec.id).diagnosis == {"revision": 1}
    assert s.get(rec.id).diagnosis_revision == 1

    # 改稿：修订号 +1，旧结论清除
    s.update(rec.id, make_payload("改稿后"))
    assert s.get(rec.id).diagnosis is None

    # 在途的旧修订结论必须被丢弃
    assert s.attach_diagnosis(rec.id, {"revision": 1}, revision=1) is None
    assert s.get(rec.id).diagnosis is None
    assert s.get(rec.id).diagnosis_revision is None

    # 基于新修订重新归因可正常保存
    assert s.attach_diagnosis(rec.id, {"revision": 2}, revision=2) is not None
    assert s.get(rec.id).diagnosis == {"revision": 2}
    assert s.get(rec.id).diagnosis_revision == 2


def test_attach_diagnosis_unknown_draft():
    s = DraftStore()
    assert s.attach_diagnosis("nope", {}, revision=1) is None
    assert s.get_snapshot("nope") is None
