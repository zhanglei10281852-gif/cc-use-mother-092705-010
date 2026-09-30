from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ParticipantInput(BaseModel):
    department: str = Field(min_length=1, max_length=100)
    role: Literal["移交方", "接收方", "协办方"]
    contact: str = Field(default="", max_length=100)


class MaterialSpec(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    responsible_department: str = Field(min_length=1, max_length=100)
    is_sensitive: bool = False
    required: bool = True


class ActionCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2000)
    owner: str = Field(min_length=1, max_length=100)
    deadline: str = Field(min_length=8, max_length=40)
    participants: list[ParticipantInput] = Field(min_length=1, max_length=50)
    materials: list[MaterialSpec] = Field(default_factory=list, max_length=500)


class BatchItemInput(BaseModel):
    material_code: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_digest: str = Field(min_length=8, max_length=128)
    size_bytes: int = Field(ge=0, le=10_000_000_000)
    change_note: str = Field(default="", max_length=1000)


class BatchSubmit(BaseModel):
    kind: Literal["handover", "supplement"] = "handover"
    sender_department: str = Field(min_length=1, max_length=100)
    receiver_department: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)
    overdue_reason: str = Field(default="", max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=160)
    items: list[BatchItemInput] = Field(min_length=1, max_length=500)


class ItemConfirmation(BaseModel):
    material_code: str = Field(min_length=1, max_length=64)
    receiver_department: str = Field(min_length=1, max_length=100)
    confirmer: str = Field(min_length=1, max_length=100)
    result: Literal["complete", "missing", "sensitive_objection"]
    note: str = Field(default="", max_length=1000)


class BatchReopen(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=1000)
