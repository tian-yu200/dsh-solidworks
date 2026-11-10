import json
import threading
import time

import pytest

from execution_health import ExecutionCircuitOpen, ExecutionHealth, RECOVERY_MESSAGE_ZH


def test_slow_reference_operation_warns_then_records_success(tmp_path):
    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=0.01, unhealthy_seconds=0.5)
    release = threading.Event()
    operation = {"id": "ref-1"}

    def invoke():
        release.wait(1)
        return {"status": "COMPLETED", "stateVersion": 2}

    threading.Timer(0.04, release.set).start()
    result = health.run_reference_geometry("op-1", 1, operation, invoke)

    assert result["status"] == "COMPLETED"
    assert not health.unhealthy
    assert not (tmp_path / "PAUSE").exists()
    rows = [json.loads(line) for line in (tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["started", "warning", "completed"]


def test_stuck_reference_op_opens_persistent_circuit_and_pauses_queue(tmp_path):
    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=0.01, unhealthy_seconds=0.04)
    release = threading.Event()

    with pytest.raises(ExecutionCircuitOpen, match="outcome is unknown"):
        health.run_reference_geometry("op-2", 3, {"type": "plane", "offset": .095},
                                      lambda: release.wait(1))

    state = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert state["status"] == "UNHEALTHY"
    assert state["operation_id"] == "op-2"
    assert (tmp_path / "PAUSE").is_file()
    assert [json.loads(line)["event"] for line in (tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()] == [
        "started", "warning", "unhealthy"]

    restarted = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE")
    assert restarted.unhealthy["operation_id"] == "op-2"
    release.set()


def test_explicit_sta_timeout_opens_circuit_even_before_monitor_deadline(tmp_path):
    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=1, unhealthy_seconds=5)
    with pytest.raises(ExecutionCircuitOpen, match="STA"):
        health.run_reference_geometry(
            "op-3", 4, {},
            lambda: {"status": "FAILED", "error": {"code": "STA_TIMEOUT", "message": "STA_TIMEOUT"}},
        )
    assert health.unhealthy["status"] == "UNHEALTHY"


def test_queue_is_paused_even_if_health_state_file_cannot_be_written(tmp_path, monkeypatch):
    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=0.01, unhealthy_seconds=0.03)
    release = threading.Event()

    def fail_state_write(_state):
        raise OSError("disk full")

    monkeypatch.setattr(health, "_write_state", fail_state_write)
    with pytest.raises(ExecutionCircuitOpen, match="outcome is unknown"):
        health.run_reference_geometry("op-state-write-fails", 4, {}, lambda: release.wait(1))

    assert health.unhealthy["status"] == "UNHEALTHY"
    assert health.unhealthy["persistence_errors"] == ["state: disk full"]
    assert (tmp_path / "PAUSE").is_file()
    release.set()


def test_backend_refuses_rollback_verify_and_identity_after_timeout(monkeypatch, tmp_path):
    import backend as backend_module
    from backend import SolidWorksBackend

    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=0.01, unhealthy_seconds=0.04)
    backend = SolidWorksBackend(execution_health=health)
    monkeypatch.setattr(backend_module, "get_state", lambda: 5)
    monkeypatch.setattr(backend_module, "_process_snapshot", lambda: {
        "solidworks_pids": [101], "execution_service_pids": [201]})
    monkeypatch.setattr(backend_module, "pooled_get", lambda *_args, **_kwargs: type(
        "Response", (), {"status_code": 503})())
    calls = []
    release = threading.Event()

    def call_tool(tool, operation_id, state_version, params, **kwargs):
        calls.append((tool, operation_id, state_version, params))
        release.wait(1)
        return {"status": "COMPLETED", "stateVersion": state_version + 1}

    monkeypatch.setattr(backend_module, "call_tool", call_tool)
    reference = backend.call("add_reference_geometry", {"type": "plane", "offset": .095})

    assert reference["error"]["code"] == "EXECUTION_UNHEALTHY"
    assert backend.rollback_transaction()["error"]["code"] == "EXECUTION_UNHEALTHY"
    assert backend.verify_state()["error"]["code"] == "EXECUTION_UNHEALTHY"
    assert backend.identity()["error"]["code"] == "EXECUTION_UNHEALTHY"
    assert len(calls) == 1
    release.set()


