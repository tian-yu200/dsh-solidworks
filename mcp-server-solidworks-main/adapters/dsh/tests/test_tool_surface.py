import asyncio
import importlib.util
import json
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


# ---------------------------------------------------------------------------
# Host tool-result elision survival (the PERMISSION_DENIED / unreadable-schema bug)
# ---------------------------------------------------------------------------
# The host rewrites an over-budget tool result as head + marker + tail, hiding the middle,
# and the budget is counted in LINES as well as characters. These tests pin the two
# invariants that keep the model's inputs visible under any such rule:
#   * every result is ONE line of compact JSON (so no line-based rule can bite), and
#   * the paged reference stays far under the conservative line/character budgets.

# Observed host pruner defaults (@deepseek-ai/dsh-compaction-tool-result-pruner):
# thresholdChars 8192 / headChars 4096 / tailChars 1024 — a result over thresholdChars is
# rewritten as head + "[... tool result middle pruned ...]" + tail. The conservative assumed
# line rule (> 20 lines may be elided) is kept separately.
ASSUMED_ELISION_LINES = 20
HOST_ELISION_CHARS = 8192
START_JOB_MAX_CHARS = 1024


def _begin_job_payload(cad_env, target_document=""):
    """The envelope server.start_job returns, through the real orchestrator.

    `cad_env` (conftest) supplies the FakeBackend, JobStore and monkeypatched module.
    """
    return cad_env.orchestrator.begin_job("elision regression", target_document)


def test_every_tool_result_is_a_single_line_of_compact_json():
    # indent=2 tripled the line count of every payload and is half of why a long result got
    # elided; one line of compact JSON cannot be elided by any line-counting rule.
    server = _load_dsh_server()
    payload = server._json_result(lambda: {"a": [1, 2], "b": {"c": "中文"}})
    assert "\n" not in payload
    assert "\r" not in payload
    assert ", " not in payload and '": ' not in payload  # compact separators, not indented
    assert "中文" in payload  # ensure_ascii=False keeps CJK readable (and shorter)
    assert json.loads(payload) == {"a": [1, 2], "b": {"c": "中文"}}


def test_start_job_result_keeps_job_token_in_the_head_lines(cad_env):
    server = _load_dsh_server()
    started = _begin_job_payload(cad_env)
    payload = server._json_result(lambda: started)

    # The whole result must survive the elision rule with margin: it is one line, so it
    # cannot be split at all, and it fits the character budget outright.
    assert "\n" not in payload
    assert len(payload) < START_JOB_MAX_CHARS, len(payload)
    assert len(payload) < HOST_ELISION_CHARS

    # Even under a HEAD-LINES interpretation of the rule, the token must be inside the head.
    head = "\n".join(payload.split("\n")[:ASSUMED_ELISION_LINES])
    assert started["job_token"] in head

    # job_token is the SECOND key (right after job_id) and is never null on success.
    assert list(json.loads(payload))[:2] == ["job_id", "job_token"]
    assert json.loads(payload)["job_token"] == started["job_token"]


def test_start_job_result_moves_bulky_detail_behind_inspect_state(cad_env):
    # The bulky readiness/partial_state detail is reachable via inspect_state instead of
    # riding along in the start_job result that must stay small.
    server = _load_dsh_server()
    started = _begin_job_payload(cad_env)
    payload = json.loads(server._json_result(lambda: started))
    assert payload["verification"]["detail"] == "inspect_state"
    # partial_state keeps only the small identity/version fields, not the whole CAD state
    assert set(payload["partial_state"]) <= {
        "stateVersion",
        "activeDocument",
        "activeDocumentPath",
        "documentType",
    }
    # nothing bulky leaked back in under another key
    assert len(server._json_result(lambda: started)) < START_JOB_MAX_CHARS


