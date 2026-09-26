"""引擎层单元测试。"""

from __future__ import annotations

from app.engine import Cable, Network, Round, diagnose, reachable_nodes


def make_network(cables_def, rounds_def, nodes=("S", "A", "B", "C", "D"), source="S"):
    cables = tuple(
        Cable(seq=i + 1, name=n, u=u, v=v, repair_risk=r)
        for i, (n, u, v, r) in enumerate(cables_def)
    )
    rounds = tuple(
        Round(index=i + 1, closed=frozenset(closed), powered=frozenset(powered))
        for i, (closed, powered) in enumerate(rounds_def)
    )
    return Network(nodes=tuple(nodes), source=source, cables=cables, rounds=rounds)


RING = [
    ("C1", "S", "A", 3),
    ("C2", "A", "B", 2),
    ("C3", "B", "C", 1),
    ("C4", "C", "D", 4),
    ("C5", "D", "S", 5),
    ("C6", "S", "B", 2),
]


def test_reachable_basic():
    net = make_network(RING, [(["C1", "C2"], ["S"])])
    reach = reachable_nodes(net.nodes, net.cables, ["C1", "C2"], "S")
    assert reach == {"S", "A", "B"}


def test_zero_fault_solution_preferred():
    net = make_network(
        RING,
        [
            (["C1", "C2"], ["S", "A", "B"]),
            (["C1", "C2", "C3", "C4", "C5"], ["S", "A", "B", "C", "D"]),
        ],
    )
    d = diagnose(net)
    assert d.feasible
    assert d.fault_count == 0
    assert d.fault_names == ()
    assert d.total_repair_risk == 0


def test_single_fault_identified_only_by_joint_rounds():
    # 第 1 轮单独看像 C3 没闭合；第 2 轮全网通电又像毫无故障。
    # 只有两轮联合才能锁定 C3 永久故障（环网替代通路所致）。
    net = make_network(
        RING,
        [
            (["C1", "C2", "C3"], ["S", "A", "B"]),
            (["C1", "C2", "C3", "C4", "C5", "C6"], ["S", "A", "B", "C", "D"]),
        ],
    )
    d = diagnose(net)
    assert d.feasible
    assert d.fault_names == ("C3",)
    assert d.fault_seqs == (3,)
    assert d.total_repair_risk == 1
    r1, r2 = d.rounds
    assert r1.matches and r2.matches
    assert set(r1.reachable) == {"S", "A", "B"}
    assert set(r2.reachable) == {"S", "A", "B", "C", "D"}
    assert r1.expected_unpowered == ("C", "D")


def test_fewer_faults_outranks_everything_else():
    # {C3} 与 {C3, C4} 都可行（C4 断开不影响第 2 轮，因 C 可经 B-C6-S-C5-D 到 D），
    # 但单故障优先，与风险无关。
    net = make_network(
        RING,
        [
            (["C1", "C2", "C3"], ["S", "A", "B"]),
            (["C1", "C2", "C3", "C5", "C6"], ["S", "A", "B", "D"]),
        ],
    )
    d = diagnose(net)
    assert d.feasible
    assert d.fault_names == ("C3",)


def test_inconsistent_readings_have_no_common_explanation():
    # 两轮闭合集合完全相同，读数却互相矛盾：
    # 永久故障集合在两轮中不变，可达性必然相同，不可能同时吻合。
    net = make_network(
        RING,
        [
            (["C1", "C2"], ["S", "A", "B"]),
            (["C1", "C2"], ["S"]),
        ],
    )
    d = diagnose(net)
    assert not d.feasible
    assert d.fault_names == ()
    assert d.fault_count == 0
    assert "不能由同一组永久故障" in d.message
    # 6 条电缆 => 枚举全部 2^6 = 64 个组合
    assert d.evaluated == 64
    assert d.candidate_count == 0


def test_full_enumeration_counts_all_feasible_candidates():
    net = make_network(
        RING,
        [
            (["C1", "C2"], ["S", "A", "B"]),
            (["C1", "C2"], ["S", "A", "B"]),
        ],
    )
    d = diagnose(net)
    assert d.feasible
    assert d.evaluated == 64
    # C1、C2 必须完好；C3~C6 本轮均未闭合，是否故障不影响可达性，
    # 故 2^4 = 16 个故障组合全部可行。
    assert d.candidate_count == 16
    assert d.fault_count == 0


def test_ordering_keys():
    """直接验证三级择优键：数量 -> 风险和 -> 编号序列字典序。"""
    from app.engine import _order_key

    net = make_network(
        [
            ("C1", "S", "A", 5),
            ("C2", "S", "A", 1),
            ("C3", "A", "B", 8),
            ("C4", "A", "B", 4),
            ("C5", "B", "C", 0),
            ("C6", "C", "D", 0),
        ],
        [([], ["S"]), ([], ["S"])],
    )
    k_single = _order_key(net, (0,))          # {C1}：1 个故障
    k_double = _order_key(net, (1, 2))        # {C2,C3}：2 个故障
    assert k_single[0] < k_double[0]          # 故障数量是第一关键字

    # 同数量：风险和低者优先（C2=1 < C1=5）
    assert _order_key(net, (1,))[1] < _order_key(net, (0,))[1]
    # 同数量同风险：编号序列字典序 (1,4) < (2,3)
    # {C1,C4}: 5+4=9；{C2,C3}: 1+8=9
    k_a = _order_key(net, (0, 3))
    k_b = _order_key(net, (1, 2))
    assert k_a[0] == k_b[0] == 2
    assert k_a[1] == k_b[1] == 9
    assert k_a[2] < k_b[2]
