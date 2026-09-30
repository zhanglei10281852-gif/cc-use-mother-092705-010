from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.clock import to_storage


class PartyInput(BaseModel):
    department: str = Field(..., min_length=1, max_length=100)
    contact: str = Field(default="", max_length=80)

    @field_validator("department")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("部门名称不能为空")
        return value


class MaterialSpec(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    name: str = Field(..., min_length=1, max_length=200)
    category: str = Field(default="", max_length=60)
    sensitive: bool = False
    optional: bool = False

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("材料编码不能为空")
        return value


class ActionCreate(BaseModel):
    code: str | None = Field(default=None, min_length=3, max_length=60)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    owner: str = Field(..., min_length=1, max_length=80)
    manager: str = Field(..., min_length=1, max_length=80)
    deadline: datetime | None = None
    senders: list[PartyInput] = Field(..., min_length=1)
    receivers: list[PartyInput] = Field(..., min_length=1)
    materials: list[MaterialSpec] = Field(..., min_length=1)

    def to_storage(self) -> dict:
        data = self.model_dump()
        data["deadline"] = to_storage(self.deadline) if self.deadline else None
        data["senders"] = [item.model_dump() for item in self.senders]
        data["receivers"] = [item.model_dump() for item in self.receivers]
        data["materials"] = [item.model_dump() for item in self.materials]
        return data


class PartyAdd(BaseModel):
    role: str = Field(..., pattern="^(sender|receiver)$")
    department: str = Field(..., min_length=1, max_length=100)
    contact: str = Field(default="", max_length=80)
    actor: str = Field(..., min_length=1, max_length=80)

    @field_validator("department")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("部门名称不能为空")
        return value


class MaterialAdd(MaterialSpec):
    actor: str = Field(..., min_length=1, max_length=80)


class HandoverItem(BaseModel):
    material_code: str = Field(..., min_length=1, max_length=60)
    title: str | None = Field(default=None, max_length=200)
    content_ref: str = Field(..., min_length=1, max_length=500)
    content_sha256: str = Field(default="", max_length=128)
    media_type: str = Field(default="", max_length=80)
    byte_size: int = Field(default=0, ge=0)
    sensitive: bool | None = None
    note: str = Field(default="", max_length=500)

    @field_validator("material_code", "content_ref")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("content_sha256")
    @classmethod
    def _normalize_hash(cls, value: str) -> str:
        return value.strip().lower()


class BatchSubmit(BaseModel):
    sender_dept: str = Field(..., min_length=1, max_length=100)
    receiver_dept: str = Field(..., min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)
    actor: str = Field(..., min_length=1, max_length=80)
    items: list[HandoverItem] = Field(..., min_length=1)

    @field_validator("sender_dept", "receiver_dept")
    @classmethod
    def _strip_dept(cls, value: str) -> str:
        return value.strip()


class ItemConfirm(BaseModel):
    material_code: str = Field(..., min_length=1, max_length=60)
    receiver_dept: str | None = Field(default=None, max_length=100)
    actor: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class ObjectionCreate(BaseModel):
    kind: str = Field(..., pattern="^(missing|sensitive|other)$")
    material_code: str | None = Field(default=None, max_length=60)
    reason: str = Field(..., min_length=1, max_length=500)
    receiver_dept: str | None = Field(default=None, max_length=100)
    actor: str = Field(..., min_length=1, max_length=80)


class SupplementRequest(BaseModel):
    sender_dept: str | None = Field(default=None, max_length=100)
    reason: str = Field(..., min_length=1, max_length=500)
    actor: str = Field(..., min_length=1, max_length=80)
    items: list[HandoverItem] = Field(..., min_length=1)


class ReopenRequest(BaseModel):
    manager: str = Field(..., min_length=1, max_length=80)
    reason: str = Field(..., min_length=1, max_length=500)
    actor: str = Field(..., min_length=1, max_length=80)


class CloseBatchRequest(BaseModel):
    actor: str = Field(..., min_length=1, max_length=80)
    receiver_dept: str | None = Field(default=None, max_length=100)


class CloseActionRequest(BaseModel):
    manager: str = Field(..., min_length=1, max_length=80)
    actor: str = Field(default="", max_length=80)

    def resolved_actor(self) -> str:
        return self.actor or self.manager


class ObjectionWithdraw(BaseModel):
    actor: str = Field(..., min_length=1, max_length=80)
