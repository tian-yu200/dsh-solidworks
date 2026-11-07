#!/usr/bin/env python
"""Batch part builder for the dsh-solidwork pipeline.

Drives the existing orchestrator directly (no DSH host needed):
    begin_job -> feature_graph(fresh) -> save -> finish_job

Usage:
    python batch_build.py --graph g.json --name ValveBody [--out DIR] [--repeat N]
                          [--intent "..."] [--keep-open] [--force] [--pace SECONDS]

--pace replays a graph slowly enough to film: after each feature the view goes to
isometric + zoom-to-fit and the build waits SECONDS; each sketch entity waits a fraction
of that. It is the same graph through the same compiler, only slower — a live DSH build
of the 44-node 阀体 took 28 s, too fast to watch.

The graph JSON is the same schema submit_feature_graph accepts. Every build is a
NEW document; the output file is never overwritten (numeric suffix is added).
Timing per stage is printed so batch throughput can be measured honestly.

Builds are strictly serial: SolidWorks has one active document, and two drivers
switching it under each other break the save (measured 2026-11-02). The script
refuses to start while a DSH-launched SolidWorks server or another batch runs.

A pending confirmation (a recovery gate, or anything else the orchestrator holds
for the user) is the USER's decision: it is shown and asked on an interactive
terminal, and stops the batch otherwise. It is never confirmed automatically.
"""
import argparse
import json
import msvcrt
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DSH = REPO / "mcp-server-solidworks-main" / "adapters" / "dsh"
COMPILER = REPO / "mcp-server-solidworks-main" / "compiler" / "solidworks"
LOCK_PATH = REPO / "artifacts" / ".batch_build.lock"
DSH_SERVER_RE = re.compile(r"adapters[\\/]+dsh[\\/]+server\.py", re.I)

sys.path.insert(0, str(DSH))
sys.path.insert(0, str(COMPILER))


class Stop(Exception):
    """Ends the whole batch: the next run would meet the same state."""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dsh_server_pids():
    """PIDs of DSH-launched SolidWorks MCP servers, or None when the process list is unreadable."""
    query = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        rows = json.loads(out) if out else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    rows = rows if isinstance(rows, list) else [rows]
    return [r["ProcessId"] for r in rows if DSH_SERVER_RE.search(r.get("CommandLine") or "")]


def acquire_batch_lock():
    """Held for the life of the process; Windows releases it if the process dies."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise Stop("another batch_build.py is already running")
    return handle


# Compiler calls that leave a new feature in the tree (revolve and sweep go through extrude_feature).
FEATURE_TOOLS = frozenset({"extrude_feature", "add_edge_feature", "add_cosmetic_thread", "create_pattern",
                           "create_shell", "create_rib", "sheet_metal_feature", "sketched_bend",
                           "edge_flange", "add_reference_geometry", "insert_component", "add_mate"})
SKETCH_TOOLS = frozenset({"add_sketch_entity"})


def _com(obj, name, *args):
    """Late-bound COM call. pywin32 already runs a no-argument method on attribute lookup and hands
    back its result (an int, or a CDispatch that is itself callable), so only a bound method is called."""
    member = getattr(obj, name)
    return member(*args) if type(member).__name__ == "method" else member


def _view_fitter():
    """Show the SolidWorks the execution layer drives, and fit each step's view through its COM object.

    That instance is the first one in the Running Object Table, the same one GetActiveObject
    returns here. It can be running without a window: on 2026-11-02 the replay built and saved
    in a SolidWorks that had been hidden since noon, while the user watched a second instance.
    So it is made visible and brought to the front before the countdown. View only: nothing in
    the model changes. Returns a no-op when SolidWorks cannot be reached (press F to fit by hand)."""
    try:
        import win32com.client
        import win32gui
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        pid = _com(sw, "GetProcessID")
        if not sw.Visible:
            log(f"SolidWorks PID {pid} was running without a window; showing it")
        sw.Visible = True
        sw.UserControl = True
        hwnd = _com(_com(sw, "Frame"), "GetHWnd")
        win32gui.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001 — Windows may refuse focus stealing; the window is still shown
            pass
        others = sorted(p for p in _solidworks_pids() if p != pid)
        if others:
            log(f"note: the build runs in SolidWorks PID {pid}; PID {', '.join(map(str, others))} "
                "is another SolidWorks window that will NOT change")
    except Exception as exc:  # noqa: BLE001 — any COM failure only costs the auto view
        log(f"could not reach SolidWorks over COM ({exc}); show its window yourself and press F to fit")
        return lambda: None

    def fit():
        try:
            doc = sw.ActiveDoc
            if doc is not None:
                _com(doc, "ShowNamedView2", "*Isometric", 7)
                _com(doc, "ViewZoomtofit2")
        except Exception:  # noqa: BLE001
            pass
    return fit


def _solidworks_pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, timeout=30).stdout
    return [int(row.split('","')[1]) for row in out.splitlines() if row.count('","') >= 2]


def pace_builds(seconds):
    """Slow every compiler call down for filming: a pause after each feature, a short one per sketch entity."""
    import ir_execution_port

    fit = _view_fitter()
    original = ir_execution_port.AdapterExecutionPort.execute

    def execute(self, tool, params, state_version):
        response = original(self, tool, params, state_version)
        if response.get("status") == "COMPLETED":
            if tool in FEATURE_TOOLS:
                fit()
                time.sleep(seconds)
            elif tool in SKETCH_TOOLS:
                time.sleep(min(seconds / 5, 0.4))
        return response

    ir_execution_port.AdapterExecutionPort.execute = execute


class StopAfterThisPart:
    """Ctrl+C during a build finishes the current part first, then ends the batch.

    A build killed halfway leaves the execution layer's undo scope open and a half-built part,
    and every later build then fails at its first step with UNDO_SCOPE_ACTIVE (2026-11-02,
    twice while filming). A second Ctrl+C still aborts at once, with that warning."""

    def __init__(self):
        self.requested = False
        self._previous = None

    def __enter__(self):
        self._previous = signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *exc):
        signal.signal(signal.SIGINT, self._previous)

    def _handler(self, signum, frame):
        if self.requested:
            signal.signal(signal.SIGINT, self._previous)
            log("second Ctrl+C: aborting now; the half-built part is left open")
            raise KeyboardInterrupt
        self.requested = True
        log("Ctrl+C: finishing this part first so SolidWorks is left clean, then stopping "
            "(press Ctrl+C again to abort at once)")


INTERRUPT = StopAfterThisPart()


def clear_stale_undo_scope():
    """Restart the execution service when an interrupted build left its undo scope open.

    Only the scope's own document can close it, and an interrupted build's half-built part is
    usually closed by hand afterwards, so the open scope can only be dropped by restarting the
    service. Safe here: the batch lock is held and no DSH server is running, so no build of
    anyone else's is in flight. begin_job starts the service again and reattaches SolidWorks."""
    from backend import backend

    if not backend.documents().get("undo_scope_active"):
        return
    log("an interrupted build left the execution service's undo scope open; restarting the service")
    subprocess.run(["taskkill", "/IM", "SolidworksExecution.exe", "/F"], capture_output=True, timeout=30)
    time.sleep(2)


