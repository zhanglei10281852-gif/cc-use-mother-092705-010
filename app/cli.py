from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_handover_demo() -> int:
    import uuid

    code = f"DEMO-{uuid.uuid4().hex[:8]}"
    d_garden, d_volunteer, d_research = "园林巡护队", "志愿者团队", "科研团队"
    digest1, digest2, digest3 = "sha256:" + "1" * 58, "sha256:" + "2" * 58, "sha256:" + "3" * 58
    with TestClient(app) as client:
        action = client.post("/api/handover/actions?actor=李保护", json={
            "code": code,
            "name": "九月湿地巡护证据交接",
            "owner": "李保护",
            "deadline": "2099-01-01T00:00:00+00:00",
            "participants": [
                {"department": d_garden, "role": "移交方", "contact": "王队"},
                {"department": d_volunteer, "role": "协办方", "contact": "赵组"},
                {"department": d_research, "role": "接收方", "contact": "周老师"},
            ],
            "materials": [
                {"code": "PATROL-LOG", "title": "巡护路线记录本", "responsible_department": d_garden, "required": True},
                {"code": "CAMERA-TRAP", "title": "红外相机影像清单", "responsible_department": d_volunteer, "is_sensitive": True, "required": True},
            ],
        })
        assert action.status_code == 201, action.text

        b1 = client.post(f"/api/handover/actions/{code}/batches?actor=王队", json={
            "sender_department": d_garden, "receiver_department": d_research,
            "items": [{"material_code": "PATROL-LOG", "filename": "patrol-v1.zip", "content_digest": digest1, "size_bytes": 1024}],
        }).json()
        client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                    json={"material_code": "PATROL-LOG", "receiver_department": d_research,
                          "confirmer": "周老师", "result": "missing", "note": "缺少 9 月 18 日签名页"})

        b2 = client.post(f"/api/handover/actions/{code}/batches?actor=赵组", json={
            "sender_department": d_volunteer, "receiver_department": d_research,
            "items": [{"material_code": "CAMERA-TRAP", "filename": "camera-v1.zip", "content_digest": digest2, "size_bytes": 4096}],
        }).json()
        client.post(f"/api/handover/batches/{b2['id']}/confirmations",
                    json={"material_code": "CAMERA-TRAP", "receiver_department": d_research,
                          "confirmer": "周老师", "result": "sensitive_objection", "note": "志愿者人脸未脱敏"})

        # 重复上传：应返回原批次
        duplicate = client.post(f"/api/handover/actions/{code}/batches?actor=王队", json={
            "sender_department": d_garden, "receiver_department": d_research,
            "items": [{"material_code": "PATROL-LOG", "filename": "patrol-v1.zip", "content_digest": digest1, "size_bytes": 1024}],
        }).json()
        assert duplicate["id"] == b1["id"] and duplicate["reused"] is True

        # 重开批次 1 并补齐日志
        client.post(f"/api/handover/batches/{b1['id']}/reopen",
                    json={"actor": "李保护", "reason": "缺签名页，允许补正"})
        client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                    json={"material_code": "PATROL-LOG", "receiver_department": d_research,
                          "confirmer": "周老师", "result": "complete", "note": "签名页补齐，确认完整"})

        # 补件批次：脱敏后影像新版本
        supplement = client.post(f"/api/handover/actions/{code}/batches?actor=赵组", json={
            "kind": "supplement", "sender_department": d_volunteer, "receiver_department": d_research,
            "note": "按异议完成人脸脱敏后补件",
            "items": [{"material_code": "CAMERA-TRAP", "filename": "camera-v2-desensitized.zip",
                       "content_digest": digest3, "size_bytes": 4000, "change_note": "人脸脱敏"}],
        }).json()
        client.post(f"/api/handover/batches/{supplement['id']}/confirmations",
                    json={"material_code": "CAMERA-TRAP", "receiver_department": d_research,
                          "confirmer": "周老师", "result": "complete", "note": "脱敏符合要求，确认完整"})

        closed = client.post(f"/api/handover/actions/{code}/close?actor=李保护")
        assert closed.status_code == 200, closed.text
        detail = client.get(f"/api/handover/actions/{code}").json()

    lines = ["=" * 68, f"保护行动 {code}《{detail['name']}》  负责人：{detail['owner']}  状态：{detail['status']}", ""]
    lines.append("【责任边界】参与部门")
    for participant in detail["participants"]:
        lines.append(f"  - {participant['department']:<8} 角色：{participant['role']}  联系人：{participant['contact']}")
    lines.append("")
    lines.append("【材料清单与版本关系】")
    for material in detail["materials"]:
        chain = " -> ".join(f"v{v['version_no']}" for v in material["version_chain"])
        lines.append(f"  - {material['code']}《{material['title']}》 责任部门：{material['responsible_department']}"
                     f"{'（敏感）' if material['is_sensitive'] else ''} 当前版本：v{material['current_version_no']} 版本链：{chain}")
    lines.append("")
    lines.append("【交接批次与每次确认】")
    for batch in detail["batches"]:
        kind_label = "补件" if batch["kind"] == "supplement" else "交接"
        lines.append(f"  批次 #{batch['batch_no']}（{kind_label}）{batch['sender_department']} -> {batch['receiver_department']}"
                     f"  状态：{batch['status']}  未决异议：{batch['active_objection_count']}  已解决异议：{batch['resolved_objection_count']}")
        for confirmation in batch["all_confirmations"]:
            result_label = {"complete": "确认完整", "missing": "缺件异议", "sensitive_objection": "敏感异议"}[confirmation["result"]]
            lines.append(f"      · {confirmation['material_code']} v{confirmation['version_no']} {result_label}"
                         f" by {confirmation['confirmer']}（{confirmation['created_at']}）：{confirmation['note']}")
    lines.append("=" * 68)
    report = "\n".join(lines)
    print(report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("handover-demo", help="执行行动材料交接端到端演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "handover-demo": command_handover_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
