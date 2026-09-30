from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.handover.repository import HandoverRepository
from app.handover.repository import ensure_schema as ensure_handover_schema


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def ensure_schema() -> None:
    ensure_handover_schema(get_connection())


class HandoverService:
    """保护行动材料交接：建行动、分批交接、逐项确认、异议、补件、重开与结案。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_handover_schema(self.connection)
        self.repository = HandoverRepository(self.connection)

    # ---------- 行动建立 ----------
    def create_action(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        deadline = payload["deadline"]
        if from_storage(deadline) is None:
            raise ValidationError("截止时间格式不正确，应为 ISO 8601 时间")
        participants = payload.get("participants", [])
        materials = payload.get("materials", [])
        if not participants:
            raise ValidationError("至少登记一个参与部门")
        departments = {item["department"] for item in participants}
        receiver_departments = {item["department"] for item in participants if item["role"] == "接收方"}
        if not receiver_departments:
            raise ValidationError("参与部门中必须至少有一个接收方")
        material_codes = [item["code"] for item in materials]
        if len(material_codes) != len(set(material_codes)):
            raise ValidationError("材料清单中存在重复编码")
        for item in materials:
            if item["responsible_department"] not in departments:
                raise ValidationError(f"材料 {item['code']} 的责任部门不在参与部门名单中")
        with transaction(immediate=True) as connection:
            repository = HandoverRepository(connection)
            if repository.action_by_code(payload["code"]):
                raise ConflictError("行动编码已存在")
            action_id = repository.create_action(
                code=payload["code"], name=payload["name"], description=payload.get("description", ""),
                owner=payload["owner"], deadline=deadline, created_by=actor, now=now,
            )
            for item in participants:
                repository.add_participant(
                    action_id=action_id, department=item["department"], role=item["role"],
                    contact=item.get("contact", ""), now=now,
                )
            for item in materials:
                repository.add_material(
                    action_id=action_id, code=item["code"], title=item["title"],
                    responsible_department=item["responsible_department"],
                    is_sensitive=item.get("is_sensitive", False), required=item.get("required", True), now=now,
                )
            repository.add_event(action_id=action_id, batch_id=None, actor=actor, action="action.create",
                                 detail={"code": payload["code"], "participants": len(participants), "materials": len(materials)}, now=now)
        return self.get_action(payload["code"])

    # ---------- 分批交接 / 补件 ----------
    def submit_batch(self, action_code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        with transaction(immediate=True) as connection:
            repository = HandoverRepository(connection)
            action = self._require_action(repository, action_code)
            action_id = int(action["id"])
            if action["status"] == "closed":
                raise ConflictError("行动已经结案，不能再交接材料；如需补件请先由负责人重开行动")
            overdue = now_dt > from_storage(action["deadline"])
            kind = payload.get("kind", "handover")
            overdue_reason = payload.get("overdue_reason", "").strip()
            if overdue:
                if kind != "supplement":
                    raise ConflictError("已超过行动截止时间，只允许提交补件批次")
                if not overdue_reason:
                    raise ValidationError("逾期补件必须填写逾期原因")
            elif kind == "supplement" and not overdue_reason:
                # 截止日前的补件也要求说明补件原因，便于追溯责任
                if not payload.get("note", "").strip():
                    raise ValidationError("补件批次需在 note 或 overdue_reason 中说明补件原因")
            sender = payload["sender_department"]
            receiver = payload["receiver_department"]
            self._check_handover_parties(repository, action_id, sender, receiver)

            raw_items = payload.get("items", [])
            if not raw_items:
                raise ValidationError("交接批次至少包含一份材料")
            codes = [item["material_code"] for item in raw_items]
            if len(codes) != len(set(codes)):
                raise ValidationError("同一批次中一份材料只能出现一次")

            normalized: list[dict[str, Any]] = []
            for item in raw_items:
                material = repository.material_by_code(action_id, item["material_code"])
                if material is None:
                    raise NotFoundError(f"材料 {item['material_code']} 不在该行动的材料清单中")
                normalized.append({
                    "material": material,
                    "filename": item["filename"],
                    "content_digest": item["content_digest"],
                    "size_bytes": item["size_bytes"],
                    "change_note": item.get("change_note", ""),
                })

            fingerprint = digest({
                "sender": sender, "receiver": receiver, "kind": kind,
                "items": sorted(
                    (item["material"]["code"], item["content_digest"], item["filename"], item["size_bytes"])
                    for item in normalized
                ),
            })

            # 幂等键命中：直接返回原批次
            idem_key = payload.get("idempotency_key")
            if idem_key:
                existing = repository.batch_by_idempotency_key(action_id, idem_key)
                if existing is not None:
                    return self._batch_detail(repository, int(existing["id"]), reused=True)

            # 同一批材料重复上传：返回原批次，不再产生新版本或新批次
            twin = repository.find_batch_by_fingerprint(action_id, fingerprint)
            if twin is not None:
                return self._batch_detail(repository, int(twin["id"]), reused=True)

            batch_no = repository.next_batch_no(action_id)
            batch_id = repository.create_batch(
                action_id=action_id, batch_no=batch_no, kind=kind, idempotency_key=idem_key,
                sender_department=sender, receiver_department=receiver, note=payload.get("note", ""),
                overdue_reason=overdue_reason, fingerprint=fingerprint, created_by=actor, now=now,
            )
            version_summary: list[dict[str, Any]] = []
            for entry in normalized:
                material = entry["material"]
                material_id = int(material["id"])
                previous_digest = repository.latest_digest(material_id)
                if previous_digest == entry["content_digest"]:
                    versions = repository.versions_of_material(material_id)
                    version_id = int(versions[-1]["id"])
                    version_no = int(versions[-1]["version_no"])
                    is_new_version = False
                else:
                    versions = repository.versions_of_material(material_id)
                    version_no = len(versions) + 1
                    supersedes = int(versions[-1]["id"]) if versions else None
                    version_id = repository.add_version(
                        material_id=material_id, version_no=version_no, batch_id=batch_id,
                        supersedes_version_id=supersedes, filename=entry["filename"],
                        content_digest=entry["content_digest"], size_bytes=entry["size_bytes"],
                        change_note=entry["change_note"], uploaded_by=actor, now=now,
                    )
                    repository.set_current_version(material_id, version_id, now)
                    is_new_version = True
                repository.add_batch_item(batch_id=batch_id, material_id=material_id, version_id=version_id)
                version_summary.append({
                    "material_code": material["code"], "version_no": version_no, "new_version": is_new_version,
                    "supersedes_version_no": (version_no - 1 if is_new_version and version_no > 1 else None),
                })
            repository.add_event(
                action_id=action_id, batch_id=batch_id, actor=actor,
                action="batch.submit" if kind == "handover" else "batch.supplement",
                detail={"batch_no": batch_no, "sender": sender, "receiver": receiver,
                        "overdue": overdue, "overdue_reason": overdue_reason, "versions": version_summary},
                now=now,
            )
        return self._batch_detail(self.repository, batch_id, reused=False)

    # ---------- 逐项确认 / 异议 ----------
    def confirm_item(self, batch_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = HandoverRepository(connection)
            batch = repository.batch_by_id(batch_id)
            if batch is None:
                raise NotFoundError("交接批次不存在")
            action = repository.action_by_id(int(batch["action_id"]))
            if action["status"] == "closed":
                raise ConflictError("行动已经结案，不能再登记确认")
            item = next((row for row in repository.items(batch_id) if row["material_code"] == payload["material_code"]), None)
            if item is None:
                raise NotFoundError("该材料不在此批次中")
            receiver = payload["receiver_department"]
            if receiver != batch["receiver_department"]:
                raise ConflictError("只有批次接收方可以逐项确认")
            roles = repository.participant_roles(int(batch["action_id"]), receiver)
            if "接收方" not in roles:
                raise ConflictError("确认部门不是该行动登记的接收方")
            result = payload["result"]
            note = payload.get("note", "").strip()
            if result in {"missing", "sensitive_objection"} and not note:
                raise ValidationError("提出缺件或敏感内容异议时必须填写说明")
            if result == "complete" and not note:
                note = "接收方逐项核对，确认完整"
            repository.add_confirmation(
                batch_id=batch_id, item_id=int(item["item_id"]), material_id=int(item["material_id"]),
                version_id=int(item["version_id"]), receiver_department=receiver,
                confirmer=payload["confirmer"], result=result, note=note, now=now,
            )
            repository.update_batch_status(batch_id, self._derive_batch_status(repository, batch_id, batch))
            repository.touch_action(int(batch["action_id"]), now)
            repository.add_event(
                action_id=int(batch["action_id"]), batch_id=batch_id, actor=payload["confirmer"],
                action="item.confirm" if result == "complete" else "item.object",
                detail={"material_code": payload["material_code"], "version_no": item["version_no"],
                        "result": result, "note": note},
                now=now,
            )
        return self._batch_detail(self.repository, batch_id)

    # ---------- 负责人重开 ----------
    def reopen_batch(self, batch_id: int, actor: str, reason: str) -> dict[str, Any]:
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        with transaction(immediate=True) as connection:
            repository = HandoverRepository(connection)
            batch = repository.batch_by_id(batch_id)
            if batch is None:
                raise NotFoundError("交接批次不存在")
            action = repository.action_by_id(int(batch["action_id"]))
            if actor != action["owner"]:
                raise ConflictError("只有行动负责人可以重开交接")
            if now_dt > from_storage(action["deadline"]):
                raise ConflictError("已超过截止时间，不能重开交接；逾期仅允许提交补件并说明原因")
            if batch["status"] == "confirmed":
                raise ConflictError("批次已全部确认完整，无需重开")
            cutoff_id = repository.max_confirmation_id(batch_id)
            repository.mark_reopened(batch_id, cutoff_id)
            repository.add_event(
                action_id=int(batch["action_id"]), batch_id=batch_id, actor=actor, action="batch.reopen",
                detail={"reason": reason, "cutoff_confirmation_id": cutoff_id, "previous_confirmations_retained": True}, now=now,
            )
        return self._batch_detail(self.repository, batch_id)

    # ---------- 结案 ----------
    def close_action(self, action_code: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = HandoverRepository(connection)
            action = self._require_action(repository, action_code)
            action_id = int(action["id"])
            if actor != action["owner"]:
                raise ConflictError("只有行动负责人可以结案")
            if action["status"] == "closed":
                raise ConflictError("行动已经结案")
            batches = repository.batches(action_id)
            if not batches:
                raise ConflictError("尚无任何交接批次，不能结案")
            unresolved_batches, missing_required = self._unresolved_state(repository, action_id)
            if unresolved_batches:
                raise ConflictError("仍有批次的当前版本材料未完成逐项确认或存在未解决异议，不能结案",
                                    context={"batches": unresolved_batches})
            if missing_required:
                raise ConflictError("仍有必备材料的当前版本未被确认完整", context={"materials": missing_required})
            repository.close_action(action_id, now)
            repository.add_event(action_id=action_id, batch_id=None, actor=actor, action="action.close",
                                 detail={"batches": len(batches)}, now=now)
        return self.get_action(action_code)

    # ---------- 查询 ----------
    def get_action(self, action_code: str) -> dict[str, Any]:
        repository = self.repository
        action_row = repository.action_by_code(action_code)
        if action_row is None:
            raise NotFoundError("保护行动不存在")
        return self._action_detail(repository, action_row)

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        return self._batch_detail(self.repository, batch_id)

    def list_actions(self) -> list[dict[str, Any]]:
        now_dt = self.clock.now()
        rows = self.connection.execute("SELECT * FROM protection_actions ORDER BY id").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["overdue"] = item["status"] == "open" and now_dt > from_storage(item["deadline"])
            item["batch_count"] = self.connection.execute(
                "SELECT COUNT(*) FROM handover_batches WHERE action_id=?", (row["id"],),
            ).fetchone()[0]
            result.append(item)
        return result

    # ---------- 组装 ----------
    def _action_detail(self, repository: HandoverRepository, action_row: sqlite3.Row) -> dict[str, Any]:
        action_id = int(action_row["id"])
        action = dict(action_row)
        now_dt = self.clock.now()
        action["overdue"] = action["status"] == "open" and now_dt > from_storage(action["deadline"])
        action["participants"] = repository.participants(action_id)
        materials: list[dict[str, Any]] = []
        for material in repository.materials(action_id):
            material["versions"] = repository.versions_of_material(int(material["id"]))
            current = next((v for v in material["versions"] if v["id"] == material["current_version_id"]), None)
            material["current_version_no"] = None if current is None else current["version_no"]
            material["version_chain"] = [
                {"version_no": v["version_no"], "version_id": v["id"],
                 "supersedes_version_id": v["supersedes_version_id"],
                 "batch_id": v["batch_id"], "content_digest": v["content_digest"]}
                for v in material["versions"]
            ]
            materials.append(material)
        action["materials"] = materials
        batches: list[dict[str, Any]] = []
        for batch_row in repository.batches(action_id):
            batches.append(self._batch_detail(repository, int(batch_row["id"])))
        action["batches"] = batches
        action["events"] = repository.events(action_id)
        unresolved_batches, missing_required = self._unresolved_state(repository, action_id)
        action["closure"] = {
            "can_close": action["status"] == "open"
            and bool(batches)
            and not unresolved_batches
            and not missing_required,
            "unresolved_batches": unresolved_batches,
            "missing_required_materials": missing_required,
        }
        return action

    def _batch_detail(self, repository: HandoverRepository, batch_id: int, *, reused: bool = False) -> dict[str, Any]:
        batch_row = repository.batch_by_id(batch_id)
        if batch_row is None:
            raise NotFoundError("交接批次不存在")
        batch = dict(batch_row)
        items = repository.items(batch_id)
        cutoff_id = int(batch.get("reopen_cutoff_id") or 0)
        latest = repository.latest_confirmation_map(batch_id, cutoff_id)
        confirmations = repository.confirmations(batch_id)
        active_objections = 0
        resolved_objections = 0
        unconfirmed = 0
        for item in items:
            marker = latest.get(int(item["item_id"]))
            result = None if marker is None else marker["result"]
            material_row = repository.material_by_id(int(item["material_id"]))
            is_current = material_row["current_version_id"] in (None, int(item["version_id"]))
            item["latest_result"] = result
            item["latest_confirmer"] = None if marker is None else marker["confirmer"]
            item["latest_confirmed_at"] = None if marker is None else marker["created_at"]
            item["version_is_current"] = is_current
            if result in {"missing", "sensitive_objection"}:
                active_objections += 1
                if not is_current:
                    # 异议针对的版本已被修订取代，且新版本另有完整确认
                    if self._version_confirmed_complete(repository, int(item["material_id"]), int(material_row["current_version_id"])):
                        resolved_objections += 1
            elif result is None and is_current:
                unconfirmed += 1
            item["item_confirmations"] = [
                {key: confirmation[key] for key in ("result", "note", "confirmer", "receiver_department", "version_no", "created_at")}
                for confirmation in confirmations if confirmation["item_id"] == item["item_id"]
            ]
        # 批次状态如实反映该批次自身在最近一轮（重开分界之后）的逐项确认结果
        results = [item["latest_result"] for item in items]
        if results and all(result == "complete" for result in results):
            stored_status = "confirmed"
        elif any(result in {"missing", "sensitive_objection"} for result in results):
            stored_status = "disputed"
        else:
            stored_status = "pending"
        batch["status"] = stored_status
        batch["unconfirmed_count"] = unconfirmed
        batch["active_objection_count"] = active_objections - resolved_objections
        batch["resolved_objection_count"] = resolved_objections
        batch["items"] = items
        batch["all_confirmations"] = [
            {key: confirmation[key] for key in ("material_code", "version_no", "result", "note", "confirmer", "receiver_department", "created_at")}
            for confirmation in confirmations
        ]
        batch["reused"] = reused
        batch.pop("fingerprint", None)
        return batch

    # ---------- 规则 ----------
    @staticmethod
    def _require_action(repository: HandoverRepository, code: str) -> sqlite3.Row:
        action = repository.action_by_code(code)
        if action is None:
            raise NotFoundError("保护行动不存在")
        return action

    @staticmethod
    def _check_handover_parties(repository: HandoverRepository, action_id: int, sender: str, receiver: str) -> None:
        if sender == receiver:
            raise ValidationError("移交方与接收方不能是同一部门")
        sender_roles = repository.participant_roles(action_id, sender)
        receiver_roles = repository.participant_roles(action_id, receiver)
        if not sender_roles:
            raise ValidationError(f"移交部门 {sender} 未登记为参与部门")
        if not receiver_roles:
            raise ValidationError(f"接收部门 {receiver} 未登记为参与部门")
        if "接收方" not in receiver_roles:
            raise ConflictError(f"部门 {receiver} 不是登记的接收方")
        if not ({'移交方', '协办方'} & sender_roles):
            raise ConflictError(f"部门 {sender} 不是登记的移交方或协办方")

    @staticmethod
    def _derive_batch_status(repository: HandoverRepository, batch_id: int, batch: sqlite3.Row) -> str:
        items = repository.items(batch_id)
        latest = repository.latest_confirmation_map(batch_id, batch["reopen_cutoff_id"] or 0)
        results: list[str | None] = []
        for item in items:
            marker = latest.get(int(item["item_id"]))
            results.append(None if marker is None else marker["result"])
        if results and all(result == "complete" for result in results):
            return "confirmed"
        if any(result in {"missing", "sensitive_objection"} for result in results):
            return "disputed"
        return "pending"

    @staticmethod
    def _version_confirmed_complete(repository: HandoverRepository, material_id: int, version_id: int | None) -> bool:
        if version_id is None:
            return False
        return repository.version_has_active_complete(material_id, int(version_id))

    def _unresolved_state(self, repository: HandoverRepository, action_id: int) -> tuple[list[int], list[str]]:
        """返回 (仍有当前版本条目未确认完整的批次号, 当前版本未确认完整的必备材料编码)。"""
        materials = {int(m["id"]): m for m in repository.materials(action_id)}
        view = repository.item_resolution_view(action_id)
        current_rows: list[dict[str, Any]] = []
        for row in view:
            material_id = int(row["material_id"])
            current_version_id = materials[material_id]["current_version_id"]
            if current_version_id is not None and int(row["version_id"]) == int(current_version_id):
                current_rows.append(row)
        complete_current = {
            int(row["material_id"]) for row in current_rows if row["active_result"] == "complete"
        }
        unresolved_batches = sorted({
            int(row["batch_no"]) for row in current_rows if int(row["material_id"]) not in complete_current
        })
        missing_required = [
            m["code"] for m in materials.values()
            if m["required"] and int(m["id"]) not in complete_current
        ]
        return unresolved_batches, missing_required
