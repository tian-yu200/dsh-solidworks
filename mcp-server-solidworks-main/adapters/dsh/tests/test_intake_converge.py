"""Reading strategy does not impose image-global quotas; resources stay bounded."""
import inspect
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from PIL import Image
import intake


@pytest.fixture
def sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "INTAKE_ROOT", tmp_path / "artifacts/intake")
    source = tmp_path / "sheet.png"
    Image.new("RGB", (1200, 900), "white").save(source)
    return str(source)


def test_same_region_is_available_without_global_counter(sheet):
    for _ in range(20):
        assert intake.read_region(sheet, "C4")[1]["status"] == "completed"
    assert set(intake.read_region(sheet)[1]["budget"]) == {"max_pixels", "max_bytes"}
    for name in ("_CROPS_USED", "_CROP_MEMORY", "_READ_ROUNDS"):
        assert not hasattr(intake, name)


def test_single_and_batch_can_mix_in_any_order(sheet):
    for _ in range(4):
        assert intake.read_region(sheet, "C4")[1]["region"]["label"] == "C4"
        blocks, line = intake.result_batch(sheet, ["B2", "C4", "D3"], 3)
        data = json.loads(line)
        assert len(blocks) == data["returned"] == 3
        assert data["status"] == "completed"
        assert [r["region"]["label"] for r in data["regions"]] == ["B2", "C4", "D3"]
        assert all(Path(r["output_path"]).is_file() for r in data["regions"])


def test_batch_decodes_and_hashes_once(sheet, monkeypatch):
    counts = [0, 0]
    original_open, original_hash = intake._open_source, intake.hashlib.sha256
    def opened(*args):
        counts[0] += 1
        return original_open(*args)
    def hashed(*args):
        counts[1] += 1
        return original_hash(*args)
    monkeypatch.setattr(intake, "_open_source", opened)
    monkeypatch.setattr(intake.hashlib, "sha256", hashed)
    assert len(intake.result_batch(sheet, ["A1", "B2", "C4"])[0]) == 3
    assert counts == [1, 1]


def test_invalid_sibling_does_not_discard_valid_crops(sheet):
    blocks, line = intake.result_batch(sheet, ["A1", "Z9", "C4"])
    data = json.loads(line)
    assert len(blocks) == 2 and data["status"] == "partial"
    assert data["regions"][1]["error"]["code"] == "INVALID_REGION"
    assert data["regions"][2]["region"]["label"] == "C4"


@pytest.mark.parametrize("regions", [[], ["A1"] * 13, "A1", None])
def test_batch_resource_count_stays_bounded(sheet, regions):
    with pytest.raises(RuntimeError, match="INVALID_REGIONS"):
        intake.read_regions(sheet, regions)


def test_model_cannot_reset_read_state(sheet):
    for fn in [intake.read_region, intake.read_regions, intake.result_payload, intake.result_batch]:
        assert "reset_budget" not in inspect.signature(fn).parameters
        assert "enforce_rounds" not in inspect.signature(fn).parameters
    with pytest.raises(TypeError):
        intake.read_region(sheet, reset_budget=True)


def test_parallel_independent_reads_do_not_compete_for_global_rounds(sheet):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda region: intake.read_region(sheet, region)[1], ["A1", "B1", "C1", "D1"]))
    assert all(row["status"] == "completed" for row in results)
