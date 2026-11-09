"""Optional, warning-only drawing dimension ledger checks.

Ledger values are drawing millimetres (or integer counts). Geometry observations are
read from the existing analyze_model geometry/faces results; this module is pure.
"""

import json
import math


SOURCES = {"explicit", "derived", "user", "unknown"}
DIMENSION_FIELDS = {"id", "label", "value", "source_kind", "view", "node_ids", "check"}
CHECK_FIELDS = {
    "bbox_extent": {"kind", "axis", "tolerance_mm"},
    "cylinder": {"kind", "axis", "through", "tolerance_mm", "selector_tolerance_mm"},
    "plane_distance": {
        "kind", "normal", "point_a", "point_b", "tolerance_mm", "selector_tolerance_mm",
    },
}
DEFAULT_TOLERANCE_MM = 0.5
DEFAULT_SELECTOR_TOLERANCE_MM = 1.0


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _vec3(value, name):
    if not isinstance(value, list) or len(value) != 3 or not all(_number(v) for v in value):
        raise ValueError("%s must be three finite numbers" % name)
    return [float(v) for v in value]


def validate_graph_ledger(graph):
    """Return structural ledger errors without changing Feature Graph compilation."""
    ledger = graph.get("dimension_ledger")
    if ledger is None:
        return []
    if not isinstance(ledger, list):
        return ["dimension_ledger must be an array when present."]
    nodes = graph.get("nodes") or []
    node_ids = {node.get("id") for node in nodes if isinstance(node, dict)}
    seen = set()
    errors = []
    for index, item in enumerate(ledger):
        prefix = "dimension_ledger[%d]" % index
        if not isinstance(item, dict):
            errors.append("%s must be an object." % prefix)
            continue
        extra = set(item) - DIMENSION_FIELDS
        missing = DIMENSION_FIELDS - set(item)
        if extra or missing:
            errors.append("%s fields must be exactly %s (missing=%s, extra=%s)." % (
                prefix, ", ".join(sorted(DIMENSION_FIELDS)),
                ", ".join(sorted(missing)), ", ".join(sorted(extra))))
            continue
        dim_id = item.get("id")
        if not isinstance(dim_id, str) or not dim_id or dim_id in seen:
            errors.append("%s.id must be a unique non-empty string." % prefix)
            continue
        seen.add(dim_id)
        if not isinstance(item.get("label"), str) or not item["label"]:
            errors.append("%s.label must be a non-empty string." % prefix)
        if item.get("source_kind") not in SOURCES:
            errors.append("%s.source_kind must be explicit|derived|user|unknown." % prefix)
        elif item.get("source_kind") == "derived" and (
            not isinstance(item.get("label"), str) or not item["label"].strip()
        ):
            errors.append("%s derived callout must be kept in label." % prefix)
        if not isinstance(item.get("view"), str) or not item["view"]:
            errors.append("%s.view must name the drawing view that supports this dimension." % prefix)
        node_refs = item.get("node_ids")
        if (not isinstance(node_refs, list) or any(not isinstance(n, str) or n not in node_ids
                                                   for n in node_refs)):
            errors.append("%s.node_ids must reference graph nodes." % prefix)

        source = item.get("source_kind")
        node_refs = item.get("node_ids")
        if source != "unknown" and isinstance(node_refs, list) and not node_refs:
            errors.append("%s.node_ids must link each resolved dimension to graph nodes." % prefix)
        value = item.get("value")
        check = item.get("check")
        if source == "unknown":
            if value is not None or check is not None:
                errors.append("%s unknown dimensions require value=null and check=null." % prefix)
            continue
        if not _number(value):
            errors.append("%s.value must be a finite drawing value (mm or count)." % prefix)
        if source == "derived" and (not isinstance(item.get("label"), str) or not item["label"].strip()):
            errors.append("%s derived dimensions need their callout/formula in label." % prefix)
        if not isinstance(check, dict) or check.get("kind") not in CHECK_FIELDS:
            errors.append("%s.check.kind must be bbox_extent|cylinder|plane_distance." % prefix)
            continue
        check_extra = set(check) - CHECK_FIELDS[check["kind"]]
        if check_extra:
            errors.append("%s.check has unsupported field(s): %s." % (prefix, ", ".join(sorted(check_extra))))
        tolerance = check.get("tolerance_mm", DEFAULT_TOLERANCE_MM)
        if not _number(tolerance) or tolerance < 0:
            errors.append("%s.check.tolerance_mm must be nonnegative." % prefix)
        selector_tolerance = check.get("selector_tolerance_mm", DEFAULT_SELECTOR_TOLERANCE_MM)
        if not _number(selector_tolerance) or selector_tolerance <= 0:
            errors.append("%s.check.selector_tolerance_mm must be positive." % prefix)
        try:
            if check["kind"] == "bbox_extent" and check.get("axis") not in {"x", "y", "z"}:
                errors.append("%s.check.axis must be x|y|z." % prefix)
            elif check["kind"] == "cylinder":
                axis = _vec3(check.get("axis"), prefix + ".check.axis")
                through = _vec3(check.get("through"), prefix + ".check.through")
                if math.sqrt(sum(v * v for v in axis)) < 1e-9:
                    errors.append("%s.check.axis must be nonzero." % prefix)
            elif check["kind"] == "plane_distance":
                normal = _vec3(check.get("normal"), prefix + ".check.normal")
                _vec3(check.get("point_a"), prefix + ".check.point_a")
                _vec3(check.get("point_b"), prefix + ".check.point_b")
                if math.sqrt(sum(v * v for v in normal)) < 1e-9:
                    errors.append("%s.check.normal must be nonzero." % prefix)
        except ValueError as exc:
            errors.append("%s: %s." % (prefix, exc))
    return errors


