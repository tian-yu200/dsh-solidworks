"""Tests for READ-CONVERGE: the no-re-read rule and the batched multi-region call.

Both exist because of a measured failure (2026-10-29, four DSH sessions, one L-bracket drawing):
none produced a plan. The model re-zoomed the same areas with slightly different boxes (35 crops
in one run) and requested only 2 crops per step at 45-90 s per step. Advisory text did not fix it,
so the rules are enforced in code:
  * a crop whose area is already covered by an earlier crop of the same drawing is a REPEAT; one
    repeat is allowed only at a strictly higher zoom, any further one is refused;
  * many regions can be read in ONE call, so a reading round costs one step instead of twelve.
"""
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import intake


@pytest.fixture
def intake_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts" / "intake"
    monkeypatch.setattr(intake, "INTAKE_ROOT", root)
    return root


@pytest.fixture
def fresh(intake_root, monkeypatch):
    """Isolate both memories and the limit: every test starts on untouched ground."""
    monkeypatch.setattr(intake, "_CROPS_USED", {})
    monkeypatch.setattr(intake, "_CROP_MEMORY", {})
    monkeypatch.delenv(intake.CROP_LIMIT_ENV, raising=False)


@pytest.fixture
def sheet(tmp_path):
    path = tmp_path / "sheet.png"
    image = Image.new("RGB", (1200, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 900, 800], outline=(0, 0, 0), width=2)
    draw.text((300, 400), "28x8 R100", fill=(0, 0, 0))
    image.save(path)
    return path


def _meta(path, region="", zoom=2):
    _data, meta = intake.read_region(str(path), region, zoom)
    return meta


# ---------------------------------------------------------------------------------------------
# 1. No re-reading
# ---------------------------------------------------------------------------------------------
def test_the_same_cell_read_twice_is_refused(fresh, sheet):
    first = _meta(sheet, "C4")
    assert first["budget"]["crops_used"] == 1
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ") as raised:
        _meta(sheet, "C4")
    message = str(raised.value)
    assert "300,450,450,600" in message        # names the earlier crop
    assert "UNKNOWN" in message                # and says what to do instead
    assert "ask the user" in message


def test_a_box_inside_an_earlier_crop_is_a_repeat(fresh, sheet):
    # containment: a box wholly inside C4 is the same ground, even with different numbers
    _meta(sheet, "C4")
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ"):
        _meta(sheet, "320,470,430,580")


def test_a_partially_overlapping_box_below_the_threshold_is_allowed(fresh, sheet):
    # 70% containment is the line: this box shares only a corner with C4, so it is new ground
    _meta(sheet, "C4")                        # 300..450 x 450..600
    meta = _meta(sheet, "420,570,600,750")    # overlap 30x30 = 900 of 180x180 -> 2.8%
    assert meta["budget"]["crops_used"] == 2


def test_one_higher_zoom_repeat_is_allowed_and_the_next_is_refused(fresh, sheet):
    _meta(sheet, "C4", zoom=2)
    allowed = _meta(sheet, "C4", zoom=4)      # "still too small" -> one more look is legitimate
    assert allowed["budget"]["crops_used"] == 2
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ"):
        _meta(sheet, "C4", zoom=4)            # the second look at the same ground is refused


def test_a_repeat_without_a_higher_zoom_is_refused(fresh, sheet):
    _meta(sheet, "C4", zoom=3)
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ"):
        _meta(sheet, "C4", zoom=3)
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ"):
        _meta(sheet, "C4", zoom=2)            # a LOWER zoom buys even fewer pixels


def test_the_overview_is_exempt_and_stays_repeatable(fresh, sheet):
    for _ in range(3):
        meta = _meta(sheet, "")
        assert meta["action"] == "overview"
        assert meta["budget"]["crops_used"] == 0
    assert intake.crops_read(intake.hashlib.sha256(sheet.read_bytes()).hexdigest()) == []


