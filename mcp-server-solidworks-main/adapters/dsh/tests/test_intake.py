"""Offline tests for read_drawing_region — synthetic images, NO SolidWorks, no network.

The tool exists because small dimension text on a whole-sheet drawing is unreadable inside the
model's image budget, and the SolidWorks preset forbids shell. These cover the contract the
drawing-intake procedure relies on: the overview grid and its cell map, cell and pixel-box crops,
zoom bounds, the output budget, local-path-only, and the loud rejections (nothing is clipped or
invented silently).
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
def sheet(tmp_path):
    """A 1200x900 white 'drawing': a border, a hatch, and one line of dimension-like text."""
    path = tmp_path / "sheet.png"
    image = Image.new("RGB", (1200, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 900, 800], outline=(0, 0, 0), width=2)
    for x in range(100, 901, 40):
        draw.line([(x, 100), (x, 800)], fill=(20, 20, 20))
    draw.text((300, 400), "28x8  R100  1060 Alloy", fill=(0, 0, 0))
    image.save(path)
    return path


def _meta(image_path, region="", zoom=2):
    _data, meta = intake.read_region(image_path, region, zoom)
    return meta


def test_overview_reports_the_grid_and_is_written_under_the_root(intake_root, sheet):
    meta = _meta(str(sheet))
    assert meta["status"] == "completed"
    assert meta["action"] == "overview"
    assert meta["source_pixels"] == [1200, 900]
    # 8 columns x 6 rows over the sheet: cells are 150x150 source px.
    assert meta["grid"]["columns"] == intake.GRID_COLUMNS
    assert meta["grid"]["rows"] == intake.GRID_ROWS
    assert meta["grid"]["cell_pixels"] == [150.0, 150.0]
    assert meta["region"] is None
    out = meta["output_path"]
    assert out.startswith(str(intake_root))
    assert "overview" in out
    with Image.open(out) as produced:
        assert produced.size == (1200, 900)          # fits the budget at 1:1
        assert produced.mode in ("RGB", "P")


def test_overview_shows_the_grid_lines_in_the_image(intake_root, sheet):
    meta = _meta(str(sheet))
    with Image.open(meta["output_path"]) as produced:
        image = produced.convert("RGB")
        width, height = image.size
        # x=150 and y=150 are column/row dividers; the source is white and unhatched there, so the
        # grey can only come from the overlay. A crop is drawn clean, never overlaid.
        assert image.getpixel((150, height // 2)) != (255, 255, 255)
        assert image.getpixel((width // 2, 150)) != (255, 255, 255)
    crop = _meta(str(sheet), "B1")
    with Image.open(crop["output_path"]) as produced:
        clean = produced.convert("RGB")
        # Same source column as the overview's divider, but a crop carries no overlay at all.
        assert clean.getpixel((0, clean.height // 8)) == (255, 255, 255)


def test_cell_label_resolves_to_its_source_box(intake_root, sheet):
    meta = _meta(str(sheet), "C4")
    assert meta["action"] == "cell"
    # column C = index 2 of 8, row 4 = index 3 of 6, on 150 px cells.
    assert meta["region"]["source_box"] == [300, 450, 450, 600]
    assert meta["region"]["label"] == "C4"


def test_zoom_enlarges_a_crop(intake_root, sheet):
    meta = _meta(str(sheet), "C4", 4)
    assert meta["zoom"] == 4
    assert meta["output_pixels"] == [600, 600]
    assert meta["scale"] == 4.0
    assert meta["scale_basis"] == "region"


def test_lower_case_cell_and_padding_are_accepted(intake_root, sheet, fresh_budget):
    assert _meta(str(sheet), " c4 ")["region"]["source_box"] == [300, 450, 450, 600]


def test_box_region_uses_source_pixels(intake_root, sheet):
    meta = _meta(str(sheet), "100,100,300,250")
    assert meta["action"] == "box"
    assert meta["region"]["source_box"] == [100, 100, 300, 250]
    assert meta["output_pixels"] == [400, 300]     # 200x150 at zoom 2


def test_zoom_is_bounded(intake_root, sheet):
    for bad in (0, 5, -1, True, "2"):
        with pytest.raises(RuntimeError, match="INVALID_ZOOM"):
            intake.read_region(str(sheet), "A1", bad)


def test_oversized_image_is_refused_before_any_pixel_is_decoded(intake_root, tmp_path, monkeypatch):
    # The cap has to come from the HEADER: a 100 MP PNG is only a few MB on disk and would decode
    # to hundreds of MB in this process before the old check could reject it.
    path = tmp_path / "small-file.png"
    Image.new("RGB", (10, 10), (255, 255, 255)).save(path)
    reached = []

    class HeaderOnly(object):
        size = (10000, 8000)

        def load(self):
            reached.append("load")
            raise AssertionError("load() must never run for an oversized image")

        def convert(self, mode):                    # pragma: no cover - load() fails first
            reached.append("convert")
            raise AssertionError("convert() must never run either")

    monkeypatch.setattr(intake.Image, "open", lambda *a, **k: HeaderOnly())
    with pytest.raises(RuntimeError, match="IMAGE_TOO_LARGE"):
        intake.read_region(str(path))
    assert reached == []


def test_oversized_file_is_refused_before_it_is_even_opened(intake_root, tmp_path, monkeypatch):
    path = tmp_path / "fat.png"
    path.write_bytes(b"padding" * 500)
    monkeypatch.setattr(intake, "MAX_SOURCE_BYTES", 64)

    def boom(*_a, **_k):
        raise AssertionError("the file must not be opened once its size is known to be too large")

    monkeypatch.setattr(intake.Image, "open", boom)
    with pytest.raises(RuntimeError, match="IMAGE_FILE_TOO_LARGE"):
        intake.read_region(str(path))


def test_clamped_output_size_never_exceeds_the_pixel_cap():
    # 201x4976 at zoom 2 rounds each axis to 402x9951 = 4,000,302 px — over the cap by 302, which
    # is exactly what an independent per-axis round produces on a tall, narrow crop.
    for (w, h, z) in [(201, 4976, 2), (4976, 201, 2), (1, 60_000_000 // 1, 1), (2000, 2000, 4)]:
        out_w, out_h = intake._clamped_output_size(w, h, z)
        assert out_w >= 1 and out_h >= 1
        assert out_w * out_h <= intake.MAX_OUTPUT_PIXELS, (w, h, z, out_w, out_h)


def test_near_boundary_crop_is_clamped_not_over_budget(intake_root, tmp_path):
    path = tmp_path / "tall.png"
    Image.new("RGB", (201, 4976), (255, 255, 255)).save(path)
    meta = _meta(str(path), "0,0,201,4976", 2)
    out_w, out_h = meta["output_pixels"]
    assert out_w * out_h <= intake.MAX_OUTPUT_PIXELS
    assert meta["scale"] < 2.0                      # the delivered scale, reported honestly
    assert meta["output_bytes"] <= intake.MAX_OUTPUT_BYTES


def test_output_stays_inside_the_image_budget(intake_root, tmp_path):
    # 5000x5000 = 25 Mpx, inside the decode cap but 6x the model's pixel budget.
    path = tmp_path / "big.png"
    Image.new("RGB", (5000, 5000), (255, 255, 255)).save(path)
    meta = _meta(str(path))
    assert meta["output_pixels"][0] * meta["output_pixels"][1] <= intake.MAX_OUTPUT_PIXELS
    assert meta["output_bytes"] <= intake.MAX_OUTPUT_BYTES
    assert meta["scale"] < 1.0


def test_palette_fallback_keeps_an_uncompressible_image_inside_the_byte_cap(
    intake_root, tmp_path
):
    # Random noise does not compress: the RGB encode busts 1 MiB and the adaptive palette is what
    # keeps the crop deliverable instead of failing it.
    import random

    rng = random.Random(7)
    image = Image.new("L", (1000, 1000))
    image.putdata([rng.randrange(256) for _ in range(1000 * 1000)])
    path = tmp_path / "noise.png"
    image.convert("RGB").save(path)
    meta = _meta(str(path))
    assert meta["output_bytes"] <= intake.MAX_OUTPUT_BYTES
    assert meta["output_mode"] in ("RGB", "P")


def test_urls_are_rejected(intake_root, sheet):
    for url in ("https://example.com/a.png", "http://x/y.png", "file:///C:/a.png",
                "ftp://h/a.png"):
        with pytest.raises(RuntimeError, match="LOCAL_PATH_REQUIRED"):
            intake.read_region(url)


def test_missing_file_and_directory_are_reported(intake_root, tmp_path):
    with pytest.raises(RuntimeError, match="FILE_NOT_FOUND"):
        intake.read_region(str(tmp_path / "nope.png"))
    with pytest.raises(RuntimeError, match="NOT_A_FILE"):
        intake.read_region(str(tmp_path))
    with pytest.raises(RuntimeError, match="MISSING_PARAMETER"):
        intake.read_region("   ")


def test_oversized_image_is_refused_before_decoding(intake_root, tmp_path, monkeypatch):
    path = tmp_path / "big-image.png"
    Image.new("RGB", (40, 10), (255, 255, 255)).save(path)
    monkeypatch.setattr(intake, "MAX_SOURCE_PIXELS", 100)
    with pytest.raises(RuntimeError, match="IMAGE_TOO_LARGE"):
        intake.read_region(str(path))


def test_region_outside_the_image_is_rejected_not_clipped(intake_root, sheet):
    with pytest.raises(RuntimeError, match="REGION_OUT_OF_IMAGE"):
        intake.read_region(str(sheet), "1000,100,1300,400")
    with pytest.raises(RuntimeError, match="REGION_OUT_OF_IMAGE"):
        intake.read_region(str(sheet), "-10,0,100,100")


def test_reversed_and_malformed_regions_are_rejected(intake_root, sheet):
    for bad in ("300,300,100,100", "J9", "A7", "1,2,3", "hello"):
        with pytest.raises(RuntimeError, match="INVALID_REGION"):
            intake.read_region(str(sheet), bad)


def test_a_region_with_no_area_is_rejected(intake_root, sheet):
    with pytest.raises(RuntimeError, match="REGION_TOO_SMALL"):
        intake.read_region(str(sheet), "0,0,4,4")


def test_crops_of_one_drawing_share_one_hashed_folder(intake_root, sheet):
    first = _meta(str(sheet), "A1")
    second = _meta(str(sheet), "H6", 3)
    assert Path(first["output_path"]).parent == Path(second["output_path"]).parent
    assert Path(first["output_path"]).parent == intake_root / first["source_sha256_16"]


def test_relative_path_resolves_against_the_workspace_root(tmp_path, monkeypatch):
    # INTAKE_ROOT is <workspace>/artifacts/intake, so a bare filename means <workspace>/name.
    monkeypatch.setattr(intake, "INTAKE_ROOT", tmp_path / "artifacts" / "intake")
    Image.new("RGB", (200, 200), (255, 255, 255)).save(tmp_path / "shot.png")
    meta = _meta("shot.png")
    assert meta["image_path"].endswith("shot.png")
    assert tmp_path.as_posix() in Path(meta["image_path"]).as_posix()


def test_tool_returns_an_image_block_and_one_line_of_json(intake_root, sheet):
    payload, text = intake.result_payload(str(sheet), "B2", 3)
    content = payload.to_image_content()
    assert content.type == "image"
    assert content.mime_type == "image/png"
    assert content.data and "\n" not in text
    meta = json.loads(text)
    assert meta["status"] == "completed"
    assert meta["zoom"] == 3


def test_the_tool_wraps_a_refusal_as_a_typed_envelope(tmp_path, monkeypatch, sheet):
    # What DSH shows for a bad region must be the same compact failure shape every other tool
    # returns — not a stack trace, and not an error the model reads as a CAD problem.
    from importlib import util as _util

    monkeypatch.setattr(intake, "INTAKE_ROOT", tmp_path / "artifacts" / "intake")
    spec = _util.spec_from_file_location(
        "dsh_intake_server_under_test", Path(intake.__file__).parent / "server.py")
    server = _util.module_from_spec(spec)
    spec.loader.exec_module(server)

    text = server._image_result(lambda: intake.result_payload(str(sheet), "Z9"))
    assert isinstance(text, str)
    payload = json.loads(text)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "INVALID_REGION"


def test_grid_geometry_covers_every_cell_of_a_non_square_sheet(intake_root, tmp_path,
                                                               fresh_budget, monkeypatch):
    # 48 crops is a geometry sweep, not a reading session: lift the budget so the test measures
    # the grid, and let the budget tests own the limit.
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "100")
    path = tmp_path / "wide.png"
    Image.new("RGB", (1680, 1107), (255, 255, 255)).save(path)
    for column in intake.GRID_COLUMNS:
        for row in range(1, intake.GRID_ROWS + 1):
            meta = _meta(str(path), f"{column}{row}")
            x0, y0, x1, y1 = meta["region"]["source_box"]
            assert 0 <= x0 < x1 <= 1680
            assert 0 <= y0 < y1 <= 1107
            assert x1 - x0 >= 1 and y1 - y0 >= 1


def test_overview_of_a_landscape_sheet_labels_the_grid_in_order(intake_root, tmp_path):
    # A1 must be the top-left cell: the crop of A1 sits at the origin, H6 at the bottom-right.
    path = tmp_path / "sheet2.png"
    Image.new("RGB", (800, 600), (255, 255, 255)).save(path)
    assert _meta(str(path), "A1")["region"]["source_box"] == [0, 0, 100, 100]
    assert _meta(str(path), "H6")["region"]["source_box"] == [700, 500, 800, 600]


# ---------------------------------------------------------------------------------------------
# The crop budget. A wall-clock limit is unenforceable for a model that has no clock, so reading is
# bounded by a COUNT per drawing, reported in every response, and refused loudly at the limit.
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def fresh_budget(monkeypatch):
    """Isolate the per-process counter and the limit, so tests never see each other's crops."""
    monkeypatch.setattr(intake, "_CROPS_USED", {})
    # The crop MEMORY (repeat detection) is per-process too: without this, a crop served by one
    # test would look "already read" to the next one.
    monkeypatch.setattr(intake, "_CROP_MEMORY", {})
    monkeypatch.delenv(intake.CROP_LIMIT_ENV, raising=False)
    return intake._CROPS_USED


