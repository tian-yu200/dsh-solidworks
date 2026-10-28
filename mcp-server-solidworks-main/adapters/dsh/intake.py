"""read_drawing_region — crop and enlarge part of an engineering drawing.

Reading a drawing is a vision task, and small dimension text is where it fails: a whole sheet
inside DSH's image budget renders a 3 mm-tall digit as a handful of pixels. This is the zoom the
workflow had no other way to get — the SolidWorks preset forbids shell, and modlens_read_image has
no crop. It is READ-ONLY, touches no CAD, and needs no job: it returns one PNG plus the
source-pixel box it came from, so a crop is evidence and not a guess.

Local files only. A drawing that still has to be downloaded is the user's step, not this tool's.
"""
import hashlib
import json
import math
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from mcp.server.mcpserver import Image as MCPImage

from settings import INTAKE_ROOT


# DSH downsamples any image over its per-request budget, and that rescale is exactly what turns a
# small digit into mush — so outputs stay INSIDE it and nothing is rescaled downstream
# (docs/DSH模型图片输入配置.md: requestImagePixelBudget 4 Mi px, requestImageMaxBytes 1 Mi B).
MAX_OUTPUT_PIXELS = 4_000_000
MAX_OUTPUT_BYTES = 1_000_000

MIN_ZOOM = 1
MAX_ZOOM = 4
DEFAULT_ZOOM = 2

# Fixed, so a cell label means the same thing in every call of one session: columns A.. left→right,
# rows 1.. top→bottom, over the whole sheet.
GRID_COLUMNS = "ABCDEFGH"
GRID_ROWS = 6

# Decoding is bounded BEFORE any pixel work happens: a phone photo is fine, a 100 MP scan is not.
# The byte cap comes first because it costs nothing to check, and the pixel cap is read from the
# image HEADER — Pillow's open() parses only the header, so an oversized image is refused without
# ever decompressing it (a fully decoded 100 MP RGB image is ~300 MB in this process).
MAX_SOURCE_BYTES = 64_000_000
MAX_SOURCE_PIXELS = 60_000_000
MIN_CROP_AREA = 64            # px² — below this there is no content to read, only noise

_CELL_RE = re.compile(r"^([A-Ha-h])([1-9][0-9]?)$")
# A leading minus is parsed on purpose: a negative coordinate is a well-formed box that lies
# outside the image, and that answer (REGION_OUT_OF_IMAGE) is more useful than "not a box".
_BOX_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*"
                     r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _fail(code, message):
    # server._json_result maps RuntimeError("<CODE>: …") onto a typed error code.
    raise RuntimeError(f"{code}: {message}")


def _open_source(image_path):
    """(Path, RGB image) for a LOCAL drawing file, with the decode bounded."""
    text = (image_path or "").strip()
    if not text:
        _fail("MISSING_PARAMETER", "image_path is required (a local image file path).")
    if _SCHEME_RE.match(text) or text.lower().startswith("file:"):
        _fail("LOCAL_PATH_REQUIRED",
              "image_path must be a local file path; URLs (http://, https://, file://) are not "
              "readable here. Save the drawing locally and pass that path.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        # A relative path has no knowable base from the model's side, so it resolves against the
        # workspace — which makes a typo fail the existence check instead of reading other content.
        path = _workspace_relative(text)
    if not path.exists():
        _fail("FILE_NOT_FOUND", f"no such file: {path}")
    if not path.is_file():
        _fail("NOT_A_FILE", f"not a file: {path}")
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        _fail("FILE_UNREADABLE", f"cannot read {path}: {exc}")
    if size_bytes > MAX_SOURCE_BYTES:
        _fail("IMAGE_FILE_TOO_LARGE",
              f"{path} is {size_bytes} B, over the {MAX_SOURCE_BYTES} B limit; ask for a smaller "
              "export of the drawing.")
    try:
        image = Image.open(path)          # header only: no pixel decode yet
        width, height = image.size
    except Exception as exc:  # noqa: BLE001 — Pillow raises many opaque decode errors
        _fail("IMAGE_UNREADABLE", f"{path}: {type(exc).__name__}: {exc}")
    if width * height > MAX_SOURCE_PIXELS:
        # Rejected here, before load(): the header said it cannot be handled.
        _fail("IMAGE_TOO_LARGE",
              f"{width}x{height} = {width * height} px exceeds {MAX_SOURCE_PIXELS}; ask for a "
              "smaller export of the drawing.")
    try:
        image.load()
    except Exception as exc:  # noqa: BLE001
        _fail("IMAGE_UNREADABLE", f"{path}: {type(exc).__name__}: {exc}")
    return path, image.convert("RGB")


def _workspace_relative(relative):
    """Resolve a relative path against the workspace root (the one holding state/ and backups/)."""
    return (INTAKE_ROOT.parent.parent / relative).resolve()


def grid_geometry(width, height):
    """The cell map a label is resolved against — derived, so 48 boxes are never sent as JSON."""
    cw = width / len(GRID_COLUMNS)
    ch = height / GRID_ROWS
    return {
        "columns": GRID_COLUMNS,
        "rows": GRID_ROWS,
        "cell_pixels": [round(cw, 2), round(ch, 2)],
        "how_to_name": "column letter (A left → H right) then row number (1 top → "
                       f"{GRID_ROWS} bottom), e.g. B2; or a box 'x0,y0,x1,y1' in source pixels.",
    }


def _clip_box(box, width, height):
    x0, y0, x1, y1 = box
    x0 = max(0.0, min(float(width), x0))
    x1 = max(0.0, min(float(width), x1))
    y0 = max(0.0, min(float(height), y0))
    y1 = max(0.0, min(float(height), y1))
    return (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))


