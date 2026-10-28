"""Off-centre hole placement (batch 10a) — pure-Python tests, no SolidWorks.

WHAT THIS PINS. A `hole` node used to sit at the referenced face's CENTRE, always: the compiler
resolved the top face and passed sketch (0, 0). Six real drawings need bolt-hole patterns and
offset holes, so `ref.position: 'point'` + `ref.at: [x,y,z]` (MODEL space, metres) was added.

The interesting part is NOT the dot product — it is that a face's SKETCH frame is not the model
frame. `create_sketch(on_face)` builds a sketch whose x/y axes SolidWorks chooses from the face's
plane, and the datum table (schema reference_model.datum_frame) says a 'front' datum sketch runs
+x=+X/+y=+Y while a 'right' datum sketch runs +x=-Z/+y=+Y. So a model point must be mapped
through the frame the sketch ACTUALLY has (create_sketch echoes it in result_geometry.frame),
never through an assumed one. These tests drive the real compiler with a fake port that reports
a chosen frame, then assert the circle lands on the requested MODEL point — which is the only
property that matters, and the one that silently breaks if the mapping is "fixed" to model axes.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_hole_placement.py`
"""
import json
import math
import os
import sys

import pytest

_COMPILER_ROOT = os.path.dirname(os.path.abspath(__file__))
if _COMPILER_ROOT not in sys.path:
    sys.path.insert(0, _COMPILER_ROOT)

from pycompiler import ir_schema, resolver  # noqa: E402
from pycompiler.compiler import compile_and_run  # noqa: E402


# ---------------------------------------------------------------------------
# Frames, as create_sketch actually echoes them (analyze_model's reader).
# ---------------------------------------------------------------------------
def _frame(origin, xdir, ydir):
    return {"origin": list(origin), "xdir": list(xdir), "ydir": list(ydir)}


