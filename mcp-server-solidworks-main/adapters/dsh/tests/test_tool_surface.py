import asyncio
import importlib.util
from pathlib import Path

import pytest

from references import TOPICS, _recipe_sections, read_reference


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


def test_every_reference_topic_resolves_to_a_section():
    # The matcher is exact-then-leading-word; a bare substring clause used to let an unrelated
    # heading hijack a topic. Every allowlisted topic must still resolve against the recipe.
    topics = sorted(set(TOPICS) | {"feature_graph_schema"})
    for topic in topics:
        value = read_reference(topic)
        assert value is not None
        if topic != "feature_graph_schema":
            assert value.strip()


def test_reference_resolution_ignores_headings_that_merely_contain_the_topic():
    # The returned body must be the exact / leading-word section — never a heading that only
    # contains the topic name (the recipe really does have "self-verification ..." for the
    # "verification" topic, which the removed substring clause would have accepted).
    sections = _recipe_sections()
    for topic, title in TOPICS.items():
        body = read_reference(topic)
        expected = [
            text
            for heading, text in sections.items()
            if heading == title or heading.startswith(title + " ")
        ]
        assert expected, f"topic '{topic}' has no exact/leading-word section"
        assert body in expected


def test_reference_match_rejects_a_heading_that_only_contains_the_topic(
    tmp_path, monkeypatch
):
    import references

    recipe = tmp_path / "recipe-usage.md"
    recipe.write_text(
        "## reverse-mapping notes\nHijack candidate.\n\n"
        "## mapping — core mapping steps\nReal section.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(references, "RECIPE_PATH", recipe)

    assert "Real section." in references.read_reference("mapping")


def _error_code(payload):
    import json

    return json.loads(payload)["error"]["code"]


def test_json_result_maps_typed_failures_to_stable_error_codes():
    from backend import ExecutionLayerError

    server = _load_dsh_server()

    def layer_error():
        raise ExecutionLayerError("cannot reach the execution layer")

    assert _error_code(server._json_result(layer_error)) == "EXECUTION_LAYER_ERROR"

    def document_changed():
        raise RuntimeError(
            "ACTIVE_DOCUMENT_CHANGED: expected 'a.SLDPRT', but SolidWorks has 'b.SLDPRT'"
        )

    assert _error_code(server._json_result(document_changed)) == (
        "ACTIVE_DOCUMENT_CHANGED"
    )

    def backup_missing():
        raise RuntimeError("BACKUP_SOURCE_MISSING: active document path does not exist")

    assert _error_code(server._json_result(backup_missing)) == "BACKUP_SOURCE_MISSING"

    def unbound():
        raise RuntimeError("JOB_DOCUMENT_UNBOUND: this CAD job is not bound")

    assert _error_code(server._json_result(unbound)) == "JOB_DOCUMENT_UNBOUND"


def test_json_result_keeps_untyped_and_lowercase_errors_generic():
    server = _load_dsh_server()

    def plain():
        raise RuntimeError("something went sideways")

    assert _error_code(server._json_result(plain)) == "UNEXPECTED_ERROR"

    def lowercase_prefix():
        raise RuntimeError("not_a_code: just prose")

    assert _error_code(server._json_result(lowercase_prefix)) == "UNEXPECTED_ERROR"

    def type_error():
        raise TypeError("bad type")

    assert _error_code(server._json_result(type_error)) == "UNEXPECTED_ERROR"
