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


def arc_radii(prim):
    """(start radius, end radius) of an arc primitive, or None when a coordinate is unusable."""
    vals = [_as_float(prim.get(k)) for k in ("cx", "cy", "x1", "y1", "x2", "y2")]
    if None in vals:
        return None
    cx, cy, x1, y1, x2, y2 = vals
    return math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy)


def true_arcs(profile, tol=CONTOUR_WELD_TOL):
    """Put every arc's centre where BOTH endpoints are on its circle.

    SolidWorks' CreateArc takes the radius from the START point and only the direction from the
    end, so an end point off that circle is drawn somewhere else and the contour opens there
    (脚踏座, 2026-11-01: an R88 arc whose end sat 0.047 mm inside its start radius failed three
    extrudes in a row; the same sketch with the centre corrected built). Hand-computed tangent
    chains drift like this, and the endpoint weld above can add up to `tol` more.

    An arc whose two radii differ by at most `tol` keeps both endpoints (they are the shared
    contour vertices) and moves its centre onto the chord's perpendicular bisector at the mean
    radius, on the side of the original centre, so the sweep sense is unchanged. The move is
    itself capped at `tol`: on a shallow arc a tiny radius mismatch means a large centre move
    (R100 over a 10 mm chord: 0.09 mm off → 0.9 mm), which would silently break concentricity
    with a hole drawn at the original centre. Larger mismatches and larger moves are left alone
    for validation to reject (arc_radius_problems).
    Returns (deep copy, [{prim, shift}]) — the IR node is never mutated.
    """
    out = copy.deepcopy(profile)
    moves = []
    for i, prim in enumerate(out):
        if not isinstance(prim, dict) or prim.get("kind") != "arc":
            continue
        radii = arc_radii(prim)
        if radii is None or abs(radii[0] - radii[1]) <= 1e-12 or abs(radii[0] - radii[1]) > tol:
            continue
        trued = _trued_centre(prim, radii)
        if trued is None or trued[1] > tol:
            continue
        prim["cx"], prim["cy"] = trued[0]
        moves.append({"prim": i, "shift": trued[1]})
    return out, moves


def _trued_centre(prim, radii):
    """((cx, cy), shift) for true_arcs, or None for a full circle (start and end are one point,
    already on its circle)."""
    cx, cy = float(prim["cx"]), float(prim["cy"])
    x1, y1, x2, y2 = (float(prim[k]) for k in ("x1", "y1", "x2", "y2"))
    half = math.hypot(x2 - x1, y2 - y1) / 2.0
    if half <= 1e-12:
        return None
    radius = max(sum(radii) / 2.0, half)
    rise = math.sqrt(radius * radius - half * half)
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    nx, ny = -(y2 - y1) / (2.0 * half), (x2 - x1) / (2.0 * half)
    centre = min(((mx + nx * rise, my + ny * rise), (mx - nx * rise, my - ny * rise)),
                 key=lambda c: math.hypot(c[0] - cx, c[1] - cy))
    return centre, math.hypot(centre[0] - cx, centre[1] - cy)


def arc_radius_problems(profile, tol=CONTOUR_WELD_TOL):
    """Arcs true_arcs cannot repair, judged after the endpoint weld: a start or end point on the
    centre (no radius), radii further apart than `tol`, or a truing move larger than `tol`.
    [{prim, r1, r2, how[, shift]}]."""
    welded, _ = weld_profile(profile, tol)
    out = []
    for i, prim in enumerate(welded):
        if not isinstance(prim, dict) or prim.get("kind") != "arc" or prim.get("construction") is True:
            continue
        radii = arc_radii(prim)
        if radii is None:
            continue
        if min(radii) <= tol:
            out.append({"prim": i, "r1": radii[0], "r2": radii[1], "how": "no-radius"})
        elif abs(radii[0] - radii[1]) > tol:
            out.append({"prim": i, "r1": radii[0], "r2": radii[1], "how": "off-circle"})
        elif abs(radii[0] - radii[1]) > 1e-12:
            trued = _trued_centre(prim, radii)
            if trued is not None and trued[1] > tol:
                out.append({"prim": i, "r1": radii[0], "r2": radii[1], "how": "centre-shift",
                            "shift": trued[1]})
    return out


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


