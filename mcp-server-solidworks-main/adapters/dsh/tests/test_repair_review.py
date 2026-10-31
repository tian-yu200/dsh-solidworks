"""Claude review B1/B2/B4: exercise public orchestration, not caller counters."""
import copy
import json

import pytest

from test_recovery_contract import GRAPH, evidence, fail_graph, submit


def approve(env, pending):
    assert pending["status"] == "pending_confirmation"
    request = env.orchestrator.request_confirmation("job-1", env.token, pending["action_id"])
    return env.orchestrator.confirm_action(
        "job-1", env.token, pending["action_id"], request["confirmation_token"],
        request["confirmation_text"], request["action_summary"],
    )


def test_unrelated_design_gets_one_confirmation_and_new_chain(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    changed = copy.deepcopy(GRAPH)
    changed["nodes"][0]["profile"][0]["diameter"] = .022
    before = len(cad_env.backend.calls)
    pending = submit(cad_env, changed)
    assert pending["status"] == "pending_confirmation"
    assert "设计" in pending["action_summary"]
    assert len(cad_env.backend.calls) == before
    response = approve(cad_env, pending)
    assert response["repair_attempts_used"] == 0
    with cad_env.store.recovery_chain() as ledger:
        assert ledger["history"][-1]["closed_by"] == "user_override"
        assert len(ledger["active"]["attempts"]) == 1


@pytest.mark.parametrize("change", ["reverse", "offset", "mirror", "translate"])
def test_placement_corrections_are_automatic_with_evidence(cad_env, change):
    fail_graph(cad_env)
    graph = copy.deepcopy(GRAPH)
    graph["nodes"][0]["ref"]["offset"] = .03
    graph["nodes"][0]["profile"][0].update(cx=.074, cy=.095)
    submit(cad_env, graph)
    fixed = copy.deepcopy(graph)
    if change == "reverse":
        fixed["nodes"][1]["reversed"] = True
    elif change == "offset":
        fixed["nodes"][0]["ref"]["offset"] = -.03
    elif change == "mirror":
        fixed["nodes"][0]["profile"][0]["cx"] *= -1
    else:
        fixed["nodes"][0]["profile"][0].update(cx=.1, cy=.2)
    response = submit(cad_env, fixed, evidence())
    assert response["recovery_status"] == "discarded"
    assert response["repair_attempts_used"] == 1


@pytest.mark.parametrize("status", ["in_flight", "rollback_failed"])
def test_unverified_or_inflight_attempt_can_be_overridden(cad_env, status):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    with cad_env.store.recovery_chain() as ledger:
        ledger["active"]["attempts"][-1]["recovery_status"] = status
    pending = submit(cad_env, GRAPH)
    assert "恢复" in pending["action_summary"] or "执行" in pending["action_summary"]
    assert approve(cad_env, pending)["repair_attempts_used"] == 0


def test_context_change_is_confirmation_not_a_dead_end(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    cad_env.backend.state["updateStamp"] = 100
    fixed = copy.deepcopy(GRAPH)
    fixed["nodes"][1]["reversed"] = True
    pending = submit(cad_env, fixed, evidence())
    assert "文档" in pending["action_summary"]
    assert approve(cad_env, pending)["repair_attempts_used"] == 0


def test_block_key_uses_path_or_stable_id_never_title_or_empty(cad_env):
    key = cad_env.orchestrator._document_block_key
    assert key({}) is None
    assert key({"title": "零件1", "type": "PART"}) is None
    assert key({"title": "零件1", "document_id": "one"}) != key({"title": "零件1", "document_id": "two"})
    assert key({"path": "D:/parts/p.sldprt", "title": "old"}) == key({"path": "d:/PARTS/p.sldprt", "title": "new"})


def test_repeated_submit_without_confirmation_does_not_execute(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    pending = submit(cad_env, GRAPH)
    calls = len(cad_env.backend.calls)
    with pytest.raises(RuntimeError, match="GRAPH_PENDING_CONFIRMATION"):
        submit(cad_env, GRAPH)
    assert len(cad_env.backend.calls) == calls
    with cad_env.store.recovery_chain() as ledger:
        assert len(ledger["active"]["attempts"]) == 1
    assert approve(cad_env, pending)["repair_attempts_used"] == 0


def test_exhausted_repairs_confirm_once_and_restart_budget(cad_env):
    fail_graph(cad_env)
    graph = copy.deepcopy(GRAPH)
    submit(cad_env, graph)
    graph["nodes"][1]["reversed"] = True
    assert submit(cad_env, graph, evidence())["repair_attempts_used"] == 1
    graph["nodes"][0]["ref"]["offset"] = .03
    assert submit(cad_env, graph, evidence(2))["repair_attempts_used"] == 2
    graph["nodes"][0]["ref"]["offset"] = -.03
    pending = submit(cad_env, graph, evidence(3))
    assert "已自动修复 2 次" in pending["action_summary"]
    response = approve(cad_env, pending)
    assert response["status"] != "pending_confirmation"
    assert response["repair_attempts_used"] == 0
    graph["nodes"][1]["reversed"] = False
    assert submit(cad_env, graph, evidence(4))["repair_attempts_used"] == 1
    with cad_env.store.recovery_chain() as ledger:
        assert ledger["history"][-1]["closed_by"] == "user_override"
        assert len(ledger["history"][-1]["attempts"]) == 3


def test_large_unrelated_graph_merges_batch_and_recovery_confirmation(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    graph = copy.deepcopy(GRAPH)
    for i in range(5):
        s,e = copy.deepcopy(GRAPH["nodes"])
        s["id"],e["id"],e["sketch"] = "s%s"%i,"e%s"%i,"s%s"%i
        graph["nodes"].extend([s,e])
    pending = submit(cad_env, graph)
    assert "12 个节点" in pending["action_summary"]
    result = approve(cad_env, pending)
    assert result["status"] != "pending_confirmation"
    assert result["repair_attempts_used"] == 0
    job = cad_env.store.load("job-1",cad_env.token)
    assert not [a for a in job["pending_actions"].values() if not a["consumed"]]


def test_context_change_after_approval_request_refreshes_before_execution(cad_env):
    fail_graph(cad_env)
    submit(cad_env, GRAPH)
    pending = submit(cad_env, GRAPH)
    request = cad_env.orchestrator.request_confirmation("job-1",cad_env.token,pending["action_id"])
    cad_env.backend.state["updateStamp"] = 99
    calls = len(cad_env.backend.calls)
    refreshed = cad_env.orchestrator.confirm_action("job-1",cad_env.token,pending["action_id"],
        request["confirmation_token"],request["confirmation_text"],request["action_summary"])
    assert refreshed["status"] == "pending_confirmation"
    assert refreshed["action_id"] != pending["action_id"]
    assert "文档有变化" in refreshed["action_summary"]
    assert len(cad_env.backend.calls) == calls
    assert approve(cad_env,refreshed)["repair_attempts_used"] == 0


def test_invalid_user_confirmation_cannot_reset_chain(cad_env):
    fail_graph(cad_env)
    submit(cad_env,GRAPH)
    pending = submit(cad_env,GRAPH)
    request = cad_env.orchestrator.request_confirmation("job-1",cad_env.token,pending["action_id"])
    calls = len(cad_env.backend.calls)
    with pytest.raises(PermissionError):
        cad_env.orchestrator.confirm_action("job-1",cad_env.token,pending["action_id"],
            "invalid-token",request["confirmation_text"],request["action_summary"])
    assert len(cad_env.backend.calls) == calls
    with cad_env.store.recovery_chain() as ledger:
        assert len(ledger["active"]["attempts"]) == 1
        assert not ledger.get("history")


@pytest.mark.parametrize("legacy",["empty_attempts","old_contract","missing_baseline"])
def test_incomplete_legacy_ledger_can_be_confirmed(cad_env,legacy):
    fail_graph(cad_env)
    submit(cad_env,GRAPH)
    with cad_env.store.recovery_chain() as ledger:
        if legacy == "empty_attempts":ledger["active"]["attempts"] = []
        elif legacy == "old_contract":ledger["active"].pop("design_contract")
        else:ledger["active"].pop("baseline")
    assert approve(cad_env,submit(cad_env,GRAPH))["repair_attempts_used"] == 0


def fail_edit(env):
    env.backend.call_handler = lambda tool,_: ({"status":"FAILED","error":{"code":"EDIT_FAILED","message":"probe"}}
        if tool == "modify_dimension" else None)
    return env.orchestrator.document_edits("job-1",env.token,
        json.dumps([{"tool":"modify_dimension","params":{"name":"D1","value":.01}}]))


@pytest.mark.parametrize("saved",[True,False])
def test_real_failure_records_path_or_document_id_and_user_can_release(cad_env,saved):
    if not saved:
        cad_env.backend.state["activeDocumentPath"] = ""
        job = cad_env.store.load("job-1",cad_env.token)
        job["expected_document"]["path"] = ""
        cad_env.store.save(job)
    fail_edit(cad_env)
    with cad_env.store.recovery_chain() as ledger:
        assert len(ledger["blocked_documents"]) == 1
        key,record = next(iter(ledger["blocked_documents"].items()))
        assert key.startswith("path:" if saved else "document:")
        assert record["identity"]["document_id"] == "existing-0"
    cad_env.backend.call_handler = None
    pending = cad_env.orchestrator.document_edits("job-1",cad_env.token,
        json.dumps([{"tool":"modify_dimension","params":{"name":"D1","value":.01}}]))
    assert "modify_dimension" in pending["action_summary"]
    assert approve(cad_env,pending)["status"] == "completed"
    with cad_env.store.recovery_chain() as ledger:
        assert not ledger["blocked_documents"]
        assert ledger["retired_document_blocks"][-1]["closed_by"] == "user_override"


@pytest.mark.parametrize("saved",[True,False])
def test_disappeared_document_does_not_block_same_named_new_instance(cad_env,saved):
    if not saved:
        cad_env.backend.state["activeDocumentPath"] = ""
        job = cad_env.store.load("job-1",cad_env.token)
        job["expected_document"]["path"] = ""
        cad_env.store.save(job)
    fail_edit(cad_env)
    cad_env.backend.state["documentId"] = "replacement-id"
    cad_env.backend.call_handler = None
    job,token = cad_env.store.create("new-job","new instance",cad_env.orchestrator._identity(cad_env.backend.state))
    job["document_write_allowed"] = True
    cad_env.store.save(job)
    result = cad_env.orchestrator.document_edits(job["job_id"],token,
        json.dumps([{"tool":"modify_dimension","params":{"name":"D1","value":.01}}]))
    assert result["status"] == "completed"
    with cad_env.store.recovery_chain() as ledger:
        assert not ledger["blocked_documents"]
        assert ledger["retired_document_blocks"]


def test_empty_identity_never_creates_document_block(cad_env):
    job = cad_env.store.load("job-1",cad_env.token)
    job["expected_document"] = {}
    cad_env.orchestrator._record_failure(job,kind="document_edits",phase="execute",
        error={"code":"TEST"},execution_status="failed",verification_status_value="unavailable",recovery_status="rollback_failed")
    with cad_env.store.recovery_chain() as ledger:
        assert not ledger.get("blocked_documents")


def profile_contract(profile):
    from repair_policy import design_contract
    graph = copy.deepcopy(GRAPH)
    graph["nodes"][0]["profile"] = profile
    return design_contract(graph)


@pytest.mark.parametrize("transform",["rotate","mirror","translate"])
def test_multisegment_sketch_allows_one_rigid_transform(transform):
    import math
    from repair_policy import dimensions_equal
    original = [{"kind":"line","x1":0,"y1":0,"x2":.03,"y2":.04},
                {"kind":"arc","cx":.03,"cy":.04,"x1":.04,"y1":.04,"x2":.03,"y2":.05,"dir":1},
                {"kind":"circle","cx":.09,"cy":.11,"diameter":.02}]
    changed = copy.deepcopy(original)
    for p in changed:
        for x,y in [("cx","cy"),("x1","y1"),("x2","y2")]:
            if x not in p:continue
            a,b = p[x],p[y]
            if transform == "rotate":p[x],p[y] = a*math.cos(.7)-b*math.sin(.7),a*math.sin(.7)+b*math.cos(.7)
            elif transform == "mirror":p[x],p[y] = -a,b
            else:p[x],p[y] = a+.2,b-.3
        if transform == "mirror" and p["kind"] == "arc":p["dir"] *= -1
    assert dimensions_equal(profile_contract(original),profile_contract(list(reversed(changed))))


@pytest.mark.parametrize("change",["line_length","circle_spacing","spline_order","homometric"])
def test_profile_rejects_size_or_local_shape_changes(change):
    from repair_policy import dimensions_equal
    if change == "line_length":
        a = [{"kind":"line","x1":0,"y1":0,"x2":.03,"y2":.04}]
        b = copy.deepcopy(a);b[0]["x2"] += .01
    elif change == "circle_spacing":
        a = [{"kind":"circle","cx":i*.02,"cy":0,"diameter":.01} for i in range(3)]
        b = copy.deepcopy(a);b[1]["cy"] = .01
    elif change == "spline_order":
        a = [{"kind":"spline","points":[0,0,.01,0,.02,.03,.05,.01]}]
        b = [{"kind":"spline","points":[0,0,.02,.03,.01,0,.05,.01]}]
    else:
        a = [{"kind":"circle","cx":i*.001,"cy":0,"diameter":.001} for i in [0,1,4,10,12,17]]
        b = [{"kind":"circle","cx":i*.001,"cy":0,"diameter":.001} for i in [0,1,8,11,13,17]]
    assert not dimensions_equal(profile_contract(a),profile_contract(b))


@pytest.mark.parametrize("delta,equal",[(.5e-9,True),(2e-9,False)])
def test_dimension_tolerance_is_one_nanometer(delta,equal):
    from repair_policy import dimensions_equal,design_contract
    a = copy.deepcopy(GRAPH);b = copy.deepcopy(a)
    b["nodes"][0]["profile"][0]["diameter"] += delta
    assert dimensions_equal(design_contract(a),design_contract(b)) == equal


@pytest.mark.parametrize("node",[
    {"id":"f","type":"fillet","radius":.002},
    {"id":"f","type":"chamfer","distance":.002,"angle":.7},
    {"id":"f","type":"hole","diameter":.01,"depth":.03,"standard":"ISO"},
    {"id":"f","type":"linear_pattern","count":3,"spacing":.02},
    {"id":"f","type":"revolve","angle":3.0},
    {"id":"f","type":"shell","thickness":.002},
    {"id":"f","type":"cosmetic_thread","pitch":.001,"specification":"M8"},
])
def test_all_nonplacement_dimensions_remain_locked(node):
    from repair_policy import dimensions_equal,design_contract
    graph = {**GRAPH,"nodes":[node]}
    for field,value in node.items():
        if field in ("id","type"):continue
        changed = copy.deepcopy(graph)
        changed["nodes"][0][field] = value+1 if isinstance(value,(int,float)) else value+" changed"
        assert not dimensions_equal(design_contract(graph),design_contract(changed)),field


@pytest.mark.parametrize("saved",[True,False])
def test_document_block_tracks_same_instance_after_save_as(cad_env,saved):
    doc = cad_env.backend.documents()["active_document"]
    if not saved:doc["path"] = ""
    key = cad_env.orchestrator._document_block_key(doc)
    moved = {**doc,"path":"D:/parts/renamed.sldprt","title":"renamed"}
    ledger = {"blocked_documents":{key:{"identity":doc}}}
    cad_env.orchestrator._prune_document_blocks(ledger,{"documents":[moved]})
    new_key = cad_env.orchestrator._document_block_key(moved)
    assert list(ledger["blocked_documents"]) == [new_key]
    assert ledger["blocked_documents"][new_key]["identity"]["document_id"] == doc["document_id"]


def test_concurrent_ledger_change_refreshes_one_concrete_confirmation(cad_env,monkeypatch):
    fail_graph(cad_env)
    submit(cad_env,GRAPH)
    original = cad_env.orchestrator._authorize_recovery_execution
    raced = False
    def changed(*args,**kwargs):
        nonlocal raced
        prepared = original(*args,**kwargs)
        if not raced:
            raced = True
            with cad_env.store.recovery_chain() as ledger:
                ledger["active"]["attempts"][-1]["recovery_status"] = "in_flight"
        return prepared
    monkeypatch.setattr(cad_env.orchestrator,"_authorize_recovery_execution",changed)
    graph = copy.deepcopy(GRAPH);graph["nodes"][1]["reversed"] = True
    calls = len(cad_env.backend.calls)
    pending = submit(cad_env,graph,evidence())
    assert pending["status"] == "pending_confirmation"
    assert "上次执行或恢复未验证" in pending["action_summary"]
    assert len(cad_env.backend.calls) == calls
    assert approve(cad_env,pending)["repair_attempts_used"] == 0



def test_existing_graph_executes_and_failed_retry_requires_user_confirmation(cad_env):
    # The existing-document path must not call the retired hard-refusal helper.
    success = cad_env.orchestrator.feature_graph("job-1",cad_env.token,json.dumps(GRAPH),False)
    assert success["status"] == "completed"
    fail_graph(cad_env)
    failed = cad_env.orchestrator.feature_graph("job-1",cad_env.token,json.dumps(GRAPH),False)
    assert failed["recovery_status"] == "recovery_unverified"
    assert failed["automatic_retry_allowed"] is False
    calls = len(cad_env.backend.calls)
    pending = cad_env.orchestrator.feature_graph("job-1",cad_env.token,json.dumps(GRAPH),False)
    assert pending["status"] == "pending_confirmation"
    assert len(cad_env.backend.calls) == calls
    cad_env.backend.feature_graph_handler = None
    assert approve(cad_env,pending)["status"] == "completed"



def test_terminating_face_location_repair_preserves_up_to_end_type(cad_env):
    fail_graph(cad_env)
    graph = copy.deepcopy(GRAPH)
    graph["nodes"][1].update(end="up_to_surface",up_to={"face":{"near":[0,0,.03]}})
    graph["nodes"][1].pop("depth")
    submit(cad_env,graph)
    graph["nodes"][1]["up_to"]["face"]["near"] = [.01,0,.03]
    assert submit(cad_env,graph,evidence())["repair_attempts_used"] == 1


def test_edge_anchor_location_can_change_but_fillet_radius_cannot():
    from repair_policy import dimensions_equal,design_contract
    a = {**GRAPH,"nodes":[{"id":"f","type":"fillet","radius":.002,
                           "edges":[{"near":[0,0,0]}]}]}
    b = copy.deepcopy(a);b["nodes"][0]["edges"][0]["near"] = [.01,.02,.03]
    assert dimensions_equal(design_contract(a),design_contract(b))
    b["nodes"][0]["radius"] = .003
    assert not dimensions_equal(design_contract(a),design_contract(b))


def test_changed_line_length_requires_confirmation_not_automatic_repair(cad_env):
    fail_graph(cad_env)
    graph = copy.deepcopy(GRAPH)
    graph["nodes"][0]["profile"] = [
        {"kind":"line","x1":0,"y1":0,"x2":.03,"y2":0},
        {"kind":"line","x1":.03,"y1":0,"x2":.03,"y2":.04},
        {"kind":"line","x1":.03,"y1":.04,"x2":0,"y2":0},
    ]
    submit(cad_env,graph)
    graph["nodes"][0]["profile"][0]["x2"] = .04
    graph["nodes"][0]["profile"][1]["x1"] = .04
    calls = len(cad_env.backend.calls)
    pending = submit(cad_env,graph,evidence())
    assert pending["status"] == "pending_confirmation"
    assert "设计有改动" in pending["action_summary"]
    assert len(cad_env.backend.calls) == calls
