"""内存草稿存储。

关键业务规则：草稿被修改后修订号 +1，且**不得保留旧归因结论**——
任何此前轮次算出的故障结论都会被立即清除，必须重新发起归因。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace

from .schemas import NetworkPayload


@dataclass
class DraftRecord:
    id: str
    revision: int
    payload: NetworkPayload
    diagnosis: dict | None = None
    diagnosis_revision: int | None = None


class DraftStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, DraftRecord] = {}

    def create(self, payload: NetworkPayload) -> DraftRecord:
        draft_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._items[draft_id] = DraftRecord(
                id=draft_id, revision=1, payload=payload
            )
            return replace(self._items[draft_id])

    def get(self, draft_id: str) -> DraftRecord | None:
        """返回记录的一致性快照（副本），调用方持有期间不被改稿写穿。"""
        with self._lock:
            rec = self._items.get(draft_id)
            return replace(rec) if rec is not None else None

    def get_snapshot(self, draft_id: str) -> tuple[NetworkPayload, int] | None:
        """在锁内读取 (草稿内容, 修订号) 的一致快照，供归因计算使用。"""
        with self._lock:
            rec = self._items.get(draft_id)
            if rec is None:
                return None
            return rec.payload, rec.revision

    def update(self, draft_id: str, payload: NetworkPayload) -> DraftRecord | None:
        """覆盖草稿内容：修订号递增并清除旧结论。"""
        with self._lock:
            rec = self._items.get(draft_id)
            if rec is None:
                return None
            rec.payload = payload
            rec.revision += 1
            # 修改草稿后不得保留旧结论
            rec.diagnosis = None
            rec.diagnosis_revision = None
            return replace(rec)

    def attach_diagnosis(
        self, draft_id: str, diagnosis: dict, revision: int
    ) -> DraftRecord | None:
        """保存归因结论，但仅当草稿修订号仍等于 ``revision``。

        归因计算期间草稿被修改（修订号已递增）时，在途结论属于旧修订，
        必须丢弃而非覆盖到新修订上——返回 None 表示结论未被保存。
        """
        with self._lock:
            rec = self._items.get(draft_id)
            if rec is None or rec.revision != revision:
                return None
            rec.diagnosis = diagnosis
            rec.diagnosis_revision = rec.revision
            return replace(rec)

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._items)


store = DraftStore()
