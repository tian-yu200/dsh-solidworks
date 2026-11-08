import copy
import json
from pathlib import Path

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


def test_repair_evidence_may_give_readback_as_an_object(cad_env):
    # 2026-11-08 bench: models wrote readback as the node log they had read; 4 of 5 automatic
    # repairs were refused as "incomplete" and fell to a confirmation round trip.
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][0]["frame"] = FRAME
    why = json.dumps({"error_code": "EXTRUSION_FAILED", "cause": "cut plane missed the solid",
                      "readback": {"completed_nodes": ["sketch"], "failed_node": "boss"},
                      "change": ["declare the measured frame"]})
    second = submit(cad_env, fixed, why)
    assert second["status"] != "pending_confirmation", second
    assert second["repair_attempts_used"] == 1
    for empty in ({}, [], "  "):
        fixed["nodes"][1]["end"] = "blind" if empty == {} else None
        bare = json.dumps({"error_code": "EXTRUSION_FAILED", "cause": "x", "readback": empty, "change": "y"})
        pending = submit(cad_env, fixed, bare)
        assert "证据须完整" in pending["action_summary"], (empty, pending)
        cad_env.orchestrator.cancel_action("job-1", cad_env.token, pending["action_id"])


def test_a_new_job_for_another_part_is_not_held_by_an_old_discarded_chain(cad_env):
    # 2026-11-01: a discarded failure on one drawing made the NEXT drawing's first build (another
    # session) wait for "设计有改动" — models cancelled it and no part came out.
    fail_graph(cad_env)
    assert submit(cad_env, GRAPH)["recovery_status"] == "discarded"
    cad_env.backend.feature_graph_handler = None
    other = copy.deepcopy(GRAPH)
    other["nodes"][0]["profile"][0]["diameter"] = .05
    job, token = cad_env.store.create("next-drawing", "another part", {})
    built = submit(cad_env, other, "{}", job["job_id"], token)
    assert built["status"] == "completed", built
    with cad_env.store.recovery_chain() as ledger:
        assert ledger["active"] is None
        assert ledger["history"][-2]["closed_by"] == "new_job"
        assert ledger["history"][-1]["closed_by"] == "completed"


