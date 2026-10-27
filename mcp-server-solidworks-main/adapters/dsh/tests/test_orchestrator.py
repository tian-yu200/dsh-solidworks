import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from policy import payload_hash, secret_hash
from models import utc_now


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

def test_iges_export_verifies_the_backend_igs_output(cad_env, tmp_path):
    requested = tmp_path / "model.iges"
    actual = tmp_path / "model.igs"

    def handler(tool, params):
        if tool == "export_document":
            assert params["file_path"] == str(requested)
            actual.write_bytes(b"iges-data")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.save_or_export(
        "job-1",
        cad_env.token,
        "export",
        str(requested),
        "IGES",
    )

    assert result["status"] == "completed"
    assert result["verification"]["output_files"][str(actual)]["valid"] is True


def test_batch_export_requires_confirmation_before_overwriting_outputs(
    cad_env, tmp_path
):
    base = tmp_path / "exports" / "model"
    base.parent.mkdir()
    (tmp_path / "exports" / "model.igs").write_bytes(b"existing")

    pending = cad_env.orchestrator.save_or_export(
        "job-1",
        cad_env.token,
        "batch_export",
        str(base),
        formats_json='["IGES"]',
    )

    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "overwrite"
    assert not any(call[0] == "batch_export" for call in cad_env.backend.calls)


def test_confirm_action_accepts_a_non_ascii_action_summary(cad_env):
    # hmac.compare_digest raises TypeError on non-ASCII str, which used to make any action
    # whose summary contains a Chinese feature name (or a Chinese output path) permanently
    # unconfirmable. The comparison is now over utf-8 bytes.
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                _edit(
                    "edit_feature",
                    feature_name="阀体-Extrude1",
                    action="delete",
                )
            ]
        ),
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "destructive"

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
    assert [call[0] for call in cad_env.backend.calls].count("edit_feature") == 1


def test_confirm_action_accepts_a_non_ascii_export_path(cad_env, tmp_path):
    target = tmp_path / "法兰.step"
    target.write_bytes(b"existing")

    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "export", str(target), "STEP"
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "overwrite"

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
    assert "export_document" in [call[0] for call in cad_env.backend.calls]


@pytest.mark.parametrize("stem", ["..", "."])
def test_safe_name_never_returns_a_dot_segment(cad_env, stem):
    assert cad_env.orchestrator._safe_name(stem) == "document"


def test_backup_directory_stays_inside_the_backup_root(cad_env, tmp_path):
    # A document literally named "...SLDPRT" has stem "..", which used to become the backup
    # sub-directory name and walked the backup one level above BACKUP_ROOT.
    source = tmp_path / "...SLDPRT"
    source.write_bytes(b"traversal")
    state = dict(cad_env.backend.state)
    state.update(
        activeDocument=source.name,
        activeDocumentPath=str(source),
    )
    cad_env.backend.state = state

    backup_path = cad_env.orchestrator._backup_once(cad_env.job, state)

    root = Path(cad_env.backup_root).resolve()
    assert Path(backup_path).resolve().is_relative_to(root)
    assert Path(backup_path).read_bytes() == b"traversal"


def test_failed_post_step_verification_is_not_reported_as_completed(cad_env, tmp_path):
    other = tmp_path / "other.SLDPRT"

    def handler(tool, _params):
        if tool == "modify_dimension":
            # The write "succeeds" but the active document changes underneath it, so the
            # post-step verification of THAT operation fails.
            cad_env.backend.state = {
                "stateVersion": 2,
                "activeDocument": other.name,
                "activeDocumentPath": str(other),
                "documentType": "PART",
                "features": [],
                "dimensions": [],
                "isDirty": False,
            }
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "POST_STEP_VERIFICATION_FAILED"
    assert result["completed_steps"] == []


def _unbound_victim(env, tmp_path):
    """Open an unrelated document that the unbound job must never touch."""
    victim = tmp_path / "CUSTOMER-PART.SLDPRT"
    victim.write_bytes(b"unrelated")
    env.backend.state = {
        "stateVersion": 42,
        "activeDocument": victim.name,
        "activeDocumentPath": str(victim),
        "documentType": "PART",
        "features": ["Boss-Extrude1"],
        "dimensions": [],
        "isDirty": False,
    }
    return victim


