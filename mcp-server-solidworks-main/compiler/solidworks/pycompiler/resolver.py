"""Reference resolver — the make-or-break module. Semantic refs -> concrete selectors.

Resolves references against LIVE geometry using the existing read-only analyze_model tool.
This is index-based selection (ADR-033, KNOWN-LIMITATIONS #6): the resolver returns face/edge
INDICES that create_sketch(on_face, face_index) / extrude_feature(up_to_face_index) /
add_edge_feature(edge_indices) select directly — no fragile coordinate pick.

v0-exp selectors (deliberately narrow; B = general 3D->sketch-2D mapping is P1.5):
  - top_face: among PLANAR, upward-facing faces (normal . +Y >= 0.9), the one with the greatest
    centroid Y. Deterministic and independent of the actual extrude/cut direction (so it is not
    fooled by SolidWorks' direction quirks, KNOWN-LIMITATIONS #19). v0 has a single body, so
    "the top face of node n" == the body's top face.
  - center: the box is built CENTERED ON THE MODEL ORIGIN, so the origin projects onto the
    top-face plane at the face centre, and a sketch-on-face seeds its 2D origin there -> center
    maps to sketch (0, 0).

v0.5 GEOMETRIC ANCHORS (hybrid decision with the user, 2026-07-04 — see feature-graph.schema.json
reference_model): a 3D point recorded from the ORIGINAL part's analyze read, matched
deterministically against the REBUILT geometry:
  - face anchor ('near' on the target face's plane): plane-containment match — planar faces whose
    plane contains 'near' within _PLANE_TOL; >1 DISTINCT plane matching => REFERENCE_AMBIGUOUS
    (coplanar faces are interchangeable for an up-to-surface, so among them the nearest
    representative point wins).
  - edge anchor ('near' at the target edge's midpoint): tolerance-ball nearest-mid match —
    exactly one edge mid within _EDGE_TOL, else REFERENCE_UNRESOLVED / REFERENCE_AMBIGUOUS.
Anchors are exact for fresh-doc replay (same geometry => same mids, 1µm rounding grid) but NOT
durable across edits — the honest limitation that feeds P1.3. The optional 'hint' field is
NEVER read here (P1.3 data collection only).
"""
import json
import math

from .errors import FeatureError

_UP = (0.0, 1.0, 0.0)   # SolidWorks default "up" axis (+Y). v0-exp binds 'top' to global +Y.
_NORMAL_TOL = 0.9       # a face counts as "upward" when normal . up >= this
_TIE_EPS = 1e-6         # two upward faces this close in height => ambiguous
_PLANE_TOL = 1e-5       # anchor->plane distance for a face-anchor match (10x the 1µm grid)
_EDGE_TOL = 1e-4        # anchor->mid distance ball for an edge-anchor match (0.1 mm)
# Circular-edge (bore mouth) matching. A blind hole contributes TWO identical circles — the
# mouth and the bottom — separated along the circle's own normal by the cut depth, so a
# coordinate ball alone can never tell them apart; the MOUTH is the one further along the
# direction that points OUT of the material at the entry face. These tolerances decide "is this
# edge the circle I mean": same centre, same axis (sign-insensitive), same radius.
_BORE_TOL = 1e-4        # centre/radius agreement for a bore-face-derived circle (0.1 mm)
_AXIS_TOL = 1e-3        # |cos| slack when comparing circle axes


# ---------------------------------------------------------------------------------------------
# analyze payload memo (SPEED task A)
#
# The geometry a resolver reads is a pure function of (state_version, analysis_type, name,
# narrowing args): no resolver call mutates the document, so a read taken at state_version N
# stays valid until something BUMPS state_version. A single feature graph still re-reads the
# same full faces/edges dump once per resolver call (a fillet with 8 anchors, a hole plus a
# face anchor, ...), and the production log shows 264 of 463 analyze_model calls repeating a
# read already taken at the SAME state_version — 2 573 s, 32% of all backend time.
#
# Scope is deliberately ONE compile_and_run: `begin_run` clears the memo, the compiler calls
# `invalidate` whenever an op returns a NEW state_version, and `end_run` clears it again so
# nothing survives into the next graph (a live run's cache must never leak a stale document).
# A cached entry is therefore only ever served for the exact state_version it was read at.
# ---------------------------------------------------------------------------------------------
class _AnalyzeMemo(object):
    """Per-run cache of analyze_model/analyze_assembly payloads, keyed by read identity."""

    def __init__(self):
        self._entries = {}

    def reset(self):
        self._entries.clear()

    def key(self, tool, state_version, analysis_type, name, narrowing):
        # `narrowing` is the tool's own narrowing args (near/k/axis/component). Sorted items so
        # an equal set of args always maps to the same key; JSON keeps lists hashable+comparable.
        return (
            tool,
            state_version,
            analysis_type,
            name,
            json.dumps(narrowing or {}, sort_keys=True, default=str),
        )

    def get(self, key):
        return self._entries.get(key)

    def put(self, key, value):
        self._entries[key] = value

    def invalidate(self, state_version):
        """Drop every entry not read at `state_version` (a write advanced the document)."""
        for key in [k for k in self._entries if k[1] != state_version]:
            del self._entries[key]

    def __len__(self):
        return len(self._entries)


