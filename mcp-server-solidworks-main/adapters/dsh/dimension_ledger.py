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
    offsets = []
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
            if not any(abs(offset - existing) <= 1e-5 for existing in offsets):
                offsets.append(offset)
    return offsets


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


def evaluate_graph_ledger(graph, verification, faces_response=None):
    """Return warning dictionaries only; ledger discrepancies never veto a successful build."""
    ledger = graph.get("dimension_ledger")
    if not isinstance(ledger, list):
        return []
    responses = (verification or {}).get("deep_checks") or []
    bbox = _parse_bbox(responses)
    faces = []
    face_error = None
    if any(isinstance(d, dict) and isinstance(d.get("check"), dict)
           and d["check"].get("kind") in {"cylinder", "plane_distance"}
           for d in ledger if d.get("source_kind") != "unknown"):
        if (faces_response or {}).get("status") != "COMPLETED":
            face_error = ((faces_response or {}).get("error") or {}).get("code") or "faces_unavailable"
        else:
            for item in _features(faces_response):
                if isinstance(item, dict) and isinstance(item.get("faces"), list):
                    faces = item["faces"]
                    break
            if not faces:
                face_error = "faces_missing"

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
            else:
                tolerance = float(check.get("selector_tolerance_mm", DEFAULT_SELECTOR_TOLERANCE_MM)) / 1000.0
                a = _plane_offsets(faces, point_a, normal, tolerance)
                b = _plane_offsets(faces, point_b, normal, tolerance)
                if len(a) != 1 or len(b) != 1:
                    reason = "plane_candidate_count_%d_%d" % (len(a), len(b))
                else:
                    actual = abs(a[0] - b[0]) * 1000.0
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