def _features(response):
    state = (response or {}).get("cadState") or {}
    result = []
    for item in state.get("features") or []:
        if not isinstance(item, str):
            continue
        try:
            parsed = json.loads(item)
        except (TypeError, ValueError):
            result.append(item)
        else:
            result.append(parsed)
    return result


def _vector(value):
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(_number(v) for v in value):
        return [float(v) for v in value]
    return None


def _unit(value):
    length = math.sqrt(sum(v * v for v in value))
    return [v / length for v in value] if length > 1e-12 else None


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _readback_values(responses):
    values = {}
    for response in responses:
        for feature in _features(response):
            if isinstance(feature, str) and "=" in feature:
                key, _, value = feature.partition("=")
                values.setdefault(key.strip(), value.strip())
    return values


def _parse_bbox(responses):
    raw = _readback_values(responses).get("bbox_size")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return [float(v) * 1000.0 for v in value] if (
        isinstance(value, list) and len(value) == 3 and all(_number(v) for v in value)
    ) else None


def _plane_offsets(faces, point, normal, selector_tol_m):
    groups = []
    for face in faces:
        if not isinstance(face, dict) or face.get("planar") is not True:
            continue
        face_normal = _unit(_vector(face.get("normal")) or [])
        face_point = _vector(face.get("point"))
        if face_normal is None or face_point is None or abs(_dot(face_normal, normal)) < 0.999:
            continue
        offset = _dot(face_point, normal)
        expected_offset = _dot(point, normal)
        if abs(offset - expected_offset) <= selector_tol_m:
            group = next((entry for entry in groups if abs(offset - entry["offset"]) <= 1e-5), None)
            if group is None:
                group = {"offset": offset, "faces": []}
                groups.append(group)
            group["faces"].append(face)
    return groups


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    for index, a in enumerate(polygon):
        b = polygon[(index + 1) % len(polygon)]
        if (a[1] > y) != (b[1] > y):
            crossing = (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
            if x < crossing:
                inside = not inside
    return inside


def _segment_distance(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 1e-24:
        return math.hypot(point[0] - a[0], point[1] - a[1])
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2))
    return math.hypot(point[0] - (a[0] + t * dx), point[1] - (a[1] + t * dy))


def _basis(normal):
    reference = [1.0, 0.0, 0.0] if abs(normal[0]) < 0.8 else [0.0, 1.0, 0.0]
    u = _unit(_cross(normal, reference))
    return u, _cross(normal, u)


def _project(point, basis):
    return [_dot(point, basis[0]), _dot(point, basis[1])]


def _loop_contains(loop, point):
    if loop["kind"] == "circle":
        return math.hypot(point[0] - loop["center"][0], point[1] - loop["center"][1]) < loop["radius"]
    return _point_in_polygon(point, loop["points"])


def _loop_sample(loop):
    if loop["kind"] == "circle":
        return [loop["center"][0] + loop["radius"] * 0.999, loop["center"][1]]
    return loop["sample"]


def _loop_boundary_distance(loop, point):
    if loop["kind"] == "circle":
        return abs(math.hypot(point[0] - loop["center"][0], point[1] - loop["center"][1]) - loop["radius"])
    return min(_segment_distance(point, a, loop["points"][(i + 1) % len(loop["points"])])
               for i, a in enumerate(loop["points"]))


