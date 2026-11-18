---
name: solidworks-cad
description: "Use for SolidWorks work AND for reading engineering drawings — a 三视图 / 零件图 / 工程图 / pictorial / sectional drawing image that must be modelled or rebuilt as a SolidWorks 3D part, plus inspection, creation, editing, saving, exporting and objective verification through the dedicated mcp__solidworks__ tools. Takes precedence over generic CAD skills (build123d, CadQuery, OpenSCAD, STEP scripting) whenever the target is SolidWorks — never install packages or drive another kernel — the SolidWorks MCP tools and the Feature Graph are the only route."
---
# SolidWorks CAD
Operate SolidWorks only through these twelve high-level `mcp__solidworks__` tools.
Approved support tools for workflow assistance: `ask_user_question`, `todo_write`,
`skill`, `modlens_read_image` (local image file paths only; URL forms such as
`http://` are rejected) and `read_mcp_resource` — the only way to open a
skill reference, since this preset has no file tool.
The runtime tool schemas and returned payloads are the authoritative contract.
Parse each tool's JSON string before acting on its envelope status. Every tool
result is compact single-line JSON. `start_job` returns `job_id` and
`job_token` first; bulky readiness and state detail is read with
`inspect_state(scope="state")`. For a local drawing, pass its path as `drawing_path` to `start_job`.
## Tool Boundary

| Tool | Use |
| --- | --- |
| `start_job` | Connect to SolidWorks, optionally open a target document, and return `job_id` plus private `job_token`. |
| `inspect_state` | Read readiness, locked document state, constrained CAD analysis, or approved recipe/schema content; references return one page at a time (`reference_offset`/`reference_limit`). |
| `submit_feature_graph` | Build a new part or assembly from one complete Feature Graph. |
| `apply_document_edits` | Apply allowlisted parametric edits to the locked active document. |
| `drawing_workflow` | Create a production drawing and requested views, annotations, tables, or checks. |
| `save_or_export` | Save, export, batch export, or close through the safety policy. |
| `request_confirmation` | Refresh one-time confirmation data for a pending high-risk action. |
| `confirm_action` | Execute exactly one action after fresh, explicit user approval. |
| `cancel_action` | Cancel a queued confirmation action without executing it — only when the user asks to cancel. |
| `finish_job` | Run final objective verification and close the authenticated job. |
| `read_drawing_text` | Optional OCR candidate table with positions; directly view the original drawing first. |
| `read_drawing_region` | Zoom part of a drawing image or PDF page (grid cell or pixel box; `page` for a PDF) so small dimensions can be read; read-only, needs no job and touches no CAD. |

There is no model-visible low-level fallback. Do not use SolidWorks COM, Shell,
macros, scripts, REST endpoints, desktop automation, or legacy low-level CAD MCP
tools. Report `CAPABILITY_GAP` when these twelve tools cannot express or inspect the
requested operation. If the tool surface is unavailable, report `MCP_UNAVAILABLE`
and make no CAD call.

Two job-scoped write rules:
- **Pending Feature Graph** — once `submit_feature_graph` returns
  `pending_confirmation`, every document write is refused with
  `GRAPH_PENDING_CONFIRMATION` until that action is confirmed or cancelled.
  Confirm it with `confirm_action`, or release the guard with
  `cancel_action`.
- **Targetless job on a pre-existing document** — a job begun without
  `target_document` binds read-only to whatever document was active, and
  refuses `save_or_export`, `apply_document_edits` and
  `submit_feature_graph(fresh_document=false)` with
  `JOB_DOCUMENT_READ_ONLY`. Start a new job with `target_document` to edit
  that document, or build a fresh document with a confirmed
  `fresh_document` Feature Graph.

## Core Workflow

1. Call `inspect_state(scope="state")` to check readiness and current document.
2. Read only the references needed for the task with
   `inspect_state(scope="reference", reference_topic=...)`; a large reference
   arrives one page at a time, so pass `reference_offset`/`reference_limit`
   instead of repeating an identical call.
