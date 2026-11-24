"""Unit tests for dsh-integration/scripts/batch_build.py cold-start and --pace fixes.

Covers:
  1. BUG-2: clear_stale_undo_scope() skips cleanly on cold-start transport/503/OS errors,
     restarts the service only when undo_scope_active is True, and never swallows
     ExecutionCircuitOpen (EXECUTION_UNHEALTHY).
  2. BUG-4: pace_builds() / _view_fitter() lazily attach over COM once SolidWorks is up,
     cache the handle on success, allow explicit reset/rebind, and never permanently
     degrade to a no-op after an early cold-start failure.
  3. Demo hint: warn_if_solidworks_not_running() prints a clear non-blocking Chinese hint
     when no SLDWORKS.exe process is detected.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

import backend as backend_module
import ir_execution_port
import orchestrator as orchestrator_module
from execution_health import ExecutionCircuitOpen, ExecutionHealth


REPO_ROOT = Path(__file__).resolve().parents[4]
BATCH_BUILD_PATH = REPO_ROOT / "dsh-integration" / "scripts" / "batch_build.py"


def _load_batch_build():
    spec = importlib.util.spec_from_file_location("batch_build_under_test", BATCH_BUILD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch_build = _load_batch_build()


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeDoc:
    def __init__(self):
        self.named_views = []
        self.zoom_calls = 0

    def ShowNamedView2(self, name, view_id):
        self.named_views.append((name, view_id))

    def ViewZoomtofit2(self):
        self.zoom_calls += 1


class _FakeFrame:
    def __init__(self, hwnd=0xBEEF):
        self._hwnd = hwnd

    def GetHWnd(self):
        return self._hwnd


class _FakeSolidWorksApp:
    def __init__(self, *, pid=1234, visible=False, hwnd=0xBEEF, doc=None):
        self._pid = pid
        self.Visible = visible
        self.UserControl = False
        self._frame = _FakeFrame(hwnd)
        self.ActiveDoc = doc if doc is not None else _FakeDoc()

    def GetProcessID(self):
        return self._pid

    def Frame(self):
        return self._frame


def _install_fake_win32(monkeypatch, *, get_active_object):
    """Install fake win32com.client and win32gui modules without touching real COM."""
    win32com_mod = types.ModuleType("win32com")
    win32com_client_mod = types.ModuleType("win32com.client")
    win32com_client_mod.GetActiveObject = get_active_object
    win32com_mod.client = win32com_client_mod

    gui_calls = {"show": [], "foreground": []}
    win32gui_mod = types.ModuleType("win32gui")
    win32gui_mod.ShowWindow = lambda hwnd, cmd: gui_calls["show"].append((hwnd, cmd))
    win32gui_mod.SetForegroundWindow = lambda hwnd: gui_calls["foreground"].append(hwnd)

    monkeypatch.setitem(sys.modules, "win32com", win32com_mod)
    monkeypatch.setitem(sys.modules, "win32com.client", win32com_client_mod)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui_mod)
    return gui_calls


# ---------------------------------------------------------------------------
# BUG-2: clear_stale_undo_scope()
# ---------------------------------------------------------------------------


def test_clear_stale_undo_scope_skips_on_cold_connect_error(monkeypatch, capsys):
    killed = []
    slept = []

    def raise_connect_error():
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(backend_module.backend, "documents", raise_connect_error)
    monkeypatch.setattr(batch_build.subprocess, "run", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(batch_build.time, "sleep", lambda s: slept.append(s))

    batch_build.clear_stale_undo_scope()

    assert killed == []
    assert slept == []
    out = capsys.readouterr().out
    assert "skipping stale undo scope check" in out


def test_clear_stale_undo_scope_skips_on_503_execution_layer_error(monkeypatch, tmp_path, capsys):
    """Exercise the real SolidWorksBackend.documents() -> 503 -> ExecutionLayerError path."""
    health = ExecutionHealth(
        tmp_path / "execution-health.json",
        tmp_path / "execution-operations.jsonl",
        tmp_path / "PAUSE",
    )
    real_backend = backend_module.SolidWorksBackend(execution_health=health)
    monkeypatch.setattr(
        backend_module,
        "pooled_get",
        lambda *_a, **_k: _FakeResponse(503, {"error": "DOCUMENT_INVENTORY_UNAVAILABLE"}),
    )
    monkeypatch.setattr(backend_module, "backend", real_backend)

    killed = []
    slept = []
    monkeypatch.setattr(batch_build.subprocess, "run", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(batch_build.time, "sleep", lambda s: slept.append(s))

    batch_build.clear_stale_undo_scope()

    assert killed == []
    assert slept == []
    out = capsys.readouterr().out
    assert "DOCUMENT_INVENTORY_UNAVAILABLE" in out
    assert "skipping stale undo scope check" in out


def test_clear_stale_undo_scope_skips_on_os_error(monkeypatch, capsys):
    killed = []
    monkeypatch.setattr(
        backend_module.backend,
        "documents",
        lambda: (_ for _ in ()).throw(OSError("socket unreachable")),
    )
    monkeypatch.setattr(batch_build.subprocess, "run", lambda *a, **k: killed.append(a))

    batch_build.clear_stale_undo_scope()

    assert killed == []
    assert "skipping stale undo scope check" in capsys.readouterr().out


def test_clear_stale_undo_scope_restarts_service_when_undo_scope_active(monkeypatch, capsys):
    monkeypatch.setattr(
        backend_module.backend,
        "documents",
        lambda: {"undo_scope_active": True, "documents": [], "active_document": None},
    )
    commands = []
    slept = []
    monkeypatch.setattr(batch_build.subprocess, "run", lambda cmd, **kw: commands.append((cmd, kw)))
    monkeypatch.setattr(batch_build.time, "sleep", lambda s: slept.append(s))

    batch_build.clear_stale_undo_scope()

    assert len(commands) == 1
    assert commands[0][0] == ["taskkill", "/IM", "SolidworksExecution.exe", "/F"]
    assert slept == [2]
    assert "restarting the service" in capsys.readouterr().out


def test_clear_stale_undo_scope_noop_when_undo_scope_inactive(monkeypatch):
    monkeypatch.setattr(
        backend_module.backend,
        "documents",
        lambda: {"undo_scope_active": False, "documents": [], "active_document": None},
    )
    commands = []
    slept = []
    monkeypatch.setattr(batch_build.subprocess, "run", lambda *a, **k: commands.append(a))
    monkeypatch.setattr(batch_build.time, "sleep", lambda s: slept.append(s))

    batch_build.clear_stale_undo_scope()

    assert commands == []
    assert slept == []


def test_clear_stale_undo_scope_propagates_execution_circuit_open(monkeypatch, tmp_path):
    """EXECUTION_UNHEALTHY circuit breaker (real ExecutionHealth + SolidWorksBackend) is never swallowed."""
    health_file = tmp_path / "execution-health.json"
    health_file.write_text(
        json.dumps({"status": "UNHEALTHY", "reason": "STA_TIMEOUT on add_reference_geometry"}),
        encoding="utf-8",
    )
    health = ExecutionHealth(
        health_file,
        tmp_path / "execution-operations.jsonl",
        tmp_path / "PAUSE",
    )
    real_backend = backend_module.SolidWorksBackend(execution_health=health)
    monkeypatch.setattr(
        backend_module,
        "_process_snapshot",
        lambda: {"solidworks_pids": [], "execution_service_pids": []},
    )
    monkeypatch.setattr(backend_module, "backend", real_backend)

    killed = []
    monkeypatch.setattr(batch_build.subprocess, "run", lambda *a, **k: killed.append(a))

    with pytest.raises(ExecutionCircuitOpen, match="EXECUTION_UNHEALTHY"):
        batch_build.clear_stale_undo_scope()

    assert killed == []


# ---------------------------------------------------------------------------
# BUG-4: --pace lazy COM attach & caching
# ---------------------------------------------------------------------------


def test_pace_builds_lazy_attaches_after_cold_start_and_caches_handle(monkeypatch, capsys):
    state = {"ready": False, "get_active_calls": 0, "app": _FakeSolidWorksApp(pid=4321, visible=False)}

    def fake_get_active(prog_id):
        assert prog_id == "SldWorks.Application"
        state["get_active_calls"] += 1
        if not state["ready"]:
            raise RuntimeError("Operation unavailable")
        return state["app"]

    gui_calls = _install_fake_win32(monkeypatch, get_active_object=fake_get_active)
    monkeypatch.setattr(batch_build, "_solidworks_pids", lambda: [4321])
    slept = []
    monkeypatch.setattr(batch_build.time, "sleep", lambda s: slept.append(s))

    # Patch AdapterExecutionPort.execute cleanly so monkeypatch restores it after the test.
    def fake_original_execute(self, tool, params, state_version):
        return {"status": params.get("_status", "COMPLETED"), "stateVersion": state_version + 1}

    monkeypatch.setattr(ir_execution_port.AdapterExecutionPort, "execute", fake_original_execute)

    fitter = batch_build.pace_builds(1.5)

    # Cold start: pace_builds() must NOT eagerly call GetActiveObject before begin_job().
    assert state["get_active_calls"] == 0
    assert not fitter.attached

    port = ir_execution_port.AdapterExecutionPort()

    # Early attempt before SolidWorks is ready must NOT permanently degrade to a no-op.
    assert fitter.attach() is False
    assert state["get_active_calls"] == 1
    assert not fitter.attached
    assert "will retry once SolidWorks is up" in capsys.readouterr().out

    # SolidWorks is now brought up by begin_job().
    state["ready"] = True

    # Sketch tool pauses briefly without touching COM view fitting.
    port.execute("add_sketch_entity", {}, 1)
    assert slept == [0.3]
    assert state["get_active_calls"] == 1
    assert not fitter.attached

    # First feature tool triggers lazy COM attach, brings hidden window forward, and fits view.
    port.execute("extrude_feature", {}, 2)
    assert state["get_active_calls"] == 2
    assert fitter.attached
    assert state["app"].Visible is True
    assert state["app"].UserControl is True
    assert gui_calls["show"] == [(0xBEEF, 3)]
    assert gui_calls["foreground"] == [0xBEEF]
    assert state["app"].ActiveDoc.named_views == [("*Isometric", 7)]
    assert state["app"].ActiveDoc.zoom_calls == 1
    assert slept == [0.3, 1.5]

    # Second feature tool reuses the cached handle (no duplicate GetActiveObject / ShowWindow).
    port.execute("add_edge_feature", {}, 3)
    assert state["get_active_calls"] == 2
    assert gui_calls["show"] == [(0xBEEF, 3)]
    assert state["app"].ActiveDoc.named_views == [("*Isometric", 7), ("*Isometric", 7)]
    assert state["app"].ActiveDoc.zoom_calls == 2
    assert slept == [0.3, 1.5, 1.5]

    # Failed feature tool does not fit view or sleep.
    port.execute("extrude_feature", {"_status": "FAILED"}, 4)
    assert state["app"].ActiveDoc.zoom_calls == 2
    assert slept == [0.3, 1.5, 1.5]


def test_view_fitter_explicit_reset_allows_rebinding(monkeypatch):
    apps = [_FakeSolidWorksApp(pid=1001, visible=True), _FakeSolidWorksApp(pid=2002, visible=True)]
    calls = {"count": 0}

    def fake_get_active(_prog_id):
        idx = calls["count"]
        calls["count"] += 1
        return apps[idx]

    _install_fake_win32(monkeypatch, get_active_object=fake_get_active)
    monkeypatch.setattr(batch_build, "_solidworks_pids", lambda: [1001, 2002])

    fitter = batch_build._view_fitter()
    fitter()
    assert calls["count"] == 1
    assert apps[0].ActiveDoc.zoom_calls == 1

    fitter.reset()
    assert not fitter.attached

    fitter()
    assert calls["count"] == 2
    assert apps[1].ActiveDoc.zoom_calls == 1


def test_build_once_calls_on_ready_after_begin_job_and_before_countdown(monkeypatch, tmp_path):
    events = []

    class FakeOrch:
        def begin_job(self, intent, target):
            events.append("begin_job")
            return {"status": "completed", "job_id": "12345678-job", "job_token": "tok"}

        def feature_graph(self, job_id, token, graph_json, fresh_document=True):
            events.append("feature_graph")
            return {"status": "completed", "warnings": []}

        def save_or_export(self, job_id, token, op, file_path=""):
            events.append("save_or_export")
            return {"status": "completed"}

        def finish_job(self, job_id, token, keep_open=False):
            events.append("finish_job")
            return {"status": "completed", "trust_level": "verified", "verification_status": "passed"}

    monkeypatch.setattr(batch_build, "countdown", lambda sec: events.append(f"countdown:{sec}"))

    batch_build.build_once(
        FakeOrch(),
        "{}",
        "intent",
        tmp_path / "part.SLDPRT",
        keep_open=False,
        lead_in=5,
        on_ready=lambda: events.append("on_ready"),
    )

    assert events == [
        "begin_job",
        "on_ready",
        "countdown:5",
        "feature_graph",
        "save_or_export",
        "finish_job",
    ]


# ---------------------------------------------------------------------------
# Demo hint & main() integration
# ---------------------------------------------------------------------------


def test_warn_if_solidworks_not_running_prints_hint_without_blocking(monkeypatch, capsys):
    monkeypatch.setattr(batch_build, "_solidworks_pids", lambda: [])
    assert batch_build.warn_if_solidworks_not_running() is True
    out = capsys.readouterr().out
    assert "未检测到已打开的 SolidWorks 窗口" in out
    assert "现场演示建议先手动打开 SolidWorks 窗口" in out

    monkeypatch.setattr(batch_build, "_solidworks_pids", lambda: [9876])
    assert batch_build.warn_if_solidworks_not_running() is False
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(
        batch_build,
        "_solidworks_pids",
        lambda: (_ for _ in ()).throw(OSError("tasklist unavailable")),
    )
    assert batch_build.warn_if_solidworks_not_running() is False


def test_main_cold_start_warns_skips_undo_and_lazy_attaches_pace(monkeypatch, tmp_path, capsys):
    graph_file = tmp_path / "demo.json"
    graph_file.write_text(json.dumps({"nodes": []}), encoding="utf-8")
    out_dir = tmp_path / "out"

    sw_state = {
        "up": False,
        "get_active_calls": 0,
        "app": _FakeSolidWorksApp(pid=5555, visible=False),
    }

    def fake_get_active(_prog_id):
        sw_state["get_active_calls"] += 1
        if not sw_state["up"]:
            raise RuntimeError("SolidWorks not started yet")
        return sw_state["app"]

    _install_fake_win32(monkeypatch, get_active_object=fake_get_active)
    monkeypatch.setattr(batch_build, "_solidworks_pids", lambda: [5555] if sw_state["up"] else [])
    monkeypatch.setattr(batch_build, "validate_graph", lambda _g: None)
    monkeypatch.setattr(batch_build, "dsh_server_pids", lambda: [])
    monkeypatch.setattr(batch_build, "LOCK_PATH", tmp_path / ".batch_build.lock")
    monkeypatch.setattr(batch_build.time, "sleep", lambda _s: None)

    # Cold start: execution service is not listening on port 5000.
    monkeypatch.setattr(
        backend_module.backend,
        "documents",
        lambda: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )

    def fake_execute(self, tool, params, state_version):
        return {"status": "COMPLETED", "stateVersion": state_version + 1}

    monkeypatch.setattr(ir_execution_port.AdapterExecutionPort, "execute", fake_execute)

    class FakeOrch:
        def begin_job(self, intent, target):
            # begin_job brings SolidWorks up on the cold machine.
            sw_state["up"] = True
            return {"status": "completed", "job_id": "abcdef12-job", "job_token": "tok"}

        def feature_graph(self, job_id, token, graph_json, fresh_document=True):
            ir_execution_port.AdapterExecutionPort().execute("extrude_feature", {}, 1)
            ir_execution_port.AdapterExecutionPort().execute("add_edge_feature", {}, 2)
            return {"status": "completed", "warnings": []}

        def save_or_export(self, job_id, token, op, file_path=""):
            return {"status": "completed"}

        def finish_job(self, job_id, token, keep_open=False):
            return {"status": "completed", "trust_level": "verified", "verification_status": "passed"}

    monkeypatch.setattr(orchestrator_module, "orchestrator", FakeOrch())
    monkeypatch.setattr(
        sys,
        "argv",
        ["batch_build.py", "--graph", str(graph_file), "--name", "DemoPart",
         "--out", str(out_dir), "--pace", "0.5", "--lead-in", "0"],
    )

    rc = batch_build.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "未检测到已打开的 SolidWorks 窗口" in out
    assert "skipping stale undo scope check" in out
    # Attached once right after begin_job() and cached for both subsequent features.
    assert sw_state["get_active_calls"] == 1
    assert sw_state["app"].Visible is True
    assert sw_state["app"].ActiveDoc.named_views == [("*Isometric", 7), ("*Isometric", 7)]
    assert sw_state["app"].ActiveDoc.zoom_calls == 2


def test_main_stops_loudly_when_execution_circuit_is_open(monkeypatch, tmp_path, capsys):
    graph_file = tmp_path / "demo.json"
    graph_file.write_text(json.dumps({"nodes": []}), encoding="utf-8")

    monkeypatch.setattr(batch_build, "_solidworks_pids", lambda: [1111])
    monkeypatch.setattr(batch_build, "validate_graph", lambda _g: None)
    monkeypatch.setattr(batch_build, "dsh_server_pids", lambda: [])
    monkeypatch.setattr(batch_build, "LOCK_PATH", tmp_path / ".batch_build.lock")
    monkeypatch.setattr(
        backend_module.backend,
        "documents",
        lambda: (_ for _ in ()).throw(
            ExecutionCircuitOpen("The SolidWorks execution backend is UNHEALTHY")
        ),
    )

    built = []
    monkeypatch.setattr(batch_build, "build_once", lambda *a, **k: built.append(a))
    monkeypatch.setattr(
        sys,
        "argv",
        ["batch_build.py", "--graph", str(graph_file), "--name", "DemoPart", "--out", str(tmp_path / "out")],
    )

    rc = batch_build.main()
    out = capsys.readouterr().out

    assert rc == 2
    assert "EXECUTION_UNHEALTHY" in out
    assert built == []