def _polygon_area(points):
    return abs(sum(a[0] * points[(i + 1) % len(points)][1]
                   - points[(i + 1) % len(points)][0] * a[1]
                   for i, a in enumerate(points))) / 2.0


def _polygon_sample(points):
    signed = sum(a[0] * points[(i + 1) % len(points)][1]
                 - points[(i + 1) % len(points)][0] * a[1]
                 for i, a in enumerate(points)) / 2.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length <= 1e-12:
            continue
        sign = 1.0 if signed >= 0 else -1.0
        epsilon = min(1e-7, length * 1e-5)
        sample = [(a[0] + b[0]) / 2 - sign * (b[1] - a[1]) / length * epsilon,
                  (a[1] + b[1]) / 2 + sign * (b[0] - a[0]) / length * epsilon]
        if _point_in_polygon(sample, points):
            return sample
    return None


def _polygon_loops(points, adjacency):
    graph = {node: set(items) for node, items in adjacency.items()}
    open_nodes = [node for node, items in graph.items() if len(items) < 2]
    while open_nodes:
        node = open_nodes.pop()
        if len(graph[node]) >= 2:
            continue
        for neighbor in tuple(graph[node]):
            graph[neighbor].discard(node)
            if len(graph[neighbor]) < 2:
                open_nodes.append(neighbor)
        graph[node].clear()
    graph = {node: items for node, items in graph.items() if items}
    neighbors = {
        node: sorted(items, key=lambda other: math.atan2(points[other][1] - points[node][1],
                                                          points[other][0] - points[node][0]))
        for node, items in graph.items()
    }
    unused = {(a, b) for a, items in graph.items() for b in items}
    loops = []
    while unused:
        start = next(iter(unused))
        edge = start
        polygon = []
        for _ in range(len(unused) + len(graph) + 1):
            if edge not in unused:
                if edge == start:
                    break
                return None
            unused.remove(edge)
            u, v = edge
            polygon.append(points[u])
            around = neighbors[v]
            try:
                reverse = around.index(u)
            except ValueError:
                return None
            edge = (v, around[(reverse - 1) % len(around)])
            if edge == start:
                break
        if edge != start or len(polygon) < 3:
            continue
        signed_area = sum(a[0] * polygon[(i + 1) % len(polygon)][1]
                          - polygon[(i + 1) % len(polygon)][0] * a[1]
                          for i, a in enumerate(polygon)) / 2.0
        if signed_area <= 1e-14:
            continue
        sample = _polygon_sample(polygon)
        if sample is None:
            return None
        loops.append({"kind": "polygon", "points": polygon, "area": signed_area, "sample": sample})
    return loops or None


def _arc_polyline(start, midpoint, end, max_sagitta=2e-6):
    ax, ay = midpoint[0] - start[0], midpoint[1] - start[1]
    bx, by = end[0] - start[0], end[1] - start[1]
    determinant = 2.0 * (ax * by - ay * bx)
    if abs(determinant) <= 1e-12:
        return [start, end]

    a2, b2 = ax * ax + ay * ay, bx * bx + by * by
    center = [start[0] + (a2 * by - b2 * ay) / determinant,
              start[1] + (ax * b2 - bx * a2) / determinant]
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if radius <= 1e-10:
        return [start, end]

    angles = [math.atan2(p[1] - center[1], p[0] - center[0]) for p in (start, midpoint, end)]
    tau = 2.0 * math.pi
    ccw_end = (angles[2] - angles[0]) % tau
    ccw_mid = (angles[1] - angles[0]) % tau
    sweep = ccw_end if ccw_mid <= ccw_end + 1e-9 else ccw_end - tau
    if abs(sweep) <= 1e-9:
        return [start, end]

    cosine = max(-1.0, min(1.0, 1.0 - max_sagitta / radius))
    max_step = min(math.pi / 18.0, 2.0 * math.acos(cosine))
    segments = max(1, int(math.ceil(abs(sweep) / max(max_step, 1e-4))))
    return [[center[0] + radius * math.cos(angles[0] + sweep * i / segments),
             center[1] + radius * math.sin(angles[0] + sweep * i / segments)]
            for i in range(segments + 1)]