3. For image or drawing reconstruction, complete the bounded workflow below,
   present the plan inline in your reply, and obtain approval before mutation.
4. Call `start_job` before any deep document inspection or mutation. Supply
   `target_document` for an existing file and leave it empty for a fresh model.
   An empty `target_document` with no active document starts an unbound job.
5. Keep `job_id` and `job_token` private. Pass both to every later job-scoped
   call. Never place `job_token` in plans, logs, summaries, or user-visible text.
6. Use the smallest matching high-level mutation tool.
7. Inspect every response before continuing. A successful transport call is not
   proof that the CAD result is correct.
8. Use requested paths; otherwise save a new file in the working directory, never overwrite; then call `finish_job`.

`start_job` locks the expected active-document identity. Every later job-scoped
call verifies that identity. If the user switches documents, stop and begin a new
job for the newly intended target.

An unbound job — started with no active document — has no identity to lock, so
every document-bound call is refused with `JOB_DOCUMENT_UNBOUND` before any CAD
call: `apply_document_edits`, `submit_feature_graph` with
`fresh_document=false`, `drawing_workflow`, save/export/batch/close, and
`confirm_action` of a pending action that does not create its own document. The
job binds only through its own document-creating call,
`submit_feature_graph` with `fresh_document=true`; `save_or_export` `close` with
`close_all=true` runs but never binds. Otherwise start a new job with
`target_document`, or open the document first and start a new job.

## Reading a Skill Reference

A `solidworks://skill/references/<file>.md` link is a read-only MCP resource, never a file path:
this preset ships no file tool, so a path cannot be opened. Read it with `read_mcp_resource`,
server `solidworks`, that exact URI as this skill links it. A URI carrying a path or a `..`
segment is refused with `SKILL_RESOURCE_TRAVERSAL_REFUSED`, an unknown name with
`SKILL_RESOURCE_NOT_FOUND`; use only the URIs linked here instead of guessing a file name.

## Drawing Reconstruction Flow

This flow applies only when geometry is inferred from drawings or images.
At node A, start a job only when deep inspection of an active CAD document is
needed; this is not modeling, and node F reuses that job. Attached images can be
analyzed before any job exists.

