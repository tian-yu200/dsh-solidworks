import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from settings import (
    ACTION_TTL_SECONDS,
    BATCH_EDIT_THRESHOLD,
    BATCH_NODE_THRESHOLD,
)


EDIT_ALLOWLIST = {
    "modify_dimension": {
        "required": {"name", "value"},
        "allowed": {"name", "value"},
    },
    "edit_feature": {
        "required": {"feature_name", "action"},
        "allowed": {"feature_name", "action", "new_name"},
    },
    "set_part_material": {
        "required": {"material_name"},
        "allowed": {"material_name", "library"},
    },
    "add_edge_feature": {
        "required": {"feature_type", "radius_or_distance"},
        "allowed": {
            "feature_type",
            "radius_or_distance",
            "ex",
            "ey",
            "ez",
            "edge_indices",
            "edges_json",
            "chamfer_type",
            "angle",
            "distance2",
            "chamfer_flip",
        },
    },
    "create_pattern": {
        "required": {"pattern_type"},
        "allowed": {
            "pattern_type",
            "feature_name",
            "spacing",
            "count",
            "direction",
            "count2",
            "spacing2",
            "flip",
            "axis_name",
            "angle",
            "equal_spacing",
            "features_json",
            "plane",
            "geometry_pattern",
        },
    },
    "add_reference_geometry": {
        "required": {"type"},
        "allowed": {
            "type",
            "ref_plane_name",
            "offset",
            "entity1_name",
            "entity1_type",
            "entity2_name",
            "entity2_type",
            "px",
            "py",
            "pz",
        },
    },
    "add_sketch_constraint": {
        "required": {"constraint_type", "px1", "py1"},
        "allowed": {
            "constraint_type",
            "px1",
            "py1",
            "px2",
            "py2",
            "entity_type1",
            "entity_type2",
        },
    },
    "insert_toolbox_component": {
        "required": {"part_number"},
        "allowed": {
            "standard",
            "part_number",
            "configuration",
            "toolbox_root",
            "x",
            "y",
            "z",
            "fixed",
        },
    },
    "create_exploded_view": {
        "required": set(),
        "allowed": {"view_name"},
    },
}

