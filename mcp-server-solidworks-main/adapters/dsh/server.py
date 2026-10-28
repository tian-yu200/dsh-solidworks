import inspect
import json
import re
from typing import Literal

from mcp.server import MCPServer

import intake
from backend import ExecutionLayerError
from orchestrator import orchestrator


mcp = MCPServer("solidworks-cad-orchestrator")

# Orchestrator refusals and transport failures are raised as `RuntimeError("<CODE>: message")`
# so the model gets a stable, actionable error code instead of a generic UNEXPECTED_ERROR.
_ERROR_CODE_PATTERN = re.compile(r"^([A-Z][A-Z0-9_]{2,}):")


def _error_code_from(exc):
    match = _ERROR_CODE_PATTERN.match(str(exc))
    return match.group(1) if match else None


def _error_payload(exc):
    """One failure envelope for any tool: same codes, same shape, as `_json_result` always sent.

    Check order is load-bearing: json.JSONDecodeError IS a ValueError, and a bad JSON argument must
    stay INVALID_JSON rather than falling through to INVALID_REQUEST."""
    if isinstance(exc, json.JSONDecodeError):
        return {
            "status": "failed",
            "risk": "read",
            "error": {"code": "INVALID_JSON", "message": str(exc)},
        }
    if isinstance(exc, PermissionError):
        return {
            "status": "failed",
            "risk": "read",
            "error": {"code": "PERMISSION_DENIED", "message": str(exc)},
        }
    if isinstance(exc, ExecutionLayerError):
        return {
            "status": "failed",
            "risk": "read",
            "error": {"code": "EXECUTION_LAYER_ERROR", "message": str(exc)},
        }
    if isinstance(exc, ValueError):
        return {
            "status": "failed",
            "risk": "read",
            "error": {"code": "INVALID_REQUEST", "message": str(exc)},
        }
    if isinstance(exc, RuntimeError):
        code = _error_code_from(exc) or "UNEXPECTED_ERROR"
        return {
            "status": "failed",
            "risk": "read",
            "error": {"code": code, "message": str(exc)},
        }
    return {
        "status": "failed",
        "risk": "read",
        "error": {
            "code": "UNEXPECTED_ERROR",
            "message": f"{type(exc).__name__}: {exc}",
        },
    }