# ------------------------------------------------------- chains that cross or retrace themselves

def _curves(prim):
    """A primitive as exact curves: ("seg", (x1, y1), (x2, y2)) and ("arc", (cx, cy), r, start
    angle, signed sweep). Lines, arcs, circles and rectangles only; anything else -> [] (not
    checked, so never falsely reported)."""
    kind = prim.get("kind")
    if kind == "line":
        ends = [(x, y) for _w, x, y in _iter_endpoints(prim)]
        if len(ends) == 2 and all(_is_finite(v) for p in ends for v in p) and ends[0] != ends[1]:
            return [("seg", ends[0], ends[1])]
        return []
    if kind == "arc":
        vals = [_as_float(prim.get(k)) for k in ("cx", "cy", "x1", "y1", "x2", "y2")]
        d = prim.get("dir")
        if any(v is None for v in vals) or d not in (1, -1):
            return []
        cx, cy, x1, y1, x2, y2 = vals
        r = math.hypot(x1 - cx, y1 - cy)
        if r <= 0.0:
            return []
        a1, a2 = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
        sweep = (a2 - a1) % (2.0 * math.pi) if d > 0 else -((a1 - a2) % (2.0 * math.pi))
        if sweep == 0.0:
            sweep = 2.0 * math.pi * d  # start == end: the execution layer draws a full circle
        return [("arc", (cx, cy), r, a1, sweep)]
    if kind == "circle":
        centre, diameter = _centre(prim), _pos(prim.get("diameter"))
        if centre is None or diameter is None:
            return []
        return [("arc", centre, diameter / 2.0, 0.0, 2.0 * math.pi)]
    if kind == "rectangle":
        ring = closed_loop(prim)
        if not ring or len(ring) != 4:
            return []
        return [("seg", ring[k], ring[(k + 1) % 4]) for k in range(4)]
    return []


def _on_arc(curve, pt, tol):
    """Whether a point already ON the arc's circle lies inside its sweep, more than `tol` of arc
    length from either end (a full circle has no ends)."""
    _k, (cx, cy), r, a0, sweep = curve
    if abs(sweep) >= 2.0 * math.pi:
        return True
    theta = math.atan2(pt[1] - cy, pt[0] - cx)
    phi = (theta - a0) % (2.0 * math.pi) if sweep > 0 else (a0 - theta) % (2.0 * math.pi)
    return phi * r > tol and (abs(sweep) - phi) * r > tol


def _seg_inner(seg, pt, tol):
    """Whether a point already ON the segment's line lies inside it, more than `tol` from both ends."""
    _k, (x1, y1), (x2, y2) = seg
    ln = math.hypot(x2 - x1, y2 - y1)
    t = ((pt[0] - x1) * (x2 - x1) + (pt[1] - y1) * (y2 - y1)) / (ln * ln)
    return t * ln > tol and (1.0 - t) * ln > tol


def _row(p, q, pos):
    """One segment as an edge row in `_ring_index`'s layout, so `_cross_at` and
    `_collinear_overlap` read it unchanged."""
    (x1, y1), (x2, y2) = p, q
    dx, dy = x2 - x1, y2 - y1
    return (x1, y1, x2, y2, dx, dy, math.hypot(dx, dy),
            min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), pos)


