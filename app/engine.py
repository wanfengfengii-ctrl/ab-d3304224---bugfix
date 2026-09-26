"""归因求解引擎：枚举永久故障电缆组合并逐轮校验电源可达性。

业务模型
========

* 海底观测网是一个无向图，节点为接驳盒/传感器，边为海底电缆。
* 存在唯一电源节点 ``source``。
* 每根电缆有固定的「修复风险」(repair_risk)，并按页面录入顺序拥有
  从 1 开始的编号 ``seq``。
* 一部分电缆是「永久故障电缆」：无论继电器如何切换，该电缆在物理上
  始终断开。
* 每轮试验闭合一部分电缆（其余电缆因继电器断开而不通）。仅当一条
  电缆「本轮闭合」且「不是永久故障」时，它才真正导通。
* 某传感器（节点）通电，当且仅当它在本轮的导通子图中与电源节点连通。

对每一个候选永久故障组合，逐轮计算电源可达集合，并与工程师录入的
通电/断电读数比对。全部轮次所有节点读数均吻合的组合才是可行解释。

择优规则（依次比较）
--------------------
1. 故障电缆数量最少；
2. 修复风险总和最低；
3. 按电缆录入顺序展开的故障编号序列字典序最小。
   故障编号按录入顺序升序排列（如 (2, 5) 与 (3, 4)），逐位比较，
   第一位较小者整体最小 —— 即编号更靠前的电缆优先计入故障集。

若不存在任何可行组合，则这组多轮试验读数不能由同一组永久故障同时解释。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Cable:
    seq: int  # 录入顺序编号，从 1 开始
    name: str
    u: str
    v: str
    repair_risk: float


@dataclass(frozen=True)
class Round:
    index: int  # 轮次编号，从 1 开始
    closed: frozenset[str]  # 本轮闭合的电缆名称
    powered: frozenset[str]  # 本轮读数为「通电」的节点


@dataclass(frozen=True)
class Network:
    nodes: tuple[str, ...]
    source: str
    cables: tuple[Cable, ...]
    rounds: tuple[Round, ...]

    def cable_index(self) -> dict[str, int]:
        return {c.name: i for i, c in enumerate(self.cables)}


@dataclass(frozen=True)
class RoundCheck:
    """单轮比对明细。"""

    round: int
    closed: tuple[str, ...]
    reachable: tuple[str, ...]
    expected_powered: tuple[str, ...]
    expected_unpowered: tuple[str, ...]
    matches: bool
    mismatch_powered: tuple[str, ...]  # 读数通电但实际不可达
    mismatch_unpowered: tuple[str, ...]  # 读数断电但实际可达


@dataclass(frozen=True)
class Diagnosis:
    feasible: bool
    fault_names: tuple[str, ...]
    fault_seqs: tuple[int, ...]
    fault_count: int
    total_repair_risk: float
    candidate_count: int  # 可行候选总数（诊断/统计用途）
    evaluated: int  # 实际评估的候选组合数
    truncated: bool  # 是否因命中上限而截断枚举
    rounds: tuple[RoundCheck, ...]
    message: str


# 枚举候选数上限：E<=14 时全量也仅 16384，留有余量。
DEFAULT_MAX_CANDIDATES = 1_000_000


def reachable_nodes(
    nodes: Sequence[str],
    cables: Sequence[Cable],
    live: Iterable[str],
    source: str,
) -> set[str]:
    """在由 ``live``（电缆名称集合）构成的导通子图中计算电源可达节点。"""
    live_set = frozenset(live)
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for c in cables:
        if c.name in live_set:
            adj.setdefault(c.u, []).append(c.v)
            adj.setdefault(c.v, []).append(c.u)
    seen: set[str] = set()
    if source not in adj:
        return seen
    stack = [source]
    seen.add(source)
    while stack:
        cur = stack.pop()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _order_key(network: Network, fault_idx: tuple[int, ...]) -> tuple:
    """三级排序键：故障数 -> 风险和 -> 故障编号序列字典序。

    ``fault_idx`` 已按录入顺序升序；同等故障数的候选元组等长，
    直接逐位比较即可，第一位故障编号更小者优先。
    """
    risk = sum(network.cables[i].repair_risk for i in fault_idx)
    return (len(fault_idx), risk, fault_idx)


def _check_rounds(
    network: Network, fault_names: frozenset[str]
) -> tuple[bool, tuple[RoundCheck, ...]]:
    checks: list[RoundCheck] = []
    all_match = True
    for rnd in network.rounds:
        live = [
            name
            for name in rnd.closed
            if name not in fault_names
        ]
        reach = reachable_nodes(network.nodes, network.cables, live, network.source)
        powered = rnd.powered
        mismatch_on = tuple(sorted(powered - reach))
        mismatch_off = tuple(sorted(reach - powered))
        matches = not mismatch_on and not mismatch_off
        all_match = all_match and matches
        checks.append(
            RoundCheck(
                round=rnd.index,
                closed=tuple(sorted(rnd.closed)),
                reachable=tuple(sorted(reach)),
                expected_powered=tuple(sorted(powered)),
                expected_unpowered=tuple(
                    sorted(n for n in network.nodes if n not in powered)
                ),
                matches=matches,
                mismatch_powered=mismatch_on,
                mismatch_unpowered=mismatch_off,
            )
        )
    return all_match, tuple(checks)


def diagnose(network: Network, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> Diagnosis:
    """枚举全部永久故障组合（含零故障），逐轮比对，返回最优可行解释。

    E<=14 时组合总数不超过 16384，全量枚举成本很低，且满足
    「在所有永久故障电缆组合中逐轮计算」的要求。
    """
    cables = network.cables
    m = len(cables)

    best_idx: tuple[int, ...] | None = None
    best_key: tuple | None = None
    best_checks: tuple[RoundCheck, ...] = ()
    feasible_count = 0
    evaluated = 0
    truncated = False

    # 故障数量从小到大枚举（仅为稳定的枚举顺序）；不提前剪枝，
    # 所有 2^m 个组合都会被逐轮评估。
    for k in range(m + 1):
        for combo in combinations(range(m), k):
            evaluated += 1
            if evaluated > max_candidates:
                truncated = True
                break
            fault_names = frozenset(cables[i].name for i in combo)
            ok, checks = _check_rounds(network, fault_names)
            if not ok:
                continue
            feasible_count += 1
            key = _order_key(network, combo)
            if best_key is None or key < best_key:
                best_key = key
                best_idx = combo
                best_checks = checks
        if truncated:
            break

    if best_idx is None:
        return Diagnosis(
            feasible=False,
            fault_names=(),
            fault_seqs=(),
            fault_count=0,
            total_repair_risk=0.0,
            candidate_count=0,
            evaluated=evaluated,
            truncated=truncated,
            rounds=_check_rounds(network, frozenset())[1],
            message=(
                "这些试验读数不能由同一组永久故障同时解释："
                "已枚举全部永久故障电缆组合，没有任何组合能使所有轮次的"
                "电源可达性与录入读数吻合。"
            ),
        )

    names = tuple(cables[i].name for i in best_idx)
    seqs = tuple(cables[i].seq for i in best_idx)
    risk = round(best_key[1], 12)  # type: ignore[index]
    return Diagnosis(
        feasible=True,
        fault_names=names,
        fault_seqs=seqs,
        fault_count=len(names),
        total_repair_risk=risk,
        candidate_count=feasible_count,
        evaluated=evaluated,
        truncated=truncated,
        rounds=best_checks,
        message=(
            f"找到共同解释：永久故障电缆 {list(names) or '无'}；"
            f"共 {feasible_count} 个可行组合，已按故障数最少、"
            f"修复风险和最低、录入顺序编号序列最小择优。"
        ),
    )
