from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.handover.schemas import (
    ActionCreate,
    BatchSubmit,
    CloseActionRequest,
    CloseBatchRequest,
    ItemConfirm,
    MaterialAdd,
    ObjectionCreate,
    ObjectionWithdraw,
    PartyAdd,
    ReopenRequest,
    SupplementRequest,
)
from app.handover.service import HandoverService

router = APIRouter(prefix="/api/handover", tags=["保护行动材料交接"])


def service() -> HandoverService:
    return HandoverService()


# -- 行动建档 ---------------------------------------------------------------


@router.post("/actions", status_code=201)
def create_action(payload: ActionCreate):
    return service().create_action(payload.to_storage(), actor=payload.manager)


@router.get("/actions")
def list_actions(status: str | None = Query(default=None, pattern="^(active|closed)$")):
    return service().list_actions(status=status)


@router.get("/actions/{action_id}")
def get_action(action_id: int):
    return service().get_action(action_id)


@router.post("/actions/{action_id}/parties", status_code=201)
def add_party(action_id: int, payload: PartyAdd):
    return service().add_party(
        action_id, payload.role, payload.department, payload.contact, actor=payload.actor
    )


@router.post("/actions/{action_id}/materials", status_code=201)
def add_material(action_id: int, payload: MaterialAdd):
    data = payload.model_dump(exclude={"actor"})
    return service().add_material(action_id, data, actor=payload.actor)


# -- 分批交接 ---------------------------------------------------------------


@router.post("/actions/{action_id}/batches")
def submit_batch(action_id: int, payload: BatchSubmit, response: Response):
    result = service().submit_batch(
        action_id,
        payload.sender_dept,
        payload.receiver_dept,
        [item.model_dump() for item in payload.items],
        actor=payload.actor,
        note=payload.note,
    )
    # 同一批材料重复上传：返回原批次（200）；新批次 201。
    response.status_code = 200 if result.pop("deduplicated", False) else 201
    return result


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    return service().get_batch(batch_id)


# -- 逐项确认与异议 ---------------------------------------------------------


@router.post("/batches/{batch_id}/confirm")
def confirm_item(batch_id: int, payload: ItemConfirm):
    return service().confirm_item(
        batch_id,
        payload.material_code,
        actor=payload.actor,
        receiver_dept=payload.receiver_dept,
        note=payload.note,
    )


@router.post("/batches/{batch_id}/objections", status_code=201)
def raise_objection(batch_id: int, payload: ObjectionCreate):
    return service().raise_objection(
        batch_id,
        actor=payload.actor,
        kind=payload.kind,
        reason=payload.reason,
        material_code=payload.material_code,
        receiver_dept=payload.receiver_dept,
    )


@router.post("/objections/{objection_id}/withdraw")
def withdraw_objection(objection_id: int, payload: ObjectionWithdraw):
    return service().withdraw_objection_by_id(objection_id, payload.actor)


# -- 补件 / 修订 / 重开 / 结案 ----------------------------------------------


@router.post("/batches/{batch_id}/supplements", status_code=201)
def supplement_batch(batch_id: int, payload: SupplementRequest):
    return service().supplement_batch(
        batch_id,
        [item.model_dump() for item in payload.items],
        actor=payload.actor,
        reason=payload.reason,
        sender_dept=payload.sender_dept,
    )


@router.post("/batches/{batch_id}/reopen")
def reopen_batch(batch_id: int, payload: ReopenRequest):
    return service().reopen_batch(
        batch_id, payload.manager, payload.reason, actor=payload.actor
    )


@router.post("/batches/{batch_id}/close")
def close_batch(batch_id: int, payload: CloseBatchRequest):
    return service().close_batch(
        batch_id, payload.actor, receiver_dept=payload.receiver_dept
    )


@router.post("/actions/{action_id}/close")
def close_action(action_id: int, payload: CloseActionRequest):
    return service().close_action(
        action_id, payload.manager, actor=payload.resolved_actor()
    )


# -- 版本链查询 -------------------------------------------------------------


@router.get("/actions/{action_id}/materials/{material_code}/versions")
def material_versions(action_id: int, material_code: str):
    return service().get_material_versions(action_id, material_code)
