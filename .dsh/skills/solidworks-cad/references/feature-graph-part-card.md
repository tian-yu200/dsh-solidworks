# Feature Graph — common part card

Default: card + mapping_part. Use sheet-metal/assembly maps; quickref/schema for gaps.
Examples are grammar, not drawing evidence. Pass local drawing as drawing_path to start_job.

Units: graph.units="meters"; lengths m, angles radians (/1000 from mm).
Each node needs unique id and type in dependency order. ref is an OBJECT, never string.
Node IDs are graph IDs, not face indices. Omit graph.material when unspecified.
Save a new working-directory file; no overwrite.
New part REQUIRES graph.expect from approved plan: bbox_mm:[a,b,c] (MM, any order),
volume_mm3?, features?:{"fillet":n,"chamfer":n}. Off-plan build: PLAN_MISMATCH.
dimension_ledger (exact keys; value/tolerance_mm/selector_tolerance_mm in mm; unknown->value:null,check:null):
[{id,label,value,source_kind:"explicit"|"derived"|"user"|"unknown",view,node_ids:[id,...],check}].
check: {kind:"bbox_extent",axis:"x"|"y"|"z",tolerance_mm?} | {kind:"cylinder",axis:[dx,dy,dz],through:[x,y,z],tolerance_mm?,selector_tolerance_mm?} (through m-pt, value=dia mm) | {kind:"plane_distance",normal:[nx,ny,nz],point_a:[x,y,z],point_b:[x,y,z],tolerance_mm?,selector_tolerance_mm?} (m-pts).

Datum frames (sketch x,y; +extrude normal): front=+X,+Y;+Z. top=+X,-Z;+Y. right=-Z,+Y;+X.
ref={"datum":"top",offset?:m}. ref={"node_face":{"node":"base","selector":"top"},"position":"center"}
selects a body's top face center. Off-center holes: position:"point", at:[x,y,z] (MODEL m on face);
no unverified face indices. Other face anchors require inspected geometry.

Common nodes (consult schema for other end conditions):
- box: width, depth, height >0; optional datum ref (default top). Centered footprint.
- sketch: ref + profile array. rectangle {kind,width,height,cx?,cy?}; circle {kind,diameter,cx?,cy?};
  lines {kind:"line",x1,y1,x2,y2} and arcs {kind:"arc",cx,cy,x1,y1,x2,y2,dir:1|-1} (center equidistant).
  Corner round: {kind:"fillet",radius} between two segments (exact tangent arc, no edge anchor).
  Shared endpoints CLOSE chain; construction:true is scaffolding, not solid contour.
- extrude: sketch:"immediately_preceding_sketch_id", operation:"boss"|"cut", depth >0 (blind default).
  end:"through_all" for through cut; end:"mid_plane" uses depth as TOTAL thickness.
  reversed:true flips growth (not plane offset).
- hole: +Y (top) face only; diameter >0, depth:"through_all"|blind. Side bore: circle sketch + extrude cut.
- fillet: radius >0, edges:[{near:[x,y,z]}] (MODEL point inside edge; full circle: anywhere on it;
  never shared vertex or arc center). Add every drawing fillet/chamfer; never silently omit one.

Example (grammar only): 50 mm cube, blind side pocket, top-center through hole, 1 mm round;
submit_feature_graph(fresh_document=true).
```json
{
  "schema_version":"0.8.0-draft","units":"meters",
  "expect":{"bbox_mm":[50,50,50],"features":{"fillet":1}},
  "nodes":[
    {"id":"base","type":"box","ref":{"datum":"top"},"width":0.05,"depth":0.05,"height":0.05},
    {"id":"pocket","type":"sketch","ref":{"datum":"front","offset":0.025},"profile":[{"kind":"rectangle","width":0.01,"height":0.01,"cy":0.025}]},
    {"id":"pocket_cut","type":"extrude","sketch":"pocket","operation":"cut","depth":0.005,"reversed":true},
    {"id":"bore","type":"hole","ref":{"node_face":{"node":"base","selector":"top"},"position":"center"},"diameter":0.008,"depth":"through_all"},
    {"id":"edge_round","type":"fillet","radius":0.001,"edges":[{"near":[0.025,0.05,0]}]}
  ]
}
```

Completed is not verified: compare readback and all views; report missing checks and EXPLICIT/DERIVED/USER/UNKNOWN sources.

## Execution recovery and L4

Datum/offset defaults use canonical axes above; face selection preserves model position.
Recorded sketches keep measured explicit frames; compiler checks orthonormal/coincident frames.
A null cut retries opposite direction; success reports result_geometry.direction_flipped=true and
DIRECTION_FLIPPED warnings — verify location, depth and direction in readback AND all three views.

Only discarded permits AUTOMATIC rebuilding: owned document absent, old documents unchanged,
Undo released, job unbound. Report operation_target separately from active_document_after_recovery;
never write the latter. Initial execution + at most 2 repairs of one design persists across jobs
(no reset, renumbering, identical replay or split graphs). Supply repair_evidence_json
(error_code matching last failure, cause, readback, change); second repair needs new evidence.

Server locks node count, ordered (type, operation), material, ALL non-placement dimensions/specs,
end types, and intrinsic sketch shape (1e-9 m tolerance). Allowed: placement anchors, reversed,
ref (datum/face/signed offset), frame and ONE whole-sketch translation/rotation/mirror; node ids/names
may change. Do not alter diameters, depths, radii, hole/thread specs, counts, spacing, angles,
thickness, sketch lengths or omit features. Moving primitives independently is not a placement repair.
After PLAN_MISMATCH only expect is locked: fix dimensions/features to match the approved plan.

Inadmissible design/evidence, exhausted budget, context changes, in_flight or uncertain recovery
returns pending_confirmation. Show Chinese reason; request_confirmation/confirm_action asks USER
(archives closed_by=user_override, resets count to 0; neither closes residual docs nor proves Undo).
Fresh-document graph needs no batch confirmation (plan approval authorizes it); existing-document >10
nodes does. Recovery needs at most ONE confirmation. Existing-document failures report recovery_unverified
and prohibit automatic retry. First save of own part: no prompt.
