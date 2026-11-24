# Construction Strategies

Choose these strategies when the drawing provides an axial section or complementary orthogonal silhouettes. Establish the projection, shared datums, axis directions and section material/void boundaries first. For an unmarked contour, exhaust multi-view correspondence, symmetry, tangency, concentricity and dimension chains. Record a unique result as DERIVED with its formula and evidence; collect only genuinely nonunique constraints in the existing question round.

## S1: Axisymmetric Sections

Partition the part by rotational symmetry. Revolve only regions whose surfaces remain circular through 360 degrees; represent bands, lugs and asymmetric shell boundaries with separate features from their controlling views.

Trace one closed half-section for each continuous axisymmetric region. Map every axial station to its outside radius, inside radius and shoulder transition, including the complete bore/cavity chain. Keep the profile on one side of the axis and encode the rotation axis explicitly. Revolve that section once, then add features absent from the section. Derive arc centers and endpoints from the established constraints; record concentric inner/outer arcs only when their shared center is established.

## S2: Orthogonal Silhouettes

Trace the principal-view material outline, then extrude along the depth established by the paired view or section. On the orthogonal plane, sketch an oversized outer loop enclosing the stock and a nested retained contour, then cut the surrounding region. This retains the intersection of the two silhouettes; section evidence still controls internal voids, local thicknesses and passages. Add the remaining flanges, ribs, holes and ports from their own datums.

Resolve the sketch normal and cut direction before choosing the end condition. A one-sided `through_all` cut travels only forward in the resolved cut direction; place the entire intended stock interval on that side. For a `mid_plane` cut, measure the stock interval `[a,b]` along the cutting axis and the sketch-plane coordinate `c`. Total depth must satisfy `D > 2 * max(abs(a-c), abs(b-c))`. Use 1 mm clearance at each end: `D = 2 * max(abs(a-c), abs(b-c)) + 2 mm` (add `0.002` when graph units are meters). Use the stock extent along this cutting axis, rather than its original extrusion depth.

## Tangent Arcs And Sketch Fillets

Use the sketch `fillet` primitive for a specified local blend between adjacent profile elements. For a dimensioned tangent arc that defines the main contour, solve its circle center and tangent endpoints, then encode an `arc`. Internal tangency requires center distance equal to the radius difference; external tangency requires the radius sum. Verify the tangent endpoints against both circles and the adjoining profile before submission.

## Editability And Robust Feature Trees

Generated SolidWorks parts must remain clean and easy for a human mechanical engineer to tweak parametrically in SolidWorks without triggering cascading rebuild errors (red/yellow feature-tree warnings):

1. **Datum-first anchoring over fragile derived geometry**:
   - Anchor primary base features, symmetric cuts, secondary bosses, and cross-bores to the three standard datum planes (`front`, `top`, `right`) or explicit datum offsets (`ref: {"datum": ..., "offset": ...}`) whenever possible.
   - Center symmetric parts on the origin and use `mid_plane` extrusions so editing overall width or length automatically preserves symmetry and hole centering.
   - Reference a body face (`node_face`) only when child design intent genuinely tracks that major planar face (such as mounting holes on a top flange). **Never** anchor sketches, planes, or holes to fillet faces, chamfer faces, narrow sliver faces, or incidental edges created by intersecting cuts.
2. **Strict feature-tree order — fillets, chamfers, and holes near the end**:
   - Order Feature Graph nodes from coarse structural stock to fine detail:
     1) Primary base solid (`box`, base `extrude` boss, or main `revolve`);
     2) Secondary structural bosses, flanges, ears, and ribs/webs;
     3) Major internal bores, cavities, pockets, and profile-trimming cuts;
     4) Hole features, counterbores/countersinks, and `cosmetic_thread` annotations;
     5) Linear/circular patterns and mirrors;
     6) **Dress-up `fillet` and `chamfer` nodes strictly LAST** (structural larger radii before small general-note rounds and edge-break chamfers).
   - Never place sketch extrusions, cuts, or holes *after* `fillet`/`chamfer` nodes on adjacent faces unless a downstream feature (such as uniform-wall `shell`) geometrically requires the outer round first. Keeping dress-up features at the bottom ensures an engineer can suppress or resize radii without breaking upstream sketches or edge selections.
3. **Single-intent sketches and semantic end conditions**:
   - Keep each sketch simple and focused on one engineering feature: put the outer contour (and major contour-defining tangent arcs) in the base sketch, and keep hole groups, keyways, slots, and local corner rounds as separate downstream `hole`, cut, or `fillet` features so dimensions are easy to find and edit in the SolidWorks FeatureManager tree.
   - Express true design intent in end conditions: use `through_all` (or `mid_plane` with explicit clearance) for through holes and through slots rather than a `blind` depth equal to the current wall thickness, so thickening a plate later does not turn a through hole into a blind hole.
4. **Guided lofts over multi-slice approximations**:
   - For curved or variable-section arms, handles, and tapered transitions with planar controlling arcs (e.g. an elliptical base transitioning along front-view arcs to a hub section), use **2–3 clean closed profiles + 2D `loft.guides`** (one open curve per sketch, with endpoints intersecting every profile contour) and optional `start_tangency`/`end_tangency`.
   - **Never** approximate a smooth curved surface by stacking 4+ parallel sliced profiles with hardcoded coordinates. Multi-slice lofts create spurious longitudinal surface seams, cannot be edited parametrically, and are capped at `built_unverified` by the orchestrator. If a surface genuinely requires 3D guide curves or boundary/surface tools not in the schema, declare a `CAPABILITY_GAP` honestly instead of faking a smooth loft.

## Verification Boundary

Treat schema validation and mock compilation as checks of representation compatibility. A live core or contour probe establishes only the included component and operations; record its covered and omitted features explicitly.

Claim a complete drawing-consistent part only after checking the full envelope, body count, relevant volume, every bore station and key hole/port, material/void boundaries, signed directions, and every supplied view and section. Resolve a mismatch through the approved repair workflow. Keep unresolved constraints and unavailable checks explicit in the final result; a successful partial probe supplies no full-part validation.