def test_call_already_waiting_on_backend_gate_fast_fails_when_breaker_opens(monkeypatch, tmp_path):
    import backend as backend_module
    from backend import SolidWorksBackend

    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=0.01, unhealthy_seconds=0.08)
    backend = SolidWorksBackend(execution_health=health)
    monkeypatch.setattr(backend_module, "get_state", lambda: 5)
    monkeypatch.setattr(backend_module, "_process_snapshot", lambda: {
        "solidworks_pids": [101], "execution_service_pids": [201]})
    monkeypatch.setattr(backend_module, "pooled_get", lambda *_args, **_kwargs: type(
        "Response", (), {"status_code": 503})())
    calls = []
    release = threading.Event()
    second_started = threading.Event()
    second_result = {}

    def call_tool(tool, operation_id, state_version, params, **kwargs):
        calls.append(tool)
        release.wait(1)
        return {"status": "COMPLETED", "stateVersion": state_version + 1}

    monkeypatch.setattr(backend_module, "call_tool", call_tool)

    def first_call():
        backend.call("add_reference_geometry", {"type": "plane", "offset": .095})

    def second_call():
        second_started.set()
        second_result["value"] = backend.verify_state()

    threading.Thread(target=first_call, daemon=True).start()
    deadline = time.monotonic() + .5
    while not calls and time.monotonic() < deadline:
        time.sleep(.002)
    assert calls == ["add_reference_geometry"]

    thread = threading.Thread(target=second_call, daemon=True)
    thread.start()
    assert second_started.wait(.1)
    thread.join(.3)

    assert not thread.is_alive()
    assert second_result["value"]["error"]["code"] == "EXECUTION_UNHEALTHY"
    assert calls == ["add_reference_geometry"]
    release.set()


def test_feature_graph_execution_port_routes_reference_geometry_through_backend(monkeypatch):
    import backend as backend_module
    from ir_execution_port import AdapterExecutionPort

    seen = []
    expected = {"status": "FAILED", "error": {"code": "EXECUTION_UNHEALTHY"}}

    def monitored(tool, params, state_version):
        seen.append((tool, params, state_version))
        return expected

    monkeypatch.setattr(backend_module.backend, "execute_ir_tool", monitored)
    result = AdapterExecutionPort().execute(
        "add_reference_geometry", {"type": "plane", "offset": .095}, 7
    )

    assert result is expected
    assert seen == [("add_reference_geometry", {"type": "plane", "offset": .095}, 7)]


def _open_circuit(tmp_path, baseline_instance):
    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", tmp_path / "PAUSE",
                             warning_seconds=0.005, unhealthy_seconds=0.015)
    release = threading.Event()
    with pytest.raises(ExecutionCircuitOpen):
        health.run_reference_geometry("op-recover", 9, {"type": "plane"},
                                      lambda: release.wait(1),
                                      baseline_instance=baseline_instance)
    release.set()
    return health