def _cell_box(label, width, height):
    match = _CELL_RE.match(label.strip())
    if not match:
        _fail("INVALID_REGION",
              f"'{label}' is not a grid cell (A–H then 1–{GRID_ROWS}) or a box 'x0,y0,x1,y1'.")
    row = int(match.group(2)) - 1
    if row >= GRID_ROWS:
        _fail("INVALID_REGION", f"row {row + 1} is outside this grid (1–{GRID_ROWS}).")
    col = GRID_COLUMNS.index(match.group(1).upper())
    cw = width / len(GRID_COLUMNS)
    ch = height / GRID_ROWS
    return _clip_box((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch), width, height)


def parse_region(region, width, height):
    """(box, kind, label); box is None for the overview."""
    text = (region or "").strip()
    if not text:
        return None, "overview", None
    if _CELL_RE.match(text):
        return _cell_box(text, width, height), "cell", text.upper()
    match = _BOX_RE.match(text)
    if match:
        x0, y0, x1, y1 = (float(value) for value in match.groups())
        if x1 <= x0 or y1 <= y0:
            _fail("INVALID_REGION", f"the box must be x0,y0,x1,y1 with x1>x0 and y1>y0 (got {text}).")
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
            _fail("REGION_OUT_OF_IMAGE",
                  f"{text} lies outside the {width}x{height} source image. A region is never "
                  "clipped silently: a clipped crop hides what it left out.")
        return (int(x0), int(y0), int(x1), int(y1)), "box", text
    _fail("INVALID_REGION",
          f"'{text}' is not a grid cell (A–H then 1–{GRID_ROWS}) or a box 'x0,y0,x1,y1'.")


def _default_font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow < 10.1 takes no size
        return ImageFont.load_default()


def draw_grid(image):
    """Grey grid + cell labels, for the OVERVIEW only — a crop is drawn clean, because the overlay
    would sit on top of exactly the strokes the model is trying to read."""
    width, height = image.size
    draw = ImageDraw.Draw(image)
    cw = width / len(GRID_COLUMNS)
    ch = height / GRID_ROWS
    line = max(1, round(min(cw, ch) / 240))
    for col in range(1, len(GRID_COLUMNS)):
        draw.line([(col * cw, 0), (col * cw, height)], fill=(185, 185, 185), width=line)
    for row in range(1, GRID_ROWS):
        draw.line([(0, row * ch), (width, row * ch)], fill=(185, 185, 185), width=line)
    font = _default_font(max(12, round(ch / 8)))
    for row in range(GRID_ROWS):
        for col, letter in enumerate(GRID_COLUMNS):
            draw.text((col * cw + line * 3, row * ch + line * 3), f"{letter}{row + 1}",
                      fill=(90, 90, 90), font=font)
    return image