EDIT_FEATURE_ACTIONS = {"suppress", "unsuppress", "delete", "rename"}
EDGE_FEATURE_TYPES = {"fillet", "chamfer"}
CHAMFER_TYPES = {"distance_angle", "distance_distance"}
PATTERN_TYPES = {"linear", "circular", "mirror"}
PATTERN_DIRECTIONS = {"X", "Y", "Z"}
REFERENCE_TYPES = {"plane", "axis", "point"}
REFERENCE_ENTITY_TYPES = {"PLANE", "EDGE"}
SKETCH_CONSTRAINT_TYPES = {
    "horizontal",
    "vertical",
    "coincident",
    "parallel",
    "perpendicular",
    "tangent",
    "equal",
    "midpoint",
}
TWO_ENTITY_CONSTRAINTS = SKETCH_CONSTRAINT_TYPES - {"horizontal", "vertical"}
SKETCH_ENTITY_TYPES = {"SKETCHSEGMENT", "SKETCHPOINT"}
DRAWING_VIEW_TYPES = {
    "front",
    "top",
    "right",
    "isometric",
    "back",
    "bottom",
    "left",
}
DRAWING_DISPLAY_MODES = {
    "",
    "hlv",
    "hlr",
    "wireframe",
    "shaded",
    "shaded_edges",
    "default",
}
DRAWING_WORKFLOW_FIELDS = {
    "model_path",
    "views",
    "flat_pattern",
    "section_views",
    "detail_views",
    "model_annotations",
    "auto_dimensions",
    "center_marks",
    "bom",
    "auto_balloons",
    "hole_table",
    "standards_check",
}
DRAWING_VIEW_FIELDS = {
    "view_type",
    "pos_x",
    "pos_y",
    "scale",
    "model_path",
    "display_mode",
}
FLAT_PATTERN_FIELDS = {
    "pos_x",
    "pos_y",
    "scale",
    "model_path",
    "config_name",
    "hide_bend_lines",
    "flip_view",
}
SECTION_VIEW_FIELDS = {
    "edge_x",
    "edge_y",
    "x1",
    "y1",
    "x2",
    "y2",
    "px",
    "py",
    "label",
    "flip",
    "scale",
}
AUTO_DIMENSION_FIELDS = {
    "all_views",
    "include_unmarked",
    "eliminate_duplicates",
}
CENTER_MARK_FIELDS = {"include_slots", "extended_lines"}
DETAIL_VIEW_FIELDS = {
    "source_view",
    "center_x_ratio",
    "center_y_ratio",
    "radius_ratio",
    "scale",
    "label",
}
MODEL_ANNOTATION_FIELDS = {"all_views", "eliminate_duplicates"}
BOM_FIELDS = {"bom_type", "template_path"}
BALLOON_FIELDS = {"view_name", "style"}
HOLE_TABLE_FIELDS = {"view_name", "tag_style"}
STANDARDS_CHECK_FIELDS = {
    "expected_view_count",
    "require_dimensions",
    "require_resolved_references",
    "max_independent_scales",
}
BALLOON_STYLES = {
    "circular",
    "square",
    "hexagon",
    "triangle",
    "box",
    "diamond",
}
BOM_TYPES = {"top_level", "parts_only", "indented"}
HOLE_TABLE_TAG_STYLES = {"numeric", "alphanumeric"}
EXPORT_FORMAT_EXTENSIONS = {
    "STEP": ".step",
    "IGES": ".igs",
    "STL": ".stl",
    "PDF": ".pdf",
    "DWG": ".dwg",
    "DXF": ".dxf",
}
EXPORT_FORMAT_SUFFIXES = {
    "STEP": {".step", ".stp"},
    "IGES": {".igs", ".iges"},
    "STL": {".stl"},
    "PDF": {".pdf"},
    "DWG": {".dwg"},
    "DXF": {".dxf"},
}
DOCUMENT_SAVE_SUFFIXES = {
    "PART": {".sldprt"},
    "ASSEMBLY": {".sldasm"},
    "DRAWING": {".slddrw"},
}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def secret_hash(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_secret():
    return secrets.token_urlsafe(32)


def normalize_path(path):
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(path))))


def same_document(expected, actual):
    expected = expected or {}
    actual = actual or {}
    expected_path = normalize_path(expected.get("path"))
    actual_path = normalize_path(actual.get("activeDocumentPath"))
    if expected_path:
        return bool(actual_path) and expected_path == actual_path
    expected_title = (expected.get("title") or "").casefold()
    actual_title = (actual.get("activeDocument") or "").casefold()
    return not expected_title or expected_title == actual_title


def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _require_string(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")


def _require_number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if positive and value <= 0:
        raise ValueError(f"{label} must be greater than zero")


def _reject_unknown_fields(value, allowed, label):
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{label} has unsupported fields: {', '.join(sorted(unknown))}"
        )