def _plane_loops(edges, normal, offset):
    basis = _basis(normal)
    plane_epsilon = 3e-6
    snap_epsilon = 5e-6
    points, adjacency = [], {}
    segments, circles = set(), []

    def node_for(projected):
        for index, existing in enumerate(points):
            if math.hypot(projected[0] - existing[0], projected[1] - existing[1]) <= snap_epsilon:
                return index
        points.append(projected)
        return len(points) - 1

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("kind") == "circle":
            center = _vector(edge.get("center"))
            axis = _unit(_vector(edge.get("normal")) or [])
            radius = edge.get("radius")
            if (center is not None and axis is not None and _number(radius) and radius > 0
                    and abs(abs(_dot(axis, normal)) - 1.0) <= 0.001
                    and abs(_dot(center, normal) - offset) <= plane_epsilon):
                circles.append({"kind": "circle", "center": _project(center, basis),
                                "radius": float(radius), "area": math.pi * float(radius) ** 2})
            continue

        start, end = _vector(edge.get("start")), _vector(edge.get("end"))
        if start is None or end is None:
            continue
        if abs(_dot(start, normal) - offset) > plane_epsilon or abs(_dot(end, normal) - offset) > plane_epsilon:
            continue
        midpoint = _vector(edge.get("mid"))
        if midpoint is not None and abs(_dot(midpoint, normal) - offset) > plane_epsilon:
            continue
        projected_start, projected_end = _project(start, basis), _project(end, basis)
        if midpoint is None:
            polyline = [projected_start, projected_end]
        else:
            projected_mid = _project(midpoint, basis)
            if _segment_distance(projected_mid, projected_start, projected_end) <= 1e-6:
                polyline = [projected_start, projected_end]
            else:
                polyline = _arc_polyline(projected_start, projected_mid, projected_end)
        chain = [node_for(p) for p in polyline]
        for a, b in zip(chain, chain[1:]):
            if a == b:
                continue
            segment = tuple(sorted((a, b)))
            if segment in segments:
                continue
            segments.add(segment)
            adjacency.setdefault(a, []).append(b)
            adjacency.setdefault(b, []).append(a)

    loops = list(circles)
    if segments:
        polygons = _polygon_loops(points, adjacency)
        if polygons is None:
            return None
        loops.extend(polygons)
    return loops if loops else None


def _plane_region_contains(faces, edges, point, normal, offset):
    loops = _plane_loops(edges, normal, offset)
    if loops is None:
        return None
    expected_area = sum(float(face["area"]) for face in faces if _number(face.get("area")))
    if len([face for face in faces if _number(face.get("area"))]) != len(faces):
        return None

    samples = [_loop_sample(loop) for loop in loops]
    region_area = 0.0
    for index, loop in enumerate(loops):
        depth = sum(1 for other_index, other in enumerate(loops)
                    if other_index != index and _loop_contains(other, samples[index]))
        region_area += loop["area"] * (1 if depth % 2 == 0 else -1)
    area_rounding_tolerance = 0.5e-6 * len(faces)
    if abs(region_area - expected_area) > max(area_rounding_tolerance, expected_area * 1e-4):
        return None

    basis = _basis(normal)
    projected = _project(point, basis)
    if any(_loop_boundary_distance(loop, projected) <= 0.0005 + 1e-12 for loop in loops):
        return True
    return sum(1 for loop in loops if _loop_contains(loop, projected)) % 2 == 1


def _select_plane_offset(faces, edges, point, normal, selector_tol_m):
    groups = _plane_offsets(faces, point, normal, selector_tol_m)
    if not groups:
        return None, "plane_candidate_count_0"
    on_face = []
    uncertain = False
    for group in groups:
        projected = [point[i] + normal[i] * (group["offset"] - _dot(point, normal)) for i in range(3)]
        result = _plane_region_contains(group["faces"], edges, projected, normal, group["offset"])
        if result is None:
            uncertain = True
        elif result:
            on_face.append(group["offset"])
    if uncertain:
        return None, "plane_boundary_unavailable"
    if len(on_face) != 1:
        return None, "plane_candidate_count_%d" % len(on_face) if on_face else "plane_point_not_on_face"
    return on_face[0], None


def _cylinder_candidates(faces, axis, through, selector_tol_m):
    found = []
    for face in faces:
        if not isinstance(face, dict) or face.get("kind") != "cylinder":
            continue
        face_axis = _unit(_vector(face.get("axis")) or [])
        origin = _vector(face.get("origin"))
        radius = face.get("radius")
        if face_axis is None or origin is None or not _number(radius):
            continue
        if abs(_dot(axis, face_axis)) < 0.999:
            continue
        line_distance = _distance(_cross([origin[i] - through[i] for i in range(3)], axis), [0, 0, 0])
        if line_distance > selector_tol_m:
            continue
        if not any(abs(radius - old) <= 1e-6 for old in found):
            found.append(float(radius))
    return found


