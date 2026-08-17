import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _edit(tool, **params):
    return {"tool": tool, "params": params}


def _failed(code="TEST_FAILURE"):
    return {
        "status": "FAILED",
        "error": {"code": code, "message": "forced test failure"},
    }


def _request_confirmation(cad_env, pending):
    return cad_env.orchestrator.request_confirmation(
        "job-1",
        cad_env.token,
        pending["action_id"],
    )


def test_document_switch_blocks_write(cad_env, tmp_path):
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")
    cad_env.backend.state.update(
        activeDocument=other.name,
        activeDocumentPath=str(other),
    )

    with pytest.raises(RuntimeError, match="ACTIVE_DOCUMENT_CHANGED"):
        cad_env.orchestrator.document_edits(
            "job-1",
            cad_env.token,
            json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
        )
    assert not cad_env.backend.calls


def test_first_write_backups_source_only_once(cad_env):
    operation = json.dumps(
        [_edit("modify_dimension", name="D1", value=0.01)]
    )
    first = cad_env.orchestrator.document_edits(
        "job-1", cad_env.token, operation
    )
    second = cad_env.orchestrator.document_edits(
        "job-1", cad_env.token, operation
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert first["backup_path"] == second["backup_path"]
    job = cad_env.store.load("job-1", cad_env.token)
    assert len(job["backups"]) == 1
    assert Path(first["backup_path"]).read_bytes() == cad_env.source.read_bytes()


def test_backup_failure_blocks_backend_write(cad_env, monkeypatch):
    def fail_copy(*_args, **_kwargs):
        raise OSError("backup unavailable")

    monkeypatch.setattr(cad_env.module.shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="backup unavailable"):
        cad_env.orchestrator.document_edits(
            "job-1",
            cad_env.token,
            json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
        )
    assert not cad_env.backend.calls


def test_destructive_edit_requires_confirmation_and_token_is_one_time(cad_env):
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit(
                    "edit_feature",
                    feature_name="Boss-Extrude1",
                    action="delete",
                )
            ]
        ),
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "destructive"
    assert "confirmation_token" not in pending
    assert not cad_env.backend.calls

    with pytest.raises(PermissionError, match="not requested"):
        cad_env.orchestrator.confirm_action(
            "job-1",
            cad_env.token,
            pending["action_id"],
            "wrong",
            pending["confirmation_text"],
            pending["action_summary"],
        )
    assert not cad_env.backend.calls

    confirmation = _request_confirmation(cad_env, pending)
    with pytest.raises(PermissionError, match="Invalid confirmation token"):
        cad_env.orchestrator.confirm_action(
            "job-1",
            cad_env.token,
            pending["action_id"],
            "wrong",
            confirmation["confirmation_text"],
            confirmation["action_summary"],
        )

    result = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )
    assert result["status"] == "completed"
    assert [call[0] for call in cad_env.backend.calls].count("edit_feature") == 1

    with pytest.raises(ValueError, match="already been consumed"):
        cad_env.orchestrator.confirm_action(
            "job-1",
            cad_env.token,
            pending["action_id"],
            confirmation["confirmation_token"],
            confirmation["confirmation_text"],
            confirmation["action_summary"],
        )


def test_expired_and_changed_pending_actions_are_rejected(cad_env):
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit(
                    "edit_feature",
                    feature_name="Boss-Extrude1",
                    action="delete",
                )
            ]
        ),
    )
    confirmation = _request_confirmation(cad_env, pending)
    job = cad_env.store.load("job-1", cad_env.token)
    action = job["pending_actions"][pending["action_id"]]
    action["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    cad_env.store.save(job)
    with pytest.raises(ValueError, match="expired"):
        cad_env.orchestrator.confirm_action(
            "job-1",
            cad_env.token,
            pending["action_id"],
            confirmation["confirmation_token"],
            confirmation["confirmation_text"],
            confirmation["action_summary"],
        )

    action["created_at"] = datetime.now(timezone.utc).isoformat()
    action["payload"]["operations"][0]["params"]["feature_name"] = "Changed"
    cad_env.store.save(job)
    with pytest.raises(PermissionError, match="parameters changed"):
        cad_env.orchestrator.confirm_action(
            "job-1",
            cad_env.token,
            pending["action_id"],
            confirmation["confirmation_token"],
            confirmation["confirmation_text"],
            confirmation["action_summary"],
        )


def test_confirmation_requeues_when_file_risk_changes(cad_env, tmp_path):
    target = tmp_path / "export.step"
    target.write_bytes(b"existing")
    pending = cad_env.orchestrator.save_or_export(
        "job-1",
        cad_env.token,
        "export",
        str(target),
        "STEP",
    )
    assert pending["risk"] == "overwrite"
    confirmation = _request_confirmation(cad_env, pending)
    target.unlink()

    refreshed = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )
    assert refreshed["status"] == "pending_confirmation"
    assert refreshed["risk"] == "write"
    assert refreshed["action_id"] != pending["action_id"]
    assert not cad_env.backend.calls


