import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from policy import payload_hash, secret_hash
from models import utc_now


def _minimal_graph():
    return {"units": "meters", "nodes": [{"id": "n1", "type": "box", "width": .02, "depth": .02, "height": .02}]}


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
            {},  # the 4th value is the state handed to an executor; unused by this stub
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
    assert result["status"] == "partial"
    assert result["completed_steps"] == ["modify_dimension"]
    assert result["recovery_status"] == "recovery_unverified"
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

    assert result["status"] == "partial"
    assert result["execution_status"] == "partial"
    assert result["verification_status"] == "failed"
    assert result["recovery_status"] == "recovery_unverified"
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
    assert result["recovery_status"] == "recovery_unverified"
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

    pending = cad_env.orchestrator.document_edits(
        "job-1", cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )
    assert pending["status"] == "pending_confirmation"
    assert "恢复未验证" in pending["action_summary"]
    assert [tool for tool, _ in cad_env.backend.calls] == ["create_drawing"]


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

    assert result["status"] == "partial"
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
            json.dumps(_minimal_graph()),
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
            _minimal_graph()
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
    assert job["recovery_status"] == "recovery_unverified"


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


def _batch_graph():
    """Small fake-backend graph over the batch threshold, without invoking any CAD service."""
    graph = _six_node_graph()
    graph["nodes"].extend(
        {"id": f"batch-{index}", "type": "box", "width": 0.01,
         "depth": 0.01, "height": 0.01}
        for index in range(27)
    )
    return graph


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
    # USER DECISION 2026-10-31: the first save of the part this job created, to a new path,
    # runs at once. The hermes-F1 title-collision hazard is closed by the stable document_id.
    assert saved["status"] == "completed", saved
    assert saved["risk"] == "write"
    return built, saved


def test_finish_job_reuses_unchanged_post_verify(cad_env, tmp_path):
    _build_save_finish(cad_env, tmp_path)
    probes_before = len(cad_env.backend.cheap_probes)
    calls_before = len(cad_env.backend.calls)

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    assert finished["status"] == "completed"
    assert finished["verification"]["deep_checks_reused"] is True
    # SPEED #6: the freshness proof IS the state _post_verify already reads (its state_version
    # plus its identity, compared by _stamp_matches), so finish_job no longer pays a separate
    # GET /api/tool/state for the same fact.
    assert cad_env.backend.cheap_probes[probes_before] == "identity"
    assert "current_state_version" not in cad_env.backend.cheap_probes[probes_before:]
    # ...and the expensive path was NOT: no verify_state, no analyze_model. The only backend
    # call finish_job adds now is its own document close (AUTOCLOSE feature), which re-checks
    # the identity through the cheap identity probe rather than a feature walk.
    new_calls = [tool for tool, _p in cad_env.backend.calls[calls_before:]]
    assert new_calls == ["close_document"], new_calls
    assert finished["closed_document"]["title"] == "out.SLDPRT"
    assert finished["closed_document"]["path"] == str(tmp_path / "out.SLDPRT")
    assert finished["document_left_open"] is None
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
    # the advanced counter is read from the identity response, no feature walk needed
    assert "identity" in [name for name, _p in cad_env.backend.events]
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
    assert [e for e, _p in cad_env.backend.events[calls_before:] if e == "identity"]
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
    # AFTER IDENTITY task C: the save guards used GET /api/tool/identity and finish_job reused
    # the stamp, so verify_state ran only post-open, after each write and in the finish.
    # AFTER SPEED #1/#2/#3/#6 (this run):
    #   build: post-open check and the post-graph _post_verify are identity reads;
    #   save: ONE identity read covers ask+confirm+execute (the confirmed write no longer
    #          re-probes, #3) and the post-save _post_verify is an identity read;
    #   finish: the stamp is compared against the state _post_verify already reads (#6), then
    #          one close_document write (#AUTOCLOSE).
    # => the whole 6-node build + save + finish flow now pays ZERO feature-tree walks, while
    # every identity gate and every geometry verdict still runs on its own data.
    assert after_save == 8, after_save
    assert finish_calls == ["close_document"], finish_calls
    assert total == 9, total
    assert verify_states == 0, verify_states
    identities = [name for name, _p in cad_env.backend.events].count("identity")
    # post-open + post-graph + save (its own part's first save runs at once since 2026-10-31,
    # so ask and confirm are one read) + post-save + finish + the close re-check
    assert identities == 6, identities
    assert cad_env.backend.cheap_probes[-1] == "identity"
    assert "current_state_version" not in cad_env.backend.cheap_probes
    assert "identity_fallback_verify_state" not in cad_env.backend.cheap_probes


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


