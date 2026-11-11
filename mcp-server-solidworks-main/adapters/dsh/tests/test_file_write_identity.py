import copy
import json
from pathlib import Path


def build_component(env):
    graph = {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
        {"id": "profile", "type": "sketch", "ref": {"datum": "front"},
         "profile": [{"kind": "circle", "diameter": .010}]},
        {"id": "body", "type": "extrude", "sketch": "profile", "operation": "boss", "depth": .020},
    ]}
    built = env.orchestrator.feature_graph("job-1", env.token, json.dumps(graph), True)
    assert built["status"] == "completed", built
    return copy.deepcopy(env.backend.state)


def test_save_refuses_source_switch_after_precheck_without_creating_output(cad_env, tmp_path):
    env = cad_env
    owned = build_component(env)
    original = copy.deepcopy(env.backend._document_stack[-1])
    output = tmp_path / "component.SLDPRT"

    def switch_before_save(tool, params):
        if tool == "save_document":
            # UI activation changes the source without advancing the execution counter.
            env.backend.state, env.backend._document_stack[-1] = (
                copy.deepcopy(original), copy.deepcopy(owned))

    env.backend.call_handler = switch_before_save
    saved = env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))

    assert saved["status"] == "failed", saved
    assert saved["error"]["code"] == "ACTIVE_DOCUMENT_CHANGED", saved
    assert not output.exists()
    assert env.backend.state == original
    assert env.backend._document_stack[-1] == owned
    assert env.source.read_bytes() == b"solidworks-test-document"


def test_save_cannot_rebind_to_another_instance_even_at_the_requested_path(cad_env, tmp_path):
    env = cad_env
    owned = build_component(env)
    original = copy.deepcopy(env.backend._document_stack[-1])
    output = tmp_path / "component.SLDPRT"

    def replace_after_save(tool, params):
        if tool == "save_document":
            # A successful transport and matching target path do not prove the source instance.
            Path(params["file_path"]).write_bytes(b"wrong-document")
            wrong = {**copy.deepcopy(original), "activeDocument": output.name,
                     "activeDocumentPath": str(output)}
            env.backend.state = wrong
            env.backend._document_stack[-1] = copy.deepcopy(owned)
            return {"status": "COMPLETED", "cadState": copy.deepcopy(wrong), "error": None,
                    "result_geometry": {"source_document_id": original["documentId"]}}

    env.backend.call_handler = replace_after_save
    saved = env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))

    assert saved["status"] == "verification_failed", saved
    assert saved["verification"]["identity_match"] is False
    job = env.store.load("job-1", env.token)
    assert job["expected_document"]["document_id"] == owned["documentId"]
    assert job["expected_document"]["path"] == ""
    assert job["trust_level"] != "verified"
    assert job["file_write_identity_evidence"]["after"]["active_document"]["document_id"] == original["documentId"]


def test_finish_identity_drift_during_autoclose_is_not_verified(cad_env, tmp_path):
    env = cad_env
    build_component(env)
    original = copy.deepcopy(env.backend._document_stack[-1])
    output = tmp_path / "component.SLDPRT"
    saved = env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))
    assert saved["status"] == "completed", saved

    def switch_before_close(tool, params):
        if tool == "close_document":
            env.backend.state = copy.deepcopy(original)

    env.backend.call_handler = switch_before_close
    finished = env.orchestrator.finish_job("job-1", env.token)

    assert finished["status"] == "verification_failed", finished
    assert finished["verification_status"] == "failed"
    assert finished["trust_level"] != "verified"
    assert finished["close_error"]["code"] == "ACTIVE_DOCUMENT_CHANGED"
    assert env.store.load("job-1", env.token)["status"] != "completed"


def test_legacy_execution_service_cannot_receive_file_writes(cad_env, tmp_path, monkeypatch):
    env = cad_env
    build_component(env)
    real_documents = env.backend.documents

    def legacy_documents():
        inventory = real_documents()
        inventory.pop("file_write_identity_guard")
        return inventory

    monkeypatch.setattr(env.backend, "documents", legacy_documents)
    output = tmp_path / "component.SLDPRT"
    saved = env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))
    assert saved["status"] == "failed"
    assert saved["error"]["code"] == "EXECUTION_SERVICE_UPGRADE_REQUIRED"
    assert not output.exists()
    assert not any(tool == "save_document" for tool, _ in env.backend.calls)


