"""Contour closure analysis for sketch profiles — the line/arc/spline CHAINS and the
CLOSED-by-construction primitives (slot, rounded_rect, rectangle, circle, ellipse).

Why: the frozen-coordinate IR closes a contour ONLY through identical shared
endpoint coordinates, and computed outlines (tangent points, filleted corners)
drift by microns — enough for the execution layer's cut to come back null and
roll back a half-built part (observed live on the 阀体 rebuild, 2026-08-17:
EXTRUSION_FAILED on a profile, sketch-node timeouts, then the model "succeeded"
by dropping features). The deterministic compiler-side defenses:

  - weld_profile(): snap chain endpoints within CONTOUR_WELD_TOL onto the FIRST
    matching earlier anchor before the lowering emits add_sketch_entity ops.
    First-anchor-wins keeps the fix deterministic; the tolerance is inside the
    drawing's own GB/T 1804-m general tolerance band, so a welded part stays in
    its artifact's tolerance class.
  - open_gaps(): report every chain endpoint that still has no partner, with the
    nearest neighbour and the gap. ir_schema.validate calls this at plan time for
    sketches consumed by extrude/revolve/sheet_metal/sweep-profile/loft profiles,
    so an unclosable contour fails BEFORE any execution call (no rollback, no
    wasted build) with a message the planner can act on.
  - collapsed_entities()/collapsed_profile(): the weld's safety net. Snapping an
    endpoint onto an anchor within tolerance can flatten a genuinely thin feature
    (or a stray spike) to a zero-length entity that SolidWorks cannot build; these
    report that so ir_schema rejects it at plan time instead of mid-build.
  - closed_loop()/loop_conflicts(): the composite defense. Everything above reads CHAIN
    endpoints, so the closed-by-construction kinds (slot, rounded_rect, rectangle, circle,
    ellipse) were invisible to it — a composite never needs welding, and two composites whose
    regions physically overlapped passed validation and only failed in SolidWorks as a null
    cut (the horizontal-slot fan, 2026-10-26). These sample each such primitive into a polygon
    and report loops that cross, coincide, run through each other's interiors, enclose no area,
    or (defensively, for a future kind) cross themselves, so the same sketch fails at plan
    time. Nested loops (a hole inside an outline) and disjoint loops are legal and untouched.

Non-finite endpoints (NaN / +/-Inf) are never welded and never count as paired —
NaN compares False to everything, so it would otherwise look like a closed contour.

Only CHAIN primitives participate in the weld/gap/extent checks. rectangle/circle/ellipse
and the two composites are closed by construction and are covered by loop_conflicts instead;
construction-flagged primitives are scaffolding whose coordinates are free-form and are skipped
by every check; rib/bend/sweep-path sketches are open by design and are never checked (the
caller decides which sketches require closure).
"""
import copy
import math
from bisect import bisect_right

# 0.1 mm — inside GB/T 1804-m's ±0.1 mm band (6-30 mm dims), so welding never
# moves geometry outside the drawing's own general tolerance class.
CONTOUR_WELD_TOL = 1e-4

# (key_x, key_y, which) per non-spline chain primitive.
_ENDPOINT_KEYS = {
    "line": (("x1", "y1", "start"), ("x2", "y2", "end")),
    "arc": (("x1", "y1", "start"), ("x2", "y2", "end")),
}


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_finite(v):
    # A non-finite endpoint (NaN / +/-Inf) can never be a real contour vertex: it must not
    # anchor a weld and must never count as "paired" (NaN compares False to everything).
    # Only a FLOAT can be non-finite — an int is always finite, and math.isfinite() raises
    # OverflowError on an int beyond float range (10**400, which json.loads produces), so an
    # int must never be handed to it.
    return _is_number(v) and (not isinstance(v, float) or math.isfinite(v))


def _to_float(v):
    """float(v), or None when the conversion OVERFLOWS — an int beyond float range
    (float(10**400) raises OverflowError; json.loads produces such ints). NaN/Inf convert
    fine and are returned as-is: the pre-existing _is_finite() guards decide what to do with
    them, so this only removes the crash and changes nothing else."""
    try:
        return float(v)
    except OverflowError:
        return None