def test_an_old_chain_that_was_not_discarded_still_needs_the_user(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    with cad_env.store.recovery_chain() as ledger:
        ledger["active"]["attempts"][-1]["recovery_status"] = "recovery_unverified"
    other = copy.deepcopy(GRAPH)
    other["nodes"][0]["profile"][0]["diameter"] = .05
    job, token = cad_env.store.create("next-drawing", "another part", {})
    assert submit(cad_env, other, "{}", job["job_id"], token)["status"] == "pending_confirmation"


def test_evidence_after_a_validation_failure_is_not_a_confirmation(cad_env):
    # The first graph failed validation (nothing ran, no chain); the fix arrives with evidence.
    cad_env.backend.feature_graph_handler = None
    cited = json.dumps({"error_code": "VALIDATION_FAILED", "cause": "rounded_rect radius at the limit",
                        "readback": "compiler rejected node before execution", "change": "slot primitive"})
    assert submit(cad_env, GRAPH, cited)["status"] == "completed"


VALIDATION_CITED = json.dumps({"error_code": "VALIDATION_FAILED", "cause": "selector front unsupported",
                               "readback": "previous build failed at the boss", "change": "declare the frame"})


def test_a_repair_may_cite_the_validation_failure_it_fixed_between_attempts(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    with pytest.raises(RuntimeError, match="VALIDATION_FAILED"):
        submit(cad_env, dict(GRAPH, units="millimeters"))
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][0]["frame"] = FRAME
    repaired = submit(cad_env, fixed, VALIDATION_CITED)
    assert repaired["status"] != "pending_confirmation", repaired
    assert repaired["repair_attempts_used"] == 1


def test_citing_a_validation_failure_that_never_happened_is_not_evidence(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][0]["frame"] = FRAME
    pending = submit(cad_env, fixed, VALIDATION_CITED)
    assert pending["status"] == "pending_confirmation"
    assert "修复证据须完整且对应上次错误" in pending["action_summary"]


def test_another_jobs_edit_neither_waits_for_nor_resets_a_discarded_chain(cad_env):
    fail_graph(cad_env)
    assert submit(cad_env, GRAPH)["recovery_status"] == "discarded"
    job, token = cad_env.store.create("another-job", "edit another part",
                                      cad_env.orchestrator._identity(cad_env.backend.state))
    job["document_write_allowed"] = True
    cad_env.store.save(job)
    edited = cad_env.orchestrator.document_edits("another-job", token,
        json.dumps([{"tool": "modify_dimension", "params": {"name": "D1", "value": .01}}]))
    assert edited["status"] != "pending_confirmation", edited
    with cad_env.store.recovery_chain() as ledger:
        assert ledger["active"] is not None and len(ledger["active"]["attempts"]) == 1
    # The same design from that job is still a repair of the old chain: a replay is caught.
    assert "原样重放" in submit(cad_env, GRAPH, evidence(), "another-job", token)["action_summary"]


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


def test_an_open_undo_scope_names_the_document_to_close(cad_env, monkeypatch):
    # The service reports a scope only while its document is open, and then confirming alone
    # cannot help (the begin would refuse), so the prompt has to say what to close.
    real = cad_env.backend.documents

    def inventory():
        result = real()
        result.update(undo_scope_active=True, undo_scope_document="零件18")
        return result

    monkeypatch.setattr(cad_env.backend, "documents", inventory)
    calls = len(cad_env.backend.calls)
    pending = submit(cad_env, GRAPH)
    assert pending["status"] == "pending_confirmation"
    assert "「零件18」" in pending["action_summary"] and "关掉" in pending["action_summary"]
    assert len(cad_env.backend.calls) == calls


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



# --- graph.expect: the build checked against the approved plan (2026-11-02, 脚踏座 d21731e7) ---

PLANNED = dict(copy.deepcopy(GRAPH), expect={"bbox_mm": [90, 80, 117]})


def readback(cad_env, size_m, volume_m3=None):
    """analyze_model(mass_properties) answers with this envelope (and volume)."""
    features = ["bbox_size=[%s]" % ",".join(str(v) for v in size_m)]
    if volume_m3 is not None:
        features.insert(0, "volume=%s" % volume_m3)

    def handler(tool, params):
        if tool == "analyze_model" and params.get("analysis_type") == "mass_properties":
            return {"status": "COMPLETED", "stateVersion": cad_env.backend.state.get("stateVersion", 1),
                    "cadState": {"features": features}, "error": None}
        return None
    cad_env.backend.call_handler = handler


def plan_cited(number=1):
    return json.dumps({"error_code": "PLAN_MISMATCH", "cause": "seat drawn Ø60 not the planned Ø38 %s" % number,
                       "readback": "built 60x90x103 against planned 80x90x117",
                       "change": "seat diameter back to the plan %s" % number})


def test_a_build_that_misses_the_planned_envelope_is_discarded_as_plan_mismatch(cad_env):
    readback(cad_env, [0.09, 0.103, 0.06], 1.28671e-4)
    response = submit(cad_env, PLANNED)
    assert response["status"] == "verification_failed", response
    assert response["error"]["code"] == "PLAN_MISMATCH"
    assert response["error"]["built_bbox_mm"] == [60, 90, 103]
    assert response["error"]["expected_bbox_mm"] == [80, 90, 117]
    assert response["recovery_status"] == "discarded" and response["created_document_discarded"] is True
    assert response["automatic_retry_allowed"] is True
    tools = [tool for tool, _ in cad_env.backend.calls]
    assert "commit_undo_scope" not in tools and "close_document" in tools
    with cad_env.store.recovery_chain() as ledger:
        assert ledger["active"]["attempts"][-1]["error_code"] == "PLAN_MISMATCH"


def test_a_plan_mismatch_repair_may_change_dimensions_but_not_the_plan(cad_env):
    readback(cad_env, [0.09, 0.103, 0.06])
    submit(cad_env, PLANNED)
    moved_plan = copy.deepcopy(PLANNED)
    moved_plan["nodes"][0]["profile"][0]["diameter"] = .038
    moved_plan["expect"]["bbox_mm"] = [90, 103, 60]
    pending = submit(cad_env, moved_plan, plan_cited())
    assert pending["status"] == "pending_confirmation" and "expect" in pending["action_summary"]
    cad_env.orchestrator.cancel_action("job-1", cad_env.token, pending["action_id"])
    fixed = copy.deepcopy(PLANNED)
    fixed["nodes"][0]["profile"][0]["diameter"] = .038
    readback(cad_env, [0.117, 0.09, 0.08])
    repaired = submit(cad_env, fixed, plan_cited())
    assert repaired["status"] == "completed", repaired
    assert repaired["repair_attempts_used"] == 1
    assert repaired["verification"]["plan_check"]["bbox_ok"] is True


def test_after_a_plan_mismatch_repair_the_corrected_dimensions_are_the_lock(cad_env):
    readback(cad_env, [0.09, 0.103, 0.06])
    submit(cad_env, PLANNED)
    fixed = copy.deepcopy(PLANNED)
    fixed["nodes"][0]["profile"][0]["diameter"] = .038
    fail_graph(cad_env)
    assert submit(cad_env, fixed, plan_cited())["repair_attempts_used"] == 1
    # The next repair answers EXTRUSION_FAILED: only placement may change now, and going back to
    # the mismatched Ø20 is a design change for the user, not a repair.
    back = copy.deepcopy(PLANNED)
    back["nodes"][0]["frame"] = FRAME
    pending = submit(cad_env, back, evidence())
    assert pending["status"] == "pending_confirmation" and "设计有改动" in pending["action_summary"]
    cad_env.orchestrator.cancel_action("job-1", cad_env.token, pending["action_id"])
    placed = copy.deepcopy(fixed)
    placed["nodes"][0]["frame"] = FRAME
    assert submit(cad_env, placed, evidence(2))["repair_attempts_used"] == 2


def test_a_matching_envelope_completes_and_a_volume_miss_is_a_warning(cad_env):
    # a36a476b: planned 93×157×90, built 94.069×157.568×90 — inside max(1 mm, 2%).
    readback(cad_env, [0.094069, 0.157568, 0.09], 1.28671e-4)
    graph = dict(copy.deepcopy(GRAPH), expect={"bbox_mm": [157, 90, 93], "volume_mm3": 100000})
    response = submit(cad_env, graph)
    assert response["status"] == "completed", response
    plan = response["verification"]["plan_check"]
    assert plan["bbox_ok"] is True and plan["volume_ok"] is False
    assert [w["code"] for w in response["warnings"]] == ["PLAN_VOLUME_MISMATCH"]
    assert "+28.7%" in response["warnings"][0]["message"]
    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)
    assert "PLAN_VOLUME_MISMATCH" in [w["code"] for w in finished.get("warnings", [])]


def test_the_dsh_tool_requires_the_plan_envelope_on_a_new_part(cad_env):
    calls = len(cad_env.backend.calls)
    with pytest.raises(RuntimeError, match="VALIDATION_FAILED: a fresh_document graph must carry graph.expect.bbox_mm"):
        cad_env.orchestrator.feature_graph("job-1", cad_env.token, json.dumps(GRAPH), True, "{}",
                                           require_expect=True)
    with pytest.raises(RuntimeError, match="graph has 0 fillet"):
        cad_env.orchestrator.feature_graph(
            "job-1", cad_env.token, json.dumps(dict(PLANNED, expect={"bbox_mm": [90, 80, 117],
                                                                     "features": {"fillet": 1}})),
            True, "{}", require_expect=True)
    assert len(cad_env.backend.calls) == calls
    server = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    assert "require_expect=True" in server
