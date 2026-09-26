"""API 业务冒烟：对运行中的服务端发起完整业务链路校验。

由 Compose 的一次性 verify 服务调用，退出码非 0 即验收失败。
覆盖：健康检查（Web + API）→ 建草稿 → 归因（锁定 C3）→ 结论查询 →
改稿清除旧结论 → 无共同解释场景的明确提示 →
并发归因在途时改稿（旧修订结论不得成为新修订结果）→ 无状态归因。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

failures: list[str] = []


def call(method: str, path: str, body: dict | None = None, expect: int = 200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        payload = json.loads(e.read().decode())
    if status != expect:
        failures.append(f"{method} {path} 期望 {expect}，实际 {status}：{payload}")
    return status, payload


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures.append(msg)


def payload(round2_readings: dict | None = None) -> dict:
    return {
        "name": "冒烟观测网",
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
             "readings": round2_readings
             or {"S": True, "A": True, "B": True, "C": True, "D": True}},
        ],
    }


_CONCURRENT_EDGES = [
    ("S", "N1"), ("N1", "N2"), ("N2", "N3"), ("N3", "N4"),
    ("N4", "N5"), ("N5", "N6"), ("N6", "N7"), ("N7", "N8"),
    ("N8", "N9"), ("N9", "S"),
    ("S", "N5"), ("N2", "N7"), ("N1", "N4"), ("N6", "N9"),
]


def concurrent_r1() -> dict:
    """规模上限草稿 r1：10 节点、14 电缆、7 轮、每轮全部节点通电。"""
    nodes = ["S"] + [f"N{i}" for i in range(1, 10)]
    cables = [
        {"name": f"E{i + 1}", "u": u, "v": v, "repair_risk": (i % 5) + 1}
        for i, (u, v) in enumerate(_CONCURRENT_EDGES)
    ]
    return {
        "name": "并发冒烟-r1",
        "nodes": nodes,
        "source": "S",
        "cables": cables,
        "rounds": [
            {"closed": [c["name"] for c in cables],
             "readings": {n: True for n in nodes}}
            for _ in range(7)
        ],
    }


def concurrent_r2() -> dict:
    """内容不同的 r2：第 1 轮 N9 读数改为断电（其余同 r1），且不再归因。"""
    changed = concurrent_r1()
    changed["name"] = "并发冒烟-r2"
    changed["rounds"][0]["readings"]["N9"] = False
    return changed


def raw_call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """与 call 相同，但不登记失败（并发归因可能 200 或 409，均属预期）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"error": str(e)}


