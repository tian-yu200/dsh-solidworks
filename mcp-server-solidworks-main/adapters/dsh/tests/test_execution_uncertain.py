"""The EXECUTION_UNCERTAIN branch: an exception (not a FAILED response) escaping a Feature Graph attempt.

Until 2026-11-06 that branch recorded recovery_status="rollback_failed" without ever attempting a
rollback, so an undo scope opened by the attempt stayed open in the execution service and every
later build failed with UNDO_SCOPE_ACTIVE. A non-fresh attempt had no branch at all: the exception
escaped with the scope open and nothing recorded.
"""
import json

import pytest


def _minimal_graph():
    return {"schema_version": "0.8.0-draft", "units": "meters",
            "nodes": [{"id": "n1", "type": "box",
                       "width": .02, "depth": .02, "height": .02}]}


def _raise_execution_layer_error(tool):
    """The shape of a transport failure in backend.call: raised, not a FAILED response."""
    from backend import ExecutionLayerError
    raise ExecutionLayerError(
        "EXECUTION_LAYER_ERROR: the execution server did not answer while handling %r" % tool)


def _tools(env):
    return [call[0] for call in env.backend.calls]


def test_execution_uncertain_branch_is_reachable(cad_env):
    def handler(tool, params):
        _raise_execution_layer_error(tool)

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True)

    assert result["status"] == "partial"
    assert (result.get("error") or {}).get("code") == "EXECUTION_UNCERTAIN"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["error"]["code"] == "EXECUTION_UNCERTAIN"
    # No scope was begun, so nothing may be rolled back: a rollback here could undo another
    # document's scope.
    assert _tools(cad_env) == ["open_new_part"]


def test_uncertain_execution_does_not_leak_the_undo_scope(cad_env):
    def handler(tool, params):
        if tool == "commit_undo_scope":
            _raise_execution_layer_error(tool)
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True)

    assert (result.get("error") or {}).get("code") == "EXECUTION_UNCERTAIN"
    assert cad_env.backend._undo_state is None, (
        "undo scope leaked: begin_undo_scope succeeded, commit raised, nothing rolled it back")
    assert "rollback_undo_scope" in _tools(cad_env)
    # The recovery is real now, so its outcome is reported, not a hard-coded rollback_failed.
    assert result["recovery_status"] == "discarded"
    assert result["status"] == "failed"
    assert _tools(cad_env)[-1] == "close_document"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["recovery_history"][-1]["recovery_status"] == "discarded"


def test_uncertain_execution_on_an_existing_part_rolls_back(cad_env):
    def handler(tool, params):
        if tool == "commit_undo_scope":
            _raise_execution_layer_error(tool)
        return None

    cad_env.backend.call_handler = handler
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=False)

    assert result["error"]["code"] == "EXECUTION_UNCERTAIN"
    assert cad_env.backend._undo_state is None
    assert "rollback_undo_scope" in _tools(cad_env)
    assert "close_document" not in _tools(cad_env)  # never close the user's own part
    # Undo is best effort: an existing part stays unverified until the user confirms.
    assert result["recovery_status"] == "recovery_unverified"
    assert result["automatic_retry_allowed"] is False


def test_uncertain_execution_after_commit_keeps_the_built_part(cad_env, monkeypatch):
    def lost_bookkeeping(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cad_env.orchestrator, "_record_success", lost_bookkeeping)
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True)

    assert result["error"]["code"] == "EXECUTION_UNCERTAIN"
    tools = _tools(cad_env)
    assert "commit_undo_scope" in tools
    # The scope is already committed: no rollback, and the finished part is not thrown away.
    assert "rollback_undo_scope" not in tools
    assert "close_document" not in tools
    assert result["recovery_status"] == "recovery_unverified"
    assert result["status"] == "partial"


def _failing(*tools):
    def handler(tool, params):
        if tool in tools:
            _raise_execution_layer_error(tool)
        return None
    return handler


def test_a_new_part_is_still_discarded_when_only_the_undo_fails(cad_env):
    # Closing the new part ends its scope too, so the discard is clean without the undo.
    cad_env.backend.call_handler = _failing("commit_undo_scope", "rollback_undo_scope")
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True)

    assert result["error"]["code"] == "EXECUTION_UNCERTAIN"
    assert result["recovery_status"] == "discarded"
    assert cad_env.backend._undo_state is None


def test_a_new_part_that_cannot_be_closed_is_rollback_failed(cad_env):
    cad_env.backend.call_handler = _failing(
        "commit_undo_scope", "rollback_undo_scope", "close_document")
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True)

    assert result["error"]["code"] == "EXECUTION_UNCERTAIN"
    assert result["recovery_status"] == "rollback_failed"
    assert result["status"] == "partial"
    assert result["automatic_retry_allowed"] is False


def test_an_existing_part_whose_undo_fails_stays_unverified(cad_env):
    cad_env.backend.call_handler = _failing("commit_undo_scope", "rollback_undo_scope")
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=False)

    assert result["error"]["code"] == "EXECUTION_UNCERTAIN"
    assert result["recovery_status"] == "recovery_unverified"
    assert result["automatic_retry_allowed"] is False


def test_a_recovery_that_raises_never_escapes(cad_env, monkeypatch):
    def broken_recovery(*args, **kwargs):
        raise RuntimeError("recovery crashed")

    cad_env.backend.call_handler = _failing("commit_undo_scope")
    monkeypatch.setattr(cad_env.orchestrator, "_recover_transaction", broken_recovery)
    result = cad_env.orchestrator.feature_graph(
        "job-1", cad_env.token, json.dumps(_minimal_graph()), fresh_document=True)

    assert result["error"]["code"] == "EXECUTION_UNCERTAIN"
    assert result["recovery_status"] == "rollback_failed"
    job = cad_env.store.load("job-1", cad_env.token)
    assert job["recovery_history"][-1]["error"]["code"] == "RECOVERY_RAISED"