def test_new_healthy_instance_clears_circuit_and_owned_pause(tmp_path):
    health = _open_circuit(tmp_path, {"solidworks_pids": [101], "state_version": 9})

    assert health.try_recover({"solidworks_pids": [202], "state_version": 9}, lambda: True)
    assert health.unhealthy is None
    assert not (tmp_path / "PAUSE").exists()
    assert not (tmp_path / "health.json").exists()
    assert json.loads((tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()[-1])["event"] == "recovered"


def test_old_instance_or_failed_probe_keeps_circuit_open(tmp_path):
    health = _open_circuit(tmp_path, {"solidworks_pids": [101], "state_version": 9})

    assert not health.try_recover({"solidworks_pids": [101], "state_version": 9}, lambda: True)
    assert not health.try_recover({"solidworks_pids": [202], "state_version": 9}, lambda: False)
    assert health.unhealthy["operation_id"] == "op-recover"
    assert (tmp_path / "PAUSE").exists()


def test_new_execution_service_pid_is_a_fresh_instance(tmp_path):
    health = _open_circuit(tmp_path, {
        "solidworks_pids": [101], "execution_service_pids": [301], "state_version": 9,
    })

    assert health.try_recover({
        "solidworks_pids": [101], "execution_service_pids": [302], "state_version": 0,
    }, lambda: True)
    assert health.unhealthy is None


def test_corrupt_state_recovers_from_owned_pause_metadata_and_explains_path(tmp_path):
    state_path = tmp_path / "health.json"
    health = _open_circuit(tmp_path, {"solidworks_pids": [101], "state_version": 9})
    state_path.write_text("{truncated", encoding="utf-8")

    restarted = ExecutionHealth(state_path, tmp_path / "ops.jsonl", tmp_path / "PAUSE")
    assert restarted.unhealthy["state_corrupt"] is True
    message = restarted.recovery_message("unreadable state")
    assert "save other work" in message.casefold()
    assert RECOVERY_MESSAGE_ZH in message
    assert restarted.try_recover({"solidworks_pids": [202], "state_version": 9}, lambda: True)
    assert restarted.unhealthy is None
    assert not (tmp_path / "PAUSE").exists()


def test_recovery_never_deletes_operator_owned_pause(tmp_path):
    pause = tmp_path / "PAUSE"
    pause.write_text("operator pause\n", encoding="utf-8")
    health = ExecutionHealth(tmp_path / "health.json", tmp_path / "ops.jsonl", pause,
                             warning_seconds=0.005, unhealthy_seconds=0.015)
    release = threading.Event()
    with pytest.raises(ExecutionCircuitOpen):
        health.run_reference_geometry("op-manual-pause", 9, {}, lambda: release.wait(1),
                                      baseline_instance={"solidworks_pids": [101], "state_version": 9})
    release.set()

    assert health.try_recover({"solidworks_pids": [202], "state_version": 9}, lambda: True)
    assert pause.read_text(encoding="utf-8") == "operator pause\n"


def test_backend_recovery_requires_new_instance_and_documents_probe(monkeypatch, tmp_path):
    import backend as backend_module
    from backend import SolidWorksBackend

    health = _open_circuit(tmp_path, {
        "solidworks_pids": [101], "execution_service_pids": [201], "state_version": 9,
    })
    backend = SolidWorksBackend(execution_health=health)
    monkeypatch.setattr(backend_module, "_process_snapshot", lambda: {
        "solidworks_pids": [101], "execution_service_pids": [202]})

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    requested = []

    def pooled_get(url, timeout):
        requested.append((url, timeout))
        if url.endswith("/health"):
            return Response({"status": "UP", "comAttached": True,
                             "solidworksPids": [101], "stateVersion": 0})
        return Response({"documents": [], "active_document": None, "undo_scope_active": False})

    monkeypatch.setattr(backend_module, "pooled_get", pooled_get)

    assert backend._unhealthy_response("verify_state") is None
    assert health.unhealthy is None
    assert [url.rsplit("/", 1)[-1] for url, _timeout in requested] == ["documents"]
    assert all(timeout == backend_module.RECOVERY_PROBE_TIMEOUT_SECONDS for _url, timeout in requested)


def test_backend_does_not_recover_when_only_old_instance_is_reported(monkeypatch, tmp_path):
    import backend as backend_module
    from backend import SolidWorksBackend

    health = _open_circuit(tmp_path, {"solidworks_pids": [101], "state_version": 9})
    backend = SolidWorksBackend(execution_health=health)
    monkeypatch.setattr(backend_module, "_process_snapshot", lambda: {
        "solidworks_pids": [101], "execution_service_pids": [201]})

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def pooled_get(url, _timeout):
        if url.endswith("/health"):
            return Response({"status": "UP", "comAttached": True,
                             "solidworksPids": [101], "stateVersion": 9})
        return Response({"documents": [], "active_document": None, "undo_scope_active": False})

    requested = []

    def record_pooled_get(url, timeout):
        requested.append((url, timeout))
        return pooled_get(url, timeout)

    monkeypatch.setattr(backend_module, "pooled_get", record_pooled_get)

    response = backend._unhealthy_response("verify_state")
    assert response["error"]["code"] == "EXECUTION_UNHEALTHY"
    assert health.unhealthy["operation_id"] == "op-recover"
    assert requested == []
