import inspect
import json
from typing import Literal

from mcp.server import MCPServer

from orchestrator import orchestrator


mcp = MCPServer("solidworks-cad-orchestrator")


def _json_result(func):
    try:
        result = func()
    except json.JSONDecodeError as exc:
        result = {
            "status": "failed",
            "risk": "read",
            "error": {"code": "INVALID_JSON", "message": str(exc)},
        }
    except PermissionError as exc:
        result = {
            "status": "failed",
            "risk": "read",
            "error": {"code": "PERMISSION_DENIED", "message": str(exc)},
        }
    except ValueError as exc:
        result = {
            "status": "failed",
            "risk": "read",
            "error": {"code": "INVALID_REQUEST", "message": str(exc)},
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "status": "failed",
            "risk": "read",
            "error": {
                "code": "UNEXPECTED_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
            },
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


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
    ] = "contract",
) -> str:
    """Read readiness, locked document state, constrained CAD analysis, or approved references.

    Use scope=reference for recipe/schema content. Deep document scopes require job credentials.
    Assembly faces/edges additionally require component.
    """
    return _json_result(
        lambda: orchestrator.inspect_state(
            job_id, job_token, scope, component, reference_topic
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
    pending_confirmation. file_path is the full target for save/export and the extensionless
    base path for batch_export.
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
def finish_job(job_id: str, job_token: str) -> str:
    """Run final objective verification and mark the authenticated CAD job completed."""
    return _json_result(lambda: orchestrator.finish_job(job_id, job_token))


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