def test_confirmation_is_bound_to_exact_action_summary(cad_env):
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit(
                    "edit_feature",
                    feature_name="Boss-Extrude1",
                    action="delete",
                )
            ]
        ),
    )
    confirmation = _request_confirmation(cad_env, pending)

    with pytest.raises(PermissionError, match="action_summary"):
        cad_env.orchestrator.confirm_action(
            "job-1",
            cad_env.token,
            pending["action_id"],
            confirmation["confirmation_token"],
            confirmation["confirmation_text"],
            confirmation["action_summary"] + " changed",
        )

    result = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )
    assert result["status"] == "completed"


def test_confirmation_requeues_when_summary_changes(
    cad_env, monkeypatch
):
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit(
                    "edit_feature",
                    feature_name="Boss-Extrude1",
                    action="delete",
                )
            ]
        ),
    )
    confirmation = _request_confirmation(cad_env, pending)
    job = cad_env.store.load("job-1", cad_env.token)
    action = job["pending_actions"][pending["action_id"]]

    monkeypatch.setattr(
        cad_env.orchestrator,
        "_reassess_risk",
        lambda _kind, _payload, _job: (
            action["payload"],
            action["risk"],
            action["summary"] + " Reassessed.",
        ),
    )
    refreshed = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )

    assert refreshed["status"] == "pending_confirmation"
    assert refreshed["action_summary"].endswith("Reassessed.")
    assert not cad_env.backend.calls


def test_partial_failure_stops_remaining_edits(cad_env):
    write_calls = 0

    def handler(tool, _params):
        nonlocal write_calls
        if tool == "modify_dimension":
            write_calls += 1
            if write_calls == 2:
                return _failed()
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit("modify_dimension", name="D1", value=0.01),
                _edit("modify_dimension", name="D2", value=0.02),
                _edit("modify_dimension", name="D3", value=0.03),
            ]
        ),
    )
    assert result["status"] == "failed"
    assert result["completed_steps"] == ["modify_dimension"]
    assert result["recovery_status"] == "rolled_back"
    assert write_calls == 2


def test_document_edit_transaction_commits_only_after_verification(cad_env):
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )

    assert result["status"] == "completed"
    tools = [tool for tool, _params in cad_env.backend.calls]
    assert tools.index("begin_undo_scope") < tools.index("modify_dimension")
    assert tools.index("modify_dimension") < tools.index("analyze_model")
    assert tools.index("analyze_model") < tools.index("commit_undo_scope")
    assert "rollback_undo_scope" not in tools


def test_verification_failure_rolls_back_and_is_never_completed(cad_env):
    def handler(tool, params):
        if (
            tool == "analyze_model"
            and params.get("analysis_type") == "mass_properties"
        ):
            return _failed("VERIFY_FAILED")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )

    assert result["status"] == "verification_failed"
    assert result["execution_status"] == "completed"
    assert result["verification_status"] == "failed"
    assert result["recovery_status"] == "rolled_back"
    tools = [tool for tool, _params in cad_env.backend.calls]
    assert "rollback_undo_scope" in tools
    assert "commit_undo_scope" not in tools


def test_rollback_failure_reports_partial_state(cad_env):
    modify_calls = 0

    def handler(tool, _params):
        nonlocal modify_calls
        if tool == "modify_dimension":
            modify_calls += 1
            if modify_calls == 2:
                return _failed("EDIT_FAILED")
        if tool == "rollback_undo_scope":
            return _failed("ROLLBACK_FAILED")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit("modify_dimension", name="D1", value=0.01),
                _edit("modify_dimension", name="D2", value=0.02),
            ]
        ),
    )

    assert result["status"] == "partial"
    assert result["execution_status"] == "partial"
    assert result["recovery_status"] == "rollback_failed"
    assert result["trust_level"] == "unverified"


def test_failed_create_drawing_does_not_replace_expected_document(cad_env):
    def handler(tool, _params):
        if tool == "create_drawing":
            return _failed("DRAWING_FAILED")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1", cad_env.token, json.dumps({"views": []})
    )
    assert result["status"] == "failed"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"]["path"] == str(cad_env.source)


def test_drawing_workflow_uses_the_required_operation_order(cad_env):
    workflow = {
        "model_path": str(cad_env.source),
        "views": [{"view_type": "front"}],
        "flat_pattern": {},
        "section_views": [
            {
                "x1": 0.1,
                "y1": 0.2,
                "x2": 0.3,
                "y2": 0.2,
                "px": 0.4,
                "py": 0.5,
            }
        ],
        "detail_views": [{"source_view": "Drawing View1"}],
        "model_annotations": True,
        "auto_dimensions": True,
        "center_marks": True,
        "bom": True,
        "auto_balloons": True,
        "hole_table": True,
        "standards_check": True,
    }
    pending = cad_env.orchestrator.drawing_workflow(
        "job-1", cad_env.token, json.dumps(workflow)
    )
    confirmation = _request_confirmation(cad_env, pending)
    result = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )

    assert result["status"] == "completed"
    tools = [tool for tool, _params in cad_env.backend.calls]
    expected = [
        "create_drawing",
        "begin_undo_scope",
        "add_drawing_view",
        "add_flat_pattern_view",
        "add_section_view",
        "create_detail_view",
        "import_model_annotations",
        "auto_dimension_drawing",
        "auto_center_marks",
        "create_bom",
        "add_balloons",
        "create_hole_table",
        "check_drawing_standards",
        "analyze_slddrw_test",
        "commit_undo_scope",
    ]
    positions = [tools.index(tool) for tool in expected]
    assert positions == sorted(positions)