def _as_float(v):
    """A finite float for arithmetic, or None when `v` is unusable (not a number, non-finite,
    or an int beyond float range). Callers treat None as "no usable point" — the same answer
    the pre-existing _is_finite() guard gave for NaN/Inf."""
    if not _is_finite(v):
        return None
    return _to_float(v)


def _iter_endpoints(prim):
    """[(which, x, y)] for a chain primitive's two contour endpoints; [] for
    closed kinds (rectangle/circle/ellipse), unknown kinds and malformed prims."""
    kind = prim.get("kind")
    if kind == "spline":
        pts = prim.get("points")
        if (isinstance(pts, list) and len(pts) >= 4 and len(pts) % 2 == 0
                and all(_is_number(v) for v in pts)):
            x1, y1 = _to_float(pts[0]), _to_float(pts[1])
            x2, y2 = _to_float(pts[-2]), _to_float(pts[-1])
            if None in (x1, y1, x2, y2):
                return []  # an int beyond float range is not a usable vertex
            return [("start", x1, y1), ("end", x2, y2)]
        return []
    out = []
    for kx, ky, which in _ENDPOINT_KEYS.get(kind, ()):
        x, y = prim.get(kx), prim.get(ky)
        if _is_number(x) and _is_number(y):
            fx, fy = _to_float(x), _to_float(y)
            if fx is not None and fy is not None:
                out.append((which, fx, fy))
    return out


def _set_endpoint(prim, which, x, y):
    kind = prim.get("kind")
    if kind == "spline":
        pts = list(prim["points"])
        idx = 0 if which == "start" else len(pts) - 2
        pts[idx], pts[idx + 1] = x, y
        prim["points"] = pts
        return
    for kx, ky, w in _ENDPOINT_KEYS.get(kind, ()):
        if w == which:
            prim[kx], prim[ky] = x, y
            return


def weld_profile(profile, tol=CONTOUR_WELD_TOL):
    """Snap chain endpoints onto earlier anchors within `tol`.

    Returns (welded_deep_copy, welds) — the IR node is never mutated. welds is a
    list of {prim, kind, endpoint, gap, to_prim} for every endpoint that MOVED
    (exact matches change nothing and are not reported).
    """
    welded = copy.deepcopy(profile)
    anchors = []  # (x, y, prim_index)
    welds = []
    for i, prim in enumerate(welded):
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        for which, x, y in _iter_endpoints(prim):
            if not (_is_finite(x) and _is_finite(y)):
                continue  # never weld onto / from a non-finite endpoint
            best = None
            for ax, ay, j in anchors:
                if j == i:
                    continue
                d = math.hypot(ax - x, ay - y)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, ax, ay, j)
            if best is None:
                anchors.append((x, y, i))
            elif best[0] > 0:
                d, ax, ay, j = best
                _set_endpoint(prim, which, ax, ay)
                welds.append({"prim": i, "kind": prim.get("kind"),
                              "endpoint": which, "gap": d, "to_prim": j})
    return welded, welds


def _spline_points(prim):
    """The flat through-point list as [(x, y)], or [] when malformed/non-finite."""
    pts = prim.get("points")
    if not (isinstance(pts, list) and len(pts) >= 4 and len(pts) % 2 == 0):
        return []
    if not all(_is_finite(v) for v in pts):
        return []
    out = []
    for i in range(0, len(pts) - 1, 2):
        x, y = _as_float(pts[i]), _as_float(pts[i + 1])
        if x is None or y is None:
            return []  # an int beyond float range: same answer as a non-finite point
        out.append((x, y))
    return out


