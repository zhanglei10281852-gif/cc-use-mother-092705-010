from __future__ import annotations


D_GARDEN = "园林巡护队"
D_VOLUNTEER = "志愿者团队"
D_RESEARCH = "科研团队"

D1 = "a" * 64
D2 = "b" * 64
D3 = "c" * 64


def action_payload(code: str = "WETLAND-2026-09", *, deadline: str = "2099-01-01T00:00:00+00:00"):
    return {
        "code": code,
        "name": "九月湿地巡护证据交接",
        "description": "月底汇总各部门巡护记录",
        "owner": "李保护",
        "deadline": deadline,
        "participants": [
            {"department": D_GARDEN, "role": "移交方", "contact": "园林-王队"},
            {"department": D_VOLUNTEER, "role": "协办方", "contact": "志愿-赵组"},
            {"department": D_RESEARCH, "role": "接收方", "contact": "科研-周老师"},
        ],
        "materials": [
            {"code": "PATROL-LOG", "title": "巡护路线记录本", "responsible_department": D_GARDEN, "required": True},
            {"code": "CAMERA-TRAP", "title": "红外相机影像清单", "responsible_department": D_VOLUNTEER, "is_sensitive": True, "required": True},
            {"code": "SPECIES-DATA", "title": "物种观测数据表", "responsible_department": D_RESEARCH, "required": False},
        ],
    }


def batch_payload(material_digests: dict[str, str], **overrides):
    items = [
        {"material_code": code, "filename": f"{code}.zip", "content_digest": digest, "size_bytes": 1024}
        for code, digest in material_digests.items()
    ]
    payload = {
        "kind": "handover",
        "sender_department": D_GARDEN,
        "receiver_department": D_RESEARCH,
        "items": items,
    }
    payload.update(overrides)
    return payload


def confirm(material_code: str, result: str = "complete", note: str = "", confirmer: str = "周老师"):
    payload = {"material_code": material_code, "receiver_department": D_RESEARCH, "confirmer": confirmer, "result": result}
    if note:
        payload["note"] = note
    return payload


def test_full_lifecycle_create_batched_handover_confirm_and_close(client):
    created = client.post("/api/handover/actions?actor=李保护", json=action_payload())
    assert created.status_code == 201, created.text
    detail = created.json()
    assert {p["department"] for p in detail["participants"]} == {D_GARDEN, D_VOLUNTEER, D_RESEARCH}
    assert detail["status"] == "open"
    assert detail["overdue"] is False
    assert detail["closure"]["can_close"] is False

    # 第一批：园林移交巡护日志
    b1 = client.post("/api/handover/actions/WETLAND-2026-09/batches?actor=王队",
                     json=batch_payload({"PATROL-LOG": D1}))
    assert b1.status_code == 201, b1.text
    b1_data = b1.json()
    assert b1_data["batch_no"] == 1
    assert b1_data["kind"] == "handover"
    assert b1_data["status"] == "pending"
    assert b1_data["reused"] is False

    c1 = client.post(f"/api/handover/batches/{b1_data['id']}/confirmations",
                     json=confirm("PATROL-LOG"))
    assert c1.status_code == 201, c1.text
    assert c1.json()["status"] == "confirmed"
    assert c1.json()["items"][0]["latest_result"] == "complete"

    # 未全部交接时不能结案
    closed = client.post("/api/handover/actions/WETLAND-2026-09/close?actor=李保护")
    assert closed.status_code == 409

    # 第二批：志愿者协办移交敏感的红外相机清单
    b2 = client.post("/api/handover/actions/WETLAND-2026-09/batches?actor=赵组",
                     json=batch_payload({"CAMERA-TRAP": D2}, sender_department=D_VOLUNTEER, note="志愿者协助归集"))
    assert b2.status_code == 201, b2.text
    assert b2.json()["batch_no"] == 2
    client.post(f"/api/handover/batches/{b2.json()['id']}/confirmations", json=confirm("CAMERA-TRAP"))

    action = client.get("/api/handover/actions/WETLAND-2026-09").json()
    assert action["closure"]["can_close"] is True
    # 非必填的 SPECIES-DATA 未交接不影响结案

    closed = client.post("/api/handover/actions/WETLAND-2026-09/close?actor=李保护")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_at"]

    # 结案后不能再确认或交接
    blocked = client.post(f"/api/handover/batches/{b1_data['id']}/confirmations", json=confirm("PATROL-LOG"))
    assert blocked.status_code == 409


