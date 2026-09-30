from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# ---------------------------------------------------------------------------
# 存储结构
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS handover_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    manager TEXT NOT NULL,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handover_parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES handover_actions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('sender','receiver')),
    department TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    UNIQUE(action_id, role, department)
);

CREATE TABLE IF NOT EXISTS handover_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES handover_actions(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(action_id, code)
);

CREATE TABLE IF NOT EXISTS handover_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES handover_actions(id) ON DELETE CASCADE,
    batch_no INTEGER NOT NULL,
    sender_dept TEXT NOT NULL,
    receiver_dept TEXT NOT NULL,
    submission_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK(status IN ('submitted','objection','supplementing','confirmed','reopened')),
    submitted_at TEXT NOT NULL,
    deadline_at_submission TEXT,
    overdue_at_submission INTEGER NOT NULL DEFAULT 0 CHECK(overdue_at_submission IN (0,1)),
    confirmed_at TEXT,
    confirmed_by TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(action_id, batch_no)
);

CREATE TABLE IF NOT EXISTS handover_material_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES handover_materials(id) ON DELETE RESTRICT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    supersedes_version_id INTEGER REFERENCES handover_material_versions(id),
    title TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    byte_size INTEGER NOT NULL DEFAULT 0 CHECK(byte_size >= 0),
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    note TEXT NOT NULL DEFAULT '',
    revision_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(material_id, version_no)
);

CREATE TABLE IF NOT EXISTS handover_item_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES handover_materials(id) ON DELETE RESTRICT,
    version_id INTEGER NOT NULL REFERENCES handover_material_versions(id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','confirmed','objection')),
    confirmed_at TEXT,
    confirmed_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(batch_id, material_id)
);

CREATE TABLE IF NOT EXISTS handover_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES handover_materials(id) ON DELETE RESTRICT,
    version_id INTEGER NOT NULL REFERENCES handover_material_versions(id) ON DELETE RESTRICT,
    confirmed_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    superseded_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handover_confirmations_batch ON handover_confirmations(batch_id, id);
CREATE INDEX IF NOT EXISTS idx_handover_confirmations_version ON handover_confirmations(version_id);

CREATE TABLE IF NOT EXISTS handover_objections (    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    material_id INTEGER REFERENCES handover_materials(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('missing','sensitive','other')),
    reason TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT NOT NULL DEFAULT '',
    resolution_kind TEXT NOT NULL DEFAULT ''
        CHECK(resolution_kind IN ('','supplemented','withdrawn','revised'))
);

CREATE INDEX IF NOT EXISTS idx_handover_objections_batch ON handover_objections(batch_id, id);