def _prim_span(prim):
    """Max distance between any two of a chain primitive's own defining points.
    line: the chord between the two endpoints; arc: the SAMPLED sweep (an arc bulges beyond
    its chord, and a full-circle arc has start == end yet is not degenerate); spline: the
    through-points' extent. Returns None when the primitive carries no usable finite points."""
    kind = prim.get("kind")
    if kind == "spline":
        pts = _spline_points(prim)
        if not pts:
            return None
        return max(math.hypot(a[0] - b[0], a[1] - b[1])
                   for a in pts for b in pts)
    if kind == "arc":
        pts = _arc_samples(prim)
        cx_f, cy_f = _as_float(prim.get("cx")), _as_float(prim.get("cy"))
        if pts and cx_f is not None and cy_f is not None:
            # include the CENTRE: a full-circle arc (start == end, radius > 0) samples to a
            # single point, but its centre still defines a real, non-degenerate entity.
            pts = pts + [(cx_f, cy_f)]
        if not pts:
            return None
        return max(math.hypot(a[0] - b[0], a[1] - b[1])
                   for a in pts for b in pts)
    ends = _iter_endpoints(prim)
    if len(ends) != 2:
        return None
    (_w1, x1, y1), (_w2, x2, y2) = ends
    if not (_is_finite(x1) and _is_finite(y1) and _is_finite(x2) and _is_finite(y2)):
        return None
    return math.hypot(x2 - x1, y2 - y1)


def collapsed_entities(profile, tol=CONTOUR_WELD_TOL):
    """Chain primitives welding left as a POINT (all defining points within `tol`).

    The weld snaps an endpoint onto any earlier anchor within CONTOUR_WELD_TOL, so a
    genuinely thin feature (or a stray spike) can be welded flat: both endpoints land on
    the same coordinate and the emitted entity is a zero-length segment that SolidWorks
    cannot build. Callers reject such a profile at plan time instead of discovering it
    mid-build. Returns a list of {prim, kind, span} (empty == nothing collapsed).
    """
    out = []
    for i, prim in enumerate(profile):
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        if prim.get("kind") not in ("line", "arc", "spline"):
            continue
        span = _prim_span(prim)
        if span is not None and span <= tol:
            out.append({"prim": i, "kind": prim.get("kind"), "span": span})
    return out


def _arc_samples(prim, n=32):
    """Points along an arc_center primitive, sampled with the execution layer's CreateArc
    semantics (+1 sweeps CCW start->end, -1 CW). [] when malformed / non-finite / bad dir."""
    cx, cy = prim.get("cx"), prim.get("cy")
    x1, y1 = prim.get("x1"), prim.get("y1")
    x2, y2 = prim.get("x2"), prim.get("y2")
    d = prim.get("dir")
    if not all(_is_finite(v) for v in (cx, cy, x1, y1, x2, y2)) or d not in (1, -1):
        return []
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    r = math.hypot(x1 - cx, y1 - cy)
    if d > 0:
        while a2 < a1:
            a2 += 2.0 * math.pi
    else:
        while a2 > a1:
            a2 -= 2.0 * math.pi
    return [(cx + r * math.cos(a1 + (a2 - a1) * i / n),
             cy + r * math.sin(a1 + (a2 - a1) * i / n)) for i in range(n + 1)]


def _prim_points(prim):
    """Representative points of a chain primitive for extent purposes. An arc is SAMPLED
    (it bulges beyond its endpoints, so the endpoint bbox alone would under-report a real
    enclosed region); a spline contributes its through-points; a line its two endpoints."""
    kind = prim.get("kind")
    if kind == "arc":
        pts = _arc_samples(prim)
        cx_f, cy_f = _as_float(prim.get("cx")), _as_float(prim.get("cy"))
        if cx_f is not None and cy_f is not None:
            # include the CENTRE: a full-circle arc (start == end) samples to one point.
            pts = pts + [(cx_f, cy_f)]
        return pts
    if kind == "spline":
        return _spline_points(prim)
    return [(x, y) for _w, x, y in _iter_endpoints(prim)
            if _is_finite(x) and _is_finite(y)]


def profile_extent(profile):
    """(min_x, min_y, max_x, max_y) over every finite chain point (arcs SAMPLED so their
    outward bulge counts), or None."""
    xs, ys = [], []
    for prim in profile:
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        for x, y in _prim_points(prim):
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def collapsed_profile(profile, tol=CONTOUR_WELD_TOL):
    """True when a CLOSED chain's outline has collapsed to a line or a point: its
    bounding box is degenerate in X or Y (near-zero enclosed area). Ordering-free, so it
    needs no chain traversal — a closed profile with any real enclosed region has both
    spans well above the weld tolerance."""
    ext = profile_extent(profile)
    if ext is None:
        return False
    return (ext[2] - ext[0]) <= tol or (ext[3] - ext[1]) <= tol