def _crossing(c1, c2, tol):
    """("cross" | "collinear", point) when two exact curves cross properly or retrace each other,
    else None. A tangency, or a contact within `tol` of an end, is not a crossing."""
    if c1[0] == "arc" and c2[0] == "seg":
        c1, c2 = c2, c1
    if c1[0] == "seg" and c2[0] == "seg":
        e1, e2 = _row(c1[1], c1[2], 0), _row(c2[1], c2[2], 0)
        point = _cross_at(e1, e2, tol)
        if point is not None:
            return "cross", point
        if _collinear_overlap(e1, e2, tol):
            box = (max(e1[7], e2[7]), max(e1[8], e2[8]), min(e1[9], e2[9]), min(e1[10], e2[10]))
            return "collinear", ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        return None
    if c1[0] == "seg":
        (x1, y1), (x2, y2) = c1[1], c1[2]
        (cx, cy), r = c2[1], c2[2]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        dist = abs((cx - x1) * dy - (cy - y1) * dx) / ln
        if dist >= r - tol:
            return None  # misses, or tangent
        foot = ((cx - x1) * dx + (cy - y1) * dy) / (ln * ln)
        half = math.sqrt(r * r - dist * dist) / ln
        for t in (foot - half, foot + half):
            pt = (x1 + t * dx, y1 + t * dy)
            if _seg_inner(c1, pt, tol) and _on_arc(c2, pt, tol):
                return "cross", pt
        return None
    (ax, ay), ra = c1[1], c1[2]
    (bx, by), rb = c2[1], c2[2]
    dcc = math.hypot(bx - ax, by - ay)
    if dcc <= tol and abs(ra - rb) <= tol:
        # One circle: the arcs retrace each other when their sweeps share more than `tol`.
        steps = 64
        for k in range(1, steps):
            theta = c1[3] + c1[4] * k / steps
            pt = (ax + ra * math.cos(theta), ay + ra * math.sin(theta))
            if _on_arc(c2, pt, tol):
                return "collinear", pt
        return None
    if not (abs(ra - rb) + tol < dcc < ra + rb - tol):
        return None  # apart, nested, or tangent
    along = (ra * ra - rb * rb + dcc * dcc) / (2.0 * dcc)
    h = math.sqrt(max(ra * ra - along * along, 0.0))
    mx, my = ax + along * (bx - ax) / dcc, ay + along * (by - ay) / dcc
    for sign in (1.0, -1.0):
        pt = (mx - sign * h * (by - ay) / dcc, my + sign * h * (bx - ax) / dcc)
        if _on_arc(c1, pt, tol) and _on_arc(c2, pt, tol):
            return "cross", pt
    return None


def chain_conflicts(profile, tol=CONTOUR_WELD_TOL):
    """Line/arc chain primitives that cross, or retrace, another primitive of the same sketch.

    Every chain endpoint can be paired (open_gaps finds nothing) and the outline still be no
    region SolidWorks can extrude: the 缺口块 bench run (2026-11-08) closed its upright outline with
    a bottom line that retraced the first line for 10 mm, and SolidWorks returned a null boss twice
    while the repair only reversed an arc. A simple loop meets each neighbour at one shared endpoint
    and nothing else, so a PROPER crossing or a retraced stretch between two primitives is never
    legal. Chain primitives are also tested against circles and rectangles (a hole that cuts through
    the outline). The geometry is exact (line/circle intersection, not sampled chords), so a
    tangency or a touch at a point stays legal, as in loop_conflicts. Splines, slots, rounded
    rectangles and ellipses are not checked here; closed-vs-closed pairs belong to loop_conflicts.

    Returns [{a, b, a_kind, b_kind, how ("cross" | "collinear"), point}], the first conflict per
    pair, at most 8.
    """
    welded, _ = weld_profile(profile or [], tol)
    prims = []
    for i, prim in enumerate(welded):
        if isinstance(prim, dict) and prim.get("construction") is not True:
            curves = _curves(prim)
            if curves:
                prims.append((i, prim.get("kind"), curves))
    out = []
    for x in range(len(prims)):
        i, kind_i, curves_i = prims[x]
        for y in range(x + 1, len(prims)):
            j, kind_j, curves_j = prims[y]
            if kind_i in _CLOSED_LOOP_KINDS and kind_j in _CLOSED_LOOP_KINDS:
                continue
            hit = None
            for c1 in curves_i:
                for c2 in curves_j:
                    hit = _crossing(c1, c2, tol)
                    if hit:
                        break
                if hit:
                    break
            if hit:
                out.append({"a": i, "b": j, "a_kind": kind_i, "b_kind": kind_j,
                            "how": hit[0], "point": hit[1]})
                if len(out) >= 8:
                    return out
    return out


# ------------------------------------------------------- sketch fillets (exact tangent arcs)

def _ends(prim):
    """[(x, y), (x, y)] of a line/arc primitive, or None."""
    pts = [(x, y) for _w, x, y in _iter_endpoints(prim)]
    return pts if len(pts) == 2 and all(_is_finite(v) for p in pts for v in p) else None


def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 0 else None


