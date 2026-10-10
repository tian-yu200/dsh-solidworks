# Feature Graph Capability Map

Use this reference when planning any model that will be built with
`submit_feature_graph`, especially while writing inline plan modeling todos. It maps
common part-drawing constructs to the Feature Graph vocabulary and records what
the vocabulary cannot express, so gaps are declared in the plan instead of
discovered mid-build.

The runtime reference topic `feature_graph_schema` (read it page by page with
`inspect_state(scope="reference", reference_topic="feature_graph_schema")`,
following `page.next_offset` while `page.has_more` is true; a smaller
`reference_limit` may be requested, larger is clamped; `page.index.feature_types`
lists the node kinds) and
`cad-planner/contracts/feature-graph.schema.json` are authoritative. This map is
decision support as of schema 0.7.4 with recipe notes through 0.9.0; re-verify
any capability it claims before relying on it. Never invent node types, fields,
selectors, or enum values not present in the runtime schema.

## Expressible Constructs

| Drawing construct | Feature Graph expression |
| --- | --- |
| Rectangular boss/pad | `box` node, or rectangle sketch + `extrude` boss |
| Free-form prismatic body | `sketch` (line/arc/circle/ellipse/spline/rectangle primitives, exact coordinates; `slot` and `rounded_rect` as one-node convenience contours) + `extrude` boss |
| Stepped shaft, flange, revolved body | `revolve` of a half-section sketch; `axis` is the axis segment's 2D sketch coordinates, `angle` in radians |
| Through hole | `hole` node (`ref` = face + position, `through_all` only) or circle sketch + `extrude` cut `through_all` |
| Blind hole, pocket, slot | circle/`slot`/closed-contour sketch + `extrude` cut with `end: "blind"` and a depth |
| Counterbore/countersink | no dedicated node — compose concentric cuts (e.g. large shallow blind cut + small through cut) |
| Keyway, flat, milled pocket | sketch on face or datum offset + `extrude` cut |
| Tapered/lofted body | `loft` through >= 2 profile sketches referenced by id (simple blend, no guide curves) |
| Swept boss (constant section) | `sweep`: earlier path sketch by id + profile sketch immediately preceding; boss only |
| Rib/web | `rib` from an open profile sketch, thickness, one- or two-sided |
| Fillet/chamfer | `fillet`/`chamfer` post-solid nodes; each edge anchored by its pre-feature `analyze_model(edges)` midpoint plus a semantic hint |
| Repeated feature (linear) | `linear_pattern` seeding an earlier feature-producing node; single direction, `flip` for negative axis |
| Repeated feature (circular) | `circular_pattern`; axis from two canonical datums' intersection, `angle_deg`, `equal_spacing` |
| Symmetric feature | `mirror` about a canonical datum plane (`front`/`top`/`right`) over earlier feature node ids |
| Sheet metal | `sheet_metal`, `sketched_bend`, `edge_flange` with the bend-position enums |
| Part material | graph-level `material: {name, library?}` at the top of the graph (document state, not a node) |

Distance is meters, angles radians, everywhere in the graph. Datums are
`front`, `top`, `right` plus a signed meter offset; a sketch on a
non-axis-aligned face uses a `{face: face_anchor}` reference resolved by
plane containment.

## Grammar That Shapes Feature Order

- `extrude`, `revolve`, `rib`, and `sweep` consume the immediately preceding
  sketch node; place each profile sketch directly before its consumer.
- `sweep`/`loft`/patterns reference earlier nodes by id; the compiler
  substitutes their runtime feature names. Order nodes in feature-tree
  dependency order.
- `fillet`/`chamfer` edge anchors reference PRE-feature geometry: after the
  feature exists its consumed edges are gone. Take `near` midpoints from the
  pre-feature state (or `analyze_model(feature_map)`) and write a semantic
  `hint`.
- Sketches rebuild frozen-coordinate: emit exact segment coordinates in segment
  order and do not add constraints or dimensions. Identical shared endpoints
  close a contour. The compiler welds chain endpoints within 0.0001 m (0.1 mm)
  onto the first matching anchor and reports the weld count on the sketch's
  node_log note; an extrude/revolve/sheet_metal/sweep-profile/loft sketch whose
  contour is still open beyond that weld is rejected at plan time with the
  exact gap named (rib/bend/sweep-path sketches are open by design).
- Extrude end conditions are `blind`, `through_all`, `up_to_surface` (needs a
  face anchor point on the terminating face's plane), and `mid_plane`. There is
  no `up_to_next`; a trim against adjacent material needs an explicit depth or
  an `up_to_surface` anchor.

## Not Expressible — Declare A Gap

Record `CAPABILITY_GAP: <construct>` in the inline plan for any of these when the
evidence requires them. Do not emulate them with invented node types:

- Physical helical threads and thread-specific geometry (no helix/thread
  vocabulary). Model the required hole/shaft envelope only, and keep the
  designation semantic: record designation, class, depth, and representation
  decision in `FEATURE_ANNOTATIONS` per the part-drawing evidence rules.
- The `hole` node's blind, counterbored, countersunk, or tapped variants
  (`depth` accepts a meter value or `through_all` only in the covered subset —
  verify against the live schema before using a blind depth there; the sketch +
  cut path is always available).
- Draft/taper on extrusions, `up_to_next`, sweep cut, loft guide curves or
  tangency, second direction on a single `linear_pattern` (compose a grid as a
  pattern of a pattern), mirror about a non-canonical plane, surface bodies,
  multi-body combine/subtract, 3D sketches, equation-driven curves, embossed
  text.
- Assembly graphs: in-context features, flexible subassemblies, component
  patterns, and configurations beyond the referenced one are outside the
  assembly sub-vocabulary.

## Gap Protocol In PLAN

1. Write the todo against the nearest expressible envelope (for a threaded
   hole: the tap-drill or minor-diameter blind cut) or leave the feature out
   entirely — never approximate with wrong geometry presented as correct.
2. Add `CAPABILITY_GAP: <designation/feature>` to the plan, naming what the
   final model will lack (thread helix, cosmetic thread, tolerance-driven
   geometry).
3. If the omission changes topology, handedness, or fitness for the stated
   purpose, ask one focused question before building; otherwise record it as an
   explicit assumption.
4. Keep `MANUFACTURING_STATUS` honest: a model missing called-out threads is
   geometrically complete but manufacturing-incomplete.
5. Validation compares against supplied views with the same limitation: mark a
   view check `UNAVAILABLE`, not `PASS`, when the missing construct is part of
   what the view shows.

This reference adds no retry loop; reuse the existing plan revision (`L2`) and
replan (`L4`) controls for any gap-driven plan change.
