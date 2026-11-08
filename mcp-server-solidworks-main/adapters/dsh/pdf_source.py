"""PDF drawings: render one page to a PNG so the image readers can treat it like any drawing.

A chat PDF reaches the model as a FILE HANDLE (a text line naming a saved path), never as an image
block, and the SolidWorks preset has no file tool — so a PDF drawing was unreadable. The readers now
render the requested page here and continue on the PNG: crops, the gridded overview and OCR are the
same code as for a screenshot.

Vector geometry and the text layer are NOT read as dimensions. Measured on a real CAD-exported
sheet (2026-11-08, 第五届上图杯试卷): every dimension number was drawn as curves, the text layer held
only page titles. The page text is therefore listed as a short preview to help pick a page, and
dimensions stay a vision task with the same evidence rules as an image.

pypdfium2 (Apache-2.0 / BSD-3) bundles PDFium, Chrome's PDF engine: one wheel, no system install.
"""
import hashlib
import math
import re
import uuid
from pathlib import Path

# PDF 1.7 §7.5.2 lets the header sit after leading bytes; readers accept it in the first 1 KiB. The
# DSH attachment object has no extension, so the CONTENT decides, not the name.
_HEADER_WINDOW = 1024
_HEADER = b"%PDF-"

# 300 dpi keeps a 3.5 mm dimension digit about 40 px tall — sharp at crop zoom 2 — and an A3 sheet
# (17.4 MP) under the cap. Bigger sheets are rendered at the cap instead, so an A0 page never
# allocates a 140 MP bitmap (PDFium's BGRA bitmap is 4 B/px before the RGB copy).
RENDER_DPI = 300
MAX_RENDER_PIXELS = 20_000_000

# The page list helps choose a page without rendering each one; long documents are cut, not refused.
MAX_LISTED_PAGES = 30
TEXT_PREVIEW_CHARS = 60

_PT_PER_INCH = 72.0
_MM_PER_PT = 25.4 / _PT_PER_INCH


def _fail(code, message):
    # server._error_payload maps RuntimeError("<CODE>: …") onto a typed error code.
    raise RuntimeError(f"{code}: {message}")


def is_pdf(path):
    try:
        with open(path, "rb") as handle:
            return _HEADER in handle.read(_HEADER_WINDOW)
    except OSError:
        return False


def _render_scale(width_pt, height_pt):
    """PDF points → pixels at RENDER_DPI, lowered so the page stays inside MAX_RENDER_PIXELS.

    PDFium sizes the bitmap with ceil() on each axis (600 pt at 300 dpi is 2500 px, 800 pt is 3334),
    so the cap is checked on the rounded-up product, not on the exact area."""
    scale = RENDER_DPI / _PT_PER_INCH
    area = width_pt * height_pt
    if area * scale * scale > MAX_RENDER_PIXELS:
        scale = math.sqrt(MAX_RENDER_PIXELS / area)
        while math.ceil(width_pt * scale) * math.ceil(height_pt * scale) > MAX_RENDER_PIXELS:
            scale *= 0.999
    return scale


def _preview(page):
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_bounded()
    finally:
        textpage.close()
    # Page numbers such as "--2--" or "-5-" carry nothing; collapse whitespace so one line stays short.
    text = re.sub(r"-+\s*\d+\s*-+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:TEXT_PREVIEW_CHARS]


def _size_mm(page):
    width, height = page.get_size()
    return [round(width * _MM_PER_PT), round(height * _MM_PER_PT)]


def render_page(path, page_number, out_root):
    """(PNG path, info) for one 1-based page of the PDF at `path`.

    The PNG is cached under out_root by PDF content hash, page and dpi, so repeated crops of the
    same page decode a PNG instead of re-rendering. Info names the page, the page count and a short
    per-page list for choosing another page.
    """
    try:
        import pypdfium2 as pdfium     # imported here: an image-only call never loads PDFium
    except ImportError:
        _fail("PDF_SUPPORT_MISSING",
              "pypdfium2 is not installed in the adapter environment; run pip install -r "
              "mcp-server-solidworks-main/adapters/claude/requirements.txt and restart DSH.")

    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        _fail("INVALID_PAGE", f"page must be an integer >= 1 (got {page_number!r}).")
    try:
        document = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        _fail("PDF_UNREADABLE",
              f"{path}: {exc}. A password-protected or damaged PDF cannot be rendered; ask for an "
              "unprotected export or a screenshot of the drawing.")
    try:
        count = len(document)
        if count == 0:
            _fail("PDF_UNREADABLE", f"{path} has no pages.")
        if page_number > count:
            _fail("PDF_PAGE_OUT_OF_RANGE", f"page {page_number} does not exist; this PDF has "
                  f"{count} page(s), numbered 1-{count}.")
        pages = []
        for index in range(min(count, MAX_LISTED_PAGES)):
            listed = document[index]
            try:
                pages.append({"page": index + 1, "size_mm": _size_mm(listed),
                              "text_preview": _preview(listed)})
            finally:
                listed.close()

        page = document[page_number - 1]
        try:
            width_pt, height_pt = page.get_size()
            scale = _render_scale(width_pt, height_pt)
            dpi = round(scale * _PT_PER_INCH)
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            out_dir = out_root / digest[:16]
            out_path = out_dir / f"page{page_number}_{dpi}dpi.png"
            if not out_path.is_file():
                out_dir.mkdir(parents=True, exist_ok=True)
                # PDFium applies the page's /Rotate, so a landscape sheet stored as a rotated
                # portrait page comes out upright.
                bitmap = page.render(scale=scale, draw_annots=True)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
                tmp_path = out_path.with_name(f"{out_path.name}.{uuid.uuid4().hex}.tmp")
                image.save(tmp_path, format="PNG")
                image.close()
                tmp_path.replace(out_path)
        finally:
            page.close()
    finally:
        document.close()

    info = {
        "path": str(path),
        "page": page_number,
        "page_count": count,
        "render_dpi": dpi,
        "rendered_png": str(out_path),
        "pages": pages,
        "pages_listed": len(pages),
        "note": "Pass the PDF path with page=N to read another page. Page text is a hint for "
                "choosing a page; dimensions are read from the rendered drawing.",
    }
    return out_path, info
