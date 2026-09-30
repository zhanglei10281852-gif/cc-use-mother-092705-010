from __future__ import annotations

from fastapi.testclient import TestClient


def _action_payload(deadline: str = "2030-01-01T00:00:00+00:00") -> dict:
    return {
        "code": "ACT-WETLAND-01",
        "name": "秋季湿地巡护证据交接",
        "description": "园林、志愿者与科研团队月度材料交接",
        "owner": "湿地保护项目组",
        "manager": "林主管",
        "deadline": deadline,
        "senders": [
            {"department": "园林处", "contact": "王巡护"},
            {"department": "志愿者协会", "contact": "陈志愿"},
        ],
        "receivers": [
            {"department": "科研团队", "contact": "周博士"},
            {"department": "档案室", "contact": "吴档案"},
        ],
        "materials": [
            {"code": "M01", "name": "巡护日志", "category": "记录"},
            {"code": "M02", "name": "物种影像证据", "category": "影像", "sensitive": True},
            {"code": "M03", "name": "样方数据", "category": "数据", "optional": True},
        ],
    }


def _create_action(
    client: TestClient, payload: dict | None = None, deadline: str | None = None
) -> dict:
    body = payload or _action_payload(deadline=deadline or "2030-01-01T00:00:00+00:00")
    response = client.post("/api/handover/actions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _submit_batch(client: TestClient, action_id: int, items: list[dict], **overrides) -> dict:
    payload = {
        "sender_dept": "园林处",
        "receiver_dept": "科研团队",
        "actor": "王巡护",
        "items": items,
    }
    payload.update(overrides)
    response = client.post(f"/api/handover/actions/{action_id}/batches", json=payload)
    assert response.status_code in (201, 200), response.text
    return response.json()


def _items_v1() -> list[dict]:
    return [
        {
            "material_code": "M01",
            "content_ref": "oss://wetland/2026-09/patrol-log.pdf",
            "content_sha256": "a1" * 32,
        },
        {
            "material_code": "M02",
            "content_ref": "oss://wetland/2026-09/species-raw.mp4",
            "content_sha256": "b2" * 32,
            "sensitive": True,
        },
    ]


def test_create_action_and_party_boundaries(client: TestClient):
    action = _create_action(client)
    assert action["code"] == "ACT-WETLAND-01"
    assert {p["department"] for p in action["parties"]["senders"]} == {"园林处", "志愿者协会"}
    assert {p["department"] for p in action["parties"]["receivers"]} == {"科研团队", "档案室"}
    assert len(action["materials"]) == 3
    assert action["overdue"] is False

    # 非参与部门不能发起交接
    response = client.post(
        f"/api/handover/actions/{action['id']}/batches",
        json={
            "sender_dept": "外部单位",
            "receiver_dept": "科研团队",
            "actor": "外人",
            "items": _items_v1(),
        },
    )
    assert response.status_code == 422

    # 清单外材料不能交接
    response = client.post(
        f"/api/handover/actions/{action['id']}/batches",
        json={
            "sender_dept": "园林处",
            "receiver_dept": "科研团队",
            "actor": "王巡护",
            "items": [
                {"material_code": "M99", "content_ref": "oss://x"},
            ],
        },
    )
    assert response.status_code == 422


def test_duplicate_submission_returns_original_batch(client: TestClient):
    action = _create_action(client)
    first = _submit_batch(client, action["id"], _items_v1())
    second = _submit_batch(client, action["id"], _items_v1())
    assert first["id"] == second["id"]
    assert first["submission_key"] == second["submission_key"]
    # 重复上传没有生成新批次
    assert action["batches"] == [] or len(action["batches"]) == 0
    listed = client.get(f"/api/handover/actions/{action['id']}").json()
    assert len(listed["batches"]) == 1


def test_full_handflow_confirm_objection_revise_reopen_close(client: TestClient):
    action = _create_action(client)
    batch = _submit_batch(client, action["id"], _items_v1())
    batch_id = batch["id"]
    assert batch["status"] == "submitted"
    assert batch["item_total"] == 2

    # 逐项确认：M01 通过
    ok = client.post(
        f"/api/handover/batches/{batch_id}/confirm",
        json={"material_code": "M01", "actor": "周博士", "receiver_dept": "科研团队"},
    )
    assert ok.status_code == 200, ok.text
    m01 = next(item for item in ok.json()["items"] if item["material_code"] == "M01")
    assert m01["state"] == "confirmed"
    assert m01["confirmed_by"] == "周博士"

    # 其他接收部门不能代确认这个批次
    forbidden = client.post(
        f"/api/handover/batches/{batch_id}/confirm",
        json={"material_code": "M02", "actor": "吴档案", "receiver_dept": "档案室"},
    )
    assert forbidden.status_code == 409

    # M02 先确认 v1，随后发现含敏感内容提出异议：旧确认留痕、批次退回异议
    client.post(
        f"/api/handover/batches/{batch_id}/confirm",
        json={"material_code": "M02", "actor": "周博士", "receiver_dept": "科研团队"},
    )
    objection = client.post(
        f"/api/handover/batches/{batch_id}/objections",
        json={
            "kind": "sensitive",
            "material_code": "M02",
            "reason": "影像出现巢点精确坐标，需脱敏",
            "actor": "周博士",
            "receiver_dept": "科研团队",
        },
    )
    assert objection.status_code == 201, objection.text
    assert objection.json()["status"] == "objection"

    # 缺件异议：M03 是可选材料，但接收方仍可登记缺件异议（批次中没有该行）
    missing = client.post(
        f"/api/handover/batches/{batch_id}/objections",
        json={
            "kind": "missing",
            "material_code": "M03",
            "reason": "科研复算还需要样方数据",
            "actor": "周博士",
            "receiver_dept": "科研团队",
        },
    )
    assert missing.status_code == 201

    # 补件必须留原因；无原因 422
    no_reason = client.post(
        f"/api/handover/batches/{batch_id}/supplements",
        json={
            "sender_dept": "园林处",
            "reason": "   ",
            "actor": "王巡护",
            "items": [
                {
                    "material_code": "M02",
                    "content_ref": "oss://wetland/2026-09/species-masked.mp4",
                    "content_sha256": "c3" * 32,
                    "sensitive": False,
                }
            ],
        },
    )
    assert no_reason.status_code == 422

    # 修订 M02 + 补齐 M03：新版本，旧版本不删除
    supplement = client.post(
        f"/api/handover/batches/{batch_id}/supplements",
        json={
            "sender_dept": "园林处",
            "reason": "影像已打码脱敏，并补交样方数据",
            "actor": "王巡护",
            "items": [
                {
                    "material_code": "M02",
                    "content_ref": "oss://wetland/2026-09/species-masked.mp4",
                    "content_sha256": "c3" * 32,
                    "sensitive": False,
                },
                {
                    "material_code": "M03",
                    "content_ref": "oss://wetland/2026-09/quadrat.csv",
                    "content_sha256": "d4" * 32,
                },
            ],
        },
    )
    assert supplement.status_code == 201, supplement.text
    body = supplement.json()
    m02_item = next(item for item in body["items"] if item["material_code"] == "M02")
    m03_item = next(item for item in body["items"] if item["material_code"] == "M03")
    assert m02_item["version_no"] == 2
    assert m02_item["supersedes_version_id"] is not None
    assert m02_item["state"] == "pending"  # 修订后必须重新确认
    assert m03_item["version_no"] == 1
    # 异议已随补件自动处理
    assert all(o["resolved_at"] for o in body["objections"])

    # 版本链：M02 两版，旧版本仍可引用
    versions = client.get(f"/api/handover/actions/{action['id']}/materials/M02/versions")
    assert versions.status_code == 200
    version_rows = versions.json()["versions"]
    assert [v["version_no"] for v in version_rows] == [1, 2]
    assert version_rows[1]["supersedes_version_id"] == version_rows[0]["id"]

    # 确认历史展示“每次确认”：M01-v1、M02-v1（已被取代但保留）、待补 M02-v2
    history_before = {
        (c["material_code"], c["version_no"]): c for c in body["confirmations"]
    }
    assert ("M01", 1) in history_before
    assert ("M02", 1) in history_before
    assert history_before[("M02", 1)]["superseded_at"]

    # 重新确认 M02 v2 与 M03
    for code in ("M02", "M03"):
        resp = client.post(
            f"/api/handover/batches/{batch_id}/confirm",
            json={"material_code": code, "actor": "周博士", "receiver_dept": "科研团队"},
        )
        assert resp.status_code == 200, resp.text
    final = client.get(f"/api/handover/batches/{batch_id}").json()
    assert final["status"] == "confirmed"
    assert final["item_confirmed"] == 3
    assert len(final["confirmations"]) == 4

    # 截止日前：负责人可重开已完成批次；非负责人不行
    not_manager = client.post(
        f"/api/handover/batches/{batch_id}/reopen",
        json={"manager": "外人", "reason": "补材料", "actor": "外人"},
    )
    assert not_manager.status_code == 409
    reopened = client.post(
        f"/api/handover/batches/{batch_id}/reopen",
        json={"manager": "林主管", "reason": "科研团队要求追加一份点位说明", "actor": "林主管"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "reopened"
    assert reopened.json()["reopen_count"] == 1

    # 重开后补件、重新确认
    client.post(
        f"/api/handover/batches/{batch_id}/supplements",
        json={
            "sender_dept": "园林处",
            "reason": "追加点位说明文件",
            "actor": "王巡护",
            "items": [
                {
                    "material_code": "M01",
                    "content_ref": "oss://wetland/2026-09/patrol-log-v2.pdf",
                    "content_sha256": "e5" * 32,
                }
            ],
        },
    )
    client.post(
        f"/api/handover/batches/{batch_id}/confirm",
        json={"material_code": "M01", "actor": "周博士", "receiver_dept": "科研团队"},
    )
    done = client.get(f"/api/handover/batches/{batch_id}").json()
    assert done["status"] == "confirmed"

    # 结案批次与行动
    closed = client.post(
        f"/api/handover/batches/{batch_id}/close",
        json={"actor": "周博士", "receiver_dept": "科研团队"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["closed_at"]

    closed_action = client.post(
        f"/api/handover/actions/{action['id']}/close",
        json={"manager": "林主管", "actor": "林主管"},
    )
    assert closed_action.status_code == 200, closed_action.text
    assert closed_action.json()["status"] == "closed"

    # 结案后时间线完整、责任边界清晰
    detail = client.get(f"/api/handover/actions/{action['id']}").json()
    event_types = {event["event_type"] for event in detail["timeline"]}
    assert {"action.created", "action.closed"} <= event_types
    batch_timeline = client.get(f"/api/handover/batches/{batch_id}").json()["timeline"]
    kinds = {event["event_type"] for event in batch_timeline}
    assert {"batch.submitted", "item.confirmed", "objection.raised",
            "batch.supplemented", "batch.reopened", "batch.closed"} <= kinds


def test_overdue_allows_only_supplements_with_reason(client: TestClient):
    # 截止日前已建好批次，随后逾期
    action = _create_action(client, deadline="2030-01-01T00:00:00+00:00")
    batch = _submit_batch(client, action["id"], _items_v1())

    from app.database import get_connection

    get_connection().execute(
        "UPDATE handover_actions SET deadline=? WHERE id=?",
        ("2026-09-01T00:00:00+00:00", action["id"]),
    )

    overdue_action = client.get(f"/api/handover/actions/{action['id']}").json()
    assert overdue_action["overdue"] is True

    # 逾期不能发起新批次
    new_batch = client.post(
        f"/api/handover/actions/{action['id']}/batches",
        json={
            "sender_dept": "志愿者协会",
            "receiver_dept": "档案室",
            "actor": "陈志愿",
            "items": [{"material_code": "M01", "content_ref": "oss://other"}],
        },
    )
    assert new_batch.status_code == 409

    # 逾期不能重开
    # 先让批次达到 confirmed
    for code in ("M01", "M02"):
        client.post(
            f"/api/handover/batches/{batch['id']}/confirm",
            json={"material_code": code, "actor": "周博士", "receiver_dept": "科研团队"},
        )
    reopen = client.post(
        f"/api/handover/batches/{batch['id']}/reopen",
        json={"manager": "林主管", "reason": "想重开", "actor": "林主管"},
    )
    assert reopen.status_code == 409

    # 逾期仍允许补件，但必须留下原因
    late = client.post(
        f"/api/handover/batches/{batch['id']}/supplements",
        json={
            "sender_dept": "园林处",
            "reason": "设备故障导致影像导出延迟，逾期补交脱敏版",
            "actor": "王巡护",
            "items": [
                {
                    "material_code": "M02",
                    "content_ref": "oss://wetland/2026-09/species-late.mp4",
                    "content_sha256": "f6" * 32,
                }
            ],
        },
    )
    assert late.status_code == 201, late.text
    supplement_record = late.json()["supplements"][-1]
    assert "设备故障" in supplement_record["reason"]


def test_withdraw_objection_restores_pending_and_closed_batch_locked(client: TestClient):
    action = _create_action(client)
    batch = _submit_batch(client, action["id"], _items_v1())
    batch_id = batch["id"]

    # 先确认 M01，对 M02 提敏感异议再撤回
    client.post(
        f"/api/handover/batches/{batch_id}/confirm",
        json={"material_code": "M01", "actor": "周博士", "receiver_dept": "科研团队"},
    )
    objection = client.post(
        f"/api/handover/batches/{batch_id}/objections",
        json={
            "kind": "sensitive",
            "material_code": "M02",
            "reason": "疑似含敏感点位",
            "actor": "周博士",
            "receiver_dept": "科研团队",
        },
    )
    objection_id = objection.json()["objections"][-1]["id"]
    withdrawn = client.post(
        f"/api/handover/objections/{objection_id}/withdraw",
        json={"actor": "周博士"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    m02 = next(i for i in withdrawn.json()["items"] if i["material_code"] == "M02")
    assert m02["state"] == "pending"
    assert withdrawn.json()["open_objections"] == 0

    # 收掉批次后不能再补件或提异议
    client.post(
        f"/api/handover/batches/{batch_id}/confirm",
        json={"material_code": "M02", "actor": "周博士", "receiver_dept": "科研团队"},
    )
    client.post(
        f"/api/handover/batches/{batch_id}/close",
        json={"actor": "周博士", "receiver_dept": "科研团队"},
    )
    locked = client.post(
        f"/api/handover/batches/{batch_id}/supplements",
        json={
            "sender_dept": "园林处",
            "reason": "结案后想再改",
            "actor": "王巡护",
            "items": [
                {"material_code": "M01", "content_ref": "oss://wetland/x.pdf",
                 "content_sha256": "11" * 32}
            ],
        },
    )
    assert locked.status_code == 409