def test_the_memory_is_per_image(fresh, tmp_path, sheet):
    other = tmp_path / "other.png"
    Image.new("RGB", (600, 400), (255, 255, 255)).save(other)
    _meta(sheet, "C4")
    meta = _meta(other, "C4")                 # same LABEL, different drawing -> not a repeat
    assert meta["budget"]["crops_used"] == 1


def test_reset_budget_also_clears_the_read_memory(fresh, sheet):
    _meta(sheet, "C4")
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ"):
        _meta(sheet, "C4")
    _data, meta = intake.read_region(str(sheet), "C4", 2, True)   # explicit request -> fresh memory
    assert meta["budget"]["crops_used"] == 1
    sha = intake.hashlib.sha256(sheet.read_bytes()).hexdigest()
    assert len(intake.crops_read(sha)) == 1


# ---------------------------------------------------------------------------------------------
# 2. Several regions in one call
# ---------------------------------------------------------------------------------------------
def test_a_batch_returns_one_image_per_region_and_one_json_line(fresh, sheet):
    blocks, line = intake.result_batch(str(sheet), ["B2", "C4", "D3"], 3)
    assert len(blocks) == 3
    payload = json.loads(line)
    assert payload["status"] == "completed"
    assert payload["requested"] == 3 and payload["returned"] == 3
    assert [r["region"]["label"] for r in payload["regions"]] == ["B2", "C4", "D3"]
    assert all(r["status"] == "completed" for r in payload["regions"])
    assert all(Path(r["output_path"]).is_file() for r in payload["regions"])


def test_every_region_in_a_batch_is_charged_against_the_budget(fresh, sheet):
    _blocks, line = intake.result_batch(str(sheet), ["B2", "C4", "D3"], 2)
    payload = json.loads(line)
    assert payload["budget"]["crops_used"] == 3
    assert _meta(sheet, "E5")["budget"]["crops_used"] == 4


def test_a_batch_stops_at_the_budget_and_still_returns_the_rest(fresh, sheet, monkeypatch):
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "2")
    blocks, line = intake.result_batch(str(sheet), ["B2", "C4", "D3"], 2)
    payload = json.loads(line)
    assert len(blocks) == 2                   # the two the budget paid for
    assert payload["regions"][2]["error"]["code"] == "INTAKE_BUDGET_EXHAUSTED"


def test_a_mixed_batch_reports_the_refused_region_and_returns_the_others(fresh, sheet):
    _meta(sheet, "C4")                        # C4 is now already read
    blocks, line = intake.result_batch(str(sheet), ["B2", "C4", "D3"], 2)
    payload = json.loads(line)
    assert len(blocks) == 2                   # B2 and D3 still returned
    by_index = {r["index"]: r for r in payload["regions"]}
    refused = by_index[1]
    assert refused["status"] == "refused"
    assert refused["error"]["code"] == "REGION_ALREADY_READ"
    assert "UNKNOWN" in refused["error"]["message"]
    # the label travels back inside the region object for the served ones
    assert by_index[0]["region"]["label"] == "B2"
    assert by_index[2]["region"]["label"] == "D3"
    assert by_index[0]["status"] == "completed" and by_index[2]["status"] == "completed"


def test_a_batch_is_capped_at_twelve_regions(fresh, sheet):
    with pytest.raises(RuntimeError, match="INVALID_REGIONS"):
        intake.read_regions(str(sheet), ["A1"] * 13, 2)
    with pytest.raises(RuntimeError, match="INVALID_REGIONS"):
        intake.read_regions(str(sheet), [], 2)


def test_the_batch_and_the_single_call_share_the_memory(fresh, sheet):
    # a region read in a batch must be refused by a later single call, and vice versa
    intake.result_batch(str(sheet), ["C4"], 2)
    with pytest.raises(RuntimeError, match="REGION_ALREADY_READ"):
        _meta(sheet, "C4")
    _meta(sheet, "B2")
    _blocks, line = intake.result_batch(str(sheet), ["B2"], 2)
    assert json.loads(line)["regions"][0]["error"]["code"] == "REGION_ALREADY_READ"
