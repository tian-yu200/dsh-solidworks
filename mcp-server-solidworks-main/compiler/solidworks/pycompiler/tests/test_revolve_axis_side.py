"""A revolve profile must lie on one side of its axis (2026-11-09).

SolidWorks returns a null revolve for a profile that crosses its axis, and the planner learns
nothing from it. Seen on dev drawing zct01 (01:55 run, first submit) and an older desktop session;
none of the 85 graphs that built in the local sessions is flagged.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pycompiler import contours                  # noqa: E402
from pycompiler.ir_schema import validate        # noqa: E402

AXIS = {"x1": 0, "y1": -0.05, "x2": 0, "y2": 0.05}  # the sketch Y axis


def L(a, b, c, d):
    return {"kind": "line", "x1": a, "y1": b, "x2": c, "y2": d}


def _graph(profile, axis=AXIS):
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
        {"id": "sec", "type": "sketch", "ref": {"datum": "front"}, "profile": profile},
        {"id": "rev", "type": "revolve", "sketch": "sec", "axis": axis}]}


HALF = [L(0, 0, .02, 0), L(.02, 0, .02, .03), L(.02, .03, 0, .03), L(0, .03, 0, 0)]  # touches the axis
FULL = [L(-.02, 0, .02, 0), L(.02, 0, .02, .03), L(.02, .03, -.02, .03), L(-.02, .03, -.02, 0)]


def test_a_half_section_touching_the_axis_is_fine():
    (low, _i), (high, _j) = contours.axis_sides(HALF, AXIS)
    assert sorted([abs(low), abs(high)]) == [0, .02]  # one side touches the axis, the other is 20 mm out
    assert not [e for e in validate(_graph(HALF)) if "revolve axis" in e]


def test_a_full_section_across_the_axis_is_rejected_with_both_reaches():
    errors = [e for e in validate(_graph(FULL)) if "BOTH sides of the revolve axis" in e]
    assert len(errors) == 1 and "20.00 mm" in errors[0], errors


def test_a_bore_drawn_as_the_inner_edge_is_fine():
    ring = [L(.01, 0, .02, 0), L(.02, 0, .02, .03), L(.02, .03, .01, .03), L(.01, .03, .01, 0)]
    assert not [e for e in validate(_graph(ring)) if "revolve axis" in e]


def test_construction_scaffolding_across_the_axis_is_ignored():
    centre = dict(L(0, -.01, 0, .04), construction=True)
    mirror = dict(L(-.02, 0, -.02, .03), construction=True)
    assert not [e for e in validate(_graph(HALF + [centre, mirror])) if "revolve axis" in e]


def test_an_arc_bulging_over_the_axis_is_caught():
    bulge = [L(.0, 0, .02, 0), L(.02, 0, .02, .03), L(.02, .03, 0, .03),
             {"kind": "arc", "cx": 0, "cy": .015, "x1": 0, "y1": .03, "x2": 0, "y2": 0, "dir": 1}]
    assert [e for e in validate(_graph(bulge)) if "BOTH sides of the revolve axis" in e]