def test_crops_are_counted_and_reported(intake_root, sheet, fresh_budget):
    assert _meta(str(sheet), "B2")["budget"]["crops_used"] == 1
    second = _meta(str(sheet), "C3")["budget"]
    assert second["crops_used"] == 2
    assert second["crop_limit"] == intake.CROP_LIMIT_DEFAULT == 15
    # the pixel/byte budget is still there — this only adds keys
    assert second["max_pixels"] == intake.MAX_OUTPUT_PIXELS
    assert second["max_bytes"] == intake.MAX_OUTPUT_BYTES


def test_overview_is_exempt_from_the_budget(intake_root, sheet, fresh_budget):
    for _ in range(5):
        meta = _meta(str(sheet))
        assert meta["action"] == "overview"
        assert meta["budget"]["crops_used"] == 0
    assert fresh_budget == {}                       # nothing charged, not even an entry
    # and the overview still reports the limit, so the model can plan against it
    assert _meta(str(sheet))["budget"]["crop_limit"] == 15


def test_crops_are_refused_once_the_limit_is_reached(intake_root, sheet, fresh_budget,
                                                     monkeypatch):
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "2")
    assert _meta(str(sheet), "B2")["budget"]["crops_used"] == 1
    third = _meta(str(sheet), "C3")
    assert third["budget"]["crops_used"] == 2
    with pytest.raises(RuntimeError, match="INTAKE_BUDGET_EXHAUSTED") as raised:
        _meta(str(sheet), "D4")
    message = str(raised.value)
    # the refusal must send the model to the plan, not back at the image
    assert "write PLAN.md v1 now" in message
    assert "UNKNOWN" in message and "ask the user" in message
    # a refused call must not charge anything further
    assert fresh_budget[intake.hashlib.sha256(sheet.read_bytes()).hexdigest()] == 2