# A 60x40 plate whose top face is Y=0.010 with its sketch origin at the plate CENTRE.
TOP_AT_ORIGIN = _frame((0.0, 0.010, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))
# The same top face, but the sketch origin is at a CORNER (SolidWorks may place it anywhere).
TOP_CORNER = _frame((-0.030, 0.010, 0.020), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))
# A 'front' face (normal +Z) at Z=0.020: sketch x=+X, y=+Y.
FRONT = _frame((0.0, 0.0, 0.020), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
# A 'right' face (normal +X) at X=0.030: sketch x=-Z, y=+Y  <-- x is NOT model X.
RIGHT = _frame((0.030, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))


class FakePort(object):
    """Minimal ExecutionPort: records calls, answers create_sketch with a chosen frame."""

    def __init__(self, frame, state_version=1, faces=None, bbox=None):
        self.frame = frame
        self.state_version = state_version
        self.calls = []
        self.faces = faces
        # The part's axis-aligned body bbox (min, max) in model metres — what analyze_model
        # (geometry) reports and what the D1 breakout check measures against. Default: the
        # 60x40x10 plate these tests model.
        self.bbox = bbox if bbox is not None else (
            [-0.030, 0.0, -0.020], [0.030, 0.010, 0.020])
        # The bore circles the thread resolver may find. They sit at `bore_centre` — the
        # caller sets it to the hole's REQUESTED model point, because that is where a real
        # build puts the mouth (the face centre only coincides for a centred hole).
        self.bore_centre = (0.0, 0.010, 0.0)
        self.bore_radius = 0.0025
        # How far the threadable MOUTH sits below the entry face: 0 for a plain hole, the
        # counterbore depth for a counterbored one (the pilot starts at the cb floor).
        self.bore_mouth_drop = 0.0

    @property
    def bore_edges(self):
        cx, cy, cz = self.bore_centre
        r = self.bore_radius
        mouth = cy - self.bore_mouth_drop
        return [
            {"i": 10, "kind": "circle", "center": [cx, mouth, cz],
             "normal": [0.0, 1.0, 0.0], "radius": r},
            {"i": 11, "kind": "circle", "center": [cx, mouth - 0.010, cz],
             "normal": [0.0, 1.0, 0.0], "radius": r},
        ]

    def execute(self, tool, params, state_version):
        self.calls.append((tool, dict(params)))
        if tool == "analyze_model":
            # The thread resolver reads the CIRCULAR edges of the finished bore. Model them as
            # two coaxial mouth/bottom circles at the hole centre so a threaded hole can plan;
            # `analysis_type` decides which array the reader returns.
            if (params or {}).get("analysis_type") == "edges":
                payload = {"edges": self.bore_edges}
            elif (params or {}).get("analysis_type") == "geometry":
                # geometry / mass_properties are a FLAT list of "key=value" strings, not JSON.
                lo, hi = self.bbox
                rows = ["bodies=1",
                        "bbox_min=[%r,%r,%r]" % tuple(lo),
                        "bbox_max=[%r,%r,%r]" % tuple(hi)]
                return {"status": "COMPLETED", "stateVersion": self.state_version,
                        "cadState": {"features": rows,
                                     "stateVersion": self.state_version}}
            else:
                payload = {"faces": self.faces or []}
            return {"status": "COMPLETED", "stateVersion": self.state_version,
                    "cadState": {"features": [json.dumps(payload)],
                                 "stateVersion": self.state_version}}
        if tool == "create_sketch":
            return {"status": "COMPLETED", "stateVersion": self.state_version + 1,
                    "cadState": {"features": ["Sketch1"],
                                 "activeSketch": "Sketch1",
                                 "stateVersion": self.state_version + 1},
                    "result_geometry": {"frame": self.frame}}
        return {"status": "COMPLETED", "stateVersion": self.state_version + 1,
                "cadState": {"features": ["F1"], "stateVersion": self.state_version + 1}}

    def get_state(self):
        return self.state_version


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------
def _plate_and_hole(at, diameter=0.0066, depth="through_all", extra=None):
    """box 60x40x10 -> hole on its top face at `at` (model space)."""
    hole = {"id": "h1", "type": "hole",
            "ref": {"node_face": {"node": "b1", "selector": "top"},
                    "position": "point", "at": list(at)},
            "diameter": diameter, "depth": depth}
    if extra:
        hole.update(extra)
    return {"units": "meters",
            "nodes": [
                {"id": "b1", "type": "box", "ref": {"datum": "top"},
                 "width": 0.060, "depth": 0.040, "height": 0.010},
                hole,
            ]}


def _top_face(area=0.0024, point=(0.0, 0.010, 0.0), normal=(0.0, 1.0, 0.0), index=5):
    return [{"i": index, "planar": True, "area": area,
             "point": list(point), "normal": list(normal)}]


def _run(graph, frame, faces=None):
    port = FakePort(frame, faces=faces if faces is not None else _top_face())
    return port, compile_and_run(port, graph)


def _circle_ops(port):
    return [p for (t, p) in port.calls
            if t == "add_sketch_entity" and p.get("entity_type") == "circle"]


def _model_point_from_uv(frame, u, v):
    """The inverse map, so the test states its expectation in MODEL space."""
    o, x, y = frame["origin"], frame["xdir"], frame["ydir"]
    return tuple(o[i] + u * x[i] + v * y[i] for i in range(3))


# ---------------------------------------------------------------------------
# 1. The mapping itself, on three differently-oriented faces.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("frame,at,face", [
    # top face, sketch origin at the plate centre, sketch x=+X y=-Z
    (TOP_AT_ORIGIN, (0.022, 0.010, -0.012), _top_face()),
    # top face, sketch origin at a CORNER (origin shift must be handled)
    (TOP_CORNER, (0.022, 0.010, -0.012), _top_face()),
])
def test_off_centre_hole_lands_on_the_requested_model_point(frame, at, face):
    # A 'top' selector only ever resolves a +Y face, so the front/right cases address the face
    # directly; the mapping under test is identical either way.
    if face[0]["normal"] == [0.0, 1.0, 0.0]:
        graph = _plate_and_hole(at)
    else:
        graph = {"units": "meters", "nodes": [
            {"id": "b1", "type": "box", "ref": {"datum": "top"},
             "width": 0.060, "depth": 0.040, "height": 0.010},
            {"id": "h1", "type": "hole",
             "ref": {"node_face": {"node": "b1", "selector": "top"},
                     "position": "point", "at": list(at)},
             "diameter": 0.0066, "depth": "through_all"},
        ]}
    port, result = _run(graph, frame, faces=face)
    assert result.status == "COMPLETED", result.to_dict()
    circles = _circle_ops(port)
    assert len(circles) == 1, circles
    got = _model_point_from_uv(frame, circles[0]["cx"], circles[0]["cy"])
    for axis, (a, b) in enumerate(zip(got, at)):
        assert abs(a - b) < 1e-9, (axis, got, at, frame)


