"""The datum-frame table is pinned documentation, not folklore.

Batch 8 (2026-10-27) measured how a canonical datum sketch's 2-D x/y map onto model axes —
12 throwaway parts built through the live pipeline (asymmetric rectangles bossed blind / reversed /
on a +/-0.012 offset plane, read back as analyze_model(geometry) bbox_min/bbox_max and re-confirmed
against the plane.frame each saved document reports). The contract had never stated it: the axes
were implied by `extrude.reversed`'s normal list and by nothing else, so authoring an off-centre
profile on 'top' or 'right' was guesswork.

Two documents carry the table and they must not drift apart:
  * cad-planner/contracts/feature-graph.schema.json  -> reference_model.datum_frame
  * cad-planner/recipe-usage.md                      -> the fenced table in `## forward`

This test pins (1) the exact row text in BOTH files, (2) that the two files agree with each other,
and (3) that the table is INTERNALLY sound — right-handed frames, growth along the printed normal,
`reversed` its negation, and the offset column naming that same axis — so a future edit that
transposes front/top cannot pass by matching itself in two places. It also pins the table against
the pre-existing `extrude.reversed` prose, which is the other place the normals are stated.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_datum_frames.py`
"""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(REPO_ROOT, "cad-planner", "contracts", "feature-graph.schema.json")
RECIPE_PATH = os.path.join(REPO_ROOT, "cad-planner", "recipe-usage.md")

HEADER = ("datum | sketch +x | sketch +y | +normal | blind boss grows into | "
          "reversed grows into | positive offset d moves the plane to")

# The measured rows, verbatim. Changing one here means changing the measurement, not the wording.
ROWS = {
    "front": "front | +X | +Y | +Z | +Z | -Z | Z=+d",
    "top": "top | +X | -Z | +Y | +Y | -Y | Y=+d",
    "right": "right | -Z | +Y | +X | +X | -X | X=+d",
}

# The measured invariants, as the schema states them (the recipe wraps, so match on whitespace).
INVARIANTS = (
    "handedness: sketch_x x sketch_y = +normal on all three (right-handed): "
    "front +X x +Y = +Z, top +X x -Z = +Y, right -Z x +Y = +X",
    "origin: the sketch origin IS the model origin on every datum (no in-plane shift), "
    "and an offset plane keeps the SAME xdir/ydir",
    "independent: offset moves the PLANE only; reversed flips the GROWTH direction only",
    "negative offset: d < 0 lands the plane on the -normal side",
)

_VEC = {
    "+X": (1, 0, 0), "-X": (-1, 0, 0),
    "+Y": (0, 1, 0), "-Y": (0, -1, 0),
    "+Z": (0, 0, 1), "-Z": (0, 0, -1),
}


def _frame():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = json.load(fh)
    frame = schema["reference_model"]["datum_frame"]
    assert isinstance(frame, dict), "reference_model.datum_frame is not an object"
    return frame


def _recipe_text():
    with open(RECIPE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _flat(text):
    return " ".join(text.split())


def _row_cells(row):
    cells = [cell.strip() for cell in row.split("|")]
    assert len(cells) == 7, "a table row must have 7 columns, got %r" % (row,)
    return cells


def test_table_rows_are_pinned_verbatim_in_both_documents():
    frame = _frame()
    assert frame["_columns"] == HEADER
    for datum, row in ROWS.items():
        assert frame[datum] == row, (datum, frame[datum])
    recipe_lines = [line.strip() for line in _recipe_text().splitlines()]
    assert HEADER in recipe_lines, "the table header is gone from recipe-usage.md"
    for datum, row in ROWS.items():
        assert row in recipe_lines, "%s row drifted out of recipe-usage.md: %r" % (datum, row)


def test_table_is_right_handed_and_internally_consistent():
    for datum, row in ROWS.items():
        name, sketch_x, sketch_y, normal, boss, reversed_boss, offset = _row_cells(row)
        assert name == datum
        x, y, n = _VEC[sketch_x], _VEC[sketch_y], _VEC[normal]
        assert _cross(x, y) == n, "%s: sketch_x x sketch_y != +normal — the frame is not " \
                                  "right-handed as measured" % datum
        assert boss == normal, "%s: a blind boss grows into %s, not the +normal %s" % (
            datum, boss, normal)
        neg = "-" + normal[1:] if normal[0] == "+" else "+" + normal[1:]
        assert reversed_boss == neg, "%s: reversed must be the -normal side" % datum
        assert offset.endswith("=+d"), "%s: a positive offset must move the plane along +d" % datum
        assert offset[0] == normal[1], "%s: offset %s moves a different axis than the normal %s" % (
            datum, offset, normal)


def test_recipe_invariants_agree_with_the_schema_invariants():
    frame = _frame()
    recipe = _flat(_recipe_text())
    for sentence in INVARIANTS:
        assert any(_flat(sentence) in entry for entry in frame["_invariants"]), \
            "schema _invariants lost: %s" % sentence
    # every schema invariant is ALSO in the recipe, whole (the recipe only re-wraps them)
    for entry in frame["_invariants"]:
        assert _flat(entry) in recipe, "recipe-usage.md does not carry: %s" % entry


def test_provenance_and_measurement_are_stated_with_the_table():
    measured = _flat(_frame()["_measured"])
    assert "MEASURED 2026-10-27" in measured
    assert "NOT shipped in the repo" in measured
    assert "asymmetric" in measured.lower()
    assert "bbox_min" in measured and "plane.frame" in measured
    recipe = _flat(_recipe_text())
    assert "MEASURED 2026-10-27" not in recipe  # the recipe says it in its own words
    assert "12 probe parts" in recipe
    assert "reference_model.datum_frame" in recipe


def test_reversed_prose_normals_match_the_table():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = json.load(fh)
    reversed_prose = schema["feature_types"]["extrude"]["reversed"]
    assert "Front→+Z" in reversed_prose and "Top→+Y" in reversed_prose and "Right→+X" in reversed_prose
    for datum, row in ROWS.items():
        cells = _row_cells(row)
        axis = cells[3]  # +normal
        label = {"front": "Front", "top": "Top", "right": "Right"}[datum]
        assert "%s→%s" % (label, axis) in reversed_prose, (
            "extrude.reversed states %s→%s but the table says %s" % (label, axis, datum))
    assert "reference_model.datum_frame" in reversed_prose
    assert "reference_model.datum_frame" in schema["feature_types"]["sketch"]["ref"]