def _into(prim, which, corner):
    """Unit tangent at `corner` (endpoint `which` of prim) pointing INTO the primitive."""
    if prim["kind"] == "line":
        far = _ends(prim)[1 - which]
        return _unit((far[0] - corner[0], far[1] - corner[1]))
    c = (float(prim["cx"]), float(prim["cy"]))
    d = int(prim["dir"])
    perp = (-(corner[1] - c[1]), corner[0] - c[0])  # CCW tangent at the corner
    sign = d if which == 0 else -d                   # leaves the start along dir, the end against it
    return _unit((sign * perp[0], sign * perp[1]))


def _locus(prim, corner, guess, r):
    """The fillet centre's locus for one neighbour: ("line", point, direction) at distance r on the
    guess's side, or ("circle", centre, radius) concentric at rho +/- r."""
    if prim["kind"] == "line":
        t = _unit((_ends(prim)[1][0] - _ends(prim)[0][0], _ends(prim)[1][1] - _ends(prim)[0][1]))
        n = (-t[1], t[0])
        side = 1.0 if (guess[0] - corner[0]) * n[0] + (guess[1] - corner[1]) * n[1] >= 0 else -1.0
        return ("line", (corner[0] + side * r * n[0], corner[1] + side * r * n[1]), t)
    c = (float(prim["cx"]), float(prim["cy"]))
    rho = math.hypot(corner[0] - c[0], corner[1] - c[1])
    inside = math.hypot(guess[0] - c[0], guess[1] - c[1]) < rho
    return ("circle", c, rho - r if inside else rho + r)


def _meet(a, b):
    """Intersection points of two loci (line/circle)."""
    if a[0] == "circle" and b[0] == "line":
        a, b = b, a
    if a[0] == "line" and b[0] == "line":
        (p, u), (q, v) = (a[1], a[2]), (b[1], b[2])
        den = u[0] * v[1] - u[1] * v[0]
        if abs(den) < 1e-12:
            return []
        t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / den
        return [(p[0] + t * u[0], p[1] + t * u[1])]
    if a[0] == "line":
        (p, u), (c, rr) = (a[1], a[2]), (b[1], b[2])
        if rr <= 0:
            return []
        fx, fy = p[0] - c[0], p[1] - c[1]
        bq = fx * u[0] + fy * u[1]
        disc = bq * bq - (fx * fx + fy * fy - rr * rr)
        if disc < 0:
            return []
        return [(p[0] + t * u[0], p[1] + t * u[1]) for t in (-bq - math.sqrt(disc), -bq + math.sqrt(disc))]
    (c1, r1), (c2, r2) = (a[1], a[2]), (b[1], b[2])
    d = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
    if r1 <= 0 or r2 <= 0 or d == 0 or d > r1 + r2 or d < abs(r1 - r2):
        return []
    along = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(r1 * r1 - along * along, 0.0))
    mx, my = c1[0] + along * (c2[0] - c1[0]) / d, c1[1] + along * (c2[1] - c1[1]) / d
    return [(mx - s * h * (c2[1] - c1[1]) / d, my + s * h * (c2[0] - c1[0]) / d) for s in (1.0, -1.0)]


def _touch(prim, centre):
    """Where a circle about `centre` touches the neighbour (foot point on its line / circle)."""
    if prim["kind"] == "line":
        (p, q) = _ends(prim)
        u = _unit((q[0] - p[0], q[1] - p[1]))
        t = (centre[0] - p[0]) * u[0] + (centre[1] - p[1]) * u[1]
        return (p[0] + t * u[0], p[1] + t * u[1])
    c = (float(prim["cx"]), float(prim["cy"]))
    rho = math.hypot(float(prim["x1"]) - c[0], float(prim["y1"]) - c[1])
    u = _unit((centre[0] - c[0], centre[1] - c[1]))
    return (c[0] + rho * u[0], c[1] + rho * u[1])


