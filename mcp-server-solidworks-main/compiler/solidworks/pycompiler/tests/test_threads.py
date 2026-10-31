"""Thread compiler tests (v0.9.0) — plan-time rules, lowering, and the mouth-edge resolver.

NO live SolidWorks: a FakePort serves canned analyze_model payloads for faces/edges/bodies and
records every call. What these tests exist to pin down, in the order the bugs actually bit:

  1. F1  `resolve_thread_edge` must be IMPORTED in compiler.py (a bare call raised
         NODE_UNEXPECTED "name not defined" on every threaded part).
  2. F2  A `hole`'s anchor is an EDGE anchor naming the mouth circle's CENTRE. A bore_face
         anchor there is one radius off the cylinder and can never resolve.
  3. F3/F4  A counterbored hole's pilot mouth is the circle on the COUNTERBORE FLOOR, not the
         entry face and not the pilot bottom. The design's M6 geometry decides the numbers:
         entry y=0.03, counterbore 6.5 mm deep, pilot 18 mm -> mouth y=0.0235, bottom y=0.0055.
  4. F5  Concentric coplanar circles (bore mouth vs an outer rim) are separated by RADIUS; a
         THROUGH bore's two ends never raise ambiguity.
  5. The plan-time rules of design section 6: PILOT_MISMATCH with the expected value, pilot <
         nominal, thread depth <= pilot depth, out-of-table sizes need pitch AND tap_drill.
  6. Lowering: counterbore cut FIRST, nominal (never pilot) diameter, end_type 2 for through,
         ASCII callout, exactly ONE add_cosmetic_thread per thread.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _COMPILER_ROOT not in sys.path:
    sys.path.insert(0, _COMPILER_ROOT)

import pytest  # noqa: E402

from pycompiler import ir_schema, lowering, resolver, thread_table  # noqa: E402
from pycompiler.compiler import _thread_anchor, compile_and_run  # noqa: E402
from pycompiler.errors import FeatureError  # noqa: E402
from pycompiler.execution_port import ExecutionPort  # noqa: E402

# A 60x60x30 mm block whose TOP face is y=0.03 (the +Y face resolve_top_face picks).
_FACES = json.dumps({
    "face_count": 6,
    "faces": [
        {"i": 0, "planar": True, "area": 0.0036, "normal": [0, 1, 0], "point": [0, 0.03, 0]},
        {"i": 1, "planar": True, "area": 0.0036, "normal": [0, -1, 0], "point": [0, 0.0, 0]},
        {"i": 2, "planar": True, "area": 0.0018, "normal": [1, 0, 0], "point": [0.03, 0.015, 0]},
        {"i": 3, "planar": True, "area": 0.0018, "normal": [-1, 0, 0], "point": [-0.03, 0.015, 0]},
        {"i": 4, "planar": True, "area": 0.0018, "normal": [0, 0, 1], "point": [0, 0.015, 0.03]},
        {"i": 5, "planar": True, "area": 0.0018, "normal": [0, 0, -1], "point": [0, 0.015, -0.03]},
    ],
})


def _circle(i, y, radius, cx=0.0, cz=0.0):
    """A closed circular edge as the reader reports it: kind/center/normal/radius (+ start/end)."""
    return {"i": i, "kind": "circle",
            "center": [cx, y, cz], "normal": [0, 1, 0], "radius": radius,
            "start": [cx + radius, y, cz], "end": [cx + radius, y, cz],
            "mid": [cx - radius, y, cz]}


def _edges(*circles):
    return json.dumps({"edge_count": len(circles), "edges": list(circles)})


def _bodies(y):
    """One solid body whose centroid sits at (0, y, 0) — the 'inside the material' reference."""
    return json.dumps({"body_count": 1,
                       "bodies": [{"i": 0, "face_start": 0, "face_count": 6,
                                   "centroid": [0.0, y, 0.0], "volume": 0.000108,
                                   "area": 0.0108, "edges": 12, "vertices": 8}]})


class FakePort(ExecutionPort):
    """Records calls; serves canned faces/edges/bodies; bumps state_version on writes."""

    def __init__(self, edges=None, centroid_y=0.015, faces=_FACES):
        self._sv = 0
        self._faces = faces
        self._edges = edges if edges is not None else _edges()
        self._centroid_y = centroid_y
        self.calls = []

    def get_state(self):
        return self._sv

    def execute(self, tool, params, state_version):
        self.calls.append((tool, dict(params)))
        if tool == "analyze_model":
            kind = params.get("analysis_type")
            if kind == "faces":
                payload = self._faces
            elif kind == "edges":
                payload = self._edges
            elif kind == "bodies":
                payload = _bodies(self._centroid_y)
            else:
                payload = None
            return {"status": "COMPLETED", "stateVersion": self._sv,
                    "cadState": {"features": [payload] if payload else []}}
        self._sv += 1
        feats = ["Cut-Extrude1"] if tool == "extrude_feature" else (
            ["Boss1"] if tool == "box_feature" else
            (["CosmeticThread_M5x0.8"] if tool == "add_cosmetic_thread" else ["Feature1"]))
        response = {"status": "COMPLETED", "stateVersion": self._sv,
                    "cadState": {"features": feats, "activeSketch": "Sketch1"}}
        if tool == "create_sketch":
            from pycompiler.ir_schema import canonical_sketch_frame
            response["result_geometry"] = {"frame": canonical_sketch_frame({
                "datum": params.get("plane", "Top Plane").split()[0].lower()})}
        return response


def _box():
    return {"id": "n1", "type": "box", "ref": {"datum": "top"},
            "width": 0.06, "depth": 0.06, "height": 0.03}


@pytest.fixture(autouse=True)
def _fresh_analyze_memo():
    """Scope the resolver's per-run analyze memo to each test.

    compile_and_run opens/closes this scope itself, but the tests that call
    resolver.resolve_thread_edge DIRECTLY do not — and the memo is keyed by
    (tool, state_version, analysis_type, ...) with every fake port sitting at
    state_version 0. Without this, the first such test's canned edges are served to the
    next one and a passing test fails only in a full run (order-dependent, the worst kind).
    """
    resolver.begin_run()
    try:
        yield
    finally:
        resolver.end_run()


def _graph(nodes, **extra):
    g = {"units": "meters", "nodes": nodes}
    g.update(extra)
    return g


def _thread_ops(port):
    return [p for (t, p) in port.calls if t == "add_cosmetic_thread"]


# ---------------------------------------------------------------------------------------------
# 1. F1 — the resolver is imported and reachable from the compiler
# ---------------------------------------------------------------------------------------------

def test_resolve_thread_edge_is_imported_by_the_compiler():
    # A bare call raised NODE_UNEXPECTED "name 'resolve_thread_edge' is not defined" on every
    # threaded part — the import list at the top of compiler.py is the fix.
    import pycompiler.compiler as compiler_module
    assert hasattr(compiler_module, "resolve_thread_edge")
    assert compiler_module.resolve_thread_edge is resolver.resolve_thread_edge


# ---------------------------------------------------------------------------------------------
# 2. F2/F3 — the anchor names the MOUTH circle, past the counterbore floor
# ---------------------------------------------------------------------------------------------

def test_hole_thread_anchor_is_an_edge_anchor_at_the_mouth_centre():
    anchor = _thread_anchor({"diameter": 0.005}, {"point": [0.0, 0.03, 0.0]})
    # An EDGE anchor, not bore_face: the entry-face point is on the AXIS, one radius off the
    # cylinder, so the bore_face branch could never match it.
    assert "edge_anchor" in anchor, anchor
    assert "bore_face" not in anchor, anchor
    assert anchor["edge_anchor"]["near"] == [0.0, 0.03, 0.0]
    # The radius travels with it so a concentric outer rim cannot be mistaken for the mouth.
    assert anchor["edge_anchor"]["radius"] == pytest.approx(0.0025)


def test_counterbore_thread_anchor_moves_to_the_counterbore_floor():
    # Design M6 counterbore: entry y=0.03, counterbore 6.5 mm deep -> the pilot starts at 0.0235.
    anchor = _thread_anchor(
        {"diameter": 0.005, "counterbore": {"diameter": 0.011, "depth": 0.0065}},
        {"point": [0.0, 0.03, 0.0]})
    assert anchor["edge_anchor"]["near"][1] == pytest.approx(0.0235), anchor


def test_counterbore_mouth_is_chosen_not_the_pilot_bottom():
    # The exact geometry the design's M6 counterbored hole produces: the pilot mouth at
    # y=0.0235 and its bottom at y=0.0055, plus the counterbore-floor rim at y=0.0235 (r=5.5)
    # and the counterbore mouth at y=0.03 (r=5.5).
    edges = _edges(
        _circle(0, 0.03, 0.0055),     # counterbore mouth, outer
        _circle(1, 0.0235, 0.0055),   # counterbore floor rim
        _circle(2, 0.0235, 0.0025),   # PILOT MOUTH  <-- the one a thread attaches to
        _circle(3, 0.0055, 0.0025),   # pilot bottom
    )
    port = FakePort(edges=edges, centroid_y=0.015)
    idx, info = resolver.resolve_thread_edge(
        port, 0, {"edge_anchor": {"near": [0.0, 0.0235, 0.0], "radius": 0.0025}})
    assert idx == 2, (idx, info)
    assert info["edge_center"][1] == pytest.approx(0.0235)


def test_axial_rank_is_unsigned_so_the_caller_picks_the_direction():
    # F4: reading a direction into the rank inverted for a counterbored bore (its bottom ranked
    # further from the centroid than its mouth). The rank is a plain distance.
    assert resolver._axial_rank([0, 0.03, 0], [0, 1, 0], [0, 0.015, 0]) == pytest.approx(0.015)
    assert resolver._axial_rank([0, 0.005, 0], [0, -1, 0], [0, 0.015, 0]) == pytest.approx(0.010)


# ---------------------------------------------------------------------------------------------
# 3. F5 — radius disambiguation and the through-bore tie
# ---------------------------------------------------------------------------------------------

def test_concentric_rim_is_separated_by_radius():
    # A revolve end face carries the bore mouth AND an outer rim, concentric and coplanar.
    edges = _edges(_circle(0, 0.03, 0.021), _circle(1, 0.03, 0.03))
    port = FakePort(edges=edges)
    idx, _info = resolver.resolve_thread_edge(
        port, 0, {"edge_anchor": {"near": [0.0, 0.03, 0.0], "radius": 0.021}})
    assert idx == 0


def test_through_bore_takes_the_entry_end_and_never_raises():
    # Both ends are equidistant from the centroid (that IS what "through" means), so the
    # interior rule ties by construction; the entry-side rule must decide instead of raising.
    edges = _edges(_circle(0, 0.03, 0.021), _circle(1, 0.0, 0.021))
    port = FakePort(edges=edges, centroid_y=0.015)
    idx, info = resolver.resolve_thread_edge(
        port, 0, {"edge_anchor": {"near": [0.0, 0.03, 0.0], "radius": 0.021}})
    assert idx == 0, (idx, info)
    assert info["edge_center"][1] == pytest.approx(0.03)


def test_through_bore_via_bore_face_also_picks_the_entry_end():
    faces = json.dumps({
        "face_count": 7,
        "faces": [
            {"i": 0, "planar": True, "normal": [0, 1, 0], "point": [0, 0.03, 0]},
            {"i": 1, "planar": True, "normal": [0, -1, 0], "point": [0, 0.0, 0]},
            {"i": 2, "kind": "cylinder", "origin": [0, 0.0, 0], "axis": [0, 1, 0],
             "radius": 0.021, "point": [0.021, 0.015, 0]},
        ],
    })
    edges = _edges(_circle(0, 0.03, 0.021), _circle(1, 0.0, 0.021))
    port = FakePort(edges=edges, faces=faces, centroid_y=0.015)
    idx, _info = resolver.resolve_thread_edge(
        port, 0, {"bore_face": {"near": [0.021, 0.015, 0.0], "radius": 0.021}})
    assert idx == 0


# ---------------------------------------------------------------------------------------------
# 4. Plan-time validation (design section 6)
# ---------------------------------------------------------------------------------------------

def _threaded_hole(**thread):
    t = {"designation": "M5", "class": "8H", "depth": 0.010}
    t.update(thread)
    return _graph([_box(),
                   {"id": "n2", "type": "hole",
                    "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
                    "diameter": 0.0042, "depth": 0.0124, "thread": t}])


def test_m5_8h_design_graph_validates():
    assert ir_schema.validate(_threaded_hole()) == []


def test_pilot_mismatch_names_the_expected_drill():
    errors = ir_schema.validate(_graph([
        _box(),
        {"id": "n2", "type": "hole",
         "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
         "diameter": 0.005, "depth": 0.02,
         "thread": {"designation": "M5", "depth": 0.01}}]))
    text = " ".join(errors)
    assert "PILOT_MISMATCH" in text
    assert "4.2" in text, text          # the expected value, in mm


def test_pilot_must_be_smaller_than_nominal():
    errors = ir_schema.validate(_graph([
        _box(),
        {"id": "n2", "type": "hole",
         "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
         "diameter": 0.0055, "depth": 0.02,
         "thread": {"designation": "M5", "depth": 0.01, "tap_drill": 0.0055}}]))
    assert any("not smaller than" in e for e in errors), errors


def test_thread_depth_may_not_exceed_the_pilot():
    errors = ir_schema.validate(_threaded_hole(depth=0.02))
    assert any("exceeds the pilot depth" in e for e in errors), errors


def test_thread_depth_equal_to_the_pilot_is_allowed():
    assert ir_schema.validate(_threaded_hole(depth=0.0124)) == []


def test_out_of_table_size_needs_pitch_and_tap_drill():
    errors = ir_schema.validate(_threaded_hole(designation="M7x1"))
    assert any("tap_drill" in e for e in errors), errors
    ok = ir_schema.validate(_threaded_hole(designation="M7x1", tap_drill=0.0042, depth=0.01))
    assert ok == [], ok


def test_thread_class_vocabulary_accepts_8H_and_rejects_9H():
    assert ir_schema.validate(_threaded_hole(**{"class": "8H"})) == []
    errors = ir_schema.validate(_threaded_hole(**{"class": "9H"}))
    assert any("tolerance class" in e for e in errors), errors


def test_lowercase_class_is_rejected_as_an_external_thread_class():
    # ISO 965-1 writes INTERNAL classes uppercase (6H) and EXTERNAL ones lowercase (6g, 6h).
    # The validator used to upper-case first, so `6h` was silently accepted as the internal
    # `6H` and the callout claimed a tolerance on a thread v1 cannot even build.
    for lower, upper in (("6h", "6H"), ("6g", "6G")):
        errors = ir_schema.validate(_threaded_hole(**{"class": lower}))
        assert len(errors) == 1, errors
        msg = errors[0]
        assert "EXTERNAL" in msg, msg
        assert "INTERNAL threads only" in msg, msg
        assert repr(upper) in msg, msg        # tells the author what to write instead
        assert "not an ISO 965-1 internal" not in msg, msg

    # The uppercase spelling of the SAME grade stays valid (6H internal, 6G internal).
    for upper in ("6H", "6G"):
        assert ir_schema.validate(_threaded_hole(**{"class": upper})) == [], upper


def test_lowercase_class_is_rejected_on_a_cosmetic_thread_node():
    g = {"units": "meters", "nodes": [
        {"id": "n1", "type": "box", "ref": {"datum": "top"},
         "width": 0.05, "depth": 0.05, "height": 0.02},
        {"id": "n2", "type": "hole",
         "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
         "diameter": 0.005, "depth": "through_all"},
        {"id": "n3", "type": "cosmetic_thread",
         "ref": {"node": "n2", "edge_anchor": {"near": [0.0, 0.02, 0.0]}},
         "designation": "M6", "depth": "through_all", "class": "6h"}]}
    errors = ir_schema.validate(g)
    assert any("EXTERNAL" in e for e in errors), errors
    g["nodes"][2]["class"] = "6H"
    assert ir_schema.validate(g) == []


def test_lowercase_class_is_rejected_in_thread_defaults():
    g = _threaded_hole()
    g["thread_defaults"] = {"class": "6h"}
    errors = ir_schema.validate(g)
    assert any("EXTERNAL" in e and "thread_defaults" in e for e in errors), errors
    g["thread_defaults"] = {"class": "6H"}
    assert ir_schema.validate(g) == []


def test_reserved_representations_are_rejected_explicitly():
    for value in ("real", "auto"):
        errors = ir_schema.validate(_threaded_hole(representation=value))
        assert any("RESERVED" in e for e in errors), (value, errors)


def test_counterbore_diameter_must_exceed_the_hole():
    g = _graph([_box(),
                {"id": "n2", "type": "hole",
                 "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
                 "diameter": 0.005, "depth": 0.018,
                 "counterbore": {"diameter": 0.004, "depth": 0.0065}}])
    assert any("must be LARGER" in e for e in ir_schema.validate(g))


def test_cosmetic_thread_requires_exactly_one_anchor():
    base = {"id": "n3", "type": "cosmetic_thread", "designation": "M42x2",
            "depth": "through_all", "ref": {"node": "n2"}}
    errors = ir_schema.validate(_graph([_box(), _revolved_bore_sketch(), _revolved_bore(), base]))
    assert any("exactly ONE of ref.edge_anchor" in e for e in errors), errors

    both = dict(base)
    both["ref"] = {"node": "n2",
                   "edge_anchor": {"near": [0, 0.03, 0]},
                   "bore_face": {"near": [0.021, 0.015, 0]}}
    errors = ir_schema.validate(_graph([_box(), _revolved_bore_sketch(), _revolved_bore(), both]))
    assert any("mutually exclusive" in e for e in errors), errors


def test_cosmetic_thread_on_a_non_bore_producer_is_rejected():
    errors = ir_schema.validate(_graph([
        _box(),
        {"id": "n3", "type": "cosmetic_thread", "designation": "M6",
         "depth": "through_all", "ref": {"node": "n1", "edge_anchor": {"near": [0, 0.03, 0]}}}]))
    assert any("threadable" in e or "must be an earlier" in e for e in errors), errors


def _revolved_bore_sketch():
    """The half-section profile a revolve cut consumes — a closed rectangle, construction axis."""
    return {"id": "s1", "type": "sketch", "ref": {"datum": "front"}, "profile": [
        {"kind": "line", "x1": 0.021, "y1": 0.0, "x2": 0.021, "y2": 0.03},
        {"kind": "line", "x1": 0.021, "y1": 0.03, "x2": 0.03, "y2": 0.03},
        {"kind": "line", "x1": 0.03, "y1": 0.03, "x2": 0.03, "y2": 0.0},
        {"kind": "line", "x1": 0.03, "y1": 0.0, "x2": 0.021, "y2": 0.0},
        {"kind": "line", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.03, "construction": True},
    ]}


def _revolved_bore():
    """A revolve CUT that leaves a through bore — the valve-body M42x2 shape, structurally."""
    return {"id": "n2", "type": "revolve",
            "sketch": "s1", "axis": {"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 1.0},
            "operation": "cut"}


def test_graph_thread_defaults_are_validated():
    g = _threaded_hole()
    g["thread_defaults"] = {"standard": "ISO", "class": "6H", "callout_style": "ascii"}
    assert ir_schema.validate(g) == []
    g["thread_defaults"] = {"callout_style": "klingon"}
    assert any("callout_style" in e for e in ir_schema.validate(g))
    g["thread_defaults"] = {"nonsense": 1}
    assert any("not a thread default" in e for e in ir_schema.validate(g))


# ---------------------------------------------------------------------------------------------
# 5. Lowering
# ---------------------------------------------------------------------------------------------

def test_hole_with_thread_lowers_to_cut_then_one_thread():
    port = FakePort(edges=_edges(_circle(0, 0.03, 0.0021), _circle(1, 0.0176, 0.0021)))
    r = compile_and_run(port, _threaded_hole())
    assert r.status == "COMPLETED", r.to_dict()
    tools = [t for (t, _p) in port.calls if t != "analyze_model"]
    # The hole contributes sketch + circle + cut, then EXACTLY ONE thread op. (The box's own
    # sketch/circle/box_feature precede all of it.)
    assert tools[-4:] == ["create_sketch", "add_sketch_entity", "extrude_feature",
                          "add_cosmetic_thread"], tools
    assert tools.count("add_cosmetic_thread") == 1, tools
    assert len(_thread_ops(port)) == 1        # ADDITIVE tool: never emit it twice


def test_thread_passes_nominal_major_not_the_pilot():
    port = FakePort(edges=_edges(_circle(0, 0.03, 0.0021), _circle(1, 0.0176, 0.0021)))
    compile_and_run(port, _threaded_hole())
    params = _thread_ops(port)[0]
    assert params["diameter"] == pytest.approx(0.005), params   # M5 nominal, NOT 0.0042


def test_thread_callout_is_ascii_and_class_qualified():
    port = FakePort(edges=_edges(_circle(0, 0.03, 0.0021), _circle(1, 0.0176, 0.0021)))
    compile_and_run(port, _threaded_hole())
    params = _thread_ops(port)[0]
    assert params["note"] == "M5-8H DEPTH 10 / DIA 4.2 DEPTH 12.4", params["note"]
    assert params["size"] == "M5x0.8"
    assert params["standard"] == 8 and params["standard_type"] == "Tapped Hole"
    assert params["end_type"] == 0 and params["depth"] == pytest.approx(0.010)


def test_through_thread_uses_end_type_2_and_omits_depth():
    g = _graph([_box(),
                {"id": "n2", "type": "hole",
                 "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
                 "diameter": 0.0042, "depth": "through_all",
                 "thread": {"designation": "M5", "depth": "through_all"}}])
    port = FakePort(edges=_edges(_circle(0, 0.03, 0.0021), _circle(1, 0.0, 0.0021)))
    r = compile_and_run(port, g)
    assert r.status == "COMPLETED", r.to_dict()
    params = _thread_ops(port)[0]
    assert params["end_type"] == 2
    assert "depth" not in params, params


def test_counterbore_is_cut_before_the_pilot():
    g = _graph([_box(),
                {"id": "n2", "type": "hole",
                 "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
                 "diameter": 0.005, "depth": 0.018,
                 "counterbore": {"diameter": 0.011, "depth": 0.0065},
                 "thread": {"designation": "M6", "class": "6H", "depth": 0.014}}])
    edges = _edges(_circle(0, 0.03, 0.0055), _circle(1, 0.0235, 0.0025),
                   _circle(2, 0.0055, 0.0025))
    port = FakePort(edges=edges)
    r = compile_and_run(port, g)
    assert r.status == "COMPLETED", r.to_dict()
    cuts = [p for (t, p) in port.calls
            if t == "extrude_feature" and p.get("feature_type") == "cut"]
    assert len(cuts) == 2, cuts
    assert cuts[0]["depth"] == pytest.approx(0.0065), cuts[0]        # counterbore FIRST
    assert cuts[1]["depth"] == pytest.approx(0.0245), cuts[1]        # 6.5 + 18 from the top
    assert len(_thread_ops(port)) == 1


def test_cosmetic_thread_node_lowers_to_exactly_one_op():
    g = _graph([_box(), _revolved_bore_sketch(), _revolved_bore(),
                {"id": "n3", "type": "cosmetic_thread", "designation": "M42x2",
                 "class": "6H", "depth": "through_all",
                 "ref": {"node": "n2",
                         "edge_anchor": {"near": [0.0, 0.03, 0.0], "radius": 0.021}}}])
    port = FakePort(edges=_edges(_circle(0, 0.03, 0.021), _circle(1, 0.0, 0.021)))
    r = compile_and_run(port, g)
    assert r.status == "COMPLETED", r.to_dict()
    params = _thread_ops(port)[0]
    assert params["size"] == "M42x2" and params["end_type"] == 2
    assert params["note"] == "M42x2-6H THROUGH", params["note"]
    assert params["diameter"] == pytest.approx(0.042)


def test_lowering_never_sends_the_internal_thread_ref_param():
    # The anchor rides beside the sentinel and must be popped before the tool sees it.
    port = FakePort(edges=_edges(_circle(0, 0.03, 0.0021), _circle(1, 0.0176, 0.0021)))
    compile_and_run(port, _threaded_hole())
    assert "thread_ref" not in _thread_ops(port)[0]


# ---------------------------------------------------------------------------------------------
# 6. The table module
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("major,pitch,drill", [
    (5.0, 0.8, 4.2), (6.0, 1.0, 5.0), (42.0, 2.0, 40.0), (40.0, 2.0, 38.0),
    # The two counter-examples that prove the table is not D-P:
    (8.0, 1.25, 6.8), (12.0, 1.75, 10.2),
])
def test_tap_drill_table(major, pitch, drill):
    assert thread_table.tap_drill_mm(major, pitch) == pytest.approx(drill)


def test_table_carries_provenance_and_the_human_confirm_marker():
    assert thread_table.SOURCE and "ISO 261" in thread_table.SOURCE
    assert thread_table.STANDARD_REVISION
    assert thread_table.MUST_BE_HUMAN_CONFIRMED is True


def test_designation_forms_normalize():
    assert thread_table.parse_designation("m5")[2] == "M5"
    assert thread_table.parse_designation("M5x0.8")[2] == "M5x0.8"
    assert thread_table.parse_designation("M5*0.8")[2] == "M5x0.8"
    with pytest.raises(ValueError):
        thread_table.parse_designation("W1/4")
