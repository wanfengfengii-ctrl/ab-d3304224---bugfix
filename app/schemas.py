"""Pydantic 模型与入参校验。

约束（来自业务规则）：
* 节点 5~10 个，名称非空且唯一；
* 唯一电源节点必须是已录入节点之一；
* 电缆 6~14 条，名称非空且唯一，端点必须是已录入节点，修复风险非负；
* 试验 2~7 轮，每轮给出闭合电缆清单与全部节点的通电/断电读数。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .engine import Cable, Network, Round

Risk = Annotated[float, Field(ge=0, le=1_000_000)]

MIN_NODES, MAX_NODES = 5, 10
MIN_CABLES, MAX_CABLES = 6, 14
MIN_ROUNDS, MAX_ROUNDS = 2, 7


class CablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    u: str = Field(min_length=1, max_length=64)
    v: str = Field(min_length=1, max_length=64)
    repair_risk: Risk = 0.0


class RoundPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closed: list[str] = Field(default_factory=list)
    # 节点名 -> true=通电 / false=断电
    readings: dict[str, bool] = Field(default_factory=dict)

    @field_validator("closed")
    @classmethod
    def _closed_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("同一轮中闭合电缆重复")
        return value


class NetworkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="未命名观测网", max_length=128)
    nodes: list[str] = Field(min_length=MIN_NODES, max_length=MAX_NODES)
    source: str = Field(min_length=1, max_length=64)
    cables: list[CablePayload] = Field(min_length=MIN_CABLES, max_length=MAX_CABLES)
    rounds: list[RoundPayload] = Field(min_length=MIN_ROUNDS, max_length=MAX_ROUNDS)

    @field_validator("nodes")
    @classmethod
    def _nodes_valid(cls, value: list[str]) -> list[str]:
        for n in value:
            if not n.strip():
                raise ValueError("节点名称不能为空")
        if len(set(value)) != len(value):
            raise ValueError("节点名称重复")
        return value

    @field_validator("cables")
    @classmethod
    def _cables_unique(cls, value: list[CablePayload]) -> list[CablePayload]:
        names = [c.name for c in value]
        if len(set(names)) != len(names):
            raise ValueError("电缆名称重复")
        return value


class DraftEnvelope(BaseModel):
    """GET /drafts/{id} 的返回：草稿内容 + 版本 + 最近一次归因结论。"""

    id: str
    revision: int
    payload: NetworkPayload
    diagnosis: dict | None = None


def build_network(payload: NetworkPayload) -> Network:
    """把 API 入参转换为引擎结构，并做跨字段一致性校验。"""
    node_set = set(payload.nodes)

    if payload.source not in node_set:
        raise ValueError(f"电源节点 {payload.source!r} 不在节点列表中")

    names: set[str] = set()
    cables: list[Cable] = []
    for i, c in enumerate(payload.cables, start=1):
        if c.u not in node_set:
            raise ValueError(f"电缆 {c.name!r} 的端点 {c.u!r} 不是已录入节点")
        if c.v not in node_set:
            raise ValueError(f"电缆 {c.name!r} 的端点 {c.v!r} 不是已录入节点")
        cables.append(Cable(seq=i, name=c.name, u=c.u, v=c.v, repair_risk=float(c.repair_risk)))
        names.add(c.name)

    rounds: list[Round] = []
    for idx, r in enumerate(payload.rounds, start=1):
        unknown = [n for n in r.closed if n not in names]
        if unknown:
            raise ValueError(f"第 {idx} 轮闭合了不存在的电缆: {unknown}")
        missing = [n for n in payload.nodes if n not in r.readings]
        extra = [n for n in r.readings if n not in node_set]
        if missing:
            raise ValueError(f"第 {idx} 轮缺少传感器读数: {missing}")
        if extra:
            raise ValueError(f"第 {idx} 轮读数包含未知节点: {extra}")
        powered = frozenset(n for n, on in r.readings.items() if on)
        rounds.append(
            Round(index=idx, closed=frozenset(r.closed), powered=powered)
        )

    return Network(
        nodes=tuple(payload.nodes),
        source=payload.source,
        cables=tuple(cables),
        rounds=tuple(rounds),
    )
