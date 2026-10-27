"""The analyze_model read and its contract must say the same thing (batch 8).

Batch 8 added two things to the C# service and to `execution/solidworks/contracts/tool-schemas.json`:
  * an axis-aligned body bounding box — `bbox_min` / `bbox_max` / `bbox_size` (meters) appended LAST
    to the `mass_properties` and `geometry` rows, so no existing key or key order moved;
  * a `kind` on every curved face with that kind's parameters — cylinder, and the newly-read
    torus / sphere / cone, whose raw COM layouts were MEASURED on parts built through this service
    (TorusParams 8, SphereParams 4, ConeParams 8 — the cone's [0..2] is a point ON the axis, not the
    apex, and its half-angle is index 7 in RADIANS);
  * a `kind='circle'` descriptor (center / normal / radius) on every CLOSED edge whose curve is a
    full circle — the bore-mouth handle `add_cosmetic_thread` selects by. `Curve.CircleParams` is
    7 doubles ([0..2] center, [3..5] axis, [6] radius), the layout confirmed on a live part.

A contract that describes a read the code does not perform is worse than no contract, so this test
diffs the documented strings against the shipped emitter. It also pins the datum vocabulary that
`tests/test_datum_frames.py` documents as the three canonical planes, because `reference_model`
prose and `covered_subset.datums` are the only two places a graph author meets them.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_analyze_read_contract.py`
"""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(REPO_ROOT, "execution", "solidworks", "contracts", "tool-schemas.json")
SERVICE = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution", "Services",
                       "SolidWorksService.Analyze.cs")
SCHEMA = os.path.join(REPO_ROOT, "cad-planner", "contracts", "feature-graph.schema.json")


def _contract():
    with open(CONTRACT, encoding="utf-8") as fh:
        return json.load(fh)


def _service():
    with open(SERVICE, encoding="utf-8") as fh:
        return fh.read()


def test_bbox_is_documented_for_both_scopes_and_appended_last():
    analyze = _contract()["analyze_model"]
    for field in (analyze["description"], analyze["output"]):
        for token in ("bbox_min", "bbox_max", "bbox_size"):
            assert token in field, field[:200]
    # documented as meters, as a triple, and as the LAST rows of each flat scope
    assert "bbox_min/bbox_max/bbox_size" in analyze["output"]
    assert "METERS" in analyze["output"]
    assert "appended last" in analyze["description"]

    lines = [line.strip() for line in _service().splitlines()]
    calls = [i for i, line in enumerate(lines) if line.startswith("AddBoundingBoxResults(")]
    assert len(calls) == 2, "the bbox helper must be called from exactly mass_properties + geometry"
    for i in calls:  # the row before each call is the pre-existing last key of that scope
        assert lines[i - 1].startswith('results.Add($"cz=') or \
               lines[i - 1].startswith('results.Add($"vertices='), lines[i - 1]
    for token in ('results.Add($"bbox_min=', 'results.Add($"bbox_max=', 'results.Add($"bbox_size='):
        assert any(line.startswith(token) for line in lines), token


def test_face_kinds_are_documented_as_the_service_emits_them():
    out = _contract()["analyze_model"]["output"]
    assert "'cylinder' {origin, axis, radius}" in out
    assert "'torus' {origin, axis, major_radius" in out and "minor_radius" in out
    assert "'sphere' {origin, radius}" in out
    assert "'cone' {origin (a point ON the axis, NOT the apex), axis, radius" in out
    assert "half_angle (RADIANS" in out

    src = _service()
    for emit in ('fj["kind"]', 'fj["major_radius"]', 'fj["minor_radius"]', 'fj["half_angle"]',
                 'fj["origin"]', 'fj["axis"]', 'fj["radius"]'):
        assert emit in src, emit
    # the guards the contract's key sets depend on: torus/cone 8 doubles, sphere 4, cylinder 7
    assert "tp.Length >= 8" in src and "cn.Length >= 8" in src
    assert "sp.Length >= 4" in src and "cp2.Length >= 7" in src
    # the debug probe key used to measure the layouts must not have shipped
    assert "dbg_cone_raw" not in src


def test_circular_edge_descriptor_is_documented_as_the_service_emits_it():
    """The `kind='circle'` edge descriptor add_cosmetic_thread selects by (design §4.2).

    Same rule as the two checks above: the contract describes the read, and this diffs the
    description against the shipped emitter. The two facts a resolver depends on and that are easy
    to break silently are (a) the new keys are APPENDED after start/end/mid/length — no existing key
    or key order moves, because `get_selection` compares JSON built by this same reader on both
    sides of an index lookup — and (b) the probe runs ONLY on the closed-edge path, so an arc is
    never tagged and absence of `kind` must not be read as 'not circular'.
    """
    analyze = _contract()["analyze_model"]
    for field in (analyze["description"], analyze["output"]):
        for token in ("kind='circle'", "center", "normal", "radius"):
            assert token in field, field[:300]
        # the closed-only caveat, in both places that promise the keys
        assert "closed" in field.lower()
    assert "add_cosmetic_thread" in analyze["description"]

    src = _service()

    def pos(needle):
        """Offset of a needle that must occur EXACTLY ONCE — so a key emitted twice (or renamed)
        fails here instead of silently comparing the wrong pair of offsets."""
        assert src.count(needle) == 1, needle
        return src.index(needle)

    for emit in ('ej["kind"] = "circle";', 'ej["center"] =', 'ej["normal"] =', 'ej["radius"] ='):
        assert emit in src, emit
    # the length guard the descriptor sits behind, and the read that sets the flag
    assert "curve.IsCircle()" in src
    assert "f.Circle.Length >= 7" in src, "the flag must be set from a SUCCESSFUL CircleParams read"
    assert "CircleParams" in src and "SurfaceParamsArray(curve.CircleParams)" in src
    # ORDER: the circle keys are appended after `length`, in the closed branch only
    p_len = pos('ej["length"] =')
    p_kind = pos('ej["kind"] = "circle"')
    p_center = pos('ej["center"] =')
    p_normal = pos('ej["normal"] =')
    p_radius = pos('ej["radius"] = R6(c[6]);')   # `ej["radius"]` alone also names sketch circles
    assert p_len < p_kind < p_center < p_normal < p_radius
    # the open-edge branch returns before ANY of it — no per-edge extra COM call on the big path
    p_open = pos("if (!f.Closed)")
    assert p_open < p_len and "return ej;" in src[p_open:p_len]
    # `normal` is documented as the circle's own axis, which is the mouth-vs-far-end discriminator
    assert "the circle's own axis" in analyze["output"]


def test_datum_vocabulary_matches_the_frame_table():
    with open(SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    assert schema["covered_subset"]["datums"] == ["front", "top", "right"]
    frame = schema["reference_model"]["datum_frame"]
    assert sorted(key for key in frame if not key.startswith("_")) == ["front", "right", "top"]