def evaluate_graph_ledger(graph, verification, faces_response=None, edges_response=None):
    """Return warning dictionaries only; ledger discrepancies never veto a successful build."""
    ledger = graph.get("dimension_ledger")
    if not isinstance(ledger, list):
        return []
    responses = (verification or {}).get("deep_checks") or []
    bbox = _parse_bbox(responses)
    faces = []
    face_error = None
    edges = []
    edge_error = None
    needs_faces = any(isinstance(d, dict) and isinstance(d.get("check"), dict)
                      and d["check"].get("kind") in {"cylinder", "plane_distance"}
                      for d in ledger if d.get("source_kind") != "unknown")
    needs_edges = any(isinstance(d, dict) and isinstance(d.get("check"), dict)
                      and d["check"].get("kind") == "plane_distance"
                      for d in ledger if d.get("source_kind") != "unknown")
    if needs_faces:
        if (faces_response or {}).get("status") != "COMPLETED":
            face_error = ((faces_response or {}).get("error") or {}).get("code") or "faces_unavailable"
        else:
            for item in _features(faces_response):
                if isinstance(item, dict) and isinstance(item.get("faces"), list):
                    faces = item["faces"]
                    break
            if not faces:
                face_error = "faces_missing"
    if needs_edges:
        if (edges_response or {}).get("status") != "COMPLETED":
            edge_error = ((edges_response or {}).get("error") or {}).get("code") or "edges_unavailable"
        else:
            for item in _features(edges_response):
                if isinstance(item, dict) and isinstance(item.get("edges"), list):
                    edges = item["edges"]
                    break
            if not edges:
                edge_error = "edges_missing"

    warnings = []
    for dimension in ledger:
        if not isinstance(dimension, dict):
            continue
        check = dimension.get("check")
        source = dimension.get("source_kind")
        value = dimension.get("value")
        reason, actual = None, None
        if source == "unknown" or not isinstance(check, dict):
            reason = "unresolved_or_invalid_check"
        elif check.get("kind") == "bbox_extent":
            axis = {"x": 0, "y": 1, "z": 2}.get(check.get("axis"))
            actual = bbox[axis] if bbox is not None and axis is not None else None
            if actual is None:
                reason = "bbox_unavailable"
        elif face_error:
            reason = face_error
        elif check.get("kind") == "cylinder":
            axis, through = _unit(_vector(check.get("axis")) or []), _vector(check.get("through"))
            if axis is None or through is None:
                reason = "invalid_cylinder_selector"
            else:
                tolerance = float(check.get("selector_tolerance_mm", DEFAULT_SELECTOR_TOLERANCE_MM)) / 1000.0
                candidates = _cylinder_candidates(faces, axis, through, tolerance)
                if len(candidates) != 1:
                    reason = "cylinder_candidate_count_%d" % len(candidates)
                else:
                    actual = candidates[0] * 2000.0
        elif check.get("kind") == "plane_distance":
            normal = _unit(_vector(check.get("normal")) or [])
            point_a, point_b = _vector(check.get("point_a")), _vector(check.get("point_b"))
            if normal is None or point_a is None or point_b is None:
                reason = "invalid_plane_selector"
            elif edge_error:
                reason = edge_error
            else:
                tolerance = float(check.get("selector_tolerance_mm", DEFAULT_SELECTOR_TOLERANCE_MM)) / 1000.0
                a, reason_a = _select_plane_offset(faces, edges, point_a, normal, tolerance)
                b, reason_b = _select_plane_offset(faces, edges, point_b, normal, tolerance)
                if reason_a or reason_b:
                    reason = reason_a or reason_b
                else:
                    actual = abs(a - b) * 1000.0
        else:
            reason = "unsupported_check"

        context = {"dimension_id": dimension.get("id"), "label": dimension.get("label"),
                   "view": dimension.get("view"), "node_ids": dimension.get("node_ids") or [],
                   "expected": value, "actual": actual,
                   "check": check.get("kind") if isinstance(check, dict) else None}
        if reason:
            warnings.append({"code": "LEDGER_UNMEASURED", "reason": reason, **context})
            continue
        tolerance = float(check.get("tolerance_mm", DEFAULT_TOLERANCE_MM))
        if not _number(value) or not _number(actual):
            warnings.append({"code": "LEDGER_UNMEASURED", "reason": "invalid_value_or_measurement", **context})
        elif abs(float(actual) - float(value)) > tolerance:
            warnings.append({"code": "LEDGER_MISMATCH", "tolerance_mm": tolerance, **context})
    return warnings