Node A directly views the original drawing first. OCR and crops are OPTIONAL aids.
Crop WHOLE VIEWS as source-pixel boxes at zoom 2 in one batch (front, top/side, sections,
lettered views, notes); never grid cells or zoom 3-4 fragments, which cut a dimension off
its dimension line and feature. Internally classify dimensions as EXPLICIT / DERIVED /
USER / UNKNOWN (DERIVED retains formula and evidence; an absent label is not necessarily
an absent constraint; NEVER infer dimensions from pixel proportions). Use mm unless the
drawing states another unit; omit unspecified material; save a new file in the working
directory without overwriting; ask about projection only if alternatives create different
parts. Never expose internal tags, topology IDs, Feature Graph keys, or meter/radian values
to the user: present a plain-Chinese mechanical 10-second dimension checklist and SolidWorks
step summary in your message text, then ONE `ask_user_question` call (three check lines, ALL
genuinely missing dimensions with recommended values, and approval, at most 600 characters /
10 lines; no three-question cap). After the answer (including plain-Chinese corrections of
any misread number), build directly; ask again only on a new contradiction. “接受/继续”
approves the plan, not a missing value. Read [drawing intake](solidworks://skill/references/drawing-intake.md);
for axial sections, cast silhouettes, or editability rules, follow [construction strategies](solidworks://skill/references/construction-strategies.md).

```mermaid
flowchart TD
    A["Read state and acquire drawing evidence"] --> B["Analyze supplied views"]
    B --> Q{"Evidence read succeeded?"}
    Q -- "No: L1" --> R["Reduce read scope and increment L1"]
    R --> B
    Q -- "Yes" --> P["Find and analyze pictorial 3D view"]
    P --> S["Classify section views and infer cut geometry"]
    S --> O["Identify views and lock orientation contract"]
    O --> C["Match edges and corners across views"]
    C --> D["One ask_user_question: reading + compact plan + all missing dimensions"]
    D --> E{"Required values supplied and plan approved?"}
    E -- "New answer contradiction/user revision: L2" --> D
    E -- "Yes" --> F["Next call: submit the complete prepared graph"]
    F --> G["Inspect response and objective model state"]
    G --> H{"Milestone matches the plan?"}
    H -- "No: L4" --> X["Set REPLAN_REQUIRED and revise the plan"]
    X --> E
    H -- "Yes" --> I["Tick todo briefly; no plan re-print"]
    I --> J{"More modeling milestones?"}
    J -- "Yes: L3" --> F
    J -- "No" --> K["Validate projections and supplied sections"]
    K --> L{"All available views and sections match?"}
    L -- "No: L5" --> X
    L -- "Yes" --> M["Publish outputs, finish job, and report"]
```

The graph has `N=19`, `E=23`, one component, independent loop rank
`E-N+1=5`, and McCabe path complexity `E-N+2=6`. Retry-control locations must
equal the loop rank: exactly these five rows.

| ID | Controlled back edge | Maximum traversal count |
| --- | --- | --- |
| L1 | reduced evidence read -> view analysis | 1 |
| L2 | plan not ready/approved -> PLAN revision | Only a user-requested revision or a new contradiction in the answer; no repeated same-matter approval |
| L3 | more milestones -> next milestone | `max(N-1,0)` for `N` modeling todos in the approved plan |
| L4 | milestone mismatch -> replan | 2 per stable modeling todo |
| L5 | directional/sectional mismatch -> replan | 3 per target |

Before a back edge: stop if its count is at maximum; otherwise increment, persist,
then traverse. Counts survive tool changes, hypotheses, and plan versions; reset
only for a changed target or explicit user reset. Keep `L3` by approved
`PLAN_VERSION`, `L4` by todo ID, and copy pre-PLAN `L1` into PLAN.

`L2` never splits missing dimensions and plan approval into separate questions.
Batch all genuine UNKNOWNs with the compact plan; an answer permits building.
Waiting for input does not increment it; user-requested revisions do. Do not add another automatic retry path. Confirmation pauses are not retries.
If a flow edit adds or removes a back edge, recompute the graph and table.

Every unresolved modeling constraint is UNKNOWN after checking explicit dimensions,
geometry, symmetry, concentricity, tangency, equalities and dimension chains. A unique
DERIVED result needs no user confirmation. Never estimate a dimension from pixels.
A revolve claims roundness: each radius it makes must appear as a full circle in the view along its axis.
A different outline circle there, a band breaking it, or contours behind a full section that differ
from the cut profile mean the form varies around the axis: revolve only what is round, extrude the rest, or ask.

At exhaustion, stop mutation:

```text
STATUS: RETRY_EXHAUSTED
LOOP_ID: <L1-L5>
ATTEMPTS: <count/max>
LAST_CAUSE: <failure or mismatch>
ACTION: Stop mutation and request decisive user input.
```

## Inspection

Use `inspect_state` with no credentials only for `state` or `reference`; document analysis requires the active job credentials.

| Document | Supported scopes |
| --- | --- |
| Part | `features`, `geometry`, `mass_properties`, `bodies`, `faces`, `edges`, `selection` |
| Assembly | `components`, `components_flat`, `mates`, `faces`, `edges`, `selection` |
| Drawing | `drawing`, `selection` |

Assembly `faces` and `edges` also require `component`. Use identifiers returned
by current inspection data. Reinspect after structural edits because prior
face/edge indexes may no longer identify the same geometry.

Reference topics are `contract`, `canonicalization`, `forward`, `mapping`,
`mapping_part`, `mapping_sheet_metal`, `mapping_assembly`, `verification`,
`reverse`, `coverage`, `feature_graph_part_card`, `feature_graph_quickref`, `feature_graph_schema`, and
`feature_graph_changelog`. A reference comes back one page at a time inside
`verification` as `topic`, `content`, and `page` (`offset`, `limit`,
`returned_lines`, `total_lines`, `total_chars`, `has_more`, `next_offset`,
`line_truncated`; `0` means the defaults, a smaller `reference_limit` may be
requested, larger is clamped; each page is at most 120 lines and about 6000
characters, and every serialized page stays under 7000 characters). Follow
`page.next_offset` while `page.has_more` is true, or jump with
`reference_offset` to `page.index.sections[].line`. For `feature_graph_schema`,
`page.index` also has `sections` (`section`, `line`, `entries`) and
`feature_types`.

If an evidence read is too broad, never repeat it unchanged. Traverse `L1` once
using a smaller image/PDF region or a narrower supported `inspect_state` scope;
do not invent unsupported flags. On another failure emit `RETRY_EXHAUSTED`.

## Feature Graph

For a new part or assembly, see the [common part card](solidworks://skill/references/feature-graph-part-card.md).

1. BEFORE the one question, in ONE parallel step: `start_job`, feature_graph_part_card
   (common part grammar + complete example), the matching mapping_part,
   mapping_sheet_metal or mapping_assembly, and the drawing reads. Read
   feature_graph_quickref or indexed schema sections only for constructs the card
   cannot settle, also before the question; never walk full schema pages. Grade the
   drawing simple or complex (the user's 简单图/复杂图 wins); simple reads nothing extra.
2. After approval the NEXT call is `submit_feature_graph(..., fresh_document=true)`
   with ONE complete graph in feature-tree dependency order, fillets and chamfers
   included. Between approval and submission: no reference reads, no plan or todo
   rewrite, no second confirmation, no turn end. A fresh-document graph needs no
   batch confirmation. Meters for distances, radians for angles.
3. Verify from the submit result first, then ONE parallel step of compact geometry,
   bodies and features reads (edges/faces only to locate a problem). Save a new file, then
   finish_job; act on its PLANNED_FEATURES_MISSING and CREATED_DOCUMENT_UNSAVED warnings.

A graph-level `material` that the library positively does not contain is reported in
`warnings` as `MATERIAL_NOT_FOUND` while the status stays `completed`: the geometry is kept, and
the warning names the material actually on the part afterwards (`material_on_part`, which may be
one it already had). `MATERIAL_NOT_APPLIED` — present in the library but not applied, or a library
that could not be read — is a real failure and rolls the graph back. Never substitute a
similar-looking material to silence a warning: apply the exact library name, or add the specified
material to the library, with `apply_document_edits`.
Spell a material name exactly as the **loaded** library holds it, which on a localised install is
the localised spelling (this install is Chinese: `1060 合金`, not `1060 Alloy`); the same material
under its English spelling is positively absent from the loaded database and warns.

Do not split a complete graph into low-level modeling calls. Do not invent graph
fields, feature types, selectors, units, or references absent from the schema.
The [capability map](solidworks://skill/references/feature-graph-capability-map.md) summarizes
expressible constructs; record inexpressible ones as `CAPABILITY_GAP`.

For a part whose complete graph would be oversized (asymmetric cast parts,
crossed-bore housings), submit ordered milestones as SEQUENTIAL graphs against
the SAME locked document: the first with `fresh_document=true`, later ones with
`fresh_document=false` into the active document after inspecting the live
geometry and anchoring to it (see the milestone strategy in
[reconstruction-reasoning.md](solidworks://skill/references/reconstruction-reasoning.md)). Never
shrink a graph by dropping evidenced features — report the split in the plan
instead.

## Cosmetic Threads
Threads are COSMETIC only: an annotation the drawing reads, with zero geometry
change (no faces or edges added). Author `hole.thread` (designation, optional
`class`, and `depth` — required for a blind hole) or a standalone
`cosmetic_thread` node whose `ref` names the bore mouth by `edge_anchor`
(`near` = the mouth circle's CENTRE, plus `radius`) or by `bore_face`.
The pilot is a tapped hole's `diameter`, never the tap drill: the drill comes
from the table (M5 4.2, M6 5.0, M8x1.25 6.8, M12x1.75 10.2, M40x2 38,
M42x2 40 mm), and a `diameter` that disagrees with it is rejected as
`PILOT_MISMATCH`. The diameter handed to SolidWorks is the NOMINAL MAJOR
diameter in meters. Put the class-qualified spec in the callout/note (e.g.
`M5-8H`): SolidWorks 2025 does not resolve `Size`, so the note carries it.
Write the class in UPPERCASE (6H): lowercase (6g, 6h) means an EXTERNAL
thread class, which v1 does not support and rejects.
A counterbore is cut BEFORE the pilot. `add_cosmetic_thread` is additive per
edge — never retry it. Inch and real (cut) threads are out of scope.

## Existing Document Edits

`apply_document_edits` accepts only an array of `{tool, params}` objects using
this allowlist: `modify_dimension`, `edit_feature`, `set_part_material`,
`add_edge_feature`, `create_pattern`, `add_reference_geometry`,
`add_sketch_constraint`, `insert_toolbox_component`, and
`create_exploded_view`. Use current feature names and inspected geometry
references; reinspect after topology changes. Read the relevant reference before
unfamiliar parameters. Deletion and large batches on existing documents enter confirmation.

## Drawing Workflow

Use `drawing_workflow` for production drawings: model/display views, flat
patterns, section/detail views, model annotations, dimensions, center marks,
BOM/balloons, hole tables, and standards checks. Save the source model first when
views need a stable path. Verify the drawing identity, view count, references,
dimensions, scale, tables, and standards findings returned by the tool.

## Confirmation

For `status="pending_confirmation"`, stop mutation and call
`request_confirmation` with the same job and action IDs. Show its exact
`action_summary`; wait for a new message explicitly approving it; then call
`confirm_action` with the returned action ID/token, exact `confirmation_text` as
`user_confirmation`, and exact summary.

The FIRST save of the part this job created (proved by its stable document_id) to a path
that does not exist runs at once. Any other save naming a path (another untitled document,
a saved document to a new path) is a Save As that renames the open document: expect
`risk="save_as"` naming document and target. Overwrites confirm. On pending_confirmation call
request_confirmation then confirm_action at once (DSH asks the user); never end the turn.

Do not infer approval from the original request. Do not reuse an old or consumed
confirmation token. If risk or summary changes, present the new summary and wait
for another explicit user message. Never automatically re-enter confirmation.

## Transactions And Failures

Feature Graph execution, document edits, and drawing workflows run inside the
SolidWorks undo transaction layer. Existing files also receive a first-write
backup when possible.

Handle response status as follows:

| Status | Action |
| --- | --- |
| `completed` | Continue only after checking `verification`, `partial_state`, and `completed_steps`. |
| `pending_confirmation` | Enter the confirmation workflow and pause. |
| `failed` | Stop; inspect `error` and `recovery_status`. |
| `partial` | Stop; some state may remain. Report `completed_steps`, `partial_state`, backup, and recovery result. |
| `verification_failed` | Stop; execution occurred but objective verification did not pass. |
| `Error: Request timed out` (no JSON envelope) | Not a failure: the call may still be running. Never resubmit or report failure; re-read `inspect_state(scope="state")` until it answers, then act on what it shows. |

Only `discarded` permits automatic rebuilding. Existing-document Undo is unverified.
`rollback_failed` or `recovery_unverified` requires user confirmation before continuing.
A backup is evidence, not proof of restored geometry. Confirmation never closes residual docs.

## Verification And Trust

Before completion, verify every applicable exposed item: document identity/type,
rebuild and feature order, topology, dimensions, directions, bounding box,
body/component/mate counts, relevant mass properties, drawing content/standards,
and nonempty requested outputs.

Use the returned trust level accurately:

- `verified`: objective final verification passed with no warning, measuring at least one `dimension_ledger` dimension.
- `drawing_consistent`: drawing-direction evidence agrees without a reference
  solid comparison.
- `built_unverified`: construction succeeded but some objective evidence was unavailable
  or disagreed; `finish_job` names each one in `verification_gaps` (report them).
- `unverified`: do not claim completion as verified.

`finish_job` is mandatory for a completed write workflow.

## Drawing Reconstruction

For a model reconstructed from images, PDF pages, orthographic views, or sections:

Default pre-read: `feature_graph_part_card` plus the corresponding `mapping_part`,
`mapping_sheet_metal` or `mapping_assembly`. For a specific reasoning, annotation,
standard-feature or assembly question, read only the relevant section of
[reconstruction reasoning](solidworks://skill/references/reconstruction-reasoning.md),
[part drawing evidence](solidworks://skill/references/part-drawing-evidence.md) or
[assembly reconstruction](solidworks://skill/references/assembly-reconstruction.md).
No full-reference prerequisite and no reading-count gate.

Before PLAN, identify the projection convention; scan for a pictorial view;
classify every orthographic, section, detail, and auxiliary view; and lock the
canonical frame `+X=right`, `+Y=up`, `+Z=front`. The bottom of Top is model front
and the left of Left is model rear. Record a signed `CAD_AXIS_MAP` for an existing
model.

Use explicit dimensions and annotations for values and standard feature meaning.
Use a pictorial view first for topology, handedness, and spatial placement; use a
section first for material/void evidence on its cut plane. Cross-check ordinary
views by visible outline, hidden line, then centerline. Pixel measurement is only
topological evidence, NEVER a source of modeled dimensions. Log conflicts and UNKNOWNs.

Resolve the envelope, normal surfaces, internal features, inclined/oblique
surfaces, then curved/cylindrical features. Assign `F##` faces, `E##` edges,
`V##` vertices, and `S##` section details. Locate every critical face, edge, and
vertex from at least two independent projections; use the third to confirm or
explain hiding, coincidence, tangency, or point projection. Auxiliary views
control true shape/size of inclined faces. Generate a missing view only by
projecting resolved 3D topology. If topology remains nonunique after all supplied
evidence, set `RECONSTRUCTION_STATUS: BLOCKED` rather than guessing.

### Plan And User-Facing Mechanical Checklist

Resolve the full reconstruction plan internally before mutating, but translate all
user-facing text into plain Simplified Chinese mechanical drafting language (see the
full template and translation rules in [drawing intake](solidworks://skill/references/drawing-intake.md)):

- **Internal reasoning coverage (keep internal — NEVER dump raw headers or tokens to the user)**:
  Verify view roles, first/third-angle projection, canonical frame (`+X=right; +Y=up; +Z=front`),
  signed `CAD_AXIS_MAP`, pictorial/section/auxiliary findings, `F##/E##/V##/S##` cross-view
  correspondence, `EXPLICIT/DERIVED/USER/UNKNOWN` dimension basis, thread/fastener semantics,
  manufacturing vs. reconstruction completeness, ordered milestones (`M1`, `M2`, ...), `PLAN_VERSION`,
  loop counters (`L1–L5`), and per-view validation checks. Use `exact` mode for dimensioned
  drawings (never offer a schematic option; check every whole view before marking UNKNOWN;
  `schematic` applies only when requested and still requires USER normalization, never pixel
  ratios). Consult the [capability map](solidworks://skill/references/feature-graph-capability-map.md)
  when expressibility is unclear and log inexpressible constructs as `CAPABILITY_GAP`.
- **Strict human-language isolation**: Never show `EXPLICIT`, `DERIVED`, `USER`, `UNKNOWN`,
  `F01/E01/V01/S01`, `MODEL_FRAME`, `CAD_AXIS_MAP`, `PLAN_VERSION`, `LOOP_COUNTS`, `expect`,
  `dimension_ledger`, `fresh_document`, Feature Graph node/field names, or meter (`0.012 m`) /
  radian (`1.57 rad`) conversions in message text, questions, or final replies. Always use
  Chinese mechanical and SolidWorks terms with **`mm`** and **`°`** (`Ø`, `R`, `M`, `C`).
- **Pre-question message text — 10-second engineer checklist (breaks `dimension_ledger` same-source blind spot)**:
  Because `dimension_ledger` is authored from the same reading as the graph, a misread digit
  (e.g. `Ø12` read as `Ø20`) passes self-checks silently. In the message text accompanying the
  single `ask_user_question`, always present three concise Chinese sections:
  1. **总包络核对**: Include `“总包络 长×宽×高 = a×b×c mm”` with drawing directions and volume (`未算` if non-closed).
  2. **已读出的关键特征与尺寸核对清单**: Group by Chinese feature names (底板/法兰、凸台/圆筒、内腔/槽宽、
     通孔/沉头孔/螺纹孔直径与中心距、壁厚/筋厚、圆角/倒角) and list key `mm`/`°` numbers with source views
     so the engineer can spot any misread number in 10 seconds.
  3. **建模分步简述**: 3–6 lines using SolidWorks terms (基准面拉伸凸台、旋转、拉伸切除、打孔、镜像/阵列、圆角/倒角).
- **One compact `ask_user_question` round**: Keep the dialog payload `<= 600` chars and `<= 10`
  lines (three summary check lines + ALL genuine missing dimensions with concrete recommended
  values + approval; never offer an empty “提供数值” option). Plain-Chinese answers or corrections
  (e.g. “孔改成 M6”, “厚度是 8”, “大孔是 Ø12”) immediately update the graph, `expect`, and ledger
  and proceed to `submit_feature_graph` without a repeat question unless a new contradiction arises.
  Waiting does not consume `L2`; any later revision to topology, dimensions, or order increments `PLAN_VERSION`.

### Execute, Checkpoint, And Validate

The approved plan is already in context: do not reread or re-present it. The job
and references were prepared before the question, so submit at once: one complete
`submit_feature_graph(..., fresh_document=true)` for a new model. Do not split a
complete graph merely to create checkpoints.

Encode design intent and human editability (see [construction strategies](solidworks://skill/references/construction-strategies.md)):
anchor primary sketches to the origin and standard datum/offset planes rather than
fragile derived edges; center symmetric parts with `mid_plane` extrusions; keep each
sketch single-purpose and cleanly constrained; use `through_all` for through cuts/holes;
use mirror/pattern for evidenced symmetry/repetition; and order features from base stock ->
secondary bosses/ribs -> major cuts/slots -> holes/threads -> patterns -> **all `fillet`
and `chamfer` nodes strictly last** so manual dimension tweaks in SolidWorks do not cascade into rebuild errors.

For every thread, nut, bolt, screw, or stud todo, record the axis, internal or
external role, cosmetic/symbolic or physical representation, designation source,
thread extent, and mating evidence. Prefer an approved standard/toolbox component
for a fully designated standard fastener. Model a physical helix only when the
request requires it and the runtime schema supports it.

One successful complete graph is one MCP milestone and may check every modeling
todo it covers after objective readback. For genuinely staged edits or drawing
operations, each high-level MCP call is one milestone and `L3` controls
continuation. Before each payload, resolve sketch plane and every extrusion, cut,
hole, mirror, pattern, and offset direction through the Orientation Contract and
`CAD_AXIS_MAP`.

After each milestone, use `inspect_state` with the smallest relevant scopes.
Compare feature results, bounds, topology, dimensions, directions, and evidence
IDs with the plan. On match, tick covered todos with short status lines; do not
re-present the plan. On mismatch, stop mutation,
use `L4`, set `REPLAN_REQUIRED`, log the smallest mismatch and root cause, revise
the plan/version and report the repair under the approved scope.
After `discarded`, L4 permits at most 2 complete repair executions of one design, persisted across jobs.
See feature_graph_part_card recovery/L4: lock all dimensions/end types, ordered feature
sequence, materials and intrinsic sketch shape; placement/reversed/frame may change.
No identical replay, reset, renumbering or splitting; supply specific new repair evidence.
If inadmissible, uncertain, context-changed or exhausted, request user confirmation;
archive the old chain as user_override and restart at 0 only after approval. Merge batch approval.
Check every `direction_flipped` feature in readback AND three views. Fix VALIDATION_FAILED at once.
Do not repeat plan approval unless the user answer introduced a new contradiction.

Validate independently:

- Front looking `-Z`: width, height, silhouette, levels, openings, and centers.
- Top looking `-Y`: drawing bottom is `+Z` front; check footprint, depth, offsets,
  openings, and front/rear placement.
- Left looking `+X`: drawing left is `-Z` rear; check transitions, thicknesses,
  cuts/holes, and front/rear placement.
- Right looking `-X`: drawing right is `-Z` rear; use this instead for a Right view.
- Each supplied section: recreate its cut plane and sightline; check cut contours,
  material/void regions, centers, transitions, connectivity, and through/blind
  depths. Missing hidden lines alone are neither PASS nor FAIL.
- Each auxiliary view: check the true shape/size and the parent-view projection;
  omitted hidden lines in a partial auxiliary view do not prove feature absence.
- Each threaded interface: check coaxiality, designation, pitch, hand, fit when
  stated, engagement length, through/blind extent, and nut open/closed topology.

Write `PASS`, `FAIL`, or `UNAVAILABLE` with evidence in each Validation Matrix
row. A completed build returns images of the part (front, top, left, isometric; hidden edges grey; again via `inspect_state(scope="views")`).
Compare each with the drawing's view feature by feature and pass that list to finish_job as view_review_json; a FAIL there means fix the graph, even when numbers agree.
FAIL = visible structure (hole, slot, step, boss, rib, outline, dimensioned fillet) missing/extra/misplaced; general-note fillets, thread representation, assumptions, defaults go in `notes` with PASS and in the final reply. A view FAIL is never a reason to stop with `built_unverified`: rebuild with a corrected fresh_document graph (not a repair-budget attempt) and finish again; a simplified stand-in (block, straight post for a curved arm) is a FAIL too.

On any directional or sectional `FAIL`, stop final save/export, traverse `L5`,
reinspect the pictorial view first when one was found, update PLAN before repair,
and repair within the approved scope. If a direction or section is `UNAVAILABLE`, do not loop
or invent a pass; finish only if MCP final verification can complete, then report
`built_unverified` and name the gap.

Without a reference solid, use `drawing_consistent` only when Front, Top, and Side
and every supplied section all pass. Mark PLAN `COMPLETE` only after required
outputs and `finish_job` succeed.

## Final Report

Report to the user in plain Simplified Chinese mechanical language (`mm` and `°`,
no raw JSON status keys, node IDs, or meter/radian decimals):

- saved document path and requested outputs (plus any superseded draft document left open)
- SolidWorks features actually built
- objective verification performed across views and key dimensions (reflecting the true
  outcome of `status`, `verification_status`, `recovery_status`, and `trust_level` honestly
  in plain Chinese — never claim verified when `unverified` or `built_unverified`)
- adopted defaults/recommendations, unavailable checks, or capability gaps

Never claim success from appearance or a tool return code alone. Keep this main
skill at or below 550 lines; disclose detail through the linked reference files.