def open_gaps(profile, tol=CONTOUR_WELD_TOL):
    """Chain endpoints with no partner after welding.

    Returns a list of {prim, kind, endpoint, x, y, nearest_prim, nearest_kind,
    nearest_which, nearest_gap} — empty when every chain endpoint pairs within
    `tol` (or when the profile has no chain primitives at all).
    """
    welded, _ = weld_profile(profile, tol)
    pts = []
    for i, prim in enumerate(welded):
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        for which, x, y in _iter_endpoints(prim):
            pts.append((i, prim.get("kind"), which, x, y))
    gaps = []
    for i, kind, which, x, y in pts:
        nearest = None
        for j, k2, w2, x2, y2 in pts:
            if j == i:
                continue
            if not (_is_finite(x) and _is_finite(y) and _is_finite(x2) and _is_finite(y2)):
                continue  # a non-finite endpoint is never "paired"
            d = math.hypot(x2 - x, y2 - y)
            if nearest is None or d < nearest[0]:
                nearest = (d, j, k2, w2, x2, y2)
        if nearest is None or nearest[0] > tol:
            gaps.append({
                "prim": i, "kind": kind, "endpoint": which, "x": x, "y": y,
                "nearest_prim": nearest[1] if nearest else None,
                "nearest_kind": nearest[2] if nearest else None,
                "nearest_which": nearest[3] if nearest else None,
                "nearest_gap": nearest[0] if nearest else None,
            })
    return gaps


# ------------------------------------------------------- closed loops (the composite kinds)

# Sampling density for a closed curve: 72 steps per FULL turn (5 deg), so a slot cap (half a
# turn) gets 36 and a rounded_rect corner (a quarter) 18. The sample is INSCRIBED — its vertices
# lie on the true curve, so the polygon sits marginally INSIDE it (worst sag ~5e-5 m even at
# r = 50 mm, under CONTOUR_WELD_TOL). The bias is deliberate: an approximation that read a
# genuine TANGENCY as an overlap would reject valid drawings, while one that undershoots an
# overlap only leaves the pre-existing behaviour (fail in SolidWorks) in place.
LOOP_SAMPLES_PER_TURN = 72

# "The same line", for _collinear_overlap only. Nanometres: see the function's docstring.
COLLINEAR_COINCIDENT_EPS = 1e-9

# Primitives that enclose a region BY CONSTRUCTION — nothing to weld, so the chain checks above
# cannot see them at all. line/arc/spline are chain kinds and stay with weld_profile/open_gaps:
# their closure is a property of the CHAIN, not of one primitive.
_CLOSED_LOOP_KINDS = frozenset(("rectangle", "circle", "ellipse", "slot", "rounded_rect"))


def _centre(prim):
    """(cx, cy) as finite floats, defaulting to the origin; None when a centre coordinate is
    present but unusable (ir_schema reports that separately — here it only means 'no polygon')."""
    out = []
    for key in ("cx", "cy"):
        raw = prim.get(key)
        if raw is None:
            out.append(0.0)
            continue
        val = _as_float(raw)
        if val is None:
            return None
        out.append(val)
    return (out[0], out[1])


def _pos(value):
    """A finite number > 0 as a float, else None."""
    num = _as_float(value)
    return num if num is not None and num > 0 else None


def _sweep(cx, cy, radius, a0, a1, steps):
    """steps+1 points on the circle (cx, cy, radius) sweeping CCW from a0 to a1 (a1 > a0)."""
    return [(cx + radius * math.cos(a0 + (a1 - a0) * i / steps),
             cy + radius * math.sin(a0 + (a1 - a0) * i / steps)) for i in range(steps + 1)]


def _dedupe(points):
    """Drop consecutive repeats (the join where an edge meets an arc) and a closing repeat of the
    first vertex — the rings below are implicitly closed, and a zero-length edge would otherwise
    reach every segment test."""
    out = []
    for p in points:
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 2 and out[0] == out[-1]:
        out.pop()
    return out