def test_unbound_job_refuses_a_destructive_edit(unbound_env, tmp_path):
    """The probe-8 scenario: a queued destructive edit must not execute on the new document."""
    env = unbound_env
    victim = _unbound_victim(env, tmp_path)
    calls_before = len(env.backend.calls)

    with pytest.raises(RuntimeError, match="JOB_DOCUMENT_UNBOUND"):
        env.orchestrator.document_edits(
            env.job_id,
            env.token,
            json.dumps(
                [_edit("edit_feature", feature_name="Boss-Extrude1", action="delete")]
            ),
        )
    assert env.backend.calls[calls_before:] == []
    assert victim.read_bytes() == b"unrelated"
    assert "edit_feature" not in [call[0] for call in env.backend.calls]


def test_unbound_job_refuses_every_document_bound_operation(
    unbound_env, tmp_path
):
    env = unbound_env
    _unbound_victim(env, tmp_path)
    calls_before = len(env.backend.calls)

    with pytest.raises(RuntimeError, match="JOB_DOCUMENT_UNBOUND"):
        env.orchestrator.feature_graph(
            env.job_id,
            env.token,
            json.dumps({"nodes": []}),
            fresh_document=False,
        )

    with pytest.raises(RuntimeError, match="JOB_DOCUMENT_UNBOUND"):
        env.orchestrator.drawing_workflow(env.job_id, env.token, json.dumps({}))

    for operation, kwargs in (
        ("save", {"file_path": str(tmp_path / "out.SLDPRT")}),
        ("export", {"file_path": str(tmp_path / "out.step"), "format": "STEP"}),
        (
            "batch_export",
            {"file_path": str(tmp_path / "base"), "formats_json": '["STEP"]'},
        ),
        ("close", {}),
    ):
        with pytest.raises(RuntimeError, match="JOB_DOCUMENT_UNBOUND"):
            env.orchestrator.save_or_export(
                env.job_id, env.token, operation, **kwargs
            )
    assert env.backend.calls[calls_before:] == []
    assert env.store.load(env.job_id, env.token)["expected_document"] == {}


def test_unbound_job_refuses_a_pending_confirmation(unbound_env, tmp_path):
    """A stale pending action must not execute once the job is (still) unbound.

    Queueing now requires a bound document, so this simulates the residual case the
    approval-time check exists for: an action left pending by a job that is unbound (e.g. a
    document-creating action whose create failed and rolled the binding back).
    """
    env = unbound_env
    victim = _unbound_victim(env, tmp_path)
    job = env.store.load(env.job_id, env.token)
    job["pending_actions"]["stale-action"] = {
        "action_id": "stale-action",
        "kind": "document_edits",
        "payload": {
            "operations": [
                _edit("edit_feature", feature_name="Boss-Extrude1", action="delete")
            ]
        },
        "payload_hash": payload_hash(
            {
                "operations": [
                    _edit(
                        "edit_feature",
                        feature_name="Boss-Extrude1",
                        action="delete",
                    )
                ]
            }
        ),
        "risk": "destructive",
        "summary": "Apply edits to 'victim': edit_feature",
        "confirmation_token_hash": secret_hash("stale-token"),
        "confirmation_text": "CONFIRM stale-action",
        "created_at": utc_now(),
        "confirmation_requested_at": utc_now(),
        "consumed": False,
    }
    env.store.save(job)
    calls_before = len(env.backend.calls)

    with pytest.raises(RuntimeError, match="JOB_DOCUMENT_UNBOUND"):
        env.orchestrator.confirm_action(
            env.job_id,
            env.token,
            "stale-action",
            "stale-token",
            "CONFIRM stale-action",
            "Apply edits to 'victim': edit_feature",
        )

    assert env.backend.calls[calls_before:] == []
    assert victim.read_bytes() == b"unrelated"
    assert "edit_feature" not in [call[0] for call in env.backend.calls]