def _json_array(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a JSON array string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must decode to an array")
    return parsed


def _validate_edit_params(index, tool, params):
    label = f"operations[{index}].params"
    if tool == "modify_dimension":
        _require_text(params["name"], f"{label}.name")
        _require_number(params["value"], f"{label}.value")
        return

    if tool == "edit_feature":
        _require_text(params["feature_name"], f"{label}.feature_name")
        action = params["action"]
        if action not in EDIT_FEATURE_ACTIONS:
            raise ValueError(
                f"{label}.action must be one of: "
                f"{', '.join(sorted(EDIT_FEATURE_ACTIONS))}"
            )
        if action == "rename":
            _require_text(params.get("new_name"), f"{label}.new_name")
        return

    if tool == "set_part_material":
        _require_text(params["material_name"], f"{label}.material_name")
        if "library" in params:
            _require_text(params["library"], f"{label}.library")
        return

    if tool == "add_edge_feature":
        feature_type = params["feature_type"]
        if feature_type not in EDGE_FEATURE_TYPES:
            raise ValueError(
                f"{label}.feature_type must be fillet or chamfer"
            )
        _require_number(
            params["radius_or_distance"],
            f"{label}.radius_or_distance",
            positive=True,
        )
        chamfer_type = params.get("chamfer_type", "distance_angle")
        if chamfer_type not in CHAMFER_TYPES:
            raise ValueError(
                f"{label}.chamfer_type must be distance_angle or "
                "distance_distance"
            )
        if feature_type == "chamfer" and chamfer_type == "distance_distance":
            _require_number(
                params.get("distance2"),
                f"{label}.distance2",
                positive=True,
            )
        if "angle" in params:
            _require_number(params["angle"], f"{label}.angle", positive=True)
            if params["angle"] >= 90:
                raise ValueError(f"{label}.angle must be less than 90 degrees")

        has_selection = False
        if params.get("edge_indices"):
            indices = _json_array(params["edge_indices"], f"{label}.edge_indices")
            if not indices or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in indices
            ):
                raise ValueError(
                    f"{label}.edge_indices must contain non-negative integers"
                )
            has_selection = True
        if params.get("edges_json") and params["edges_json"] != "[]":
            edges = _json_array(params["edges_json"], f"{label}.edges_json")
            if not edges or any(
                not isinstance(edge, dict)
                or not {"ex", "ey", "ez"} <= set(edge)
                for edge in edges
            ):
                raise ValueError(
                    f"{label}.edges_json must contain {{ex, ey, ez}} objects"
                )
            has_selection = True
        if {"ex", "ey", "ez"} <= set(params):
            for coordinate in ("ex", "ey", "ez"):
                _require_number(params[coordinate], f"{label}.{coordinate}")
            has_selection = True
        if not has_selection:
            raise ValueError(
                f"{label} requires edge_indices, edges_json, or ex/ey/ez"
            )
        return

    if tool == "create_pattern":
        pattern_type = params["pattern_type"]
        if pattern_type not in PATTERN_TYPES:
            raise ValueError(
                f"{label}.pattern_type must be linear, circular, or mirror"
            )
        if "count" in params:
            count = params["count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 2:
                raise ValueError(f"{label}.count must be an integer >= 2")
        if pattern_type in {"linear", "circular"}:
            _require_text(params.get("feature_name"), f"{label}.feature_name")
        if pattern_type == "linear":
            if params.get("direction", "X") not in PATTERN_DIRECTIONS:
                raise ValueError(f"{label}.direction must be X, Y, or Z")
            if "spacing" in params:
                _require_number(
                    params["spacing"], f"{label}.spacing", positive=True
                )
            if "count2" in params:
                count2 = params["count2"]
                if (
                    isinstance(count2, bool)
                    or not isinstance(count2, int)
                    or count2 < 1
                ):
                    raise ValueError(f"{label}.count2 must be an integer >= 1")
            if "spacing2" in params:
                _require_number(
                    params["spacing2"], f"{label}.spacing2", positive=True
                )
        elif pattern_type == "circular":
            _require_text(params.get("axis_name"), f"{label}.axis_name")
            if "angle" in params:
                _require_number(
                    params["angle"], f"{label}.angle", positive=True
                )
                if params["angle"] > 360:
                    raise ValueError(f"{label}.angle must be <= 360 degrees")
        else:
            names = []
            if params.get("features_json") and params["features_json"] != "[]":
                names = _json_array(
                    params["features_json"], f"{label}.features_json"
                )
                if not names or any(
                    not isinstance(name, str) or not name.strip()
                    for name in names
                ):
                    raise ValueError(
                        f"{label}.features_json must contain feature names"
                    )
            if not names and not (
                isinstance(params.get("feature_name"), str)
                and params["feature_name"].strip()
            ):
                raise ValueError(
                    f"{label} requires feature_name or features_json for mirror"
                )
        return

    if tool == "add_reference_geometry":
        reference_type = params["type"]
        if reference_type not in REFERENCE_TYPES:
            raise ValueError(f"{label}.type must be plane, axis, or point")
        if reference_type == "plane":
            _require_text(
                params.get("ref_plane_name"), f"{label}.ref_plane_name"
            )
            if "offset" in params:
                _require_number(params["offset"], f"{label}.offset")
        elif reference_type == "axis":
            for field in ("entity1_name", "entity2_name"):
                _require_text(params.get(field), f"{label}.{field}")
            for field in ("entity1_type", "entity2_type"):
                value = params.get(field, "PLANE")
                if value not in REFERENCE_ENTITY_TYPES:
                    raise ValueError(f"{label}.{field} must be PLANE or EDGE")
        else:
            for field in ("px", "py", "pz"):
                if field not in params:
                    raise ValueError(f"{label}.{field} is required for point")
                _require_number(params[field], f"{label}.{field}")
        return

    if tool == "add_sketch_constraint":
        constraint_type = params["constraint_type"]
        if constraint_type not in SKETCH_CONSTRAINT_TYPES:
            raise ValueError(
                f"{label}.constraint_type must be one of: "
                f"{', '.join(sorted(SKETCH_CONSTRAINT_TYPES))}"
            )
        for field in ("px1", "py1"):
            _require_number(params[field], f"{label}.{field}")
        if constraint_type in TWO_ENTITY_CONSTRAINTS:
            for field in ("px2", "py2"):
                if field not in params:
                    raise ValueError(
                        f"{label}.{field} is required for {constraint_type}"
                    )
                _require_number(params[field], f"{label}.{field}")
        for field in ("entity_type1", "entity_type2"):
            if field in params and params[field] not in SKETCH_ENTITY_TYPES:
                raise ValueError(
                    f"{label}.{field} must be SKETCHSEGMENT or SKETCHPOINT"
                )
        return

    if tool == "insert_toolbox_component":
        _require_text(params["part_number"], f"{label}.part_number")
        for field in ("standard", "configuration", "toolbox_root"):
            if field in params:
                _require_text(params[field], f"{label}.{field}")
        for field in ("x", "y", "z"):
            if field in params:
                _require_number(params[field], f"{label}.{field}")
        if "fixed" in params and not isinstance(params["fixed"], bool):
            raise ValueError(f"{label}.fixed must be a boolean")
        return

    if tool == "create_exploded_view":
        if "view_name" in params:
            _require_text(params["view_name"], f"{label}.view_name")


def validate_edits(edits):
    if not isinstance(edits, list) or not edits:
        raise ValueError("operations must be a non-empty JSON array")
    normalized = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"operations[{index}] must be an object")
        tool = edit.get("tool")
        params = edit.get("params") or {}
        rule = EDIT_ALLOWLIST.get(tool)
        if rule is None:
            raise ValueError(f"operations[{index}].tool '{tool}' is not allowed")
        if not isinstance(params, dict):
            raise ValueError(f"operations[{index}].params must be an object")
        missing = rule["required"] - set(params)
        unknown = set(params) - rule["allowed"]
        if missing:
            raise ValueError(
                f"operations[{index}] is missing: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ValueError(
                f"operations[{index}] has unsupported parameters: "
                f"{', '.join(sorted(unknown))}"
            )
        normalized_params = dict(params)
        _validate_edit_params(index, tool, normalized_params)
        normalized.append({"tool": tool, "params": normalized_params})
    return normalized