def test_first_save_of_the_job_created_part_to_a_new_file_runs_at_once(cad_env, tmp_path):
    # USER DECISION 2026-10-31. hermes F1 removed an earlier exemption because an untitled
    # document was bound by TITLE only; the exemption is back ONLY when the active document is
    # this job's own instance by the execution layer's stable document_id.
    built = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True
    )
    assert built["status"] == "completed", built
    saved = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "save", file_path=str(tmp_path / "first.SLDPRT")
    )
    assert saved["status"] == "completed", saved
    assert saved["risk"] == "write"
    assert [tool for tool, _ in cad_env.backend.calls].count("save_document") == 1


@pytest.mark.parametrize("hazard", ["other_instance_active", "target_exists"])
def test_first_save_still_confirms_without_proof_of_ownership_or_onto_a_file(
    cad_env, tmp_path, monkeypatch, hazard
):
    built = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True
    )
    assert built["status"] == "completed", built
    target = tmp_path / "first.SLDPRT"
    if hazard == "target_exists":
        target.write_bytes(b"existing")
    else:
        real = cad_env.module.backend.documents
        def another_active():
            inventory = real()
            return {**inventory, "active_document": {**(inventory.get("active_document") or {}),
                                                     "document_id": "someone-else"}}
        monkeypatch.setattr(cad_env.module.backend, "documents", another_active)
    pending = cad_env.orchestrator.save_or_export(
        "job-1", cad_env.token, "save", file_path=str(target)
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
    assert result["status"] == "partial"
    assert result["error"]["code"] == "POST_STEP_VERIFICATION_FAILED"
    assert result["recovery_status"] == "recovery_unverified"


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
    assert result["status"] == "partial"
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
    assert job["recovery_status"] == "discarded"
    assert job["expected_document"] == {}


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
# AFTER SPEED #3/#6 (post-verify reads identity too, and reuses the last step's read instead
# of reading the same document twice):
#   3-edit batch:   7 execute calls, verify_state x1 (the pre-write read only), identity x3
#   3-step drawing: 7 execute calls, verify_state x0,                     identity x8
# The identity probe reports the same fields the guards compare (title/path/type/isDirty/
# stateVersion), so every predicate runs on identical data — only the feature-tree walk left.
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
    # only the pre-write read still walks the feature tree: the post-verify gate needs no
    # second read because it reuses the last step's identity (#3/#6)
    assert _count_events(cad_env.backend, "verify_state") == 1
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
    # and now ZERO feature walks: the post-verify reuses the last step's identity read (#3/#6)
    assert _count_events(cad_env.backend, "verify_state") == 0
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
    "active_document_after_recovery",
    "execution_status",
    "verification_status",
    "recovery_status",
    "trust_level",
    "automatic_retry_allowed", "repair_attempts_used", "operation_target",
    "remaining_modified_document", "recovery_method", "undo_restoration_verified",
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
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True
    )

    assert result["status"] == "failed"
    assert result["recovery_status"] == "discarded"
    # the write target is the DISCARDED created untitled part, never the restored file
    assert result["target_document"] == "Part1"
    assert result["created_document_discarded"] is True
    assert result["active_document_after_recovery"] == str(cad_env.source)
    # the restored previous document appears as recovered CAD state, not as the target
    assert result["partial_state"]["cad"]["activeDocumentPath"] == str(
        cad_env.source
    )
    # completed steps still list the nodes that ran (in the discarded part)
    assert result["completed_steps"] == ["n1", "n2"]
    # reporting-only change: nothing else was added to or removed from the envelope
    assert set(result) == _FAILURE_ENVELOPE_KEYS
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"] == {}
    assert job["document_write_allowed"] is False


# -----------------------------------------------------------------------------
# MATERIAL-NONFATAL: a graph-level material the library does not contain is a WARNING on a
# completed build, never a reason to discard finished geometry. The compiler decides which code
# is exempt (pycompiler/tests/test_compiler.py); these prove the orchestrator commits and reports
# it — and that nothing else about the failure/rollback contract moved.
# -----------------------------------------------------------------------------
def _graph_with_material(name, library=None):
    material = {"name": name} if library is None else {"name": name, "library": library}
    return {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "material": material,
        "nodes": [
            {"id": "n1", "type": "box", "ref": {"datum": "top"},
             "width": 0.05, "depth": 0.05, "height": 0.02},
        ],
    }


