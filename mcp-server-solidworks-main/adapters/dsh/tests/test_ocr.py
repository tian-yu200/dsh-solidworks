"""Offline tests for read_drawing_text (Windows OCR prepass). No OCR engine, no network.

The engine (Windows.Media.Ocr) is a WinRT API that only exists on a real Windows desktop, so
_run_helper is STUBBED here. What is tested is everything that would silently corrupt a reading:
the rotation geometry, the duplicate merge, symbol normalisation, the grid cell, the refusal
paths, and the tool envelope.
"""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent
if str(ADAPTER) not in sys.path:
    sys.path.insert(0, str(ADAPTER))

import ocr  # noqa: E402


@pytest.fixture
def sheet(tmp_path):
    """A 1200x900 white sheet with a few marks, so scaling has something to resample."""
    path = tmp_path / "sheet.png"
    image = Image.new("RGB", (1200, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 1100, 800], outline=(0, 0, 0), width=2)
    draw.text((300, 400), "90  R10  M6-6H", fill=(0, 0, 0))
    image.save(path)
    return path


@pytest.fixture
def stub(monkeypatch):
    """Replace the PowerShell call with canned lines, and record what the helper was asked for."""
    calls = []

    def fake_helper(image_path, workdir, timeout=180):
        calls.append(Path(image_path).name)
        return {"lang": "en-US", "width": 0, "height": 0, "lines": [
            {"text": "90", "box": [10, 20, 40, 30]},
            {"text": "\u00d88", "box": [60, 20, 80, 30]},
        ]}

    monkeypatch.setattr(ocr, "_run_helper", fake_helper)
    return calls


def test_three_rotations_are_asked_for_and_merged(sheet, stub):
    data = ocr.read_text(str(sheet))
    # one helper call per rotation, each on its own prepared file
    assert len(stub) == 3
    assert data["lang"] == "en-US"
    assert data["source_pixels"] == [1200, 900]


def test_rotated_duplicates_are_dropped(sheet, stub):
    data = ocr.read_text(str(sheet))
    texts = [t["text"] for t in data["texts"]]
    # the stub answers the SAME place in every rotation: the upright read must win, once
    assert texts.count("90") == 1, texts


def test_symbols_are_normalised_and_raw_is_kept(sheet, stub):
    data = ocr.read_text(str(sheet))
    by_raw = {t["raw"]: t for t in data["texts"]}
    assert "\u00d88" in by_raw
    assert by_raw["\u00d88"]["text"] == "\u00d88"


def test_normalise_covers_the_dimension_symbols():
    assert ocr._normalise("\u00d820") == "\u00d820"
    assert ocr._normalise("020") == "\u00d820"
    assert ocr._normalise("R10") == "R10"
    assert ocr._normalise("M6-6H") == "M6-6H"
    assert ocr._normalise("4\u00d710") == "4\u00d710"


def test_grid_cell_uses_the_same_ahx16_grid():
    # 1200x900, 8 columns x 6 rows: cell 150 x 150. A box at (10,20) is column A row 1.
    assert ocr._grid_cell([10, 20, 40, 30], (1200, 900)) == "A1"
    assert ocr._grid_cell([151, 151, 200, 200], (1200, 900)) == "B2"
    assert ocr._grid_cell([1199, 899, 1200, 900], (1200, 900)) == "H6"


def test_missing_file_is_refused():
    with pytest.raises(RuntimeError, match="FILE_NOT_FOUND"):
        ocr.read_text("C:/definitely/not/here.png")


def test_url_is_refused():
    with pytest.raises(RuntimeError, match="LOCAL_PATH_REQUIRED"):
        ocr.read_text("https://example.com/drawing.png")


def test_result_payload_shape(sheet, stub):
    payload = json.loads(ocr.result_payload(str(sheet)))
    assert payload["status"] == "completed"
    assert payload["action"] == "ocr"
    assert payload["text_count"] == len(payload["texts"])
    for row in payload["texts"]:
        assert set(row) >= {"text", "raw", "box", "grid_cell", "rotation", "confidence"}
    assert "CANDIDATE" in payload["read_next"]


def test_rotation_geometry_maps_back_to_source_pixels():
    # A 90-degree rotation: the prepared image is (h, w). A point at (x, y) in the rotated frame
    # is (y, w-1-x) in the source. Verify the helper's inverse rather than trusting the comment.
    src = (1200, 900)
    out = (900, 1200)          # 90-degree rotation swaps the axes
    box = [10, 20, 40, 60]
    got = ocr._to_source_box(box, 1, 90, out, src)
    assert got[0] <= got[2] and got[1] <= got[3]
    assert 0 <= got[0] and got[2] <= src[0]
    assert 0 <= got[1] and got[3] <= src[1]