def test_duplicate_upload_returns_original_batch(client):
    client.post("/api/handover/actions?actor=李保护", json=action_payload("DUP-01"))
    payload = batch_payload({"PATROL-LOG": D1})
    first = client.post("/api/handover/actions/DUP-01/batches?actor=王队", json=payload)
    again = client.post("/api/handover/actions/DUP-01/batches?actor=王队", json=payload)
    assert first.status_code == again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["batch_no"] == first.json()["batch_no"]
    assert again.json()["reused"] is True

    action = client.get("/api/handover/actions/DUP-01").json()
    assert len(action["batches"]) == 1
    assert action["materials"][0]["current_version_no"] == 1

    # 幂等键：首次建立批次，之后无论是否重试，同键都返回同一批次
    keyed = client.post("/api/handover/actions/DUP-01/batches?actor=赵组",
                        json=batch_payload({"CAMERA-TRAP": D2}, sender_department=D_VOLUNTEER, idempotency_key="idem-0001"))
    keyed_retry = client.post("/api/handover/actions/DUP-01/batches?actor=赵组",
                              json=batch_payload({"CAMERA-TRAP": D2}, sender_department=D_VOLUNTEER, idempotency_key="idem-0001"))
    keyed_other_body = client.post("/api/handover/actions/DUP-01/batches?actor=赵组",
                                   json=batch_payload({"SPECIES-DATA": D3}, sender_department=D_VOLUNTEER, idempotency_key="idem-0001"))
    assert keyed.status_code == keyed_retry.status_code == keyed_other_body.status_code == 201
    assert keyed.json()["id"] == keyed_retry.json()["id"] == keyed_other_body.json()["id"] != first.json()["id"]


def test_revision_creates_new_version_and_keeps_old_reference(client):
    client.post("/api/handover/actions?actor=李保护", json=action_payload("VER-01"))
    b1 = client.post("/api/handover/actions/VER-01/batches?actor=王队",
                     json=batch_payload({"PATROL-LOG": D1})).json()
    client.post(f"/api/handover/batches/{b1['id']}/confirmations", json=confirm("PATROL-LOG"))
    v1_version_id = b1["items"][0]["version_id"]

    # 修订后再次交接同一材料：必须产生新版本
    revised = client.post(
        "/api/handover/actions/VER-01/batches?actor=王队",
        json={**batch_payload({"PATROL-LOG": D2}, note="补正页码错误"),
              "items": [{"material_code": "PATROL-LOG", "filename": "PATROL-LOG-v2.zip",
                         "content_digest": D2, "size_bytes": 2048, "change_note": "补正页码错误"}]},
    )
    assert revised.status_code == 201, revised.text
    b2 = revised.json()
    assert b2["id"] != b1["id"]
    assert b2["items"][0]["version_no"] == 2
    assert b2["items"][0]["supersedes_version_id"] == v1_version_id

    # 已确认的旧版本仍被旧批次与旧确认引用
    old_batch = client.get(f"/api/handover/batches/{b1['id']}").json()
    assert old_batch["items"][0]["version_id"] == v1_version_id
    assert old_batch["all_confirmations"][0]["version_no"] == 1

    action = client.get("/api/handover/actions/VER-01").json()
    material = next(m for m in action["materials"] if m["code"] == "PATROL-LOG")
    assert material["current_version_no"] == 2
    chain = material["version_chain"]
    assert [v["version_no"] for v in chain] == [1, 2]
    assert chain[1]["supersedes_version_id"] == chain[0]["version_id"]