def test_feature_graph_material_warning_keeps_the_geometry_committed(unbound_env):
    env = unbound_env
    result = env.orchestrator.feature_graph(
        env.job_id,
        env.token,
        json.dumps(_graph_with_material("HT200")),
        fresh_document=True,
    )

    assert result["status"] == "completed", result
    # the warning is a warning: error stays null, so nothing here reads as a failed build
    assert result["error"] is None
    assert [w["code"] for w in result["warnings"]] == ["MATERIAL_NOT_FOUND"]
    warning = result["warnings"][0]
    assert warning["material"] == "HT200"
    assert warning["library"] == "SolidWorks Materials"
    assert warning["step"] == "set_part_material"
    assert warning["message"] and warning["hint"]
    # the node that ran is reported; the material node is NOT among the completed steps
    assert result["completed_steps"] == ["n1"]
    assert result["partial_state"]["compiler"]["node_log"][-1]["id"] == "_material"
    # committed, not recovered
    tools = [tool for tool, _params in env.backend.events]
    assert "commit_undo_scope" in tools
    assert "rollback_undo_scope" not in tools
    # and the built part is still the job's document — nothing discarded, nothing restored
    assert "created_document_discarded" not in result
    assert result["target_document"] == "Part1"
    job = env.store.load(env.job_id, env.token)
    assert job["expected_document"]["title"] == "Part1"


def test_feature_graph_valid_material_applies_with_no_warning(unbound_env):
    env = unbound_env
    result = env.orchestrator.feature_graph(
        env.job_id,
        env.token,
        json.dumps(_graph_with_material("1060 Alloy")),
        fresh_document=True,
    )

    assert result["status"] == "completed", result
    # an absent key means "nothing to report"; a null warnings list would read as a dropped one
    assert "warnings" not in result
    assert result["completed_steps"] == ["n1", "_material"]


def test_feature_graph_material_warning_names_the_material_actually_on_the_part(unbound_env):
    # A part that already carries a material keeps it, and the warning must say so — a message
    # claiming "no material" would send the user to fix the wrong thing (and a weight check would
    # silently read the old material's properties).
    env = unbound_env
    env.backend.material_on_part = "Gray Cast Iron"
    env.backend.material_library_on_part = "SolidWorks Materials"

    result = env.orchestrator.feature_graph(
        env.job_id,
        env.token,
        json.dumps(_graph_with_material("HT200")),
        fresh_document=True,
    )

    assert result["status"] == "completed", result
    warning = result["warnings"][0]
    assert warning["code"] == "MATERIAL_NOT_FOUND"
    assert warning["material"] == "HT200"
    assert warning["material_on_part"] == "Gray Cast Iron"
    assert warning["library_on_part"] == "SolidWorks Materials"
    # the pre-existing material is untouched, and the run still committed
    assert env.backend.material_on_part == "Gray Cast Iron"
    assert result["error"] is None
    tools = [tool for tool, _params in env.backend.events]
    assert "commit_undo_scope" in tools and "rollback_undo_scope" not in tools


def test_feature_graph_material_not_applied_rolls_back(cad_env):
    # MATERIAL_NOT_APPLIED is present-but-refused: a real failure, fatal, and the geometry is
    # rolled back like any other node failure. Only the positively-absent name is exempt.
    cad_env.backend.feature_graph_handler = None
    result = cad_env.orchestrator.feature_graph(
        "job-1",
        cad_env.token,
        json.dumps(_graph_with_material("Refused Material")),
        fresh_document=True,
    )

    assert result["status"] == "failed", result
    assert result["error"]["code"] == "MATERIAL_NOT_APPLIED"
    assert result["error"]["node_id"] == "_material"
    assert "warnings" not in result
    assert result["recovery_status"] == "discarded"
    assert result["completed_steps"] == ["n1"]
    tools = [tool for tool, _params in cad_env.backend.events]
    assert "rollback_undo_scope" in tools
    assert "commit_undo_scope" not in tools


def test_material_not_found_reported_as_a_failure_still_rolls_back(cad_env):
    # The exemption lives in the compiler's STATUS, not in a code name checked here. If
    # set_part_material fails the graph — with MATERIAL_NOT_FOUND included — the recovery path is
    # exactly the one every other node failure takes.
    def feature_graph_handler(_graph, _fresh_document):
        return {
            "status": "FAILED",
            "stateVersion": 7,
            "compiler": {
                "nodes_completed": 1,
                "warnings": [],
                "node_log": [
                    {"id": "n1", "status": "COMPLETED"},
                    {"id": "_material", "status": "FAILED"},
                ],
            },
            "error": {"code": "MATERIAL_NOT_FOUND", "message": "material not found"},
        }

    cad_env.backend.feature_graph_handler = feature_graph_handler
    result = cad_env.orchestrator.feature_graph(
        "job-1",
        cad_env.token,
        json.dumps(_graph_with_material("HT200")),
        fresh_document=True,
    )

    assert result["status"] == "failed"
    assert result["recovery_status"] == "discarded"
    assert "warnings" not in result
    assert result["created_document_discarded"] is True
    assert result["active_document_after_recovery"] == str(cad_env.source)
    assert set(result) == _FAILURE_ENVELOPE_KEYS