def test_unbound_job_binds_through_its_own_feature_graph(unbound_env, tmp_path):
    """The normal new-part flow: start with no document, build a graph, then edit."""
    env = unbound_env
    result = env.orchestrator.feature_graph(
        env.job_id,
        env.token,
        json.dumps(
            {"nodes": [{"id": "n1", "type": "sketch", "plane": "Top Plane"}]}
        ),
        fresh_document=True,
    )

    assert result["status"] == "completed"
    job = env.store.load(env.job_id, env.token)
    assert job["expected_document"]["title"] == "Part1"

    edits = env.orchestrator.document_edits(
        env.job_id,
        env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )
    assert edits["status"] == "completed"


def test_unbound_job_may_close_all_documents(unbound_env, tmp_path):
    """close_all is deliberately document-agnostic, so it stays allowed when unbound."""
    env = unbound_env
    _unbound_victim(env, tmp_path)
    pending = env.orchestrator.save_or_export(
        env.job_id, env.token, "close", close_all=True
    )
    # close_all always discards unsaved changes, so it is queued for confirmation first.
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "destructive"

    confirmation = env.orchestrator.request_confirmation(
        env.job_id, env.token, pending["action_id"]
    )
    result = env.orchestrator.confirm_action(
        env.job_id,
        env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )
    assert result["status"] == "completed"


def test_pending_confirmation_summary_names_the_target_document(cad_env, tmp_path):
    target = tmp_path / "existing.step"
    target.write_bytes(b"existing")
    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "export", str(target), "STEP"
    )
    assert pending["status"] == "pending_confirmation"
    assert str(cad_env.source) in pending["action_summary"]

    edits = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)] * 6),
    )
    assert edits["status"] == "pending_confirmation"
    assert str(cad_env.source) in edits["action_summary"]
    assert edits["action_summary"].startswith("Apply edits to ")


def test_post_verify_exception_rolls_back_and_is_recorded_for_document_edits(
    cad_env,
):
    from backend import ExecutionLayerError

    def handler(tool, params):
        if tool == "analyze_model":
            raise ExecutionLayerError("post-verify transport failure")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )

    tools = [tool for tool, _params in cad_env.backend.calls]
    assert "begin_undo_scope" in tools
    assert "rollback_undo_scope" in tools
    assert "commit_undo_scope" not in tools
    assert result["status"] in {"partial", "verification_failed"}
    assert result["error"]["code"] == "EXECUTION_LAYER_ERROR"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["failure_history"]
    assert job["failure_history"][-1]["phase"] == "verification"
    assert job["verification_status"] in {"failed", "unavailable"}
    assert job["recovery_status"] in {"rolled_back", "rollback_failed"}


def test_post_verify_exception_rolls_back_and_is_recorded_for_save_or_export(
    cad_env, tmp_path
):
    from backend import ExecutionLayerError

    def handler(tool, params):
        if tool == "analyze_model":
            raise ExecutionLayerError("post-verify transport failure")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "export", str(tmp_path / "out.step"), "STEP"
    )

    assert result["status"] == "verification_failed"
    assert result["error"]["code"] == "EXECUTION_LAYER_ERROR"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["failure_history"]
    assert job["failure_history"][-1]["phase"] == "verification"
    assert job["verification_status"] in {"failed", "unavailable"}


def test_post_verify_exception_rolls_back_and_is_recorded_for_drawing_workflow(
    cad_env, tmp_path
):
    from backend import ExecutionLayerError

    def handler(tool, params):
        if tool == "analyze_slddrw_test":
            raise ExecutionLayerError("post-verify transport failure")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps({"views": [{"view_type": "front"}]}),
    )

    tools = [tool for tool, _params in cad_env.backend.calls]
    assert "begin_undo_scope" in tools
    assert "rollback_undo_scope" in tools
    assert "commit_undo_scope" not in tools
    assert result["error"]["code"] == "EXECUTION_LAYER_ERROR"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["failure_history"]
    assert job["failure_history"][-1]["phase"] == "verification"
    assert job["verification_status"] == "failed"


