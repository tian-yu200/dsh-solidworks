import copy
import json

import pytest


GRAPH = {"schema_version": "0.0.1-draft-ir-exp", "units": "meters", "nodes": [
    {"id": "s", "type": "sketch", "ref": {"datum": "front"},
     "profile": [{"kind": "circle", "diameter": .02}]},
    {"id": "e", "type": "extrude", "sketch": "s", "operation": "boss", "depth": .03},
]}


def fail_graph(cad_env):
    cad_env.backend.feature_graph_handler = lambda *_: {
        "status": "FAILED", "error": {"code": "EXTRUSION_FAILED", "message": "probe"},
        "compiler": {"node_log": [{"id": "s", "status": "COMPLETED"}]},
    }


def test_discard_with_another_old_document_active_keeps_job_unbound(cad_env, monkeypatch):
    fail_graph(cad_env)
    a = {"document_id": "A", "title": "零件2", "path": "", "type": "PART", "isDirty": True, "updateStamp": 161}
    b = {**a, "document_id": "B", "title": "零件4", "updateStamp": 153}
    snapshots = iter([
        {"documents": [a, b], "active_document": a, "undo_scope_active": False},
        {"documents": [a, b], "active_document": a, "undo_scope_active": False},
        {"documents": [a, b, {**a, "document_id": "created-1", "title": "Part1"}], "active_document": {**a, "document_id": "created-1", "title": "Part1"}, "undo_scope_active": False},
        {"documents": [a, b], "active_document": b, "undo_scope_active": False},
    ])
    monkeypatch.setattr(cad_env.backend, "documents", lambda: copy.deepcopy(next(snapshots)), raising=False)
    response = cad_env.orchestrator.feature_graph("job-1", cad_env.token, json.dumps(GRAPH))
    assert response["recovery_status"] == "discarded"
    assert response["target_document"] == "Part1"
    assert response["active_document_after_recovery"] == "零件4"
    assert response["created_document_discarded"] is True
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"] == {}
    assert job["document_origin"] == "unbound"
    assert job["document_write_allowed"] is False
    assert not any(t == "activate_document" for t, _ in cad_env.backend.calls)


def test_existing_document_undo_success_is_still_unverified(cad_env):
    cad_env.backend.call_handler = lambda tool, _: ({"status": "FAILED", "error": {"code": "EDIT_FAILED", "message": "probe"}} if tool == "modify_dimension" else None)
    response = cad_env.orchestrator.document_edits("job-1", cad_env.token,
        json.dumps([{"tool": "modify_dimension", "params": {"name": "D1", "value": .01}}]))
    assert response["recovery_status"] == "recovery_unverified"
    assert response["automatic_retry_allowed"] is False
    assert response["verification"]["all_completed"] is False


@pytest.mark.parametrize("created_id", [None, "closed-original-id"])
def test_same_named_replacement_cannot_be_claimed_or_closed(cad_env, created_id):
    job = cad_env.store.load("job-1", cad_env.token)
    job["documents_before_create"] = {"documents": []}
    # The response refers to C, but only same-named D is still open.
    cad_env.backend.state.update(documentId="replacement-id", activeDocument="Part1", activeDocumentPath="")
    identity = cad_env.orchestrator._capture_created_identity(
        job, {"title": "Part1", "path": "", "type": "PART"}, created_id,
    )
    assert "document_id" not in identity
    status, _, _ = cad_env.orchestrator._close_created_document(
        job, kind="feature_graph", created_identity=identity, previous_identity={},
    )
    assert status == "rollback_failed"
    assert not any(tool == "close_document" for tool, _ in cad_env.backend.calls)


@pytest.mark.parametrize("edit", ["schema_version", "unknown_field", "description", "node_name"])
def test_non_executable_changes_do_not_admit_a_repair(cad_env, edit):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    replay = copy.deepcopy(GRAPH)
    if edit == "schema_version":
        replay["schema_version"] = "different"
    elif edit == "unknown_field":
        replay["unused_extra"] = 42
    elif edit == "description":
        replay["description"] = "new description only"
    else:
        replay["nodes"][0]["name"] = "renamed sketch"
    calls = len(cad_env.backend.calls)
    assert submit(cad_env, replay, evidence())["status"] == "pending_confirmation"
    assert len(cad_env.backend.calls) == calls