def test_created_source_replaced_by_same_title_instance_is_refused(cad_env, tmp_path):
    env = cad_env
    build_component(env)
    env.backend.state["documentId"] = "same-title-replacement"
    output = tmp_path / "component.SLDPRT"
    saved = env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))
    assert saved["status"] == "failed"
    assert saved["error"]["code"] == "ACTIVE_DOCUMENT_CHANGED"
    assert not output.exists()


def test_started_existing_target_binds_its_stable_source_instance(cad_env, tmp_path):
    env = cad_env
    started = env.orchestrator.begin_job("save existing target", target_document=str(env.source))
    job = env.store.load(started["job_id"], started["job_token"])
    assert job["expected_document"]["document_id"] == "existing-0"
    env.backend.state["documentId"] = "reopened-same-path"
    saved = env.orchestrator.save_or_export(started["job_id"], started["job_token"], "save")
    assert saved["status"] == "failed"
    assert saved["error"]["code"] == "ACTIVE_DOCUMENT_CHANGED"


def test_finish_rejects_reopened_same_path_instance_without_reusing_pass(cad_env, tmp_path):
    env = cad_env
    build_component(env)
    output = tmp_path / "component.SLDPRT"
    assert env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))["status"] == "completed"
    env.backend.state["documentId"] = "reopened-same-path"
    finished = env.orchestrator.finish_job("job-1", env.token)
    assert finished["status"] == "verification_failed", finished
    assert finished["verification"]["identity_match"] is False
    assert finished["verification"].get("deep_checks_reused") is not True
    assert not any(tool == "close_document" for tool, _ in env.backend.calls)


def test_autoclose_refuses_same_path_replacement_after_finish_precheck(cad_env, tmp_path):
    env = cad_env
    build_component(env)
    output = tmp_path / "component.SLDPRT"
    assert env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))["status"] == "completed"

    def reopen_before_close(tool, params):
        if tool == "close_document":
            env.backend.state["documentId"] = "reopened-during-autoclose"

    env.backend.call_handler = reopen_before_close
    finished = env.orchestrator.finish_job("job-1", env.token)

    assert finished["status"] == "verification_failed", finished
    assert finished["close_error"]["code"] == "ACTIVE_DOCUMENT_CHANGED"
    assert env.backend.state["documentId"] == "reopened-during-autoclose"
    assert env.backend.state["activeDocumentPath"] == str(output)
    assert finished["trust_level"] != "verified"


def test_explicit_close_refuses_same_path_replacement_after_precheck(cad_env, tmp_path):
    env = cad_env
    build_component(env)
    output = tmp_path / "component.SLDPRT"
    assert env.orchestrator.save_or_export("job-1", env.token, "save", file_path=str(output))["status"] == "completed"

    def reopen_before_close(tool, params):
        if tool == "close_document":
            env.backend.state["documentId"] = "reopened-during-close"

    env.backend.call_handler = reopen_before_close
    closed = env.orchestrator.save_or_export("job-1", env.token, "close")
    assert closed["status"] == "failed", closed
    assert closed["error"]["code"] == "ACTIVE_DOCUMENT_CHANGED"
    assert env.backend.state["documentId"] == "reopened-during-close"


def test_historical_job_without_stable_id_cannot_claim_the_current_instance(cad_env, tmp_path):
    env = cad_env
    job = env.store.load("job-1", env.token)
    job["expected_document"].pop("document_id")
    env.store.save(job)
    saved = env.orchestrator.save_or_export("job-1", env.token, "save")
    assert saved["status"] == "failed", saved
    assert saved["error"]["code"] == "DOCUMENT_IDENTITY_UNVERIFIED"
    assert not any(tool == "save_document" for tool, _ in env.backend.calls)