def closed_loop(prim):
    """The polygon a CLOSED-by-construction primitive encloses, as [(x, y), ...] in traversal
    order and implicitly closed, or None for a chain kind / malformed primitive.

    The outline is rebuilt from the primitive's OWN parameters rather than from the entities
    `lowering` emits, because lowering imports this module — the dependency only runs one way.
    It is the same geometry: slot/rounded_rect are the rigid-rotated obround/stadium described by
    the same length/width/radius/centre/angle, and rectangle/circle/ellipse are the entity the
    C# creates directly.
    """
    if not isinstance(prim, dict):
        return None
    kind = prim.get("kind")
    if kind not in _CLOSED_LOOP_KINDS:
        return None
    centre = _centre(prim)
    if centre is None:
        return None
    cx, cy = centre
    if kind == "rectangle":
        w, h = _pos(prim.get("width")), _pos(prim.get("height"))
        if w is None or h is None:
            return None
        # Offsetting the four corners by (cx, cy) is what lowering._rectangle_op emits (its x1/y1/
        # x2/y2 ARE cx±w/2, cy±h/2), so the sampled ring and the drawn contour agree. With the
        # default centre the arithmetic is 0.0 - w/2 and 0.0 + w/2 — bit-for-bit the old vertices.
        return _dedupe([(cx - w / 2.0, cy - h / 2.0), (cx + w / 2.0, cy - h / 2.0),
                        (cx + w / 2.0, cy + h / 2.0), (cx - w / 2.0, cy + h / 2.0)])
    if kind == "circle":
        diameter = _pos(prim.get("diameter"))
        if diameter is None:
            return None
        return _dedupe(_sweep(cx, cy, diameter / 2.0, 0.0, 2.0 * math.pi,
                              LOOP_SAMPLES_PER_TURN))
    if kind == "ellipse":
        # Centre + a point on the major axis + a point on the minor axis, exactly what
        # add_sketch_entity(ellipse) consumes: p(t) = c + u*cos t + v*sin t. A zero-length axis
        # collapses the ring to a segment, which loop_conflicts then reports as degenerate.
        vals = [_as_float(prim.get(k)) for k in ("x1", "y1", "x2", "y2")]
        if any(v is None for v in vals):
            return None
        ux, uy = vals[0] - cx, vals[1] - cy
        vx, vy = vals[2] - cx, vals[3] - cy
        step = 2.0 * math.pi / LOOP_SAMPLES_PER_TURN
        return _dedupe([(cx + ux * math.cos(i * step) + vx * math.sin(i * step),
                         cy + uy * math.cos(i * step) + vy * math.sin(i * step))
                        for i in range(LOOP_SAMPLES_PER_TURN)])
    if kind == "slot":
        length, width = _pos(prim.get("length")), _pos(prim.get("width"))
        if length is None or width is None or length <= width:
            return None
        angle = 0.0 if prim.get("angle") is None else _as_float(prim.get("angle"))
        if angle is None:
            return None
        r = width / 2.0
        half = (length - width) / 2.0
        caps = LOOP_SAMPLES_PER_TURN // 2  # each cap is a half turn
        local = ([(-half, -r), (half, -r)]
                 + _sweep(half, 0.0, r, -math.pi / 2.0, math.pi / 2.0, caps)
                 + [(half, r), (-half, r)]
                 + _sweep(-half, 0.0, r, math.pi / 2.0, 3.0 * math.pi / 2.0, caps))
        cos_t, sin_t = math.cos(angle), math.sin(angle)
        return _dedupe([(cx + x * cos_t - y * sin_t, cy + x * sin_t + y * cos_t)
                        for x, y in local])
    # rounded_rect — CCW from the bottom edge, the same order lowering emits its 8 entities in.
    w, h, radius = _pos(prim.get("width")), _pos(prim.get("height")), _pos(prim.get("radius"))
    if (w is None or h is None or radius is None
            or radius * 2.0 >= min(w, h)):  # ir_schema rejects it; no polygon to sample
        return None
    a, b = w / 2.0, h / 2.0
    xl, xr = -a + radius, a - radius
    yb, yt = -b + radius, b - radius
    corner = LOOP_SAMPLES_PER_TURN // 4  # each corner arc is a quarter turn
    local = ([(-a + radius, -b), (a - radius, -b)]
             + _sweep(xr, yb, radius, -math.pi / 2.0, 0.0, corner)
             + [(a, yb), (a, yt)]
             + _sweep(xr, yt, radius, 0.0, math.pi / 2.0, corner)
             + [(a - radius, b), (-a + radius, b)]
             + _sweep(xl, yt, radius, math.pi / 2.0, math.pi, corner)
             + [(-a, yt), (-a, yb)]
             + _sweep(xl, yb, radius, math.pi, 3.0 * math.pi / 2.0, corner))
    return _dedupe([(x + cx, y + cy) for x, y in local])


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _ring_index(points):
    """One closed polygon's segments, computed once and sorted two ways.

    A row is (x1, y1, x2, y2, dx, dy, length, min-x, min-y, max-x, max-y, position). `position` is
    the segment's index in `points`, which the self-crossing scan needs to tell an adjacency (a
    shared vertex) from a real crossing.

    The index is (rows by min-x, their min-x column, the longest chord, rows by min-y, their min-y
    column, the longest y-band). Rings are sampled at LOOP_SAMPLES_PER_TURN points, so an unindexed
    scan is a few thousand row tests per pair — and a sketch of N closed loops is N^2/2 pairs, all
    of it at PLAN time, where a flange outline and its bolt circle have to stay a rounding error.
    Both sorts feed `_window`, which is what makes the two scans read a handful of rows instead.
    """
    n = len(points)
    rows = []
    for k in range(n):
        x1, y1 = points[k]
        x2, y2 = points[(k + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        rows.append((x1, y1, x2, y2, dx, dy, ln,
                     x1 if x1 <= x2 else x2, y1 if y1 <= y2 else y2,
                     x2 if x1 <= x2 else x1, y2 if y1 <= y2 else y1, k))
    if not rows:
        return (), (), 0.0, (), (), 0.0
    by_x = sorted(rows, key=lambda r: r[7])
    by_y = sorted(rows, key=lambda r: r[8])
    return (by_x, [r[7] for r in by_x], max(r[9] - r[7] for r in rows),
            by_y, [r[8] for r in by_y], max(r[10] - r[8] for r in rows))


def _window(mins, longest, reach_min, reach_max):
    """The half-open index range of a min-sorted edge list whose band can reach the given band.

    Exact, not a heuristic. Anything counted here is a SHARED coordinate, so it lies inside both
    bands: an edge starting above reach_max misses (the sort makes that a prefix bound, by bisect),
    and an edge ending below reach_min misses too — its min is at least its own max minus `longest`,
    which is where the walk back stops.
    """
    hi = bisect_right(mins, reach_max)
    lo = hi
    floor = reach_min - longest
    while lo > 0 and mins[lo - 1] >= floor:
        lo -= 1
    return lo, hi


def _boxes_apart(e1, e2):
    return (e2[9] < e1[7] or e1[9] < e2[7] or e2[10] < e1[8] or e1[10] < e2[8])


def _cross_at(e1, e2, tol=CONTOUR_WELD_TOL):
    """The crossing point of two edge rows (see `_ring_index`), or None unless it is PROPER.

    Proper means the intersection sits more than `tol` metres inside BOTH segments. That exempts
    a shared endpoint, a vertex that grazes the other loop within the drawing's own 0.1 mm band,
    and a tangency — all of which SolidWorks builds fine (a notch legitimately joins its outline
    at a point), so calling them conflicts would reject correct drawings."""
    if _boxes_apart(e1, e2):
        return None
    x1, y1, _, _, dx12, dy12, len12 = e1[:7]
    x3, y3, _, _, dx34, dy34, len34 = e2[:7]
    if len12 <= 0.0 or len34 <= 0.0:
        return None
    den = dx12 * dy34 - dy12 * dx34
    if den == 0.0:
        return None  # parallel: _collinear_overlap owns that case
    t = ((x3 - x1) * dy34 - (y3 - y1) * dx34) / den
    u = ((x3 - x1) * dy12 - (y3 - y1) * dx12) / den
    if min(t * len12, (1.0 - t) * len12, u * len34, (1.0 - u) * len34) <= tol:
        return None
    return (x1 + t * dx12, y1 + t * dy12)


def _collinear_overlap(e1, e2, tol=CONTOUR_WELD_TOL):
    """True when both edge rows lie on ONE line and share more than `tol` of it. Two identical
    loops (the same slot written twice) never cross, yet the profile is still uncuttable.

    The band that decides "the same line" is COLLINEAR_COINCIDENT_EPS — a NANOmetre, not the
    0.1 mm weld band. A curved pair of chords meeting at a tangent point deviates from one another
    by far less than the weld tolerance over its own short length, so a loose band would read
    legal TANGENCY as an illegal coincidence; exact duplicates are what this branch owns, and
    their coordinates agree to within the 1e-9 rounding `lowering` emits them at."""
    if _boxes_apart(e1, e2):
        return False
    x1, y1, x2, y2, ax, ay, la = e1[:7]
    x3, y3, x4, y4, bx, by, lb = e2[:7]
    if la <= 0.0 or lb <= 0.0:
        return False
    # perpendicular distance of each endpoint from the OTHER segment's supporting line, in metres:
    # |cross(d, p)| / |d| <= eps, written without the division to keep it exact.
    if (abs((x3 - x1) * ay - (y3 - y1) * ax) > COLLINEAR_COINCIDENT_EPS * la
            or abs((x4 - x1) * ay - (y4 - y1) * ax) > COLLINEAR_COINCIDENT_EPS * la
            or abs((x1 - x3) * by - (y1 - y3) * bx) > COLLINEAR_COINCIDENT_EPS * lb
            or abs((x2 - x3) * by - (y2 - y3) * bx) > COLLINEAR_COINCIDENT_EPS * lb):
        return False
    ux, uy = ax / la, ay / la
    s3 = (x3 - x1) * ux + (y3 - y1) * uy
    s4 = (x4 - x1) * ux + (y4 - y1) * uy
    return min(la, max(s3, s4)) - max(0.0, min(s3, s4)) > tol


def _vertex_in_ring(pt, index):
    """Ray-cast parity test, run over the ring's y-index. A point exactly ON the ring reads
    undefined, which is fine: callers only use it to tell NESTING (one loop wholly inside the
    other, legal) from mutual containment (impossible for two simple loops that do not cross), and
    both are non-crossing cases.

    Only the edges whose y-band straddles the point can flip the parity, and the y-sorted index
    hands over exactly that band — three rows out of 74 for a 72-chord circle — so the whole-ring
    scan became a handful of compares with the same answer."""
    x, y = pt
    by_y, minys, longest_y = index[3], index[4], index[5]
    inside = False
    lo, hi = _window(minys, longest_y, y, y)
    for pos in range(lo, hi):
        row = by_y[pos]
        if (row[1] > y) != (row[3] > y) and x < row[0] + row[4] * (y - row[1]) / row[5]:
            inside = not inside
    return inside


def _overlap_centre(box_a, box_b):
    """Centre of two bounding boxes' shared rectangle. Used as `point` for the findings whose
    geometry has no single crossing to name (mutual / collinear) — an APPROXIMATE address the
    planner can look up, never presented as a point on either outline."""
    return ((max(box_a[0], box_b[0]) + min(box_a[2], box_b[2])) / 2.0,
            (max(box_a[1], box_b[1]) + min(box_a[3], box_b[3])) / 2.0)


def loop_conflicts(profile, tol=CONTOUR_WELD_TOL):
    """Closed loops in ONE profile that cannot both be built.

    Each finding is {a, b, a_kind, b_kind, how, point, more} with `how` one of:
      "degenerate"  the loop encloses no area (b is None)
      "self-cross"  the loop crosses itself (b is None)
      "cross"       two loops cross properly; `point` is one crossing, `more` counts the rest
      "collinear"   two loops run along each other (identical or coincident outlines)
      "mutual"      each loop has a vertex inside the other, with no crossing between them
    For "collinear"/"mutual" `point` is the CENTRE OF THE SHARED BOUNDING BOX (approximate).

    NOT reported, because both are valid multi-contour sketches: DISJOINT loops (a plate with two
    holes) and NESTED loops (an annulus — a hole inside an outline). Construction-flagged
    primitives are scaffolding and are skipped, as everywhere else in this module.

    "self-cross" cannot be reached from today's five kinds — each samples to a simple polygon, and
    test_closed_loop_primitive_alone_is_never_a_conflict pins that. It stays because closed_loop()
    is the sampling seam for any future kind, and a bowtie outline is the one shape that would
    reach here instead of to "degenerate".

    The scan is indexed per ring (`_ring_index` + `_window`), not a fresh edge^2 sweep per pair:
    a sketch of N closed loops is N^2/2 pairs of ~74-row polygons, and validation runs at plan
    time, where a flange outline and its 24 holes have to stay a rounding error, not a second.
    Every bound is exact — a skipped row cannot share a coordinate with the query — so the
    findings are the same ones the sweep produced, only sooner.
    """
    out = []
    rings = []
    for i, prim in enumerate(profile or ()):
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        pts = closed_loop(prim)
        if pts is None or len(pts) < 3:
            continue
        box = _bbox(pts)
        if (box[2] - box[0]) <= tol or (box[3] - box[1]) <= tol:
            out.append({"a": i, "b": None, "a_kind": prim.get("kind"), "b_kind": None,
                        "how": "degenerate", "point": None, "more": 0})
            continue
        rings.append((i, prim.get("kind"), pts, box,
                      (box[2] - box[0]) * (box[3] - box[1]), _ring_index(pts)))

    for idx, kind_i, pts, box, area, index in rings:
        rows, minxs, longest = index[0], index[1], index[2]
        n = len(rows)
        hit = None
        for pos in range(n):
            ea = rows[pos]
            lo, hi = _window(minxs, longest, ea[7], ea[9])
            for other in range(lo, hi):
                eb = rows[other]
                apart = abs(ea[11] - eb[11])
                if apart <= 1 or apart == n - 1:
                    continue  # adjacent in the ring: the shared vertex is not a crossing
                hit = _cross_at(ea, eb, tol)
                if hit is not None:
                    break
            if hit is not None:
                break
        if hit is not None:
            out.append({"a": idx, "b": None, "a_kind": kind_i, "b_kind": None,
                        "how": "self-cross", "point": hit, "more": 0})

    for x in range(len(rings)):
        i, kind_i, pa, box_a, area_a, index_a = rings[x]
        for y in range(x + 1, len(rings)):
            j, kind_j, pb, box_b, area_b, index_b = rings[y]
            if (box_a[0] > box_b[2] or box_b[0] > box_a[2]
                    or box_a[1] > box_b[3] or box_b[1] > box_a[3]):
                continue  # bounding boxes apart: no shared region is possible
            # Query with the SMALLER ring against the LARGER one's table: a row's window is its own
            # band plus the indexed ring's longest band, so a small loop inside a big outline reads
            # a handful of that outline's rows instead of all of them.
            q, big = (index_a, index_b) if area_a <= area_b else (index_b, index_a)
            crossings = []
            coincident = False
            for s1 in q[0]:
                lo, hi = _window(big[1], big[2], s1[7], s1[9])
                for pos in range(lo, hi):
                    s2 = big[0][pos]
                    point = _cross_at(s1, s2, tol)
                    if point is not None:
                        crossings.append(point)
                        if len(crossings) >= 32:
                            break
                    elif not coincident and _collinear_overlap(s1, s2, tol):
                        coincident = True
                if len(crossings) >= 32:
                    break
            found = {"a": i, "b": j, "a_kind": kind_i, "b_kind": kind_j,
                     "point": None, "more": 0}
            if crossings:
                found["how"] = "cross"
                found["point"] = crossings[0]
                found["more"] = max(0, len(crossings) - 1)
            elif coincident:
                found["how"] = "collinear"
                found["point"] = _overlap_centre(box_a, box_b)
            elif (any(_vertex_in_ring(p, index_b) for p in pa)
                    and any(_vertex_in_ring(p, index_a) for p in pb)):
                # EVERY vertex, not just one: the fan's slots do dip into each other, but their
                # crossings land inside the weld band, so "no proper crossing" does NOT mean the
                # loops are simple about each other and a single vertex can sit outside a region
                # its neighbours reach into. This is the class that catches the real overlap.
                found["how"] = "mutual"
                found["point"] = _overlap_centre(box_a, box_b)
            else:
                continue  # disjoint or properly nested: both build
            out.append(found)
    return out
