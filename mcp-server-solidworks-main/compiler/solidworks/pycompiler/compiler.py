"""Compiler orchestrator: validate -> (per node) resolve -> lower -> execute, threading
state_version through the run.

Failure model (CAD ops are NOT transactional): nodes run in declaration order; on the first
failure the run STOPS, no rollback is attempted, and the result reports how far it got plus a
feature-level error. The caller (the adapter tool) resyncs state_version afterwards either way.
One exception: a graph-level material the library positively does not contain (MATERIAL_NOT_FOUND)
is a warning, not a failure — the finished geometry is kept and the part keeps whatever material it
already had (possibly none). A present-but-not-applied material is MATERIAL_NOT_APPLIED and fails.

Public API (consumed by pycompiler/__init__.py and the adapter):
    compile_and_run(port, graph) -> CompileResult
"""
import math

from . import lowering
from . import resolver
from .errors import FeatureError
from .ir_schema import validate
from .resolver import (resolve_center_on_face, resolve_component_entity_by_anchor,
                       resolve_edges_by_anchor, resolve_face_by_anchor, resolve_face_on_plane,
                       resolve_thread_edge, resolve_top_face)

# The one execution-layer failure that does NOT invalidate the geometry a graph just built.
MATERIAL_NOT_FOUND = "MATERIAL_NOT_FOUND"


class CompileResult(object):
    """Outcome of one compile+run. Plain data so the adapter can render it however it likes."""

    def __init__(self):
        self.status = "COMPLETED"          # COMPLETED | FAILED
        self.nodes_total = 0
        self.nodes_completed = 0
        self.exec_calls = 0                # state-bumping ops actually executed
        self.node_log = []                 # [{id, type, status, ops:[{tool, note, state_version}]}]
        self.final_state_version = None
        self.error = None                  # {code, message, [node_id, node_type, step]}
        self.warnings = []                 # non-fatal findings: the run still COMPLETED

    def to_dict(self):
        return {
            "status": self.status,
            "nodes_total": self.nodes_total,
            "nodes_completed": self.nodes_completed,
            "exec_calls": self.exec_calls,
            "final_state_version": self.final_state_version,
            "error": self.error,
            "warnings": self.warnings,
            "node_log": self.node_log,
        }

    def summary(self):
        """One-line human-readable summary for the MCP result string."""
        if self.status == "COMPLETED":
            line = ("COMPLETED | IR built %d/%d nodes | exec_calls=%d"
                    % (self.nodes_completed, self.nodes_total, self.exec_calls))
            if self.warnings:
                # submit_feature_graph in the claude adapter returns ONLY this string, so a
                # warning missing here is a warning the model never sees.
                line += " | WARNINGS(%d): %s" % (
                    len(self.warnings),
                    "; ".join("%s at %s: %s" % (w.get("code"), w.get("step"), w.get("message"))
                              for w in self.warnings))
            return line
        err = self.error or {}
        if err.get("code") == "VALIDATION_FAILED":
            return "FAILED | VALIDATION_FAILED | %s | no execution performed" % err.get("message")
        node = err.get("node_id")
        step = err.get("step")
        return ("FAILED | %s | node %s (%s)%s: %s | completed %d/%d nodes before failure "
                "(partial geometry may remain — CAD ops are not transactional)"
                % (err.get("code"), node, err.get("node_type"),
                   (" step=%s" % step) if step else "", err.get("message"),
                   self.nodes_completed, self.nodes_total))


def compile_and_run(port, graph):
    """Public entry: one graph in, one CompileResult out.

    Opens the analyze-payload memo scope for THIS run and always closes it (SPEED task A) — a
    cached read must never outlive the graph that produced it, and a previous graph's reads must
    never be served here."""
    resolver.begin_run()
    try:
        return _compile_and_run(port, graph)
    finally:
        resolver.end_run()