def _compact(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_result(func):
    """Serialize a tool result as COMPACT SINGLE-LINE JSON.

    The host elides a long tool result down to head + marker + tail, and the rule is
    budgeted in LINES as well as characters — so `indent=2` (which multiplied the line
    count ~3x) was itself part of the bug. One line of compact JSON cannot be elided for
    its length, and `ensure_ascii=False` keeps the Chinese text in the CAD payloads
    readable instead of escaping it into a much longer string.
    """
    try:
        return _compact(func())
    except Exception as exc:  # noqa: BLE001
        return _compact(_error_payload(exc))


def _image_result(func):
    """`(image, one-line JSON)` on success; the one-line JSON alone on failure.

    The JSON half is what makes a crop citable — the image block is what the model actually reads,
    and DSH only forwards it when the active model route declares image input."""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001
        return _compact(_error_payload(exc))


@mcp.tool(structured_output=False)
def start_job(
    intent: str,
    target_document: str = "",
    as_assembly: bool = False,
) -> str:
    """Start an authenticated CAD job and optionally open an existing target.

    Returns job_id and private job_token. Pass both to every later job-scoped call.
    """
    return _json_result(
        lambda: orchestrator.begin_job(intent, target_document, as_assembly)
    )

@mcp.tool(structured_output=False)
def inspect_state(
    job_id: str = "",
    job_token: str = "",
    scope: Literal[
        "state",
        "features",
        "geometry",
        "mass_properties",
        "bodies",
        "faces",
        "edges",
        "components",
        "components_flat",
        "mates",
        "drawing",
        "selection",
        "reference",
    ] = "state",
    component: str = "",
    reference_topic: Literal[
        "contract",
        "canonicalization",
        "forward",
        "mapping",
        "mapping_part",
        "mapping_sheet_metal",
        "mapping_assembly",
        "verification",
        "reverse",
        "coverage",
        "feature_graph_schema",
        "feature_graph_quickref",
        "feature_graph_changelog",
    ] = "contract",
    reference_offset: int = 0,
    reference_limit: int = 0,
) -> str:
    """Read readiness, locked document state, constrained CAD analysis, or approved references.

    Use scope=reference for recipe/schema content; it is returned ONE PAGE AT A TIME so the
    result always survives the host's tool-result elision rule. `reference_offset` is the
    0-based first line of the page and `reference_limit` an optional smaller page size (0 =
    defaults; a larger value is clamped). The page reports total_lines/total_chars, has_more
    and next_offset, and for feature_graph_schema an index of its sections and node kinds.

    PRE-BUILD READING: start with `feature_graph_quickref` (generated from the schema — every
    node type with its required/optional fields, units and a one-line meaning, 1-2 pages), then
    read only the schema sections you need by jumping with `page.index.sections[].line`. The
    full `feature_graph_schema` is 9 pages and no longer carries the changelog prose, which is
    its own topic (`feature_graph_changelog`) and is NOT needed to build.
    Deep document scopes require job credentials.
    Assembly faces/edges additionally require component.
    """
    return _json_result(
        lambda: orchestrator.inspect_state(
            job_id,
            job_token,
            scope,
            component,
            reference_topic,
            reference_offset or None,
            reference_limit or None,
        )
    )


@mcp.tool(structured_output=False)
def submit_feature_graph(
    job_id: str,
    job_token: str,
    graph_json: str,
    fresh_document: bool = True,
) -> str:
    """Compile and execute a complete Feature Graph using SolidPilot's existing compiler.

    IR distances are meters and angles are radians. Large graphs return
    pending_confirmation instead of executing immediately.

    A completed result may carry `warnings`: a non-fatal finding (today MATERIAL_NOT_FOUND)
    on work that is committed and verified — do NOT resubmit the graph or roll it back;
    fix the property afterwards with apply_document_edits.
    """
    return _json_result(
        lambda: orchestrator.feature_graph(
            job_id, job_token, graph_json, fresh_document
        )
    )


@mcp.tool(structured_output=False)
def apply_document_edits(
    job_id: str,
    job_token: str,
    operations_json: str,
) -> str:
    """Apply a strict allowlist of parametric edits to the authenticated active document.

    operations_json is an array of {"tool": ..., "params": {...}}. Supported tools:
    modify_dimension, edit_feature, set_part_material, add_edge_feature, create_pattern,
    add_reference_geometry, add_sketch_constraint, insert_toolbox_component, and
    create_exploded_view. Deletes and large batches require explicit confirmation.
    """
    return _json_result(
        lambda: orchestrator.document_edits(
            job_id, job_token, operations_json
        )
    )


@mcp.tool(structured_output=False)
def drawing_workflow(
    job_id: str,
    job_token: str,
    workflow_json: str,
) -> str:
    """Create a drawing and orchestrate production drawing operations.

    workflow_json accepts model_path, views[], flat_pattern, section_views[],
    detail_views[], model_annotations, auto_dimensions, center_marks, bom,
    auto_balloons, hole_table, and standards_check. Long workflows require
    explicit confirmation.
    """
    return _json_result(
        lambda: orchestrator.drawing_workflow(
            job_id, job_token, workflow_json
        )
    )


@mcp.tool(structured_output=False)
def save_or_export(
    job_id: str,
    job_token: str,
    operation: Literal["save", "export", "batch_export", "close"],
    file_path: str = "",
    format: Literal["", "STEP", "IGES", "STL", "PDF", "DWG", "DXF"] = "",
    formats_json: str = "[]",
    save_before_close: bool = False,
    close_all: bool = False,
) -> str:
    """Save, export, batch-export, or close through the CAD safety policy.

    Existing-file overwrite, batch export, close-all, and discarding dirty changes return
    pending_confirmation. A save that names a path for an untitled document — including one
    this job just created — or a different path for a saved document is a Save As that renames
    the open document in memory and also returns pending_confirmation (risk save_as; the
    action_summary names the document and the target path). file_path is the full target for
    save/export and the extensionless base path for batch_export.
    """
    return _json_result(
        lambda: orchestrator.save_or_export(
            job_id,
            job_token,
            operation,
            file_path,
            format,
            formats_json,
            save_before_close,
            close_all,
        )
    )


@mcp.tool(structured_output=False)
def request_confirmation(
    job_id: str,
    job_token: str,
    action_id: str,
) -> str:
    """Issue fresh one-time confirmation data for a pending high-risk action.

    Show the returned action_summary to the user before calling confirm_action.
    """
    return _json_result(
        lambda: orchestrator.request_confirmation(
            job_id, job_token, action_id
        )
    )


@mcp.tool(structured_output=False)
def confirm_action(
    job_id: str,
    job_token: str,
    action_id: str,
    confirmation_token: str,
    user_confirmation: str,
    action_summary: str,
) -> str:
    """Execute one pending high-risk action after the user explicitly approves its exact summary.

    Pass the action_id, one-time confirmation_token and exact confirmation_text returned by
    request_confirmation. Never infer approval from prior conversation.
    """
    return _json_result(
        lambda: orchestrator.confirm_action(
            job_id,
            job_token,
            action_id,
            confirmation_token,
            user_confirmation,
            action_summary,
        )
    )


@mcp.tool(structured_output=False)
def finish_job(job_id: str, job_token: str, keep_open: bool = False) -> str:
    """Run final objective verification and mark the authenticated CAD job completed.

    After a verified finish the job's own document is closed, freeing SolidWorks memory,
    when the job created or opened it, it is saved to a path and it is not dirty. Pass
    keep_open=true to leave it open. Anything left open is reported with the reason.
    """
    return _json_result(
        lambda: orchestrator.finish_job(job_id, job_token, keep_open)
    )


@mcp.tool(structured_output=False)
def read_drawing_region(image_path: str, region: str = "", zoom: int = 2):
    """Crop and enlarge part of an engineering drawing so small dimension text can be READ.

    Read-only: it touches no CAD document, needs no job, and never starts SolidWorks. It exists
    because a whole sheet inside the model's image budget renders 3 mm dimension digits as a few
    pixels, and no other approved tool can zoom.

    image_path: LOCAL image file path only (URLs including file:// are rejected).
    region: '' for the OVERVIEW — the full sheet with a labelled grid (columns A left → H right,
        rows 1 top → 6 bottom) drawn on it, so a feature can be named by cell instead of by
        guessed pixels; or a cell label like 'B2'; or a source-pixel box 'x0,y0,x1,y1'.
    zoom: integer 1–4, applied to a crop (the overview is fitted to the image budget). Use 3–4 for
        dimension text, and re-ask with a bigger zoom before calling anything illegible.

    Returns the cropped image and one line of JSON: output path, output pixels/bytes, the exact
    source-pixel box, and the grid map. Crops accumulate under artifacts/intake/<image-hash>/ for
    the session to cite. A region outside the image is rejected, never clipped: a silently clipped
    crop hides what it left out. Whatever stays illegible after zooming is UNKNOWN and goes to the
    user — do not invent a dimension, tolerance, material or standard-part value.
    """
    return _image_result(
        lambda: intake.result_payload(image_path, region, zoom)
    )


def _normalize_tool_surface():
    for tool in mcp._tool_manager.list_tools():
        if tool.description:
            tool.description = inspect.cleandoc(tool.description)
        schema = tool.parameters
        if isinstance(schema, dict):
            schema.pop("title", None)
            for prop in (schema.get("properties") or {}).values():
                if isinstance(prop, dict):
                    prop.pop("title", None)
            schema.setdefault("additionalProperties", False)


_normalize_tool_surface()


if __name__ == "__main__":
    mcp.run()
