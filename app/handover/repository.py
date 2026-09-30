from __future__ import annotations

import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS protection_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS action_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES protection_actions(id) ON DELETE CASCADE,
    department TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('移交方','接收方','协办方')),
    contact TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    UNIQUE(action_id, department, role)
);
CREATE TABLE IF NOT EXISTS action_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES protection_actions(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    responsible_department TEXT NOT NULL,
    is_sensitive INTEGER NOT NULL DEFAULT 0 CHECK(is_sensitive IN (0,1)),
    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
    current_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(action_id, code)
);
CREATE TABLE IF NOT EXISTS material_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES action_materials(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    batch_id INTEGER REFERENCES handover_batches(id),
    supersedes_version_id INTEGER REFERENCES material_versions(id),
    filename TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    change_note TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(material_id, version_no)
);
CREATE TABLE IF NOT EXISTS handover_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES protection_actions(id) ON DELETE CASCADE,
    batch_no INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'handover' CHECK(kind IN ('handover','supplement')),
    idempotency_key TEXT,
    sender_department TEXT NOT NULL,
    receiver_department TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    overdue_reason TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    reopen_cutoff_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','disputed','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(action_id, batch_no)
);
CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES action_materials(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES material_versions(id),
    UNIQUE(batch_id, material_id)
);
CREATE TABLE IF NOT EXISTS batch_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES batch_items(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES action_materials(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES material_versions(id),
    receiver_department TEXT NOT NULL,
    confirmer TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('complete','missing','sensitive_objection')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES protection_actions(id),
    batch_id INTEGER,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handover_batches_action ON handover_batches(action_id, batch_no);
CREATE INDEX IF NOT EXISTS idx_batch_confirmations_batch ON batch_confirmations(batch_id, id);
CREATE INDEX IF NOT EXISTS idx_material_versions_material ON material_versions(material_id, version_no);
CREATE INDEX IF NOT EXISTS idx_handover_events_action ON handover_events(action_id, id);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


class HandoverRepository:
    """封装行动材料交接领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ----- 行动 -----
    def action_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM protection_actions WHERE code=?", (code,)).fetchone()

    def action_by_id(self, action_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM protection_actions WHERE id=?", (action_id,)).fetchone()

    def create_action(self, *, code: str, name: str, description: str, owner: str, deadline: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO protection_actions(code,name,description,owner,deadline,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?,?)",
            (code, name, description, owner, deadline, created_by, now, now),
        )
        return int(cursor.lastrowid)

    def touch_action(self, action_id: int, now: str) -> None:
        self.connection.execute("UPDATE protection_actions SET updated_at=? WHERE id=?", (now, action_id))

    def close_action(self, action_id: int, now: str) -> None:
        self.connection.execute("UPDATE protection_actions SET status='closed',closed_at=?,updated_at=? WHERE id=?", (now, now, action_id))

    def reopen_action(self, action_id: int, now: str) -> None:
        self.connection.execute("UPDATE protection_actions SET status='open',closed_at=NULL,updated_at=? WHERE id=?", (now, action_id))

    # ----- 参与部门 -----
    def add_participant(self, *, action_id: int, department: str, role: str, contact: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO action_participants(action_id,department,role,contact,joined_at) VALUES(?,?,?,?,?)",
            (action_id, department, role, contact, now),
        )

    def participants(self, action_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,department,role,contact,joined_at FROM action_participants WHERE action_id=? ORDER BY id",
            (action_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def participant_roles(self, action_id: int, department: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT role FROM action_participants WHERE action_id=? AND department=?",
            (action_id, department),
        ).fetchall()
        return {str(row["role"]) for row in rows}

    def departments(self, action_id: int) -> set[str]:
        rows = self.connection.execute("SELECT DISTINCT department FROM action_participants WHERE action_id=?", (action_id,)).fetchall()
        return {str(row["department"]) for row in rows}

    # ----- 材料清单 -----
    def add_material(self, *, action_id: int, code: str, title: str, responsible_department: str, is_sensitive: bool, required: bool, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO action_materials(action_id,code,title,responsible_department,is_sensitive,required,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (action_id, code, title, responsible_department, 1 if is_sensitive else 0, 1 if required else 0, now, now),
        )
        return int(cursor.lastrowid)

    def material_by_code(self, action_id: int, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM action_materials WHERE action_id=? AND code=?", (action_id, code)).fetchone()

    def material_by_id(self, material_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM action_materials WHERE id=?", (material_id,)).fetchone()

    def materials(self, action_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM action_materials WHERE action_id=? ORDER BY id", (action_id,)).fetchall()
        return [dict(row) for row in rows]

    def set_current_version(self, material_id: int, version_id: int, now: str) -> None:
        self.connection.execute("UPDATE action_materials SET current_version_id=?,updated_at=? WHERE id=?", (version_id, now, material_id))

    # ----- 材料版本 -----
    def add_version(self, *, material_id: int, version_no: int, batch_id: int | None, supersedes_version_id: int | None, filename: str, content_digest: str, size_bytes: int, change_note: str, uploaded_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO material_versions(material_id,version_no,batch_id,supersedes_version_id,filename,content_digest,size_bytes,change_note,uploaded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (material_id, version_no, batch_id, supersedes_version_id, filename, content_digest, size_bytes, change_note, uploaded_by, now),
        )
        return int(cursor.lastrowid)

    def version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM material_versions WHERE id=?", (version_id,)).fetchone()

    def versions_of_material(self, material_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM material_versions WHERE material_id=? ORDER BY version_no", (material_id,)).fetchall()
        return [dict(row) for row in rows]

    def latest_digest(self, material_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT content_digest FROM material_versions WHERE material_id=? ORDER BY version_no DESC LIMIT 1",
            (material_id,),
        ).fetchone()
        return None if row is None else str(row["content_digest"])

    # ----- 交接批次 -----
    def next_batch_no(self, action_id: int) -> int:
        row = self.connection.execute("SELECT COALESCE(MAX(batch_no),0)+1 AS next FROM handover_batches WHERE action_id=?", (action_id,)).fetchone()
        return int(row["next"])

    def batch_by_id(self, batch_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM handover_batches WHERE id=?", (batch_id,)).fetchone()

    def batch_by_idempotency_key(self, action_id: int, key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM handover_batches WHERE action_id=? AND idempotency_key=?",
            (action_id, key),
        ).fetchone()

    def find_batch_by_fingerprint(self, action_id: int, fingerprint: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM handover_batches WHERE action_id=? AND fingerprint=? ORDER BY id DESC LIMIT 1",
            (action_id, fingerprint),
        ).fetchone()

    def create_batch(self, *, action_id: int, batch_no: int, kind: str, idempotency_key: str | None, sender_department: str, receiver_department: str, note: str, overdue_reason: str, fingerprint: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO handover_batches(action_id,batch_no,kind,idempotency_key,sender_department,receiver_department,note,overdue_reason,fingerprint,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (action_id, batch_no, kind, idempotency_key, sender_department, receiver_department, note, overdue_reason, fingerprint, created_by, now),
        )
        return int(cursor.lastrowid)

    def update_batch_status(self, batch_id: int, status: str) -> None:
        self.connection.execute("UPDATE handover_batches SET status=? WHERE id=?", (status, batch_id))

    def batches(self, action_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM handover_batches WHERE action_id=? ORDER BY batch_no", (action_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_batch_item(self, *, batch_id: int, material_id: int, version_id: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO batch_items(batch_id,material_id,version_id) VALUES(?,?,?)",
            (batch_id, material_id, version_id),
        )
        return int(cursor.lastrowid)

    def items(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT i.id AS item_id,i.batch_id,i.material_id,i.version_id,
                   m.code AS material_code,m.title AS material_title,m.responsible_department,
                   v.version_no,v.filename,v.content_digest,v.supersedes_version_id
            FROM batch_items i
            JOIN action_materials m ON m.id=i.material_id
            JOIN material_versions v ON v.id=i.version_id
            WHERE i.batch_id=? ORDER BY i.id
            """,
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----- 逐项确认 -----
    def add_confirmation(self, *, batch_id: int, item_id: int, material_id: int, version_id: int, receiver_department: str, confirmer: str, result: str, note: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO batch_confirmations(batch_id,item_id,material_id,version_id,receiver_department,confirmer,result,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (batch_id, item_id, material_id, version_id, receiver_department, confirmer, result, note, now),
        )

    def confirmations(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT c.*, m.code AS material_code, v.version_no
            FROM batch_confirmations c
            JOIN action_materials m ON m.id=c.material_id
            JOIN material_versions v ON v.id=c.version_id
            WHERE c.batch_id=? ORDER BY c.id
            """,
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_confirmation_map(self, batch_id: int, cutoff_id: int = 0) -> dict[int, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT c.* FROM batch_confirmations c
            JOIN (SELECT item_id,MAX(id) AS max_id FROM batch_confirmations
                  WHERE batch_id=? AND id>? GROUP BY item_id) latest
              ON latest.max_id=c.id
            """,
            (batch_id, cutoff_id),
        ).fetchall()
        return {int(row["item_id"]): dict(row) for row in rows}

    def max_confirmation_id(self, batch_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM batch_confirmations WHERE batch_id=?", (batch_id,),
        ).fetchone()[0])

    def mark_reopened(self, batch_id: int, cutoff_id: int) -> None:
        self.connection.execute(
            "UPDATE handover_batches SET status='pending',reopen_cutoff_id=? WHERE id=?",
            (cutoff_id, batch_id),
        )

    def version_has_active_complete(self, material_id: int, version_id: int) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM batch_confirmations c
            JOIN handover_batches b ON b.id=c.batch_id
            WHERE c.material_id=? AND c.version_id=? AND c.result='complete'
              AND c.id>b.reopen_cutoff_id
            LIMIT 1
            """,
            (material_id, version_id),
        ).fetchone()
        return row is not None

    def item_resolution_view(self, action_id: int) -> list[dict[str, Any]]:
        """每个批次条目与其重开分界之后的最新有效确认，并标出材料当前版本。"""
        rows = self.connection.execute(
            """
            SELECT i.id AS item_id,i.batch_id,b.batch_no,i.material_id,i.version_id,
                   m.code AS material_code,m.required AS material_required,
                   m.current_version_id,b.reopen_cutoff_id,
                   (SELECT c.result FROM batch_confirmations c
                      WHERE c.item_id=i.id AND c.id>b.reopen_cutoff_id
                      ORDER BY c.id DESC LIMIT 1) AS active_result,
                   (SELECT c.note FROM batch_confirmations c
                      WHERE c.item_id=i.id AND c.id>b.reopen_cutoff_id
                      ORDER BY c.id DESC LIMIT 1) AS active_note
            FROM batch_items i
            JOIN handover_batches b ON b.id=i.batch_id
            JOIN action_materials m ON m.id=i.material_id
            WHERE b.action_id=?
            """,
            (action_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----- 事件 -----
    def add_event(self, *, action_id: int, batch_id: int | None, actor: str, action: str, detail: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO handover_events(action_id,batch_id,actor,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (action_id, batch_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, action_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM handover_events WHERE action_id=? ORDER BY id", (action_id,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            result.append(item)
        return result