def _compile_and_run(port, graph):
    result = CompileResult()

    # 1) Structural validation. If it fails, NO execution call is made.
    errors = validate(graph)
    if errors:
        result.status = "FAILED"
        result.error = {"code": "VALIDATION_FAILED", "message": "; ".join(errors)}
        return result

    nodes = graph["nodes"]
    result.nodes_total = len(nodes)

    # 2) Seed state_version from the authoritative source (also the basis for the post-run resync).
    try:
        sv = port.get_state()
    except Exception as ex:
        result.status = "FAILED"
        result.error = {"code": "STATE_SEED_FAILED", "message": str(ex)}
        return result

    # 3) Run nodes in order. node_features records each node's CREATED feature name (the last
    # non-empty cadState.features[0] among its ops) so later nodes can reference it by node id
    # (lowering.node_feature — e.g. a circular_pattern's seed), localization-proof.
    # BATCH 10a: off-centre holes are also collected so two holes on the SAME face whose circles
    # overlap fail BEFORE any execution call (check_holes_do_not_overlap).
    node_features = {}
    hole_placements = []
    for node in nodes:
        nid = node.get("id")
        ntype = node.get("type")
        rec = {"id": nid, "type": ntype, "status": "PENDING", "ops": []}
        try:
            ops = _plan_node(port, node, sv, node_features,
                             graph.get("thread_defaults"))  # may do a live (read-only) resolve
            if node.get("type") == "hole":
                # The overlap test is per FACE, so record which face this hole resolved to.
                # _plan_node already resolved it; re-read the cheap value it cached. EVERY
                # hole is recorded, not only the off-centre ones: a centred hole has a
                # real circle too, and two holes can collide when only one names ref.at.
                # The key is (BODY node, face index), not the bare index: face indices are
                # per-body, so two holes on DIFFERENT boxes can share index 0 and must not
                # be compared.
                body_node = ((node.get("ref") or {}).get("node_face") or {}).get("node")
                placement_face = (body_node, node.get("_resolved_face_index"))
            last_resp = None
            frame_fn = None  # 2D coord transform for this node's entities (sketch frame fix)
            frame_mirror = False
            # BATCH 10a: an off-centre hole carries its MODEL point here until create_sketch
            # echoes the measured frame; placement_uv is the resulting sketch-2D centre. Both
            # are per-NODE (one hole), reset on each node so a later node never inherits them.
            placement_at = _hole_placement_at(node)
            placement_uv = None
            for op in ops:
                params = _fill_placeholders(op, last_resp, node_features, nid, ntype)
                if frame_fn is not None and op.tool == "add_sketch_entity":
                    params = _transform_entity_params(params, frame_fn, frame_mirror)
                if op.tool == "add_sketch_entity" and placement_uv is not None:
                    # BATCH 10a: the mapped sketch-2D centre replaces the placeholder the
                    # lowering wrote (0,0) — applied HERE, before the op is sent, so the
                    # circle is drawn at the requested model point. A counterbored hole's
                    # two sketches share the same face, hence the same centre.
                    params = dict(params)
                    params["cx"], params["cy"] = placement_uv
                if params.get("edge_index") == lowering.THREAD_EDGE_PENDING:
                    # The mouth circle did not exist when this node was planned (the cut that
                    # makes it had not run yet). Resolve it NOW, against the post-cut geometry,
                    # with a read-only analyze_model. This is the ONLY place a thread edge is
                    # resolved for a `hole` node, and it happens exactly once per op: the
                    # add_cosmetic_thread tool is ADDITIVE, so a second call would create a
                    # second thread on the same edge — never re-issue this op.
                    params = dict(params)
                    anchor = lowering.thread_ref(params.pop("thread_ref", None) or {})
                    params["edge_index"], edge_info = resolver.resolve_thread_edge(
                        port, sv, anchor, node_id=nid)
                    rec.setdefault("resolved", []).append(
                        {"step": "thread_edge", "edge_index": params["edge_index"],
                         "edge_center": edge_info.get("edge_center")})
                resp = port.execute(op.tool, params, sv)
                result.exec_calls += 1
                previous_sv = sv
                sv = _advance(resp, sv, nid, ntype, op)
                if sv != previous_sv:
                    # A write advanced the document: every analyze payload read at the old
                    # state_version is stale (SPEED task A). Reads that did NOT bump
                    # state_version (analyze_* itself) stay cached.
                    resolver.invalidate(sv)
                last_resp = resp
                feats = ((resp.get("cadState") or {}).get("features")) or []
                if nid and feats and feats[0]:
                    node_features[nid] = feats[0]
                if op.tool == "create_sketch":
                    # A sketch's runtime NAME (renumbered/localized on the rebuild) lives in
                    # cadState.activeSketch, not features — record it so a later loft can reference
                    # this sketch node BY NAME (like circular_pattern's seed). Harmless for sketches
                    # consumed by an immediately-following extrude/revolve/rib (nothing reads it).
                    ask = (resp.get("cadState") or {}).get("activeSketch")
                    if nid and ask:
                        node_features[nid] = ask
                if _echoes_frame(op) and node.get("frame"):
                    # The node records the ORIGINAL sketch's frame; the response carries the
                    # frame the op just MEASURED (create_sketch — or edge_flange_sketch, whose
                    # generated profile sketch has an UNPREDICTABLE frame). Coordinates are
                    # transformed between them — correctness never depends on the support
                    # (face/plane/normal sign) or on how the API oriented the generated sketch.
                    # NOTE: the wire key is snake_case "result_geometry" (ADR-023's explicit
                    # JsonProperty), unlike the camelCase rest of the response.
                    measured = ((resp.get("result_geometry") or {}).get("frame"))
                    if not measured:
                        raise FeatureError(
                            "the node declares a sketch frame but the sketch-creating op "
                            "echoed none (execution layer too old?)",
                            code="FRAME_UNAVAILABLE", node_id=nid, node_type=ntype,
                            step=op.tool)
                    frame_fn, frame_mirror = _sketch_frame_transform(node["frame"], measured)
                if op.tool == "create_sketch" and placement_at is not None \
                        and placement_uv is None:
                    # BATCH 10a — off-centre hole: the 3-D model point becomes 2-D HERE, once
                    # the sketch's own frame is known. A face sketch's x-axis is NOT model X
                    # (a 'front' face runs +X/+Y, a 'right' face -Z/+Y), so mapping through the
                    # MEASURED frame is what makes one `ref.at` work on any face without the
                    # author reasoning about sketch axes. `placement_frame` records the frame
                    # used, so a wrong placement is diagnosable from the node log.
                    # A counterbored hole emits TWO sketches on the SAME face (counterbore, then
                    # pilot); the map is computed from the first and reused for the second, so
                    # both circles stay coaxial.
                    measured = ((resp.get("result_geometry") or {}).get("frame"))
                    if not measured:
                        raise FeatureError(
                            "ref.at needs the sketch's measured frame, but create_sketch "
                            "echoed none (execution layer too old?)",
                            code="FRAME_UNAVAILABLE", node_id=nid, node_type=ntype,
                            step=op.tool)
                    u, v, off = resolver.project_point_into_frame(measured, placement_at)
                    if abs(off) > resolver._PLANE_TOL:
                        raise FeatureError(
                            "ref.at %s is %.6f m off the sketch plane — the point must lie ON "
                            "the referenced face." % (list(placement_at), off),
                            code="PLACEMENT_OFF_FACE", node_id=nid, node_type=ntype,
                            step=op.tool)
                    placement_uv = (u, v)
                    rec.setdefault("resolved", []).append(
                        {"step": "hole_placement", "at": list(placement_at),
                         "sketch_uv": [round(u, 9), round(v, 9)],
                         "frame": measured})
                rec["ops"].append({"tool": op.tool, "note": op.note, "state_version": sv})
            rec["status"] = "COMPLETED"
            if node.get("type") == "hole":
                # D2: EVERY hole joins the cross-hole overlap test, centred ones included
                # (a centred hole still cuts a real circle, and it can collide with an
                # off-centre one). The centre used is the FACE-UV one: for ref.at that is
                # the mapped (u, v); for a centred hole it is the sketch origin (0, 0), so
                # both land in the same coordinate system on this face.
                centre_uv = placement_uv if placement_uv is not None else (0.0, 0.0)
                hole_placements.append((nid, placement_face, centre_uv,
                                        float(node["diameter"])))
                resolver.check_holes_do_not_overlap(hole_placements, node_id=nid)
            result.node_log.append(rec)
            result.nodes_completed += 1
        except FeatureError as fe:
            rec["status"] = "FAILED"
            result.node_log.append(rec)
            result.status = "FAILED"
            result.error = {
                "code": fe.code, "message": fe.message,
                "node_id": fe.node_id or nid, "node_type": ntype, "step": fe.step,
            }
            result.final_state_version = sv
            return result
        except Exception as ex:  # never let a bug crash the host; report it as a clean failure
            rec["status"] = "FAILED"
            result.node_log.append(rec)
            result.status = "FAILED"
            result.error = {"code": "NODE_UNEXPECTED", "message": str(ex),
                            "node_id": nid, "node_type": ntype}
            result.final_state_version = sv
            return result

    # 4) Graph-level material (v0.7.0) — document state, applied AFTER the geometry so a
    # material failure never aborts the build mid-tree. Part graphs only (validation enforced).
    #
    # MATERIAL_NOT_FOUND is the one material failure that says nothing about the geometry, and the
    # execution layer now reports it ONLY for a name it positively read out of the material database
    # as absent (a present-but-refused name, or a database it could not read, is MATERIAL_NOT_APPLIED
    # and stays fatal). Rolling back minutes of finished features because one NAME is not in the
    # library is the wrong trade, so that code is reported as a warning on an otherwise COMPLETED
    # run. EVERY other failure (MATERIAL_NOT_APPLIED, COM_ERROR, WRONG_DOCUMENT_TYPE, a rejected
    # state_version, an unexpected exception) keeps the previous fatal behaviour.
    material = graph.get("material")
    if material is not None:
        rec = {"id": "_material", "type": "material", "status": "PENDING", "ops": []}
        attempted = None
        resp = None
        try:
            for op in lowering.lower_material(material):
                attempted = op
                resp = port.execute(op.tool, op.params, sv)
                sv = _advance(resp, sv, "_material", "material", op)  # raises on FAILED
                # Only a call that landed counts: exec_calls is the number of state-bumping ops
                # performed, and a refused material op bumped nothing (review note).
                result.exec_calls += 1
                rec["ops"].append({"tool": op.tool, "note": op.note, "state_version": sv})
            rec["status"] = "COMPLETED"
            result.node_log.append(rec)
        except FeatureError as fe:
            if fe.code == MATERIAL_NOT_FOUND:
                on_part, on_part_db = _material_on_part(resp)
                warning = _material_not_found_warning(material, fe, on_part, on_part_db)
                rec["ops"].append({"tool": attempted.tool, "note": attempted.note,
                                   "state_version": sv, "outcome": MATERIAL_NOT_FOUND})
                rec["status"] = "WARNING"
                rec["warning"] = warning
                result.warnings.append(warning)
                result.node_log.append(rec)
            else:
                rec["status"] = "FAILED"
                result.node_log.append(rec)
                result.status = "FAILED"
                result.error = {"code": fe.code, "message": fe.message,
                                "node_id": "_material", "node_type": "material", "step": fe.step}
                result.final_state_version = sv
                return result
        except Exception as ex:  # never let a bug crash the host
            rec["status"] = "FAILED"
            result.node_log.append(rec)
            result.status = "FAILED"
            result.error = {"code": "NODE_UNEXPECTED", "message": str(ex),
                            "node_id": "_material", "node_type": "material"}
            result.final_state_version = sv
            return result

    result.final_state_version = sv
    return result