def _encode(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _clamped_output_size(width, height, scale):
    """Integer output dimensions at `scale`, never over MAX_OUTPUT_PIXELS.

    Rounding each axis on its own can push the PRODUCT over the cap (a 201x4976 crop at zoom 2
    rounds to 402x9951 = 4,000,302 px). The scale is first reduced to the largest the cap allows
    for THIS aspect ratio, then both axes are floored: floor(w*s)*floor(h*s) <= w*h*s^2 <= cap
    holds by construction, so no iteration is needed and no extreme ratio escapes it."""
    limit = math.sqrt(MAX_OUTPUT_PIXELS / float(width * height))
    effective = min(float(scale), limit)
    out_w = max(1, int(width * effective))
    out_h = max(1, int(height * effective))
    # The floor bound only breaks where an axis would round down to ZERO pixels and is raised back
    # to 1 (a 1px-wide strip); then the other axis is capped by the budget itself.
    if out_w * out_h > MAX_OUTPUT_PIXELS:
        if out_w <= 1:
            out_h = MAX_OUTPUT_PIXELS
        else:
            out_w = MAX_OUTPUT_PIXELS
    return out_w, out_h


def _fit_budget(image, zoom):
    """(image, png bytes, scale, mode) inside the model's image budget.

    LANCZOS: it keeps a 1 px stroke continuous when enlarging (bilinear smears it, nearest
    stair-steps every diagonal). A line drawing in an adaptive 256-colour PNG is usually 3–6×
    smaller than RGB and still tells green annotations from black geometry, so that is tried
    before any pixel is thrown away."""
    width, height = image.size
    scale = float(zoom)
    if width * height * scale * scale > MAX_OUTPUT_PIXELS:
        scale = math.sqrt(MAX_OUTPUT_PIXELS / float(width * height))
    scale = min(max(scale, 0.05), float(zoom))
    for _attempt in range(6):
        out_w, out_h = _clamped_output_size(width, height, scale)
        out = image.resize((out_w, out_h), Image.LANCZOS)
        # The scale actually delivered, after the dimension clamp — reported, not assumed.
        applied = math.sqrt((out_w / float(width)) * (out_h / float(height)))
        palette = out.convert("P", palette=Image.ADAPTIVE, colors=256)
        for candidate in (out, palette):
            data = _encode(candidate)
            if len(data) <= MAX_OUTPUT_BYTES:
                return candidate, data, applied, candidate.mode
        scale *= 0.8
    _fail("BUDGET_EXCEEDED",
          f"no rendering of this region fits the {MAX_OUTPUT_BYTES} B / {MAX_OUTPUT_PIXELS} px "
          "budget; ask for a smaller region.")


def _output_name(source_sha, kind, region, zoom):
    if kind == "overview":
        return f"{source_sha[:16]}_overview.png"
    safe = re.sub(r"[^0-9A-Za-z.,_-]", "", str(region or ""))
    return f"{source_sha[:16]}_{kind}_{safe}_z{zoom}.png"


def read_region(image_path, region="", zoom=DEFAULT_ZOOM):
    """Crop/zoom one region of a drawing, or return the labelled overview grid."""
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not MIN_ZOOM <= zoom <= MAX_ZOOM:
        _fail("INVALID_ZOOM", f"zoom must be an integer {MIN_ZOOM}–{MAX_ZOOM} (got {zoom!r}).")
    path, image = _open_source(image_path)
    width, height = image.size
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    box, kind, label = parse_region(region, width, height)

    if kind == "overview":
        fitted, data, scale, mode = _fit_budget(draw_grid(image.copy()), 1)
        region_out = None
        basis = "source_sheet"
    else:
        x0, y0, x1, y1 = box
        if (x1 - x0) * (y1 - y0) < MIN_CROP_AREA:
            _fail("REGION_TOO_SMALL",
                  f"the {x1 - x0}x{y1 - y0} region holds less than {MIN_CROP_AREA} px of content; "
                  "widen it, or read the overview grid first.")
        fitted, data, scale, mode = _fit_budget(image.crop(box), zoom)
        region_out = {"kind": kind, "label": label, "requested": region,
                      "source_box": [x0, y0, x1, y1]}
        basis = "region"

    directory = INTAKE_ROOT / source_sha[:16]
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / _output_name(source_sha, kind, region, zoom)
    out_path.write_bytes(data)

    meta = {
        "status": "completed",
        "risk": "read",
        "action": kind,
        "image_path": str(path),
        "source_pixels": [width, height],
        "source_sha256_16": source_sha[:16],
        "output_path": str(out_path),
        "output_pixels": list(fitted.size),
        "output_bytes": len(data),
        "output_mode": mode,
        "scale": round(scale, 4),
        "scale_basis": basis,
        "zoom": 1 if kind == "overview" else zoom,
        "region": region_out,
        "grid": grid_geometry(width, height),
        "budget": {"max_pixels": MAX_OUTPUT_PIXELS, "max_bytes": MAX_OUTPUT_BYTES},
        "read_next": "Read the returned path. If a dimension is still not legible, zoom that cell "
                     "(zoom 3 or 4) rather than guessing; anything still illegible is UNKNOWN and "
                     "goes to the user — never an invented value.",
    }
    return data, meta


def result_payload(image_path, region="", zoom=DEFAULT_ZOOM):
    """(image content block, one-line JSON) — what the MCP tool returns."""
    data, meta = read_region(image_path, region, zoom)
    return (MCPImage(data=data, format="png"),
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