def countdown(seconds):
    for left in range(seconds, 0, -1):
        if INTERRUPT.requested:
            raise Stop("cancelled with Ctrl+C before anything was built")
        log(f"building in {left} s — bring SolidWorks to the front and start recording")
        time.sleep(1)


def validate_graph(graph):
    from pycompiler.ir_schema import validate

    errors = validate(graph)
    if errors:
        raise Stop("VALIDATION_FAILED: " + "; ".join(errors))


def unwrap(result):
    return result if isinstance(result, dict) else json.loads(result)


def settle_pending(orch, job_id, token, pending):
    """Show a held action to the user; confirm it only on their explicit 'yes'."""
    action_id = pending["action_id"]
    req = unwrap(orch.request_confirmation(job_id, token, action_id))
    summary = req.get("action_summary", "")
    log(f"confirmation required: {summary}")
    answer = ""
    if sys.stdin.isatty():
        answer = input("Execute this action? Type yes to confirm: ").strip().lower()
    if answer != "yes":
        orch.cancel_action(job_id, token, action_id)
        raise Stop("stopped at a confirmation the user did not give "
                   "(rerun in an interactive terminal to decide, or resolve it in DSH)")
    return unwrap(orch.confirm_action(
        job_id, token, action_id,
        req["confirmation_token"], req["confirmation_text"], summary,
    ))


def next_index(directory, name, suffix):
    """First N such that name_N.suffix and every later index are free."""
    taken = [int(m.group(1)) for p in directory.glob(f"{name}_*{suffix}")
             if (m := re.fullmatch(re.escape(name) + r"_(\d+)" + re.escape(suffix), p.name))]
    return max(taken, default=0) + 1


def unique_path(directory, name, suffix):
    candidate = directory / f"{name}{suffix}"
    return candidate if not candidate.exists() else directory / f"{name}_{next_index(directory, name, suffix)}{suffix}"