# ---------------------------------------------------------------------------------------------
# SPEED task C — finish_job reuses an unchanged post-verify instead of re-running it
#
# finish_job used to always pay verify_state + two analyze_model calls even when the preceding
# write had JUST verified the same document at the same state_version. It now asks the cheap
# counter (GET /api/tool/state, ~2 ms vs ~2.5 s) first and only re-verifies when something
# actually changed. The document-switch guard is NOT weakened: a stamp carries the identity and
# is compared with the same same_document() predicate the write path uses.
# ---------------------------------------------------------------------------------------------
def _six_node_graph():
    """A representative small part graph: box, 2 holes, fillet, linear pattern, mirror."""
    return {
        "schema_version": "0.7.4-draft",
        "units": "meters",
        "nodes": [
            {"id": "n1", "type": "box", "ref": {"datum": "top"},
             "width": 0.05, "depth": 0.05, "height": 0.02},
            {"id": "n2", "type": "hole",
             "ref": {"node_face": {"node": "n1", "selector": "top"},
                     "position": "center"},
             "diameter": 0.008, "depth": "through_all"},
            {"id": "n3", "type": "hole",
             "ref": {"node_face": {"node": "n1", "selector": "top"},
                     "position": "center"},
             "diameter": 0.006, "depth": "through_all"},
            {"id": "n4", "type": "fillet", "radius": 0.002,
             "edges": [{"near": [0.025, 0.02, 0.025], "hint": "top"}]},
            {"id": "n5", "type": "linear_pattern", "feature": "n2",
             "direction": "x", "spacing": 0.01, "count": 2},
            {"id": "n6", "type": "mirror", "plane": {"datum": "right"},
             "features": ["n2"]},
        ],
    }


