"""contours.chain_conflicts: closed line/arc chains that cross or retrace themselves (2026-11-08).

The 缺口块 bench run closed its upright outline with a bottom line that retraced the first line,
every endpoint paired, and SolidWorks returned a null boss twice. These pin that such a chain now
fails validation and that legal outlines (tangent arcs, nested holes, touching points) do not.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pycompiler import contours                # noqa: E402
from pycompiler.ir_schema import validate      # noqa: E402


def _line(x1, y1, x2, y2):
    return {"kind": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _graph(profile):
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
        {"id": "base", "type": "box", "ref": {"datum": "top"}, "width": 0.05, "depth": 0.05, "height": 0.01},
        {"id": "s", "type": "sketch", "ref": {"datum": "front"}, "profile": profile},
        {"id": "e", "type": "extrude", "sketch": "s", "operation": "boss", "depth": 0.01}]}


# The upright profile of bench run f800dcde, verbatim.
QUEKOUKUAI = [
    _line(-0.0175, 0.01, -0.0075, 0.01),
    _line(-0.0075, 0.01, -0.0075, 0.02),
    {"kind": "arc", "cx": 0.0025, "cy": 0.02, "x1": -0.0075, "y1": 0.02, "x2": 0.0025, "y2": 0.03, "dir": 1},
    _line(0.0025, 0.03, 0.0175, 0.03),
    _line(0.0175, 0.03, 0.0175, 0.01),
    _line(0.0175, 0.01, -0.0175, 0.01),
]


def test_the_bench_outline_that_retraced_its_first_line_fails_at_plan_time():
    assert contours.open_gaps(QUEKOUKUAI) == []  # why the old checks let it through
    found = contours.chain_conflicts(QUEKOUKUAI)
    assert [(c["how"], c["a"], c["b"]) for c in found] == [("collinear", 0, 5)], found
    errors = [e for e in validate(_graph(QUEKOUKUAI)) if "self-intersecting" in e]
    assert errors and "profile[0] (line) and profile[5] (line)" in errors[0], errors
    assert "extrude 'e'" in errors[0], errors


def test_the_same_outline_drawn_as_a_simple_loop_passes():
    fixed = [
        _line(-0.0075, 0.01, -0.0075, 0.02),
        {"kind": "arc", "cx": 0.0025, "cy": 0.02, "x1": -0.0075, "y1": 0.02, "x2": 0.0025, "y2": 0.03, "dir": -1},
        _line(0.0025, 0.03, 0.0175, 0.03),
        _line(0.0175, 0.03, 0.0175, 0.01),
        _line(0.0175, 0.01, -0.0075, 0.01),
    ]
    assert contours.chain_conflicts(fixed) == []
    assert not [e for e in validate(_graph(fixed)) if "self-intersecting" in e]


def test_a_bowtie_crosses_itself():
    bowtie = [_line(0, 0, 0.02, 0.02), _line(0.02, 0.02, 0.02, 0), _line(0.02, 0, 0, 0.02), _line(0, 0.02, 0, 0)]
    found = contours.chain_conflicts(bowtie)
    assert [(c["how"], c["a"], c["b"]) for c in found] == [("cross", 0, 2)], found
    assert abs(found[0]["point"][0] - 0.01) < 1e-9 and abs(found[0]["point"][1] - 0.01) < 1e-9


def test_a_tangent_corner_arc_is_legal():
    # 20 x 20 square with an R5 fillet at the top-right corner, tangent to both edges.
    rounded = [
        _line(0, 0, 0.02, 0), _line(0.02, 0, 0.02, 0.015),
        {"kind": "arc", "cx": 0.015, "cy": 0.015, "x1": 0.02, "y1": 0.015, "x2": 0.015, "y2": 0.02, "dir": 1},
        _line(0.015, 0.02, 0, 0.02), _line(0, 0.02, 0, 0),
    ]
    assert contours.chain_conflicts(rounded) == []


def test_a_hole_inside_the_outline_is_legal_and_one_through_it_is_not():
    square = [_line(0, 0, 0.04, 0), _line(0.04, 0, 0.04, 0.04), _line(0.04, 0.04, 0, 0.04), _line(0, 0.04, 0, 0)]
    inside = {"kind": "circle", "cx": 0.02, "cy": 0.02, "diameter": 0.01}
    across = {"kind": "circle", "cx": 0.04, "cy": 0.02, "diameter": 0.01}
    assert contours.chain_conflicts(square + [inside]) == []
    found = contours.chain_conflicts(square + [across])
    assert [(c["how"], c["a"], c["b"], c["b_kind"]) for c in found] == [("cross", 1, 4, "circle")], found


def test_a_chain_that_touches_itself_at_one_point_is_left_alone():
    # Two triangles meeting at a vertex: touching at a point is legal here as in loop_conflicts.
    touching = [_line(0, 0, 0.01, 0.01), _line(0.01, 0.01, 0, 0.02), _line(0, 0.02, 0, 0),
                _line(0.01, 0.01, 0.02, 0), _line(0.02, 0, 0.02, 0.02), _line(0.02, 0.02, 0.01, 0.01)]
    assert contours.chain_conflicts(touching) == []