def _material_on_part(resp):
    """(material, library) the layer reports as ACTUALLY on the part after the attempt.

    The execution layer puts them in cadState.features as `material=` / `library=` on BOTH the
    completed and the failed response, because a refused apply leaves whatever was already there.
    `<none>` is the layer's explicit "nothing is set" — reported as None, never as the string."""
    features = ((resp or {}).get("cadState") or {}).get("features") or []
    found = {}
    for item in features:
        text = item if isinstance(item, str) else str(item)
        for key in ("material", "library"):
            if text.startswith(key + "=") and key not in found:
                value = text.split("=", 1)[1].strip()
                found[key] = None if value in ("", "<none>") else value
    return found.get("material"), found.get("library")


def _material_not_found_warning(material, fe, on_part=None, on_part_library=None):
    """Non-fatal report for a material the library positively does not contain.

    Says what was asked, what is ACTUALLY on the part now (which may be a material that was already
    there — the warning must never claim the part has none), and how to finish. Deliberately NO
    suggested replacement: mapping e.g. HT200 onto 'Gray Cast Iron' would invent properties the
    drawing never stated, which is worse than an unset material."""
    name = material["name"]
    library = material.get("library") or lowering.DEFAULT_MATERIAL_LIBRARY
    if on_part:
        state = ("The part still carries the material it already had — '%s' (library '%s') — which "
                 "is NOT the requested one, so mass properties and any weight check reflect THAT "
                 "material, not '%s'."
                 % (on_part, on_part_library or "unknown", name))
    else:
        state = ("The part is left with no material, so mass properties read the document default "
                 "until one is set.")
    return {
        "code": MATERIAL_NOT_FOUND,
        "step": "set_part_material",
        "material": name,
        "library": library,
        "material_on_part": on_part,
        "library_on_part": on_part_library,
        "message": ("Material '%s' is not present in library '%s', so it was NOT applied. The part "
                    "keeps every feature built by this graph. %s Layer response: %s"
                    % (name, library, state, fe.message)),
        "hint": ("Apply an EXACT name from that library (SolidWorks: right-click the document's "
                 "Material row -> Edit Material to see what it actually contains; the names are "
                 "localised per language, so use the one the dialog shows). If the specified "
                 "material is missing, add it to the library as a custom entry with the properties "
                 "the drawing states, then apply it with apply_document_edits "
                 '[{"tool":"set_part_material","params":{"material_name":"<exact name>",'
                 '"library":"%s"}}]. Do NOT substitute a similar-looking material: that would '
                 "invent properties the drawing never gave." % library),
    }