def test_right_face_sketch_x_is_not_model_x():
    # The regression this whole design exists for: if the compiler passed the model point
    # straight through as sketch (cx, cy), a right-face hole would land at (X, Y) on a sketch
    # whose x runs -Z — a mirrored/rotated placement. Assert the mapped uv is NOT (x, y).
    #
    # This is a UNIT test of the mapping: a hole node can only name a face through the
    #  selector today, so a right/front face cannot be driven through a graph. The
    # mapping itself is face-agnostic — it is the measured frame that decides — so the frame
    # of a right face is what the assertion feeds.
    at = (0.030, 0.012, -0.022)
    u, v, off = resolver.project_point_into_frame(RIGHT, at)
    assert (u, v) != (at[0], at[1]), "the model point must NOT pass through as sketch (cx, cy)"
    assert abs(off) < 1e-9
    # right face: sketch x = -Z, so u = -(z - 0) = +0.022, v = y = 0.012
    assert abs(u - 0.022) < 1e-9, u
    assert abs(v - 0.012) < 1e-9, v
    got = _model_point_from_uv(RIGHT, u, v)
    for a, b in zip(got, at):
        assert abs(a - b) < 1e-9


def test_centre_placement_is_unchanged_and_makes_no_extra_read():
    # The default (no `at`) must keep passing sketch (0, 0) and must not gain an analyze call.
    graph = _plate_and_hole((0.0, 0.010, 0.0))
    graph["nodes"][1]["ref"]["position"] = "center"
    graph["nodes"][1]["ref"].pop("at")
    port, result = _run(graph, TOP_AT_ORIGIN)
    assert result.status == "COMPLETED", result.to_dict()
    circle = _circle_ops(port)[0]
    assert (circle["cx"], circle["cy"]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# 2. Plan-time rejection: zero execution calls.
# ---------------------------------------------------------------------------
def test_point_off_the_face_is_rejected_with_zero_execution_calls():
    # Y=0.020 is 10 mm above the top face at Y=0.010.
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    result = compile_and_run(port, _plate_and_hole((0.022, 0.020, -0.012)))
    assert result.status == "FAILED", result.to_dict()
    assert result.error["code"] == "PLACEMENT_OFF_FACE", result.error
    assert "ON the face" in result.error["message"]
    # The box node before the hole already executed; what matters is that the REJECTED hole
    # issued no write of its own (plan-time checks need only the read-only face dump).
    hole_writes = [t for (t, _p) in port.calls
                   if t not in ("analyze_model", "open_new_part", "create_sketch",
                                "add_sketch_entity", "extrude_feature", "open_new_assembly")]
    assert hole_writes == [], hole_writes


def test_hole_breaking_out_of_the_face_is_rejected():
    # D1: the breakout test measures the CIRCLE against the part's real extent on the face plane
    # (body bbox), so WHERE the hole is matters. This plate is 60x40 mm; a d6.6 hole at x=+29.5
    # has its edge at 29.5+3.3 = 32.8 mm, past the 30 mm half-width.
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    result = compile_and_run(port, _plate_and_hole((0.0295, 0.010, 0.0)))
    assert result.status == "FAILED", result.to_dict()
    assert result.error["code"] == "PLACEMENT_BREAKS_OUT", result.error
    # The rejection is a plan-time guard for THIS node: the box before it has already run
    # (a hole can only be planned once its face exists), so the hole issues no call of its own.
    assert result.error["node_id"] == "h1", result.error
    assert not [t for (t, _p) in port.calls
                if t == "create_sketch" and _p.get("on_face")], port.calls


def test_corner_holes_at_22mm_still_pass_on_a_60x40_face():
    # The regression the equal-area square got backwards: half of sqrt(area)/2 is 24.5 mm for a
    # 60x40 face, which would reject these LEGAL corner holes (22 + 3.3 = 25.3 mm from centre).
    # The real extent is 30 x 20 mm, so all four pass.
    for x in (-0.022, 0.022):
        for z in (-0.012, 0.012):
            port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
            result = compile_and_run(port, _plate_and_hole((x, 0.010, z)))
            assert result.status == "COMPLETED", (x, z, result.to_dict())


def test_centred_hole_overlapping_an_off_centre_hole_is_rejected():
    # D2: a CENTRED hole (sketch origin) must join the overlap test too. The box is 60x40 mm, so
    # the sketch origin is the plate centre; an off-centre d10 hole 4 mm away overlaps it.
    graph = {"units": "meters", "nodes": [
        {"id": "b1", "type": "box", "ref": {"datum": "top"},
         "width": 0.060, "depth": 0.040, "height": 0.010},
        {"id": "h1", "type": "hole",
         "ref": {"node_face": {"node": "b1", "selector": "top"}, "position": "center"},
         "diameter": 0.010, "depth": "through_all"},
        {"id": "h2", "type": "hole",
         "ref": {"node_face": {"node": "b1", "selector": "top"},
                 "position": "point", "at": [0.004, 0.010, 0.0]},
         "diameter": 0.010, "depth": "through_all"},
    ]}
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    result = compile_and_run(port, graph)
    assert result.status == "FAILED", result.to_dict()
    assert result.error["code"] == "PLACEMENT_OVERLAP", result.error
    assert "h1" in result.error["message"] and "h2" in result.error["message"]


def test_two_overlapping_holes_on_the_same_face_are_rejected():
    graph = {"units": "meters", "nodes": [
        {"id": "b1", "type": "box", "ref": {"datum": "top"},
         "width": 0.060, "depth": 0.040, "height": 0.010},
        {"id": "h1", "type": "hole",
         "ref": {"node_face": {"node": "b1", "selector": "top"},
                 "position": "point", "at": [0.0, 0.010, 0.0]},
         "diameter": 0.010, "depth": "through_all"},
        {"id": "h2", "type": "hole",
         "ref": {"node_face": {"node": "b1", "selector": "top"},
                 "position": "point", "at": [0.004, 0.010, 0.0]},
         "diameter": 0.010, "depth": "through_all"},
    ]}
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    result = compile_and_run(port, graph)
    assert result.status == "FAILED", result.to_dict()
    assert result.error["code"] == "PLACEMENT_OVERLAP", result.error


def test_two_holes_far_apart_on_the_same_face_are_fine():
    graph = {"units": "meters", "nodes": [
        {"id": "b1", "type": "box", "ref": {"datum": "top"},
         "width": 0.060, "depth": 0.040, "height": 0.010},
        {"id": "h1", "type": "hole",
         "ref": {"node_face": {"node": "b1", "selector": "top"},
                 "position": "point", "at": [0.022, 0.010, 0.012]},
         "diameter": 0.0066, "depth": "through_all"},
        {"id": "h2", "type": "hole",
         "ref": {"node_face": {"node": "b1", "selector": "top"},
                 "position": "point", "at": [-0.022, 0.010, -0.012]},
         "diameter": 0.0066, "depth": "through_all"},
    ]}
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    result = compile_and_run(port, graph)
    assert result.status == "COMPLETED", result.to_dict()
    assert len(_circle_ops(port)) == 2


# ---------------------------------------------------------------------------
# 3. Structural validation (ir_schema) — before any resolve, hence zero calls.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ref,needle", [
    ({"position": "point"}, "ref.at must be a 3-D point"),
    ({"position": "point", "at": [0.0, 0.010]}, "ref.at must be a 3-D point"),
    ({"position": "point", "at": [0.0, "x", 0.0]}, "ref.at must be a 3-D point"),
    ({"position": "center", "at": [0.0, 0.010, 0.0]}, "only read when ref.position is 'point'"),
    ({"position": "corner"}, "ref.position"),
])
def test_structural_validation_rejects_bad_placement(ref, needle):
    base = {"node_face": {"node": "b1", "selector": "top"}}
    base.update(ref)
    graph = {"units": "meters", "nodes": [
        {"id": "b1", "type": "box", "ref": {"datum": "top"},
         "width": 0.060, "depth": 0.040, "height": 0.010},
        {"id": "h1", "type": "hole", "ref": base,
         "diameter": 0.0066, "depth": "through_all"},
    ]}
    errors = ir_schema.validate(graph)
    assert any(needle in e for e in errors), errors
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    result = compile_and_run(port, graph)
    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION_FAILED"
    assert result.exec_calls == 0
    assert port.calls == []


