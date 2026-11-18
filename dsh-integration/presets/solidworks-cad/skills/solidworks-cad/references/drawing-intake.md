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

## Defaults, user-facing Chinese mechanical language, and 10-second checklist

Use mm by default; an explicit drawing unit overrides it. Do not set a material
that was not specified. Save a new, non-overwriting file in the working directory.
Use projection symbols, labels, aligned views and topology to resolve projection;
ask only if unresolved alternatives create different parts.

### Internal reasoning vs. user-facing mechanical Chinese (strict boundary)

Mechanical engineers know SolidWorks and engineering drawings, not AI or compiler
internals. Keep internal reasoning symbols strictly inside your reasoning and tool
payloads — **NEVER** show any of the following in user-visible message text,
`ask_user_question` prompts/options, or final reports:

- Internal source tags: `EXPLICIT`, `DERIVED`, `USER`, `UNKNOWN`.
- Topology / frame tokens: `F01`, `E01`, `V01`, `S01`, `MODEL_FRAME`, `CAD_AXIS_MAP`,
  `PLAN_VERSION`, `LOOP_COUNTS`, `RECONSTRUCTION_STATUS`.
- Schema / tool keys: `expect`, `bbox_mm`, `dimension_ledger`, `fresh_document`,
  `node_face`, `edge_anchor`, `through_all`, `mid_plane`, `source_kind`.
- SI conversion values or steps: meters (`0.012 m`) or radians (`1.5708 rad`).

Always address the user in plain Simplified Chinese mechanical drafting and
SolidWorks terminology, using **`mm`** for all lengths and **`°`** for all angles
(`Ø` for diameters, `R` for radii, `M` for metric threads, `C` or `倒角 a×45°` for
chamfers, “主视图/俯视图/左视图/A-A剖视图”, “前视/上视/右视基准面”, “拉伸凸台、两侧对称拉伸、
旋转凸台、拉伸切除、贯穿通孔、盲孔、沉头孔、螺纹孔、加强筋、镜像、阵列、圆角、倒角”).

### 10-second key dimension checklist (breaking the `dimension_ledger` blind spot)

Because `dimension_ledger` and the Feature Graph are authored by the same model from
the same reading, misreading a digit (e.g. reading `Ø12` as `Ø20`) passes self-checks
silently and builds a scrap part. Before the single `ask_user_question` call, your
**message text in the same step** MUST present a clean, scannable Chinese checklist
so the engineer can catch any misread number against the drawing in 10 seconds:

1. **① 总包络核对**: Include exact format `“总包络 长×宽×高 = a×b×c mm”` with drawing
   directions (e.g. 左右长 × 前后宽 × 上下高) and estimated volume (or `未算`).
2. **② 已读出的关键特征与尺寸核对清单**: Group by recognizable Chinese mechanical features
   (底板/法兰、主凸台/圆筒、内腔/台阶孔、安装孔/螺纹孔、键槽/切口、壁厚/筋厚、圆角/倒角) and list
   every key read or derived value in `mm`/`°` with its source view (for derived
   values, state plain arithmetic like `中心距 70 mm（总长 90 减两侧边距 10）`). Flag any
   low-confidence reading in plain Chinese.
3. **③ 建模分步简述**: 3–6 concise lines in SolidWorks Chinese feature language
   (e.g. `1. 上视基准面两侧对称拉伸底板 100×60×10 mm；2. 底板顶面拉伸中心圆筒 Ø40×高 25 mm；
   3. 贯穿切除 Ø12 中心通孔并打 4×M6 安装通孔（中心距 80×40 mm）；4. 末尾添加 R3 圆角与 C1 倒角`).