MEMO = _AnalyzeMemo()


def begin_run():
    """Start a compile_and_run scope: nothing from a previous run may be reused."""
    MEMO.reset()


def end_run():
    """Close the scope so no payload outlives the graph that produced it."""
    MEMO.reset()


def invalidate(state_version):
    """A write op advanced the document — drop reads taken at any other state_version."""
    MEMO.invalidate(state_version)


def resolve_top_face(port, state_version, node_id=None):
    """Return (face_index, info) for the semantic 'top' face. Raises FeatureError on
    REFERENCE_UNRESOLVED (no upward face / analyze failed) or REFERENCE_AMBIGUOUS (a tie).

    Routed through _analyze_payload so it shares the per-run memo (SPEED task A): a hole, an
    up-to face anchor and an offset sketch on the same body all want this same dump."""
    data = _analyze_payload(port, state_version, "faces", node_id, "resolve_top_face")

    candidates = []  # (centroid_y, face_index, face)
    for f in data.get("faces") or []:
        if not f.get("planar"):
            continue
        n = f.get("normal")
        p = f.get("point")
        if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
            continue
        dot = n[0] * _UP[0] + n[1] * _UP[1] + n[2] * _UP[2]
        if dot >= _NORMAL_TOL:
            candidates.append((p[1], int(f.get("i", -1)), f))

    if not candidates:
        raise FeatureError("no upward-facing planar face found — a 'top face' could not be resolved",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step="resolve_top_face")

    candidates.sort(key=lambda c: c[0], reverse=True)
    top_y, top_i, top_face = candidates[0]
    if top_i < 0:
        raise FeatureError("resolved top face has no valid index",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step="resolve_top_face")
    if len(candidates) > 1 and abs(candidates[1][0] - top_y) < _TIE_EPS:
        raise FeatureError("top face is ambiguous — multiple upward faces at the same height",
                           code="REFERENCE_AMBIGUOUS", node_id=node_id, step="resolve_top_face")

    return top_i, {"face_index": top_i, "centroid_y": top_y, "point": top_face.get("point")}


def resolve_center_on_face(face_info):
    """Resolve position 'center' to sketch-2D coords. v0-exp: origin-centered box -> sketch (0, 0).
    (General 3D point -> sketch-2D projection for non-origin faces is deferred to P1.5 / option B.)"""
    return (0.0, 0.0)


