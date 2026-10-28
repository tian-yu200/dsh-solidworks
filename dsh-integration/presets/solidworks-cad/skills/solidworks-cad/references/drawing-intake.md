# Drawing Intake — reading an engineering drawing before any CAD call

Use this for every drawing / 三视图 / 零件图 / 工程图 image the user supplies, before writing a plan
or a Feature Graph. Its purpose is measured, not ceremonial: a whole sheet inside the model's image
budget renders small dimension text as a few pixels, and guessed dimensions are the failure this
workflow must never produce.

`read_drawing_region` is a read-only MCP tool. It touches no CAD document, needs no `job_id`, and
does not start SolidWorks, so it runs before `start_job`. Like `modlens_read_image` it accepts a
**local image file path only** — URLs (`http://`, `https://`, `ftp://`, `file://`) are rejected, and
it is not restricted to a folder: any local file the session can name is readable.

## 1. Overview first

Call `read_drawing_region(image_path="<local path>")` with no `region`. The result is the full sheet
with a labelled grid — columns `A`..`H` left to right, rows `1`..`6` top to bottom — plus JSON with
`output_path`, `source_pixels`, and the `cell_pixels` that define every label.

From the overview, record before measuring anything:

- every view and what it shows (main / top / side / isometric / section / detail / removed section);
- the projection system, proved by the layout and the projection symbol, not assumed;
- the scale named in the title block (and per-view scales);
- the title block: part name/number, material, weight, finish, revision;
- which cells contain the dimension clusters and the title block.

If the drawing is still unreadable as a whole (a very large sheet, or `output_mode` reported `P`
because the RGB encode bust the byte budget), work only from crops from here on.

## 2. Zoom every dimension cluster

For each cluster: `read_drawing_region(image_path, region="C4", zoom=3)` — a cell label, or a
source-pixel box `x0,y0,x1,y1` when a cluster straddles cells. Use `zoom=4` for thread callouts,
tolerance stacks, and radii. Crops are drawn without the grid overlay, so the pixels the tool
returns are exactly the drawing's.

Fill one row of the ledger per dimension. Values are what the drawing STATES, in millimetres:

| id | value | kind | view | cell/crop | legible |
| --- | --- | --- | --- | --- | --- |
| L1 | 90 | linear | A (top view) | B2 | yes |
| D1 | Ø20H7 | diameter + tolerance | main | D3 (zoom 4) | yes |
| R3 | R100 | radius, centre not dimensioned | main | C4 | yes |
| T1 | M6-6H 深12 | thread | detail | A5 | no → UNKNOWN |

`kind` ∈ linear, angle, Ø, R, thread, tolerance/GD&T, finish, note, count (e.g. "4× Ø10").
`legible` is `yes` only when the digits are unambiguous at the returned crop; `no` otherwise.
Keep the crop path with the row — it is the evidence a later question refers to.

## 3. Never guess: ask, then record the answer

Anything illegible after `zoom=4`, absent from the drawing, or ambiguous between views is
`UNKNOWN`, and `ASSUMED` is only ever a labelled assumption the user has seen. Ask with
`ask_user_question`, one question per blocking value, and cite the crop path so the user can look at
the same pixels. A missing overall dimension on an exercise drawing (a dimensioned-looking view
with no numbers) is a question, not a pixel measurement.

Reading geometry off pixels to estimate a number the drawing does not state is forbidden: an
estimated dimension is an invented one, and it silently becomes the model's truth.

## 4. Only then plan and build

Write the inline PLAN.md-style plan (see
[reconstruction reasoning](reconstruction-reasoning.md)) with the ledger's UNKNOWNs listed as open
questions, get approval for that plan version, and only then compose the Feature Graph. Every
graph value must be traceable to a `legible: yes` ledger row or to a user answer.

## 5. Forbidden during intake

- `pip`, `uv`, `conda`, npm or any other install, and any new virtual environment.
- Any other CAD kernel or geometry library: build123d, CadQuery, OpenSCAD, pythonocc, FreeCAD,
  STEP/OBJ scripting. The route to a SolidWorks part is `submit_feature_graph`, nothing else.
- Writing ad-hoc scripts to scan the image (dark-pixel counting, edge detection, OCR attempts).
  `read_drawing_region` and vision are the supported readers; `modlens_read_image` may be used on a
  returned crop if a second look helps.
- Shell, macros, COM, or direct calls to the execution service.
- Treating "a tool call returned" as "the dimension is verified".

## 6. Material

Copy the drawing's material into the graph's `material` name as written (e.g. `HT200`). If the
library does not contain it the run completes with a `MATERIAL_NOT_FOUND` **warning** and the part
is left without a material: that is the correct outcome. Report it and let the user pick an exact
library name or add a custom entry. Never substitute a similar-looking material — that invents
density, strength and weight the drawing never stated.
