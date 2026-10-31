# Feature Graph — common part card

Default: this card + mapping_part. For sheet metal/assembly use its mapping;
lookup quickref/indexed schema only for missing constructs. Never page full schema
as a ritual. This is a grammar example, not permission to invent drawing dimensions.

Units: graph.units="meters"; all lengths m, all angles radians. Convert mm /1000.
Each node needs unique id and type; order is dependency/feature-tree order.
ref is an OBJECT, never a string. Node IDs are graph IDs, not runtime face indices.
Omit graph.material when unspecified. Save a new working-directory file; no overwrite.

Datum frames (sketch x,y; positive extrusion normal):
front = +X,+Y; +Z. top = +X,-Z; +Y. right = -Z,+Y; +X.
ref={"datum":"top"}; optional signed offset shifts along the normal.
ref={"node_face":{"node":"base","selector":"top"},"position":"center"}
selects a previous body's top face and its center. For off-center holes use
position:"point", at:[x,y,z] in MODEL meters, on the selected face; do not use
unverified face indices. Other face anchors require inspected geometry.

Common nodes (required fields shown; consult schema for other end conditions):
- box: width, depth, height >0; optional datum ref (default top). Centered footprint.
- sketch: ref plus profile array. rectangle {kind,width,height,cx?,cy?}; circle
  {kind,diameter,cx?,cy?}; or lines {kind:"line",x1,y1,x2,y2} and arcs
  {kind:"arc",cx,cy,x1,y1,x2,y2,dir:1|-1}. Exact shared endpoints form a CLOSED
  chain before extrusion; construction:true is scaffolding, not the solid contour.
- extrude: sketch:"immediately_preceding_sketch_id", operation:"boss"|"cut",
  depth positive (blind default). end:"through_all" for a through cut; end:"mid_plane"
  uses depth as TOTAL thickness. reversed:true flips growth; it is not a plane offset.
- hole: ref as above, diameter >0, depth:"through_all" or positive blind depth.
- fillet: radius >0, edges:[{near:[x,y,z]}]. near is any MODEL-space point inside the
  edge (a full circle: anywhere on it), never a shared vertex or arc center. Ambiguous
  or absent anchors fail. Add every drawing fillet/chamfer; never silently omit one.

Complete example: 50 mm cube on top datum, blind side pocket, top-center through
hole, then 1 mm round on the +X/+Y edge. All numbers here are example facts only.
A fresh graph is submitted via submit_feature_graph(fresh_document=true); use job
credentials and retain the normal server confirmation/verification procedures.
```json
{
  "schema_version":"0.8.0-draft","units":"meters",
  "nodes":[
    {"id":"base","type":"box","ref":{"datum":"top"},"width":0.05,"depth":0.05,"height":0.05},
    {"id":"pocket","type":"sketch","ref":{"datum":"front","offset":0.025},"profile":[{"kind":"rectangle","width":0.01,"height":0.01,"cy":0.025}]},
    {"id":"pocket_cut","type":"extrude","sketch":"pocket","operation":"cut","depth":0.005,"reversed":true},
    {"id":"bore","type":"hole","ref":{"node_face":{"node":"base","selector":"top"},"position":"center"},"diameter":0.008,"depth":"through_all"},
    {"id":"edge_round","type":"fillet","radius":0.001,"edges":[{"near":[0.025,0.05,0]}]}
  ]
}
```

Return statuses are not dimensional verification. Inspect rebuilt geometry and
compare all supplied views; report unavailable checks honestly. Keep the complete
feature list, EXPLICIT/DERIVED/USER/UNKNOWN sources, and the approved compact plan.

## Execution recovery and L4

Datum/offset defaults use the canonical axes above; face selection preserves model position.
Recorded sketches keep measured explicit frames. The compiler checks orthonormal/coincident
frames and maps coordinates/direction. A null cut retries the opposite direction; success
reports result_geometry.direction_flipped=true, node/operation flags and DIRECTION_FLIPPED
warnings. Check that feature's location, depth and direction in readback AND all three views.

Only discarded permits AUTOMATIC rebuilding: owned document absent, old documents unchanged,
Undo released, job unbound.
Report operation_target separately from active_document_after_recovery; never write the latter.
Initial execution plus at most 2 repairs is persisted across jobs. No reset, renumbering,
identical replay or split graphs. Supply repair_evidence_json (error_code matching the last
failure, cause, readback, change); the second repair needs new evidence.

The server locks node count and ordered (type, operation), material name/library, ALL
non-placement dimensions/specifications and end types (blind/through_all/mid_plane/up_to),
and each sketch's intrinsic shape, with 1e-9 m dimension tolerance. It allows placement anchors, reversed,
ref (datum/face/signed offset), frame and ONE whole-sketch translation/rotation/mirror;
node ids/names/descriptions may change. Do not change diameters, depths, radii, hole/thread
specs, counts, spacing, angles, thickness, sketch lengths or omit features. Moving separate
primitives independently is not a whole-sketch placement repair.

Inadmissible design/evidence, exhausted budget, context changes, in_flight or uncertain
recovery returns pending_confirmation. Show the Chinese reason; request_confirmation/
confirm_action asks the USER. Approval archives closed_by=user_override and starts a new
first execution at count 0; it neither closes residual documents nor proves Undo restoration.
A fresh-document graph needs no batch confirmation (plan approval authorizes it);
an existing-document graph over 10 nodes does. Recovery needs at most ONE confirmation.
Existing-document failures always report recovery_unverified and prohibit automatic retry;
continuation also requires user confirmation. First save of the own part: no prompt.
Document blocks use normalized saved path or unsaved stable document_id, never title/empty
identity; absent instances retire automatically and explicit approval can release the target.