def validate_drawing_workflow(payload):
    if not isinstance(payload, dict):
        raise ValueError("workflow_json must decode to a JSON object")
    normalized = dict(payload)
    _reject_unknown_fields(
        normalized, DRAWING_WORKFLOW_FIELDS, "workflow_json"
    )
    if "model_path" in normalized:
        _require_string(normalized["model_path"], "model_path")

    views = normalized.get("views", [])
    if not isinstance(views, list):
        raise ValueError("views must be an array")
    for index, view in enumerate(views):
        label = f"views[{index}]"
        if not isinstance(view, dict):
            raise ValueError(f"{label} must be an object")
        _reject_unknown_fields(view, DRAWING_VIEW_FIELDS, label)
        if view.get("view_type") not in DRAWING_VIEW_TYPES:
            raise ValueError(
                f"{label}.view_type must be one of: "
                f"{', '.join(sorted(DRAWING_VIEW_TYPES))}"
            )
        display_mode = view.get("display_mode", "")
        if display_mode not in DRAWING_DISPLAY_MODES:
            raise ValueError(f"{label}.display_mode is not supported")
        for field in ("pos_x", "pos_y"):
            if field in view:
                _require_number(view[field], f"{label}.{field}")
        if "scale" in view:
            _require_number(view["scale"], f"{label}.scale", positive=True)
        if "model_path" in view:
            _require_string(view["model_path"], f"{label}.model_path")

    flat_pattern = normalized.get("flat_pattern")
    if flat_pattern not in (None, False):
        if not isinstance(flat_pattern, dict):
            raise ValueError("flat_pattern must be an object when enabled")
        _reject_unknown_fields(
            flat_pattern, FLAT_PATTERN_FIELDS, "flat_pattern"
        )
        for field in ("pos_x", "pos_y"):
            if field in flat_pattern:
                _require_number(
                    flat_pattern[field], f"flat_pattern.{field}"
                )
        if "scale" in flat_pattern:
            _require_number(
                flat_pattern["scale"], "flat_pattern.scale", positive=True
            )
        for field in ("model_path", "config_name"):
            if field in flat_pattern:
                _require_string(
                    flat_pattern[field], f"flat_pattern.{field}"
                )
        for field in ("hide_bend_lines", "flip_view"):
            if field in flat_pattern and not isinstance(
                flat_pattern[field], bool
            ):
                raise ValueError(f"flat_pattern.{field} must be a boolean")

    section_views = normalized.get("section_views", [])
    if not isinstance(section_views, list):
        raise ValueError("section_views must be an array")
    for index, section in enumerate(section_views):
        label = f"section_views[{index}]"
        if not isinstance(section, dict):
            raise ValueError(f"{label} must be an object")
        _reject_unknown_fields(section, SECTION_VIEW_FIELDS, label)
        for field in ("px", "py"):
            if field not in section:
                raise ValueError(f"{label}.{field} is required")
            _require_number(section[field], f"{label}.{field}")

        edge_fields = ("edge_x", "edge_y")
        line_fields = ("x1", "y1", "x2", "y2")
        has_edge = any(field in section for field in edge_fields)
        has_line = any(field in section for field in line_fields)
        if has_edge == has_line:
            raise ValueError(
                f"{label} must provide exactly one cut mode: "
                "edge_x/edge_y or x1/y1/x2/y2"
            )
        required_cut_fields = edge_fields if has_edge else line_fields
        for field in required_cut_fields:
            if field not in section:
                raise ValueError(
                    f"{label}.{field} is required for this cut mode"
                )
            _require_number(section[field], f"{label}.{field}")
        if has_line and (
            section["x1"] == section["x2"]
            and section["y1"] == section["y2"]
        ):
            raise ValueError(f"{label} cut line endpoints must differ")

        if "label" in section:
            _require_text(section["label"], f"{label}.label")
        if "flip" in section and not isinstance(section["flip"], bool):
            raise ValueError(f"{label}.flip must be a boolean")
        if "scale" in section:
            _require_number(
                section["scale"], f"{label}.scale", positive=True
            )

    detail_views = normalized.get("detail_views", [])
    if not isinstance(detail_views, list):
        raise ValueError("detail_views must be an array")
    for index, detail in enumerate(detail_views):
        label = f"detail_views[{index}]"
        if not isinstance(detail, dict):
            raise ValueError(f"{label} must be an object")
        _reject_unknown_fields(detail, DETAIL_VIEW_FIELDS, label)
        _require_text(detail.get("source_view"), f"{label}.source_view")
        for field in ("center_x_ratio", "center_y_ratio"):
            if field in detail:
                _require_number(detail[field], f"{label}.{field}")
                if not 0 <= detail[field] <= 1:
                    raise ValueError(f"{label}.{field} must be between 0 and 1")
        if "radius_ratio" in detail:
            _require_number(
                detail["radius_ratio"], f"{label}.radius_ratio", positive=True
            )
            if detail["radius_ratio"] > 0.5:
                raise ValueError(f"{label}.radius_ratio must be <= 0.5")
        if "scale" in detail:
            _require_number(detail["scale"], f"{label}.scale", positive=True)
        if "label" in detail:
            _require_text(detail["label"], f"{label}.label")

    structured_options = (
        ("auto_dimensions", AUTO_DIMENSION_FIELDS),
        ("center_marks", CENTER_MARK_FIELDS),
        ("model_annotations", MODEL_ANNOTATION_FIELDS),
    )
    for field, allowed in structured_options:
        value = normalized.get(field)
        if value in (None, False, True):
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a boolean or object")
        _reject_unknown_fields(value, allowed, field)
        for option, option_value in value.items():
            if not isinstance(option_value, bool):
                raise ValueError(f"{field}.{option} must be a boolean")

    optional_objects = (
        ("bom", BOM_FIELDS),
        ("auto_balloons", BALLOON_FIELDS),
        ("hole_table", HOLE_TABLE_FIELDS),
        ("standards_check", STANDARDS_CHECK_FIELDS),
    )
    for field, allowed in optional_objects:
        value = normalized.get(field)
        if value in (None, False, True):
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a boolean or object")
        _reject_unknown_fields(value, allowed, field)

    bom = normalized.get("bom")
    if isinstance(bom, dict):
        if bom.get("bom_type", "top_level") not in BOM_TYPES:
            raise ValueError(
                "bom.bom_type must be top_level, parts_only, or indented"
            )
        if "template_path" in bom:
            _require_text(bom["template_path"], "bom.template_path")

    balloons = normalized.get("auto_balloons")
    if isinstance(balloons, dict):
        if "view_name" in balloons:
            _require_text(balloons["view_name"], "auto_balloons.view_name")
        if balloons.get("style", "circular") not in BALLOON_STYLES:
            raise ValueError(
                "auto_balloons.style must be circular, square, hexagon, "
                "triangle, box, or diamond"
            )

    hole_table = normalized.get("hole_table")
    if isinstance(hole_table, dict):
        if "view_name" in hole_table:
            _require_text(hole_table["view_name"], "hole_table.view_name")
        if hole_table.get("tag_style", "numeric") not in HOLE_TABLE_TAG_STYLES:
            raise ValueError(
                "hole_table.tag_style must be numeric or alphanumeric"
            )

    standards = normalized.get("standards_check")
    if isinstance(standards, dict):
        for field in ("expected_view_count", "max_independent_scales"):
            if field in standards:
                value = standards[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    raise ValueError(
                        f"standards_check.{field} must be a non-negative integer"
                    )
        for field in ("require_dimensions", "require_resolved_references"):
            if field in standards and not isinstance(standards[field], bool):
                raise ValueError(
                    f"standards_check.{field} must be a boolean"
                )
    return normalized


def validate_save_or_export_payload(payload, state):
    operation = payload.get("operation")
    if operation not in {"save", "export", "batch_export", "close"}:
        raise ValueError(
            "operation must be save, export, batch_export, or close"
        )

    normalized = {
        "operation": operation,
        "file_path": payload.get("file_path", ""),
        "format": payload.get("format", ""),
        "formats_json": payload.get("formats_json", "[]"),
        "save_before_close": payload.get("save_before_close", False),
        "close_all": payload.get("close_all", False),
    }
    if not isinstance(normalized["file_path"], str):
        raise ValueError("file_path must be a string")
    if not isinstance(normalized["format"], str):
        raise ValueError("format must be a string")
    if not isinstance(normalized["formats_json"], str):
        raise ValueError("formats_json must be a JSON array string")
    if not isinstance(normalized["save_before_close"], bool):
        raise ValueError("save_before_close must be a boolean")
    if not isinstance(normalized["close_all"], bool):
        raise ValueError("close_all must be a boolean")

    file_path = normalized["file_path"].strip()
    file_format = normalized["format"].strip().upper()
    formats_json = normalized["formats_json"].strip() or "[]"
    normalized.update(
        file_path=file_path,
        format=file_format,
        formats_json=formats_json,
    )
    document_type = (state or {}).get("documentType")

    if operation == "save":
        if file_format or formats_json != "[]":
            raise ValueError("save does not accept format or formats_json")
        if normalized["save_before_close"] or normalized["close_all"]:
            raise ValueError(
                "save does not accept save_before_close or close_all"
            )
        if not file_path and not state.get("activeDocumentPath"):
            raise ValueError(
                "file_path is required for the first save of an unsaved document"
            )
        if file_path and document_type in DOCUMENT_SAVE_SUFFIXES:
            suffix = Path(file_path).suffix.lower()
            if suffix not in DOCUMENT_SAVE_SUFFIXES[document_type]:
                expected = ", ".join(
                    sorted(DOCUMENT_SAVE_SUFFIXES[document_type])
                )
                raise ValueError(
                    f"save file_path for a {document_type} document must end "
                    f"with: {expected}"
                )
    elif operation == "export":
        if not file_path:
            raise ValueError("export requires file_path")
        if file_format not in EXPORT_FORMAT_EXTENSIONS:
            raise ValueError(
                "export format must be STEP, IGES, STL, PDF, DWG, or DXF"
            )
        suffix = Path(file_path).suffix.lower()
        if suffix not in EXPORT_FORMAT_SUFFIXES[file_format]:
            expected = ", ".join(sorted(EXPORT_FORMAT_SUFFIXES[file_format]))
            raise ValueError(
                f"export file_path for {file_format} must end with: {expected}"
            )
        if file_format in {"PDF", "DWG", "DXF"} and document_type != "DRAWING":
            raise ValueError(
                f"{file_format} export requires an active DRAWING document"
            )
        if formats_json != "[]":
            raise ValueError("export does not accept formats_json")
        if normalized["save_before_close"] or normalized["close_all"]:
            raise ValueError(
                "export does not accept save_before_close or close_all"
            )
    elif operation == "batch_export":
        if not file_path:
            raise ValueError("batch_export requires an extensionless file_path base")
        if Path(file_path).suffix:
            raise ValueError(
                "batch_export file_path must be an extensionless base path"
            )
        if file_format:
            raise ValueError("batch_export does not accept format")
        if normalized["save_before_close"] or normalized["close_all"]:
            raise ValueError(
                "batch_export does not accept save_before_close or close_all"
            )
        formats = _json_array(formats_json, "formats_json")
        formats = [
            value.strip().upper() if isinstance(value, str) else value
            for value in formats
        ]
        if not formats or any(
            value not in EXPORT_FORMAT_EXTENSIONS for value in formats
        ):
            raise ValueError(
                "formats_json must be a non-empty array containing only "
                "STEP, IGES, STL, PDF, DWG, and DXF"
            )
        if len(formats) != len(set(formats)):
            raise ValueError("formats_json must not contain duplicate formats")
        if (
            any(
                file_format in {"PDF", "DWG", "DXF"}
                for file_format in formats
            )
            and document_type != "DRAWING"
        ):
            raise ValueError(
                "PDF, DWG, and DXF batch export require an active DRAWING document"
            )
        normalized["formats_json"] = canonical_json(formats)
    else:
        if file_path or file_format or formats_json != "[]":
            raise ValueError(
                "close does not accept file_path, format, or formats_json"
            )
        if normalized["close_all"] and normalized["save_before_close"]:
            raise ValueError(
                "close_all always discards unsaved changes and cannot be "
                "combined with save_before_close"
            )
        if (
            normalized["save_before_close"]
            and state
            and not state.get("activeDocumentPath")
        ):
            raise ValueError(
                "Cannot save-before-close an unsaved document without a path"
            )
    return normalized


def expected_output_paths(payload, state):
    operation = payload["operation"]
    if operation == "save":
        return [
            payload.get("file_path") or state.get("activeDocumentPath", "")
        ]
    if operation == "export":
        path = Path(payload["file_path"])
        if payload["format"] == "IGES" and path.suffix.lower() != ".igs":
            path = path.with_suffix(".igs")
        return [str(path)]
    if operation == "batch_export":
        formats = json.loads(payload["formats_json"])
        base = payload["file_path"]
        return [
            base + EXPORT_FORMAT_EXTENSIONS[file_format]
            for file_format in formats
        ]
    return []


def graph_risk(graph):
    count = len(graph.get("nodes") or [])
    return "batch" if count > BATCH_NODE_THRESHOLD else "write"


def edits_risk(edits):
    if any(
        edit["tool"] == "edit_feature"
        and edit["params"].get("action") == "delete"
        for edit in edits
    ):
        return "destructive"
    return "batch" if len(edits) > BATCH_EDIT_THRESHOLD else "write"


def action_expired(action):
    created = datetime.fromisoformat(action["created_at"])
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age > ACTION_TTL_SECONDS


def output_exists(path):
    return bool(path) and Path(path).exists()


def output_file_status(path):
    candidate = Path(path)
    try:
        is_file = candidate.is_file()
        size = candidate.stat().st_size if is_file else 0
    except OSError:
        is_file = False
        size = 0
    return {"exists": is_file, "size": size, "valid": is_file and size > 0}
