"""Capability tests for BLIND HOLE depth, REVOLVE CUT (batch 1) + REVOLVE AXIS (batch 2)
+ SWEEP SKETCH NAMES and the IDENTITY endpoint (batch 3) + SHELL (batch 4)
+ SLOT ORIENTATION (batch 5) + SMALL-SHELL VERIFICATION and RECTANGLE CENTRE (batch 7).

FAKE ExecutionPort, NO live SolidWorks.

Covered here:
  1. hole.depth as a positive NUMBER of metres lowers to a BLIND cut, while 'through_all'
     (and the 'through' alias) still lowers to the original through-all cut.
  2. revolve.operation 'boss' | 'cut' — 'cut' forwards the tool's cut flag; omitting it emits
     params IDENTICAL to the historical boss-only path (backward compatibility).
  3. ir_schema accepts finite positive metres and rejects everything else, with NO execution call.
  4. Batch 2: the executor resolves the revolve axis deterministically from the sketch's own
     segments, so the compiler must keep delivering the axis endpoints verbatim (either
     orientation, no new params), and a textbook shaft whose profile edge lies ON the axis must
     still validate. Plus the is_cut contract on both tool-facing files and the recipe doc wording.
  5. Batch 3: a sweep's path sketch must reach the tool as the RUNTIME name recorded from
     create_sketch's activeSketch echo (with no name recorded the sweep aborts instead of selecting
     a bogus sketch), and the read-only identity endpoint is documented as an endpoint — never as a
     tool — while staying on the STA thread with the cached COM attach and no feature-tree walk.
  6. Batch 4: the `shell` node — face anchors resolved on the PRE-shell geometry into the tool's
     JSON-array STRING, a closed shell emitting no face param at all, body_producers/feature_producers
     registration on both sides of the contract, and create_shell's verification of an API that
     returns nothing.
  7. Batch 5: the `slot` profile composite's `angle` — the 4-entity chain rotates as a rigid body
     (so a vertical or radial slot is no longer silently drawn horizontal), the un-rotated output
     stays byte-identical, shared endpoints chain EXACTLY at any angle, both caps still bulge
     OUTWARD, and a bad `angle` fails at plan time with zero execution calls.
  8. Batch 7: create_shell's "the volume must change" check reads the UNROUNDED mass property
     (the R6 echo it used to read quantises a volume to 1 mm3, a million times coarser than the
     1e-12 threshold, so a small or thin shell that WORKED was reported SHELL_FAILED), and the
     `rectangle` profile primitive's optional `cx`/`cy` centre is actually drawn — in lowering's
     emitted corners, in contours.closed_loop's sampled ring, and in the plan-time overlap
     verdict — while a graph that writes no centre lowers byte-identically.

Run two ways:
  - standalone:  python compiler/solidworks/pycompiler/tests/test_capabilities.py
  - pytest:      pytest compiler/solidworks/pycompiler/tests/test_capabilities.py
"""
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER_ROOT = os.path.dirname(os.path.dirname(_HERE))  # compiler/solidworks/
if _COMPILER_ROOT not in sys.path:
    sys.path.insert(0, _COMPILER_ROOT)

from pycompiler import contours, lowering                    # noqa: E402
from pycompiler.compiler import compile_and_run          # noqa: E402
from pycompiler.execution_port import ExecutionPort       # noqa: E402

# A canonical box's 6 planar faces. The +Y face at y=0.05 is the unambiguous "top".
_BOX_FACES = json.dumps({
    "face_count": 6,
    "faces": [
        {"i": 0, "planar": True, "area": 0.0025, "normal": [0, 1, 0],  "point": [0, 0.05, 0]},
        {"i": 1, "planar": True, "area": 0.0025, "normal": [0, -1, 0], "point": [0, 0.0, 0]},
        {"i": 2, "planar": True, "area": 0.0025, "normal": [1, 0, 0],  "point": [0.025, 0.025, 0]},
        {"i": 3, "planar": True, "area": 0.0025, "normal": [-1, 0, 0], "point": [-0.025, 0.025, 0]},
        {"i": 4, "planar": True, "area": 0.0025, "normal": [0, 0, 1],  "point": [0, 0.025, 0.025]},
        {"i": 5, "planar": True, "area": 0.0025, "normal": [0, 0, -1], "point": [0, 0.025, -0.025]},
    ],
})


def _hole_graph(depth):
    """box + a hole drilled into its top face at the given depth."""
    return {
        "schema_version": "0.7.5-draft", "units": "meters",
        "nodes": [
            {"id": "n1", "type": "box", "ref": {"datum": "top"},
             "width": 0.05, "depth": 0.05, "height": 0.05},
            {"id": "n2", "type": "hole",
             "ref": {"node_face": {"node": "n1", "selector": "top"}, "position": "center"},
             "diameter": 0.01, "depth": depth},
        ],
    }


# A half-section profile + its construction centerline (the 1-2 flange revolve idiom).
_REVOLVE_SKETCH = {
    "id": "s1", "type": "sketch", "ref": {"datum": "front"}, "profile": [
        {"kind": "line", "x1": 0.0, "y1": 0.0254, "x2": -0.0389, "y2": 0.0254},
        {"kind": "line", "x1": -0.0389, "y1": 0.0254, "x2": -0.0389, "y2": 0.0},
        {"kind": "line", "x1": -0.0389, "y1": 0.0, "x2": 0.0, "y2": 0.0},
        {"kind": "line", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.0254},
        {"kind": "line", "x1": 0.0, "y1": 0.0254, "x2": 0.0, "y2": 0.0, "construction": True},
    ]
}
_REVOLVE_AXIS = {"x1": 0.0, "y1": 0.0254, "x2": 0.0, "y2": 0.0}


def _revolve_graph(operation=None):
    node = {"id": "r1", "type": "revolve", "sketch": "s1", "axis": dict(_REVOLVE_AXIS),
            "angle": 6.283185}
    if operation is not None:
        node["operation"] = operation
    return {"schema_version": "0.7.5-draft", "units": "meters",
            "nodes": [dict(_REVOLVE_SKETCH), node]}


class FakePort(ExecutionPort):
    """Records calls, bumps state_version on writes, returns the canned box faces for
    analyze_model. Mirrors test_compiler.py's fake closely enough for these two features:
    a hole's create_sketch/add_sketch_entity/extrude_feature and a revolve's extrude_feature."""

    def __init__(self, faces=_BOX_FACES, sketch_names=None):
        self._sv = 0
        self._faces = faces
        self.calls = []  # list of (tool, params)
        # When given, each create_sketch echoes the next name as cadState.activeSketch — the field
        # the compiler records as that sketch node's RUNTIME name (a sweep's path is selected by
        # exactly that string). Left None, the response keeps the shape the batch-1/2 tests saw.
        self._sketch_names = list(sketch_names or [])

    def get_state(self):
        return self._sv

    def execute(self, tool, params, state_version):
        self.calls.append((tool, dict(params)))
        if tool == "analyze_model":
            payload = None if params.get("analysis_type") == "edges" else self._faces
            return {"status": "COMPLETED", "stateVersion": self._sv,
                    "cadState": {"features": [payload] if payload else []}}
        self._sv += 1
        feats = []
        if tool == "extrude_feature":
            feats = ["Cut-Extrude1" if params.get("feature_type") == "cut" else "Boss1"]
        elif tool == "box_feature":
            feats = ["Boss1"]
        cad = {"features": feats}
        if tool == "create_sketch" and self._sketch_names:
            cad["activeSketch"] = self._sketch_names.pop(0)
        return {"status": "COMPLETED", "stateVersion": self._sv, "cadState": cad}


def _cut_op(port):
    """The extrude_feature issued by a hole node (the graph's LAST call)."""
    tool, params = port.calls[-1]
    assert tool == "extrude_feature", port.calls
    return params


def _hole_ops(port):
    """The three ops a hole node emits, found by content rather than by index — the box that
    precedes them in the test graph contributes its own sketch/extrude calls."""
    sketch = [p for (t, p) in port.calls if t == "create_sketch" and p.get("on_face")]
    circle = [p for (t, p) in port.calls
              if t == "add_sketch_entity" and p.get("entity_type") == "circle"]
    assert len(sketch) == 1 and len(circle) == 1, port.calls
    return sketch[0], circle[0]


def _last_note(r):
    """The note attached to the LAST op of the LAST node (CompileResult reports per-node logs)."""
    log = r.to_dict()["node_log"]
    return log[-1]["ops"][-1]["note"]


# ---------------------------------------------------------------- blind hole


def test_blind_hole_lowers_to_blind_cut():
    port = FakePort()
    r = compile_and_run(port, _hole_graph(0.02))
    assert r.status == "COMPLETED", r.to_dict()
    cut = _cut_op(port)
    assert cut == {"feature_type": "cut", "depth": 0.02}, cut  # blind: depth, NO through flag


def test_blind_hole_note_reports_the_depth():
    port = FakePort()
    r = compile_and_run(port, _hole_graph(0.01))
    assert r.status == "COMPLETED", r.to_dict()
    assert "blind" in _last_note(r) and "0.01" in _last_note(r), _last_note(r)


def test_through_all_hole_unchanged():
    for token in ("through_all", "through"):
        port = FakePort()
        r = compile_and_run(port, _hole_graph(token))
        assert r.status == "COMPLETED", (token, r.to_dict())
        assert _cut_op(port) == {"feature_type": "cut", "through": True}, token


def test_blind_hole_sketch_and_circle_ops_unchanged():
    # Only the cut's end condition changes — the sketch-on-face and circle ops stay put.
    port = FakePort()
    r = compile_and_run(port, _hole_graph(0.02))
    assert r.status == "COMPLETED", r.to_dict()
    sketch, circle = _hole_ops(port)
    assert sketch == {"on_face": True, "face_index": 0}, sketch
    assert circle == {"entity_type": "circle", "cx": 0.0, "cy": 0.0, "radius": 0.005}, circle


def test_blind_hole_accepts_positive_meters():
    for depth in (0.001, 0.02, 5, 1e-4):
        port = FakePort()
        r = compile_and_run(port, _hole_graph(depth))
        assert r.status == "COMPLETED", (depth, r.to_dict())
        assert port.calls[-1][1]["depth"] == float(depth)


def test_blind_hole_rejects_bad_depths_with_no_execution():
    for depth in (0, -0.01, "3mm", "through_al", True, False, None,
                  float("nan"), float("inf")):
        port = FakePort()
        r = compile_and_run(port, _hole_graph(depth))
        assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", (depth, r.to_dict())
        assert port.calls == [], depth  # the compiler executed nothing
        assert "blind hole" in r.error["message"] or "through_all" in r.error["message"], depth