def _keeps(prim, which, corner, point, tol):
    """Whether moving endpoint `which` from the corner to `point` leaves a real, shorter piece."""
    if prim["kind"] == "line":
        far = _ends(prim)[1 - which]
        full = math.hypot(far[0] - corner[0], far[1] - corner[1])
        cut = math.hypot(point[0] - corner[0], point[1] - corner[1])
        return tol < cut < full - tol
    c = (float(prim["cx"]), float(prim["cy"]))
    rho = math.hypot(corner[0] - c[0], corner[1] - c[1])
    a0 = math.atan2(corner[1] - c[1], corner[0] - c[0])
    a1 = math.atan2(point[1] - c[1], point[0] - c[0])
    far = _ends(prim)[1 - which]
    af = math.atan2(far[1] - c[1], far[0] - c[0])
    into = _into(prim, which, corner)
    turn = 1.0 if (corner[0] - c[0]) * into[1] - (corner[1] - c[1]) * into[0] > 0 else -1.0
    cut = (turn * (a1 - a0)) % (2 * math.pi)
    full = (turn * (af - a0)) % (2 * math.pi) or 2 * math.pi
    return tol < cut * rho < full * rho - tol


def resolve_fillets(profile, tol=CONTOUR_WELD_TOL):
    """Replace each {"kind": "fillet", "radius": r} with the exact tangent arc of that radius.

    A fillet sits in the profile list between the two primitives it rounds, which must be lines or
    arcs sharing an endpoint (the sharp corner, as the drawing dimensions it). The arc's centre is
    where the two neighbours' offset loci meet (a parallel line at r, or a concentric circle at
    rho +/- r), nearest the bisector estimate; the neighbours are trimmed back to the tangent
    points. Planners computed these points by hand and landed 0.75 mm off on the 脚踏座 R100 blend
    (findings H15); an edge fillet instead needs an edge anchor (H12). Cyclic: a fillet first or
    last in the list rounds the corner with the other end.

    Returns (new_profile, problems); with problems the profile is returned unchanged.
    """
    profile = list(profile or [])
    spots = [i for i, p in enumerate(profile) if isinstance(p, dict) and p.get("kind") == "fillet"]
    if not spots:
        return profile, []
    out = [copy.deepcopy(p) for p in profile]
    problems = []
    n = len(out)
    for i in spots:
        r = _as_float(out[i].get("radius"))
        prev_i, next_i = (i - 1) % n, (i + 1) % n
        prev, nxt = out[prev_i], out[next_i]
        label = "profile[%d] (fillet R%g mm)" % (i, (r or 0) * 1000)
        if r is None or r <= 0:
            problems.append("%s: 'radius' must be a positive number (meters)." % label)
            continue
        if n < 3 or any(not isinstance(p, dict) or p.get("kind") not in ("line", "arc") or p.get("construction") is True
                        for p in (prev, nxt)):
            problems.append("%s must sit between two (non-construction) line/arc primitives of the outline, "
                            "the sharp corner it rounds; one fillet per corner." % label)
            continue
        e1, e2 = _ends(prev), _ends(nxt)
        if e1 is None or e2 is None or (prev["kind"] == "arc" and prev.get("dir") not in (1, -1)) \
                or (nxt["kind"] == "arc" and nxt.get("dir") not in (1, -1)):
            continue  # the primitives' own checks report them
        pair = min(((a, b) for a in (0, 1) for b in (0, 1)),
                   key=lambda ab: math.hypot(e1[ab[0]][0] - e2[ab[1]][0], e1[ab[0]][1] - e2[ab[1]][1]))
        if math.hypot(e1[pair[0]][0] - e2[pair[1]][0], e1[pair[0]][1] - e2[pair[1]][1]) > tol:
            problems.append("%s: profile[%d] and profile[%d] around it share no endpoint — put the fillet "
                            "between the two primitives that meet at the corner." % (label, prev_i, next_i))
            continue
        corner = e1[pair[0]]
        t1, t2 = _into(prev, pair[0], corner), _into(nxt, pair[1], corner)
        if t1 is None or t2 is None:
            continue
        cos_t = max(-1.0, min(1.0, t1[0] * t2[0] + t1[1] * t2[1]))
        theta = math.acos(cos_t)
        if theta < 1e-6 or math.pi - theta < 1e-6:
            problems.append("%s: the corner is straight (or folds back), so there is nothing to round." % label)
            continue
        b = _unit((t1[0] + t2[0], t1[1] + t2[1]))
        guess = (corner[0] + b[0] * r / math.sin(theta / 2), corner[1] + b[1] * r / math.sin(theta / 2))
        found = _meet(_locus(prev, corner, guess, r), _locus(nxt, corner, guess, r))
        if not found:
            problems.append("%s: no circle of this radius touches both neighbours here — the radius is "
                            "too large for the corner." % label)
            continue
        centre = min(found, key=lambda p: math.hypot(p[0] - guess[0], p[1] - guess[1]))
        p1, p2 = _touch(prev, centre), _touch(nxt, centre)
        if not (_keeps(prev, pair[0], corner, p1, tol) and _keeps(nxt, pair[1], corner, p2, tol)):
            problems.append("%s: the tangent points fall outside profile[%d]/profile[%d] — the radius is too "
                            "large for these segments." % (label, prev_i, next_i))
            continue
        for prim, which, point in ((prev, pair[0], p1), (nxt, pair[1], p2)):
            _set_endpoint(prim, ("start", "end")[which], round(point[0], 9), round(point[1], 9))
        cross = (p1[0] - centre[0]) * (p2[1] - centre[1]) - (p1[1] - centre[1]) * (p2[0] - centre[0])
        out[i] = {"kind": "arc", "cx": round(centre[0], 9), "cy": round(centre[1], 9),
                  "x1": round(p1[0], 9), "y1": round(p1[1], 9), "x2": round(p2[0], 9), "y2": round(p2[1], 9),
                  "dir": 1 if cross > 0 else -1}
    return (profile, problems) if problems else (out, [])


