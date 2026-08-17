import asyncio
import importlib.util
from pathlib import Path

import pytest

from references import read_reference


EXPECTED_TOOLS = {
    "start_job",
    "inspect_state",
    "submit_feature_graph",
    "apply_document_edits",
    "drawing_workflow",
    "save_or_export",
    "request_confirmation",
    "confirm_action",
    "finish_job",
}


def _load_dsh_server():
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    spec = importlib.util.spec_from_file_location("dsh_adapter_server", server_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_surface_is_exactly_the_nine_high_level_tools():
    server = _load_dsh_server()
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    confirm = next(
        tool for tool in tools if tool.name == "confirm_action"
    )
    assert "action_summary" in confirm.input_schema["required"]
    request = next(
        tool for tool in tools if tool.name == "request_confirmation"
    )
    assert "action_id" in request.input_schema["required"]


def test_reference_topics_are_allowlisted():
    with pytest.raises(ValueError, match="Unknown topic"):
        read_reference("../../arbitrary-file")