def _thread_anchor(node, face_info):
    """An EDGE anchor naming the mouth circle's CENTRE for a `hole`'s own bore.

    Two things this gets right, both learned from live failures:

    * It is an EDGE anchor, not a bore_face one. A bore_face anchor must lie ON the cylinder,
      and the entry face's representative point is on the AXIS — exactly one radius away, so
      that branch would reject it every time. An edge anchor names the mouth circle's centre,
      which IS the point we have.
    * With a COUNTERBORE the pilot's mouth is not on the entry face: the counterbore is cut
      first and the pilot starts at the counterbore FLOOR, so the mouth centre is the entry
      point pushed `counterbore.depth` INTO the material (along -entry normal). Anchoring at the
      entry face would name a circle that is no longer the pilot's mouth (and, on a blind hole,
      the resolver could then pick the pilot's BOTTOM).

    `face_info['face']['normal']` is the ENTRY NORMAL: the counterbore offset is applied along
    it, so a front/right-face hole (normal +Z / +X) would otherwise be pushed the wrong way. The
    normal is read from the resolved face rather than assumed +Y (the old assumption held only
    because the top-face selector always returns a +Y-facing face — a future front/right selector
    would have silently broken every counterbored thread anchor).
    """
    # BATCH 10a: an off-centre hole's mouth circle is centred on `ref.at`, NOT on the entry
    # face's representative point. Using the face centre here would name a circle that does not
    # exist (or, worse, a concentric rim) and the thread would fail to resolve — the failure is
    # loud (REFERENCE_UNRESOLVED), which is how this was caught.
    at = (node.get("ref") or {}).get("at")
    if ((node.get("ref") or {}).get("position") == "point"
            and isinstance(at, list) and len(at) >= 3):
        point = at
    else:
        # `resolve_top_face` returns the representative point at the TOP level; the face object
        # itself (info['face']) also carries one. Accept either shape so a caller that passes the
        # resolved FACE (as the front/right path will) gets the same anchor.
        point = (face_info or {}).get("point") or ((face_info or {}).get("face") or {}).get("point")
    if not (isinstance(point, list) and len(point) >= 3):
        return {}
    near = [float(c) for c in point[:3]]
    counterbore = node.get("counterbore")
    if counterbore and _is_pos(counterbore.get("depth")):
        # Push the anchor INTO the material along the face's own inward normal. `_entry_normal`
        # returns the face normal oriented AWAY from the body, so -n is into the material.
        n = _entry_normal(face_info)
        for axis in range(3):
            near[axis] -= n[axis] * float(counterbore["depth"])
    anchor = {"near": near}
    # Radius is a DISAMBIGUATOR, not a filter on the axis: a revolve end face can carry the
    # bore mouth AND an outer rim as concentric coplanar circles, and only the radius tells
    # them apart.
    anchor["radius"] = float(node["diameter"]) / 2.0
    return {"edge_anchor": anchor}