# ------------------------------------------------------- revolve: a profile on one side of its axis

def axis_sides(profile, axis, tol=CONTOUR_WELD_TOL):
    """Where a revolve profile lies relative to its axis line.

    SolidWorks revolves only a profile on ONE side of the axis (touching it is fine); one that
    crosses it comes back as a null feature mid-build. Returns ((min distance, prim), (max distance,
    prim)) over every non-construction point (arcs and closed kinds sampled, sketch fillets
    resolved), signed by the side of the axis line, in metres; None when nothing is measurable.
    """
    x1, y1, x2, y2 = (_as_float(axis.get(k)) for k in ("x1", "y1", "x2", "y2"))
    if None in (x1, y1, x2, y2):
        return None
    ux, uy = x2 - x1, y2 - y1
    length = math.hypot(ux, uy)
    if length == 0:
        return None
    resolved, problems = resolve_fillets(profile)
    if problems:
        return None
    low = high = None
    for i, prim in enumerate(resolved):
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        kind = prim.get("kind")
        if kind in _CLOSED_LOOP_KINDS:
            pts = closed_loop(prim) or []
        elif kind in ("line", "arc", "spline"):
            pts = _prim_points(prim)
        else:
            continue
        for x, y in pts:
            d = (ux * (y - y1) - uy * (x - x1)) / length
            if low is None or d < low[0]:
                low = (d, i)
            if high is None or d > high[0]:
                high = (d, i)
    return (low, high) if low is not None else None


# ------------------------------------------------------- sweep: where the path meets the profile

def chain_ends(profile, tol=CONTOUR_WELD_TOL):
    """The open ends of a line/arc chain (after fillets are resolved): endpoints no other
    primitive shares. A sweep path has two; a closed loop none."""
    resolved, problems = resolve_fillets(profile)
    if problems:
        return []
    pts = []
    for prim in resolved:
        if isinstance(prim, dict) and prim.get("construction") is not True and prim.get("kind") in ("line", "arc"):
            pts.extend((x, y) for _w, x, y in _iter_endpoints(prim))
    return [p for i, p in enumerate(pts)
            if not any(j != i and math.hypot(p[0] - q[0], p[1] - q[1]) <= tol for j, q in enumerate(pts))]


def smallest_bend(profile):
    """The smallest arc radius along a path (sketch fillets resolved), or None for a straight path."""
    resolved, problems = resolve_fillets(profile)
    radii = []
    for prim in resolved if not problems else []:
        if isinstance(prim, dict) and prim.get("kind") == "arc" and prim.get("construction") is not True:
            rr = arc_radii(prim)
            if rr and rr[0] > 0:
                radii.append(rr[0])
    return min(radii) if radii else None