def test_drawing_creation_identity_failure_reports_target_separately(cad_env, monkeypatch):
    original_call = cad_env.backend.call

    def missing_created_id(tool, params):
        response = original_call(tool, params)
        if tool == "create_drawing":
            response.pop("result_geometry", None)
        return response

    monkeypatch.setattr(cad_env.backend, "call", missing_created_id)
    response = cad_env.orchestrator.drawing_workflow("job-1", cad_env.token, "{}")
    assert response["recovery_status"] == "rollback_failed"
    assert response["operation_target"]["title"] == "Drawing1"
    assert response["created_document_discarded"] is False
    assert response["undo_restoration_verified"] is False
    assert not any(tool == "close_document" for tool, _ in cad_env.backend.calls)


FRAME = {"origin": [0, 0, 0], "xdir": [1, 0, 0], "ydir": [0, 1, 0]}


def evidence(number=1):
    return json.dumps({"error_code": "EXTRUSION_FAILED", "cause": "measured frame %s" % number,
                       "readback": "probe %s: same model plane and approved design" % number,
                       "change": "encode declaration without changing geometry %s" % number})


def submit(env, graph, why="{}", job_id="job-1", token=None):
    return env.orchestrator.feature_graph(job_id, token or env.token, json.dumps(graph), True, why)


def test_repair_budget_survives_new_jobs_renumbering_and_new_orchestrator(cad_env):
    fail_graph(cad_env)
    first = submit(cad_env, GRAPH)
    assert first["recovery_status"] == "discarded"
    assert first["repair_attempts_used"] == 0
    replay = copy.deepcopy(GRAPH)
    replay["nodes"][0]["id"] = "new-sketch"
    replay["nodes"][1].update(id="new-boss", sketch="new-sketch")
    pending = submit(cad_env, replay, evidence())
    assert "原样重放" in pending["action_summary"]
    cad_env.orchestrator.cancel_action("job-1", cad_env.token, pending["action_id"])
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][0]["frame"] = FRAME
    job, token = cad_env.store.create("replacement-job", "renumbered plan", {})
    second = submit(cad_env, fixed, evidence(), job["job_id"], token)
    assert second["repair_attempts_used"] == 1
    assert second["recovery_status"] == "discarded"
    newer = cad_env.module.CadOrchestrator()
    fixed["nodes"][1]["end"] = "blind"  # explicit encoding of the same default
    third = newer.feature_graph(job["job_id"], token, json.dumps(fixed), True, evidence(2))
    assert third["repair_attempts_used"] == 2
    assert third["automatic_retry_allowed"] is False
    fixed["nodes"][1]["reversed"] = False
    pending = newer.feature_graph("job-1", cad_env.token, json.dumps(fixed), True, evidence(3))
    assert pending["status"] == "pending_confirmation"
    assert "已自动修复 2 次" in pending["action_summary"]
    with cad_env.store.recovery_chain() as ledger:
        assert len(ledger["active"]["attempts"]) == 3


@pytest.mark.parametrize("change", ["diameter", "depth", "material", "omit", "through"])
def test_repair_cannot_change_approved_design(cad_env, change):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    altered = copy.deepcopy(GRAPH)
    if change == "diameter":
        altered["nodes"][0]["profile"][0]["diameter"] += .001
    elif change == "depth":
        altered["nodes"][1]["depth"] += .001
    elif change == "material":
        altered["material"] = {"name": "AISI 1020"}
    elif change == "omit":
        altered["nodes"].pop()
    elif change == "through":
        altered["nodes"][1].update(end="through_all")
    calls = len(cad_env.backend.calls)
    pending = submit(cad_env, altered, evidence())
    assert pending["status"] == "pending_confirmation"
    assert "设计有改动" in pending["action_summary"]
    assert len(cad_env.backend.calls) == calls


def test_repair_material_name_is_not_ignored(cad_env):
    fail_graph(cad_env)
    graph = copy.deepcopy(GRAPH)
    graph["material"] = {"name": "AISI 1020"}
    submit(cad_env, graph)
    graph["material"]["name"] = "ABS"
    pending = submit(cad_env, graph, evidence())
    assert "设计有改动" in pending["action_summary"]