def _entry_normal(face_info):
    """The resolved entry face's UNIT normal, or the +Y fallback the top-face selector implies.

    A planar face's reported normal points AWAY from the material, so it is also the direction
    the bore is cut INTO. Reading it from the face keeps a counterbore offset correct on any
    face; the +Y default only applies when the face payload carries no usable normal (older
    execution layer), which is exactly the case the top-face selector produces today.
    """
    normal = ((face_info or {}).get("face") or {}).get("normal")
    if isinstance(normal, list) and len(normal) >= 3:
        try:
            v = [float(normal[0]), float(normal[1]), float(normal[2])]
        except (TypeError, ValueError):
            return (0.0, 1.0, 0.0)
        length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if length > 1e-12:
            return (v[0] / length, v[1] / length, v[2] / length)
    return (0.0, 1.0, 0.0)


def _is_pos(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value > 0)


def _plan_node(port, node, state_version, node_features, graph_thread_defaults=None):
    """Resolve + lower ONE node into ops (may do read-only analyze calls, never a write)."""
    ntype = node["type"]
    if ntype == "box":
        return lowering.lower_box(node)
    if ntype == "sketch":
        ref = node.get("ref") or {}
        face_ref = ref.get("face")
        if face_ref is not None:
            # v0.5.5: NON-axis-aligned support (a bent flange's face) — the anchor resolves to a
            # concrete face index (plane-containment, same resolver as extrude's up_to). Unlike
            # the datum/offset path this MUST resolve (there is no plane to fall back to).
            face_index, _info = resolve_face_by_anchor(port, state_version, face_ref["near"],
                                                       node_id=node.get("id"))
            return lowering.lower_sketch(node, face_index=face_index)
        offset = float(ref.get("offset") or 0.0)
        face_index = None
        if offset != 0.0:
            # Face preference (2026-07-05): sketch ON an existing face at that plane when one
            # exists — matches original authoring; a scaffolding plane only as the fallback.
            face_index = resolve_face_on_plane(port, state_version,
                                               ref.get("datum", "top"), offset,
                                               node_id=node.get("id"))
        return lowering.lower_sketch(node, face_index=face_index)
    if ntype == "extrude":
        if node.get("end") == "up_to_surface":
            near = ((node.get("up_to") or {}).get("face") or {}).get("near")
            face_index, _info = resolve_face_by_anchor(port, state_version, near,
                                                       node_id=node.get("id"))
            return lowering.lower_extrude(node, up_to_face_index=face_index)
        return lowering.lower_extrude(node)
    if ntype == "revolve":
        return lowering.lower_revolve(node)
    if ntype == "sweep":
        # The PROFILE is the active sketch (immediately preceding node); the PATH sketch's
        # runtime name flows via the node_feature sentinel (filled at execute time).
        return lowering.lower_sweep(node)
    if ntype == "rib":
        return lowering.lower_rib(node)
    if ntype == "linear_pattern":
        return lowering.lower_linear_pattern(node)
    if ntype == "circular_pattern":
        return lowering.lower_circular_pattern(node)
    if ntype == "hole":
        face_index, info = resolve_top_face(port, state_version, node_id=node.get("id"))
        ref = node.get("ref") or {}
        if ref.get("position") == "point":
            # BATCH 10a — off-centre placement. Plan-time checks, all read-only:
            #   1. the point must lie ON the face's plane (check_hole_on_face);
            #   2. the CIRCLE must stay inside the part's real extent, measured on the face
            #      plane from the body bbox (check_hole_inside_extent). This replaced an
            #      equal-area-square test that ignored `at` entirely and so passed a hole
            #      hanging off the edge of a large face.
            # A rejection here fails the node with ZERO ops executed FOR IT. Note this is a
            # node-level guard, not a graph rollback: the compiler is not transactional, so
            # earlier nodes of the same graph have already run (see the graph-level note).
            resolver.check_hole_on_face(ref["at"], info.get("face") or {},
                                        float(node["diameter"]), node_id=node.get("id"))
            resolver.check_hole_inside_extent(
                ref["at"], info.get("face") or {}, float(node["diameter"]),
                resolver.body_bbox(port, state_version, node_id=node.get("id")),
                node_id=node.get("id"))
            node["_resolved_face_index"] = face_index
            center2d = (0.0, 0.0)  # placeholder: the executor overwrites cx/cy from ref.at
        else:
            center2d = resolve_center_on_face(info)
            # A CENTRED hole resolves to the same face; record it too so the D2 overlap test
            # can compare it with an off-centre hole on that face (the index is per-body, so
            # the caller keys on (body node, index)).
            node["_resolved_face_index"] = face_index
        # A threaded hole needs its mouth CIRCLE, and that circle only exists AFTER the cuts —
        # but every analyze read is read-only and a node's ops are planned before any of them
        # execute, so the edge cannot be resolved here. The lowering emits a PLACEHOLDER op and
        # the executor resolves the edge between the last cut and the thread (the loop in
        # _compile_and_run, on THREAD_EDGE_PENDING). The anchor is the hole's own mouth centre
        # (ref.at when placed, else the entry face's point) pushed in by any counterbore depth
        # along the face NORMAL, which _thread_anchor derives from the resolved face.
        return lowering.lower_hole(node, face_index, center2d,
                                   thread_edge=lowering.THREAD_EDGE_PENDING
                                   if node.get("thread") else None,
                                   defaults=graph_thread_defaults,
                                   thread_anchor=_thread_anchor(node, info))
    if ntype == "cosmetic_thread":
        # The bore already exists, so its mouth edge is resolvable NOW (read-only) — no
        # placeholder needed. Emitting exactly ONE add_cosmetic_thread is load-bearing: the tool
        # is ADDITIVE (a second call on the same edge creates a second thread), so a retry that
        # re-issued this op would silently double the annotation.
        edge_index, _info = resolve_thread_edge(port, state_version, node.get("ref") or {},
                                                node_id=node.get("id"))
        return lowering.lower_cosmetic_thread(node, edge_index, defaults=graph_thread_defaults)
    if ntype == "fillet":
        edge_indices, _info = resolve_edges_by_anchor(port, state_version, node["edges"],
                                                      node_id=node.get("id"))
        return lowering.lower_fillet(node, edge_indices)
    if ntype == "chamfer":
        edge_indices, _info = resolve_edges_by_anchor(port, state_version, node["edges"],
                                                      node_id=node.get("id"))
        return lowering.lower_chamfer(node, edge_indices)
    if ntype == "shell":
        # The faces left OPEN are anchor-resolved on the PRE-shell geometry — the shell consumes
        # them, exactly the fillet/chamfer pre-consumption anchor rule (IR-ADR-008). One
        # analyze_model(faces) read serves all anchors (resolver memo).
        taken = {}
        face_indices = []
        for k, anchor in enumerate(node.get("faces_to_remove") or []):
            idx, _info = resolve_face_by_anchor(port, state_version, anchor["near"],
                                                node_id=node.get("id"))
            if idx in taken:
                raise FeatureError(
                    "shell face anchors %d and %d resolved to the SAME face #%d — duplicate anchor"
                    % (taken[idx], k, idx),
                    code="REFERENCE_AMBIGUOUS", node_id=node.get("id"))
            taken[idx] = k
            face_indices.append(idx)
        return lowering.lower_shell(node, face_indices)
    if ntype == "sheet_metal":
        return lowering.lower_sheet_metal(node)
    if ntype == "sketched_bend":
        return lowering.lower_sketched_bend(node)
    if ntype == "edge_flange":
        # The attach edge resolves BY INDEX (fillet/chamfer's anchor resolver) — the coordinate
        # pick misses real edges (KNOWN-LIMITATIONS #6; live on 4-1: EF2's slanted edge existed
        # EXACTLY at the pick point yet SelectByID2 returned nothing).
        edge_indices, _info = resolve_edges_by_anchor(port, state_version, [node["edge"]],
                                                      node_id=node.get("id"))
        return lowering.lower_edge_flange(node, edge_indices[0])
    if ntype == "mirror":
        # Like loft's profiles: the mirrored features ran earlier, so their runtime names are
        # already in node_features. Resolve now; the tool selects by name (localization-proof).
        names = []
        for fid in node["features"]:
            nm = node_features.get(fid)
            if not nm:
                raise FeatureError("mirror feature '%s' has no recorded runtime feature name "
                                   "(it must be an earlier feature-producing node)" % fid,
                                   code="NODE_FEATURE_UNAVAILABLE", node_id=node.get("id"))
            names.append(nm)
        return lowering.lower_mirror(node, names)
    if ntype == "component":
        return lowering.lower_component(node)
    if ntype == "mate":
        # Each side: the component's RUNTIME Name2 comes from node_features (insert_component's
        # response — instance-numbered by SolidWorks, never guessed), and the COMPONENT-LOCAL
        # anchor resolves to a face/edge INDEX on that component (live read-only reads).
        sides = []
        for side in node["sides"]:
            cid = side["component"]
            comp_name = node_features.get(cid)
            if not comp_name:
                raise FeatureError("mate side references component node '%s' with no recorded "
                                   "runtime component name" % cid,
                                   code="NODE_FEATURE_UNAVAILABLE", node_id=node.get("id"))
            kind, index = resolve_component_entity_by_anchor(port, state_version, comp_name,
                                                             side["anchor"], node_id=node.get("id"))
            sides.append((comp_name, kind, index))
        return lowering.lower_mate(node, sides)
    if ntype == "loft":
        # The profile sketches ran earlier (loft comes after them in tree order), so each one's
        # runtime name is already recorded in node_features. Resolve them now; the loft selects
        # by name (localization/numbering-proof).
        names = []
        for pid in node["profiles"]:
            nm = node_features.get(pid)
            if not nm:
                raise FeatureError("loft profile '%s' has no recorded runtime sketch name "
                                   "(it must be an earlier sketch node)" % pid,
                                   code="NODE_FEATURE_UNAVAILABLE", node_id=node.get("id"))
            names.append(nm)
        return lowering.lower_loft(node, names)
    raise FeatureError("unknown node type '%s'" % ntype, code="UNKNOWN_NODE_TYPE", node_id=node.get("id"))