def _build_save_finish(env, tmp_path):
    """The task's scenario: build a fresh 6-node graph, save it, then finish the job.

    fresh_document=True on purpose: the Save As safeguard requires confirmation when a
    job-bound SAVED document is saved to another path, while the first save of a document
    the job itself created is exempt. This is the normal build->save flow.
    """
    built = env.orchestrator.feature_graph(
        "job-1", env.token, json.dumps(_six_node_graph()), fresh_document=True
    )
    assert built["status"] == "completed", built
    saved = env.orchestrator.save_or_export(
        "job-1", env.token, "save", file_path=str(tmp_path / "out.SLDPRT")
    )
    # The first save of a job-created untitled document confirms too (USER DECISION on hermes
    # F1), so the canonical build->save->finish flow runs through the confirmation protocol.
    assert saved["status"] == "pending_confirmation", saved
    assert saved["risk"] == "save_as"
    confirmation = _request_confirmation(env, saved)
    saved = env.orchestrator.confirm_action(
        "job-1",
        env.token,
        saved["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )
    assert saved["status"] == "completed", saved
    return built, saved


def test_finish_job_reuses_unchanged_post_verify(cad_env, tmp_path):
    _build_save_finish(cad_env, tmp_path)
    probes_before = len(cad_env.backend.cheap_probes)
    calls_before = len(cad_env.backend.calls)

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    assert finished["status"] == "completed"
    assert finished["verification"]["deep_checks_reused"] is True
    # The cheap counter WAS consulted...
    assert len(cad_env.backend.cheap_probes) == probes_before + 1
    assert cad_env.backend.cheap_probes[-1] == "current_state_version"
    # ...and the expensive path was NOT: no verify_state, no analyze_model.
    new_calls = [tool for tool, _p in cad_env.backend.calls[calls_before:]]
    assert new_calls == [], new_calls
    assert finished["verification"]["all_completed"] is True
    assert finished["trust_level"] == "verified"


def test_finish_job_reverifies_when_state_version_advanced(cad_env, tmp_path):
    _build_save_finish(cad_env, tmp_path)
    # Something wrote to the document after the last post-verify (a manual edit, a rebuild).
    cad_env.backend.state["stateVersion"] += 1
    calls_before = len(cad_env.backend.calls)

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    assert finished["status"] == "completed"
    assert finished["verification"].get("deep_checks_reused") is not True
    new_calls = [tool for tool, _p in cad_env.backend.calls[calls_before:]]
    assert "verify_state" in [name for name, _p in cad_env.backend.events]
    assert new_calls.count("analyze_model") == 2, new_calls


def test_finish_job_reverifies_when_document_switched(cad_env, tmp_path):
    """A document switch does NOT bump state_version, so the stamp must carry identity too."""
    _build_save_finish(cad_env, tmp_path)
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")
    # Same counter, different document: exactly the case a counter-only shortcut would miss.
    cad_env.backend.state.update(
        activeDocument=other.name, activeDocumentPath=str(other)
    )
    calls_before = len(cad_env.backend.calls)

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    new_calls = [tool for tool, _p in cad_env.backend.calls[calls_before:]]
    assert new_calls.count("analyze_model") == 2, new_calls
    assert [e for e, _p in cad_env.backend.events[calls_before:] if e == "verify_state"]
    # The switch is detected and the job does NOT come back verified.
    assert finished["status"] != "completed"
    assert finished["verification"]["all_completed"] is False


def test_finish_job_without_a_stamp_still_verifies(cad_env, tmp_path):
    """No prior post-verify (e.g. a read-only job): the full path must still run."""
    calls_before = len(cad_env.backend.calls)
    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)
    new_calls = [tool for tool, _p in cad_env.backend.calls[calls_before:]]
    assert new_calls.count("analyze_model") == 2, new_calls
    assert finished["verification"].get("deep_checks_reused") is not True


def test_measure_backend_calls_for_typical_build_save_finish(cad_env, tmp_path):
    """SPEED task C measurement: backend calls for a 6-node build + save + finish.

    Prints the counted totals so the before/after numbers can be compared from the raw run
    output. The assertions pin the AFTER numbers (the reuse path) so a regression that
    reintroduces the redundant final verification fails here.
    """
    _build_save_finish(cad_env, tmp_path)
    after_save = len(cad_env.backend.calls)

    cad_env.orchestrator.finish_job("job-1", cad_env.token)
    total = len(cad_env.backend.calls)
    finish_calls = [tool for tool, _p in cad_env.backend.calls[after_save:]]
    verify_states = [name for name, _p in cad_env.backend.events].count("verify_state")

    # Measured BEFORE the identity work (every guard a full verify_state feature walk):
    #   build (fresh doc): open + 2 analyze + begin + commit execute calls, verify_state x2;
    #   save: assess verify_state + execute verify_state + 2 analyze + save execute, post verify_state;
    #   finish: full re-verify (verify_state + 2 analyze).
    # AFTER: the save assessment/execution guards use GET /api/tool/identity (identity-only
    # cadState) and finish_job reuses the stamp, so the whole flow pays verify_state only
    # where CAD content is genuinely consumed (post-open, and after each write).
    assert after_save == 8, after_save
    assert finish_calls == [], finish_calls
    assert total == 8, total
    # Post-open, post-build, post-save, and the finish's own identity re-check inside
    # _post_verify (its deep checks come from the stamp, so no analyze_model runs).
    assert verify_states == 4, verify_states
    identities = [name for name, _p in cad_env.backend.events].count("identity")
    # save assessment + confirm-time reassessment + save execution guards
    assert identities == 3, identities
    assert cad_env.backend.cheap_probes[-1] == "current_state_version"


# ---------------------------------------------------------------------------------------------
# IDENTITY task C — Save As safeguard
#
# A save to a path DIFFERENT from the active document's existing path is a SolidWorks Save
# As: it renames the user's open document in memory. That now requires confirmation, with an
# action_summary naming BOTH paths. Exempt: a document this job created that is still
# untitled (the normal first save of a fresh build).
# ---------------------------------------------------------------------------------------------
def test_save_to_a_different_path_requires_save_as_confirmation(cad_env, tmp_path):
    target = tmp_path / "renamed.SLDPRT"
    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "save", file_path=str(target)
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "save_as"
    assert "confirmation_token" not in pending
    assert not cad_env.backend.calls
    # the summary must name BOTH paths: where the document is open from, and where it lands
    assert str(cad_env.source) in pending["action_summary"]
    assert str(target) in pending["action_summary"]
    assert "renam" in pending["action_summary"].lower()

    confirmation = _request_confirmation(cad_env, pending)
    result = cad_env.orchestrator.confirm_action(
        "job-1",
        cad_env.token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )
    assert result["status"] == "completed", result
    assert [tool for tool, _ in cad_env.backend.calls].count("save_document") == 1
    job = cad_env.store.load("job-1", cad_env.token)
    # the in-memory rename is real: the job's binding followed the document
    assert job["expected_document"]["path"] == str(target)


def test_first_save_of_a_job_created_untitled_document_confirms_too(
    cad_env, tmp_path
):
    # USER DECISION (hermes F1): the job-created exemption is GONE. An unsaved document is
    # bound by TITLE only (same_document compares titles when there is no path), so a title
    # collision could point an unconfirmed first save at the USER's document — the first
    # save of an untitled document therefore ALWAYS confirms, even for a fresh build.
    built = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps({"nodes": []}), fresh_document=True
    )
    assert built["status"] == "completed", built
    assert cad_env.store.load("job-1", cad_env.token)[
        "expected_document"
    ]["path"] == ""

    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "save", file_path=str(tmp_path / "first.SLDPRT")
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "save_as"
    assert "Part1" in pending["action_summary"]
    assert str(tmp_path / "first.SLDPRT") in pending["action_summary"]
    assert not any(
        tool == "save_document" for tool, _ in cad_env.backend.calls
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
    assert result["status"] == "completed", result
    assert [tool for tool, _ in cad_env.backend.calls].count("save_document") == 1


def test_first_save_of_an_untitled_document_the_job_did_not_create_confirms(
    cad_env, tmp_path
):
    # The exemption is load-bearing: without it, ANY untitled save would pass unconfirmed.
    # A document the job did NOT create gets the confirmation even untitled.
    cad_env.backend.state.update(
        activeDocument="Part1", activeDocumentPath="", isDirty=True
    )
    job = cad_env.store.load("job-1", cad_env.token)
    job["expected_document"] = {"title": "Part1", "path": ""}
    cad_env.store.save(job)

    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "save", file_path=str(tmp_path / "named.SLDPRT")
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "save_as"
    assert "Part1" in pending["action_summary"]
    assert str(tmp_path / "named.SLDPRT") in pending["action_summary"]
    assert not cad_env.backend.calls


def test_save_to_the_document_s_own_path_still_only_needs_overwrite_confirmation(
    cad_env,
):
    # Same path (even a differing case) is a plain save, not a Save As; the existing
    # overwrite confirmation semantics are untouched.
    upper = Path(str(cad_env.source).upper())
    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "save", file_path=str(upper)
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["risk"] == "overwrite"
    assert not any(tool == "save_document" for tool, _ in cad_env.backend.calls)


# ---------------------------------------------------------------------------------------------
# IDENTITY task B — document switches stay detected through the cheap identity guards
#
# The per-step / per-edit guards now compare identity via GET /api/tool/identity, but the
# predicate is the same same_document() as before and backend.identity() falls back to a
# full verify_state whenever the endpoint is missing or fails — so a switch must be caught
# identically through both paths.
# ---------------------------------------------------------------------------------------------
def _switch_to(other):
    return {
        "activeDocument": other.name,
        "activeDocumentPath": str(other),
        "documentType": "PART",
    }


def test_document_switch_between_edit_steps_is_detected_via_identity(
    cad_env, tmp_path
):
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")

    def handler(tool, _params):
        if tool == "modify_dimension":
            cad_env.backend.state.update(**_switch_to(other))
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)] * 2),
    )

    events = [name for name, _p in cad_env.backend.events]
    assert "identity" in events
    assert result["status"] == "failed"
    assert result["error"]["code"] == "POST_STEP_VERIFICATION_FAILED"
    assert result["recovery_status"] == "rolled_back"


