"""Windows built-in OCR (Windows.Media.Ocr) for drawing text — no installs.

Optional aid for small dimension text. Directly view the original drawing first;
OCR is a candidate table, not an obligatory first pass or verified geometry.

ENGINE CHOICE. Windows.Media.Ocr ships with Windows 10/11, so there is no pip/npm package and
nothing to install. It is a WinRT API, and the access route matters:

  * PowerShell 7 cannot project WinRT types here (CsWinRT needs the .winmd references, and Add-Type
    refuses them), so pwsh is NOT used.
  * Windows PowerShell 5.1 CAN, with the type-accelerator syntax
    `[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]`. Its async
    results are awaited through the generic `System.WindowsRuntimeSystemExtensions.AsTask` overload
    found by reflection — the well-known PowerShell "Await" helper. That is what _HELPER does.
  * Python has no WinRT projection in this venv and WinRT is not reachable through pywin32's COM
    dispatch, so the engine is driven out-of-process through powershell.exe.

CONSTRAINT FOUND EMPIRICALLY: StorageFile.GetFileFromPathAsync fails on a path containing non-ASCII
characters or spaces (it throws a bare "value does not fall within the expected range"). The helper
therefore always receives an ASCII-only copy of the image, which this module makes.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image

from intake import _open_source

# The helper is kept as text (not a separate file) so the adapter stays a single deployable tree
# and the exact engine calls are reviewable next to the Python that parses them.
_HELPER = r'''
param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)][string]$OutFile)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw "no OCR engine for the user profile languages" }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$rows = @()
foreach ($line in @($result.Lines)) {
    $words = @($line.Words)
    if ($words.Length -eq 0) { continue }
    $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = -1.0; $y1 = -1.0
    foreach ($w in $words) {
        $r = $w.BoundingRect
        if ([double]$r.X -lt $x0) { $x0 = [double]$r.X }
        if ([double]$r.Y -lt $y0) { $y0 = [double]$r.Y }
        if (([double]$r.X + [double]$r.Width) -gt $x1) { $x1 = [double]$r.X + [double]$r.Width }
        if (([double]$r.Y + [double]$r.Height) -gt $y1) { $y1 = [double]$r.Y + [double]$r.Height }
    }
    # NOTE: Windows.Media.Ocr exposes NO per-text confidence (OcrWord carries only Text and
    # BoundingRect). The field is emitted as null rather than invented — a made-up score would
    # tell the model to trust a guess.
    $rows += [pscustomobject]@{
        text = [string]$line.Text
        box = @([math]::Round($x0,1), [math]::Round($y0,1), [math]::Round($x1,1), [math]::Round($y1,1))
    }
}
$payload = [pscustomobject]@{
    lang = $engine.RecognizerLanguage.LanguageTag
    width = $decoder.PixelWidth
    height = $decoder.PixelHeight
    lines = $rows
}
$json = $payload | ConvertTo-Json -Depth 6 -Compress
[System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("WROTE " + $OutFile + " lines=" + $rows.Count)
'''

# Windows OCR refuses images above this on the long edge; we stay well under it.
MAX_OCR_EDGE = 4000
# Small text is the whole problem: upscaling before OCR is what turns 6-px digits into readable
# ones. 2x is the sweet spot measured here — 3x gains nothing and costs time.
UPSCALE = 2


class OcrUnavailable(RuntimeError):
    """The engine or its helper could not run (not a drawing error)."""


def engine_languages():
    """The OCR languages installed on this machine, as tags (['en-US', ...])."""
    ps = (
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
        "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null;"
        "([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | "
        "ForEach-Object { $_.LanguageTag }) -join ','"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-Command", ps],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrUnavailable("could not query the OCR engine: %s" % exc)
    tags = [t.strip() for t in (out.stdout or "").strip().split(",") if t.strip()]
    if not tags:
        raise OcrUnavailable("no OCR recognizer language is installed on this Windows machine")
    return tags


def _ascii_copy(path, workdir):
    """A copy of `path` under an ASCII-only name.

    StorageFile.GetFileFromPathAsync throws on a path with non-ASCII characters (a Chinese file
    name is the common case for the user's drawings), so the helper never sees the original name.
    """
    suffix = Path(path).suffix.lower() or ".png"
    target = Path(workdir) / ("src" + suffix)
    shutil.copyfile(path, target)
    return target


def _prepare(path, workdir, rotate=0):
    """An ASCII copy, upscaled and optionally rotated, as PNG. Returns (Path, scale)."""
    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size
        scale = 1
        longest = max(width, height)
        if longest * UPSCALE <= MAX_OCR_EDGE:
            scale = UPSCALE
        elif longest < MAX_OCR_EDGE:
            scale = max(1, MAX_OCR_EDGE // longest)
        if scale > 1:
            image = image.resize((width * scale, height * scale), Image.LANCZOS)
        if rotate:
            # PIL rotates counter-clockwise; expand so nothing is cropped.
            image = image.rotate(rotate, expand=True)
        target = Path(workdir) / ("in_%d.png" % rotate)
        image.save(target, format="PNG")
        return target, scale


def _run_helper(image_path, workdir, timeout=180):
    helper = Path(workdir) / "ocr.ps1"
    helper.write_text(_HELPER, encoding="utf-8")
    out = Path(workdir) / "out.json"
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(helper), "-Path", str(image_path), "-OutFile", str(out)],
        capture_output=True, text=True, timeout=timeout)
    if not out.exists():
        raise OcrUnavailable("the OCR helper produced no result: %s"
                             % ((proc.stderr or proc.stdout or "").strip()[:400]))
    return json.loads(out.read_text(encoding="utf-8"))


def _to_source_box(box, scale, rotate, out_size, src_size):
    """A box in the pre-processed image mapped back to SOURCE pixels.

    The image was upscaled by `scale`, then rotated (PIL, expand=True). Undo both in order: first
    divide by the scale, then rotate the point back around the rotated canvas.
    """
    x0, y0, x1, y1 = [float(v) / scale for v in box]
    if not rotate:
        return [x0, y0, x1, y1]
    ow, oh = out_size
    sw, sh = src_size
    pts = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    mapped = []
    # PIL rotate(a) turns the image COUNTER-CLOCKWISE, and expand=True swaps the canvas size for
    # 90/270. A point at rotated-canvas (X, Y) therefore came from the source point below — the
    # INVERSE of the forward map. Getting this backwards puts every vertical dimension in the
    # wrong grid cell, which is worse than not reading it.
    for (x, y) in pts:
        if rotate == 90:      # forward: (x,y) -> (y, W-x)   =>  inverse: (X,Y) -> (W-Y, X)
            mapped.append((sw - y, x))
        elif rotate == 270:   # forward: (x,y) -> (H-y, x)   =>  inverse: (Y, H-X)
            mapped.append((y, sh - x))
        else:
            mapped.append((x, y))
    xs = [p[0] for p in mapped]
    ys = [p[1] for p in mapped]
    return [min(xs), min(ys), max(xs), max(ys)]


def read_text(image_path, rotations=(0, 90, 270)):
    """OCR the whole sheet. Returns {"lang", "source_pixels", "texts": [...]}.

    Every text is {text, raw, box, grid_cell, rotation, confidence}. The same text found in more
    than one rotation is merged (the 0-degree read wins; a rotation only contributes texts the
    upright pass did not already cover in the same place).
    """
    path, validated_image = _open_source(image_path)
    src_size = validated_image.size
    validated_image.close()
    started = time.time()
    workdir = tempfile.mkdtemp(prefix="ocr_")
    try:
        merged = []
        lang = None
        for rotate in rotations:
            prepared, scale = _prepare(path, workdir, rotate)
            with Image.open(prepared) as out_img:
                out_size = out_img.size
            payload = _run_helper(prepared, workdir)
            lang = lang or payload.get("lang")
            for row in payload.get("lines") or []:
                text = (row.get("text") or "").strip()
                if not text:
                    continue
                box = _to_source_box(row.get("box") or [0, 0, 0, 0], scale, rotate,
                                     out_size, src_size)
                norm = _normalise(text)
                raw_box = list(row.get("box") or [0, 0, 0, 0])
                merged.append({"text": norm, "raw": text, "box": box, "raw_box": raw_box,
                               "grid_cell": _grid_cell(box, src_size),
                               "rotation": rotate,
                               "confidence": row.get("confidence")})
        return {
            "lang": lang,
            "source_pixels": list(src_size),
            "texts": _merge(merged, src_size),
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


GRID_COLUMNS = "ABCDEFGH"
GRID_ROWS = 6


def _grid_cell(box, size):
    """The A-H x 1-6 cell that holds the box's CENTRE — the same grid the overview draws."""
    width, height = size
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    col = min(len(GRID_COLUMNS) - 1, max(0, int(cx / (width / len(GRID_COLUMNS)))))
    row = min(GRID_ROWS - 1, max(0, int(cy / (height / GRID_ROWS))))
    return "%s%d" % (GRID_COLUMNS[col], row + 1)


# Look-alikes the engine returns for drawing symbols. Digits and ASCII are never touched: a
# wrong "O" -> "0" would invent a dimension, so only the characters BELOW are mapped, and the
# raw text stays next to the normalised one for the model to check.
_SYMBOLS = {
    "Ø": "Ø", "φ": "Ø", "⌀": "Ø", "o": "Ø",
    "Ф": "Ø", "ø": "Ø",
    "×": "×", "x": "×", "✕": "×",
    "±": "±", "°": "°",
}


def _normalise(text):
    """Repair the dimension symbols the OCR misreads, leaving digits and letters alone.

    Only a symbol POSITION is rewritten: an 'o' immediately before digits at the START of a token
    is a diameter sign ('o20' -> 'Ø20'), which is the single most common drawing-text misread.
    """
    import re as _re
    out = _re.sub(r"(?<![A-Za-z0-9])[oO](?=\d)", "Ø", text)
    # The other half of the same misread: a LEADING ZERO before digits is a diameter sign the
    # engine turned into '0' ("020H7" is "Ø20H7"). Only a zero at the START of a token that has
    # more digits after it, and only when the rest is a plausible size, is rewritten — a real
    # value like "0.5" or a datum "0" must survive untouched.
    out = _re.sub(r"(?<![A-Za-z0-9.])0(?=\d{1,3}(?:\.\d+)?(?:[A-Za-z]|$))", "Ø", out)
    return out


def _centre(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _merge(items, src_size):
    """Drop a rotated duplicate of a text the upright pass already found in the same place."""
    def same_place(a, b):
        ax, ay = _centre(a)
        bx, by = _centre(b)
        width, height = src_size
        return abs(ax - bx) < width * 0.02 and abs(ay - by) < height * 0.02

    # Compare the box the OCR REPORTED (in its own rotated canvas), not the source-mapped one: a
    # 90-degree pass reports a vertical dimension at a completely different place in the rotated
    # image, so comparing mapped boxes would never match and every duplicate would survive.
    def raw_centre(item):
        b = item.get("raw_box") or item["box"]
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)

    kept = []
    for item in items:
        if item["rotation"] != 0:
            cx, cy = raw_centre(item)
            duplicate = False
            for k in kept:
                if k["rotation"] != 0:
                    continue
                kx, ky = raw_centre(k)
                if abs(cx - kx) <= 12 and abs(cy - ky) <= 12:
                    duplicate = True
                    break
            if duplicate:
                continue
        kept.append(item)
    return kept


def result_payload(image_path):
    """The one-line JSON the MCP tool returns (no image: the answer is the TEXT table).

    Kept deliberately small — one row per text, no per-character boxes. The model maps these
    values to features; anything it still cannot settle is asked, and a crop of the same area is
    then cheap because the OCR pass already told it WHERE to look.
    """
    data = read_text(image_path)
    texts = data["texts"]
    payload = {
        "status": "completed",
        "risk": "read",
        "action": "ocr",
        "image_path": str(Path(image_path)),
        "source_pixels": data["source_pixels"],
        "ocr_language": data["lang"],
        "text_count": len(texts),
        "elapsed_s": data["elapsed_s"],
        "texts": [
            {"text": t["text"], "raw": t["raw"], "box": [round(v, 1) for v in t["box"]],
             "grid_cell": t["grid_cell"], "rotation": t["rotation"],
             "confidence": t["confidence"]}
            for t in texts
        ],
        "read_next": ("Optional CANDIDATE table: verify against the original image. Use EXPLICIT / "
                      "DERIVED (formula and constraints) / USER / UNKNOWN. Combine genuinely "
                      "missing dimensions and compact plan approval in one question round. "
                      "Tool failures are technical failures, not missing dimensions."),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