def _fill_placeholders(op, last_resp, node_features, nid, ntype):
    """Substitute runtime-name sentinels in op params (returns a copy only when needed):
      - FROM_PREV_FEATURE -> the PREVIOUS op's created feature name (cadState.features[0]) —
        how an offset plane's / pattern axis's RUNTIME name (localized, 'Düzlem1'/'Eksen1')
        reaches the very next op;
      - node_feature(id) -> the feature name recorded when node <id> executed — how a
        circular_pattern references its seed feature across nodes."""
    def _fill(v):
        if v == lowering.FROM_PREV_FEATURE:
            feats = ((last_resp or {}).get("cadState") or {}).get("features") or []
            if not feats or not feats[0]:
                raise FeatureError(
                    "op '%s' needs the previous op's created feature name, but the previous "
                    "response carried none" % op.tool,
                    code="PREV_FEATURE_UNAVAILABLE", node_id=nid, node_type=ntype, step=op.tool)
            return feats[0]
        ref = lowering.parse_node_feature(v)
        if ref is not None:
            name = node_features.get(ref)
            if not name:
                raise FeatureError(
                    "op '%s' references node '%s's created feature, but no feature name was "
                    "recorded for it" % (op.tool, ref),
                    code="NODE_FEATURE_UNAVAILABLE", node_id=nid, node_type=ntype, step=op.tool)
            return name
        return v

    if not any(v == lowering.FROM_PREV_FEATURE or lowering.parse_node_feature(v) is not None
               for v in op.params.values()):
        return op.params
    return {k: _fill(v) for k, v in op.params.items()}


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _echoes_frame(op):
    """Ops that create/activate a sketch and echo its MEASURED frame in result_geometry:
    create_sketch, and sheet_metal_feature(edge_flange_sketch) (the generated edge-flange
    profile sketch)."""
    if op.tool == "create_sketch":
        return True
    return (op.tool == "sheet_metal_feature"
            and (op.params or {}).get("feature_type") == "edge_flange_sketch")