def test_document_switch_is_still_detected_via_the_verify_state_fallback(
    cad_env, tmp_path
):
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")
    cad_env.backend.identity_endpoint_down = True

    def handler(tool, _params):
        if tool == "modify_dimension":
            cad_env.backend.state.update(**_switch_to(other))
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)] * 2),
    )

    # the fallback ran the FULL verify_state instead of the probe...
    assert "identity_fallback_verify_state" in cad_env.backend.cheap_probes
    assert "verify_state" in [name for name, _p in cad_env.backend.events]
    # ...and the switch was caught exactly the same way.
    assert result["status"] == "failed"
    assert result["error"]["code"] == "POST_STEP_VERIFICATION_FAILED"


def test_drawing_document_switch_between_steps_is_detected_via_identity(
    cad_env, tmp_path
):
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")

    def handler(tool, _params):
        if tool == "add_drawing_view":
            # a switch behind the tool's back, as a user clicking another window would
            cad_env.backend.state.update(**_switch_to(other))
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps(
            {
                "model_path": str(cad_env.source),
                "views": [{"view_type": "front"}, {"view_type": "top"}],
            }
        ),
    )

    events = [name for name, _p in cad_env.backend.events]
    assert "identity" in events
    assert result["status"] == "failed"
    assert result["error"]["code"] == "POST_STEP_VERIFICATION_FAILED"
    # recovery closed the created drawing and restored the previous document
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["recovery_status"] == "rolled_back"
    assert job["expected_document"]["title"] == "part.SLDPRT"


