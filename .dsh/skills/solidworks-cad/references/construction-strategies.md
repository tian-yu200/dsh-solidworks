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

## Verification Boundary

Treat schema validation and mock compilation as checks of representation compatibility. A live core or contour probe establishes only the included component and operations; record its covered and omitted features explicitly.

Claim a complete drawing-consistent part only after checking the full envelope, body count, relevant volume, every bore station and key hole/port, material/void boundaries, signed directions, and every supplied view and section. Resolve a mismatch through the approved repair workflow. Keep unresolved constraints and unavailable checks explicit in the final result; a successful partial probe supplies no full-part validation.