def test_missing_and_sensitive_objections_then_supplement_and_close(client):
    client.post("/api/handover/actions?actor=李保护", json=action_payload("OBJ-01"))
    b1 = client.post("/api/handover/actions/OBJ-01/batches?actor=王队",
                     json=batch_payload({"PATROL-LOG": D1, "CAMERA-TRAP": D2})).json()

    # 异议必须填写说明
    no_note = client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                          json=confirm("PATROL-LOG", result="missing"))
    assert no_note.status_code == 422

    # 缺件异议 + 敏感内容异议
    client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                json=confirm("PATROL-LOG", result="missing", note="缺少 9 月 18 日巡护页"))
    disputed = client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                           json=confirm("CAMERA-TRAP", result="sensitive_objection",
                                        note="影像含未脱敏的志愿者人脸，需脱敏处理")).json()
    assert disputed["status"] == "disputed"
    assert client.post("/api/handover/actions/OBJ-01/close?actor=李保护").status_code == 409

    # 接收方改为确认日志完整；批次仍因敏感异议处于 disputed
    client.post(f"/api/handover/batches/{b1['id']}/confirmations", json=confirm("PATROL-LOG"))

    # 截止日前补件：说明补正原因，提交脱敏后的新版本
    sup = client.post(
        "/api/handover/actions/OBJ-01/batches?actor=赵组",
        json={**batch_payload({"CAMERA-TRAP": D3}, sender_department=D_VOLUNTEER,
                              kind="supplement", note="按异议完成人脸脱敏后补件")},
    )
    assert sup.status_code == 201, sup.text
    assert sup.json()["kind"] == "supplement"
    assert sup.json()["items"][0]["version_no"] == 2
    client.post(f"/api/handover/batches/{sup.json()['id']}/confirmations",
                json=confirm("CAMERA-TRAP", note="脱敏处理符合要求，确认完整"))

    action = client.get("/api/handover/actions/OBJ-01").json()
    assert action["closure"]["can_close"] is True
    closed = client.post("/api/handover/actions/OBJ-01/close?actor=李保护")
    assert closed.status_code == 200
    # 异议与每次确认都在事件流中可追溯
    actions = [event["action"] for event in action["events"]]
    assert actions.count("item.object") == 2
    assert actions.count("item.confirm") >= 2


def test_owner_can_reopen_before_deadline_and_old_confirmations_remain(client):
    payload = action_payload("REOPEN-01")
    for material in payload["materials"]:
        if material["code"] != "PATROL-LOG":
            material["required"] = False
    client.post("/api/handover/actions?actor=李保护", json=payload)
    b1 = client.post("/api/handover/actions/REOPEN-01/batches?actor=王队",
                     json=batch_payload({"PATROL-LOG": D1})).json()
    client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                json=confirm("PATROL-LOG", result="missing", note="缺签名页"))

    # 非负责人不能重开
    forbidden = client.post(f"/api/handover/batches/{b1['id']}/reopen",
                            json={"actor": "王队", "reason": "我想重开"})
    assert forbidden.status_code == 409

    reopened = client.post(f"/api/handover/batches/{b1['id']}/reopen",
                           json={"actor": "李保护", "reason": "接收方反馈材料待补正，允许重新交接"})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "pending"
    # 旧确认作为历史保留可见
    assert len(reopened.json()["all_confirmations"]) == 1
    assert reopened.json()["all_confirmations"][0]["result"] == "missing"

    # 重开后重新确认，旧记录不再参与批次完成判定，但仍可查询
    again = client.post(f"/api/handover/batches/{b1['id']}/confirmations",
                        json=confirm("PATROL-LOG", note="签名页补齐后确认完整"))
    assert again.json()["status"] == "confirmed"
    assert len(again.json()["items"][0]["item_confirmations"]) == 2

    assert client.post("/api/handover/actions/REOPEN-01/close?actor=李保护").status_code == 200