def test_batch_export_reports_missing_or_empty_outputs(cad_env, tmp_path):
    base = tmp_path / "batch" / "part"

    def handler(tool, params):
        if tool == "batch_export":
            Path(str(base) + ".step").parent.mkdir(parents=True, exist_ok=True)
            Path(str(base) + ".step").write_bytes(b"step-data")
            Path(str(base) + ".stl").write_bytes(b"")
        return None

    cad_env.backend.call_handler = handler
    pending = cad_env.orchestrator.save_or_export(
        "job-1",
        cad_env.token,
        "batch_export",
        str(base),
        formats_json='["STEP", "STL", "IGES"]',
    )
    confirmation = _request_confirmation(cad_env, pending)
    result = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )

    assert result["status"] == "verification_failed"
    assert result["error"]["code"] == "OUTPUT_VERIFICATION_FAILED"
    checks = result["verification"]["output_files"]
    assert checks[str(base) + ".step"]["valid"] is True
    assert checks[str(base) + ".stl"]["valid"] is False
    assert checks[str(base) + ".igs"]["valid"] is False


def test_empty_single_export_is_not_reported_as_verified(cad_env, tmp_path):
    target = tmp_path / "empty.step"

    def handler(tool, _params):
        if tool == "export_document":
            target.write_bytes(b"")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.save_or_export(
        "job-1",
        cad_env.token,
        "export",
        str(target),
        "STEP",
    )
    assert result["status"] == "verification_failed"
    assert result["verification"]["all_outputs_valid"] is False


def test_closed_target_cannot_be_reused_and_can_be_finished(cad_env):
    closed = cad_env.orchestrator.save_or_export(
        "job-1",
        cad_env.token,
        "close",
    )
    assert closed["status"] == "completed"
    assert closed["verification"]["target_closed"] is True

    with pytest.raises(ValueError, match="already closed"):
        cad_env.orchestrator.document_edits(
            "job-1",
            cad_env.token,
            json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
        )

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)
    assert finished["status"] == "completed"
    assert finished["verification"]["target_closed"] is True


def test_finish_job_stays_active_when_final_verification_fails(cad_env):
    def handler(tool, _params):
        if tool == "analyze_model":
            return _failed("FINAL_VERIFY_FAILED")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    assert result["status"] == "verification_failed"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["status"] == "active"
    assert job["verification_status"] == "failed"


def test_drawing_stops_if_created_document_is_not_still_active(cad_env):
    created_state = {
        **cad_env.backend.state,
        "activeDocument": "Drawing1",
        "activeDocumentPath": "",
        "documentType": "DRAWING",
    }
    switched_state = {
        **created_state,
        "activeDocument": "OtherDrawing",
    }

    def handler(tool, _params):
        if tool == "create_drawing":
            cad_env.backend.state = switched_state
            return {
                "status": "COMPLETED",
                "stateVersion": 2,
                "cadState": created_state,
                "error": None,
            }
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps(
            {
                "views": [
                    {
                        "view_type": "front",
                        "model_path": str(cad_env.source),
                    }
                ]
            }
        ),
    )

    assert result["status"] == "partial"
    assert result["error"]["code"] == (
        "ACTIVE_DOCUMENT_CHANGED_DURING_OPERATION"
    )
    assert [tool for tool, _ in cad_env.backend.calls] == ["create_drawing"]
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"]["title"] == "Drawing1"
    assert job["recovery_status"] == "rollback_failed"

    with pytest.raises(RuntimeError, match="ACTIVE_DOCUMENT_CHANGED"):
        cad_env.orchestrator.document_edits(
            "job-1",
            cad_env.token,
            json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
        )


def test_drawing_recovery_never_closes_a_switched_document(cad_env):
    def handler(tool, _params):
        if tool == "add_drawing_view":
            cad_env.backend.state = {
                **cad_env.backend.state,
                "activeDocument": cad_env.source.name,
                "activeDocumentPath": str(cad_env.source),
                "documentType": "PART",
            }
            return _failed("DRAWING_STEP_FAILED")
        if tool == "rollback_undo_scope":
            return _failed("ACTIVE_DOCUMENT_CHANGED")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps(
            {
                "views": [
                    {
                        "view_type": "front",
                        "model_path": str(cad_env.source),
                    }
                ]
            }
        ),
    )

    assert result["status"] == "partial"
    assert result["recovery_status"] == "rollback_failed"
    tools = [tool for tool, _ in cad_env.backend.calls]
    assert "close_document" not in tools
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"]["title"] == "Drawing1"
