"""Build a drawing-and-view sheet for first-angle visual comparison."""

from pathlib import Path
from typing import Mapping

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


# One size for every layout. A model's image pipeline shrinks a big square far more than a
# 1568x980 sheet (to ~0.5-0.7 of its side instead of ~0.8-0.9), and the views are the only
# picture of the part the model gets, so a landscape drawing does not get a taller sheet.
SHEET_SIZE = (1568, 980)
REQUIRED_VIEWS = ("front", "top", "left", "isometric")
FALLBACK_PANELS = {
    "front": (28, 90, 772, 470),
    "top": (28, 550, 772, 952),
    "left": (796, 90, 1540, 470),
    "isometric": (796, 550, 1540, 952),
}
PORTRAIT_PANELS = {
    "drawing": (28, 90, 540, 952),
    "front": (568, 90, 1046, 470),
    "left": (1068, 90, 1540, 470),
    "top": (568, 550, 1046, 952),
    "isometric": (1068, 550, 1540, 952),
}
LANDSCAPE_PANELS = {
    "drawing": (28, 90, 700, 952),
    "front": (724, 90, 1124, 470),
    "left": (1140, 90, 1540, 470),
    "top": (724, 550, 1124, 952),
    "isometric": (1140, 550, 1540, 952),
}
INK_LUMINANCE = 185  # rendered edges are black or grey (hidden); the view background is 210-255
LABELS = {
    "drawing": "工程图 DRAWING",
    "front": "正视 FRONT",
    "top": "俯视 TOP",
    "left": "左视 LEFT",
    "isometric": "轴测 ISOMETRIC",
}


def _font(size):
    candidates = (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "arial.ttf",
        "DejaVuSans.ttf",
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _load_rgb(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    with Image.open(path) as source:
        source.load()
        return source.convert("RGB")


def _crop_to_content(image, margin=0.06):
    """Trim the empty graphics-area background around the part, keeping a small margin.

    A rendered view is the whole graphics window; the part often fills a third of it, so
    without this most of each panel is background."""
    box = image.convert("L").point(lambda v: 255 if v < INK_LUMINANCE else 0).getbbox()
    if not box:
        return image
    pad = max(8, int(margin * max(box[2] - box[0], box[3] - box[1])))
    return image.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                       min(image.width, box[2] + pad), min(image.height, box[3] + pad)))


def _strengthen_lines(image):
    # Rendered CAD edges are thin grey strokes; a small min-filter keeps them visible after scaling.
    return image.filter(ImageFilter.MinFilter(3))


def _paste_panel(canvas, image, rect, label, font, *, strengthen=False):
    draw = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = rect
    draw.text((x0 + 6, y0 - 34), label, fill=(28, 36, 42), font=font)
    draw.rectangle(rect, fill=(250, 251, 252), outline=(154, 164, 172), width=2)

    inset = 12
    box = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
    fitted = ImageOps.contain(
        _strengthen_lines(_crop_to_content(image)) if strengthen else image,
        (box[2] - box[0], box[3] - box[1]),
        method=Image.Resampling.LANCZOS,
    )
    x = box[0] + (box[2] - box[0] - fitted.width) // 2
    y = box[1] + (box[3] - box[1] - fitted.height) // 2
    canvas.paste(fitted, (x, y))


def create_comparison_sheet(drawing_path, view_paths: Mapping[str, str], output_path):
    """Create a 1568px-long-edge PNG, falling back to a 2x2 view sheet without a drawing.

    ``view_paths`` maps front, top, left and isometric to the PNG paths returned by
    ``render_views``. Orthographic images keep their aspect ratios; no engineering scale
    is implied. An absent or missing drawing path selects the four-view layout.
    """
    if not isinstance(view_paths, Mapping) or set(view_paths) != set(REQUIRED_VIEWS):
        raise ValueError("view_paths must contain exactly: %s" % ", ".join(REQUIRED_VIEWS))

    images = {name: _load_rgb(view_paths[name]) for name in REQUIRED_VIEWS}
    drawing = None
    if drawing_path:
        try:
            drawing = _load_rgb(drawing_path)
        except (OSError, ValueError):
            drawing = None

    if drawing is None:
        size, panels = SHEET_SIZE, FALLBACK_PANELS
    elif drawing.width < drawing.height * 1.15:
        size, panels = SHEET_SIZE, PORTRAIT_PANELS
    else:
        size, panels = SHEET_SIZE, LANDSCAPE_PANELS

    canvas = Image.new("RGB", size, "white")
    font = _font(24)
    if drawing is not None:
        _paste_panel(canvas, drawing, panels["drawing"], LABELS["drawing"], font)
    for name in REQUIRED_VIEWS:
        _paste_panel(canvas, images[name], panels[name], LABELS[name], font, strengthen=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path