def test_overdue_only_supplement_with_reason_allowed(client):
    client.post("/api/handover/actions?actor=李保护",
                json=action_payload("LATE-01", deadline="2020-01-01T00:00:00+00:00"))
    action = client.get("/api/handover/actions/LATE-01").json()
    assert action["overdue"] is True

    # 逾期普通交接被拒绝
    normal = client.post("/api/handover/actions/LATE-01/batches?actor=王队",
                         json=batch_payload({"PATROL-LOG": D1}))
    assert normal.status_code == 409
    assert "补件" in normal.json()["error"]["message"]

    # 逾期补件必须留原因
    no_reason = client.post("/api/handover/actions/LATE-01/batches?actor=王队",
                            json=batch_payload({"PATROL-LOG": D1}, kind="supplement"))
    assert no_reason.status_code == 422

    sup = client.post(
        "/api/handover/actions/LATE-01/batches?actor=王队",
        json=batch_payload({"PATROL-LOG": D1}, kind="supplement",
                           overdue_reason="档案柜封存延期，9 月 30 日才取出"),
    )
    assert sup.status_code == 201, sup.text
    assert sup.json()["overdue_reason"]

    # 逾期不允许重开
    reopen = client.post(f"/api/handover/batches/{sup.json()['id']}/reopen",
                         json={"actor": "李保护", "reason": "逾期也想重开"})
    assert reopen.status_code == 409


def test_party_validation_and_required_material_blocks_closure(client):
    payload = action_payload("RULE-01")
    payload["participants"].append({"department": "生态监测中心", "role": "接收方", "contact": "监测-林工"})
    client.post("/api/handover/actions?actor=李保护", json=payload)

    # 未登记部门不能移交
    unknown = client.post(
        "/api/handover/actions/RULE-01/batches?actor=外人",
        json=batch_payload({"PATROL-LOG": D1}, sender_department="外部单位"),
    )
    assert unknown.status_code == 422

    # 同一部门不能既移交又接收
    same = client.post(
        "/api/handover/actions/RULE-01/batches?actor=周老师",
        json=batch_payload({"PATROL-LOG": D1}, sender_department=D_RESEARCH),
    )
    assert same.status_code == 422

    # 仅登记为接收方的部门不能充当移交方
    wrong_sender = client.post(
        "/api/handover/actions/RULE-01/batches?actor=林工",
        json=batch_payload({"PATROL-LOG": D1}, sender_department="生态监测中心"),
    )
    assert wrong_sender.status_code == 409

    # 只有批次指定接收方可以确认
    b1 = client.post("/api/handover/actions/RULE-01/batches?actor=王队",
                     json=batch_payload({"PATROL-LOG": D1, "CAMERA-TRAP": D2})).json()
    wrong_receiver = client.post(
        f"/api/handover/batches/{b1['id']}/confirmations",
        json={**confirm("PATROL-LOG"), "receiver_department": D_GARDEN},
    )
    assert wrong_receiver.status_code == 409

    # 只确认日志、必备的影像未确认：不能结案
    client.post(f"/api/handover/batches/{b1['id']}/confirmations", json=confirm("PATROL-LOG"))
    blocked = client.post("/api/handover/actions/RULE-01/close?actor=李保护")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["batches"] == [1]


def test_action_creation_validation(client):
    # 必须有接收方
    payload = action_payload("BAD-01")
    payload["participants"] = [{"department": D_GARDEN, "role": "移交方"}]
    assert client.post("/api/handover/actions?actor=李保护", json=payload).status_code == 422

    # 材料责任部门必须在参与部门名单中
    payload = action_payload("BAD-02")
    payload["materials"][0]["responsible_department"] = "外部单位"
    assert client.post("/api/handover/actions?actor=李保护", json=payload).status_code == 422

    # 重复行动编码
    client.post("/api/handover/actions?actor=李保护", json=action_payload("BAD-03"))
    assert client.post("/api/handover/actions?actor=李保护", json=action_payload("BAD-03")).status_code == 409