CREATE TABLE IF NOT EXISTS handover_supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES handover_materials(id) ON DELETE RESTRICT,
    version_id INTEGER NOT NULL REFERENCES handover_material_versions(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handover_supplements_batch ON handover_supplements(batch_id, id);

CREATE TABLE IF NOT EXISTS handover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL,
    batch_id INTEGER,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handover_events_action ON handover_events(action_id, id);
CREATE INDEX IF NOT EXISTS idx_handover_events_batch ON handover_events(batch_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _now_storage() -> str:
    return to_storage(utc_now())


def _sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _bool(value: Any) -> bool:
    return bool(value) and value != 0


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class HandoverService:
    """行动材料交接：行动建档、分批交接、逐项确认、异议、补件与结案。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()

    # -- 工具 --------------------------------------------------------------

    def _event(
        self,
        connection: sqlite3.Connection,
        action_id: int | None,
        batch_id: int | None,
        event_type: str,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO handover_events(action_id,batch_id,event_type,actor,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                action_id,
                batch_id,
                event_type,
                actor,
                json.dumps(detail or {}, ensure_ascii=False),
                _now_storage(),
            ),
        )

    def _get_action(self, connection: sqlite3.Connection, action_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM handover_actions WHERE id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("行动不存在")
        return row

    def _get_batch(self, connection: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM handover_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("交接批次不存在")
        return row

    def _party_depts(self, connection: sqlite3.Connection, action_id: int, role: str) -> set[str]:
        rows = connection.execute(
            "SELECT department FROM handover_parties WHERE action_id=? AND role=?",
            (action_id, role),
        ).fetchall()
        return {row["department"] for row in rows}

    @staticmethod
    def _is_overdue(action: sqlite3.Row, at: Any | None = None) -> bool:
        deadline = from_storage(action["deadline"])
        if deadline is None:
            return False
        return (at or utc_now()) > deadline

    # -- 行动与清单 --------------------------------------------------------

    def create_action(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        materials = payload.get("materials") or []
        if not materials:
            raise ValidationError("行动至少要有一份材料清单")
        codes = [item["code"].strip() for item in materials]
        if len(codes) != len(set(codes)):
            raise ValidationError("材料编码在同一行动内必须唯一")
        senders = self._normalize_parties(payload.get("senders"), "sender")
        receivers = self._normalize_parties(payload.get("receivers"), "receiver")
        if not senders or not receivers:
            raise ValidationError("行动必须至少有一个移交方部门和一个接收方部门")

        now = _now_storage()
        digest_seed = {
            "name": payload["name"],
            "senders": senders,
            "receivers": receivers,
            "materials": [
                {
                    "code": item["code"].strip(),
                    "name": item["name"],
                    "sensitive": _bool(item.get("sensitive")),
                }
                for item in materials
            ],
        }
        code = payload.get("code") or "ACT-" + _sha256(digest_seed)[:12].upper()

        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO handover_actions(code,name,description,owner,manager,deadline,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        code,
                        payload["name"],
                        payload.get("description", ""),
                        payload["owner"],
                        payload["manager"],
                        payload.get("deadline"),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("行动编码已存在") from exc
            action_id = cursor.lastrowid
            for party in senders:
                connection.execute(
                    "INSERT INTO handover_parties(action_id,role,department,contact,joined_at)"
                    " VALUES(?,?,?,?,?)",
                    (action_id, "sender", party["department"], party.get("contact", ""), now),
                )
            for party in receivers:
                connection.execute(
                    "INSERT INTO handover_parties(action_id,role,department,contact,joined_at)"
                    " VALUES(?,?,?,?,?)",
                    (action_id, "receiver", party["department"], party.get("contact", ""), now),
                )
            for item in materials:
                connection.execute(
                    "INSERT INTO handover_materials(action_id,code,name,category,sensitive,required,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        action_id,
                        item["code"].strip(),
                        item["name"],
                        item.get("category", ""),
                        1 if _bool(item.get("sensitive")) else 0,
                        0 if _bool(item.get("optional")) else 1,
                        now,
                    ),
                )
            self._event(connection, action_id, None, "action.created", actor, {"code": code})
        return self.get_action(action_id)

    @staticmethod
    def _normalize_parties(parties: list[dict[str, Any]] | None, role: str) -> list[dict[str, Any]]:
        del role
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for party in parties or []:
            dept = party["department"].strip()
            if not dept or dept in seen:
                continue
            seen.add(dept)
            result.append({"department": dept, "contact": party.get("contact", "")})
        return result

    def add_party(
        self, action_id: int, role: str, department: str, contact: str, actor: str
    ) -> dict[str, Any]:
        now = _now_storage()
        with transaction(immediate=True) as connection:
            self._get_action(connection, action_id)
            try:
                connection.execute(
                    "INSERT INTO handover_parties(action_id,role,department,contact,joined_at)"
                    " VALUES(?,?,?,?,?)",
                    (action_id, role, department.strip(), contact, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该部门已在参与方名单中") from exc
            self._event(
                connection, action_id, None, "party.added", actor,
                {"role": role, "department": department.strip()},
            )
        return self.get_action(action_id)

    def add_material(
        self, action_id: int, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        now = _now_storage()
        with transaction(immediate=True) as connection:
            action = self._get_action(connection, action_id)
            if action["status"] == "closed":
                raise ConflictError("行动已结案，不能再登记材料")
            try:
                connection.execute(
                    "INSERT INTO handover_materials(action_id,code,name,category,sensitive,required,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        action_id,
                        payload["code"].strip(),
                        payload["name"],
                        payload.get("category", ""),
                        1 if _bool(payload.get("sensitive")) else 0,
                        0 if _bool(payload.get("optional")) else 1,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("材料编码已存在") from exc
            self._event(
                connection, action_id, None, "material.added", actor,
                {"code": payload["code"].strip()},
            )
        return self.get_action(action_id)

    # -- 分批交接 ----------------------------------------------------------

    def submit_batch(
        self,
        action_id: int,
        sender_dept: str,
        receiver_dept: str,
        items: list[dict[str, Any]],
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        if not items:
            raise ValidationError("交接批次至少包含一份材料")

        normalized: dict[str, dict[str, Any]] = {}
        for item in items:
            code = item["material_code"].strip()
            if code in normalized:
                raise ValidationError(f"同一批次中材料 {code} 出现多次")
            normalized[code] = item

        with transaction(immediate=True) as connection:
            action = self._get_action(connection, action_id)
            if action["status"] == "closed":
                raise ConflictError("行动已结案，不能再发起交接")
            if self._is_overdue(action):
                raise ConflictError("已超过行动截止日，不能发起新批次；逾期只允许对既有批次补件并登记原因")
            if sender_dept not in self._party_depts(connection, action_id, "sender"):
                raise ValidationError(f"移交方 {sender_dept} 不是该行动的参与部门")
            if receiver_dept not in self._party_depts(connection, action_id, "receiver"):
                raise ValidationError(f"接收方 {receiver_dept} 不是该行动的参与部门")

            materials = {
                row["code"]: row
                for row in connection.execute(
                    "SELECT * FROM handover_materials WHERE action_id=?", (action_id,)
                ).fetchall()
            }
            for code in normalized:
                if code not in materials:
                    raise ValidationError(f"材料 {code} 不在行动清单内")

            item_digest = [
                {
                    "material_code": code,
                    "content_ref": str(item.get("content_ref", "")).strip(),
                    "content_sha256": str(item.get("content_sha256", "")).strip().lower(),
                    "title": item.get("title") or materials[code]["name"],
                    "sensitive": _bool(item.get("sensitive", materials[code]["sensitive"])),
                }
                for code, item in sorted(normalized.items())
            ]
            payload_digest = _sha256(
                {
                    "sender": sender_dept,
                    "receiver": receiver_dept,
                    "items": item_digest,
                }
            )

            # 重复上传同一批材料：直接返回原批次，不生成新批次、不重置确认。
            existing = connection.execute(
                "SELECT * FROM handover_batches"
                " WHERE action_id=? AND sender_dept=? AND receiver_dept=? AND payload_digest=?",
                (action_id, sender_dept, receiver_dept, payload_digest),
            ).fetchone()
            if existing is not None:
                self._event(
                    connection, action_id, existing["id"], "batch.deduplicated", actor,
                    {"submission_key": existing["submission_key"]},
                )
                result = self.get_batch(existing["id"])
                result["deduplicated"] = True
                return result

            overdue = self._is_overdue(action)
            batch_no_row = connection.execute(
                "SELECT COALESCE(MAX(batch_no),0) AS max_no FROM handover_batches WHERE action_id=?",
                (action_id,),
            ).fetchone()
            batch_no = batch_no_row["max_no"] + 1
            submission_key = (
                f"{action['code']}-B{batch_no:03d}-{_sha256(payload_digest)[:8]}"
            )
            now = _now_storage()
            cursor = connection.execute(
                "INSERT INTO handover_batches(action_id,batch_no,sender_dept,receiver_dept,"
                "submission_key,payload_digest,status,submitted_at,deadline_at_submission,overdue_at_submission)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    batch_no,
                    sender_dept,
                    receiver_dept,
                    submission_key,
                    payload_digest,
                    "submitted",
                    now,
                    action["deadline"],
                    1 if overdue else 0,
                ),
            )
            batch_id = cursor.lastrowid

            for code, item in sorted(normalized.items()):
                material = materials[code]
                content_ref = str(item.get("content_ref", "")).strip()
                content_sha = str(item.get("content_sha256", "")).strip().lower()
                if not content_ref:
                    raise ValidationError(f"材料 {code} 缺少 content_ref（证据定位信息）")
                # 版本号按材料在整个行动内全局递增：新批次再次提交同一材料即形成新版本，
                # 并挂接到上一版本，旧版本记录永久保留可引用。
                last = connection.execute(
                    "SELECT id,version_no FROM handover_material_versions WHERE material_id=?"
                    " ORDER BY version_no DESC LIMIT 1",
                    (material["id"],),
                ).fetchone()
                version_no = (last["version_no"] + 1) if last else 1
                supersedes = last["id"] if last else None
                version_cursor = connection.execute(
                    "INSERT INTO handover_material_versions(material_id,batch_id,version_no,"
                    "supersedes_version_id,title,content_ref,content_sha256,media_type,byte_size,"
                    "sensitive,note,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        material["id"],
                        batch_id,
                        version_no,
                        supersedes,
                        item.get("title") or material["name"],
                        content_ref,
                        content_sha,
                        item.get("media_type", ""),
                        int(item.get("byte_size", 0) or 0),
                        1 if _bool(item.get("sensitive", material["sensitive"])) else 0,
                        item.get("note", ""),
                        actor,
                        now,
                    ),
                )
                version_id = version_cursor.lastrowid
                connection.execute(
                    "INSERT INTO handover_item_receipts(batch_id,material_id,version_id,state)"
                    " VALUES(?,?,?,'pending')",
                    (batch_id, material["id"], version_id),
                )

            self._event(
                connection, action_id, batch_id, "batch.submitted", actor,
                {"batch_no": batch_no, "items": len(normalized), "overdue": overdue,
                 "submission_key": submission_key},
            )
        return self.get_batch(batch_id)

    # -- 逐项确认 / 异议 ---------------------------------------------------

    def confirm_item(
        self,
        batch_id: int,
        material_code: str,
        actor: str,
        receiver_dept: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            batch = self._get_batch(connection, batch_id)
            self._assert_receiver(connection, batch, receiver_dept)
            receipt, material = self._find_receipt(connection, batch, material_code)
            if batch["closed_at"]:
                raise ConflictError("批次已结案，不能再变更确认")
            if receipt["state"] == "confirmed":
                raise ConflictError(f"材料 {material_code} 在本批次已确认，无需重复确认")
            if batch["status"] not in {"submitted", "objection", "supplementing", "reopened"}:
                raise ConflictError("当前批次状态不允许确认")
            now = _now_storage()
            connection.execute(
                "UPDATE handover_item_receipts SET state='confirmed',confirmed_at=?,confirmed_by=?,note=?"
                " WHERE id=?",
                (now, actor, note, receipt["id"]),
            )
            # 每次确认独立留痕；即使日后修订形成新版本，旧确认记录仍指向当时的版本。
            connection.execute(
                "INSERT INTO handover_confirmations(batch_id,material_id,version_id,"
                "confirmed_by,note,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, receipt["material_id"], receipt["version_id"], actor, note, now),
            )
            self._event(
                connection, batch["action_id"], batch_id, "item.confirmed", actor,
                {"material_code": material["code"], "version_id": receipt["version_id"]},
            )
            self._refresh_batch_state(connection, batch)
        return self.get_batch(batch_id)

    def raise_objection(
        self,
        batch_id: int,
        actor: str,
        kind: str,
        reason: str,
        material_code: str | None = None,
        receiver_dept: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"missing", "sensitive", "other"}:
            raise ValidationError("异议类型必须是 missing、sensitive 或 other")
        if not reason.strip():
            raise ValidationError("异议必须说明原因")
        with transaction(immediate=True) as connection:
            batch = self._get_batch(connection, batch_id)
            self._assert_receiver(connection, batch, receiver_dept)
            if batch["closed_at"]:
                raise ConflictError("批次已结案，不能再提出异议")
            if batch["status"] not in {"submitted", "objection", "supplementing", "reopened", "confirmed"}:
                raise ConflictError("当前批次状态不允许提出异议")
            material_id = None
            if material_code is not None:
                material = connection.execute(
                    "SELECT m.* FROM handover_materials m"
                    " WHERE m.action_id=? AND m.code=?",
                    (batch["action_id"], material_code.strip()),
                ).fetchone()
                if material is None:
                    raise NotFoundError(f"行动清单中没有材料 {material_code}")
                material_id = material["id"]
                receipt = connection.execute(
                    "SELECT * FROM handover_item_receipts WHERE batch_id=? AND material_id=?",
                    (batch_id, material_id),
                ).fetchone()
                if receipt is not None:
                    # 已在批次内的材料（含此前已确认的）：标记该项为异议，
                    # 待补件/修订后重新确认；历史确认在 handover_confirmations 中保留。
                    connection.execute(
                        "UPDATE handover_item_receipts SET state='objection' WHERE id=?",
                        (receipt["id"],),
                    )
            now = _now_storage()
            cursor = connection.execute(
                "INSERT INTO handover_objections(batch_id,material_id,kind,reason,raised_by,raised_at)"
                " VALUES(?,?,?,?,?,?)",
                (batch_id, material_id, kind, reason.strip(), actor, now),
            )
            connection.execute(
                "UPDATE handover_batches SET status='objection' WHERE id=?", (batch_id,)
            )
            self._event(
                connection, batch["action_id"], batch_id, "objection.raised", actor,
                {"objection_id": cursor.lastrowid, "kind": kind,
                 "material_code": material_code},
            )
        return self.get_batch(batch_id)

    def withdraw_objection(self, batch_id: int, objection_id: int, actor: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            batch = self._get_batch(connection, batch_id)
            objection = connection.execute(
                "SELECT * FROM handover_objections WHERE id=? AND batch_id=?",
                (objection_id, batch_id),
            ).fetchone()
            if objection is None:
                raise NotFoundError("异议不存在")
            if objection["resolved_at"]:
                raise ConflictError("异议已处理，不能撤回")
            now = _now_storage()
            connection.execute(
                "UPDATE handover_objections SET resolved_at=?,resolution_kind='withdrawn',resolution=?"
                " WHERE id=?",
                (now, f"由 {actor} 撤回", objection_id),
            )
            if objection["material_id"] is not None:
                still_open = connection.execute(
                    "SELECT COUNT(*) AS c FROM handover_objections"
                    " WHERE batch_id=? AND material_id=? AND resolved_at IS NULL",
                    (batch_id, objection["material_id"]),
                ).fetchone()["c"]
                if still_open == 0:
                    receipt = connection.execute(
                        "SELECT * FROM handover_item_receipts WHERE batch_id=? AND material_id=?",
                        (batch_id, objection["material_id"]),
                    ).fetchone()
                    if receipt is not None and receipt["state"] == "objection":
                        prior = connection.execute(
                            "SELECT * FROM handover_confirmations"
                            " WHERE batch_id=? AND material_id=? AND version_id=?"
                            " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
                            (batch_id, objection["material_id"], receipt["version_id"]),
                        ).fetchone()
                        if prior is not None:
                            # 异议撤回，且当前版本此前已确认：恢复确认态
                            connection.execute(
                                "UPDATE handover_item_receipts SET state='confirmed',"
                                " confirmed_at=?,confirmed_by=? WHERE id=?",
                                (prior["created_at"], prior["confirmed_by"], receipt["id"]),
                            )
                        else:
                            connection.execute(
                                "UPDATE handover_item_receipts SET state='pending'"
                                " WHERE id=?",
                                (receipt["id"],),
                            )
            self._event(
                connection, batch["action_id"], batch_id, "objection.withdrawn", actor,
                {"objection_id": objection_id},
            )
            self._refresh_batch_state(connection, batch)
        return self.get_batch(batch_id)

    def withdraw_objection_by_id(self, objection_id: int, actor: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT batch_id FROM handover_objections WHERE id=?", (objection_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("异议不存在")
        return self.withdraw_objection(row["batch_id"], objection_id, actor)

    # -- 补件 / 修订（新版本） ---------------------------------------------

    def supplement_batch(
        self,
        batch_id: int,
        items: list[dict[str, Any]],
        actor: str,
        reason: str,
        sender_dept: str | None = None,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("补件必须留下原因")
        if not items:
            raise ValidationError("补件至少包含一份材料")
        with transaction(immediate=True) as connection:
            batch = self._get_batch(connection, batch_id)
            self._assert_sender(connection, batch, sender_dept)
            action = self._get_action(connection, batch["action_id"])
            overdue = self._is_overdue(action)

            if action["status"] == "closed":
                raise ConflictError("行动已结案，不能补件")
            if batch["closed_at"]:
                raise ConflictError("批次已结案，不能再补件")
            if overdue:
                # 逾期：只允许补件，不允许整体重开；补件本身是允许的唯一通道。
                pass
            elif batch["status"] not in {"objection", "supplementing", "submitted", "reopened"}:
                raise ConflictError("批次已确认完成，截止日前应由负责人重开后再补件")

            materials = {
                row["code"]: row
                for row in connection.execute(
                    "SELECT * FROM handover_materials WHERE action_id=?", (action["id"],)
                ).fetchall()
            }
            now = _now_storage()
            for item in items:
                code = item["material_code"].strip()
                material = materials.get(code)
                if material is None:
                    raise ValidationError(f"材料 {code} 不在行动清单内")
                content_ref = str(item.get("content_ref", "")).strip()
                if not content_ref:
                    raise ValidationError(f"材料 {code} 缺少 content_ref（证据定位信息）")

                receipt = connection.execute(
                    "SELECT * FROM handover_item_receipts WHERE batch_id=? AND material_id=?",
                    (batch_id, material["id"]),
                ).fetchone()

                if receipt is None:
                    # 缺件补齐：把材料补进本批次，首版本。
                    version_no = 1
                    supersedes = None
                else:
                    # 已有材料发生修订：必须形成新版本，旧版本记录保留且旧确认不被改写。
                    last_version = connection.execute(
                        "SELECT * FROM handover_material_versions WHERE id=?",
                        (receipt["version_id"],),
                    ).fetchone()
                    version_no = last_version["version_no"] + 1
                    supersedes = last_version["id"]
                    if (
                        last_version["content_ref"] == content_ref
                        and last_version["content_sha256"] == str(item.get("content_sha256", "")).strip().lower()
                    ):
                        raise ConflictError(f"材料 {code} 内容与当前版本一致，无需新建版本")

                version_cursor = connection.execute(
                    "INSERT INTO handover_material_versions(material_id,batch_id,version_no,"
                    "supersedes_version_id,title,content_ref,content_sha256,media_type,byte_size,"
                    "sensitive,note,revision_reason,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        material["id"],
                        batch_id,
                        version_no,
                        supersedes,
                        item.get("title") or material["name"],
                        content_ref,
                        str(item.get("content_sha256", "")).strip().lower(),
                        item.get("media_type", ""),
                        int(item.get("byte_size", 0) or 0),
                        1 if _bool(item.get("sensitive", material["sensitive"])) else 0,
                        item.get("note", ""),
                        reason.strip(),
                        actor,
                        now,
                    ),
                )
                version_id = version_cursor.lastrowid

                open_objection = connection.execute(
                    "SELECT id FROM handover_objections"
                    " WHERE batch_id=? AND material_id=? AND resolved_at IS NULL"
                    " ORDER BY id LIMIT 1",
                    (batch_id, material["id"]),
                ).fetchone()
                resolution_kind = "revised" if receipt is not None else "supplemented"

                if receipt is None:
                    connection.execute(
                        "INSERT INTO handover_item_receipts(batch_id,material_id,version_id,state)"
                        " VALUES(?,?,?,'pending')",
                        (batch_id, material["id"], version_id),
                    )
                else:
                    # 旧版本引用保留：只更新当前指针；任何修订都要求接收方重新确认，
                    # 旧版本及其历史确认仍可在版本链与时间线中查询。
                    was_confirmed = receipt["state"] == "confirmed"
                    connection.execute(
                        "UPDATE handover_item_receipts SET version_id=?,state='pending',"
                        " confirmed_at=NULL,confirmed_by=''"
                        " WHERE id=?",
                        (version_id, receipt["id"]),
                    )
                    connection.execute(
                        "UPDATE handover_confirmations SET superseded_at=COALESCE(superseded_at,?)"
                        " WHERE batch_id=? AND material_id=? AND superseded_at IS NULL",
                        (now, batch_id, material["id"]),
                    )
                    if was_confirmed:
                        self._event(
                            connection, action["id"], batch_id, "item.reconfirmed_required",
                            actor, {"material_code": code, "old_version_id": supersedes,
                                    "new_version_id": version_id},
                        )

                connection.execute(
                    "INSERT INTO handover_supplements(batch_id,material_id,version_id,reason,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (batch_id, material["id"], version_id, reason.strip(), actor, now),
                )
                if open_objection is not None:
                    connection.execute(
                        "UPDATE handover_objections SET resolved_at=?,resolution_kind=?,resolution=?"
                        " WHERE id=?",
                        (now, resolution_kind, f"补件/修订：{reason.strip()}", open_objection["id"]),
                    )

            connection.execute(
                "UPDATE handover_batches SET status='supplementing' WHERE id=?", (batch_id,)
            )
            self._event(
                connection, action["id"], batch_id, "batch.supplemented", actor,
                {"items": len(items), "reason": reason.strip(), "overdue": overdue},
            )
            self._refresh_batch_state(connection, batch)
        return self.get_batch(batch_id)

    # -- 负责人重开 / 结案 -------------------------------------------------

    def reopen_batch(
        self, batch_id: int, manager: str, reason: str, actor: str
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("重开必须说明原因")
        with transaction(immediate=True) as connection:
            batch = self._get_batch(connection, batch_id)
            action = self._get_action(connection, batch["action_id"])
            if manager != action["manager"]:
                raise ConflictError("只有行动负责人可以重开交接")
            if self._is_overdue(action):
                raise ConflictError("已超过行动截止日，不能重开；逾期只允许补件并登记原因")
            if batch["closed_at"]:
                raise ConflictError("批次已结案，不能重开")
            if batch["status"] != "confirmed":
                raise ConflictError("只有已确认完成的批次可以重开")
            connection.execute(
                "UPDATE handover_batches SET status='reopened',reopen_count=reopen_count+1 WHERE id=?",
                (batch_id,),
            )
            self._event(
                connection, action["id"], batch_id, "batch.reopened", actor,
                {"manager": manager, "reason": reason.strip()},
            )
        return self.get_batch(batch_id)

    def close_batch(self, batch_id: int, actor: str, receiver_dept: str | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            batch = self._get_batch(connection, batch_id)
            self._assert_receiver(connection, batch, receiver_dept)
            pending = connection.execute(
                "SELECT COUNT(*) AS c FROM handover_item_receipts WHERE batch_id=? AND state!='confirmed'",
                (batch_id,),
            ).fetchone()["c"]
            open_objections = connection.execute(
                "SELECT COUNT(*) AS c FROM handover_objections WHERE batch_id=? AND resolved_at IS NULL",
                (batch_id,),
            ).fetchone()["c"]
            if pending or open_objections:
                raise ConflictError(
                    f"批次尚有 {pending} 项未确认、{open_objections} 条异议未处理，不能结案"
                )
            if batch["status"] not in {"confirmed", "reopened"}:
                raise ConflictError("批次尚未达到确认完成状态")
            now = _now_storage()
            connection.execute(
                "UPDATE handover_batches SET closed_at=COALESCE(closed_at,?) WHERE id=?",
                (now, batch_id),
            )
            self._event(
                connection, batch["action_id"], batch_id, "batch.closed", actor, {}
            )
        return self.get_batch(batch_id)

    def close_action(self, action_id: int, manager: str, actor: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            action = self._get_action(connection, action_id)
            if manager != action["manager"]:
                raise ConflictError("只有行动负责人可以结案")
            if action["status"] == "closed":
                raise ConflictError("行动已经结案")
            open_rows = connection.execute(
                "SELECT b.* FROM handover_batches b"
                " WHERE b.action_id=? AND b.status NOT IN ('confirmed','reopened')",
                (action_id,),
            ).fetchall()
            unfinished = [
                row for row in open_rows
                if connection.execute(
                    "SELECT COUNT(*) AS c FROM handover_item_receipts"
                    " WHERE batch_id=? AND state!='confirmed'", (row["id"],)
                ).fetchone()["c"] > 0
                or connection.execute(
                    "SELECT COUNT(*) AS c FROM handover_objections"
                    " WHERE batch_id=? AND resolved_at IS NULL", (row["id"],)
                ).fetchone()["c"] > 0
            ]
            if unfinished:
                raise ConflictError("尚有批次未完成确认，不能结案整个行动")
            now = _now_storage()
            connection.execute(
                "UPDATE handover_actions SET status='closed',closed_at=?,updated_at=? WHERE id=?",
                (now, now, action_id),
            )
            connection.execute(
                "UPDATE handover_batches SET closed_at=COALESCE(closed_at,?) WHERE action_id=? AND closed_at IS NULL",
                (now, action_id),
            )
            self._event(connection, action_id, None, "action.closed", actor, {"manager": manager})
        return self.get_action(action_id)

    # -- 查询 --------------------------------------------------------------

    def list_actions(self, status: str | None = None) -> dict[str, Any]:
        ensure_schema()
        sql = "SELECT * FROM handover_actions"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        rows = self.connection.execute(sql, params).fetchall()
        return {"items": [self._summary(row) for row in rows]}

    def get_action(self, action_id: int) -> dict[str, Any]:
        ensure_schema()
        action = self.connection.execute(
            "SELECT * FROM handover_actions WHERE id=?", (action_id,)
        ).fetchone()
        if action is None:
            raise NotFoundError("行动不存在")
        result = self._summary(action)
        result["parties"] = self._parties(action_id)
        result["materials"] = [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM handover_materials WHERE action_id=? ORDER BY id", (action_id,)
            ).fetchall()
        ]
        result["batches"] = [
            self._batch_summary(row)
            for row in self.connection.execute(
                "SELECT * FROM handover_batches WHERE action_id=? ORDER BY batch_no", (action_id,)
            ).fetchall()
        ]
        result["timeline"] = self._timeline(action_id=action_id)
        return result

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        ensure_schema()
        batch = self.connection.execute(
            "SELECT * FROM handover_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFoundError("交接批次不存在")
        result = self._batch_summary(batch)
        result["items"] = self._batch_items(batch)
        result["confirmations"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT c.*, m.code AS material_code, m.name AS material_name,"
                " v.version_no, v.content_ref"
                " FROM handover_confirmations c"
                " JOIN handover_materials m ON m.id=c.material_id"
                " JOIN handover_material_versions v ON v.id=c.version_id"
                " WHERE c.batch_id=? ORDER BY c.id",
                (batch_id,),
            ).fetchall()
        ]
        result["objections"] = [
            self._objection_view(row)
            for row in self.connection.execute(
                "SELECT * FROM handover_objections WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]
        result["supplements"] = [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM handover_supplements WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]
        result["timeline"] = self._timeline(batch_id=batch_id)
        return result

    def get_material_versions(self, action_id: int, material_code: str) -> dict[str, Any]:
        material = self.connection.execute(
            "SELECT * FROM handover_materials WHERE action_id=? AND code=?",
            (action_id, material_code.strip()),
        ).fetchone()
        if material is None:
            raise NotFoundError("材料不存在")
        rows = self.connection.execute(
            "SELECT v.*, b.batch_no, b.submission_key, b.sender_dept, b.receiver_dept,"
            " r.confirmed_at, r.confirmed_by, r.state AS receipt_state"
            " FROM handover_material_versions v"
            " JOIN handover_batches b ON b.id=v.batch_id"
            " LEFT JOIN handover_item_receipts r ON r.version_id=v.id"
            " WHERE v.material_id=? ORDER BY v.version_no",
            (material["id"],),
        ).fetchall()
        return {
            "material": dict(material),
            "versions": [dict(row) for row in rows],
        }

    # -- 视图组装 ----------------------------------------------------------

    def _summary(self, action: sqlite3.Row) -> dict[str, Any]:
        data = dict(action)
        data["overdue"] = self._is_overdue(action) and action["status"] != "closed"
        counts = self.connection.execute(
            "SELECT status, COUNT(*) AS c FROM handover_batches WHERE action_id=? GROUP BY status",
            (action["id"],),
        ).fetchall()
        data["batch_counts"] = {row["status"]: row["c"] for row in counts}
        return data

    def _parties(self, action_id: int) -> dict[str, list[dict[str, Any]]]:
        rows = self.connection.execute(
            "SELECT * FROM handover_parties WHERE action_id=? ORDER BY role,id", (action_id,)
        ).fetchall()
        return {
            "senders": [dict(row) for row in rows if row["role"] == "sender"],
            "receivers": [dict(row) for row in rows if row["role"] == "receiver"],
        }

    def _batch_summary(self, batch: sqlite3.Row) -> dict[str, Any]:
        data = dict(batch)
        total = self.connection.execute(
            "SELECT COUNT(*) AS c FROM handover_item_receipts WHERE batch_id=?", (batch["id"],)
        ).fetchone()["c"]
        confirmed = self.connection.execute(
            "SELECT COUNT(*) AS c FROM handover_item_receipts WHERE batch_id=? AND state='confirmed'",
            (batch["id"],),
        ).fetchone()["c"]
        objections = self.connection.execute(
            "SELECT COUNT(*) AS c FROM handover_objections WHERE batch_id=? AND resolved_at IS NULL",
            (batch["id"],),
        ).fetchone()["c"]
        data["item_total"] = total
        data["item_confirmed"] = confirmed
        data["open_objections"] = objections
        data["fully_confirmed"] = total > 0 and confirmed == total and objections == 0
        return data

    def _batch_items(self, batch: sqlite3.Row) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT r.id AS receipt_id,r.state,r.confirmed_at,r.confirmed_by,r.note AS receipt_note,"
            " m.id AS material_id,m.code AS material_code,m.name AS material_name,"
            " m.sensitive AS material_sensitive,m.required AS material_required,"
            " v.id AS version_id,v.version_no,v.supersedes_version_id,v.title,v.content_ref,"
            " v.content_sha256,v.media_type,v.byte_size,v.sensitive AS version_sensitive,"
            " v.revision_reason,v.created_by,v.created_at"
            " FROM handover_item_receipts r"
            " JOIN handover_materials m ON m.id=r.material_id"
            " JOIN handover_material_versions v ON v.id=r.version_id"
            " WHERE r.batch_id=? ORDER BY m.code",
            (batch["id"],),
        ).fetchall()
        return [dict(row) for row in rows]

    def _objection_view(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        if row["material_id"]:
            material = self.connection.execute(
                "SELECT code,name FROM handover_materials WHERE id=?", (row["material_id"],)
            ).fetchone()
            data["material_code"] = material["code"] if material else None
            data["material_name"] = material["name"] if material else None
        else:
            data["material_code"] = None
            data["material_name"] = None
        return data

    def _timeline(self, *, action_id: int | None = None, batch_id: int | None = None) -> list[dict[str, Any]]:
        if batch_id is not None:
            rows = self.connection.execute(
                "SELECT * FROM handover_events WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM handover_events WHERE action_id=? AND batch_id IS NULL ORDER BY id",
                (action_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(row["detail_json"] or "{}")
            del item["detail_json"]
            result.append(item)
        return result

    # -- 内部规则 ----------------------------------------------------------

    def _assert_receiver(
        self, connection: sqlite3.Connection, batch: sqlite3.Row, receiver_dept: str | None
    ) -> None:
        if receiver_dept is None:
            receiver_dept = batch["receiver_dept"]
        allowed = self._party_depts(connection, batch["action_id"], "receiver")
        if receiver_dept not in allowed:
            raise ConflictError(f"接收方部门 {receiver_dept} 无权操作该行动的接收确认")
        if receiver_dept != batch["receiver_dept"]:
            raise ConflictError(
                f"本批次的接收方是 {batch['receiver_dept']}，{receiver_dept} 不能代为确认"
            )

    def _assert_sender(
        self, connection: sqlite3.Connection, batch: sqlite3.Row, sender_dept: str | None
    ) -> None:
        if sender_dept is None:
            sender_dept = batch["sender_dept"]
        allowed = self._party_depts(connection, batch["action_id"], "sender")
        if sender_dept not in allowed:
            raise ConflictError(f"移交方部门 {sender_dept} 无权向该行动提交材料")
        if sender_dept != batch["sender_dept"]:
            raise ConflictError(
                f"本批次的移交方是 {batch['sender_dept']}，{sender_dept} 不能代为补件"
            )

    def _find_receipt(
        self, connection: sqlite3.Connection, batch: sqlite3.Row, material_code: str
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        row = connection.execute(
            "SELECT r.*, m.code, m.name, m.sensitive, m.required"
            " FROM handover_item_receipts r"
            " JOIN handover_materials m ON m.id=r.material_id"
            " WHERE r.batch_id=? AND m.code=?",
            (batch["id"], material_code.strip()),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"批次中没有材料 {material_code}")
        material = connection.execute(
            "SELECT * FROM handover_materials WHERE id=?", (row["material_id"],)
        ).fetchone()
        return row, material

    def _refresh_batch_state(
        self, connection: sqlite3.Connection, batch: sqlite3.Row
    ) -> None:
        """根据逐项收据与异议重算批次状态，但不覆盖已结案/重开语义。"""
        if batch["status"] == "confirmed" and batch["closed_at"]:
            return
        pending = connection.execute(
            "SELECT COUNT(*) AS c FROM handover_item_receipts WHERE batch_id=? AND state!='confirmed'",
            (batch["id"],),
        ).fetchone()["c"]
        open_objections = connection.execute(
            "SELECT COUNT(*) AS c FROM handover_objections WHERE batch_id=? AND resolved_at IS NULL",
            (batch["id"],),
        ).fetchone()["c"]
        if pending == 0 and open_objections == 0:
            connection.execute(
                "UPDATE handover_batches SET status='confirmed',"
                " confirmed_at=COALESCE(confirmed_at,?) WHERE id=?",
                (_now_storage(), batch["id"]),
            )
        else:
            current = connection.execute(
                "SELECT status,reopen_count FROM handover_batches WHERE id=?", (batch["id"],)
            ).fetchone()
            if current["status"] == "confirmed":
                connection.execute(
                    "UPDATE handover_batches SET status='reopened' WHERE id=?", (batch["id"],)
                )
            elif current["status"] == "objection" and open_objections == 0:
                # 异议全部撤回/处理，材料尚未重新确认完：回到交接中状态
                connection.execute(
                    "UPDATE handover_batches SET status=? WHERE id=?",
                    ("reopened" if current["reopen_count"] > 0 else "submitted", batch["id"]),
                )