def test_document_edit_material_failure_is_unchanged(cad_env):
    # apply_document_edits is not the feature-graph path: an unknown material there stays a step
    # failure with a rollback. The exempt step is the graph's own post-geometry material op.
    result = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("set_part_material", material_name="HT200")]),
    )

    assert result["status"] == "partial"
    assert result["error"]["code"] == "MATERIAL_NOT_FOUND"
    assert result["recovery_status"] == "recovery_unverified"
    assert result["completed_steps"] == []


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
    assert result["recovery_status"] == "discarded"
    assert result["target_document"] == "Drawing1"
    assert result["created_document_discarded"] is True
    assert result["active_document_after_recovery"] == str(cad_env.source)
    assert set(result) == _FAILURE_ENVELOPE_KEYS
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"] == {}


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
    assert result["active_document_after_recovery"] == str(cad_env.source)


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

    assert result["status"] == "partial"
    assert "created_document_discarded" not in result
    assert "active_document_after_recovery" not in result
    # document_edits writes really did happen in part.SLDPRT and the rollback restored it,
    # so target_document stays the restored document here — unchanged pre-existing behavior.
    assert result["target_document"] == str(cad_env.source)


# ---------------------------------------------------------------------------------------------
# AUTOCLOSE — finish_job closes the document the job itself produced
#
# SLDWORKS.exe is one process holding every open document (measured at 5+ GB private memory),
# so a finished job releases its own document as soon as the work is verified. The gate is
# deliberately narrow: only a document the job CREATED or OPENED, that is saved to a path,
# that is not dirty, and that the caller did not ask to keep, is ever closed. Anything else is
# reported in document_left_open with a reason, and a failed close is reported in close_error
# WITHOUT changing the completed verdict — verified CAD work is never re-judged by cleanup.
# ---------------------------------------------------------------------------------------------
def _autoclose_build(env, job_id, token, tmp_path, name="autoclose.SLDPRT", save=True):
    """The canonical flow for a document the job produced: fresh build + first save (no
    confirmation since the user decision of 2026-10-31: its own part, to a new file)."""
    built = env.orchestrator.feature_graph(
        job_id, token, json.dumps(_six_node_graph()), fresh_document=True
    )
    assert built["status"] == "completed", built
    if not save:
        return built, None
    target = tmp_path / name
    saved = env.orchestrator.save_or_export(
        job_id, token, "save", file_path=str(target)
    )
    assert saved["status"] == "completed", saved
    return built, saved


def test_finish_job_closes_the_document_its_job_created_and_saved(unbound_env, tmp_path):
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)
    target = tmp_path / "autoclose.SLDPRT"

    finished = env.orchestrator.finish_job(env.job_id, env.token)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"] == {
        "title": target.name,
        "path": str(target),
        "type": "PART",
    }
    assert finished["document_left_open"] is None
    assert "close_error" not in finished
    closes = [
        (tool, params)
        for tool, params in env.backend.calls
        if tool == "close_document"
    ]
    assert len(closes) == 1, closes
    # The existing identity-guarded path with save=False: it can neither save nor discard.
    assert closes[0][1] == {
        "save": False,
        "close_all": False,
        "expected_title": target.name,
        "expected_path": str(target),
        "expected_type": "PART",
    }
    job = env.store.load(env.job_id, env.token)
    assert job["status"] == "completed"
    assert job["target_closed"] is True
    assert job["closed_document"]["path"] == str(target)


def test_finish_job_leaves_a_dirty_document_open(unbound_env, tmp_path):
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)
    # The saved document changed after the last verification (a manual CAD edit): closing now
    # would either discard that work or silently save it.
    env.backend.state["isDirty"] = True

    finished = env.orchestrator.finish_job(env.job_id, env.token, keep_open=False)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"] is None
    assert finished["document_left_open"]["reason"] == "dirty"
    assert finished["document_left_open"]["document"]["path"] == str(
        tmp_path / "autoclose.SLDPRT"
    )
    assert not [
        tool for tool, _params in env.backend.calls if tool == "close_document"
    ]
    assert env.store.load(env.job_id, env.token).get("target_closed") is None


def test_finish_job_leaves_an_unsaved_document_open(unbound_env, tmp_path):
    env = unbound_env
    # Built but never saved: the document is still untitled in SolidWorks.
    _autoclose_build(env, env.job_id, env.token, tmp_path, save=False)

    finished = env.orchestrator.finish_job(env.job_id, env.token)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"] is None
    assert finished["document_left_open"]["reason"] == "unsaved"
    assert finished["document_left_open"]["document"]["path"] == ""
    assert not [
        tool for tool, _params in env.backend.calls if tool == "close_document"
    ]


def test_finish_job_never_closes_a_pre_existing_document(cad_env):
    # The normal work-on-an-existing-file flow: the document was already active when the job
    # started, and it is saved and clean — exactly the document cleanup must not touch.
    edits = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )
    assert edits["status"] == "completed", edits
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["expected_document"]["path"] == str(cad_env.source)
    assert job.get("document_origin") not in ("created", "opened")

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"] is None
    assert finished["document_left_open"]["reason"] == "pre_existing"
    assert not [
        tool for tool, _params in cad_env.backend.calls if tool == "close_document"
    ]
    after = cad_env.store.load("job-1", cad_env.token)
    assert after["expected_document"]["path"] == str(cad_env.source)
    assert after.get("target_closed") is None


