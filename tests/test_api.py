"""API 集成测试：草稿生命周期、归因、改稿废弃旧结论、健康检查。"""

from __future__ import annotations

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
