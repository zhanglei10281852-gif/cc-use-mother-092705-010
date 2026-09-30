from __future__ import annotations

from fastapi import APIRouter, Query

from app.handover.schemas import ActionCreate, BatchReopen, BatchSubmit, ItemConfirmation
from app.handover.service import HandoverService

router = APIRouter(prefix="/api/handover", tags=["保护行动材料交接"])


def service() -> HandoverService:
    return HandoverService()


@router.post("/actions", status_code=201)
def create_action(payload: ActionCreate, actor: str = Query(..., min_length=1)):
    return service().create_action(payload.model_dump(), actor)


@router.get("/actions")
def list_actions():
    return {"items": service().list_actions()}


@router.get("/actions/{action_code}")
def get_action(action_code: str):
    return service().get_action(action_code)


@router.post("/actions/{action_code}/batches", status_code=201)
def submit_batch(action_code: str, payload: BatchSubmit, actor: str = Query(..., min_length=1)):
    return service().submit_batch(action_code, payload.model_dump(), actor)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    return service().get_batch(batch_id)


@router.post("/batches/{batch_id}/confirmations", status_code=201)
def confirm_item(batch_id: int, payload: ItemConfirmation):
    return service().confirm_item(batch_id, payload.model_dump())


@router.post("/batches/{batch_id}/reopen")
def reopen_batch(batch_id: int, payload: BatchReopen):
    return service().reopen_batch(batch_id, payload.actor, payload.reason)


@router.post("/actions/{action_code}/close")
def close_action(action_code: str, actor: str = Query(..., min_length=1)):
    return service().close_action(action_code, actor)