def _hole_placement_at(node):
    """The MODEL point of an off-centre hole, or None for the default centred placement.
    Read once per node so the executor can map it into whatever sketch frame create_sketch
    reports; validation has already guaranteed the shape (ir_schema._is_point3)."""
    ref = node.get("ref") or {}
    if node.get("type") != "hole" or ref.get("position") != "point":
        return None
    at = ref.get("at")
    return tuple(float(c) for c in at[:3])


def _sketch_frame_transform(declared, measured):
    """Build the 2D transform ORIGINAL-sketch-frame -> MEASURED-rebuild-frame.

    Both frames lie on the SAME model plane (same support plane), so a 2D point (u,v) in the
    original frame is the model point O_d + u·X_d + v·Y_d, re-expressed in the measured frame as
    ((m-O_m)·X_m, (m-O_m)·Y_m). Handles rotation, MIRROR (support normal flipped — the 2-1
    Sketch5 failure class) and origin shift in one affine map. Returns (fn(u,v)->(u',v'),
    mirrored) — fn is None when the frames already coincide; mirrored=True flips arc sweep
    senses (a mirror reverses CCW/CW)."""
    od, xd, yd = declared["origin"], declared["xdir"], declared["ydir"]
    om, xm, ym = measured["origin"], measured["xdir"], measured["ydir"]
    d_o = [od[i] - om[i] for i in range(3)]
    tu, tv = _dot3(d_o, xm), _dot3(d_o, ym)
    a, b = _dot3(xd, xm), _dot3(yd, xm)
    c, d = _dot3(xd, ym), _dot3(yd, ym)
    if (abs(tu) < 1e-9 and abs(tv) < 1e-9
            and abs(a - 1) < 1e-9 and abs(b) < 1e-9 and abs(c) < 1e-9 and abs(d - 1) < 1e-9):
        return None, False  # identical frames — no transform needed

    def fn(u, v):
        return (round(tu + a * u + b * v, 9), round(tv + c * u + d * v, 9))

    return fn, (a * d - b * c) < 0


