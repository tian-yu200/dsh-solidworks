"""Every tool request runs with ISldWorks.CommandInProgress set, and always clears it (2026-11-08).

The execution server is out of process. Without CommandInProgress, SolidWorks services its UI
between our COM calls: measured on a 73-face part, the ReadFaces calls cost 1436 ms/face as is and
14.7 ms/face with it set (hiding the window or turning graphics updates off changed nothing). Reads
on complex parts grew past the adapter's 120 s limit and whole parts were discarded (findings
H3/H4/H5). Left set, the user's SolidWorks window stops responding, so it must be cleared.

Run with the adapter venv python from `mcp-server-solidworks-main/`.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "execution", "solidworks", "SolidworksExecution")
SERVICE = os.path.join(ROOT, "Services", "SolidWorksService.cs")
CONTROLLER = os.path.join(ROOT, "Controllers", "ToolController.cs")


def _code(path):
    with open(path, encoding="utf-8") as fh:
        return "\n".join(l for l in fh.read().splitlines() if not l.strip().startswith("//"))


def _body(text, signature):
    start = text.index(signature)
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(signature)


def test_execute_wraps_every_dispatch():
    execute = _body(_code(CONTROLLER), "public HttpResponseMessage Execute(")
    assert re.search(r"WithCommandInProgress\(\s*\(\)\s*=>\s*Dispatch\(request\)\s*\)", execute), execute


def test_it_is_cleared_in_finally_on_the_instance_it_was_set_on():
    body = _body(_code(SERVICE), "public T WithCommandInProgress<T>(")
    assert "_solidWorks.CommandInProgress = true" in body
    assert "app = _solidWorks" in body
    final = body[body.index("finally"):]
    assert "app.CommandInProgress = false" in final, final
    assert "ExecLog.Write(" in body[:body.index("try\n            {\n                return work();")] or \
        body.count("ExecLog.Write(") >= 2, "set and clear failures must be logged"


def test_a_fresh_attach_clears_a_flag_left_by_a_dead_server():
    connect = _body(_code(SERVICE), "public bool Connect()")
    attach = connect[connect.index("GetActiveObject"):connect.index("IsConnected = true")]
    assert "CommandInProgress = false" in attach, attach
