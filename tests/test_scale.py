"""规模上限测试：10 节点、14 电缆、7 轮时全量枚举仍快速且正确。"""

from __future__ import annotations

import time

from app.engine import Cable, Network, Round, diagnose


def test_max_size_enumeration_is_fast():
    # 10 个节点：S 与 N1..N9；14 条电缆构成环 + 弦。
    nodes = ("S",) + tuple(f"N{i}" for i in range(1, 10))
    edge_pairs = [
        ("S", "N1"), ("N1", "N2"), ("N2", "N3"), ("N3", "N4"),
        ("N4", "N5"), ("N5", "N6"), ("N6", "N7"), ("N7", "N8"),
        ("N8", "N9"), ("N9", "S"),
        ("S", "N5"), ("N2", "N7"), ("N1", "N4"), ("N6", "N9"),
    ]
    cables = tuple(
        Cable(seq=i + 1, name=f"E{i + 1}", u=u, v=v, repair_risk=float((i % 5) + 1))
        for i, (u, v) in enumerate(edge_pairs)
    )
    name = {i + 1: f"E{i + 1}" for i in range(14)}

    # 7 轮：偶数轮全闭合（全网通电），奇数轮只闭合环的前若干条，
    # 其中 E3（N2-N3）永久故障时各轮读数自洽。
    rounds = []
    all_closed = [name[i] for i in range(1, 15)]
    for r in range(1, 8):
        if r % 2 == 0:
            closed = all_closed
            powered = set(nodes)
        else:
            # 只闭合 S-N1-N2；E3 永久故障，N3 及以后不可达
            closed = ["E1", "E2", "E3"]
            powered = {"S", "N1", "N2"}
        rounds.append(Round(index=r, closed=frozenset(closed), powered=frozenset(powered)))

    net = Network(nodes=nodes, source="S", cables=cables, rounds=tuple(rounds))

    start = time.perf_counter()
    d = diagnose(net)
    elapsed = time.perf_counter() - start

    assert d.feasible
    assert "E3" in d.fault_names
    # 全量枚举 2^14 = 16384 个组合
    assert d.evaluated == 16384
    assert d.truncated is False
    # 7 轮明细全部吻合
    assert len(d.rounds) == 7
    assert all(rc.matches for rc in d.rounds)
    # 性能上限：纯 Python 全量枚举应在 2 秒内完成
    assert elapsed < 2.0, f"枚举耗时 {elapsed:.3f}s 超出预期"
