"""The `fillet` profile primitive: an exact tangent arc at a sharp corner (2026-11-09, H15/H12).

Planners computed tangent points by hand and the 脚踏座 R100 blend came out 0.75 mm proud of the hub
in two independent runs; an edge fillet instead needs an edge anchor, which often fails to resolve.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pycompiler import contours, lowering       # noqa: E402
from pycompiler.ir_schema import validate       # noqa: E402

TOL = 1e-9


def L(x1, y1, x2, y2):
    return {"kind": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def F(r):
    return {"kind": "fillet", "radius": r}


def _dist_to_line(p, line):
    (x1, y1, x2, y2) = (line["x1"], line["y1"], line["x2"], line["y2"])
    return abs((p[0] - x1) * (y2 - y1) - (p[1] - y1) * (x2 - x1)) / math.hypot(x2 - x1, y2 - y1)


def _closed(profile):
    return (contours.open_gaps(profile) == [] and contours.chain_conflicts(profile) == []
            and contours.arc_radius_problems(profile) == [])


def test_a_square_corner_gets_the_exact_quarter_arc():
    out, problems = contours.resolve_fillets(
        [L(0, 0, .04, 0), L(.04, 0, .04, .04), F(.01), L(.04, .04, 0, .04), L(0, .04, 0, 0)])
    assert problems == []
    arc = out[2]
    assert (arc["kind"], arc["cx"], arc["cy"], arc["dir"]) == ("arc", .03, .03, 1)
    assert {(arc["x1"], arc["y1"]), (arc["x2"], arc["y2"])} == {(.04, .03), (.03, .04)}
    assert (out[1]["x2"], out[1]["y2"]) == (.04, .03) and (out[3]["x1"], out[3]["y1"]) == (.03, .04)
    assert _closed(out)


def test_an_acute_corner_is_tangent_to_both_lines():
    a, b = L(0, 0, .05, 0), L(.05, 0, 0, .03)  # about 31 degrees at (0.05, 0)
    out, problems = contours.resolve_fillets([L(0, .03, 0, 0), a, F(.004), b])
    assert problems == []
    c = (out[2]["cx"], out[2]["cy"])
    assert abs(_dist_to_line(c, a) - .004) < TOL and abs(_dist_to_line(c, b) - .004) < TOL
    assert _closed(out)


def test_a_line_meeting_a_hub_arc_is_tangent_to_both():
    # Hub R19 about the origin, reached clockwise from the top; the outline then runs down-left.
    hub = {"kind": "arc", "cx": 0, "cy": 0, "x1": 0, "y1": .019, "x2": .019, "y2": 0, "dir": -1}
    line = L(.019, 0, -.001, -.02)
    out, problems = contours.resolve_fillets([L(-.001, -.02, 0, .019), hub, F(.005), line])
    assert problems == []
    c = (out[2]["cx"], out[2]["cy"])
    assert abs(math.hypot(*c) - (.019 - .005)) < TOL       # inside the hub: rho - r
    assert abs(_dist_to_line(c, line) - .005) < TOL
    assert abs(math.hypot(out[1]["x2"], out[1]["y2"]) - .019) < TOL  # the hub end stays on R19
    assert _closed(out)


def test_two_arcs_meeting_get_a_blend_tangent_to_both():
    left = {"kind": "arc", "cx": -.01, "cy": 0, "x1": -.03, "y1": 0, "x2": 0, "y2": .01732050808, "dir": -1}
    right = {"kind": "arc", "cx": .01, "cy": 0, "x1": 0, "y1": .01732050808, "x2": .03, "y2": 0, "dir": -1}
    out, problems = contours.resolve_fillets([L(.03, 0, -.03, 0), left, F(.003), right])
    assert problems == []
    c = (out[2]["cx"], out[2]["cy"])
    for centre in ((-.01, 0), (.01, 0)):
        d = math.hypot(c[0] - centre[0], c[1] - centre[1])
        assert abs(d - .023) < 1e-8 or abs(d - .017) < 1e-8, d
    assert _closed(out)


def test_a_fillet_first_in_the_list_rounds_the_closing_corner():
    out, problems = contours.resolve_fillets([F(.005), L(0, 0, .04, 0), L(.04, 0, .04, .02), L(.04, .02, 0, 0)])
    assert problems == []
    assert (out[0]["kind"], out[1]["x1"] > 0) == ("arc", True)
    assert _closed(out)


def test_problems_are_named():
    square = [L(0, 0, .04, 0), L(.04, 0, .04, .04), L(.04, .04, 0, .04), L(0, .04, 0, 0)]
    too_big = square[:2] + [F(.05)] + square[2:]
    _, problems = contours.resolve_fillets(too_big)
    assert problems and "too large" in problems[0]
    apart = [L(0, 0, .04, 0), F(.005), L(.04, .01, .04, .04), L(.04, .04, 0, 0)]
    _, problems = contours.resolve_fillets(apart)
    assert problems and "share no endpoint" in problems[0]
    straight = [L(0, 0, .02, 0), F(.005), L(.02, 0, .04, 0), L(.04, 0, .04, .02), L(.04, .02, 0, 0)]
    _, problems = contours.resolve_fillets(straight)
    assert problems and "straight" in problems[0]
    doubled = square[:2] + [F(.005), F(.005)] + square[2:]
    _, problems = contours.resolve_fillets(doubled)
    assert problems and "one fillet per corner" in problems[0]
    circle = [{"kind": "circle", "diameter": .01}, F(.002), L(0, 0, .01, 0)]
    _, problems = contours.resolve_fillets(circle)
    assert problems and "line/arc" in problems[0]


def _graph(profile):
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
        {"id": "s", "type": "sketch", "ref": {"datum": "front"}, "profile": profile},
        {"id": "e", "type": "extrude", "sketch": "s", "operation": "boss", "depth": .01}]}


def test_validation_accepts_a_good_fillet_and_names_a_bad_one():
    good = [L(0, 0, .04, 0), L(.04, 0, .04, .04), F(.01), L(.04, .04, 0, .04), L(0, .04, 0, 0)]
    assert validate(_graph(good)) == []
    bad = [L(0, 0, .04, 0), L(.04, 0, .04, .04), F(.05), L(.04, .04, 0, .04), L(0, .04, 0, 0)]
    errors = validate(_graph(bad))
    assert len(errors) == 1 and "fillet R50 mm" in errors[0] and "too large" in errors[0], errors
    extra = [L(0, 0, .04, 0), L(.04, 0, .04, .04), {"kind": "fillet", "radius": .01, "cx": .03},
             L(.04, .04, 0, .04), L(0, .04, 0, 0)]
    assert any("takes only 'radius'" in e for e in validate(_graph(extra)))


def test_lowering_draws_the_fillet_as_an_arc_entity():
    good = [L(0, 0, .04, 0), L(.04, 0, .04, .04), F(.01), L(.04, .04, 0, .04), L(0, .04, 0, 0)]
    ops = lowering.lower_sketch(_graph(good)["nodes"][0])
    kinds = [op.params.get("entity_type") for op in ops if op.tool == "add_sketch_entity"]
    assert kinds == ["line", "line", "arc_center", "line", "line"]
    arc = next(op.params for op in ops if op.params.get("entity_type") == "arc_center")
    assert (arc["cx"], arc["cy"], arc["direction"]) == (.03, .03, 1)