Example message text before `ask_user_question`:
```text
已完成图纸识读，请花 10 秒对照图纸核对以下关键尺寸是否有认错数字：

1. 总包络核对
- 总包络 长×宽×高 = 100×60×35 mm（左右长 100 × 前后宽 60 × 上下总高 35 mm，体积约 未算）

2. 关键特征与已读尺寸核对清单
- 底板（主视/俯视）：长 100 mm × 宽 60 mm，板厚 10 mm，四角圆角 R8
- 中心圆筒凸台（主视/A-A剖视）：外径 Ø40 mm，自底板顶面向上凸起 25 mm（总高 35 mm）
- 中心台阶通孔（A-A剖视）：上部沉孔 Ø24×深 8 mm，下部贯穿通孔 Ø12 mm
- 四角安装孔（俯视）：4×M6 螺纹通孔，孔距 80×40 mm（对中心对称）
- 底部减重槽（A-A剖视）：宽 30 mm × 深 3 mm，贯通前后
- 未注圆角与倒角（技术要求）：其余铸造圆角待确认（推荐 R3），孔口倒角 C1

3. SolidWorks 建模分步简述
1) 在上视基准面拉伸底板（100×60×10 mm，四角 R8）；
2) 在底板顶面拉伸中心圆筒凸台（Ø40，高 25 mm）；
3) 切除底部 30×3 mm 贯通槽、中心 Ø24×8 沉孔与 Ø12 通孔；
4) 在底板四角打 4×M6 螺纹通孔（中心距 80×40 mm）；
5) 在特征树末尾统一添加 R3 圆角与 C1 倒角。

如发现上方任意尺寸看错（或需修改待定项），直接点选下方推荐项或用日常机械话术回复即可（例如“中心通孔是 Ø14”、“底板厚 12”、“孔改成 M8”）。
```

### Compact `ask_user_question` prompt and plain-Chinese options

Keep the `ask_user_question` payload itself at **at most 600 characters and 10 lines**
(the DSH dialog does not scroll): in ONE ask_user_question call include only the three
summary check lines (`总包络 长×宽×高 = a×b×c mm`, `体积约 V mm³`, and `最没把握的 3 处读法`
in plain Chinese), ALL genuinely missing dimensions, and plan approval; no hard limit
(no three-question cap). Every missing-dimension option must carry a concrete
recommended value in plain Chinese (e.g. `未注圆角采用 R3（推荐）`, never an empty
`提供数值` option). A fully resolved drawing asks only that one compact approval round.

After the user answers, build directly:
- Selecting the recommendation or replying “接受/继续/按推荐/批准” adopts recommended
  values as user-approved (`USER` internally) — build immediately without re-asking.
- Note that bare “接受/继续” on an option without a number supplies approval, not an absent numeric value (which is why every option must carry a recommended value).
- If the user corrects a misread dimension in plain Chinese (e.g. “孔改成 M6”,
  “厚度是 8”, “大孔是 Ø12 不是 Ø20”) and it introduces no new geometric contradiction,
  update the Feature Graph, `expect`, and `dimension_ledger` with the corrected value
  and call `submit_feature_graph(fresh_document=true)` immediately — do NOT ask a
  second confirmation round. Ask again only if the answer creates a NEW contradiction;
  report BLOCKED only if the user explicitly rejects recommendations without giving a value.

## Optional tool use and attachment IDs

Both read_drawing_text and read_drawing_region accept a local image or PDF path OR
sha256:<64 hexadecimal characters>. The current instance resolves an attachment
under DSH_HOME/attachments/v1/objects/<first two hex>/<hash> (default home ~/.dsh).
An invalid ID or absent object produces one clear error; do not guess paths.
URLs including file:// are refused. modlens_read_image remains local-path-only.

A PDF drawing is the one case with no original image in view: DSH passes a PDF as a
file handle (a saved path in the message text), and there is no file tool. Pass that
path to read_drawing_region with an empty region; the overview renders page 1 and its
`pdf.pages` list gives every page's size and title text. Choose the drawing's page,
read that page's overview with page=N, then crop with the same page=N. One page can
hold several part drawings: name the part you model. Never claim to have read a PDF
the tools did not render.

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