def test_finish_job_honours_keep_open(unbound_env, tmp_path):
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)

    finished = env.orchestrator.finish_job(env.job_id, env.token, keep_open=True)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"] is None
    assert finished["document_left_open"]["reason"] == "keep_open"
    assert not [
        tool for tool, _params in env.backend.calls if tool == "close_document"
    ]
    assert env.store.load(env.job_id, env.token).get("target_closed") is None


def test_finish_job_reports_a_close_failure_without_failing_the_job(unbound_env, tmp_path):
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)
    env.backend.call_handler = lambda tool, _params: (
        _failed("COM_ERROR") if tool == "close_document" else None
    )

    finished = env.orchestrator.finish_job(env.job_id, env.token)

    # The CAD work is verified: a cleanup failure must not change the verdict.
    assert finished["status"] == "completed", finished
    assert finished["trust_level"] == "verified"
    assert finished["verification_status"] == "passed"
    assert finished["closed_document"] is None
    assert finished["document_left_open"]["reason"] == "close_failed"
    assert finished["close_error"]["code"] == "COM_ERROR"
    job = env.store.load(env.job_id, env.token)
    assert job["status"] == "completed"
    assert job.get("target_closed") is None
    # The document stays bound to the job, so its identity remains on record.
    assert job["expected_document"]["path"] == str(tmp_path / "autoclose.SLDPRT")


def test_finish_job_reports_a_close_that_did_not_take_effect(unbound_env, tmp_path):
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)
    before_state = dict(env.backend.state)

    def handler(tool, _params):
        if tool == "close_document":
            # Reports success while changing nothing: the re-check has to catch that.
            return {
                "status": "COMPLETED",
                "stateVersion": before_state.get("stateVersion"),
                "cadState": dict(before_state),
                "error": None,
            }
        return None

    env.backend.call_handler = handler
    finished = env.orchestrator.finish_job(env.job_id, env.token)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"] is None
    assert finished["close_error"]["code"] == "CLOSE_NOT_CONFIRMED"
    assert finished["document_left_open"]["reason"] == "close_not_confirmed"
    assert env.store.load(env.job_id, env.token).get("target_closed") is None


def test_finish_job_auto_close_runs_inside_the_write_lock(
    unbound_env, tmp_path, monkeypatch
):
    """The close happens under the same _write_lock finish_job already holds: unchanged."""
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)
    lock = env.orchestrator._write_lock
    seen = {}
    real_close = env.orchestrator._close_document

    def spy(expected_identity=None, *, save=False, close_all=False):
        # While the close is in flight, no other thread may hold the orchestrator lock.
        def probe():
            acquired = lock.acquire(blocking=False)
            seen["other_thread_acquired"] = acquired
            if acquired:
                lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return real_close(expected_identity, save=save, close_all=close_all)

    monkeypatch.setattr(env.orchestrator, "_close_document", spy)
    finished = env.orchestrator.finish_job(env.job_id, env.token)

    assert finished["status"] == "completed", finished
    assert finished["closed_document"]["path"] == str(tmp_path / "autoclose.SLDPRT")
    assert seen == {"other_thread_acquired": False}
    # Released once finish_job returns — the close path leaks no lock.
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_finish_job_of_an_already_closed_target_reports_no_new_close(cad_env):
    closed = cad_env.orchestrator.save_or_export("job-1", cad_env.token, "close")
    assert closed["status"] == "completed", closed
    calls_before = len(cad_env.backend.calls)

    finished = cad_env.orchestrator.finish_job("job-1", cad_env.token)

    assert finished["status"] == "completed", finished
    assert finished["document_left_open"] is None
    assert finished["closed_document"] == closed["verification"]["closed_document"]
    assert [tool for tool, _p in cad_env.backend.calls[calls_before:]] == []


def test_begin_job_records_opened_when_nothing_was_active(unbound_env, tmp_path):
    env = unbound_env
    target = tmp_path / "made_before.SLDPRT"
    target.write_bytes(b"saved-by-an-earlier-job")
    opened_state = {
        "stateVersion": 2,
        "activeDocument": target.name,
        "activeDocumentPath": str(target),
        "documentType": "PART",
        "activeSketch": None,
        "features": [],
        "dimensions": [],
        "isDirty": False,
    }

    def handler(tool, _params):
        if tool == "open_document":
            env.backend.state = dict(opened_state)
        return None

    env.backend.call_handler = handler
    started = env.orchestrator.begin_job("work on the part I just built", str(target))
    assert started["status"] == "completed", started
    job = env.store.load(started["job_id"], started["job_token"])
    # Nothing was active before the open, so this job is what brought the document in.
    assert job["document_origin"] == "opened"

    finished = env.orchestrator.finish_job(started["job_id"], started["job_token"])
    assert finished["status"] == "completed", finished
    assert finished["closed_document"]["path"] == str(target)