def test_drawing_switch_is_still_detected_via_the_verify_state_fallback(
    cad_env, tmp_path
):
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")
    cad_env.backend.identity_endpoint_down = True

    def handler(tool, _params):
        if tool == "add_drawing_view":
            cad_env.backend.state.update(**_switch_to(other))
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps(
            {
                "model_path": str(cad_env.source),
                "views": [{"view_type": "front"}, {"view_type": "top"}],
            }
        ),
    )

    assert "identity_fallback_verify_state" in cad_env.backend.cheap_probes
    assert "verify_state" in [name for name, _p in cad_env.backend.events]
    assert result["status"] == "failed"
    assert result["error"]["code"] == "POST_STEP_VERIFICATION_FAILED"


# -----------------------------------------------------------------------------
# IDENTITY task D — backend call counts for the two canonical workflows (FakeBackend)
#
# BEFORE (every guard a full verify_state feature walk):
#   3-edit batch:   7 execute calls, verify_state x5 (initial + 3 per-edit + post)
#   3-step drawing: 7 execute calls, verify_state x9 (pre-check + created + 6 per-step + post)
# AFTER (identity-only guards):
#   3-edit batch:   7 execute calls, verify_state x2 (initial + post), identity x3
#   3-step drawing: 7 execute calls, verify_state x1 (post), identity x8
# -----------------------------------------------------------------------------


def _count_events(backend, name):
    return [event for event, _p in backend.events].count(name)


def test_measure_backend_calls_for_a_three_edit_batch(cad_env):
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
    assert result["status"] == "completed", result

    tools = [tool for tool, _p in cad_env.backend.calls]
    assert len(tools) == 7, tools  # begin + 3 edits + 2 analyze + commit
    assert tools.count("modify_dimension") == 3
    assert tools.count("analyze_model") == 2
    # the per-edit guards are identity probes, not feature walks
    assert _count_events(cad_env.backend, "identity") == 3
    assert _count_events(cad_env.backend, "verify_state") == 2
    assert "identity_fallback_verify_state" not in cad_env.backend.cheap_probes


def test_measure_backend_calls_for_a_three_step_drawing_workflow(cad_env):
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps(
            {
                "model_path": str(cad_env.source),
                "views": [
                    {"view_type": "front"},
                    {"view_type": "top"},
                    {"view_type": "right"},
                ],
            }
        ),
    )
    assert result["status"] == "completed", result

    tools = [tool for tool, _p in cad_env.backend.calls]
    assert len(tools) == 7, tools  # create_drawing + begin + 3 views + analyze + commit
    assert tools.count("add_drawing_view") == 3
    # every identity-only guard (pre-check, created-check, 3x before/after) is the probe
    assert _count_events(cad_env.backend, "identity") == 8
    # only the final _post_verify still walks the feature tree
    assert _count_events(cad_env.backend, "verify_state") == 1
    assert "identity_fallback_verify_state" not in cad_env.backend.cheap_probes


