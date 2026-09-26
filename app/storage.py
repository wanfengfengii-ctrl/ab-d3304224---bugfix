"""内存草稿存储。

关键业务规则：草稿被修改后修订号 +1，且**不得保留旧归因结论**——
任何此前轮次算出的故障结论都会被立即清除，必须重新发起归因。

并发安全
========

归因计算耗时较长，计算期间若并发发生改稿，旧修订开始的归因不得写回
成为新修订的结论。归因开始时通过 ``begin_diagnosis`` 取走「修订号 +
草稿内容」的快照；计算结束 ``finish_diagnosis`` 时只有草稿仍处于同一
修订才允许写回，改稿后修订号已递增，旧结果在此被直接丢弃。
读取一律经锁内快照（``DraftView``），不外借可变的草稿对象，避免调用方
看到改稿中途的半成品。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from .schemas import NetworkPayload


@dataclass
class DraftRecord:
    id: str
    revision: int
    payload: NetworkPayload
    diagnosis: dict | None = None
    diagnosis_revision: int | None = None


@dataclass(frozen=True)
class DraftView:
    """草稿在某一时刻的不可变快照。"""

    id: str
    revision: int
    payload: NetworkPayload
    diagnosis: dict | None
    diagnosis_revision: int | None


class DraftStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, DraftRecord] = {}

    def create(self, payload: NetworkPayload) -> DraftView:
        draft_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._items[draft_id] = DraftRecord(
                id=draft_id, revision=1, payload=payload
            )
            return self._snapshot_locked(self._items[draft_id])

    def exists(self, draft_id: str) -> bool:
        with self._lock:
            return draft_id in self._items

    def snapshot(self, draft_id: str) -> DraftView | None:
        with self._lock:
            rec = self._items.get(draft_id)
            return self._snapshot_locked(rec) if rec is not None else None

    @staticmethod
    def _snapshot_locked(rec: DraftRecord) -> DraftView:
        return DraftView(
            id=rec.id,
            revision=rec.revision,
            payload=rec.payload,
            diagnosis=rec.diagnosis,
            diagnosis_revision=rec.diagnosis_revision,
        )

    def update(self, draft_id: str, payload: NetworkPayload) -> DraftView | None:
        """覆盖草稿内容：修订号递增并清除旧结论。"""
        with self._lock:
            rec = self._items.get(draft_id)
            if rec is None:
                return None
            rec.payload = payload
            rec.revision += 1
            # 修改草稿后不得保留旧结论；旧修订在途归因结束时会因修订号
            # 不再相等而被 finish_diagnosis 拒绝写回。
            rec.diagnosis = None
            rec.diagnosis_revision = None
            return self._snapshot_locked(rec)

    def begin_diagnosis(self, draft_id: str) -> DraftView | None:
        """归因开始：取当前修订号与草稿内容的快照供离线计算。"""
        return self.snapshot(draft_id)

    def finish_diagnosis(
        self, draft_id: str, revision: int, diagnosis: dict
    ) -> DraftView | None:
        """归因计算结束后写回结论。

        仅当草稿仍是归因开始时的同一修订才写入；归因期间改稿会使修订号
        递增，旧修订的结果在此被丢弃（返回 None），绝不覆盖新草稿。
        """
        with self._lock:
            rec = self._items.get(draft_id)
            if rec is None or rec.revision != revision:
                return None
            rec.diagnosis = diagnosis
            rec.diagnosis_revision = revision
            return self._snapshot_locked(rec)

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._items)


store = DraftStore()