def test_begin_job_records_pre_existing_when_a_document_was_active(cad_env, tmp_path):
    other = tmp_path / "other.SLDPRT"
    other.write_bytes(b"other")
    started = cad_env.orchestrator.begin_job("open a file", str(other))
    assert started["status"] == "completed", started

    job = cad_env.store.load(started["job_id"], started["job_token"])
    # part.SLDPRT was on screen, so that file may already have been open in the background.
    # With no way to enumerate open documents, the job must not claim it.
    assert job["document_origin"] == "pre_existing"


def test_begin_job_records_pre_existing_with_no_target(cad_env):
    # One module-level backend per test: an env fixture per test, never two in one signature.
    started = cad_env.orchestrator.begin_job("attach to whatever is open")
    assert started["status"] == "completed", started
    job = cad_env.store.load(started["job_id"], started["job_token"])
    assert job["document_origin"] == "pre_existing"


def test_begin_job_records_unbound_with_no_target(unbound_env):
    unbound = unbound_env.orchestrator.begin_job("no document yet")
    assert unbound["status"] == "completed", unbound
    job = unbound_env.store.load(unbound["job_id"], unbound["job_token"])
    assert job["document_origin"] == "unbound"


def test_unverified_create_never_rebinds_or_closes_a_previous_document(cad_env):
    # open_new_part reports a document that verify_state does not agree is active, so the
    # created document is discarded and the job is back on the file it never created. The
    # origin has to follow: leaving it "created" would let a later close name a document the
    # job no longer owns.
    imposter = {
        "stateVersion": 3,
        "activeDocument": "PartZ",
        "activeDocumentPath": "",
        "documentType": "PART",
        "activeSketch": None,
        "features": [],
        "dimensions": [],
    }

    def handler(tool, _params):
        if tool == "open_new_part":
            return {
                "status": "COMPLETED",
                "stateVersion": 3,
                "cadState": dict(imposter),
                "error": None,
            }
        if tool == "close_document":
            # The discard succeeds and leaves the pre-existing document on screen.
            return {
                "status": "COMPLETED",
                "stateVersion": 3,
                "cadState": dict(cad_env.backend.state),
                "error": None,
            }
        return None

    cad_env.backend.call_handler = handler
    built = cad_env.orchestrator.feature_graph(
        "job-1",
        cad_env.token,
        json.dumps(_minimal_graph()),
        fresh_document=True,
    )
    assert built["status"] != "completed", built

    job = cad_env.store.load("job-1", cad_env.token)
    assert job["recovery_status"] == "rollback_failed"
    assert job["expected_document"]["title"] == "PartZ"
    assert job["document_origin"] == "created"
    assert job["document_write_allowed"] is False
    assert "close_document" not in [t for t, _ in cad_env.backend.calls]


def test_finish_job_close_keeps_the_verified_pre_close_state(unbound_env, tmp_path):
    """Cleanup may not rewrite the verdict: partial_state stays the verified document."""
    env = unbound_env
    _autoclose_build(env, env.job_id, env.token, tmp_path)

    finished = env.orchestrator.finish_job(env.job_id, env.token)

    assert finished["status"] == "completed"
    assert finished["partial_state"]["activeDocumentPath"] == str(
        tmp_path / "autoclose.SLDPRT"
    )
    assert finished["target_document"] == str(tmp_path / "autoclose.SLDPRT")
    assert finished["verification"]["all_completed"] is True


def _confirm_job_action(env, job_id, job_token, pending):
    confirmation = env.orchestrator.request_confirmation(
        job_id, job_token, pending["action_id"]
    )
    return env.orchestrator.confirm_action(
        job_id,
        job_token,
        pending["action_id"],
        confirmation["confirmation_token"],
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    )


def test_invalid_batch_graph_fails_before_confirmation_or_document_creation(cad_env):
    env = cad_env
    started = env.orchestrator.begin_job("new model, validate before approval")
    graph = _batch_graph()
    graph["units"] = "m"
    calls_before = len(env.backend.calls)

    with pytest.raises(RuntimeError, match="VALIDATION_FAILED: units must be 'meters'"):
        env.orchestrator.feature_graph(
            started["job_id"], started["job_token"], json.dumps(graph),
            fresh_document=True,
        )

    assert len(env.backend.calls) == calls_before
    job = env.store.load(started["job_id"], started["job_token"])
    assert not any(
        action.get("kind") == "feature_graph" and not action.get("consumed")
        for action in (job.get("pending_actions") or {}).values()
    )


