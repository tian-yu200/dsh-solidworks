import pytest
from PIL import Image

from drawing_comparison import (
    FALLBACK_PANELS,
    LANDSCAPE_PANELS,
    PORTRAIT_PANELS,
    REQUIRED_VIEWS,
    SHEET_SIZE,
    create_comparison_sheet,
)


def _solid_image(path, color, size=(120, 80)):
    Image.new("RGB", size, color).save(path)
    return path


def _views(tmp_path):
    colors = {
        "front": (220, 40, 40),
        "top": (40, 180, 60),
        "left": (40, 80, 220),
        "isometric": (190, 40, 190),
    }
    paths = {name: _solid_image(tmp_path / (name + ".png"), color)
             for name, color in colors.items()}
    return paths, colors


def _assert_panel_color(sheet, panels, name, color):
    x0, y0, x1, y1 = panels[name]
    assert sheet.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == color


def test_landscape_drawing_keeps_the_sheet_landscape_with_first_angle_views(tmp_path):
    drawing = _solid_image(tmp_path / "drawing-landscape.png", (230, 210, 40), (1200, 800))
    views, colors = _views(tmp_path)
    output = tmp_path / "landscape.png"

    create_comparison_sheet(drawing, views, output)

    with Image.open(output) as sheet:
        assert sheet.size == SHEET_SIZE  # a square sheet is shrunk much harder by the model's image pipeline
        _assert_panel_color(sheet, LANDSCAPE_PANELS, "drawing", (230, 210, 40))
        for name, color in colors.items():
            _assert_panel_color(sheet, LANDSCAPE_PANELS, name, color)
    assert LANDSCAPE_PANELS["drawing"][2] < LANDSCAPE_PANELS["front"][0]
    assert LANDSCAPE_PANELS["top"][1] > LANDSCAPE_PANELS["front"][3]
    first_row_bottom = max(LANDSCAPE_PANELS["front"][3], LANDSCAPE_PANELS["left"][3])
    assert LANDSCAPE_PANELS["top"][1] - first_row_bottom >= 60
    assert LANDSCAPE_PANELS["left"][0] > LANDSCAPE_PANELS["front"][2]
    assert max(SHEET_SIZE) == 1568


def test_views_are_trimmed_to_the_part_before_fitting(tmp_path):
    # A render is the whole graphics window; the part may fill a corner of it.
    views, _ = _views(tmp_path)
    window = Image.new("RGB", (400, 300), (240, 242, 246))
    window.paste((0, 0, 0), (10, 10, 50, 40))
    window.save(views["front"])
    output = tmp_path / "trimmed.png"

    create_comparison_sheet(None, views, output)

    with Image.open(output) as sheet:
        x0, y0, x1, y1 = FALLBACK_PANELS["front"]
        assert sheet.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == (0, 0, 0)


def test_portrait_drawing_uses_left_panel_and_first_angle_views(tmp_path):
    drawing = _solid_image(tmp_path / "drawing-portrait.png", (230, 210, 40), (800, 1200))
    views, colors = _views(tmp_path)
    output = tmp_path / "portrait.png"

    create_comparison_sheet(drawing, views, output)

    with Image.open(output) as sheet:
        assert sheet.size == SHEET_SIZE
        _assert_panel_color(sheet, PORTRAIT_PANELS, "drawing", (230, 210, 40))
        for name, color in colors.items():
            _assert_panel_color(sheet, PORTRAIT_PANELS, name, color)
    assert PORTRAIT_PANELS["drawing"][2] < PORTRAIT_PANELS["front"][0]
    assert PORTRAIT_PANELS["top"][1] > PORTRAIT_PANELS["front"][3]
    first_row_bottom = max(PORTRAIT_PANELS["front"][3], PORTRAIT_PANELS["left"][3])
    assert PORTRAIT_PANELS["top"][1] - first_row_bottom >= 60
    assert PORTRAIT_PANELS["left"][0] > PORTRAIT_PANELS["front"][2]


@pytest.mark.parametrize("drawing_path", [None, "missing-drawing.png"])
def test_missing_drawing_uses_four_view_2x2_fallback(tmp_path, drawing_path):
    views, colors = _views(tmp_path)
    output = tmp_path / "fallback.png"

    create_comparison_sheet(drawing_path, views, output)

    with Image.open(output) as sheet:
        assert sheet.size == SHEET_SIZE
        for name, color in colors.items():
            _assert_panel_color(sheet, FALLBACK_PANELS, name, color)
    assert FALLBACK_PANELS["top"][1] - FALLBACK_PANELS["front"][3] >= 60
    assert FALLBACK_PANELS["isometric"][1] - FALLBACK_PANELS["left"][3] >= 60


def test_comparison_sheet_requires_exact_render_view_set(tmp_path):
    drawing = _solid_image(tmp_path / "drawing.png", "white")
    with pytest.raises(ValueError, match="exactly"):
        create_comparison_sheet(drawing, {"front": drawing}, tmp_path / "out.png")


def test_comparison_sheet_rejects_missing_view_file(tmp_path):
    drawing = _solid_image(tmp_path / "drawing.png", "white")
    views = {name: drawing for name in REQUIRED_VIEWS}
    views["left"] = tmp_path / "missing.png"

    with pytest.raises(FileNotFoundError):
        create_comparison_sheet(drawing, views, tmp_path / "out.png")