# ---------------------------------------------------------------------------
# 4. The feature keeps working WITH thread / counterbore / blind depth.
# ---------------------------------------------------------------------------
def test_off_centre_counterbored_threaded_hole_maps_once_and_keeps_thread():
    at = (0.022, 0.010, -0.012)
    graph = _plate_and_hole(at, diameter=0.005, depth=0.014, extra={
        "counterbore": {"diameter": 0.011, "depth": 0.0065},
        "thread": {"designation": "M6", "class": "6H", "depth": 0.014},
    })
    port = FakePort(TOP_AT_ORIGIN, faces=_top_face())
    port.bore_centre = at
    port.bore_radius = 0.0025
    port.bore_mouth_drop = 0.0065   # the counterbore depth: the pilot's mouth is its floor
    result = compile_and_run(port, graph)
    assert result.status == "COMPLETED", result.to_dict()
    circles = _circle_ops(port)
    # counterbore circle + pilot circle, BOTH at the same mapped centre
    assert len(circles) == 2, circles
    centres = {(round(c["cx"], 9), round(c["cy"], 9)) for c in circles}
    assert len(centres) == 1, centres
    cx, cy = next(iter(centres))
    got = _model_point_from_uv(TOP_AT_ORIGIN, cx, cy)
    for a, b in zip(got, at):
        assert abs(a - b) < 1e-9
    # the counterbore is cut BEFORE the pilot, and the thread comes last
    tools = [t for (t, _p) in port.calls]
    assert tools.index("extrude_feature") < len(tools) - 1
    thread_calls = [p for (t, p) in port.calls if t == "add_cosmetic_thread"]
    assert len(thread_calls) == 1, thread_calls
    assert thread_calls[0]["diameter"] == pytest.approx(0.006)  # NOMINAL, not the 5.0 pilot
    assert "M6-6H" in thread_calls[0]["note"]