def test_fresh_batch_graph_builds_without_a_second_confirmation(cad_env):
    # User decision 2026-10-31: the approved plan authorizes a fresh build of any size; it only
    # creates its own document, so the over-ten-nodes confirmation applies to existing documents.
    env = cad_env
    started = env.orchestrator.begin_job("new model, no explicit target")
    built = env.orchestrator.feature_graph(
        started["job_id"], started["job_token"], json.dumps(_batch_graph()),
        fresh_document=True,
    )
    assert built["status"] == "completed", built
    assert built["risk"] == "write"


def test_pending_graph_blocks_all_job_writes_until_cancelled(cad_env, tmp_path):
    env = cad_env
    started = env.orchestrator.begin_job("edit explicitly targeted part", str(env.source))
    pending = env.orchestrator.feature_graph(
        started["job_id"], started["job_token"], json.dumps(_batch_graph()),
        fresh_document=False,
    )
    assert pending["status"] == "pending_confirmation"
    output = tmp_path / "incident-save-as.SLDPRT"
    calls_before = len(env.backend.calls)
    for operation, kwargs in (
        ("save", {"file_path": str(output)}),
        ("export", {"file_path": str(tmp_path / "incident.step"), "format": "STEP"}),
        ("batch_export", {"file_path": str(tmp_path / "incident"), "formats_json": '["STEP"]'}),
    ):
        with pytest.raises(RuntimeError, match="GRAPH_PENDING_CONFIRMATION"):
            env.orchestrator.save_or_export(
                started["job_id"], started["job_token"], operation, **kwargs
            )
    with pytest.raises(RuntimeError, match="GRAPH_PENDING_CONFIRMATION"):
        env.orchestrator.document_edits(
            started["job_id"], started["job_token"],
            json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
        )
    with pytest.raises(RuntimeError, match="GRAPH_PENDING_CONFIRMATION"):
        env.orchestrator.feature_graph(
            started["job_id"], started["job_token"], json.dumps(_minimal_graph()),
            fresh_document=False,
        )
    with pytest.raises(RuntimeError, match="GRAPH_PENDING_CONFIRMATION"):
        env.orchestrator.drawing_workflow(
            started["job_id"], started["job_token"], json.dumps({"views": []})
        )
    with pytest.raises(RuntimeError, match="GRAPH_PENDING_CONFIRMATION"):
        env.orchestrator.finish_job(started["job_id"], started["job_token"])
    assert env.backend.calls[calls_before:] == []
    assert not output.exists()
    assert env.backend.state["activeDocumentPath"] == str(env.source)

    cancelled = env.orchestrator.cancel_action(
        started["job_id"], started["job_token"], pending["action_id"]
    )
    assert cancelled["status"] == "cancelled"
    assert env.backend.calls[calls_before:] == []
    assert env.backend.state["activeDocumentPath"] == str(env.source)


def test_targetless_preexisting_document_is_read_only(cad_env, tmp_path):
    env = cad_env
    started = env.orchestrator.begin_job("inspect active user part")
    calls_before = len(env.backend.calls)
    for operation, kwargs in (
        ("save", {"file_path": str(tmp_path / "copy.SLDPRT")}),
        ("export", {"file_path": str(tmp_path / "out.step"), "format": "STEP"}),
        ("batch_export", {"file_path": str(tmp_path / "out"), "formats_json": '["STEP"]'}),
    ):
        with pytest.raises(RuntimeError, match="JOB_DOCUMENT_READ_ONLY"):
            env.orchestrator.save_or_export(
                started["job_id"], started["job_token"], operation, **kwargs
            )
    with pytest.raises(RuntimeError, match="JOB_DOCUMENT_READ_ONLY"):
        env.orchestrator.document_edits(
            started["job_id"], started["job_token"],
            json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
        )
    with pytest.raises(RuntimeError, match="JOB_DOCUMENT_READ_ONLY"):
        env.orchestrator.feature_graph(
            started["job_id"], started["job_token"], json.dumps(_minimal_graph()),
            fresh_document=False,
        )
    for kwargs in ({}, {"close_all": True}):
        with pytest.raises(RuntimeError, match="JOB_DOCUMENT_READ_ONLY"):
            env.orchestrator.save_or_export(
                started["job_id"], started["job_token"], "close", **kwargs
            )
    assert env.backend.calls[calls_before:] == []
    assert env.backend.state["activeDocumentPath"] == str(env.source)


def test_explicit_target_can_be_edited_and_saved(cad_env):
    env = cad_env
    started = env.orchestrator.begin_job("edit explicitly targeted part", str(env.source))
    assert started["status"] == "completed", started
    edited = env.orchestrator.document_edits(
        started["job_id"], started["job_token"],
        json.dumps([_edit("modify_dimension", name="D1", value=0.01)]),
    )
    assert edited["status"] == "completed", edited
    saved = env.orchestrator.save_or_export(
        started["job_id"], started["job_token"], "save"
    )
    if saved["status"] == "pending_confirmation":
        saved = _confirm_job_action(env, started["job_id"], started["job_token"], saved)
    assert saved["status"] == "completed", saved


