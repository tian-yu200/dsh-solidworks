"""Where a failed extrude's profile sat relative to the solid built so far (findings H11, 2026-11-08).

SolidWorks answers a cut or boss it cannot make with a bare null, and the orchestrator discards the
document right after, so the planner had nothing to reason from: on the v2 bench three repairs in a
row guessed "the direction is reversed", flipped it, and failed identically. The execution layer had
already tried both directions of every cut, so the guess could never have helped.

MaterialLog follows the ops the compiler sends (each sketch's MEASURED frame, the profile entities in
that frame, every boss that completed) and keeps one axis-aligned box per modelled solid. When an
extrude fails, explain() states, from those boxes alone:
  - where the profile and the solid are, in model millimetres;
  - for a cut, whether any profile region can reach material in either direction (a region that
    reaches none is a placement error, not a direction error);
  - a circle that only touches a planar face of a box-shaped solid (a zero-thickness wall).

Boxes over-approximate (a cylinder's box includes its corners), so "reaches material" can be wrong
but "reaches none" never is, as long as every material-adding node was modelled. Nodes it cannot
model (sweeps, lofts, patterns of bosses, sheet metal...) are named, and the conclusion is weakened
to say so. No execution calls: this costs the happy path nothing.
"""
import math

_TOL = 1e-6        # metres; overlap/touch band for boxes
_TOUCH = 2e-5      # a circle within 0.02 mm of a planar face touches it
_THROUGH = 10.0    # metres; "through all" reach for a box test

_PATTERN_TOOLS = ("create_pattern",)
_UNMODELLED_TOOLS = ("create_rib", "sheet_metal_feature")


def _frame(frame):
    """(origin, xdir, ydir, normal) as float tuples, or None."""
    if not isinstance(frame, dict):
        return None
    vecs = []
    for key in ("origin", "xdir", "ydir"):
        v = frame.get(key)
        if not (isinstance(v, (list, tuple)) and len(v) >= 3
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v[:3])):
            return None
        vecs.append(tuple(float(c) for c in v[:3]))
    o, x, y = vecs
    n = (x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0])
    ln = math.sqrt(sum(c * c for c in n))
    if ln == 0.0:
        return None
    return o, x, y, tuple(c / ln for c in n)


def _to_model(fr, u, v):
    o, x, y, _n = fr
    return tuple(o[i] + u * x[i] + v * y[i] for i in range(3))


def _num(params, key):
    v = params.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _entity_box(params):
    """(umin, vmin, umax, vmax, kind) of one sketch entity in its sketch frame, or None."""
    if params.get("construction") is True:
        return None
    kind = params.get("entity_type")
    if kind == "circle":
        cx, cy, r = _num(params, "cx"), _num(params, "cy"), _num(params, "radius")
        if None in (cx, cy, r):
            return None
        return (cx - r, cy - r, cx + r, cy + r, kind)
    if kind in ("arc_center", "ellipse"):
        cx, cy, x1, y1 = (_num(params, k) for k in ("cx", "cy", "x1", "y1"))
        x2, y2 = _num(params, "x2"), _num(params, "y2")
        if None in (cx, cy, x1, y1, x2, y2):
            return None
        r = max(math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy))
        return (cx - r, cy - r, cx + r, cy + r, kind)
    if kind in ("line", "rectangle"):
        pts = [(_num(params, "x1"), _num(params, "y1")), (_num(params, "x2"), _num(params, "y2"))]
    elif kind == "spline":
        flat = params.get("points") or []
        pts = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
    else:
        return None
    if not pts or any(not isinstance(c, (int, float)) for p in pts for c in p):
        return None
    us, vs = [p[0] for p in pts], [p[1] for p in pts]
    return (min(us), min(vs), max(us), max(vs), kind)


def _regions(entities):
    """Profile regions as (label, (umin, vmin, umax, vmax), kind): each closed entity on its own,
    and the open chain entities (lines, arcs, splines) together as one outline."""
    out, chain = [], None
    for i, params in enumerate(entities):
        box = _entity_box(params)
        if box is None:
            continue
        if box[4] in ("circle", "rectangle", "ellipse"):
            out.append(("profile entity %d (%s)" % (i, box[4]), box[:4], box[4]))
        else:
            chain = box[:4] if chain is None else (min(chain[0], box[0]), min(chain[1], box[1]),
                                                   max(chain[2], box[2]), max(chain[3], box[3]))
    if chain is not None:
        out.append(("the line/arc outline", chain, "chain"))
    return out


