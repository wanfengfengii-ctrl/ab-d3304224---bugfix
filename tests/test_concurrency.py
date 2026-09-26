"""并发回归：归因在途时改稿，旧修订结论不得成为新修订结果。

场景（与缺陷报告一致）：
* 创建含 10 个节点、14 条电缆、7 轮记录且每轮全部节点均显示通电的 r1；
* 对 r1 并发发起多次归因，在这些请求尚未完成时把同一草稿 PUT 为 r2，
  且不再为 r2 发起归因；
* 此后读取归因结果必须始终 409；在途归因即使随后完成也不得写回 r2。
"""

from __future__ import annotations

import threading

import app.routers as routers
from app.engine import diagnose as real_diagnose
from app.storage import store
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


NODES = ["S"] + [f"N{i}" for i in range(1, 10)]
EDGE_PAIRS = [
    ("S", "N1"), ("N1", "N2"), ("N2", "N3"), ("N3", "N4"),
    ("N4", "N5"), ("N5", "N6"), ("N6", "N7"), ("N7", "N8"),
    ("N8", "N9"), ("N9", "S"),
    ("S", "N5"), ("N2", "N7"), ("N1", "N4"), ("N6", "N9"),
]
CABLES = [
    {"name": f"E{i + 1}", "u": u, "v": v, "repair_risk": (i % 5) + 1}
    for i, (u, v) in enumerate(EDGE_PAIRS)
]
ALL_CABLES = [c["name"] for c in CABLES]
ALL_POWERED = {n: True for n in NODES}


def r1_payload() -> dict:
    """r1：10 节点 / 14 电缆 / 7 轮，每轮全部电缆闭合、全部节点通电。"""
    return {
        "name": "并发回归-r1",
        "nodes": list(NODES),
        "source": "S",
        "cables": [dict(c) for c in CABLES],
        "rounds": [
            {"closed": list(ALL_CABLES), "readings": dict(ALL_POWERED)}
            for _ in range(7)
        ],
    }


def r2_payload() -> dict:
    """r2：内容不同的修订——第 1 轮 N9 读数改为断电，其余同 r1。"""
    p = r1_payload()
    p["name"] = "并发回归-r2"
    p["rounds"][0]["readings"]["N9"] = False
    return p


def test_concurrent_r1_diagnoses_never_become_r2_result(monkeypatch):
    store._items.clear()
    try:
        draft_id = client.post("/api/drafts", json=r1_payload()).json()["id"]

        # 让并发归因在引擎计算阶段阻塞，确定性地制造「在途请求」窗口。
        # 3 个归因线程 + 主线程共同参与 barrier：主线程被放行时，
        # 三次归因必然都已处于在途计算中。
        entered = threading.Barrier(4)
        release = threading.Event()

        def paused_diagnose(network):
            entered.wait(timeout=10)
            assert release.wait(timeout=10)
            return real_diagnose(network)

        monkeypatch.setattr(routers, "diagnose", paused_diagnose)

        results: list[tuple[int, dict]] = []

        def worker() -> None:
            r = client.post(f"/api/drafts/{draft_id}/diagnose")
            results.append((r.status_code, r.json()))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        entered.wait(timeout=10)  # 三次归因全部进入在途计算

        # 在途请求尚未完成时改稿为 r2，且之后不再对 r2 发起归因
        upd = client.put(f"/api/drafts/{draft_id}", json=r2_payload())
        assert upd.status_code == 200
        assert upd.json()["revision"] == 2
        assert upd.json()["diagnosis"] is None

        # 改稿成功后、在途请求完成前：读取必须 409，而不是 r1 的结论
        assert client.get(f"/api/drafts/{draft_id}/diagnosis").status_code == 409

        # 让 r1 时代开始的在途归因随后完成
        release.set()
        for t in threads:
            t.join(timeout=10)

        # 在途归因全部因修订过期被拒绝，不得写回
        assert len(results) == 3
        for status, body in results:
            assert status == 409
            assert "修订" in str(body["detail"])

        # 此后页面/查询端依旧读不到任何 r1 结论
        got = client.get(f"/api/drafts/{draft_id}/diagnosis")
        assert got.status_code == 409

        rec = client.get(f"/api/drafts/{draft_id}").json()
        assert rec["revision"] == 2
        assert rec["diagnosis"] is None
        # 保存的确实是 r2 内容，而非 r1
        assert rec["payload"]["rounds"][0]["readings"]["N9"] is False
    finally:
        store._items.clear()


def test_normal_draft_diagnosis_still_saved_after_concurrent_window(monkeypatch):
    """对照：无改稿时归因正常完成，同修订结论可被读取（200）。"""
    store._items.clear()
    try:
        draft_id = client.post("/api/drafts", json=r1_payload()).json()["id"]

        gate = threading.Event()

        def once_paused(network):
            gate.wait(timeout=10)
            return real_diagnose(network)

        monkeypatch.setattr(routers, "diagnose", once_paused)

        outcome = {}

        def worker() -> None:
            r = client.post(f"/api/drafts/{draft_id}/diagnose")
            outcome["status"] = r.status_code
            outcome["body"] = r.json()

        t = threading.Thread(target=worker)
        t.start()
        # 在途期间读取仍为 409（尚无结论），但草稿仍停留在 r1
        assert client.get(f"/api/drafts/{draft_id}/diagnosis").status_code == 409
        gate.set()
        t.join(timeout=10)

        assert outcome["status"] == 200
        assert outcome["body"]["revision"] == 1
        got = client.get(f"/api/drafts/{draft_id}/diagnosis")
        assert got.status_code == 200
        assert got.json()["revision"] == 1
    finally:
        store._items.clear()
