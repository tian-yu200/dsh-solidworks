"""The analyze_model read and its contract must say the same thing (batch 8).

Batch 8 added two things to the C# service and to `execution/solidworks/contracts/tool-schemas.json`:
  * an axis-aligned body bounding box — `bbox_min` / `bbox_max` / `bbox_size` (meters) appended LAST
    to the `mass_properties` and `geometry` rows, so no existing key or key order moved;
  * a `kind` on every curved face with that kind's parameters — cylinder, and the newly-read
    torus / sphere / cone, whose raw COM layouts were MEASURED on parts built through this service
    (TorusParams 8, SphereParams 4, ConeParams 8 — the cone's [0..2] is a point ON the axis, not the
    apex, and its half-angle is index 7 in RADIANS).

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


def test_datum_vocabulary_matches_the_frame_table():
    with open(SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    assert schema["covered_subset"]["datums"] == ["front", "top", "right"]
    frame = schema["reference_model"]["datum_frame"]
    assert sorted(key for key in frame if not key.startswith("_")) == ["front", "right", "top"]