def _prism(fr, uv, near, far):
    """Model-space box of the uv rectangle swept along the frame normal from `near` to `far`."""
    pts = []
    for u in (uv[0], uv[2]):
        for v in (uv[1], uv[3]):
            p = _to_model(fr, u, v)
            for t in (near, far):
                pts.append(tuple(p[i] + t * fr[3][i] for i in range(3)))
    return (tuple(min(p[i] for p in pts) for i in range(3)), tuple(max(p[i] for p in pts) for i in range(3)))


def _overlaps(a, b):
    return all(min(a[1][i], b[1][i]) - max(a[0][i], b[0][i]) > _TOL for i in range(3))


def _mm(v):
    return "%g" % round(v * 1000.0, 2)


def _box_text(box):
    return " ".join("%s %s..%s" % ("XYZ"[i], _mm(box[0][i]), _mm(box[1][i])) for i in range(3))


class MaterialLog(object):
    """Boxes of the solids a graph has built so far, fed op by op by the compiler."""

    def __init__(self, nodes=()):
        self.solids = []        # {"node", "box", "planar"}
        self.unmodelled = []    # "'id' (type)"
        self.frame = None
        self.entities = []
        self.pending = None     # (tool, params, node id) of the op being sent
        self._cut_nodes = {n.get("id") for n in nodes if isinstance(n, dict)
                           and (n.get("type") == "hole" or n.get("operation") == "cut")}

    def sending(self, tool, params, nid):
        self.pending = (tool, dict(params or {}), nid)

    def completed(self, tool, params, resp, node):
        nid, ntype = node.get("id"), node.get("type")
        params = params or {}
        if tool == "create_sketch":
            self.frame = _frame((resp.get("result_geometry") or {}).get("frame"))
            self.entities = []
        elif tool == "add_sketch_entity":
            self.entities.append(dict(params))
        elif tool == "extrude_feature":
            kind = params.get("feature_type")
            if kind == "boss":
                self._add_boss(nid, params)
            elif kind == "revolve" and not params.get("is_cut"):
                self._add_revolve(nid, params)
            elif kind in ("sweep", "loft"):
                self.unmodelled.append("'%s' (%s)" % (nid, kind))
        elif tool in _PATTERN_TOOLS:
            seeds = node.get("features") or ([node["feature"]] if node.get("feature") else [])
            if not seeds or any(s not in self._cut_nodes for s in seeds):
                self.unmodelled.append("'%s' (%s)" % (nid, ntype))
        elif tool in _UNMODELLED_TOOLS:
            self.unmodelled.append("'%s' (%s)" % (nid, ntype))

    def _sweep_range(self, params):
        """(near, far) along the frame normal for an extrude's end condition, or None."""
        if params.get("up_to_face_index") is not None:
            return None
        if params.get("through"):
            depth = _THROUGH
        else:
            depth = _num(params, "depth")
            if depth is None:
                return None
        if params.get("mid_plane"):
            return (-depth / 2.0, depth / 2.0)
        return (-depth, 0.0) if params.get("reverse") else (0.0, depth)

    def _add_boss(self, nid, params):
        span = self._sweep_range(params) if self.frame else None
        regions = _regions(self.entities) if span else []
        if not regions:
            self.unmodelled.append("'%s' (boss)" % nid)
            return
        for _label, uv, kind in regions:
            # Only a rectangle sweeps to a box whose faces are real planar faces of the part.
            self.solids.append({"node": nid, "box": _prism(self.frame, uv, *span),
                                "planar": kind == "rectangle" and len(regions) == 1})

    def _add_revolve(self, nid, params):
        fr = self.frame
        regions = _regions(self.entities) if fr else []
        axis = [_num(params, k) for k in ("axis_x1", "axis_y1", "axis_x2", "axis_y2")]
        if not regions or None in axis or (axis[0], axis[1]) == (axis[2], axis[3]):
            self.unmodelled.append("'%s' (revolve)" % nid)
            return
        a0, a1 = _to_model(fr, axis[0], axis[1]), _to_model(fr, axis[2], axis[3])
        d = [a1[i] - a0[i] for i in range(3)]
        ln = math.sqrt(sum(c * c for c in d))
        d = [c / ln for c in d]
        ts, radius = [], 0.0
        for _label, uv, _kind in regions:
            for u in (uv[0], uv[2]):
                for v in (uv[1], uv[3]):
                    p = _to_model(fr, u, v)
                    rel = [p[i] - a0[i] for i in range(3)]
                    t = sum(rel[i] * d[i] for i in range(3))
                    ts.append(t)
                    radius = max(radius, math.sqrt(max(sum(c * c for c in rel) - t * t, 0.0)))
        ends = [[a0[i] + t * d[i] for i in range(3)] for t in (min(ts), max(ts))]
        grow = [radius * math.sqrt(max(1.0 - d[i] * d[i], 0.0)) for i in range(3)]
        box = (tuple(min(e[i] for e in ends) - grow[i] for i in range(3)),
               tuple(max(e[i] for e in ends) + grow[i] for i in range(3)))
        self.solids.append({"node": nid, "box": box, "planar": False})

    def explain(self):
        """Text to append to an EXTRUSION_FAILED message ('' when there is nothing sound to say)."""
        if not self.pending or self.pending[0] != "extrude_feature" or self.frame is None:
            return ""
        params = self.pending[1]
        kind = params.get("feature_type")
        if kind not in ("cut", "boss"):
            return ""
        regions = _regions(self.entities)
        if not regions:
            return ""
        fr = self.frame
        lines = [" Where it failed (model mm, from the compiler's own record): the sketch plane "
                 "passes through (%s) with normal (%s)." % (", ".join(_mm(c) for c in fr[0]),
                                                          ", ".join("%g" % (round(c, 3) + 0.0) for c in fr[3]))]
        if self.solids:
            shown = ["'%s' %s" % (s["node"], _box_text(s["box"])) for s in self.solids[:5]]
            if len(self.solids) > 5:
                shown.append("+%d more" % (len(self.solids) - 5))
            lines.append(" Solid so far (bounding boxes): %s." % "; ".join(shown))
        else:
            lines.append(" No solid had been built before this feature.")
        caveat = ""
        if self.unmodelled:
            caveat = (" Not counted: material added by %s, which this record cannot model."
                      % ", ".join(self.unmodelled[:4]))
        if kind == "boss":
            span = self._sweep_range(params)
            if span:
                lines.append(" The profile sweeps to %s." % "; ".join(
                    "%s %s" % (label, _box_text(_prism(fr, uv, *span))) for label, uv, _k in regions[:4]))
            return "".join(lines) + caveat
        lines.append(" The execution layer already tried BOTH directions of this cut, so flipping "
                     "`reversed` cannot fix it.")
        span = self._sweep_range(dict(params, reverse=False, mid_plane=params.get("mid_plane")))
        if span is None:
            return "".join(lines) + caveat
        depth = span[1] - span[0]
        sweeps = [(0.0, depth), (-depth, 0.0)] if not params.get("mid_plane") else [span]
        missing, reaching = [], []
        for label, uv, k in regions:
            boxes = [_prism(fr, uv, *s) for s in sweeps]
            hits = [s["node"] for s in self.solids for b in boxes if _overlaps(b, s["box"])]
            (reaching if hits else missing).append((label, uv, k, boxes[0]))
        if missing and not reaching:
            lines.append(" None of the %d profile region(s) reaches %s in either direction: %s. "
                         "That is a placement error: check the sketch plane, its offset, and these "
                         "coordinates against the drawing." % (
                             len(missing), "the modelled solid" if self.unmodelled else "material",
                             "; ".join("%s %s" % (m[0], _box_text(m[3])) for m in missing[:4])))
        elif missing:
            lines.append(" %d of %d profile region(s) reach no material in either direction: %s." % (
                len(missing), len(regions), "; ".join("%s %s" % (m[0], _box_text(m[3])) for m in missing[:4])))
        touching = self._touching(regions)
        if touching:
            lines.append(" %s. A cut tangent to a face leaves zero-thickness geometry, which "
                         "SolidWorks refuses: move it inside, or let it cross the face." % "; ".join(touching[:4]))
        elif reaching and not missing:
            lines.append(" Every profile region does reach the solid's bounding box, so the placement "
                         "is plausible: look for material an earlier cut already removed there, or a "
                         "region that only grazes a curved face.")
        return "".join(lines) + caveat

    def _touching(self, regions):
        """Circles that touch, without crossing, an in-plane face of a box-shaped solid."""
        out = []
        n = self.frame[3]
        for label, uv, kind in regions:
            if kind != "circle":
                continue
            flat = _prism(self.frame, uv, 0.0, 0.0)
            for s in self.solids:
                if not s["planar"]:
                    continue
                for i in range(3):
                    if abs(n[i]) > 0.5:
                        continue  # the sweep axis: a face there is where the cut starts
                    for edge, side in ((flat[1][i], s["box"][1][i]), (flat[0][i], s["box"][0][i])):
                        if abs(edge - side) <= _TOUCH:
                            out.append("%s touches the face of '%s' at %s = %s mm"
                                       % (label, s["node"], "XYZ"[i], _mm(side)))
        return out
