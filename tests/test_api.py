"""API 集成测试：草稿生命周期、归因、改稿废弃旧结论、并发改稿竞争、健康检查。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_store():
    store._items.clear()
    yield
    store._items.clear()


def sample_payload():
    return {
        "name": "环网-示例",
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
             "readings": {"S": True, "A": True, "B": True, "C": False, "D": False}},
            {"closed": ["C1", "C2", "C3", "C4", "C5", "C6"],
             "readings": {"S": True, "A": True, "B": True, "C": True, "D": True}},
        ],
    }


def max_payload_all_powered():
    """10 节点、14 电缆、7 轮且每轮全部节点通电的草稿（枚举规模最大）。"""
    nodes = ["S"] + [f"N{i}" for i in range(1, 10)]
    edge_pairs = [
        ("S", "N1"), ("N1", "N2"), ("N2", "N3"), ("N3", "N4"),
        ("N4", "N5"), ("N5", "N6"), ("N6", "N7"), ("N7", "N8"),
        ("N8", "N9"), ("N9", "S"),
        ("S", "N5"), ("N2", "N7"), ("N1", "N4"), ("N6", "N9"),
    ]
    cables = [
        {"name": f"E{i + 1}", "u": u, "v": v, "repair_risk": float((i % 5) + 1)}
        for i, (u, v) in enumerate(edge_pairs)
    ]
    all_closed = [c["name"] for c in cables]
    rounds = [
        {"closed": list(all_closed), "readings": {n: True for n in nodes}}
        for _ in range(7)
    ]
    return {
        "name": "大规模-全通电",
        "nodes": nodes,
        "source": "S",
        "cables": cables,
        "rounds": rounds,
    }


def test_health_endpoints():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    r2 = client.get("/api/health")
    assert r2.status_code == 200


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "永久故障" in r.text


def test_validation_node_count():
    p = sample_payload()
    p["nodes"] = ["S", "A"]  # 少于 5
    r = client.post("/api/drafts", json=p)
    assert r.status_code == 422


def test_validation_cable_endpoint_and_round_count():
    p = sample_payload()
    p["cables"][0]["v"] = "GHOST"
    r = client.post("/api/drafts", json=p)
    assert r.status_code == 422

    p2 = sample_payload()
    p2["rounds"] = p2["rounds"][:1]  # 仅 1 轮
    r2 = client.post("/api/drafts", json=p2)
    assert r2.status_code == 422


def test_full_diagnosis_flow_fault_c3():
    r = client.post("/api/drafts", json=sample_payload())
    assert r.status_code == 200, r.text
    draft_id = r.json()["id"]
    assert r.json()["revision"] == 1
    assert r.json()["diagnosis"] is None

    # 未归因时查询结论 -> 409
    assert client.get(f"/api/drafts/{draft_id}/diagnosis").status_code == 409

    d = client.post(f"/api/drafts/{draft_id}/diagnose").json()
    assert d["feasible"] is True
    assert [f["name"] for f in d["fault_cables"]] == ["C3"]
    assert d["fault_count"] == 1
    assert d["total_repair_risk"] == 1
    # {C3} 与 {C3,C6} 均可行（C6 故障不影响两轮读数），
    # 但故障数最少的 {C3} 唯一胜出。
    assert d["candidate_count"] == 2
    assert len(d["rounds"]) == 2
    assert all(rc["matches"] for rc in d["rounds"])
    # 每轮可达传感器与读数明细
    assert d["rounds"][0]["reachable"] == ["A", "B", "S"]
    assert d["rounds"][1]["reachable"] == ["A", "B", "C", "D", "S"]

    # 结论已持久化
    got = client.get(f"/api/drafts/{draft_id}/diagnosis")
    assert got.status_code == 200
    assert got.json()["fault_count"] == 1


def test_inconsistent_payload_reports_no_common_explanation():
    p = sample_payload()
    p["rounds"] = [
        {"closed": ["C1", "C2"],
         "readings": {"S": True, "A": True, "B": True, "C": False, "D": False}},
        {"closed": ["C1", "C2"],
         "readings": {"S": True, "A": False, "B": False, "C": False, "D": False}},
    ]
    r = client.post("/api/diagnose", json=p)
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is False
    assert body["fault_cables"] == []
    assert "不能由同一组永久故障" in body["message"]
    # 逐轮明细中标出了冲突
    assert body["rounds"][0]["matches"] is True
    assert body["rounds"][1]["matches"] is False
    assert body["rounds"][1]["mismatch"]["powered_but_unreachable"] == []
    assert set(body["rounds"][1]["mismatch"]["unpowered_but_reachable"]) == {"A", "B"}


def test_draft_update_clears_old_diagnosis_and_bumps_revision():
    # 1) 建草稿并归因（C3 故障）
    p = sample_payload()
    draft_id = client.post("/api/drafts", json=p).json()["id"]
    d1 = client.post(f"/api/drafts/{draft_id}/diagnose").json()
    assert d1["feasible"] and d1["fault_cables"][0]["name"] == "C3"

    # 2) 修改草稿为一组与第 1 轮物理矛盾的读数：
    #    第 1 轮 A 通电且 A 仅经 C1 接入 => C1 必然完好；
    #    第 2 轮 A 断电却 B 通电（B 可经 C6），逼 C1 永久故障。
    p["rounds"][1]["readings"] = {
        "S": True, "A": False, "B": True, "C": False, "D": False
    }
    upd = client.put(f"/api/drafts/{draft_id}", json=p)
    assert upd.status_code == 200
    assert upd.json()["revision"] == 2
    # 旧结论不得保留
    assert upd.json()["diagnosis"] is None
    assert client.get(f"/api/drafts/{draft_id}/diagnosis").status_code == 409

    # 3) 重新归因 -> 明确无共同解释
    d2 = client.post(f"/api/drafts/{draft_id}/diagnose").json()
    assert d2["feasible"] is False
    assert d2["revision"] == 2


def test_update_unknown_draft_404():
    assert client.put("/api/drafts/nope", json=sample_payload()).status_code == 404
    assert client.post("/api/drafts/nope/diagnose").status_code == 404


def test_extra_fields_rejected():
    p = sample_payload()
    p["bogus"] = 1
    r = client.post("/api/drafts", json=p)
    assert r.status_code == 422


def test_concurrent_diagnose_discarded_after_mid_flight_update(monkeypatch):
    """并发改稿场景：r1 上在途的归因，在改稿为 r2 后不得成为保存/页面结果。

    场景（对应缺陷报告）：对 r1 并发发起多次归因；这些请求尚未完成时
    将草稿更新为内容不同的 r2，且不再为 r2 发起归因。此后：
    - 在途归因一律作废（409），不得保存为 r2 的结论；
    - 读取归因结果必须返回 409，草稿不得携带 r1 的旧结论；
    - 重新对 r2 归因后恢复正常。
    """
    from app import routers

    draft_id = client.post("/api/drafts", json=max_payload_all_powered()).json()["id"]

    real_diagnose = routers.diagnose
    workers = 3
    entered = threading.Barrier(workers + 1)
    release = threading.Event()

    def gated_diagnose(network):
        # 全部并发归因都基于 r1 进入计算后，阻塞到改稿完成再返回结果
        entered.wait(timeout=15)
        assert release.wait(timeout=15), "改稿未在预期时间内完成"
        return real_diagnose(network)

    monkeypatch.setattr(routers, "diagnose", gated_diagnose)

    outcomes: list[tuple[int, dict]] = []

    def worker():
        r = client.post(f"/api/drafts/{draft_id}/diagnose")
        outcomes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()

    # 等待所有归因请求基于 r1 开始计算（快照已在 PUT 之前完成）
    entered.wait(timeout=15)

    # 在途期间将草稿更新为内容不同的 r2，且不再为 r2 发起归因
    p2 = max_payload_all_powered()
    p2["rounds"][0]["readings"]["N9"] = False
    upd = client.put(f"/api/drafts/{draft_id}", json=p2)
    assert upd.status_code == 200
    assert upd.json()["revision"] == 2
    assert upd.json()["diagnosis"] is None

    release.set()
    for t in threads:
        t.join(timeout=30)
    assert len(outcomes) == workers

    # 在途归因全部作废：不得返回可展示的旧修订结论
    assert [s for s, _ in outcomes] == [409] * workers
    for _, body in outcomes:
        assert "作废" in body["detail"]

    # 未对 r2 重新归因：读取必须 409，草稿不得携带 r1 的结论
    assert client.get(f"/api/drafts/{draft_id}/diagnosis").status_code == 409
    rec = client.get(f"/api/drafts/{draft_id}").json()
    assert rec["revision"] == 2
    assert rec["diagnosis"] is None
    assert rec["diagnosis_revision"] is None

    # 恢复真实引擎后，对 r2 重新归因可正常保存与读取
    monkeypatch.setattr(routers, "diagnose", real_diagnose)
    d = client.post(f"/api/drafts/{draft_id}/diagnose")
    assert d.status_code == 200
    assert d.json()["revision"] == 2
    got = client.get(f"/api/drafts/{draft_id}/diagnosis")
    assert got.status_code == 200
    assert got.json()["revision"] == 2
