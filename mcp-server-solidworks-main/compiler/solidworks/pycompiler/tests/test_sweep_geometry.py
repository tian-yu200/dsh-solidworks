"""Plan-time sweep checks: the path must pierce the profile plane inside the profile, and bend
wider than the profile (2026-11-09). Both otherwise come back as a null sweep mid-build.

The path is drawn on the front plane (x right, y up) from the origin upwards; the pipe profile is
a circle on the top plane, whose sketch x is model X and sketch y is model -Z.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pycompiler.ir_schema import validate        # noqa: E402


def L(a, b, c, d):
    return {"kind": "line", "x1": a, "y1": b, "x2": c, "y2": d}


def _graph(path, profile):
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
        {"id": "path", "type": "sketch", "ref": {"datum": "front"}, "profile": path},
        {"id": "sec", "type": "sketch", "ref": {"datum": "top"}, "profile": profile},
        {"id": "pipe", "type": "sweep", "sketch": "sec", "path": "path"}]}


def _sweep_errors(path, profile):
    return [e for e in validate(_graph(path, profile)) if "sweep" in e and
            ("plane" in e or "bends" in e or "outside the profile" in e)]


BENT = [L(0, 0, 0, .05), {"kind": "fillet", "radius": .02}, L(0, .05, .06, .05)]
PIPE = [{"kind": "circle", "diameter": .01}]


def test_a_pipe_bent_wider_than_its_section_passes():
    assert _sweep_errors(BENT, PIPE) == []


def test_a_path_that_starts_off_the_profile_plane_is_named():
    lifted = [L(0, .01, 0, .05), {"kind": "fillet", "radius": .02}, L(0, .05, .06, .05)]
    errors = _sweep_errors(lifted, PIPE)
    assert len(errors) == 1 and "never reaches the profile's plane" in errors[0] and "10.00 mm" in errors[0]


def test_a_profile_beside_the_path_is_named():
    errors = _sweep_errors(BENT, [{"kind": "circle", "diameter": .01, "cx": .03}])
    assert len(errors) == 1 and "outside the profile" in errors[0]


def test_a_bend_tighter_than_the_section_is_named():
    tight = [L(0, 0, 0, .05), {"kind": "fillet", "radius": .004}, L(0, .05, .06, .05)]
    errors = _sweep_errors(tight, PIPE)
    assert len(errors) == 1 and "R4.00 mm" in errors[0] and "5.00 mm" in errors[0]


def test_a_face_sketch_is_left_to_the_build():
    graph = _graph(BENT, PIPE)
    graph["nodes"][1]["ref"] = {"face": {"near": [0, 0, 0]}}
    assert not [e for e in validate(graph) if "never reaches" in e or "bends at" in e]
