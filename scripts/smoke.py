"""API 业务冒烟：对运行中的服务端发起完整业务链路校验。

由 Compose 的一次性 verify 服务调用，退出码非 0 即验收失败。
覆盖：健康检查（Web + API）→ 建草稿 → 归因（锁定 C3）→ 结论查询 →
改稿清除旧结论 → 无共同解释场景的明确提示。
"""

from __future__ import annotations

import json
import os
import sys
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


def main() -> int:
    print(f"[smoke] 目标服务：{BASE}")

    print("[1/6] 健康检查（Web 与 API）")
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

    print("[2/6] 创建草稿")
    s, rec = call("POST", "/api/drafts", payload())
    check(s == 200, "草稿创建成功")
    check(rec["revision"] == 1 and rec["diagnosis"] is None,
          "初始修订号为 1 且无旧结论")
    draft_id = rec["id"]

    print("[3/6] 发起归因：多轮联合锁定 C3")
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

    print("[4/6] 结论持久化查询")
    s, got = call("GET", f"/api/drafts/{draft_id}/diagnosis")
    check(s == 200 and got["fault_count"] == 1, "GET 结论与归因一致")

    print("[5/6] 修改草稿后旧结论必须清除")
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

    print("[6/6] 无状态接口同样可用")
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