@pytest.mark.parametrize("keep_open", [False, True])
def test_fresh_graph_can_save_and_finish(cad_env, tmp_path, keep_open):
    env = cad_env
    started = env.orchestrator.begin_job("new model without target")
    built = env.orchestrator.feature_graph(
        started["job_id"], started["job_token"], json.dumps(_batch_graph()),
        fresh_document=True,
    )
    assert built["status"] == "completed", built
    output = tmp_path / ("keep-open.SLDPRT" if keep_open else "auto-close.SLDPRT")
    saved = env.orchestrator.save_or_export(
        started["job_id"], started["job_token"], "save", file_path=str(output)
    )
    assert saved["status"] == "completed", saved  # first save of its own part: no confirmation
    finished = env.orchestrator.finish_job(
        started["job_id"], started["job_token"], keep_open=keep_open
    )
    assert finished["status"] == "completed", finished
    if keep_open:
        assert finished["document_left_open"]["reason"] == "keep_open"
        assert env.backend.state["activeDocumentPath"] == str(output)
    else:
        assert finished["closed_document"]["path"] == str(output)
        assert env.backend.state["activeDocumentPath"] == str(env.source)


# --- finish_job completeness (2026-10-31: a staged recovery dropped the R3 fillet node and the part
# was finished without it, unsaved) --------------------------------------------------------------

def _box_and_fillet():
    return {"units": "meters", "nodes": [
        {"id": "n1", "type": "box", "width": .02, "depth": .02, "height": .02},
        {"id": "r3", "type": "fillet", "radius": .003, "edges": [{"near": [.01, .01, 0]}]},
    ]}


def test_finish_reports_a_feature_a_resubmission_dropped(cad_env, tmp_path):
    env = cad_env
    started = env.orchestrator.begin_job("new part")
    env.backend.feature_graph_handler = lambda *_: {
        "status": "FAILED", "error": {"code": "REFERENCE_UNRESOLVED", "message": "probe"},
        "compiler": {"node_log": [{"id": "n1", "status": "COMPLETED"}]}}
    first = env.orchestrator.feature_graph(started["job_id"], started["job_token"],
                                           json.dumps(_box_and_fillet()), fresh_document=True)
    assert first["recovery_status"] == "discarded", first
    env.backend.feature_graph_handler = None
    pending = env.orchestrator.feature_graph(started["job_id"], started["job_token"],
                                             json.dumps(_minimal_graph()), fresh_document=True)
    assert pending["status"] == "pending_confirmation"  # dropping a node is a design change
    built = _confirm_job_action(env, started["job_id"], started["job_token"], pending)
    assert built["status"] == "completed", built
    output = tmp_path / "dropped.SLDPRT"
    saved = env.orchestrator.save_or_export(started["job_id"], started["job_token"], "save",
                                            file_path=str(output))
    if saved["status"] == "pending_confirmation":
        saved = _confirm_job_action(env, started["job_id"], started["job_token"], saved)
    finished = env.orchestrator.finish_job(started["job_id"], started["job_token"])
    codes = [w["code"] for w in finished.get("warnings", [])]
    assert codes == ["PLANNED_FEATURES_MISSING"], finished
    assert finished["warnings"][0]["missing"] == {"fillet": 1}
    assert finished["warnings"][0]["planned_ids"] == {"fillet": ["r3"]}


def test_finish_reminds_to_save_a_new_part(cad_env):
    env = cad_env
    started = env.orchestrator.begin_job("new part")
    built = env.orchestrator.feature_graph(started["job_id"], started["job_token"],
                                           json.dumps(_box_and_fillet()), fresh_document=True)
    assert built["status"] == "completed", built
    finished = env.orchestrator.finish_job(started["job_id"], started["job_token"], keep_open=True)
    assert [w["code"] for w in finished.get("warnings", [])] == ["CREATED_DOCUMENT_UNSAVED"], finished


def test_complete_saved_build_finishes_without_warnings(cad_env, tmp_path):
    env = cad_env
    started = env.orchestrator.begin_job("new part")
    built = env.orchestrator.feature_graph(started["job_id"], started["job_token"],
                                           json.dumps(_box_and_fillet()), fresh_document=True)
    assert built["status"] == "completed", built
    saved = env.orchestrator.save_or_export(started["job_id"], started["job_token"], "save",
                                            file_path=str(tmp_path / "complete.SLDPRT"))
    if saved["status"] == "pending_confirmation":
        saved = _confirm_job_action(env, started["job_id"], started["job_token"], saved)
    assert saved["status"] == "completed", saved
    finished = env.orchestrator.finish_job(started["job_id"], started["job_token"])
    assert "warnings" not in finished, finished