def build_once(orch, graph_json, intent, out_path, keep_open, lead_in=0):
    t = {"begin": 0.0, "build": 0.0, "save": 0.0, "finish": 0.0}
    t0 = time.time()
    r = unwrap(orch.begin_job(intent, ""))  # brings the execution layer and SolidWorks up
    if r.get("status") != "completed":
        raise Stop(f"begin_job failed: {r.get('error')}")
    job_id, token = r["job_id"], r["job_token"]
    t["begin"] = time.time() - t0
    log(f"job {job_id[:8]} begun ({t['begin']:.1f}s)")
    countdown(lead_in)

    t0 = time.time()
    r = unwrap(orch.feature_graph(job_id, token, graph_json, fresh_document=True))
    if r.get("status") == "pending_confirmation":
        r = settle_pending(orch, job_id, token, r)
    t["build"] = time.time() - t0
    if r.get("status") != "completed":
        err = r.get("error") or {}
        raise RuntimeError(
            f"build failed: {err.get('code')}: {str(err.get('message'))[:300]}"
        )
    warnings = r.get("warnings") or []
    log(f"graph built ({t['build']:.1f}s, {len(warnings)} warning(s))")

    t0 = time.time()
    r = unwrap(orch.save_or_export(job_id, token, "save", file_path=str(out_path)))
    if r.get("status") == "pending_confirmation":
        r = settle_pending(orch, job_id, token, r)
    if r.get("status") != "completed":
        raise RuntimeError(f"save failed: {r.get('error')}")
    t["save"] = time.time() - t0
    log(f"saved {out_path} ({t['save']:.1f}s)")

    t0 = time.time()
    r = unwrap(orch.finish_job(job_id, token, keep_open=keep_open))
    if r.get("status") != "completed":
        raise RuntimeError(f"finish_job failed: {r.get('error')}")
    t["finish"] = time.time() - t0
    trust = r.get("trust_level", "?")
    verif = r.get("verification_status", "?")
    log(f"job finished ({t['finish']:.1f}s) verification={verif} trust={trust}")
    return t, verif, trust, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", required=True, help="feature graph JSON path")
    ap.add_argument("--name", required=True, help="output file base name")
    ap.add_argument("--out", default=str(REPO / "artifacts" / "batch"))
    ap.add_argument("--repeat", type=int, default=1, help="build N copies")
    ap.add_argument("--intent", default="Batch build from feature graph (batch_build.py)")
    ap.add_argument("--keep-open", action="store_true", help="finish each job but leave its document open")
    ap.add_argument("--force", action="store_true",
                    help="run even though a DSH SolidWorks server is running (you are sure it is idle)")
    ap.add_argument("--pace", type=float, default=0.0,
                    help="demo replay for filming: seconds to pause after each feature (e.g. 2)")
    ap.add_argument("--lead-in", type=int, default=None,
                    help="seconds of countdown before the build starts (default 10 with --pace, else 0)")
    args = ap.parse_args()
    lead_in = args.lead_in if args.lead_in is not None else (10 if args.pace > 0 else 0)

    graph = json.loads(Path(args.graph).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        validate_graph(graph)
        log(f"graph valid: {len(graph.get('nodes', []))} nodes")
        lock = acquire_batch_lock()
        pids = dsh_server_pids()
        if pids is None:
            log("WARNING: could not read the process list; make sure no DSH session is building")
        elif pids and not args.force:
            raise Stop(f"a DSH SolidWorks server is running (PID {', '.join(map(str, pids))}); "
                       "close DSH, or pass --force if it is certainly idle")
    except Stop as exc:
        log(f"NOT STARTED: {exc}")
        return 2

    graph_json = json.dumps(graph, ensure_ascii=False)
    import orchestrator

    orch = orchestrator.orchestrator
    clear_stale_undo_scope()
    if args.pace > 0:
        pace_builds(args.pace)
        log(f"demo replay: {args.pace:g} s after each feature, view fitted to isometric")
    runs = []
    first = next_index(out_dir, args.name, ".SLDPRT") if args.repeat > 1 else None
    total0 = time.time()
    for i in range(args.repeat):
        suffix = ".SLDPRT"
        out_path = (unique_path(out_dir, args.name, suffix) if first is None
                    else out_dir / f"{args.name}_{first + i}{suffix}")
        log(f"=== run {i + 1}/{args.repeat} -> {out_path.name}")
        try:
            with INTERRUPT:
                t, verif, trust, warnings = build_once(orch, graph_json, args.intent, out_path,
                                                       args.keep_open, lead_in)
            runs.append({"file": str(out_path), "ok": True, "timing": t, "verification": verif, "trust": trust, "warnings": warnings})
        except Stop as exc:
            runs.append({"file": str(out_path), "ok": False, "error": str(exc)})
            log(f"STOPPED: {exc}")
            break
        except Exception as exc:
            runs.append({"file": str(out_path), "ok": False, "error": str(exc)})
            log(f"FAILED: {exc}")
        if INTERRUPT.requested:
            log("stopped after this part, as asked with Ctrl+C")
            break
    total = time.time() - total0
    lock.close()

    ok = [r for r in runs if r["ok"]]
    print("\n===== batch summary =====")
    print(f"runs: {len(ok)}/{args.repeat} succeeded, wall {total:.1f}s, avg {total / max(len(runs), 1):.1f}s/run")
    for r in runs:
        if r["ok"]:
            tt = r["timing"]
            print(f"  OK  {Path(r['file']).name}: begin {tt['begin']:.1f}s + build {tt['build']:.1f}s + save {tt['save']:.1f}s + finish {tt['finish']:.1f}s -> {r['verification']}/{r['trust']}")
            for w in r["warnings"]:
                print(f"      warning: {w.get('code')}")
        else:
            print(f"  FAIL {Path(r['file']).name}: {r['error'][:160]}")
    return 0 if len(ok) == args.repeat else 1


if __name__ == "__main__":
    sys.exit(main())