# -----------------------------------------------------------------------------
# Reporting fix (safety-fresh-doc.md section 6): when a fresh_document build or a drawing
# workflow FAILS, recovery rolls back, discards the created untitled document and re-activates
# the previous one. The failure envelope used to report that restored previous document as
# target_document — reading as if the previous file had been written into and restored. The
# envelope now names the DISCARDED created document as the write target, flags the discard,
# and surfaces the re-activated previous document under restored_active_document. Reporting
# only: no behaviour change.
# -----------------------------------------------------------------------------
_FAILURE_ENVELOPE_KEYS = {
    "job_id",
    "status",
    "risk",
    "action_id",
    "target_document",
    "backup_path",
    "completed_steps",
    "partial_state",
    "verification",
    "error",
    "created_document_discarded",
    "restored_active_document",
    "execution_status",
    "verification_status",
    "recovery_status",
    "trust_level",
}


def test_fresh_feature_graph_failure_reports_the_discarded_created_document(cad_env):
    def feature_graph_handler(_graph, _fresh_document):
        return {
            "status": "FAILED",
            "stateVersion": 99,
            "compiler": {
                "nodes_completed": 2,
                "node_log": [
                    {"id": "n1", "status": "COMPLETED"},
                    {"id": "n2", "status": "COMPLETED"},
                    {"id": "n3", "status": "FAILED"},
                ],
            },
            "error": {"code": "FEATURE_GRAPH_FAILED", "message": "node n3 failed."},
        }

    cad_env.backend.feature_graph_handler = feature_graph_handler
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps({"nodes": []}), fresh_document=True
    )

    assert result["status"] == "failed"
    assert result["recovery_status"] == "rolled_back"
    # the write target is the DISCARDED created untitled part, never the restored file
    assert result["target_document"] == "Part1"
    assert result["created_document_discarded"] is True
    assert result["restored_active_document"] == str(cad_env.source)
    # the restored previous document appears as recovered CAD state, not as the target
    assert result["partial_state"]["cad"]["activeDocumentPath"] == str(
        cad_env.source
    )
    # completed steps still list the nodes that ran (in the discarded part)
    assert result["completed_steps"] == ["n1", "n2"]
    # reporting-only change: nothing else was added to or removed from the envelope
    assert set(result) == _FAILURE_ENVELOPE_KEYS
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"]["path"] == str(cad_env.source)


def test_drawing_step_failure_reports_the_discarded_created_drawing(cad_env):
    def handler(tool, _params):
        if tool == "add_drawing_view":
            return _failed("DRAWING_STEP_FAILED")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps(
            {
                "model_path": str(cad_env.source),
                "views": [{"view_type": "front"}, {"view_type": "top"}],
            }
        ),
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "DRAWING_STEP_FAILED"
    assert result["recovery_status"] == "rolled_back"
    assert result["target_document"] == "Drawing1"
    assert result["created_document_discarded"] is True
    assert result["restored_active_document"] == str(cad_env.source)
    assert set(result) == _FAILURE_ENVELOPE_KEYS
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"]["path"] == str(cad_env.source)


def test_drawing_post_verify_failure_reports_the_discarded_created_drawing(cad_env):
    from backend import ExecutionLayerError

    def handler(tool, params):
        if tool == "analyze_slddrw_test":
            raise ExecutionLayerError("post-verify transport failure")
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.drawing_workflow(
        "job-1",
        cad_env.token,
        json.dumps({"views": [{"view_type": "front"}]}),
    )

    assert result["status"] == "verification_failed"
    assert result["target_document"] == "Drawing1"
    assert result["created_document_discarded"] is True
    assert result["restored_active_document"] == str(cad_env.source)


def test_failure_without_a_created_document_has_no_discard_fields(cad_env, tmp_path):
    # document_edits never creates a document, so its failure envelope must stay exactly
    # as before — no discard flag, no restored-document key, target unchanged.
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")

    def handler(tool, _params):
        if tool == "modify_dimension":
            cad_env.backend.state.update(
                activeDocument=other.name,
                activeDocumentPath=str(other),
            )
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )

    assert result["status"] == "failed"
    assert "created_document_discarded" not in result
    assert "restored_active_document" not in result
    # document_edits writes really did happen in part.SLDPRT and the rollback restored it,
    # so target_document stays the restored document here — unchanged pre-existing behavior.
    assert result["target_document"] == str(cad_env.source)
