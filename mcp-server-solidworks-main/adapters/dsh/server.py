import inspect
import json
import re
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import ResourceSecurity

import intake
import ocr
import skill_resources
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
        "feature_graph_part_card",
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

    PRE-BUILD READING: read feature_graph_part_card (one compact page) and the mapping
    matching the target. Read quickref or indexed schema sections only for constructs not
    covered by the card; full schema and changelog are not a mandatory pre-build sequence.
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
    repair_evidence_json: str = "{}",
) -> str:
    """Compile and execute a complete Feature Graph using SolidPilot's existing compiler.

    IR distances are meters and angles are radians. A fresh_document=true graph executes
    immediately at any size: the approved plan authorizes it and it creates its own document.
    A graph over 10 nodes into an existing document returns pending_confirmation. While a graph is pending,
    document writes are refused with GRAPH_PENDING_CONFIRMATION until it is confirmed
    or cancelled with cancel_action.

    A failed fresh graph may be rebuilt automatically only after recovery_status=discarded: the owned
    document is absent, earlier documents are unchanged and the job is unbound. Report
    operation_target separately from active_document_after_recovery. Never edit the latter.
    L4 permits at most two repair executions after the initial attempt, persisted across jobs.
    Resubmit the full graph with repair_evidence_json (error_code matching last failure,
    cause, readback, change); second repair needs new evidence, never identical replay.
    Lock node count/ordered (type,operation), material name/library, all non-placement
    dimensions/specs/end types and intrinsic sketch shape (1e-9 m tolerance). Placement anchors, reversed,
    ref/datum/face/signed offset, frame and whole-sketch rigid motion/mirror may change.
    Inadmissible, exhausted, context-changed, in_flight or unverified recovery requests user
    confirmation; approval archives closed_by=user_override and starts a new first attempt
    at count 0, as one prompt. It does not close residual documents.
    recovery_unverified / rollback_failed prohibits AUTOMATIC retry. Undo is not yet verified.

    A completed result may carry non-fatal warnings. DIRECTION_FLIPPED means a null cut
    succeeded after opposite-direction retry: inspect that feature's location, depth and
    direction by readback AND front/top/side verification. MATERIAL_NOT_FOUND can be fixed
    later with apply_document_edits. Neither warning alone calls for graph replay/rollback.
    """
    return _json_result(
        lambda: orchestrator.feature_graph(
            job_id, job_token, graph_json, fresh_document, repair_evidence_json
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
    A failure reports recovery_status=recovery_unverified even if Undo returns COMPLETED.
    Automatic retries are prohibited; explicit continuation uses user confirmation. Blocks
    use normalized saved path or unsaved document_id, expire when absent, and can be released
    by that confirmation. Approval does not verify Undo or automatically close residual docs.
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
    pending_confirmation. The FIRST save of the part this job created (proved by its stable
    document_id) to a path that does not exist runs at once. Any other save that names a path
    is a Save As that renames the open document and returns pending_confirmation (risk save_as,
    summary names document and target): call request_confirmation, then confirm_action at once
    — DSH asks the user; never end the turn to ask in chat. file_path is the full target for
    save/export and the extensionless base path for batch_export.

    A queued Feature Graph blocks this and other writes until confirm_action or cancel_action.
    Jobs started without target_document bind to the active document read-only; saving,
    exporting, closing, or editing it is refused with JOB_DOCUMENT_READ_ONLY. A fresh
    document created by a confirmed Feature Graph is writable by its job.
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
def cancel_action(job_id: str, job_token: str, action_id: str) -> str:
    """Cancel a queued confirmation action without executing it.

    Use this to cancel an unconfirmed Feature Graph and release its document-write guard.
    Cancellation is final for that action; submit a new action if work is still wanted.
    """
    return _json_result(
        lambda: orchestrator.cancel_action(job_id, job_token, action_id)
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
def read_drawing_text(image_path: str) -> str:
    """Optional OCR aid after directly viewing the original drawing; not a required first step.

    image_path accepts a local file path or sha256:<64 hex> in the current DSH home.
    URLs are refused. Malformed/missing attachment objects fail once; do not guess paths.
    Returns candidate text, raw text, source-pixel boxes, grid cells, rotation and nullable
    confidence. Check OCR candidates visually before using them as EXPLICIT evidence.
    Use EXPLICIT / DERIVED (formula + source facts) / USER / UNKNOWN. Only genuinely missing
    constraints become questions; combine every missing dimension, reading conclusion and
    compact plan approval in ONE ask_user_question round. User answers unblock building;
    do not ask again about the same matter unless those answers introduce a new contradiction.
    Tool failures are infrastructure failures, not missing dimensions.
    """
    return _json_result(lambda: ocr.result_payload(image_path))


@mcp.tool(structured_output=False)
def read_drawing_region(image_path: str, region: str = "", zoom: int = 2,
                        regions: list[str] | None = None):
    """Optional crop/zoom aid after directly viewing the original drawing.

    image_path: local image path OR sha256:<64 hex> in the current DSH home; URLs refused.
    region: empty for a gridded overview, A1-H6 cell, or source-pixel x0,y0,x1,y1 box.
    zoom: integer 1-4. regions: optional 1-12 labels/boxes sharing the zoom; returns one
    image per successful region and explicit per-region errors. Read with ONE batch of
    whole-view pixel boxes at zoom 2 (front, top/side, sections, lettered views, notes):
    a dimension must stay with its dimension line and feature. Cells and zoom 3-4 are only
    for one unreadable number, boxed together with its feature.
    Input bytes/pixels, decode, output bytes/pixels, coordinates and batch size stay bounded.
    No crop quota, round cap, already-read refusal or reset argument. Host task-scoped
    no-progress detection handles repeated failed/redundant attempts without image-global state.
    Use EXPLICIT / DERIVED (formula + source constraints) / USER / UNKNOWN; never estimate
    dimensions from pixel proportions. OCR/crops are optional, not mandatory first stages.
    Combine reading conclusion, compact plan approval and ALL truly missing dimensions in
    ONE ask_user_question round. Build after the answer; ask again only for a new contradiction
    introduced by that answer. “接受/继续” is not a missing numeric value. Tool errors are
    technical failures, not missing dimensions. No CAD document or job is touched here.
    """
    if regions is not None:
        return _image_result(lambda: intake.result_batch(image_path, regions, zoom))
    return _image_result(lambda: intake.result_payload(image_path, region, zoom))


def read_skill_reference(name: str) -> str:
    """One solidworks-cad skill reference document, by bare file name.

    Resource URI: solidworks://skill/references/<file>.md — read it with read_mcp_resource
    (server "solidworks", that URI). `name` is the file name only: a path, a `..` segment or a
    drive letter is refused with SKILL_RESOURCE_TRAVERSAL_REFUSED, and a name that is not a served
    file with SKILL_RESOURCE_NOT_FOUND. The bytes come from the skill directory, which is a
    junction to the repository, so this is the same file SKILL.md links.

    SKILL.md links every reference by its exact URI; the SolidWorks preset does not expose the
    roster tool, so read only those URIs. A reference added after this server started is readable
    through this URI immediately.
    """
    return skill_resources.refuse_as_resource_error(skill_resources.read, name)


def _register_skill_resources():
    """Expose the skill's references as resources, because the preset has no file tool.

    The template's `name` parameter is exempt from the framework's own path checks on purpose: that
    check answers a `..` URI with a generic "unknown resource", which reads as a typo and invites
    retries, while the coded refusal states the rule. Containment is enforced in skill_resources
    against the resolved root instead, which is the stronger check (it also stops a symlink).
    """
    mcp.resource(
        skill_resources.TEMPLATE_URI,
        name=skill_resources.TEMPLATE_NAME,
        title="solidworks-cad skill reference",
        description=(
            "One reference document of the solidworks-cad skill, addressed by bare file name: "
            "solidworks://skill/references/<file>.md. No paths, no parent segments."
        ),
        mime_type="text/markdown",
        security=ResourceSecurity(exempt_params={"name"}),
    )(read_skill_reference)
    return skill_resources.register_on(mcp)


_register_skill_resources()


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
