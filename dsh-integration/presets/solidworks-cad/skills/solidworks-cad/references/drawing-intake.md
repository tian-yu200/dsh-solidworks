# Drawing Intake — original image first

Directly inspect the attached original drawing before calling aids. OCR and crop
reads are OPTIONAL, chosen only for unreadable or conflicting evidence. Avoid
serial round trips: independent regions can share one batch (1–12); a single
region is equally valid. There is no per-image crop quota, fixed round count or
model-controlled reset. Keep file/pixel/decode/output, coordinate and zoom limits.

## Evidence and dimension ledger

Record each parameter as EXPLICIT / DERIVED / USER / UNKNOWN, with units, view,
feature and source location. EXPLICIT is visibly stated; OCR is a candidate until
checked against the image. DERIVED is a unique geometric result: retain the
formula, EXPLICIT/USER inputs and evidenced constraints. USER is a specific
user-supplied value/relation. UNKNOWN is still underconstrained or contradictory.

Examples:
- Overall 90 and two equal margins 10 determine span 70: DERIVED = 90−2×10.
- A concentric hole needs no separate center coordinate when both axes are evidenced.
- R10 tangent to two resolved perpendicular lines has a derived center; do not ask for it.
- A slot offset lacking any datum, symmetry or dimension chain is UNKNOWN: ask for its offset.
- A visible small fillet without a readable radius is still a required feature; ask for
  the radius when it cannot be derived. Never drop it to make the graph smaller.

Check every supplied view, section, hole/thread/depth callout, dimension chain,
symmetry, equality, concentricity, tangency, radius and small corner detail.
Do not turn every absent label into a missing dimension. NEVER estimate dimensions
from pixel proportions, image scale or shape appearance, even after plan approval.

## Defaults and one question round

Use mm by default; an explicit drawing unit overrides it. Do not set a material
that was not specified. Save a new, non-overwriting file in the working directory.
Use projection symbols, labels, aligned views and topology to resolve projection;
ask only if unresolved alternatives create different parts.

Combine the reading conclusion, compact PLAN.md-style plan (including feature
completeness, milestones, outputs and validation), and ALL genuine missing dimensions
in ONE ask_user_question. Include plan approval in that same round; no hard limit
of three questions. A fully resolved drawing only needs that compact plan approval,
not invented dimension questions. A previously approved matching plan needs no repeat.
After the answer, build directly; ask again only if the answer introduces a NEW
contradiction. “接受/继续” supplies approval, not an absent numeric value. If required
numbers remain absent, report BLOCKED without guessing or re-asking the same question.

## Optional tool use and attachment IDs

Both read_drawing_text and read_drawing_region accept a local image path OR
sha256:<64 hexadecimal characters>. The current instance resolves an attachment
under DSH_HOME/attachments/v1/objects/<first two hex>/<hash> (default home ~/.dsh).
An invalid ID or absent object produces one clear error; do not guess paths.
URLs including file:// are refused. modlens_read_image remains local-path-only.

Read with ONE batch of whole-view boxes at zoom 2, e.g.
regions=["45,45,430,610","65,610,635,905","690,80,1050,525"] (front, bottom, view A):
a dimension must stay with its dimension line and the feature it points to. Grid cells
("C4") and zoom 3–4 are only for one unreadable number, boxed with its feature. An empty
region returns the optional A–H × 1–6 gridded overview. Boxes use source pixel
coordinates to locate evidence, never to measure physical dimensions.
Keep crop paths as citations; malformed regions fail explicitly without clipping.

read_drawing_text returns optional OCR candidates (raw text, positions, rotation,
nullable confidence). A tool failure is a technical failure, not a missing dimension.
The policy detects three consecutive redundant/failed image reads within one
host Agent/turn; it blocks the repeated attempt and logs the count. A materially
new successful evidence read restores progress; only host task changes discard
state. Report a stalled evidence/tool chain once; do not ask the user repeatedly
for image paths or disguise infrastructure failures as dimension questions.

No package installation, alternate CAD kernels, shell/COM, macros or pixel-based
size estimation during intake. Continue through the approved SolidWorks tools.