def test_blind_hole_depth_alone_is_not_added_to_the_vocabulary_arrays():
    # hole_depth in covered_subset stays ["through_all"]: a NUMBER is a type, not a vocabulary
    # token, so test_ir_schema_contract.py must keep machine-diffing the token set unchanged.
    schema = json.load(open(os.path.join(_COMPILER_ROOT, os.pardir, os.pardir,
                                         "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    assert schema["covered_subset"]["hole_depth"] == ["through_all"]


# ---------------------------------------------------------------- revolve cut


def test_revolve_cut_forwards_the_cut_flag():
    port = FakePort()
    r = compile_and_run(port, _revolve_graph("cut"))
    assert r.status == "COMPLETED", r.to_dict()
    tool, params = port.calls[-1]
    assert tool == "extrude_feature" and params["feature_type"] == "revolve", params
    assert params["is_cut"] is True, params


def test_revolve_boss_default_emits_no_cut_flag():
    # Backward compatibility: omitting operation must emit EXACTLY the historical params.
    omitted = FakePort()
    r1 = compile_and_run(omitted, _revolve_graph(None))
    explicit = FakePort()
    r2 = compile_and_run(explicit, _revolve_graph("boss"))
    assert r1.status == r2.status == "COMPLETED", (r1.to_dict(), r2.to_dict())
    assert "is_cut" not in omitted.calls[-1][1], omitted.calls[-1][1]
    assert omitted.calls == explicit.calls  # default and explicit 'boss' are the same call


def test_revolve_axis_and_angle_unaffected_by_operation():
    port = FakePort()
    r = compile_and_run(port, _revolve_graph("cut"))
    assert r.status == "COMPLETED", r.to_dict()
    params = port.calls[-1][1]
    assert (params["axis_x1"], params["axis_y1"], params["axis_x2"], params["axis_y2"]) \
        == (0.0, 0.0254, 0.0, 0.0)
    assert abs(params["angle"] - 359.999971) < 1e-3  # radians at IR -> degrees at the tool
    assert "is_cut" in params


def test_revolve_cut_note_names_the_operation():
    for op, want in ((None, "boss"), ("cut", "cut")):
        port = FakePort()
        r = compile_and_run(port, _revolve_graph(op))
        assert r.status == "COMPLETED", r.to_dict()
        assert ("revolve %s " % want) in _last_note(r), _last_note(r)


def test_revolve_rejects_unknown_operation_with_no_execution():
    # Every value below is set EXPLICITLY (operation omitted => boss, which is the compat path).
    for op in ("throttle", "Cut", "", True, False, None, 0):
        g = _revolve_graph("cut")
        g["nodes"][1]["operation"] = op
        port = FakePort()
        r = compile_and_run(port, g)
        assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", (op, r.to_dict())
        assert port.calls == [], op
        assert "'operation' must be 'boss' or 'cut'" in r.error["message"], op


def test_revolve_cut_sketch_must_still_close():
    # A revolve-cut consumes a closed profile exactly like a boss: an open chain is a plan-time
    # rejection (v0.7.3 closure rule lists 'revolve', not 'revolve boss'), never a mid-build failure.
    g = _revolve_graph("cut")
    g["nodes"][0]["profile"] = g["nodes"][0]["profile"][:2]  # drop the closing segments
    port = FakePort()
    r = compile_and_run(port, g)
    assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", r.to_dict()
    assert port.calls == []


def test_revolve_cut_still_must_follow_its_sketch():
    g = _revolve_graph("cut")
    g["nodes"].insert(1, {"id": "n9", "type": "box", "ref": {"datum": "top"},
                          "width": 0.01, "depth": 0.01, "height": 0.01})
    port = FakePort()
    r = compile_and_run(port, g)
    assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", r.to_dict()
    assert port.calls == []


# ---------------------------------------------------------------- schema prose


def test_schema_documents_both_new_params():
    schema = json.load(open(os.path.join(_COMPILER_ROOT, os.pardir, os.pardir,
                                         "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    assert "BLIND hole" in schema["feature_types"]["hole"]["depth"]
    assert "'boss'" in schema["feature_types"]["revolve"]["operation"]
    # The status text no longer claims revolve is boss-only (sweep/loft still are).
    assert "revolve/sweep/loft are boss-only" not in schema["_meta"]["status"]
    assert "sweep/loft are boss-only" in schema["_meta"]["status"]


# ============================================================ batch 2: revolve axis
#
# The axis is no longer found by a coordinate pick at its midpoint (SelectByID2 could return the
# PROFILE edge when the shaft is drawn with an edge on the axis, and converting that edge opens the
# contour). The executor now matches the axis coordinates against the sketch's own segments and, if
# the sketch holds no centerline there, creates a dedicated construction one — so nothing in the IR
# changes. These tests pin what the new resolution depends on: the exact axis numbers must reach the
# tool, unchanged and in the given orientation, with no new axis-carrying parameter.

_SHAFT_PROFILE = [
    # the textbook stepped shaft: half-section whose CLOSING EDGE LIES ON THE AXIS ...
    {"kind": "line", "x1": 0.0, "y1": 0.0, "x2": 0.010, "y2": 0.0},
    {"kind": "line", "x1": 0.010, "y1": 0.0, "x2": 0.010, "y2": 0.030},
    {"kind": "line", "x1": 0.010, "y1": 0.030, "x2": 0.006, "y2": 0.030},
    {"kind": "line", "x1": 0.006, "y1": 0.030, "x2": 0.006, "y2": 0.050},
    {"kind": "line", "x1": 0.006, "y1": 0.050, "x2": 0.0, "y2": 0.050},
    {"kind": "line", "x1": 0.0, "y1": 0.050, "x2": 0.0, "y2": 0.0},
    # ... AND a centerline drawn on that same line (the ambiguity that broke the midpoint pick)
    {"kind": "line", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.050, "construction": True},
]
_SHAFT_AXIS = {"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.050}


def _shaft_graph(axis=None):
    return {"schema_version": "0.7.5-draft", "units": "meters",
            "nodes": [
                {"id": "s1", "type": "sketch", "ref": {"datum": "front"},
                 "profile": [dict(p) for p in _SHAFT_PROFILE]},
                {"id": "r1", "type": "revolve", "sketch": "s1",
                 "axis": dict(axis or _SHAFT_AXIS), "angle": 2.0 * math.pi},
            ]}


def _revolve_call(port):
    return port.calls[-1][1]


def test_textbook_shaft_with_edge_on_axis_still_closes():
    # The profile edge ON the axis and a coincident centerline: closure must hold (construction
    # geometry is skipped by the contour check), or the graph never reaches the executor.
    port = FakePort()
    r = compile_and_run(port, _shaft_graph())
    assert r.status == "COMPLETED", r.to_dict()
    assert port.calls[0][0] == "create_sketch"
    assert _revolve_call(port)["feature_type"] == "revolve"


def test_axis_endpoints_reach_the_tool_verbatim():
    port = FakePort()
    compile_and_run(port, _shaft_graph())
    p = _revolve_call(port)
    assert (p["axis_x1"], p["axis_y1"], p["axis_x2"], p["axis_y2"]) == \
        (0.0, 0.0, 0.0, 0.050)  # exact values, no rounding/welding by the compiler


def test_axis_orientation_is_passed_through_unnormalised():
    # The executor matches endpoints in EITHER orientation, so the compiler must not canonicalise
    # the pair (reversing it in the graph is a different segment description, not an error).
    rev = {"x1": 0.0, "y1": 0.050, "x2": 0.0, "y2": 0.0}
    port = FakePort()
    r = compile_and_run(port, _shaft_graph(rev))
    assert r.status == "COMPLETED", r.to_dict()
    p = _revolve_call(port)
    assert (p["axis_x1"], p["axis_y1"], p["axis_x2"], p["axis_y2"]) == (0.0, 0.050, 0.0, 0.0)


def test_revolve_carries_no_axis_params_beyond_the_four_endpoints():
    # The robustness fix is entirely inside the executor: no new knob for the caller to get wrong.
    boss = FakePort()
    compile_and_run(boss, _shaft_graph())
    assert set(_revolve_call(boss)) == {"feature_type", "angle",
                                        "axis_x1", "axis_y1", "axis_x2", "axis_y2"}
    cut = FakePort()
    g = _shaft_graph()
    g["nodes"][1]["operation"] = "cut"
    compile_and_run(cut, g)
    assert set(_revolve_call(cut)) == set(_revolve_call(boss)) | {"is_cut"}


def test_full_revolve_angle_survives_the_degree_boundary():
    port = FakePort()
    compile_and_run(port, _shaft_graph())
    assert _revolve_call(port)["angle"] == 360.0


def test_tool_contract_documents_is_cut_in_both_places():
    # Batch-2 task 2: the contract and the adapter signature must gain is_cut together, or
    # adapters/claude/tests/test_schema_contract.py reports param drift.
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    contract = json.load(open(os.path.join(root, "execution", "solidworks", "contracts",
                                           "tool-schemas.json"), encoding="utf-8"))
    params = contract["extrude_feature"]["input"]["params"]
    assert "is_cut" in params
    assert "revolve" in params["is_cut"] and "cut" in params["is_cut"].lower()
    src = open(os.path.join(root, "adapters", "claude", "server.py"), encoding="utf-8").read()
    assert "is_cut: bool = False," in src
    assert '"is_cut": is_cut,' in src


def test_schema_prose_describes_the_deterministic_axis():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    schema = json.load(open(os.path.join(root, "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    desc = schema["feature_types"]["revolve"]["description"]
    assert "sketch's OWN segments" in desc
    assert "dedicated construction centerline" in desc
    assert "the tool selects it in the active sketch" not in desc


def test_recipe_doc_drops_revolve_from_the_boss_only_list():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    doc = open(os.path.join(root, "cad-planner", "recipe-usage.md"), encoding="utf-8").read()
    assert "sweep/revolve/loft" not in doc
    lines = doc.splitlines()
    at = [i for i, ln in enumerate(lines) if "Know what is NOT expressible" in ln][0]
    bullet = " ".join(lines[at:at + 2])          # the bullet wraps over two lines
    assert "sweep/loft" in bullet and '"cut"' in bullet
    # The ## headings are load-bearing (the resource split parses them) — the edit must not move one.
    assert [ln for ln in doc.splitlines() if ln.startswith("## ")] == [
        "## contract — Input / output contract",
        "## canonicalization — Rules C1–C7",
        "## forward — Forward authoring (intent → IR, no original part)",
        "## mapping — Core mapping steps (recipe → IR)",
        "## mapping_part — Part-vocabulary rules",
        "## mapping_sheet_metal — Sheet-metal rules",
        "## mapping_assembly — Assembly rules (component + mate)",
        "## verification — \"The LLM proposes, the round-trip decides\"",
        "## reverse — Drawing (DXF/DWG) → part reconstruction",
        "## coverage — Coverage reporting",
    ]


# ============================================================ batch 3: sweep runtime names
#
# A sweep selects its PATH sketch BY NAME, so the compiler must deliver the path node's RUNTIME
# sketch name to the tool — and that name exists only because create_sketch echoes it as
# cadState.activeSketch. The executor used to echo the literal placeholder "Sketch" there (and
# SketchManager.ActiveSketch is typed Sketch, which carries no name of its own), so a sweep asked
# to select a sketch called "Sketch", found nothing, and no pipeline sweep had ever built. The
# graph below is the one that built artifacts/bench/cap3_sweep.SLDPRT live: a
# 40 mm line plus a tangent R20 90-degree arc as the path on the Front datum, a Ø10 mm circle
# profile on the Right datum, volume 5.608994e-06 m3 against Pappus-Guldinus pi*r^2*(L+R*theta).

_SWEEP_PATH = [
    {"kind": "line", "x1": 0.0, "y1": 0.0, "x2": 0.040, "y2": 0.0},
    # centre (0.040, 0.020), from (0.040, 0) to (0.060, 0.020), CCW: tangent to the line above
    {"kind": "arc", "cx": 0.040, "cy": 0.020, "x1": 0.040, "y1": 0.0,
     "x2": 0.060, "y2": 0.020, "dir": 1},
]


def _sweep_graph():
    return {"schema_version": "0.7.5-draft", "units": "meters",
            "nodes": [
                {"id": "p1", "type": "sketch", "ref": {"datum": "front"},
                 "profile": [dict(s) for s in _SWEEP_PATH]},
                {"id": "s2", "type": "sketch", "ref": {"datum": "right"},
                 "profile": [{"kind": "circle", "diameter": 0.010, "cx": 0.0, "cy": 0.0}]},
                {"id": "w1", "type": "sweep", "sketch": "s2", "path": "p1"},
            ]}


def test_sweep_path_reaches_the_tool_as_the_sketch_runtime_name():
    # Two DISTINCT, non-sequential names: the sweep must carry the PATH node's name, not the
    # profile's, not the last one echoed, and not anything derived from node ids.
    port = FakePort(sketch_names=["Sketch7", "Sketch9"])
    r = compile_and_run(port, _sweep_graph())
    assert r.status == "COMPLETED", r.to_dict()
    tool, params = port.calls[-1]
    assert tool == "extrude_feature", port.calls
    assert params == {"feature_type": "sweep", "path_sketch": "Sketch7"}, params


def test_sweep_profile_stays_the_implicit_active_sketch():
    # The profile is never named in the params: extrude's grammar consumes the ACTIVE sketch, and
    # naming it too would be a second, contradictable source of truth.
    port = FakePort(sketch_names=["Sketch7", "Sketch9"])
    r = compile_and_run(port, _sweep_graph())
    assert r.status == "COMPLETED", r.to_dict()
    assert "Sketch9" not in json.dumps(port.calls[-1][1]), port.calls[-1][1]
    assert [p for (t, p) in port.calls if t == "create_sketch"][-1]["plane"] == "Right Plane"
    # Order is load-bearing: path sketch, then profile sketch, then the sweep.
    assert [t for (t, _) in port.calls] == [
        "create_sketch", "add_sketch_entity", "add_sketch_entity",
        "create_sketch", "add_sketch_entity", "extrude_feature"], port.calls


def test_sweep_fails_loudly_when_no_sketch_name_was_recorded():
    # The pre-fix echo (no activeSketch at all) must NOT degrade into a sweep against a bogus name:
    # the compiler aborts with NODE_FEATURE_UNAVAILABLE before the feature op is ever issued.
    port = FakePort()
    r = compile_and_run(port, _sweep_graph())
    assert r.status == "FAILED", r.to_dict()
    assert r.error["code"] == "NODE_FEATURE_UNAVAILABLE", r.to_dict()
    assert "no feature name was recorded" in r.error["message"], r.error
    assert [t for (t, _) in port.calls if t == "extrude_feature"] == [], port.calls


def test_sweep_path_sketch_may_stay_open_while_the_profile_must_close():
    # The live graph's path is an OPEN line+arc (a sweep path is never a contour) and its profile
    # is a closed circle. Adding a stray open segment to the PROFILE must be a plan-time rejection,
    # not a mid-build failure.
    g = _sweep_graph()
    port = FakePort(sketch_names=["S1", "S2"])
    assert compile_and_run(port, g).status == "COMPLETED"
    g["nodes"][1]["profile"] = [{"kind": "circle", "diameter": 0.010, "cx": 0.0, "cy": 0.0},
                                {"kind": "line", "x1": 0.0, "y1": 0.0, "x2": 0.003, "y2": 0.0}]
    broken = FakePort(sketch_names=["S1", "S2"])
    r = compile_and_run(broken, g)
    assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", r.to_dict()
    assert broken.calls == []


def test_sweep_requires_its_profile_immediately_before_and_a_different_path():
    for mutate, want in (
            ("swap", "two DIFFERENT sketch nodes"),
            ("interleave", "must immediately follow its PROFILE sketch node")):
        g = _sweep_graph()
        if mutate == "swap":
            g["nodes"][2]["path"] = "s2"
        else:
            g["nodes"].insert(2, {"id": "n9", "type": "box", "ref": {"datum": "top"},
                                  "width": 0.01, "depth": 0.01, "height": 0.01})
        port = FakePort(sketch_names=["S1", "S2"])
        r = compile_and_run(port, g)
        assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", (mutate, r.to_dict())
        assert want in r.error["message"], (mutate, r.error)
        assert port.calls == [], mutate


# ---------------------------------------------------------------- identity endpoint

def test_identity_endpoint_is_documented_but_is_not_a_tool():
    # /api/tool/identity is a plain GET, not a CAD operation: adding it as a top-level key of
    # tool-schemas.json would make adapters/claude/tests/test_schema_contract.py demand an adapter
    # tool for it. It belongs in the endpoint list in execution/solidworks/architecture.md.
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    contract = json.load(open(os.path.join(root, "execution", "solidworks", "contracts",
                                           "tool-schemas.json"), encoding="utf-8"))
    assert "identity" not in contract and "get_identity" not in contract
    assert "verify_state" in contract  # the endpoint must not displace the tool it mirrors
    doc = open(os.path.join(root, "execution", "solidworks", "architecture.md"),
               encoding="utf-8").read()
    at = doc.index("/api/tool/identity")
    line = doc[at - 4:at + 400]
    for field in ("activeDocument", "activeDocumentPath", "documentType", "stateVersion", "isDirty"):
        assert field in line, (field, line)
    assert "identity subset" in line and "feature-tree" in line  # the whole point: the identity
    # fields WITHOUT the tree walk that makes verify_state expensive.


def test_identity_endpoint_runs_on_sta_with_the_cached_attach():
    # Two invariants the speedup depends on, pinned against the source: COM calls go through the STA
    # thread, and the attach is the CACHED one every tool call uses — a fresh ROT lookup per read
    # would make the endpoint no cheaper than verify_state. And it must not touch the feature tree.
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    controller = open(os.path.join(root, "execution", "solidworks", "SolidworksExecution",
                                   "Controllers", "ToolController.cs"), encoding="utf-8").read()
    assert '[Route("identity")]' in controller
    body = controller[controller.index("public HttpResponseMessage Identity()"):
                      controller.index("// Health endpoint")]
    assert "StaExecutor.Instance.Run(() => _service.GetIdentityInfo())" in body, body
    assert "GetFeatures" not in body

    service = open(os.path.join(root, "execution", "solidworks", "SolidworksExecution",
                                "Services", "SolidWorksService.cs"), encoding="utf-8").read()
    start = service.index("public Dictionary<string, object> GetIdentityInfo()")
    info = service[start:start + 2000]
    info = info[:info.index("\n        }") + 1]  # the method body only
    assert "EnsureConnected()" in info and "Connect()" not in info, info
    assert "GetFeatures" not in info
    for field in ('result["stateVersion"]', 'result["activeDocument"]',
                  'result["activeDocumentPath"]', 'result["documentType"]', 'result["isDirty"]'):
        assert field in info, (field, info)


# ============================================================ batch 4: SHELL (v0.8.0)
#
# A shell hollows an EXISTING solid body to a uniform wall and leaves the chosen faces OPEN — the
# thin-wall-part / casting shape (the drawing whose wall is E=1.5). It is the first node whose
# geometry input is FACE anchors on the body it CONSUMES, so the anchors resolve on the PRE-shell
# geometry: the same pre-consumption reference rule fillet and chamfer already follow for edges
# (IR-ADR-008). The execution surface had no shell tool, so this batch grew create_shell around
# IModelDoc2.InsertFeatureShell — which is VOID (and has no IFeatureManager variant at all), hence
# the tree + volume verification instead of a return-value check.
#
# The canned FakePort faces are a 50 mm cube: face 0 is the +Y "top" (plane y=0.05), face 2 the
# +X side (plane x=0.025).

_SHELL_BOX = {"id": "n1", "type": "box", "ref": {"datum": "top"},
              "width": 0.060, "depth": 0.040, "height": 0.030}
_SHELL_TOP = {"near": [0.0, 0.05, 0.0], "hint": "the box top face"}
_SHELL_SIDE = {"near": [0.025, 0.03, 0.01]}


def _shell_graph(faces=(_SHELL_TOP,), **over):
    """box + a 2 mm shell leaving `faces` open. faces=None omits the key entirely (a CLOSED shell)."""
    node = {"id": "n2", "type": "shell", "body": "n1", "thickness": 0.002}
    if faces is not None:
        node["faces_to_remove"] = [dict(f) for f in faces]
    node.update(over)
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [dict(_SHELL_BOX), node]}


def _tools(port):
    return [t for (t, _p) in port.calls]


def test_shell_lowers_to_one_create_shell_with_resolved_face_indices():
    port = FakePort()
    r = compile_and_run(port, _shell_graph())
    assert r.status == "COMPLETED", r.to_dict()
    assert _tools(port).count("create_shell") == 1, port.calls
    tool, params = port.calls[-1]
    assert tool == "create_shell", tool
    # the anchor's plane is face 0 of the canned cube, delivered as the JSON-array STRING the
    # contract documents (the add_edge_feature/edge_indices idiom, ADR-022)
    assert params == {"thickness": 0.002, "face_indices": "[0]"}, params
    assert "shell t=0.002" in _last_note(r), _last_note(r)


def test_shell_face_anchor_is_resolved_on_the_pre_shell_geometry():
    # The anchor read must happen BEFORE the shell runs — after it, the selected faces are gone.
    port = FakePort()
    r = compile_and_run(port, _shell_graph())
    assert r.status == "COMPLETED", r.to_dict()
    tools = _tools(port)
    assert tools.index("analyze_model") < tools.index("create_shell"), tools
    # one read per shell node (the resolver memo serves every anchor from it)
    assert tools.count("analyze_model") == 1, tools


def test_shell_closed_shell_omits_face_indices():
    for faces in (None, ()):
        port = FakePort()
        r = compile_and_run(port, _shell_graph(faces=faces))
        assert r.status == "COMPLETED", (faces, r.to_dict())
        # nothing selected = InsertFeatureShell hollows the body keeping EVERY face
        assert port.calls[-1][1] == {"thickness": 0.002}, (faces, port.calls[-1])


def test_shell_outward_is_emitted_only_when_true():
    base = {"thickness": 0.002, "face_indices": "[0]"}
    for outward, expected in ((None, base), (False, base), (True, dict(base, outward=True))):
        g = _shell_graph()
        if outward is not None:
            g["nodes"][1]["outward"] = outward
        port = FakePort()
        r = compile_and_run(port, g)
        assert r.status == "COMPLETED", (outward, r.to_dict())
        assert port.calls[-1][1] == expected, (outward, port.calls[-1])


def test_shell_multiple_anchors_become_sorted_unique_indices():
    # Given side-then-top, the emitted indices are still ascending (deterministic ops).
    port = FakePort()
    r = compile_and_run(port, _shell_graph(faces=(_SHELL_SIDE, _SHELL_TOP)))
    assert r.status == "COMPLETED", r.to_dict()
    assert port.calls[-1][1]["face_indices"] == "[0,2]", port.calls[-1]


def test_shell_two_anchors_on_the_same_face_are_rejected_before_shelling():
    port = FakePort()
    r = compile_and_run(port, _shell_graph(faces=(_SHELL_TOP, {"near": [0.01, 0.05, 0.02]})))
    assert r.status == "FAILED" and r.error["code"] == "REFERENCE_AMBIGUOUS", r.to_dict()
    assert "duplicate anchor" in r.error["message"], r.error
    assert "create_shell" not in _tools(port), port.calls


def test_shell_anchor_on_no_plane_is_unresolved():
    port = FakePort()
    r = compile_and_run(port, _shell_graph(faces=({"near": [0.1, 0.1, 0.1]},)))
    assert r.status == "FAILED" and r.error["code"] == "REFERENCE_UNRESOLVED", r.to_dict()
    assert "create_shell" not in _tools(port), port.calls


def test_shell_body_must_be_a_body_producer_and_costs_no_execution():
    # A sketch is not a body, and a SHELL is not one either: v0 never shells a shell (and a hole
    # still cannot reference a shelled body), so shell is deliberately NOT in body_producers.
    sketch = {"id": "s0", "type": "sketch", "ref": {"datum": "top"},
              "profile": [{"kind": "rectangle", "width": 0.01, "height": 0.01}]}
    graphs = [
        {"schema_version": "0.8.0-draft", "units": "meters",
         "nodes": [dict(_SHELL_BOX), sketch,
                   {"id": "n3", "type": "shell", "body": "s0", "thickness": 0.002}]},
        {"schema_version": "0.8.0-draft", "units": "meters",
         "nodes": [dict(_SHELL_BOX),
                   {"id": "n2", "type": "shell", "body": "n1", "thickness": 0.002},
                   {"id": "n3", "type": "shell", "body": "n2", "thickness": 0.002}]},
    ]
    for g in graphs:
        port = FakePort()
        r = compile_and_run(port, g)
        assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", r.to_dict()
        assert "'body' must reference an earlier" in r.error["message"], r.error
        assert port.calls == [], port.calls  # the compiler executed nothing


def test_shell_rejects_bad_params_with_no_execution():
    for mutate in ({"thickness": 0}, {"thickness": -0.002}, {"thickness": "2mm"},
                   {"thickness": float("nan")}, {"thickness": float("inf")},
                   {"faces_to_remove": [{"near": [0, 0]}]},
                   {"faces_to_remove": [{"near": "top"}]},
                   {"faces_to_remove": "0"},
                   {"outward": "yes"}, {"body": "nope"}, {"body": ["n1"]}):
        g = _shell_graph()
        g["nodes"][1].update(mutate)
        port = FakePort()
        r = compile_and_run(port, g)
        assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", (mutate, r.to_dict())
        assert port.calls == [], mutate


def test_shell_is_registered_in_both_vocabularies():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    schema = json.load(open(os.path.join(root, "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    subset = schema["covered_subset"]
    assert "shell" in subset["node_types"], subset["node_types"]
    assert "shell" in subset["feature_producers"], subset["feature_producers"]
    assert "shell" not in subset["body_producers"], subset["body_producers"]
    assert set(schema["feature_types"]["shell"]) == {
        "description", "body", "thickness", "faces_to_remove", "outward"}

    from pycompiler import ir_schema
    assert "shell" in ir_schema.NODE_TYPES
    assert "shell" in ir_schema.FEATURE_PRODUCERS   # Shell1 is a real feature: patternable, mirrorable
    assert "shell" not in ir_schema.BODY_PRODUCERS   # a shell is never a body to hollow or to drill


def test_generated_quickref_lists_shell():
    # The quickref is GENERATED from the schema, so this pins that the new node type reaches the
    # model-facing capability card without any hand-written summary.
    import sys

    adapter = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir, "adapters", "dsh")
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import references as _references

    quickref = _references.render_quickref()
    assert "\nshell — " in quickref, quickref[:400]
    line = quickref[quickref.index("\nshell — "):].splitlines()[0]
    for field in ("body", "thickness", "faces_to_remove", "outward"):
        assert field in quickref[quickref.index("\nshell — "):], field
    assert "meters" in line or "meters" in quickref  # the units header is generated too


def test_create_shell_contract_and_dispatch_are_wired():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    contract = json.load(open(os.path.join(root, "execution", "solidworks", "contracts",
                                           "tool-schemas.json"), encoding="utf-8"))
    assert set(contract["create_shell"]["input"]["params"]) == {
        "thickness", "face_indices", "outward"}, contract["create_shell"]
    assert contract["create_shell"]["input"]["operation_id"]
    assert contract["create_shell"]["input"]["state_version"]

    controller = open(os.path.join(root, "execution", "solidworks", "SolidworksExecution",
                                   "Controllers", "ToolController.cs"), encoding="utf-8").read()
    assert 'case "create_shell"' in controller
    assert "return _service.CreateShell(request);" in controller


def test_create_shell_verifies_a_void_api_and_fails_loudly():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    src = open(os.path.join(root, "execution", "solidworks", "SolidworksExecution", "Services",
                            "SolidWorksService.Features.cs"), encoding="utf-8").read()
    start = src.index("public ExecutionResponse CreateShell(")
    body = src[start:src.index("public ExecutionResponse AddEdgeFeature(", start)]
    # THE decisive interop fact: the shell API is on IModelDoc2 and returns void, so verification
    # is the feature-tree diff plus a volume comparison — never a return-value check.
    assert "modelDoc.InsertFeatureShell(thickness.Value, outward)" in body, body
    assert "LastFeatureName(modelDoc)" in body and "EditRebuild3()" in body
    assert "FeatureManager.InsertFeatureShell" not in body  # that method does not exist
    # faces are selected by INDEX through the existing helper, plainly marked (no end condition)
    assert "SelectFaceByIndexAppend(modelDoc, fi, 0)" in body
    assert 'JArray.Parse(faceIndicesJson)' in body
    for code in ("SHELL_FAILED", "FACE_NOT_FOUND", "MISSING_PARAMETER", "INVALID_PARAMETER",
                 "WRONG_DOCUMENT_TYPE", "NO_ACTIVE_DOCUMENT", "COM_ATTACH_FAILED"):
        assert '"%s"' % code in body, code


def test_claude_adapter_exposes_create_shell_with_the_contract_params():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    src = open(os.path.join(root, "adapters", "claude", "server.py"), encoding="utf-8").read()
    start = src.index("def create_shell(")
    body = src[start:src.index("# Tool: add_edge_feature", start)]
    assert "@mcp.tool" in src[max(0, start - 200):start], src[max(0, start - 200):start]
    for pair in ('"thickness": thickness', '"face_indices": face_indices', '"outward": outward'):
        assert pair in body, pair


def test_recipe_mapping_vocabulary_lists_shell_at_the_registry_version():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    schema = json.load(open(os.path.join(root, "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    doc = open(os.path.join(root, "cad-planner", "recipe-usage.md"), encoding="utf-8").read()
    lines = doc.splitlines()
    at = [i for i, ln in enumerate(lines) if "Part vocabulary" in ln]
    assert len(at) == 1, at
    block = " ".join(lines[at[0]:at[0] + 4])
    assert "(%s)" % schema["_meta"]["version"] in block, block
    assert "`shell`" in block, block
    # the ## headings are load-bearing (the resource split parses them)
    assert [ln for ln in lines if ln.startswith("## ")][3] == "## mapping — Core mapping steps (recipe → IR)"


def test_shell_closed_form_predicts_the_cap4_live_geometry():
    """The arithmetic the cap4 live part was checked against, kept so a regression in shell
    semantics has a number to fail on without a CAD session.

    60 x 40 x 30 mm box, t = 2 mm, the +Y cap removed: 5 outer faces, 5 inner faces and the 2 mm rim
    as ONE annular face (SolidWorks merges the loop — 11 faces, not 14), every one of them planar.
    """
    outer_floor = 0.060 * 0.040
    inner_floor = 0.056 * 0.036
    rim = outer_floor - inner_floor                        # 60x40 minus the 56x36 opening
    walls = ([0.060 * 0.030] * 2 + [0.040 * 0.030] * 2     # outer: 1800 / 1200 mm2
             + [0.056 * 0.028] * 2 + [0.036 * 0.028] * 2)  # inner: 1568 / 1008 mm2
    faces = sorted([outer_floor, inner_floor, rim] + walls)
    assert len(faces) == 11 and len(set(faces)) == 7

    volume = 0.060 * 0.040 * 0.030 - 0.056 * 0.036 * 0.028  # 60*40*30 - 56*36*28 mm3
    assert abs(volume - 1.5552e-05) < 1e-12, volume
    assert abs(sum(faces) - 0.015952) < 1e-12, sum(faces)

    # what the live part reported face by face (cap4_verify_outputs.json)
    measured = sorted([3.84000e-04, 1.20000e-03, 1.20000e-03, 1.80000e-03, 1.80000e-03,
                       2.40000e-03, 1.00800e-03, 1.00800e-03, 1.56800e-03, 1.56800e-03,
                       2.01600e-03])
    assert [round(a, 9) for a in faces] == [round(b, 9) for b in measured], (faces, measured)


def test_shell_schema_prose_fits_the_quickref_without_being_clipped():
    """references.render_quickref() clips every field line at 150 chars and takes only the FIRST
    SENTENCE of a description, so verbose shell prose would appear mid-word in the card the planner
    actually reads — and would cost the topic pages it does not have. Keep the entry short."""
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    schema = json.load(open(os.path.join(root, "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    spec = schema["feature_types"]["shell"]
    for field, text in spec.items():
        collapsed = " ".join(str(text).split())
        if field == "description":
            # the meaning line is the first sentence, cut at ". " / " — " / ";" within 160 chars
            at = collapsed.find(". ")
            assert 0 < at < 70, (at, collapsed[:200])
        else:
            assert len(collapsed) <= 150, (field, len(collapsed), collapsed)
    # and nothing that the clip would drop is load-bearing: the facts moved into the description
    desc = " ".join(spec["description"].split())
    for fact in ("OMITTED or empty", "INWARD", "OUTSIDE", "analyze_model(faces)", "SHELL_FAILED"):
        assert fact in desc, fact


# ====================================================== batch 5: slot orientation (v0.8.0 batch 5)
#
# The `slot` composite expanded to a chain whose length ALWAYS lay on +X — there was no field to
# say otherwise, so a vertical or radial slot was silently drawn horizontal. Live evidence
# (2026-10-26, 3D01_01 attempt 1 and graph_diag_slotsonly): the fan's five angled slots became five
# OVERLAPPING horizontal ones running off the 72 mm tray, and their `through_all` cut returned null
# (EXTRUSION_FAILED at n7, both extrude directions) — while the SAME shape drawn as explicit
# arc/line entities cut first try. The fix is `angle` (RADIANS, CCW about cx/cy): the chain rotates
# as a rigid body and every shared endpoint is computed and rounded ONCE, so the contour still
# closes with no weld. These tests pin the rotation geometry, not just that it runs.

_SLOT_LENGTH = 0.05
_SLOT_WIDTH = 0.02
# 0 / quarter / half / three-quarter turns plus angles with no closed form at all: the quadrant
# cases are where a signed-zero or axis-swap bug shows, the irrational ones where precision shows.
_SLOT_ANGLES = (0.0, math.pi / 2, math.pi, -math.pi / 2, math.pi / 4, 0.52, -1.05, 2.4)


def _near(value, tol=1e-9):
    """Float comparison without a pytest dependency (this file also runs standalone). Accepts a
    scalar or a tuple; `tol` is widened where a SAMPLED arc is compared, because the emitted
    coordinates carry the 9-dp rounding the sampling inherits."""
    return _Near(value, tol)


class _Near:
    def __init__(self, value, tol):
        self.value = value
        self.tol = tol

    def __eq__(self, other):
        if isinstance(other, tuple) or isinstance(self.value, tuple):
            return (len(other) == len(self.value)
                    and all(abs(a - b) <= self.tol for a, b in zip(other, self.value)))
        return abs(other - self.value) <= self.tol

    def __repr__(self):
        return "_near(%r, tol=%g)" % (self.value, self.tol)


def _slot_prim(angle=None, cx=0.0, cy=0.0, length=_SLOT_LENGTH, width=_SLOT_WIDTH,
               construction=False):
    prim = {"kind": "slot", "length": length, "width": width, "cx": cx, "cy": cy}
    if angle is not None:
        prim["angle"] = angle
    if construction:
        prim["construction"] = True
    return prim


def _slot_ops(**kw):
    """The add_sketch_entity params ONE slot composite lowers to. Compiler-side sugar, so
    _profile_op is the function under test and no execution port is involved."""
    return [op.params for op in lowering._profile_op(_slot_prim(**kw))]


def _slot_graph(angle=None, cx=0.0, cy=0.0, length=_SLOT_LENGTH, width=_SLOT_WIDTH):
    """A plate with the slot cut through_all — the shape the failing benchmark graph used."""
    return {"schema_version": "0.8.0-draft", "units": "meters",
            "nodes": [
                {"id": "n1", "type": "box", "ref": {"datum": "top"},
                 "width": 0.12, "depth": 0.08, "height": 0.005},
                {"id": "n2", "type": "sketch", "ref": {"datum": "top"},
                 "profile": [_slot_prim(angle=angle, cx=cx, cy=cy, length=length, width=width)]},
                {"id": "n3", "type": "extrude", "sketch": "n2", "operation": "cut",
                 "end": "through_all"},
            ]}


def _sampled_arc(params, n=720):
    """Points along an emitted arc_center, sampled exactly as the execution layer's CreateArc
    resolves `direction` (+1 sweeps CCW start->end, -1 CW — SolidWorksService.Sketch.cs:243-288).
    The compiler always sends an explicit sign."""
    cx, cy = params["cx"], params["cy"]
    a1 = math.atan2(params["y1"] - cy, params["x1"] - cx)
    a2 = math.atan2(params["y2"] - cy, params["x2"] - cx)
    r = math.hypot(params["x1"] - cx, params["y1"] - cy)
    if params["direction"] > 0:
        while a2 < a1:
            a2 += 2.0 * math.pi
    else:
        while a2 > a1:
            a2 -= 2.0 * math.pi
    return [(cx + r * math.cos(a1 + (a2 - a1) * i / n),
             cy + r * math.sin(a1 + (a2 - a1) * i / n)) for i in range(n + 1)]


def _slot_contour(ops):
    """The 4 expanded entities as ONE closed contour in traversal order, arcs SAMPLED — so a cap
    sweeping the wrong way shows up as a self-crossing outline (and a wrong enclosed area)."""
    cap_l, top, cap_r, bot = ops
    pts = list(_sampled_arc(cap_l))
    pts.append((top["x2"], top["y2"]))
    pts.extend(_sampled_arc(cap_r))
    pts.append((bot["x2"], bot["y2"]))
    return pts


def _polygon_area(pts):
    """Shoelace, unsigned — the enclosed area of the sampled contour."""
    twice = 0.0
    for i, (x, y) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        twice += x * y2 - x2 * y
    return abs(twice) / 2.0


def _assert_chain_closes(ops):
    """Every shared endpoint must be the SAME number in both entities (exact, no tolerance): the
    contour closes by identical coordinates, which is the only way a frozen-coordinate IR sketch
    cuts. A weld would mean the rotation lost precision."""
    cap_l, top, cap_r, bot = ops
    pairs = ((cap_l, top, "x2", "y2", "x1", "y1"), (top, cap_r, "x2", "y2", "x1", "y1"),
             (cap_r, bot, "x2", "y2", "x1", "y1"), (bot, cap_l, "x2", "y2", "x1", "y1"))
    for earlier, later, kx1, ky1, kx2, ky2 in pairs:
        assert (earlier[kx1], earlier[ky1]) == (later[kx2], later[ky2]), (earlier, later)


def test_slot_without_angle_lowers_to_the_unchanged_horizontal_chain():
    # Back-compat: an omitted `angle` emits EXACTLY the entities the v0.7.4 primitive did, so no
    # already-passing graph changes shape (test_compiler.py's slot test pins the same numbers).
    assert _slot_ops() == [
        {"entity_type": "arc_center", "cx": -0.015, "cy": 0.0,
         "x1": -0.015, "y1": -0.01, "x2": -0.015, "y2": 0.01, "direction": -1},
        {"entity_type": "line", "x1": -0.015, "y1": 0.01, "x2": 0.015, "y2": 0.01},
        {"entity_type": "arc_center", "cx": 0.015, "cy": 0.0,
         "x1": 0.015, "y1": 0.01, "x2": 0.015, "y2": -0.01, "direction": -1},
        {"entity_type": "line", "x1": 0.015, "y1": -0.01, "x2": -0.015, "y2": -0.01},
    ]
    # spelling the default out must be indistinguishable from omitting it
    assert _slot_ops(angle=0.0) == _slot_ops()
    # and a full turn is the identity rotation, not a drift-by-epsilon (cos 2pi == 1.0 exactly)
    assert _slot_ops(angle=2.0 * math.pi) == _slot_ops()
    assert _slot_ops(angle=-2.0 * math.pi) == _slot_ops()


def test_vertical_slot_puts_length_on_y_with_the_caps_outward():
    # The case the benchmark needed: a quarter turn about (cx, cy) = a 50 x 20 slot standing up.
    ops = _slot_ops(angle=math.pi / 2, cx=0.01, cy=0.02)
    assert ops == [
        {"entity_type": "arc_center", "cx": 0.01, "cy": 0.005,
         "x1": 0.02, "y1": 0.005, "x2": 0.0, "y2": 0.005, "direction": -1},
        {"entity_type": "line", "x1": 0.0, "y1": 0.005, "x2": 0.0, "y2": 0.035},
        {"entity_type": "arc_center", "cx": 0.01, "cy": 0.035,
         "x1": 0.0, "y1": 0.035, "x2": 0.02, "y2": 0.035, "direction": -1},
        {"entity_type": "line", "x1": 0.02, "y1": 0.035, "x2": 0.02, "y2": 0.005},
    ]
    # the SAMPLED outline spans exactly width in X and length in Y, centred on (0.01, 0.02):
    # the caps bulge BEYOND the straight edges (which stop at y = 0.005 / 0.035), so the full
    # height is 0.045 - (-0.005) = 0.05 = length and the width is 0.02 with no cap overshoot.
    pts = _slot_contour(ops)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert (min(xs), max(xs)) == _near((0.0, 0.02), tol=1e-8)         # width across
    assert (min(ys), max(ys)) == _near((-0.005, 0.045), tol=1e-8)     # length about cy = 0.02


def test_slot_chain_closes_exactly_at_every_angle():
    for angle in _SLOT_ANGLES:
        for cx, cy in ((0.0, 0.0), (0.013, -0.027)):
            _assert_chain_closes(_slot_ops(angle=angle, cx=cx, cy=cy))


def test_slot_caps_bulge_outward_at_every_angle():
    # The sweep SENSE that survives a rotation: a rigid turn preserves CW/CCW, so both caps keep
    # direction -1 and still reach +/-(length/2) ALONG the (rotated) slot axis. The bug class this
    # pins is the inward 180 deg complement, which stops at +/-half instead and silently shrinks
    # the profile — a contour that cuts the wrong material.
    # Tolerance 1e-8: the arc radius is re-derived from coordinates rounded to 1e-9, so a sampled
    # extreme sits a few nanometres off the exact one.
    r = _SLOT_WIDTH / 2.0
    half = (_SLOT_LENGTH - _SLOT_WIDTH) / 2.0
    cx, cy = 0.01, 0.02
    for angle in _SLOT_ANGLES:
        cap_l, _top, cap_r, _bot = _slot_ops(angle=angle, cx=cx, cy=cy)
        axis = (math.cos(angle), math.sin(angle))
        norm = (-math.sin(angle), math.cos(angle))

        def frame(pt, axis=axis, norm=norm):
            dx, dy = pt[0] - cx, pt[1] - cy
            return (dx * axis[0] + dy * axis[1], dx * norm[0] + dy * norm[1])

        left = [frame(p) for p in _sampled_arc(cap_l)]
        right = [frame(p) for p in _sampled_arc(cap_r)]
        assert min(a for a, _n in left) == _near(-_SLOT_LENGTH / 2.0, tol=1e-8)  # -L/2 reached
        assert max(a for a, _n in left) == _near(-half, tol=1e-8)                # no further
        assert max(a for a, _n in right) == _near(_SLOT_LENGTH / 2.0, tol=1e-8)  # +L/2 reached
        assert min(a for a, _n in right) == _near(half, tol=1e-8)
        for arc in (left, right):
            # a half-circle spans the full width on BOTH sides of the axis; an inward sweep would
            # still do this, which is why the axis extremes above are the load-bearing check.
            assert max(n for _a, n in arc) == _near(r, tol=1e-8)
            assert min(n for _a, n in arc) == _near(-r, tol=1e-8)


def test_slot_enclosed_area_is_rotation_invariant():
    # (L-W)*W + pi*r^2 — the exact slot area, recomputed from the SAMPLED contour at every angle.
    # A cap that flipped inward loses a full semicircle of area, so this catches what a
    # bbox-only check would miss.
    expected = ((_SLOT_LENGTH - _SLOT_WIDTH) * _SLOT_WIDTH
                + math.pi * (_SLOT_WIDTH / 2.0) ** 2)
    for angle in _SLOT_ANGLES:
        assert _polygon_area(_slot_contour(_slot_ops(angle=angle))) == _near(expected, tol=1e-7)


def test_slot_orientation_is_compiler_side_only_no_new_tool_param():
    # add_sketch_entity knows nothing about slots: the composite must reach it as plain
    # line/arc entities with the SAME param vocabulary as before — no `angle` leaks through.
    allowed = {"entity_type", "cx", "cy", "x1", "y1", "x2", "y2", "direction", "construction"}
    for angle in _SLOT_ANGLES:
        for construction in (False, True):
            for params in _slot_ops(angle=angle, construction=construction):
                assert set(params) <= allowed, params
                if construction:
                    assert params["construction"] is True
                else:
                    assert "construction" not in params


def test_slot_fan_keeps_every_orientation_distinct():
    # The failing graph's own shape: five slots fanned across a 72 mm tray. Before `angle` all
    # five chains were IDENTICAL up to translation (five horizontal slots, overlapping, off the
    # body -> FeatureCut null). Now each group of four has its own axis direction.
    angles = (-1.05, -0.52, 0.0, 0.52, 1.05)
    centres = ((-0.028, 0.085533), (-0.014, 0.0881719), (0.0, 0.089),
               (0.014, 0.0881719), (0.028, 0.085533))
    ops = []
    for angle, (cx, cy) in zip(angles, centres):
        group = _slot_ops(angle=angle, cx=cx, cy=cy, length=0.022, width=0.004)
        _assert_chain_closes(group)
        ops.extend(group)
    assert len(ops) == 20
    axes = set()
    for params in [o for o in ops if o["entity_type"] == "line"]:
        dx, dy = params["x2"] - params["x1"], params["y2"] - params["y1"]
        d = math.hypot(dx, dy)
        # UNORIENTED axis: a slot's two straight edges run in opposite directions along it, so
        # fold the sign (else 5 slots read as 10 directions).
        if dx < 0 or (dx == 0 and dy < 0):
            dx, dy = -dx, -dy
        axes.add((round(dx / d, 6), round(dy / d, 6)))
    assert len(axes) == 5, axes          # the pre-fix build produced exactly ONE


def test_slot_fan_and_single_slot_compile_for_a_through_all_cut():
    # Whole-pipeline (fake port) regression: the graph that failed live must compile, emit 4
    # entities per slot, and need no weld to close.
    port = FakePort()
    r = compile_and_run(port, _slot_graph(angle=math.pi / 2, cx=0.02, cy=0.0))
    assert r.status == "COMPLETED", r.to_dict()
    # the plate's own `box` node contributes a rectangle entity — keep the slot's four
    entities = [p for (t, p) in port.calls
                if t == "add_sketch_entity" and p["entity_type"] in ("arc_center", "line")]
    assert len(entities) == 4
    assert [e["entity_type"] for e in entities] == ["arc_center", "line", "arc_center", "line"]
    assert _slot_ops(angle=math.pi / 2, cx=0.02, cy=0.0) == entities
    assert port.calls[-1][0] == "extrude_feature"
    # the rotated chain closed on its own: no endpoint weld was needed anywhere in the graph
    assert all("welded" not in op["note"]
               for node in r.node_log for op in node["ops"]), r.node_log


def test_slot_rejects_bad_angles_before_any_execution():
    # Non-numbers, non-finite, and DEGREES: `angle: 90` is the mistake the bound exists to catch,
    # because 90 rad lands within a few degrees of +X and would redraw the original bug silently.
    for angle in ("0.5", True, float("nan"), float("inf"), -float("inf"), 90, -100, 6.3, 7.0):
        port = FakePort()
        r = compile_and_run(port, _slot_graph(angle=angle))
        assert r.status == "FAILED", (angle, r.to_dict())
        assert r.error["code"] == "VALIDATION_FAILED", (angle, r.to_dict())
        assert "angle" in r.error["message"], (angle, r.error)
        assert port.calls == [], (angle, port.calls)
    # the bound is inclusive at one full turn, and every sampled angle in range compiles
    for angle in (2.0 * math.pi, -2.0 * math.pi) + _SLOT_ANGLES:
        port = FakePort()
        r = compile_and_run(port, _slot_graph(angle=angle))
        assert r.status == "COMPLETED", (angle, r.to_dict())


# ====================================================== batch 6: closed-loop overlap (v0.8.0 batch 6)
#
# Every check in contours.py worked on CHAINS — line/arc endpoints welded together. The five
# CLOSED-by-construction kinds (slot, rounded_rect, rectangle, circle, ellipse) enclose a region on
# their own, so they have no endpoints: open_gaps() saw nothing, collapsed_profile() saw nothing,
# and a sketch holding two of them OVERLAPPING validated clean and then failed in SolidWorks as a
# null cut. That is exactly how the 3D01_01 fan died twice (2026-10-26): without `angle` all five
# slots lowered horizontal, four adjacent pairs overlapped by ~1.4 mm, and EXTRUSION_FAILED came
# back at n7 after the part was already half built. contours.loop_conflicts() samples each closed
# primitive into a polygon and rejects, AT PLAN TIME, loops that cross / run collinear / reach
# through each other / cross themselves / enclose no area — while still allowing the two shapes
# SolidWorks does build: DISJOINT loops and properly NESTED ones (a hole inside an outline).
#
# The sample is INSCRIBED at 72 points per turn, and a crossing only counts when it lands more than
# CONTOUR_WELD_TOL (0.1 mm) inside both segments. Both choices bias toward ALLOWING: a false
# rejection blocks a correct drawing and the planner cannot argue with it, while a missed overlap
# leaves the pre-fix behaviour (fail in the build) in place.

# The failing graph's own five slots, verbatim from graph_diag_slotsonly.json sketch 'n6'.
_FAN_CENTRES = ((-0.028, 0.085533), (-0.014, 0.0881719), (0.0, 0.089),
                (0.014, 0.0881719), (0.028, 0.085533))
_FAN_LENGTHS = (0.028934, 0.0236562, 0.022, 0.0236562, 0.028934)
_FAN_WIDTH = 0.004


def _fan_prims(radial=False):
    prims = []
    for (cx, cy), length in zip(_FAN_CENTRES, _FAN_LENGTHS):
        prim = {"kind": "slot", "length": length, "width": _FAN_WIDTH, "cx": cx, "cy": cy}
        if radial:
            # each slot turned onto its own radial direction about the tray centre
            prim["angle"] = math.pi / 2.0 - math.atan2(cx, cy)
        prims.append(prim)
    return prims


def _closed_graph(*prims):
    """A plate whose top sketch carries exactly these closed primitives, cut through_all — the
    shape of the failing graph, so validate() reaches the loop check through a real consumer."""
    return {"schema_version": "0.8.0-draft", "units": "meters",
            "nodes": [
                {"id": "n1", "type": "box", "ref": {"datum": "top"},
                 "width": 0.12, "depth": 0.2, "height": 0.005},
                {"id": "n2", "type": "sketch", "ref": {"datum": "top"}, "profile": list(prims)},
                {"id": "n3", "type": "extrude", "sketch": "n2", "operation": "cut",
                 "end": "through_all"},
            ]}


def _how(prims):
    return [(c["how"], c["a"], c["b"], c["a_kind"], c["b_kind"])
            for c in contours.loop_conflicts(prims)]


def test_closed_loop_rejects_the_horizontal_slot_fan_naming_both_primitives():
    # The graph that died twice must now fail at PLAN TIME, and the message must name the two
    # primitives so the planner knows which coordinates to move.
    port = FakePort()
    r = compile_and_run(port, _closed_graph(*_fan_prims()))
    assert r.status == "FAILED", r.to_dict()
    assert r.error["code"] == "VALIDATION_FAILED", r.to_dict()
    message = r.error["message"]
    assert "overlapping closed contours" in message, message
    assert "profile[0] (slot)" in message and "profile[1] (slot)" in message, message
    assert "extrude 'n3'" in message, message        # says WHICH feature would have returned null
    assert port.calls == [], port.calls        # zero execution calls, so nothing to roll back
    # exactly the four adjacent pairs fan_overlap_check.py measured, and no spurious fifth
    assert len(_how(_fan_prims())) == 4, _how(_fan_prims())


def test_closed_loop_allows_the_same_fan_with_its_radial_angles():
    # The fixed shape: identical primitives, each given its own angle. 0 overlaps pre-fix /
    # post-fix is the whole point of the field, so the check must not fire here.
    assert _how(_fan_prims(radial=True)) == [], _how(_fan_prims(radial=True))
    port = FakePort()
    r = compile_and_run(port, _closed_graph(*_fan_prims(radial=True)))
    assert r.status == "COMPLETED", r.to_dict()
    arcs = [p for (t, p) in port.calls
            if t == "add_sketch_entity" and p["entity_type"] in ("arc_center", "line")]
    assert len(arcs) == 20, len(arcs)              # 4 entities per slot, five slots


def test_nested_and_disjoint_closed_loops_stay_legal():
    # The two multi-contour sketches SolidWorks DOES build. Rejecting these would break every
    # annulus and every two-hole plate in the corpus.
    rect = {"kind": "rectangle", "width": 0.10, "height": 0.06}
    hole = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.0}          # inside rect
    far = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.045}        # clear of rect
    left = {"kind": "circle", "diameter": 0.01, "cx": -0.04, "cy": 0.0}
    right = {"kind": "circle", "diameter": 0.01, "cx": 0.04, "cy": 0.0}
    rr = {"kind": "rounded_rect", "width": 0.012, "height": 0.006, "radius": 0.002, "cx": 0.0,
          "cy": 0.0}                                                          # inside hole
    slot_in = {"kind": "slot", "length": 0.03, "width": 0.006, "cx": 0.03, "cy": 0.0,
               "angle": math.pi / 2}                                          # inside rect only
    # 3D01_01 attempt 2's own n4 sketch, verbatim: the hub boss and its bore as two concentric
    # circles. That graph BUILT, so nesting is not just legal in theory here.
    a2_outer = {"kind": "circle", "diameter": 0.025, "cx": 0.0, "cy": 0.05}
    a2_bore = {"kind": "circle", "diameter": 0.022, "cx": 0.0, "cy": 0.05}
    for prims in ([rect, hole], [left, right], [hole, rr], [rect, rr, slot_in, left, right],
                  [rect, far], [a2_outer, a2_bore]):
        assert _how(prims) == [], prims
    port = FakePort()
    r = compile_and_run(port, _closed_graph(rect, hole, left, right))
    assert r.status == "COMPLETED", r.to_dict()


def test_crossing_closed_loops_are_rejected_with_the_crossing_point():
    # Two overlapping circles: their outlines DO cross, so this is the "cross" branch with a
    # coordinate the planner can act on — not the coarser "each reaches inside the other" fallback.
    a = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.0}
    b = {"kind": "circle", "diameter": 0.02, "cx": 0.012, "cy": 0.0}
    found = contours.loop_conflicts([a, b])
    assert [c["how"] for c in found] == ["cross"], _how([a, b])
    assert found[0]["point"] is not None and found[0]["more"] >= 1, found[0]
    # a rectangle through a circle's boundary crosses the same way
    wide = {"kind": "rectangle", "width": 0.06, "height": 0.004}
    ring = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.0}
    found = contours.loop_conflicts([wide, ring])
    assert [c["how"] for c in found] == ["cross"], _how([wide, ring])


def test_crossing_at_a_rings_bbox_extremity_is_still_found():
    # The indexed scan (contours._window) skips ring rows that cannot share a coordinate. The rows
    # it must NEVER skip are the ones at the ENDS of the ring's span: there a chord's min-x and
    # max-x nearly coincide, so the bisect ceiling and the walk-back floor close in on the same
    # value and a chord one step in from the extremity is the only one left in range. A hole
    # breaking out of an outline at its leftmost point is exactly that case.
    big = {"kind": "circle", "diameter": 0.10, "cx": 0.0, "cy": 0.0}
    out = {"kind": "circle", "diameter": 0.02, "cx": -0.05, "cy": 0.0}   # straddles x = -0.05
    assert [c["how"] for c in contours.loop_conflicts([big, out])] == ["cross"], _how([big, out])
    # the straight-edged version, on the kind written WITHOUT a centre (v0.8.0 batch 7 gave
    # rectangle an optional cx/cy; these bars write neither, so lowering._rectangle_op still draws
    # them on the origin): 0.104 wide breaks the r = 0.05 ring at BOTH of its x extremes.
    bar = {"kind": "rectangle", "width": 0.104, "height": 0.004}
    assert [c["how"] for c in contours.loop_conflicts([bar, big])] == ["cross"], _how([bar, big])
    # and the offset version of the same bar crosses the ring at ONE extreme only, where it now sits
    shifted = {"kind": "rectangle", "width": 0.104, "height": 0.004, "cx": 0.02}
    assert [c["how"] for c in contours.loop_conflicts([shifted, big])] == ["cross"], \
        _how([shifted, big])
    # the control: the same small circle wholly INSIDE the outline stays legal (nested)
    inner = {"kind": "circle", "diameter": 0.02, "cx": -0.038, "cy": 0.0}
    assert _how([big, inner]) == [], _how([big, inner])


def test_tangent_closed_loops_are_allowed():
    # Tangency is the boundary case the 0.1 mm inside-both-segments margin exists for: a plate with
    # two holes whose edges just touch is a legal (if unusual) sketch, and the inscribed polygon
    # sits slightly INSIDE the true circle, so it must not read as a crossing.
    a = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.0}
    b = {"kind": "circle", "diameter": 0.02, "cx": 0.02, "cy": 0.0}     # r+r = 0.02 apart exactly
    assert _how([a, b]) == [], _how([a, b])
    # a circle resting on a straight edge: the 72-gon has a VERTEX exactly on the line, and the
    # chords either side of it fall short of it (inscribed), so nothing crosses.
    tray = {"kind": "rectangle", "width": 0.072, "height": 0.196}
    resting = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.108}   # touches y = 0.098
    assert _how([tray, resting]) == [], _how([tray, resting])


def test_duplicate_closed_loop_is_rejected_as_collinear():
    # The same contour written twice never crosses, yet the profile is still uncuttable — the
    # "crossing" test alone would miss it, so coincident edges are their own finding.
    same = {"kind": "slot", "length": 0.022, "width": 0.004, "cx": 0.01, "cy": 0.02}
    assert [c["how"] for c in contours.loop_conflicts([same, dict(same)])] == ["collinear"]
    circle = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.0}
    assert [c["how"] for c in contours.loop_conflicts([circle, dict(circle)])] == ["collinear"]
    port = FakePort()
    r = compile_and_run(port, _closed_graph(same, dict(same)))
    assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", r.to_dict()
    assert "profile[0] (slot)" in r.error["message"] and "profile[1] (slot)" in r.error["message"]


def test_degenerate_closed_loop_is_rejected():
    # A loop with no enclosed area is a null cut waiting to happen; the weld checks cannot see it
    # because a composite has nothing to weld.
    sliver = {"kind": "rectangle", "width": 0.05, "height": 1e-6}
    flat_ellipse = {"kind": "ellipse", "cx": 0.0, "cy": 0.0, "x1": 0.01, "y1": 0.0,
                    "x2": 0.01, "y2": 0.0}      # minor axis == major axis: the ring collapses
    assert [c["how"] for c in contours.loop_conflicts([sliver])] == ["degenerate"]
    assert [c["how"] for c in contours.loop_conflicts([flat_ellipse])] == ["degenerate"]
    assert [c["b"] for c in contours.loop_conflicts([sliver])] == [None]


def test_closed_loop_check_skips_construction_and_chain_primitives():
    # Construction geometry is scaffolding (its coordinates are free-form), and chains belong to
    # the weld/gap checks — sampling them here would double-report the same profile twice.
    a = {"kind": "circle", "diameter": 0.02, "cx": 0.0, "cy": 0.0}
    b = {"kind": "circle", "diameter": 0.02, "cx": 0.012, "cy": 0.0}
    assert _how([a, b]) != [], "fixture must overlap"
    assert _how([a, dict(b, construction=True)]) == []
    assert contours.closed_loop({"kind": "line", "x1": 0.0, "y1": 0.0,
                                 "x2": 0.01, "y2": 0.01}) is None
    assert contours.closed_loop({"kind": "arc", "cx": 0.0, "cy": 0.0, "x1": 0.01, "y1": 0.0,
                                 "x2": 0.0, "y2": 0.01}) is None
    assert contours.closed_loop({"kind": "spline", "points": [0.0, 0.0, 0.01, 0.01]}) is None
    # a self-crossing line chain is the weld checks' business, not this one
    bowtie = [{"kind": "line", "x1": -0.01, "y1": -0.01, "x2": 0.01, "y2": 0.01},
              {"kind": "line", "x1": 0.01, "y1": -0.01, "x2": -0.01, "y2": 0.01},
              {"kind": "line", "x1": -0.01, "y1": -0.01, "x2": 0.01, "y2": -0.01},
              {"kind": "line", "x1": -0.01, "y1": 0.01, "x2": 0.01, "y2": 0.01}]
    assert contours.loop_conflicts(bowtie) == []


def test_closed_loop_polygon_is_the_geometry_lowering_emits():
    # closed_loop() rebuilds the outline from the primitive's OWN parameters (lowering imports
    # contours, so it cannot reuse lowering's entities). These pin that the two agree: the sample
    # must enclose the analytic area and span the analytic box at every orientation.
    for angle in _SLOT_ANGLES:
        prim = _slot_prim(angle=angle, cx=0.013, cy=-0.027)
        pts = contours.closed_loop(prim)
        area = _polygon_area(pts)
        expected = ((_SLOT_LENGTH - _SLOT_WIDTH) * _SLOT_WIDTH
                    + math.pi * (_SLOT_WIDTH / 2.0) ** 2)
        # 1e-6, not 1e-9: the two caps sample to a 36-chord polygon each, and an inscribed 72-gon
        # per turn is short by ~0.13% of the round's area. The STRAIGHT edges are exact, so this
        # still pins the geometry — a cap that swept inward would lose a whole semicircle (1.6e-4).
        assert area == _near(expected, tol=1e-6), (angle, area, expected)
        assert area < expected, (angle, area)      # inscribed: never overshoots the true outline
        # and the sampled span stays inside the true box
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        assert max(xs) - min(xs) <= _SLOT_LENGTH + 1e-9
        assert max(ys) - min(ys) <= _SLOT_LENGTH + 1e-9
    circle = contours.closed_loop({"kind": "circle", "diameter": 0.02, "cx": 0.005, "cy": 0.0})
    assert _polygon_area(circle) < math.pi * 0.01 ** 2      # inscribed => under the true area
    rr_area = 0.04 * 0.02 - (4.0 - math.pi) * 0.005 ** 2
    rr = contours.closed_loop({"kind": "rounded_rect", "width": 0.04, "height": 0.02,
                               "radius": 0.005})
    assert _polygon_area(rr) == _near(rr_area, tol=1e-6)
    assert _polygon_area(rr) < rr_area


def test_loop_conflicts_reports_every_distinct_pair():
    # Three mutually overlapping circles = 3 pairs, not one: the message has to name all of them,
    # or the planner moves two and the third pair still fails the build. (0.008 spacing, so even
    # the outer pair overlaps — at 0.01 they would only be tangent, which stays legal.)
    circles = [{"kind": "circle", "diameter": 0.02, "cx": cx, "cy": 0.0}
               for cx in (0.0, 0.008, 0.016)]
    pairs = sorted((c["a"], c["b"]) for c in contours.loop_conflicts(circles))
    assert pairs == [(0, 1), (0, 2), (1, 2)], pairs


def test_overlapping_fan_pair_names_the_shared_region():
    # "mutual" has no single crossing to point at, so the coordinate it carries is the centre of
    # the two loops' shared bounding box — the address the planner reads to find the pair.
    prims = _fan_prims()
    found = [c for c in contours.loop_conflicts(prims) if c["a"] == 0 and c["b"] == 1]
    assert len(found) == 1, found
    x, y = found[0]["point"]
    # slot 0 spans x -0.042467..-0.013533 / y 0.083533..0.087533, slot 1 x -0.0258281..-0.0021719
    # / y 0.0861719..0.0901719 — their overlap is x -0.0258281..-0.013533 (12.3 mm) by
    # y 0.0861719..0.087533 (1.36 mm), the exact numbers fan_overlap_check.py measured.
    assert (x, y) == _near((-0.0196806, 0.0868525), tol=1e-6), (x, y)
    assert found[0]["more"] == 0, found[0]


def test_slot_angle_is_documented_for_the_planner_in_radians():
    # The field only helps if the model-facing contract says what units it takes: the failure mode
    # this pins is a degrees value silently drawing the slot horizontal again.
    schema = json.load(open(os.path.join(_COMPILER_ROOT, os.pardir, os.pardir,
                                         "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    slot = schema["feature_types"]["sketch"]["profile_primitives"]["slot"]
    assert "angle" in slot, sorted(slot)
    for fact in ("RADIANS", "CCW", "2*pi", "pi/2"):
        assert fact in slot["angle"], fact
    assert "horizontal" in slot["angle"] and "angle" in slot["_note"], slot["_note"]


def test_generated_quickref_shows_the_slot_angle_field():
    # The quickref is GENERATED from the schema (references._compact_field_spec lists the dict's
    # keys), so a new primitive parameter reaches the planner with no hand-written summary to
    # update — and no stale card claiming slot has no orientation field.
    import sys

    adapter = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir, "adapters", "dsh")
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import references as _references

    quickref = _references.render_quickref()
    assert "slot(" in quickref, quickref[:400]
    card = quickref[quickref.index("slot("):]
    head = card[:card.index(")")]
    for field in ("length", "width", "cx", "cy", "angle"):
        assert field in head, head
    assert "rounded_rect(" in quickref, "the sibling composite must still be generated"


def test_closed_loop_primitive_alone_is_never_a_conflict():
    # A lonely closed primitive must never be reported — not even as "self-cross". This is the
    # invariant that keeps the check from rejecting a plain hole, and it is what makes the
    # self-cross branch a guard for a future sampling kind rather than a live code path: every
    # kind below samples to a SIMPLE polygon at every sane parameter set.
    singles = [
        {"kind": "rectangle", "width": 0.04, "height": 0.02},
        {"kind": "circle", "diameter": 0.02, "cx": 0.005, "cy": -0.005},
        {"kind": "ellipse", "cx": 0.0, "cy": 0.0, "x1": 0.02, "y1": 0.0, "x2": 0.0, "y2": 0.01},
        {"kind": "rounded_rect", "width": 0.04, "height": 0.02, "radius": 0.005},
        {"kind": "slot", "length": 0.03, "width": 0.006, "cx": 0.01, "cy": 0.0,
         "angle": 0.7},
    ]
    for prim in singles:
        assert contours.closed_loop(prim) is not None, prim
        assert contours.loop_conflicts([prim]) == [], prim
    # every orientation of the slot and a near-stadium rounded_rect too: the sampled ring changes
    # shape but never becomes non-simple
    for angle in _SLOT_ANGLES:
        assert contours.loop_conflicts([_slot_prim(angle=angle)]) == [], angle
    tight = {"kind": "rounded_rect", "width": 0.02, "height": 0.02, "radius": 0.0099999}
    assert contours.loop_conflicts([tight]) == [], tight


# ====================================================== batch 7: small-shell verification + rectangle centre (v0.8.0 batch 7)
#
# Two unrelated defects pi's review (review-pi-qoder.md, Defect 1) and a geometry audit turned up,
# each with its own live evidence.
#
# 1. create_shell verifies itself with "the volume must CHANGE" — IModelDoc2.InsertFeatureShell is
#    VOID (batch 4), so the feature-tree diff alone would report success on a shell that hollowed
#    nothing. The comparison read BuildBodySummary's "volume", which is R6: Math.Round(v, 6) on a
#    volume in METRES, a 1e-6 m3 = 1 mm3 quantum, against a 1e-12 "unchanged" threshold. On a body
#    smaller than that quantum BOTH sides round to 0.0, the delta reads "unchanged", and a shell
#    that worked is reported SHELL_FAILED. Reproduced live on the PRE-fix binary (2026-10-27, a
#    6 mm cube shelled 0.2 mm, cap removed): "InsertFeatureShell added '抽壳1' but the body volume
#    is unchanged (0 m3)" — while the SAME graph on the fixed build COMPLETED at exactly
#    3.4112e-08 m3 (34.112 mm3 of wall, the closed form). The fix reads the UNROUNDED
#    GetMassProperties2 value (RawVolume) for the delta and leaves R6 only in result_geometry,
#    where 1 mm3 is a fine resolution to echo.
#
# 2. A `rectangle` profile primitive ACCEPTED cx/cy at validation and then ignored them: lowering
#    emitted ±width/2, ±height/2 about the sketch ORIGIN and read no centre field, so the most
#    natural way to place a rectangle (analyze_model(sketch) reports a rectangle's real position,
#    so a planner reads a centre back and writes one) drew the wrong geometry silently, and
#    contours.closed_loop sampled the same wrong ring — two rectangles at different centres
#    collided as "collinear" while two that really overlapped could be missed.
#
# The rectangle fix must be free to every graph already in the field, so the byte-identity of the
# un-centred path is pinned as an EXACT equality, not a tolerance.

_RECT_SIZES = ((0.05, 0.05), (0.06, 0.04), (0.022, 0.004), (0.001, 0.196), (0.0127, 0.0083))
_RECT_CENTRES = ((0.013, -0.027), (-0.05, 0.03), (0.0, 0.005), (0.061, 0.0), (-0.0045, -0.0045))


def _rect_prim(width, height, cx=None, cy=None, construction=False):
    prim = {"kind": "rectangle", "width": width, "height": height}
    if cx is not None:
        prim["cx"] = cx
    if cy is not None:
        prim["cy"] = cy
    if construction:
        prim["construction"] = True
    return prim


def _rect_op(width, height, cx=None, cy=None, construction=False):
    """The add_sketch_entity params ONE rectangle lowers to. Compiler-side sugar: _profile_op is
    the function under test, so no execution port is involved."""
    return lowering._profile_op(_rect_prim(width, height, cx, cy, construction)).params


def _json(params):
    """The params as the execution layer will actually SEE them — POSTed as JSON, where -0.0 and
    0.0 are DIFFERENT BYTES while == says they are equal."""
    return json.dumps(params, sort_keys=True)


def test_rectangle_without_a_centre_lowers_byte_identically():
    # Every graph in the field writes a centred rectangle, so the offset must be exact arithmetic:
    # the corners are EMITTED (the tool's rectangle is two opposite corners), and a graph with no
    # cx/cy sends 0.0 - half and 0.0 + half — IEEE for -half and +half, bit for bit.
    for width, height in _RECT_SIZES:
        centred = {"entity_type": "rectangle", "x1": -width / 2.0, "y1": -height / 2.0,
                   "x2": width / 2.0, "y2": height / 2.0}
        assert _rect_op(width, height) == centred, (width, height)
        assert _json(_rect_op(width, height)) == _json(centred)
        # an explicit zero — int, float, or the signed variant — is the same drawing, byte for byte
        for cx, cy in ((0, 0), (0.0, 0.0), (-0.0, 0.0), (0, 0.0), (-0.0, 0)):
            assert _json(_rect_op(width, height, cx, cy)) == _json(centred), (cx, cy)


def test_rectangle_cx_cy_offsets_the_emitted_corners():
    for (width, height), (cx, cy) in zip(_RECT_SIZES, _RECT_CENTRES):
        params = _rect_op(width, height, cx, cy)
        assert (params["x1"], params["y1"], params["x2"], params["y2"]) == (
            cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0), params
        # the shape the centre claims: the box the corners describe is width x height AT (cx, cy)
        assert ((params["x1"] + params["x2"]) / 2.0,
                (params["y1"] + params["y2"]) / 2.0) == _near((cx, cy), tol=1e-15), params
        assert (params["x2"] - params["x1"],
                params["y2"] - params["y1"]) == _near((width, height), tol=1e-15), params
    # construction geometry keeps the same offset plus the flag
    params = _rect_op(0.04, 0.02, 0.013, -0.027, construction=True)
    assert params["construction"] is True and (params["x1"], params["y2"]) == (0.013 - 0.02,
                                                                                -0.027 + 0.01)


def test_rectangle_offset_needs_no_new_tool_param():
    # The offset is EMITTED corners, so the execution layer is untouched: add_sketch_entity already
    # takes x1/y1/x2/y2 for a rectangle and must receive nothing else — no `cx`/`cy` leak through
    # the rectangle path (those keys belong to circle/arc/ellipse).
    allowed = {"entity_type", "x1", "y1", "x2", "y2", "construction"}
    for cx, cy in ((None, None), (0.013, -0.027)):
        params = _rect_op(0.04, 0.02, cx, cy)
        assert set(params) <= allowed, params
        assert "cx" not in params and "cy" not in params, params
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    contract = json.load(open(os.path.join(root, "execution", "solidworks", "contracts",
                                           "tool-schemas.json"), encoding="utf-8"))
    params = contract["add_sketch_entity"]["input"]["params"]
    assert {"entity_type", "x1", "y1", "x2", "y2"} <= set(params), sorted(params)


def test_box_stays_centred_on_the_sketch_origin():
    # `box` is DEFINED as origin-centred (lowering's module docstring): it calls the same helper and
    # must not pick up the new optional centre, or every existing box would shift.
    ops = lowering.lower_box({"id": "n1", "type": "box", "ref": {"datum": "top"},
                              "width": 0.06, "depth": 0.04, "height": 0.03})
    rect = [op for op in ops if op.params.get("entity_type") == "rectangle"]
    assert len(rect) == 1, ops
    assert rect[0].params == {"entity_type": "rectangle", "x1": -0.03, "y1": -0.02,
                              "x2": 0.03, "y2": 0.02}, rect[0].params
    assert "centred" in rect[0].note, rect[0].note


def test_closed_loop_samples_the_rectangle_where_it_is_drawn():
    # contours.closed_loop rebuilds the ring from the primitive's own parameters (lowering imports
    # contours, so it cannot reuse lowering's entities) — the sampled ring must be the SAME four
    # corners the tool is told to draw, or the plan-time check judges a different sketch.
    for (width, height), (cx, cy) in zip(_RECT_SIZES, _RECT_CENTRES):
        params = _rect_op(width, height, cx, cy)
        ring = contours.closed_loop(_rect_prim(width, height, cx, cy))
        assert ring == [(params["x1"], params["y1"]), (params["x2"], params["y1"]),
                        (params["x2"], params["y2"]), (params["x1"], params["y2"])], (ring, params)
        assert _polygon_area(ring) == _near(width * height, tol=1e-15), ring
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        assert (min(xs), max(xs), min(ys), max(ys)) == _near(
            (cx - width / 2.0, cx + width / 2.0, cy - height / 2.0, cy + height / 2.0),
            tol=1e-15), ring
    # and the un-centred ring is bit-for-bit what it always was
    plain = contours.closed_loop(_rect_prim(0.05, 0.05))
    assert plain == [(-0.025, -0.025), (0.025, -0.025), (0.025, 0.025), (-0.025, 0.025)], plain


def test_rectangle_overlap_is_judged_at_its_real_position():
    # The payoff: two side-by-side rectangles are a legal sketch (SolidWorks cuts both), and two
    # that genuinely overlap are not. Before the offset was honoured, the pair below was reported
    # "collinear" — a FALSE rejection of a correct drawing, because both were sampled at the origin.
    left = _rect_prim(0.04, 0.02, -0.03, 0.0)      # x in [-0.05, -0.01]
    right = _rect_prim(0.04, 0.02, 0.03, 0.0)      # x in [ 0.01,  0.05] — a 20 mm gap between them
    assert _how([left, right]) == [], _how([left, right])
    port = FakePort()
    r = compile_and_run(port, _closed_graph(left, right))
    assert r.status == "COMPLETED", r.to_dict()
    # move the second rectangle 24 mm toward the first and 3 mm up (no edge is allowed to
    # coincide — coincidence is a DIFFERENT finding, "collinear") and the check now sees the overlap
    overlapping = _rect_prim(0.04, 0.02, 0.006, 0.003)
    assert [c["how"] for c in contours.loop_conflicts([left, overlapping])] == ["cross"], \
        _how([left, overlapping])
    port = FakePort()
    r = compile_and_run(port, _closed_graph(left, overlapping))
    assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", r.to_dict()
    # coincident centres are still the duplicate it looks like
    assert [c["how"] for c in contours.loop_conflicts([right, _rect_prim(0.04, 0.02, 0.03, 0.0)])] \
        == ["collinear"]


def test_rectangle_rejects_a_bad_centre_before_any_execution():
    # Same discipline as circle/slot/rounded_rect: an absent centre is legal (default 0), a written
    # one must be a finite number. Anything else fails at PLAN time — silently ignoring it is the
    # bug this batch fixes, and "ignored" is exactly what a planner cannot debug.
    for key in ("cx", "cy"):
        for bad in ("0.013", True, False, float("nan"), float("inf"), -float("inf"),
                    [0.013], {"v": 0.013}):
            prim = _rect_prim(0.04, 0.02, 0.0, 0.0)
            prim[key] = bad
            port = FakePort()
            r = compile_and_run(port, _closed_graph(prim))
            assert r.status == "FAILED" and r.error["code"] == "VALIDATION_FAILED", \
                (key, bad, r.to_dict())
            assert "rectangle" in r.error["message"] and key in r.error["message"], r.error
            assert port.calls == [], (key, bad, port.calls)
    # legal positions: omitted, zero, negative, and a size a plate would actually carry
    for cx, cy in ((None, None), (0, 0), (-0.025, 0.025), (0.061, 0.0)):
        port = FakePort()
        r = compile_and_run(port, _closed_graph(_rect_prim(0.04, 0.02, cx, cy)))
        assert r.status == "COMPLETED", (cx, cy, r.to_dict())


def test_rectangle_centre_is_in_the_contract_and_the_generated_card():
    # The planner learns the field exists from the CONTRACT: references._compact_field_spec lists a
    # dict-valued primitive's keys, so the card reads rectangle(width,height,cx,cy) with no
    # hand-written summary to update — and the prose stays inside the clip bound batch 4 pinned.
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    schema = json.load(open(os.path.join(root, "cad-planner", "contracts",
                                         "feature-graph.schema.json"), encoding="utf-8"))
    rect = schema["feature_types"]["sketch"]["profile_primitives"]["rectangle"]
    assert [k for k in rect if not k.startswith("_") and k != "kind"] == ["width", "height",
                                                                          "cx", "cy"], list(rect)
    for key in ("cx", "cy"):
        prose = " ".join(str(rect[key]).split())
        assert len(prose) <= 150, (key, prose)
        assert "optional" in prose and "default 0" in prose, prose
    note = " ".join(str(rect["_note"]).split())
    for fact in ("byte-identically", "sketch origin"):
        assert fact in note, fact
    # the release note in _meta.status is where the loop-check half of the change is recorded —
    # the card cannot carry it (a dict-valued primitive renders field NAMES only)
    status = " ".join(str(schema["_meta"]["status"]).split())
    for fact in ("batch 7", "contours.closed_loop"):
        assert fact in status, fact

    import sys
    adapter = os.path.join(root, "adapters", "dsh")
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import references as _references
    assert "rectangle(width,height,cx,cy)" in _references.render_quickref()


def _shell_source():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    src = open(os.path.join(root, "execution", "solidworks", "SolidworksExecution", "Services",
                            "SolidWorksService.Features.cs"), encoding="utf-8").read()
    start = src.index("public ExecutionResponse CreateShell(")
    return src[start:src.index("public ExecutionResponse AddEdgeFeature(", start)]


def _analyze_source():
    root = os.path.join(_COMPILER_ROOT, os.pardir, os.pardir)
    return open(os.path.join(root, "execution", "solidworks", "SolidworksExecution", "Services",
                             "SolidWorksService.Analyze.cs"), encoding="utf-8").read()


def test_create_shell_compares_the_unrounded_volume():
    body = _shell_source()
    cut = body.index("modelDoc.InsertFeatureShell(")
    # the DELTA decides SHELL_FAILED, so it must be read at full precision...
    assert "double? volBefore = RawVolume(modelDoc);" in body, body
    assert "double? volAfter = RawVolume(modelDoc);" in body, body
    assert body.index("volBefore = RawVolume") < cut < body.index("volAfter = RawVolume"), \
        "both reads must straddle the shell — reading both after would compare a value with itself"
    # ...never from the R6 summary echo, whose quantum (1e-6 m3) is coarser than the threshold
    assert 'BuildBodySummary(modelDoc)?.Value<double?>("volume")' not in body, body
    assert body.count("RawVolume(modelDoc)") == 2, body
    assert "Math.Abs(volAfter.Value - volBefore.Value) < 1e-12" in body, body
    # the response still echoes the R6 value: 1 mm3 is a fine resolution to REPORT
    assert "ResultGeometry = BuildBodySummary(modelDoc)" in body, body


def test_raw_volume_reader_returns_the_unrounded_mass_property():
    src = _analyze_source()
    start = src.index("private static double? RawVolume(")
    body = src[start:src.index("private static double R6(", start)]
    assert "GetMassProperties2" in body and "return mp[3];" in body, body
    assert "R6(" not in body, body                      # the whole point: no rounding
    assert "catch { return null; }" in body, body        # a document that reports nothing is null
    # and the summary the response carries keeps its R6 rounding (unchanged behaviour elsewhere)
    summary = src[src.index("private JObject BuildBodySummary("):start]
    assert 's["volume"] = R6(mp[3])' in summary, summary


def test_r6_hides_a_small_shells_volume_change_while_the_raw_change_does_not():
    """The arithmetic behind Defect 1, with no CAD session.

    R6 rounds a volume in METRES to 6 decimals = a 1e-6 m3 = 1 mm3 quantum; CreateShell's
    "unchanged" threshold is 1e-12. Any shell whose before/after volumes share a quantum read
    "unchanged" and was reported SHELL_FAILED. The 6 mm cube below is the case the live pre-fix
    binary failed; the 10 mm cube — the size the review prescribed — does NOT discriminate, both
    sides of it round apart (1.0e-6 vs 0.0), which is why the regression needs the smaller part.
    """
    def r6(v):
        return round(v, 6)              # C# Math.Round(v, 6) is the same midpoint-to-even rounding

    def shell(side_mm, thick_mm):
        side, t = side_mm / 1000.0, thick_mm / 1000.0
        before = side ** 3
        after = before - (side - 2 * t) ** 2 * (side - t)   # the cavity stays 2t narrower, t shallower
        return before, after

    discriminating = {6.0: True, 10.0: False}
    for side_mm, expected_collapses in discriminating.items():
        before, after = shell(side_mm, 0.2)
        assert abs(after - before) > 1e-12, (side_mm, before, after)   # the RAW delta: material went
        assert (r6(before) == r6(after)) == expected_collapses, \
            (side_mm, r6(before), r6(after))
    before6, after6 = shell(6.0, 0.2)
    assert r6(before6) == 0.0 and r6(after6) == 0.0, (before6, after6)
    assert abs(after6 - before6) == _near(1.81888e-7, tol=1e-15), after6 - before6


def test_small_open_shell_closed_form_matches_the_live_geometry():
    """bench/fix7_graph_open10.json and fix7_graph_open6.json, run live 2026-10-27.

    A cube on a Top datum (so the boss grows toward +Y and the cap lies on y = side), shelled
    t = 0.2 mm with that cap removed: 5 outer faces (floor + 4 full-height walls), 4 inner walls,
    1 inner floor, and the rim as ONE annular face = 11, every one planar — the same topology cap4
    reported for the 60x40x30 box, at a tenth of the size.
    """
    live = {
        10.0: (sorted([8e-06, 9.2e-05] + [0.0001] * 5 + [9.4e-05] * 4), 9.6832e-8),
        6.0: (sorted([5e-06, 3.1e-05] + [3.6e-05] * 5 + [3.2e-05] * 4), 3.4112e-8),
    }
    for side_mm, (measured_areas, measured_volume) in live.items():
        s, t = side_mm / 1000.0, 0.2 / 1000.0
        outer = s * s                                 # floor and 4 walls, all side x side
        inner_wall = (s - 2 * t) * (s - t)
        inner_floor = (s - 2 * t) ** 2
        rim = outer - inner_floor
        faces = sorted([outer] * 5 + [inner_wall] * 4 + [inner_floor, rim])
        volume = s ** 3 - inner_floor * (s - t)
        assert len(faces) == 11, faces
        assert volume == _near(measured_volume, tol=1e-15), (side_mm, volume, measured_volume)
        # face AREAS come back R6-rounded (1e-6 m2 = 1 mm2), so the echo lands on the same
        # quantum that hid the volume change — quantising the closed form reproduces every
        # printed value exactly, which is how the two runs are known to be this geometry.
        assert [round(a, 6) for a in faces] == measured_areas, (side_mm, faces)


def test_sub_millimetre_shell_thickness_lowers_like_a_thick_one():
    # Nothing in the compiler's vocabulary is millimetre-scaled, and the shell node must not start
    # now: 0.2 mm on a 10 mm cube lowers to the same two params as 2 mm on cap4's box.
    g = _shell_graph()
    g["nodes"][0].update({"width": 0.010, "depth": 0.010, "height": 0.010})
    g["nodes"][1]["thickness"] = 0.0002
    port = FakePort()
    r = compile_and_run(port, g)
    assert r.status == "COMPLETED", r.to_dict()
    assert port.calls[-1] == ("create_shell", {"thickness": 0.0002, "face_indices": "[0]"}), \
        port.calls[-1]
    # this is the graph whose DELTA the R6 comparison could not see: the whole remaining wall is
    # 96.832 mm3 = 9.6832e-8 m3, under the 1e-6 m3 quantum the pre-fix check compared with 1e-12.
    assert 0.010 ** 3 - 0.0096 ** 2 * 0.0098 < 1e-6


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print("ok   -", t.__name__)
        except AssertionError as ex:
            failed += 1
            print("FAIL -", t.__name__, "::", ex)
        except Exception as ex:  # noqa
            failed += 1
            print("ERR  -", t.__name__, "::", type(ex).__name__, ex)
    print("\n%d/%d passed" % (len(_TESTS) - failed, len(_TESTS)))
    sys.exit(1 if failed else 0)
