"""内存草稿存储。

关键业务规则：草稿被修改后修订号 +1，且**不得保留旧归因结论**——
任何此前轮次算出的故障结论都会被立即清除，必须重新发起归因。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

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
            return self._items[draft_id]

    def get(self, draft_id: str) -> DraftRecord | None:
        with self._lock:
            return self._items.get(draft_id)

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
            return rec

    def attach_diagnosis(self, draft_id: str, diagnosis: dict) -> DraftRecord | None:
        with self._lock:
            rec = self._items.get(draft_id)
            if rec is None:
                return None
            rec.diagnosis = diagnosis
            rec.diagnosis_revision = rec.revision
            return rec

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._items)


store = DraftStore()