@pytest.mark.parametrize(
    "topic",
    sorted(set(TOPICS) | {"feature_graph_schema"}),
)
def test_every_reference_page_fits_the_elision_budget(topic):
    # Every page of every topic must fit the host's budget with margin, so no page can be
    # elided wholesale (which is what made feature_graph_schema unreadable at 498 lines).
    import references

    offset = 0
    pages = 0
    while True:
        page = references.page_reference(topic, offset, None)
        meta = page["page"]
        rendered = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        assert rendered.count("\n") + 1 == 1  # compact: still one line
        assert meta["returned_lines"] <= references.REFERENCE_PAGE_LINES
        assert meta["returned_lines"] >= 1  # paging always makes progress
        assert len(rendered) < HOST_ELISION_CHARS, (topic, len(rendered))
        assert meta["total_lines"] >= meta["returned_lines"]
        pages += 1
        if not meta["has_more"]:
            assert meta["next_offset"] is None
            break
        assert meta["next_offset"] == offset + meta["returned_lines"]
        offset = meta["next_offset"]
        assert pages < 500  # guard against a non-terminating walk
    assert pages >= 1


def test_reference_pages_stay_under_the_line_rule_with_margin():
    # The line half of the rule: every page is well under the conservative >20-line
    # threshold, so a line-counting elision cannot bite even where one page is large.
    import references

    for topic in sorted(set(TOPICS) | {"feature_graph_schema"}):
        page = references.page_reference(topic, 0, None)
        rendered_lines = (
            json.dumps(page, ensure_ascii=False, separators=(",", ":")).count("\n") + 1
        )
        assert rendered_lines == 1
        assert page["page"]["returned_lines"] <= references.REFERENCE_PAGE_LINES
        assert page["page"]["returned_lines"] <= ASSUMED_ELISION_LINES * 2


def test_single_line_longer_than_the_page_budget_is_returned_whole():
    # The schema's `_meta.status` line is ~3.4 KB and cannot be split without destroying the
    # JSON. It is returned whole (flagged line_truncated) rather than cut — correctness of the
    # reconstruction wins, and one such line still fits the host character cap.
    import references

    text = references.render_reference("feature_graph_schema")
    page = references.page_reference("feature_graph_schema", 4, None)
    assert page["page"]["returned_lines"] == 1
    assert page["page"]["line_truncated"] is True
    assert page["content"] == text.split("\n")[4]  # NOT truncated mid-line
    assert len(page["content"]) < HOST_ELISION_CHARS


def test_smaller_reference_limit_is_honoured_and_larger_is_clamped():
    import references

    small = references.page_reference("feature_graph_schema", 0, 5)
    assert small["page"]["returned_lines"] <= 5
    assert small["page"]["returned_lines"] >= 1
    assert small["page"]["next_offset"] == small["page"]["returned_lines"]
    assert small["page"]["has_more"] is True

    clamped = references.page_reference("feature_graph_schema", 0, 10 ** 6)
    assert clamped["page"]["limit"] == references.REFERENCE_PAGE_LINES
    assert clamped["page"]["returned_lines"] <= references.REFERENCE_PAGE_LINES


def test_paging_feature_graph_schema_reconstructs_the_full_content():
    # The whole point: the model can read the schema in pieces and lose nothing.
    import references

    full = references.render_reference("feature_graph_schema")
    rebuilt = []
    offset = 0
    while True:
        page = references.page_reference("feature_graph_schema", offset, None)
        rebuilt.append(page["content"])
        if not page["page"]["has_more"]:
            break
        offset = page["page"]["next_offset"]
    assert "\n".join(rebuilt) == full
    # and it really is a multi-page read, not a single lucky page
    assert len(rebuilt) > 1


def test_feature_graph_schema_page_indexes_its_sections_and_node_kinds():
    import references

    page = references.page_reference("feature_graph_schema", 0, None)
    index = page["page"]["index"]
    sections = {entry["section"]: entry for entry in index["sections"]}
    # every top-level section of the schema is addressable, with the line it starts on
    assert {"graph", "feature_types", "example"} <= set(sections)
    for entry in index["sections"]:
        assert isinstance(entry["line"], int) and entry["line"] >= 0
    # the planner can see the vocabulary WITHOUT reading the body
    kinds = index["feature_types"]
    assert "extrude" in kinds and "fillet" in kinds and "_note" not in kinds

    # the advertised line really is where that section starts
    offset = sections["feature_types"]["line"]
    page = references.page_reference("feature_graph_schema", offset, None)
    assert page["content"].split("\n")[0] == '  "feature_types": {'


def test_text_references_carry_no_schema_index():
    import references

    page = references.page_reference("contract", 0, None)
    assert "index" not in page["page"]
    assert page["page"]["has_more"] is False
    assert page["content"] == references.read_reference("contract")