def test_off_centre_blind_hole_uses_the_requested_depth():
    at = (0.022, 0.010, -0.012)
    port, result = _run(_plate_and_hole(at, depth=0.004), TOP_AT_ORIGIN)
    assert result.status == "COMPLETED", result.to_dict()
    # the box's own boss is the FIRST cut; the hole's blind cut is the one with a `depth` that
    # is not the plate thickness, so select it by the through/depth shape the hole emits.
    cuts = [p for (t, p) in port.calls
            if t == "extrude_feature" and p.get("feature_type") == "cut"]
    assert cuts, [t for (t, _p) in port.calls]
    assert cuts[-1]["depth"] == pytest.approx(0.004)

# ---------------------------------------------------------------------------
# NOTE: the counterbore thread anchor follows the ENTRY FACE's normal
# ---------------------------------------------------------------------------
def test_counterbore_thread_anchor_uses_the_face_normal_not_hardcoded_up():
    """A counterbored threaded hole pushes its mouth anchor INTO the material along -normal.
    The anchor used to hardcode +Y, which is only correct for a top face; on a front face
    (normal +Z) the anchor would have been offset along the wrong axis entirely, naming a
    circle that is not the pilot's mouth. Derive it from the resolved face instead."""
    from pycompiler import compiler as _c
    top = {"face": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.010, 0.0]}}
    front = {"face": {"normal": [0.0, 0.0, 1.0], "point": [0.0, 0.0, 0.020]}}
    node = {"type": "hole", "diameter": 0.005, "depth": 0.018,
            "ref": {"node_face": {"node": "b1", "selector": "top"}, "position": "center"},
            "counterbore": {"diameter": 0.011, "depth": 0.0065}}

    a_top = _c._thread_anchor(node, top)["edge_anchor"]["near"]
    assert a_top[1] == pytest.approx(0.010 - 0.0065)   # pushed down along -Y
    assert a_top[2] == pytest.approx(0.0)

    a_front = _c._thread_anchor(node, front)["edge_anchor"]["near"]
    assert a_front[1] == pytest.approx(0.0), a_front       # NOT pushed along Y
    assert a_front[2] == pytest.approx(0.020 - 0.0065), a_front  # pushed along -Z

    # No usable normal in the payload -> the historical +Y fallback still applies.
    a_fallback = _c._thread_anchor(node, {"face": {"point": [0.0, 0.010, 0.0]}})["edge_anchor"]["near"]
    assert a_fallback[1] == pytest.approx(0.010 - 0.0065)
