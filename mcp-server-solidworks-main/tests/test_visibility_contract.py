"""The SolidWorks the automation drives must stay on screen: the per-request visibility contract.

A cold-launched SolidWorks (EnsureReady's Activator.CreateInstance) starts with Visible=false and
UserControl=false. EnsureVisible writes both, but only once per attach, behind
`_sessionVisibilityApplied`, and its failures are swallowed. While the instance is still starting it
rejects those writes: on 2026-11-06 an instance left by ensure_ready still read Visible=false and
UserControl=false, so both attach-time attempts (after CreateInstance and inside RetryAttach) had
failed. Every tool call then went through EnsureConnected's live-reference shortcut, which never
reaches Connect, so nothing retried. The whole job ran in a window-less SolidWorks while the user
watched a screen on which nothing happened.

That is a regression from 1721d30 (2026-10-27). Before it, `_solidWorks` was an instance field on a
service built per request, so every tool call re-attached through Connect and re-ran EnsureVisible.
Caching the attach (to save a ~375 ms ROT lookup) also made visibility once-per-attach. The
contracts below keep the cache and put the visibility check back on every request:

1. EnsureConnected's live-reference shortcut calls the per-request check directly. Calling it only
   from Connect is the bug: Connect runs only when the cache is dead.
2. The per-request check reads the live `_solidWorks.Visible` property every time. The once-per-
   attach flag must not short-circuit it. It only un-hides the window and never takes focus.
3. Its failures are logged. A silent catch is how this bug went unnoticed: execution.log had no
   visibility lines at all. A check that keeps failing must say so.
4. The three attach-time EnsureVisible() calls stay (Connect, RetryAttach, after CreateInstance).
5. The pure inventory (GetOpenDocumentsInfo) still makes no visibility writes. It is documented as
   side-effect free, so it must not pick up the per-request check by accident.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_visibility_contract.py`
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution", "Services",
                       "SolidWorksService.cs")

PER_REQUEST = "EnsureVisiblePerRequest"


def _source():
    with open(SERVICE, encoding="utf-8") as fh:
        return fh.read()


def _code(text):
    """Drop // comment lines: the comments name these methods, the code must call them."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _method_body(signature):
    """The method's text from its signature to its matching closing brace."""
    text = _source()
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unbalanced braces after " + signature)


def _block_after(text, marker):
    """The brace block that opens at the first '{' after `marker` (an if/try/catch body)."""
    start = text.index(marker)
    open_at = text.find("{", start)
    assert open_at >= 0, "no braced block after %r in:\n%s" % (marker, text)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at:i + 1]
    raise AssertionError("unbalanced braces after " + marker)


def test_the_live_reference_shortcut_reasserts_visibility():
    body = _code(_method_body("private bool EnsureConnected()"))
    shortcut = _block_after(body, "IsAlive(_solidWorks)")
    assert PER_REQUEST + "()" in shortcut, (
        "EnsureConnected returns on a live cached attach without checking visibility, so an "
        "instance whose attach-time write failed stays hidden for the whole job:\n" + body)
    assert "return true" in shortcut, shortcut


def test_the_per_request_check_reads_the_live_property_and_never_takes_focus():
    body = _code(_method_body("private void " + PER_REQUEST + "()"))
    assert "_sessionVisibilityApplied" not in body, (
        "the per-request check must not be gated by the once-per-attach flag:\n" + body)
    assert re.search(r"if\s*\(\s*_solidWorks\.Visible\s*\)\s*return", body), (
        "read Visible first and write only when the window is hidden:\n" + body)
    assert "_solidWorks.Visible = true" in body
    assert "_solidWorks.UserControl = true" in body
    for focus in ("SetForegroundWindow", "ShowWindow", "FrameState", "ActivateDoc"):
        assert focus not in body, "un-hiding must not take focus: found " + focus


def test_the_per_request_check_logs_what_it_did_and_why_it_failed():
    body = _code(_method_body("private void " + PER_REQUEST + "()"))
    catch = _block_after(body, "catch")
    assert "ExecLog.Write(" in catch, (
        "a failed re-show must be logged, or a fix that never takes effect looks the same as "
        "one that works:\n" + body)
    tried = body[:body.index("catch")]
    assert "ExecLog.Write(" in tried, "log when a hidden window was actually re-shown:\n" + body


def test_connect_rechecks_the_live_window_for_ensure_ready():
    # Forced-hide test, 2026-11-08: with the cache alive, ensure_ready (every job's first call) goes
    # through Connect, whose EnsureVisible is once per attach, and left a hidden SolidWorks hidden.
    connect = _code(_method_body("public bool Connect()"))
    assert PER_REQUEST + "()" in connect, connect
    assert connect.index("EnsureVisible();") < connect.index(PER_REQUEST + "()"), connect


def test_the_three_attach_entries_keep_their_visibility_call():
    connect = _code(_method_body("public bool Connect()"))
    assert connect.count("EnsureVisible();") == 1, connect
    retry = _code(_method_body("private bool RetryAttach(TimeSpan timeout)"))
    assert "EnsureVisible();" in retry, retry
    ensure_ready = _code(_method_body("public Dictionary<string, object> EnsureReady()"))
    launch = ensure_ready[ensure_ready.index("Activator.CreateInstance"):
                          ensure_ready.index("RetryAttach(TimeSpan.FromSeconds(90))")]
    assert "EnsureVisible();" in launch, launch


def test_the_pure_inventory_still_makes_no_visibility_writes():
    body = _code(_method_body("public Dictionary<string, object> GetOpenDocumentsInfo()"))
    assert not re.search(r"Ensure\w*Visible\w*\(", body), body
    assert ".Visible =" not in body and ".UserControl =" not in body, body