def test_reference_topic_literal_still_resolves_to_every_topic():
    # The Literal on inspect_state is the model-visible allowlist; it must still resolve and
    # must still cover exactly the topics references.py serves.
    import typing

    server = _load_dsh_server()
    literal = typing.get_args(typing.get_type_hints(server.inspect_state)["reference_topic"])
    assert literal, "reference_topic Literal failed to resolve"
    assert set(literal) == set(TOPICS) | {"feature_graph_schema"}
    for topic in literal:
        assert read_reference(topic) is not None


def test_inspect_state_exposes_the_reference_paging_parameters():
    # The paging knobs must be visible to the model in the registered tool schema, and the
    # tool surface must still be exactly the nine high-level tools.
    import asyncio

    server = _load_dsh_server()
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    inspect = next(tool for tool in tools if tool.name == "inspect_state")
    properties = inspect.input_schema["properties"]
    assert "reference_offset" in properties
    assert "reference_limit" in properties
    assert "reference_topic" in properties
    # defaults stay backward compatible: offset 0 / limit 0 == "use the defaults"
    assert properties["reference_offset"].get("default") == 0
    assert properties["reference_limit"].get("default") == 0


# ---------------------------------------------------------------------------
# job_token presence (a null token reads as "your credential was reset")
# ---------------------------------------------------------------------------


def test_non_start_job_results_omit_the_job_token_key(cad_env):
    # Only start_job mints a token. Every other result used to carry `"job_token": null`,
    # which a model can misread as its credential having been reset to null — so the key is
    # omitted entirely. job_id and every other key are unchanged.
    server = _load_dsh_server()

    inspect = server._json_result(lambda: cad_env.orchestrator.inspect_state())
    assert "job_token" not in json.loads(inspect)
    assert "job_id" in json.loads(inspect)

    # a reference read is equally tokenless
    reference = server._json_result(
        lambda: cad_env.orchestrator.inspect_state(
            "", "", "reference", "", "contract"
        )
    )
    assert "job_token" not in json.loads(reference)

    # and so is the whole pending-confirmation round trip
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                {
                    "tool": "edit_feature",
                    "params": {"feature_name": "Boss-Extrude1", "action": "delete"},
                }
            ]
        ),
    )
    assert pending["status"] == "pending_confirmation"
    assert "job_token" not in pending
    assert "confirmation_token" not in pending

    confirmation = cad_env.orchestrator.request_confirmation(
        "job-1", cad_env.token, pending["action_id"]
    )
    serialized = server._json_result(lambda: confirmation)
    assert "job_token" not in json.loads(serialized)
    assert "job_id" in json.loads(serialized)


def test_start_job_still_emits_job_token_as_the_second_key(cad_env):
    server = _load_dsh_server()
    started = _begin_job_payload(cad_env)
    payload = server._json_result(lambda: started)
    parsed = json.loads(payload)
    assert "job_token" in parsed
    assert parsed["job_token"] == started["job_token"]
    assert list(parsed)[:2] == ["job_id", "job_token"]


def test_request_confirmation_token_is_within_the_first_1024_chars(cad_env):
    # The confirmation token has the same "must survive an elided head" requirement as the
    # job token: if it is buried past the head, the model cannot approve anything.
    server = _load_dsh_server()
    pending = cad_env.orchestrator.document_edits(
        "job-1",
        cad_env.token,
        json.dumps(
            [
                {
                    "tool": "edit_feature",
                    "params": {"feature_name": "Boss-Extrude1", "action": "delete"},
                }
            ]
        ),
    )
    confirmation = cad_env.orchestrator.request_confirmation(
        "job-1", cad_env.token, pending["action_id"]
    )
    serialized = server._json_result(lambda: confirmation)

    assert "\n" not in serialized  # one line: nothing can be elided in the middle
    token = confirmation["confirmation_token"]
    offset = serialized.find(token)
    assert offset >= 0, "confirmation_token missing from the serialized result"
    assert offset < 1024, (offset, len(serialized))
    assert serialized[:1024].count(token) == 1
    # the exact values the model must echo back are all in that head too
    for value in (
        confirmation["confirmation_text"],
        confirmation["action_summary"],
    ):
        assert serialized.find(value) < 1024
