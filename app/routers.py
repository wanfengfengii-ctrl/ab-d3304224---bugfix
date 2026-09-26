"""FastAPI 路由：草稿录入/修改、发起归因、结论与逐轮明细查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from .engine import Diagnosis, diagnose
from .schemas import NetworkPayload, build_network
from .storage import DraftRecord, store

router = APIRouter(prefix="/api")


def diagnosis_to_dict(d: Diagnosis) -> dict[str, Any]:
    return {
        "feasible": d.feasible,
        "fault_cables": [
            {"seq": seq, "name": name}
            for seq, name in zip(d.fault_seqs, d.fault_names)
        ],
        "fault_count": d.fault_count,
        "total_repair_risk": d.total_repair_risk,
        "candidate_count": d.candidate_count,
        "evaluated": d.evaluated,
        "truncated": d.truncated,
        "message": d.message,
        "rounds": [
            {
                "round": rc.round,
                "closed": list(rc.closed),
                "reachable": list(rc.reachable),
                "readings": {
                    "expected_powered": list(rc.expected_powered),
                    "expected_unpowered": list(rc.expected_unpowered),
                },
                "matches": rc.matches,
                "mismatch": {
                    "powered_but_unreachable": list(rc.mismatch_powered),
                    "unpowered_but_reachable": list(rc.mismatch_unpowered),
                },
            }
            for rc in d.rounds
        ],
    }


def _record_to_dict(rec: DraftRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "revision": rec.revision,
        "payload": rec.payload.model_dump(),
        "diagnosis": rec.diagnosis,
        "diagnosis_revision": rec.diagnosis_revision,
    }


def _parse_payload(data: dict) -> NetworkPayload:
    try:
        payload = NetworkPayload(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    try:
        build_network(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=[{"msg": str(exc)}]) from exc
    return payload


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "seabed-observer-attribution"}


@router.post("/drafts")
def create_draft(body: dict) -> dict:
    payload = _parse_payload(body)
    rec = store.create(payload)
    return _record_to_dict(rec)


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str) -> dict:
    rec = store.get(draft_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return _record_to_dict(rec)


@router.put("/drafts/{draft_id}")
def update_draft(draft_id: str, body: dict) -> dict:
    if store.get(draft_id) is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    payload = _parse_payload(body)
    rec = store.update(draft_id, payload)
    assert rec is not None
    return _record_to_dict(rec)


@router.post("/drafts/{draft_id}/diagnose")
def run_diagnosis(draft_id: str) -> dict:
    """对当前草稿发起归因；结论绑定发起时的修订号。

    归因计算期间若草稿被并发修改（修订号已递增），本次结论属于旧修订：
    不保存、不作为当前结果返回，直接以 409 作废，须基于新修订重新发起。
    """
    snapshot = store.get_snapshot(draft_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    draft_payload, revision = snapshot
    network = build_network(draft_payload)
    result = diagnose(network)
    payload = diagnosis_to_dict(result)
    payload["revision"] = revision
    if store.attach_diagnosis(draft_id, payload, revision) is None:
        raise HTTPException(
            status_code=409,
            detail="归因计算期间草稿已被修改，该结论已作废；请基于当前草稿重新发起归因",
        )
    return payload


@router.get("/drafts/{draft_id}/diagnosis")
def get_diagnosis(draft_id: str) -> dict:
    rec = store.get(draft_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    # 结论必须属于当前修订：改稿后未重新归因（或旧结论已被废弃）时返回 409
    if rec.diagnosis is None or rec.diagnosis_revision != rec.revision:
        raise HTTPException(status_code=409, detail="当前草稿尚无归因结论，请先发起归因")
    return rec.diagnosis


@router.post("/diagnose")
def diagnose_ephemeral(body: dict) -> dict:
    """无状态归因接口：直接对提交的草稿计算，便于业务冒烟与外部集成。"""
    payload = _parse_payload(body)
    network = build_network(payload)
    return diagnosis_to_dict(diagnose(network))