def _analyze_payload(port, state_version, analysis_type, node_id, step):
    """Run read-only analyze_model(analysis_type) through the port and parse its JSON payload.

    Memoized for the duration of one compile_and_run (SPEED task A): the SAME read at the SAME
    state_version is issued to the backend once. A write bumps state_version, the compiler calls
    invalidate(), and the next resolver call re-reads. Errors are never cached — a failed read
    raises and is retried by the caller's own control flow."""
    params = {"analysis_type": analysis_type, "name": ""}
    key = MEMO.key("analyze_model", state_version, analysis_type, "", None)
    cached = MEMO.get(key)
    if cached is not None:
        return cached
    resp = port.execute("analyze_model", params, state_version)
    if resp.get("status") != "COMPLETED":
        err = resp.get("error") or {}
        raise FeatureError("analyze_model(%s) failed: %s" % (analysis_type, err.get("message")),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    feats = (resp.get("cadState") or {}).get("features") or []
    if not feats:
        raise FeatureError("analyze_model(%s) returned no data (is there a solid body?)" % analysis_type,
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    try:
        data = json.loads(feats[0])
    except Exception as ex:
        raise FeatureError("could not parse analyze_model(%s) payload: %s" % (analysis_type, ex),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    MEMO.put(key, data)
    return data


def _dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def resolve_face_by_anchor(port, state_version, near, node_id=None):
    """Face anchor -> face index (the up-to-surface end-condition reference).

    Match rule: planar faces whose PLANE contains 'near' (|(near - point) . normal| <= _PLANE_TOL).
    The face's reported 'point' is a REPRESENTATIVE on-plane point, not a centroid — so the plane
    test is primary and the nearest representative point only breaks ties among COPLANAR matches
    (coplanar faces share the same surface, so any of them terminates an up-to identically).
    Faces on DISTINCT planes both matching => REFERENCE_AMBIGUOUS (anchor sits between two planes
    closer than the tolerance — should not happen on a 1µm-rounded rebuild)."""
    step = "resolve_face_by_anchor"
    data = _analyze_payload(port, state_version, "faces", node_id, step)

    matches = []  # (euclidean_dist_to_point, face_index, plane_key)
    for f in data.get("faces") or []:
        if not f.get("planar"):
            continue
        n = f.get("normal")
        p = f.get("point")
        if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
            continue
        plane_dist = abs((near[0] - p[0]) * n[0] + (near[1] - p[1]) * n[1] + (near[2] - p[2]) * n[2])
        if plane_dist <= _PLANE_TOL:
            # Plane key: normal direction (sign-insensitive) + plane offset, quantized, so
            # coplanar faces group together and distinct planes are told apart.
            nn = tuple(round(abs(c), 3) for c in n[:3])
            d = round(abs(near[0] * n[0] + near[1] * n[1] + near[2] * n[2]), 5)
            matches.append((_dist3(near, p), int(f.get("i", -1)), (nn, d)))

    if not matches:
        raise FeatureError("no planar face's plane contains the anchor point %s — the up-to face "
                           "could not be resolved" % (list(near),),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    distinct_planes = set(m[2] for m in matches)
    if len(distinct_planes) > 1:
        raise FeatureError("face anchor %s matches faces on %d DISTINCT planes — ambiguous"
                           % (list(near), len(distinct_planes)),
                           code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
    matches.sort(key=lambda m: m[0])
    idx = matches[0][1]
    if idx < 0:
        raise FeatureError("anchored face has no valid index",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    return idx, {"face_index": idx, "coplanar_candidates": len(matches)}


_DATUM_AXIS = {"front": (0.0, 0.0, 1.0), "top": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0)}


def resolve_face_on_plane(port, state_version, datum, offset, node_id=None):
    """An EXISTING planar face lying ON the (datum, offset) plane -> face index, or None.

    Preference order for an offset sketch (decided with the user, 2026-07-05): sketch ON the
    face when one exists there — matching how the original was authored and avoiding scaffolding
    planes; create a reference plane only when no face lies on that plane. Among coplanar
    candidates the LARGEST face wins (most robust support; they share the same plane anyway).
    Unlike the anchor resolvers, None is a NORMAL outcome (no body yet / plane in empty space) —
    the caller falls back to add_reference_geometry."""
    try:
        data = _analyze_payload(port, state_version, "faces", node_id, "resolve_face_on_plane")
    except FeatureError:
        return None  # e.g. first sketch, no solid body yet
    axis = _DATUM_AXIS[datum]
    best = None  # (area, face_index)
    for f in data.get("faces") or []:
        if not f.get("planar"):
            continue
        n = f.get("normal")
        p = f.get("point")
        if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
            continue
        if abs(n[0] * axis[0] + n[1] * axis[1] + n[2] * axis[2]) < 0.999:
            continue  # not parallel to the datum
        pos = p[0] * axis[0] + p[1] * axis[1] + p[2] * axis[2]
        if abs(pos - float(offset)) > _PLANE_TOL:
            continue
        area = f.get("area") or 0.0
        idx = int(f.get("i", -1))
        if idx >= 0 and (best is None or area > best[0]):
            best = (area, idx)
    return best[1] if best else None


def _analyze_assembly_payload(port, state_version, analysis_type, component, node_id, step):
    """Run read-only analyze_assembly(analysis_type, component) and parse its JSON payload.

    Memoized per (component, analysis_type, state_version) for the duration of ONE
    compile_and_run (SPEED task A) — a mate graph resolves several anchors on the same
    component at the same state_version and used to re-read the same dump each time."""
    params = {"analysis_type": analysis_type, "component": component}
    key = MEMO.key("analyze_assembly", state_version, analysis_type, component, None)
    cached = MEMO.get(key)
    if cached is not None:
        return cached
    resp = port.execute("analyze_assembly", params, state_version)
    if resp.get("status") != "COMPLETED":
        err = resp.get("error") or {}
        raise FeatureError("analyze_assembly(%s, %s) failed: %s"
                           % (analysis_type, component, err.get("message")),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    feats = (resp.get("cadState") or {}).get("features") or []
    if not feats:
        raise FeatureError("analyze_assembly(%s, %s) returned no data" % (analysis_type, component),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    try:
        data = json.loads(feats[0])
    except Exception as ex:
        raise FeatureError("could not parse analyze_assembly(%s) payload: %s" % (analysis_type, ex),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    MEMO.put(key, data)
    return data


_FACE_ANCHOR_KINDS = frozenset(("plane", "cylinder", "cone", "sphere"))
_EDGE_ANCHOR_KINDS = frozenset(("line", "circle"))


def _point_axis_distance(p, origin, axis):
    """Distance from point p to the infinite line origin + t*axis (axis unit-ish)."""
    d = [p[i] - origin[i] for i in range(3)]
    t = d[0] * axis[0] + d[1] * axis[1] + d[2] * axis[2]
    proj = [origin[i] + t * axis[i] for i in range(3)]
    return _dist3(p, proj)


def resolve_component_entity_by_anchor(port, state_version, component_name, anchor, node_id=None):
    """A mate side's COMPONENT-LOCAL anchor {kind, near} -> ('face'|'edge', index) on that
    component — index-first selection for add_mate (KNOWN-LIMITATIONS #6).

    The anchor coordinates are COMPONENT-LOCAL (the IR generator maps the original mate's
    assembly-space EntityParams through the inverse component transform), which makes them
    transform-invariant: they match the rebuild's component regardless of where it currently
    sits. analyze_assembly(faces|edges, component) reports the same local coords (proven live).

    Match rules by kind (the reader's swMateEntity2ReferenceType_e name, verbatim):
      plane            -> planar faces whose plane contains 'near' (distinct planes = ambiguous;
                          nearest representative point among coplanar wins — coplanar faces are
                          interchangeable constraints);
      cylinder         -> faces reported kind='cylinder' with |dist(near, axis) - radius| <= tol
                          (coaxial same-radius faces group; distinct axes = ambiguous);
      line | circle    -> edges whose MID lies within the anchor ball (exactly one);
      cone | sphere    -> not resolvable yet (loud VOCABULARY/RESOLVER gap, C5)."""
    step = "resolve_component_entity_by_anchor"
    kind = anchor.get("kind")
    near = anchor["near"]

    if kind in _EDGE_ANCHOR_KINDS:
        data = _analyze_assembly_payload(port, state_version, "edges", component_name, node_id, step)
        in_ball = []
        nearest = None
        for e in data.get("edges") or []:
            m = e.get("mid")
            if not (isinstance(m, list) and len(m) >= 3):
                continue
            d = _dist3(near, m)
            nearest = d if nearest is None or d < nearest else nearest
            if d <= _EDGE_TOL:
                in_ball.append((d, int(e.get("i", -1))))
        if not in_ball:
            raise FeatureError("edge anchor %s matched no edge mid on '%s' within %g m (nearest %.6f m)"
                               % (list(near), component_name, _EDGE_TOL, nearest if nearest is not None else -1),
                               code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        if len(in_ball) > 1:
            raise FeatureError("edge anchor %s matched %d edges on '%s' — ambiguous"
                               % (list(near), len(in_ball), component_name),
                               code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
        return "edge", in_ball[0][1]

    if kind not in _FACE_ANCHOR_KINDS:
        raise FeatureError("anchor kind %r is not resolvable (supported: %s)"
                           % (kind, sorted(_FACE_ANCHOR_KINDS | _EDGE_ANCHOR_KINDS)),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    if kind in ("cone", "sphere"):
        raise FeatureError("anchor kind %r is a recorded RESOLVER gap (v0 resolves plane/cylinder "
                           "faces and line/circle edges)" % kind,
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    # Optional stored DIRECTION (C7 ground truth from the original mate's EntityParams): the
    # plane NORMAL / cylinder AXIS. A stored point can lie on TWO distinct planes (the 4-1
    # interior-anchor lesson — hit live on 1-1's shaft flat); the direction disambiguates.
    a_dir = anchor.get("dir")
    a_radius = anchor.get("radius")

    data = _analyze_assembly_payload(port, state_version, "faces", component_name, node_id, step)
    matches = []  # (tie_break_dist, face_index, group_key)
    for f in data.get("faces") or []:
        idx = int(f.get("i", -1))
        p = f.get("point")
        if kind == "plane":
            if not f.get("planar"):
                continue
            n = f.get("normal")
            if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
                continue
            if a_dir is not None and abs(n[0] * a_dir[0] + n[1] * a_dir[1] + n[2] * a_dir[2]) < 0.999:
                continue  # normal disagrees with the stored one — a different plane
            plane_dist = abs((near[0] - p[0]) * n[0] + (near[1] - p[1]) * n[1] + (near[2] - p[2]) * n[2])
            if plane_dist <= _PLANE_TOL:
                nn = tuple(round(abs(c), 3) for c in n[:3])
                d = round(abs(near[0] * n[0] + near[1] * n[1] + near[2] * n[2]), 5)
                matches.append((_dist3(near, p), idx, (nn, d)))
        else:  # cylinder
            if f.get("kind") != "cylinder":
                continue
            origin, axis, radius = f.get("origin"), f.get("axis"), f.get("radius")
            if not (isinstance(origin, list) and isinstance(axis, list) and _is_num(radius)):
                continue
            if a_dir is not None and abs(axis[0] * a_dir[0] + axis[1] * a_dir[1] + axis[2] * a_dir[2]) < 0.999:
                continue  # axis disagrees with the stored one
            if a_radius is not None and abs(float(radius) - float(a_radius)) > _PLANE_TOL:
                continue  # the stored fit radius pins WHICH coaxial surface is meant
            if abs(_point_axis_distance(near, origin, axis) - float(radius)) <= _PLANE_TOL:
                ax = tuple(round(abs(c), 3) for c in axis[:3])
                # axis line key: direction + the origin's projection distance off the axis
                # through the model origin — coaxial faces share it.
                op_ = _point_axis_distance((0.0, 0.0, 0.0), origin, axis)
                key = (ax, round(op_, 5), round(float(radius), 5))
                tie = _dist3(near, p) if isinstance(p, list) and len(p) >= 3 else 0.0
                matches.append((tie, idx, key))

    if not matches:
        raise FeatureError("%s anchor %s matched no face on '%s'"
                           % (kind, list(near), component_name),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    if len(set(m[2] for m in matches)) > 1:
        raise FeatureError("%s anchor %s matches faces on %d DISTINCT surfaces of '%s' — ambiguous"
                           % (kind, list(near), len(set(m[2] for m in matches)), component_name),
                           code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
    matches.sort(key=lambda m: m[0])
    idx = matches[0][1]
    if idx < 0:
        raise FeatureError("anchored face has no valid index",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    return "face", idx


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve_edges_by_anchor(port, state_version, anchors, node_id=None):
    """Edge anchors -> edge indices (for add_edge_feature edge_indices).

    ONE analyze_model(edges) read serves all anchors. Per anchor: edges whose MID lies within
    the _EDGE_TOL ball around 'near' — exactly one => matched; zero => REFERENCE_UNRESOLVED;
    more than one => REFERENCE_AMBIGUOUS. Two anchors matching the SAME edge is an error too
    (duplicate anchor). anchors: iterable of {near: [x,y,z], hint?: str} (hint is never read)."""
    step = "resolve_edges_by_anchor"
    data = _analyze_payload(port, state_version, "edges", node_id, step)

    mids = []  # (edge_index, mid)
    for e in data.get("edges") or []:
        m = e.get("mid")
        if isinstance(m, list) and len(m) >= 3:
            mids.append((int(e.get("i", -1)), m))
    if not mids:
        raise FeatureError("analyze_model(edges) reported no edge midpoints",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    resolved = []
    taken = {}
    for k, anchor in enumerate(anchors):
        near = anchor["near"]
        in_ball = [(d, i) for (i, m) in mids for d in (_dist3(near, m),) if d <= _EDGE_TOL]
        if not in_ball:
            nearest = min(_dist3(near, m) for (_i, m) in mids)
            raise FeatureError("edge anchor %d %s matched no edge midpoint within %g m "
                               "(nearest was %.6f m away)" % (k, list(near), _EDGE_TOL, nearest),
                               code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        if len(in_ball) > 1:
            raise FeatureError("edge anchor %d %s matched %d edge midpoints within %g m — ambiguous"
                               % (k, list(near), len(in_ball), _EDGE_TOL),
                               code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
        idx = in_ball[0][1]
        if idx < 0:
            raise FeatureError("edge anchor %d resolved to an edge with no valid index" % k,
                               code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        if idx in taken:
            raise FeatureError("edge anchors %d and %d resolved to the SAME edge #%d — duplicate anchor"
                               % (taken[idx], k, idx),
                               code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
        taken[idx] = k
        resolved.append(idx)
    return resolved, {"edge_indices": resolved}


# ---------------------------------------------------------------------------------------------
# Thread edge (v0.9.0) — the CIRCULAR edge at a bore MOUTH
#
# A cosmetic thread attaches to the circular edge at the mouth of a bore. analyze_model(edges)
# tags ONLY closed full-circle edges with kind/center/normal/radius (a partial arc, a line and a
# spline carry none), so the candidate set is exactly "edges that report kind == 'circle'".
#
# THE AMBIGUITY THAT MATTERS: a blind hole has TWO identical circles — the mouth and the bottom —
# with the same axis and the same radius, separated along the axis by the cut depth. A coordinate
# ball cannot tell them apart, and picking the wrong one threads the bottom of the hole. The
# MOUTH is the one whose centre lies furthest along the direction pointing OUT of the material
# at the entry face; for a bore cut into a top face (face normal +Z) that is the larger centre.z.
# So every path below ends in one shared mouth rule, and a wrong-end pick is impossible by
# construction. A through hole's two circles are its two mouths, so either is valid — the
# extreme is still deterministic, which is what matters for reproducibility.
# ---------------------------------------------------------------------------------------------

def _circle_edges(data):
    """[(edge_index, center, normal, radius)] for the edges the reader tagged kind='circle'."""
    out = []
    for e in data.get("edges") or []:
        if e.get("kind") != "circle":
            continue
        c, n, r = e.get("center"), e.get("normal"), e.get("radius")
        if not (isinstance(c, list) and len(c) >= 3 and isinstance(n, list) and len(n) >= 3
                and _is_num(r)):
            continue
        out.append((int(e.get("i", -1)), c, n, float(r)))
    return out


def _body_centroid(port, state_version, node_id, step):
    """The largest solid body's centroid — a point INSIDE the material. None if unavailable.

    Used to orient a bore's axis: the MOUTH is the end further from the material's interior, so
    "furthest from the centroid along the axis" needs no guess about the sign of a circle's
    normal (the reader reports the curve's own axis, and its sign is arbitrary).

    Read from analyze_model(bodies), which reports `centroid` per body — NOT from
    mass_properties, whose payload is a list of "cx=.." STRINGS rather than JSON numbers. The
    largest body by volume wins (a multibody part's thread belongs to the part, not a scrap).
    Read-only and memoized like every other analyze read.
    """
    try:
        data = _analyze_payload(port, state_version, "bodies", node_id, step)
    except FeatureError:
        return None
    best = None
    for body in data.get("bodies") or []:
        c = body.get("centroid")
        if not (isinstance(c, list) and len(c) >= 3 and all(_is_num(x) for x in c[:3])):
            continue
        vol = body.get("volume")
        score = float(vol) if _is_num(vol) else 0.0
        if best is None or score > best[0]:
            best = (score, [float(x) for x in c[:3]])
    return None if best is None else best[1]


def _axial_rank(center, axis, reference):
    """How far a circle's centre lies from `reference`, measured ALONG the bore axis.

    This is an UNSIGNED distance on purpose. The caller always supplies the reference it means
    and then sorts in the direction it means:

      * entry_point  -> sort ASCENDING  (nearest the entry face = the mouth)
      * interior     -> sort DESCENDING (furthest from inside the material = the mouth)

    Do NOT read a direction into the number: `axis` is the circle's own reported normal and its
    SIGN is arbitrary (SolidWorks reports the curve's axis, not an outward one), so any signed
    reading would flip with the reader's choice of normal. A plain distance cannot.
    """
    d = [center[i] - reference[i] for i in range(3)]
    t = d[0] * axis[0] + d[1] * axis[1] + d[2] * axis[2]
    return abs(t)


def _pick_mouth(candidates, interior, axis_hint, node_id, step, what, entry_point=None,
                 tie_is_one_bore=False):
    """The bore MOUTH circle among `candidates` — deterministic, never ambiguous for one bore.

    `candidates` is [(index, center, normal, radius)]. Two rules, in order:

      1. ENTRY SIDE (authoritative when `entry_point` is known). The thread starts at the face
         the bore was entered from, so the mouth is the circle whose centre is nearest that
         point along the bore axis. This is the ONLY rule that can separate a THROUGH bore's two
         identical end circles: they sit at the SAME distance from the material's interior
         (that is what "through" means), so the interior rule ties on them by construction and
         raising ambiguity there would block every through thread — M42x2/M40x2 included.
      2. INTERIOR (fallback when there is no entry point). The circle FURTHEST from a point
         inside the material: a blind bore's bottom is closer to the interior than its mouth.

    A tie is only reported when NEITHER rule can order the candidates — two circles the same
    distance from the interior AND the same distance from the entry point, which means two
    genuinely different bores.
    """
    if not candidates:
        raise FeatureError("%s: no circular edge candidates" % what,
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    axis = axis_hint or candidates[0][2]

    def _rank(entry):
        return sorted(((_axial_rank(c, axis, entry), i, c, n, r) for (i, c, n, r) in candidates),
                      key=lambda t: t[0])

    if entry_point is not None:
        ranked = _rank(entry_point)          # nearest the entry face wins
        if len(ranked) == 1 or abs(ranked[0][0] - ranked[1][0]) > _BORE_TOL:
            return ranked[0]
        # The two ends are equidistant from the entry point too (a symmetric bore): fall
        # through to the interior rule, which can still order a blind bore.

    if interior is None:
        # No centroid: the midpoint of the candidate centres stands in for "inside".
        interior = [sum(c[i] for (_x, c, _n, _r) in candidates) / float(len(candidates))
                    for i in range(3)]
    ranked = _rank(interior)
    ranked.reverse()                          # furthest from the interior wins
    if tie_is_one_bore and len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) <= _BORE_TOL:
        # Both ends of the SAME bore (same axis, same radius — the caller built the candidate
        # set that way). Deterministic and honest: the lowest edge index wins, so the same
        # graph always threads the same end and a rebuild is reproducible.
        return min(ranked, key=lambda t: t[1])
    if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) <= _BORE_TOL:
        raise FeatureError(
            "%s: %d circular edges are the same distance from both the entry point and the "
            "material's interior (%s) — they are two different bores, not the two ends of one. "
            "Anchor the mouth circle's CENTRE with ref.edge_anchor instead of ref.bore_face."
            % (what, len(ranked), ", ".join("#%d" % t[1] for t in ranked[:4])),
            code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
    return ranked[0]


def resolve_thread_edge(port, state_version, ref, node_id=None):
    """A thread ref -> (edge_index, info) for the bore MOUTH circle.

    Two anchor kinds, both validated structurally before they get here:

      ref.edge_anchor {near}      `near` is the mouth circle's CENTRE (the reader reports it as
                                  `center` for kind='circle'). A circle's `mid` is one arbitrary
                                  point on its circumference, so a mid-ball test would depend on
                                  where the reader happened to sample — hence the centre.
      ref.bore_face {near, radius?}
                                  a point ON the cylindrical face; from it, the circles sharing
                                  that axis and radius, then the mouth rule.

    Both paths end in the same mouth rule. Raises REFERENCE_UNRESOLVED / REFERENCE_AMBIGUOUS —
    the same codes the rest of the resolver uses, so the failure surface does not grow.
    """
    step = "resolve_thread_edge"
    edges = _analyze_payload(port, state_version, "edges", node_id, step)
    circles = _circle_edges(edges)
    if not circles:
        raise FeatureError(
            "analyze_model(edges) reported no circular edges (kind='circle') — a cosmetic "
            "thread needs the CIRCULAR edge at a bore mouth. Does this build report circle "
            "descriptors, and does the part actually contain a bore?",
            code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    edge_anchor = ref.get("edge_anchor")
    if edge_anchor is not None:
        near = edge_anchor["near"]
        want_radius = edge_anchor.get("radius")
        # RADIUS IS A DISAMBIGUATOR, not an axis filter. A revolve end face carries the bore
        # mouth AND an outer rim as CONCENTRIC COPLANAR circles; a counterbored hole carries the
        # pilot mouth and the counterbore-floor rim the same way. Their centres are identical, so
        # only the radius separates them.
        pool = circles
        if want_radius is not None:
            pool = [c for c in circles if abs(c[3] - float(want_radius)) <= _BORE_TOL]
            if not pool:
                raise FeatureError(
                    "ref.edge_anchor %s asks for a circle of radius %g m and no circular edge "
                    "matches it (radii present: %s)."
                    % (list(near), float(want_radius),
                       ", ".join("%g" % c[3] for c in circles[:8])),
                    code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        near_c = [(_dist3(near, c), i, c, n, r) for (i, c, n, r) in pool]
        near_c.sort(key=lambda t: t[0])
        best = near_c[0]
        if best[0] > _EDGE_TOL:
            raise FeatureError(
                "ref.edge_anchor %s matched no circular-edge CENTRE within %g m (nearest was "
                "#%d at %.6f m). The anchor names the mouth circle's CENTRE (analyze_model"
                "(edges) reports it as `center` for kind='circle'), not a point on the rim."
                % (list(near), _EDGE_TOL, best[1], best[0]),
                code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        # Same centre, opposite ends of a bore (a blind bore's bottom, or a through bore's far
        # end). Break the tie by the mouth rule with the anchor as the ENTRY reference: the
        # anchor names the mouth, so the end nearest it is the mouth — and for a through bore,
        # whose two ends are equidistant from the material's interior, this is the ONLY rule
        # that can decide. Never raise for the two ends of one bore.
        tied = [t for t in near_c if abs(t[0] - best[0]) <= _BORE_TOL]
        if len(tied) == 1:
            return best[1], {"edge_index": best[1], "edge_center": best[2],
                             "edge_normal": best[3], "edge_radius": best[4],
                             "matched": "edge_anchor"}
        interior = _body_centroid(port, state_version, node_id, step)
        _rank, idx, center, normal, radius = _pick_mouth(
            [(t[1], t[2], t[3], t[4]) for t in tied], interior, best[3], node_id, step,
            "ref.edge_anchor %s" % list(near),
            entry_point=near)
        return idx, {"edge_index": idx, "edge_center": center, "edge_normal": normal,
                     "edge_radius": radius, "matched": "edge_anchor (mouth rule)"}

    bore_face = ref["bore_face"]
    near = bore_face["near"]
    want_radius = bore_face.get("radius")
    faces = _analyze_payload(port, state_version, "faces", node_id, step)

    cylinders = []
    for f in faces.get("faces") or []:
        if f.get("kind") != "cylinder":
            continue
        origin, axis, radius = f.get("origin"), f.get("axis"), f.get("radius")
        if not (isinstance(origin, list) and isinstance(axis, list) and _is_num(radius)):
            continue
        if want_radius is not None and abs(float(radius) - float(want_radius)) > _BORE_TOL:
            continue
        if abs(_point_axis_distance(near, origin, axis) - float(radius)) > _BORE_TOL:
            continue
        cylinders.append((int(f.get("i", -1)), origin, axis, float(radius)))
    if not cylinders:
        raise FeatureError(
            "ref.bore_face %s matched no cylindrical face%s — check the point is ON the bore's "
            "surface and, when given, that `radius` is that bore's radius."
            % (list(near), "" if want_radius is None else " of radius %g m" % float(want_radius)),
            code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    if len(cylinders) > 1:
        # Coaxial same-radius faces cannot be told apart by the anchor; the reader reports one
        # face per connected surface, so this is a real ambiguity worth refusing.
        raise FeatureError(
            "ref.bore_face %s matched %d cylindrical faces (%s) — add 'radius' to pin which "
            "coaxial bore is meant, or anchor the mouth edge directly."
            % (list(near), len(cylinders), ", ".join("#%d r=%g" % (c[0], c[3])
                                                     for c in cylinders[:4])),
            code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
    _fidx, origin, axis, radius = cylinders[0]

    # The circles ON that cylinder: same axis line, same radius.
    on_axis = []
    for (i, c, n, r) in circles:
        if abs(r - radius) > _BORE_TOL:
            continue
        if abs(abs(n[0] * axis[0] + n[1] * axis[1] + n[2] * axis[2]) - 1.0) > _AXIS_TOL:
            continue
        if _point_axis_distance(c, origin, axis) > _BORE_TOL:
            continue
        on_axis.append((i, c, n, r))
    if not on_axis:
        raise FeatureError(
            "bore face #%d (r=%g m) has no circular edge on its own axis/radius — the bore's "
            "mouth and bottom circles should be there; is the face a full cylinder?"
            % (_fidx, radius),
            code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    # The mouth is the circle nearest the ANCHOR, projected on this cylinder's axis: a bore_face
    # anchor is a point on the bore's surface, so the end it is written next to is the end the
    # author means. That resolves a THROUGH bore's two equidistant ends (the interior rule ties
    # on them by construction) and it also beats the interior rule on a COUNTERBORED blind bore,
    # where the counterbore floor makes the pilot's bottom look "further out" than its mouth.
    # The axis is passed as the axis_hint so the ranking never depends on the sign of a circle's
    # reported normal (which is the curve's axis, sign arbitrary).
    interior = _body_centroid(port, state_version, node_id, step)
    # `on_axis` is by construction the circles of ONE cylinder (same axis, same radius), so a
    # tie between two of them is that bore's two ENDS — a through bore, where either end is a
    # valid mouth (end_type 2 threads the whole bore). Never raise there: pick deterministically.
    _rank, idx, center, normal, _r = _pick_mouth(on_axis, interior, axis, node_id, step,
                                                 "ref.bore_face %s" % list(near),
                                                 entry_point=near, tie_is_one_bore=True)
    return idx, {"edge_index": idx, "edge_center": center, "edge_normal": normal,
                 "edge_radius": radius, "bore_face": _fidx,
                 "matched": "bore_face (mouth rule)"}