def main() -> int:
    print(f"[smoke] 目标服务：{BASE}")

    print("[1/7] 健康检查（Web 与 API）")
    s, web = call("GET", "/health")
    check(s == 200 and web["status"] == "ok", "GET /health 返回 ok")
    s, api = call("GET", "/api/health")
    check(s == 200 and api["status"] == "ok", "GET /api/health 返回 ok")
    try:
        with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
            html = resp.read().decode()
            page_ok = resp.status == 200 and "永久故障" in html
    except Exception:
        page_ok = False
    check(page_ok, "GET / 返回录入页面")

    print("[2/7] 创建草稿")
    s, rec = call("POST", "/api/drafts", payload())
    check(s == 200, "草稿创建成功")
    check(rec["revision"] == 1 and rec["diagnosis"] is None,
          "初始修订号为 1 且无旧结论")
    draft_id = rec["id"]

    print("[3/7] 发起归因：多轮联合锁定 C3")
    s, d = call("POST", f"/api/drafts/{draft_id}/diagnose")
    check(s == 200, "归因接口 200")
    check(d["feasible"] is True, "存在共同解释")
    names = [f["name"] for f in d["fault_cables"]]
    check(names == ["C3"], f"故障电缆为 ['C3']，实际 {names}")
    check(d["fault_count"] == 1, "故障数量 = 1（最少）")
    check(d["total_repair_risk"] == 1, "修复风险和 = 1（最低）")
    check(d["candidate_count"] >= 1, f"可行组合数 {d['candidate_count']} ≥ 1")
    check(len(d["rounds"]) == 2 and all(r["matches"] for r in d["rounds"]),
          "两轮可达性与读数全部吻合")
    reach1 = d["rounds"][0]["reachable"]
    check(set(reach1) == {"S", "A", "B"}, f"第 1 轮可达 {{S,A,B}}，实际 {reach1}")
    reach2 = d["rounds"][1]["reachable"]
    check(set(reach2) == {"S", "A", "B", "C", "D"},
          f"第 2 轮环网替代通路使全网可达，实际 {reach2}")

    print("[4/7] 结论持久化查询")
    s, got = call("GET", f"/api/drafts/{draft_id}/diagnosis")
    check(s == 200 and got["fault_count"] == 1, "GET 结论与归因一致")

    print("[5/7] 修改草稿后旧结论必须清除")
    changed = payload(round2_readings={
        "S": True, "A": False, "B": True, "C": False, "D": False})
    s, upd = call("PUT", f"/api/drafts/{draft_id}", changed)
    check(s == 200 and upd["revision"] == 2, "修订号递增到 2")
    check(upd["diagnosis"] is None, "旧归因结论已随改稿废弃")
    s, _ = call("GET", f"/api/drafts/{draft_id}/diagnosis", expect=409)
    check(s == 409, "未重新归因前查询结论返回 409")
    s, d2 = call("POST", f"/api/drafts/{draft_id}/diagnose")
    check(s == 200 and d2["feasible"] is False,
          "矛盾读数归因结果为不可共同解释")
    check("不能由同一组永久故障" in d2["message"],
          "明确提示读数不能由同一组永久故障同时解释")

    print("[6/7] 并发归因在途时改稿：r1 旧结论不得成为 r2 结果")
    cid = call("POST", "/api/drafts", concurrent_r1())[1]["id"]

    concurrent: list[tuple[int, dict]] = []
    concurrent_lock = threading.Lock()
    # 让每个线程错峰发起，增加与改稿请求真正重叠的概率
    barrier = threading.Barrier(4)

    def diagnose_r1() -> None:
        barrier.wait()
        status, body = raw_call("POST", f"/api/drafts/{cid}/diagnose")
        with concurrent_lock:
            concurrent.append((status, body))

    workers = [threading.Thread(target=diagnose_r1) for _ in range(3)]
    for w in workers:
        w.start()
    barrier.wait()
    # 在归因请求飞行期间（规模上限网络单次要数百毫秒）改稿为 r2
    time.sleep(0.05)
    s, cupd = call("PUT", f"/api/drafts/{cid}", concurrent_r2())
    check(s == 200 and cupd["revision"] == 2, "并发改稿成功，修订号递增到 2")
    check(cupd["diagnosis"] is None, "改稿响应不含任何旧结论")
    for w in workers:
        w.join(timeout=30)

    statuses = sorted(s for s, _ in concurrent)
    check(all(s in (200, 409) for s in statuses),
          f"在途归因只可能 200（改稿前完成）或 409（改稿后作废），实际 {statuses}")
    s, _ = call("GET", f"/api/drafts/{cid}/diagnosis", expect=409)
    check(s == 409, "未对 r2 重新归因，读取结论返回 409")
    time.sleep(0.3)  # 等待最后一批在途归因彻底完成
    s, cgot = call("GET", f"/api/drafts/{cid}/diagnosis", expect=409)
    check(s == 409, "在途归因随后完成后，r1 结论仍未写回（依旧 409）")
    s, crec = call("GET", f"/api/drafts/{cid}")
    check(
        s == 200 and crec["diagnosis"] is None
        and crec["payload"]["rounds"][0]["readings"]["N9"] is False,
        "草稿当前为 r2 内容，且保存结论为空（无 r1 结论、无 r2 修订标识）",
    )

    print("[7/7] 无状态接口同样可用")
    s, d3 = call("POST", "/api/diagnose", payload())
    check(s == 200 and [f["name"] for f in d3["fault_cables"]] == ["C3"],
          "POST /api/diagnose 直接归因正确")

    if failures:
        print(f"\n[smoke] 失败 {len(failures)} 项：")
        for f in failures:
            print("  - " + f)
        return 1
    print("\n[smoke] 全部业务冒烟通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