_ENTITY_COORD_PAIRS = (("x1", "y1"), ("x2", "y2"), ("xm", "ym"), ("cx", "cy"), ("vx", "vy"))


def _transform_entity_params(params, frame_fn, mirrored):
    """Apply the sketch-frame transform to an add_sketch_entity op's 2D coordinates.
    A mirror also flips arc_center's sweep sense (direction) — radii/distances are invariant.
    Ellipse rides the coordinate pairs (centre + major/minor points all map); a spline's flat
    through-point JSON string is parsed, mapped pairwise and re-dumped (the points themselves
    define the curve, so a mirror needs no extra flip there)."""
    out = dict(params)
    for kx, ky in _ENTITY_COORD_PAIRS:
        if kx in out and ky in out:
            out[kx], out[ky] = frame_fn(out[kx], out[ky])
    if out.get("entity_type") == "spline" and "points" in out:
        flat = out["points"]  # a REAL flat number list (see lowering's spline note)
        moved = []
        for i in range(0, len(flat) - 1, 2):
            u, v = frame_fn(flat[i], flat[i + 1])
            moved.extend((u, v))
        out["points"] = moved
    if mirrored and out.get("entity_type") == "arc_center" and "direction" in out:
        out["direction"] = -out["direction"]
    return out


def _advance(resp, sv, nid, ntype, op):
    """Read the resulting state_version, or map a low-level FAILED to a feature-level error."""
    status = resp.get("status")
    if status == "COMPLETED":
        nv = resp.get("stateVersion")
        return nv if nv is not None else sv
    if status == "DUPLICATE":
        nv = resp.get("last_known_state_version")
        return nv if nv is not None else sv
    err = resp.get("error") or {}
    raise FeatureError("low-level tool '%s' failed: %s" % (op.tool, err.get("message")),
                       code=err.get("code") or "TOOL_FAILED",
                       node_id=nid, node_type=ntype, step=op.tool)