def test_repair_needs_specific_new_evidence_and_unchanged_context(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][0]["frame"] = FRAME
    pending = submit(cad_env, fixed)
    assert "证据须完整" in pending["action_summary"]
    cad_env.orchestrator.cancel_action("job-1", cad_env.token, pending["action_id"])
    submit(cad_env, fixed, evidence())
    fixed["nodes"][1]["end"] = "blind"
    pending = submit(cad_env, fixed, evidence())
    assert "新的证据" in pending["action_summary"]
    cad_env.orchestrator.cancel_action("job-1", cad_env.token, pending["action_id"])
    cad_env.backend.state["updateStamp"] = 99
    pending = submit(cad_env, fixed, evidence(2))
    assert "文档有变化" in pending["action_summary"]


def test_large_fresh_repair_needs_no_batch_confirmation_but_still_counts(cad_env):
    # User decision 2026-10-31: plan approval authorizes a fresh build, so a graph over ten nodes
    # runs at once. The repair budget and the user confirmation for inadmissible changes remain.
    graph = copy.deepcopy(GRAPH)
    for i in range(1, 6):
        sketch, extrude = copy.deepcopy(GRAPH["nodes"])
        sketch["id"], extrude["id"], extrude["sketch"] = "s%s" % i, "e%s" % i, "s%s" % i
        graph["nodes"].extend([sketch, extrude])
    assert len(graph["nodes"]) > 10
    fail_graph(cad_env)
    first = submit(cad_env, graph)
    assert first["recovery_status"] == "discarded", first
    graph["nodes"][0]["frame"] = FRAME
    repaired = submit(cad_env, graph, evidence())
    assert repaired["status"] != "pending_confirmation", repaired
    assert repaired["repair_attempts_used"] == 1
    with cad_env.store.recovery_chain() as ledger:
        assert len(ledger["active"]["attempts"]) == 2
    pending = submit(cad_env, {**graph, "nodes": graph["nodes"][:2]}, evidence(2))
    assert pending["status"] == "pending_confirmation"
    assert "设计有改动" in pending["action_summary"]


@pytest.mark.parametrize("problem", ["still_open", "old_changed", "undo_active", "missing_inventory"])
def test_discard_requires_complete_inventory_and_released_scope(cad_env, monkeypatch, problem):
    fail_graph(cad_env)
    real = cad_env.backend.documents
    count = 0
    def inventory():
        nonlocal count
        count += 1
        result = real()
        if count < 3:
            return result
        if problem == "still_open":
            result["documents"].append({"document_id": "created-1", "title": "Part1", "path": "",
                                         "type": "PART", "isDirty": True, "updateStamp": 9})
        elif problem == "old_changed":
            result["documents"][0]["updateStamp"] += 1
        elif problem == "undo_active":
            result["undo_scope_active"] = True
        else:
            raise RuntimeError("inventory unavailable")
        return result
    monkeypatch.setattr(cad_env.backend, "documents", inventory)
    result = submit(cad_env, GRAPH)
    assert result["recovery_status"] == "rollback_failed"
    assert result["automatic_retry_allowed"] is False
    calls = len(cad_env.backend.calls)
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][0]["frame"] = FRAME
    if problem == "missing_inventory":
        monkeypatch.setattr(cad_env.backend, "documents", real)
    pending = submit(cad_env, fixed, evidence())
    assert pending["status"] == "pending_confirmation"
    assert "恢复未验证" in pending["action_summary"]
    assert len(cad_env.backend.calls) == calls


def test_no_inventory_stops_before_creating_any_document(cad_env, monkeypatch):
    monkeypatch.setattr(cad_env.backend, "documents", lambda: (_ for _ in ()).throw(RuntimeError("missing endpoint")))
    with pytest.raises(RuntimeError, match="missing endpoint"):
        submit(cad_env, GRAPH)
    assert not cad_env.backend.calls


def test_existing_edit_failure_blocks_a_new_job_on_same_document(cad_env):
    test_existing_document_undo_success_is_still_unverified(cad_env)
    job, token = cad_env.store.create("another-job", "same document", cad_env.orchestrator._identity(cad_env.backend.state))
    job["document_write_allowed"] = True
    cad_env.store.save(job)
    calls = len(cad_env.backend.calls)
    pending = cad_env.orchestrator.document_edits("another-job", token,
        json.dumps([{"tool": "modify_dimension", "params": {"name": "D1", "value": .01}}]))
    assert pending["status"] == "pending_confirmation"
    assert "已有文档上次恢复未验证" in pending["action_summary"]
    assert len(cad_env.backend.calls) == calls

