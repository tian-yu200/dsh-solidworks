# Drawing Intake — reading an engineering drawing before any CAD call

Use this for every drawing / 三视图 / 零件图 / 工程图 image the user supplies, before writing a plan
or a Feature Graph. Its purpose is measured, not ceremonial: a whole sheet inside the model's image
budget renders small dimension text as a few pixels, and guessed dimensions are the failure this
workflow must never produce.

`read_drawing_region` is a read-only MCP tool. It touches no CAD document, needs no `job_id`, and
does not start SolidWorks, so it runs before `start_job`. Like `modlens_read_image` it accepts a
**local image file path only** — URLs (`http://`, `https://`, `ftp://`, `file://`) are rejected, and
it is not restricted to a folder: any local file the session can name is readable.

## 0. The reading protocol — three rounds, then the reading result

Reading must CONVERGE FAST. Each step costs 45-90 s, so the number of STEPS matters as much as the
number of crops: batching is the point, not politeness. Follow these rounds exactly.

- **Round 1 — one overview.** `read_drawing_region(image_path)` with an empty `region`: the gridded
  overview, which is FREE (never charged to the budget). From it, write down every view, the
  projection system, the scale and title block, and **the complete list of dimension clusters to
  read**, each with its cell (`C4`) or source-pixel box (`x0,y0,x1,y1`) and the zoom it needs.
- **Round 2 — ONE call, every cluster.** Pass them all at once:
  `read_drawing_region(image_path, regions=["B2","C4","D3",...], zoom=3)` — up to 12 regions in one
  call, sharing one zoom. One call, not one call per cluster. The response is one image per region
  plus one JSON line listing each region's box, output path and budget.
- **Round 3 — at most one more call.** Only for values still illegible after round 2 (a tighter
  box, or `zoom=4`). Anything still ambiguous after this round is `UNKNOWN`.
- **Then send the READING RESULT and stop.** The first deliverable is NOT the full plan: it is
  - the **dimension ledger** — id, value, kind, view, grid cell or box, `legible: yes|no`;
  - a **question list** for every uncertain or undimensioned item.
  Send both with `ask_user_question`. A candidate reading may be offered ("looks like 26 — confirm?")
  but never enters the ledger as a fact. The full PLAN.md and the Feature Graph come only after the
  user answers. **Do not zoom again before then.**

**No re-reading — enforced by the tool.** Every crop box is remembered per drawing (by SHA-256). A
new crop whose area lies at least 70% inside ONE earlier crop is a REPEAT. ONE repeat of an area is
allowed, and only at a **strictly higher zoom** (the legitimate "this is still too small" move). Any
further repeat is refused with `REGION_ALREADY_READ`, naming the earlier crop; what is still
unreadable there is `UNKNOWN` and is asked. The overview is exempt, and `reset_budget` clears the
memory too.

The tool also enforces a **crop budget per drawing**: every response carries
`budget: {crops_used, crop_limit}`. The default is **15 crops**; the overview is free; the count is
per source image for the life of the server process. Each region of a batch is charged
individually, and a refused region does not stop the others. At the limit further crops are
refused with `INTAKE_BUDGET_EXHAUSTED` — that means **send the reading result now**, list the unread
values as `UNKNOWN` and ask the user; it is not a reason to retry. `reset_budget=true` zeroes this
drawing's counter and read memory before the call and may be used **ONLY after the user has
explicitly asked for more reading** — never on your own initiative.

**No escape hatch.** Never measure pixels, open a browser, run a script, or use any other tool to
estimate a size. A value the drawing does not state is `UNKNOWN` and is ASKED, not estimated.

## 1. Overview first

Call `read_drawing_region(image_path="<local path>")` with no `region`. The result is the full sheet
with a labelled grid — columns `A`..`H` left to right, rows `1`..`6` top to bottom — plus JSON with
`output_path`, `source_pixels`, and the `cell_pixels` that define every label.

From the overview, record before measuring anything:

- every view and what it shows (main / top / side / isometric / section / detail / removed section);
- the projection system, proved by the layout and the projection symbol, not assumed;
- the scale named in the title block (and per-view scales);
- the title block: part name/number, material, weight, finish, revision;
- which cells contain the dimension clusters and the title block;
- **the complete list of dimension clusters to read** — every one, with its cell or source-pixel box
  and zoom. This list IS the round-2 batch.

If the drawing is still unreadable as a whole (a very large sheet, or `output_mode` reported `P`
because the RGB encode bust the byte budget), work only from crops from here on.

## 2. Zoom every dimension cluster — in one batch

For each cluster in the round-1 list: `read_drawing_region(image_path, region="C4", zoom=3)` — a
cell label, or a source-pixel box `x0,y0,x1,y1` when a cluster straddles cells. Use `zoom=4` for
thread callouts, tolerance stacks, and radii. Crops are drawn without the grid overlay, so the
pixels the tool returns are exactly the drawing's.

Pass the whole list as ONE call: `read_drawing_region(image_path, regions=[...], zoom=3)`. A
second, final call is allowed for whatever is still illegible; after that the ledger is closed and
the reading result is sent.

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
[reconstruction reasoning](solidworks://skill/references/reconstruction-reasoning.md), read with
`read_mcp_resource` on server `solidworks`) with the ledger's UNKNOWNs listed as open
questions, get approval for that plan version, and only then compose the Feature Graph. Every
graph value must be traceable to a `legible: yes` ledger row or to a user answer.

The READING RESULT (ledger + questions) goes to the user at the end of the reading rounds (§0),
before they answer. The full PLAN.md is written from the confirmed ledger once they have answered;
approval of that plan is what unblocks building.

## 5. Forbidden during intake

- `pip`, `uv`, `conda`, npm or any other install, and any new virtual environment.
- Any other CAD kernel or geometry library: build123d, CadQuery, OpenSCAD, pythonocc, FreeCAD,
  STEP/OBJ scripting. The route to a SolidWorks part is `submit_feature_graph`, nothing else.
