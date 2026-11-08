"""PDF drawings through the image readers — synthetic PDFs written by Pillow, no SolidWorks.

A chat PDF reaches the model as a saved file path (DSH file handle) or, under a full digest, as an
extensionless object in file-objects/. Both must render to the same page image the crop and OCR
code already read, with the page chosen explicitly and every failure loud.
"""
import hashlib
import json

import pytest
from PIL import Image, ImageDraw

import intake
import ocr
import pdf_source


@pytest.fixture(autouse=True)
def intake_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts" / "intake"
    monkeypatch.setattr(intake, "INTAKE_ROOT", root)
    return root


def _page(size, label):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 10, size[0] - 10, size[1] - 10], outline="black", width=3)
    draw.text((40, 40), label, fill="black")
    return image


@pytest.fixture
def two_page_pdf(tmp_path):
    """Page 1 portrait 600x800 px, page 2 landscape 800x600 px, both at 72 dpi (1 px = 1 pt)."""
    path = tmp_path / "图纸.pdf"
    first, second = _page((600, 800), "page one"), _page((800, 600), "page two")
    first.save(path, "PDF", resolution=72.0, save_all=True, append_images=[second])
    return path


def test_overview_defaults_to_page_one_and_lists_pages(two_page_pdf):
    _data, meta = intake.read_region(str(two_page_pdf))
    pdf = meta["pdf"]
    assert meta["image_path"] == str(two_page_pdf.resolve())
    assert (pdf["page"], pdf["page_count"], pdf["render_dpi"]) == (1, 2, 300)
    # 600x800 pt at 300 dpi
    assert meta["source_pixels"] == [2500, 3334]   # PDFium rounds each axis up
    assert [p["page"] for p in pdf["pages"]] == [1, 2]
    assert pdf["pages"][1]["size_mm"] == [282, 212]


def test_page_two_renders_its_own_sheet_and_crops_name_the_page(two_page_pdf):
    _data, overview = intake.read_region(str(two_page_pdf), page=2)
    assert overview["source_pixels"] == [3334, 2500]
    assert overview["pdf"]["page"] == 2
    _data, crop = intake.read_region(str(two_page_pdf), "0,0,800,600", 1, page=2)
    assert crop["region"]["source_box"] == [0, 0, 800, 600]
    assert crop["pdf"]["page"] == 2 and "pages" not in crop["pdf"]


def test_batch_reports_the_page_once(two_page_pdf):
    blocks, line = intake.result_batch(str(two_page_pdf), ["A1", "B2"], 2, page=2)
    payload = json.loads(line)
    assert len(blocks) == 2
    assert payload["pdf"]["page"] == 2 and payload["pdf"]["page_count"] == 2


def test_rendered_page_is_cached(two_page_pdf, intake_root, monkeypatch):
    intake.read_region(str(two_page_pdf), page=2)
    rendered = list((intake_root / "pdf").rglob("*.png"))
    assert len(rendered) == 1 and rendered[0].name == "page2_300dpi.png"
    stamp = rendered[0].stat().st_mtime_ns
    intake.read_region(str(two_page_pdf), "A1", page=2)
    assert rendered[0].stat().st_mtime_ns == stamp


@pytest.mark.parametrize("page,code", [
    (3, "PDF_PAGE_OUT_OF_RANGE"),
    (0, "INVALID_PAGE"),
    (True, "INVALID_PAGE"),
    ("2", "INVALID_PAGE"),
])
def test_bad_page_fails_with_a_code(two_page_pdf, page, code):
    with pytest.raises(RuntimeError, match=code):
        intake.read_region(str(two_page_pdf), page=page)


def test_page_on_an_image_is_refused(tmp_path):
    path = tmp_path / "sheet.png"
    Image.new("RGB", (200, 200), "white").save(path)
    with pytest.raises(RuntimeError, match="PAGE_NOT_APPLICABLE"):
        intake.read_region(str(path), page=2)
    assert intake.read_region(str(path), page=1)[1]["source_pixels"] == [200, 200]
    assert "pdf" not in intake.read_region(str(path))[1]


def test_damaged_pdf_is_a_typed_failure(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nthis is not a PDF body\n")
    with pytest.raises(RuntimeError, match="PDF_UNREADABLE"):
        intake.read_region(str(path))


def test_huge_page_is_rendered_under_the_pixel_cap(tmp_path):
    """A 100 in square page would be 900 MP at 300 dpi; it is rendered at the cap instead."""
    path = tmp_path / "huge.pdf"
    Image.new("RGB", (100, 100), "white").save(path, "PDF", resolution=1.0)
    _data, meta = intake.read_region(str(path))
    width, height = meta["source_pixels"]
    assert width * height <= pdf_source.MAX_RENDER_PIXELS
    assert meta["pdf"]["render_dpi"] < 300


def test_extensionless_attachment_is_detected_by_content(two_page_pdf, tmp_path, monkeypatch):
    """DSH stores a non-image upload under file-objects/<2>/<hash> with no extension."""
    digest = hashlib.sha256(two_page_pdf.read_bytes()).hexdigest()
    home = tmp_path / "home"
    obj = home / "attachments/v1/file-objects" / digest[:2] / digest
    obj.parent.mkdir(parents=True)
    obj.write_bytes(two_page_pdf.read_bytes())
    monkeypatch.setenv("DSH_HOME", str(home))
    assert intake.read_region(str(obj), page=2)[1]["pdf"]["page"] == 2
    assert intake.read_region("sha256:" + digest)[1]["pdf"]["page_count"] == 2


def test_ocr_reads_the_rendered_page(two_page_pdf, monkeypatch):
    seen = []

    def fake_helper(image_path, workdir, timeout=180):
        with Image.open(image_path) as image:
            seen.append(image.size)
        return {"lang": "en-US", "lines": [{"text": "Ø20", "box": [100, 100, 160, 120]}]}

    monkeypatch.setattr(ocr, "_run_helper", fake_helper)
    payload = json.loads(ocr.result_payload(str(two_page_pdf), page=2))
    assert payload["pdf"]["page"] == 2 and "pages" not in payload["pdf"]
    assert payload["source_pixels"] == [3334, 2500]
    assert seen[0] == (3334, 2500)
    assert payload["texts"][0]["text"] == "Ø20"
