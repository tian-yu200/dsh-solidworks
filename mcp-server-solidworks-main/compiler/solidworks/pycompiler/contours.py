"""Contour closure analysis for sketch profiles (line/arc/spline chains).

Why: the frozen-coordinate IR closes a contour ONLY through identical shared
endpoint coordinates, and computed outlines (tangent points, filleted corners)
drift by microns — enough for the execution layer's cut to come back null and
roll back a half-built part (observed live on the 阀体 rebuild, 2026-08-17:
EXTRUSION_FAILED on a profile, sketch-node timeouts, then the model "succeeded"
by dropping features). Two deterministic compiler-side defenses:

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

Non-finite endpoints (NaN / +/-Inf) are never welded and never count as paired —
NaN compares False to everything, so it would otherwise look like a closed contour.

Only CHAIN primitives participate. rectangle/circle/ellipse are closed by
construction; construction-flagged primitives are scaffolding whose coordinates
are free-form; rib/bend/sweep-path sketches are open by design and are never
checked (the caller decides which sketches require closure).
"""
import copy
import math

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
