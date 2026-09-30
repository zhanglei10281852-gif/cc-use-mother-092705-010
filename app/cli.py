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
    """用进程内本地 API 走完：建档→分批交接→重复上传→确认→异议→补件→重开→结案。"""
    from app.database import close_connection

    with TestClient(app) as client:
        action_body = {
            "code": "ACT-DEMO-2026-09",
            "name": "九月湿地巡护证据交接",
            "description": "园林处、志愿者协会向科研团队交接巡护材料",
            "owner": "湿地保护项目组",
            "manager": "林主管",
            "deadline": "2030-10-01T00:00:00+00:00",
            "senders": [
                {"department": "园林处", "contact": "王巡护"},
                {"department": "志愿者协会", "contact": "陈志愿"},
            ],
            "receivers": [{"department": "科研团队", "contact": "周博士"}],
            "materials": [
                {"code": "M01", "name": "巡护日志", "category": "记录"},
                {"code": "M02", "name": "物种影像证据", "category": "影像", "sensitive": True},
                {"code": "M03", "name": "样方数据", "category": "数据"},
            ],
        }
        created = client.post("/api/handover/actions", json=action_body)
        assert created.status_code == 201, created.text
        action_id = created.json()["id"]

        items_v1 = [
            {"material_code": "M01", "content_ref": "oss://wetland/2026-09/patrol-log.pdf",
             "content_sha256": "a1" * 32},
            {"material_code": "M02", "content_ref": "oss://wetland/2026-09/species-raw.mp4",
             "content_sha256": "b2" * 32, "sensitive": True},
        ]
        batch_body = {
            "sender_dept": "园林处", "receiver_dept": "科研团队",
            "actor": "王巡护", "items": items_v1,
        }
        first = client.post(f"/api/handover/actions/{action_id}/batches", json=batch_body)
        assert first.status_code == 201, first.text
        batch_id = first.json()["id"]
        # 同一批材料重复上传：必须返回原批次
        second = client.post(f"/api/handover/actions/{action_id}/batches", json=batch_body)
        assert second.status_code == 200 and second.json()["id"] == batch_id, second.text

        def confirm(code: str):
            resp = client.post(
                f"/api/handover/batches/{batch_id}/confirm",
                json={"material_code": code, "actor": "周博士",
                      "receiver_dept": "科研团队"},
            )
            assert resp.status_code == 200, resp.text

        confirm("M01")
        confirm("M02")
        # 已确认后发现敏感内容：提出异议
        obj = client.post(
            f"/api/handover/batches/{batch_id}/objections",
            json={"kind": "sensitive", "material_code": "M02",
                  "reason": "影像含巢点精确坐标，需要脱敏",
                  "actor": "周博士", "receiver_dept": "科研团队"},
        )
        assert obj.status_code == 201, obj.text
        # 缺件异议：M03 整批未交
        missing = client.post(
            f"/api/handover/batches/{batch_id}/objections",
            json={"kind": "missing", "material_code": "M03",
                  "reason": "复算需要样方数据",
                  "actor": "周博士", "receiver_dept": "科研团队"},
        )
        assert missing.status_code == 201, missing.text

        # 补件必须带原因：M02 修订新版本 + M03 补齐
        supplement = client.post(
            f"/api/handover/batches/{batch_id}/supplements",
            json={"sender_dept": "园林处", "reason": "影像打码脱敏并补交样方数据",
                  "actor": "王巡护",
                  "items": [
                      {"material_code": "M02",
                       "content_ref": "oss://wetland/2026-09/species-masked.mp4",
                       "content_sha256": "c3" * 32, "sensitive": False},
                      {"material_code": "M03",
                       "content_ref": "oss://wetland/2026-09/quadrat.csv",
                       "content_sha256": "d4" * 32},
                  ]},
        )
        assert supplement.status_code == 201, supplement.text
        confirm("M02")
        confirm("M03")

        # 截止日前负责人重开，再补交一份修订
        reopened = client.post(
            f"/api/handover/batches/{batch_id}/reopen",
            json={"manager": "林主管", "reason": "追加点位说明", "actor": "林主管"},
        )
        assert reopened.status_code == 200, reopened.text
        extra = client.post(
            f"/api/handover/batches/{batch_id}/supplements",
            json={"sender_dept": "园林处", "reason": "追加点位说明文件",
                  "actor": "王巡护",
                  "items": [{"material_code": "M01",
                             "content_ref": "oss://wetland/2026-09/patrol-log-v2.pdf",
                             "content_sha256": "e5" * 32}]},
        )
        assert extra.status_code == 201, extra.text
        confirm("M01")

        closed = client.post(
            f"/api/handover/batches/{batch_id}/close",
            json={"actor": "周博士", "receiver_dept": "科研团队"},
        )
        assert closed.status_code == 200, closed.text
        closed_action = client.post(
            f"/api/handover/actions/{action_id}/close",
            json={"manager": "林主管", "actor": "林主管"},
        )
        assert closed_action.status_code == 200, closed_action.text

        action = client.get(f"/api/handover/actions/{action_id}").json()
        batch = client.get(f"/api/handover/batches/{batch_id}").json()
        versions = client.get(
            f"/api/handover/actions/{action_id}/materials/M02/versions"
        ).json()

    result = {
        "行动": {
            "编号": action["code"], "名称": action["name"], "状态": action["status"],
            "负责人": action["manager"], "截止日": action["deadline"],
            "移交方": [p["department"] for p in action["parties"]["senders"]],
            "接收方": [p["department"] for p in action["parties"]["receivers"]],
            "材料清单": [m["code"] + ":" + m["name"] for m in action["materials"]],
        },
        "批次": {
            "批次号": batch["batch_no"], "交接": f'{batch["sender_dept"]} → {batch["receiver_dept"]}',
            "状态": batch["status"], "材料数": batch["item_total"],
            "已确认": batch["item_confirmed"], "重开次数": batch["reopen_count"],
            "首次提交": batch["submitted_at"], "结案时间": batch["closed_at"],
        },
        "逐项状态": [
            {"材料": i["material_code"], "版本": i["version_no"], "状态": i["state"],
             "确认人": i["confirmed_by"], "证据": i["content_ref"],
             "上一版本ID": i["supersedes_version_id"]}
            for i in batch["items"]
        ],
        "M02版本链": [
            {"版本": v["version_no"], "证据": v["content_ref"],
             "取代版本ID": v["supersedes_version_id"], "批次": v["batch_no"]}
            for v in versions["versions"]
        ],
        "每次确认": [
            {"材料": c["material_code"], "版本": c["version_no"],
             "确认人": c["confirmed_by"], "时间": c["created_at"],
             "被取代时间": c["superseded_at"]}
            for c in batch["confirmations"]
        ],
        "异议处理": [
            {"类型": o["kind"], "材料": o["material_code"], "原因": o["reason"],
             "处理": o["resolution_kind"], "处理结果": o["resolution"]}
            for o in batch["objections"]
        ],
        "批次时间线": [f'{e["created_at"]} {e["actor"]} {e["event_type"]}' for e in batch["timeline"]],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    close_connection()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("handover-demo", help="执行保护行动材料交接全流程演示")
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