def test_the_budget_is_per_drawing(intake_root, tmp_path, fresh_budget, monkeypatch):
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "1")
    one = tmp_path / "one.png"
    two = tmp_path / "two.png"
    Image.new("RGB", (600, 400), (255, 255, 255)).save(one)
    Image.new("RGB", (600, 400), (250, 250, 250)).save(two)
    assert _meta(str(one), "A1")["budget"]["crops_used"] == 1
    with pytest.raises(RuntimeError, match="INTAKE_BUDGET_EXHAUSTED"):
        _meta(str(one), "B2")
    # the exhausted drawing must not block a different sheet
    other = _meta(str(two), "A1")["budget"]
    assert other["crops_used"] == 1 and other["crop_limit"] == 1


def test_reset_budget_clears_the_counter_and_the_env_sets_the_limit(
        intake_root, sheet, fresh_budget, monkeypatch):
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "1")
    assert _meta(str(sheet), "B2")["budget"]["crops_used"] == 1
    with pytest.raises(RuntimeError, match="INTAKE_BUDGET_EXHAUSTED"):
        _meta(str(sheet), "C3")
    # explicit reset (the docs allow it only on the user's request) zeroes it BEFORE the call,
    # so the call that resets is itself charged — one crop, not zero
    _data, meta = intake.read_region(str(sheet), "C3", 2, True)
    assert meta["budget"]["crops_used"] == 1
    with pytest.raises(RuntimeError, match="INTAKE_BUDGET_EXHAUSTED"):
        _meta(str(sheet), "D4")
    # a malformed override must not disable the budget
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "not-a-number")
    assert intake.crop_limit() == intake.CROP_LIMIT_DEFAULT
    monkeypatch.setenv(intake.CROP_LIMIT_ENV, "0")
    assert intake.crop_limit() == intake.CROP_LIMIT_DEFAULT
