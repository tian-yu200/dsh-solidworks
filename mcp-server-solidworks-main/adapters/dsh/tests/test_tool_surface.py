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
    "read_drawing_region",
}


def _load_dsh_server():
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    spec = importlib.util.spec_from_file_location("dsh_adapter_server", server_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_surface_is_exactly_the_ten_high_level_tools():
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
    region = next(
        tool for tool in tools if tool.name == "read_drawing_region"
    )
    # image_path is the only required argument: region '' means the overview, and the tool needs
    # no job, so nothing else may be mandatory.
    assert region.input_schema["required"] == ["image_path"]
    assert region.input_schema["properties"]["zoom"]["type"] == "integer"
    assert region.input_schema["additionalProperties"] is False


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
    # Pages may now carry up to REFERENCE_PAGE_LINES lines (~120) and ~6000 characters, because
    # results are single-line compact JSON: a LINE-counting rule can never bite, and the
    # character rule is enforced separately by the serialized cap below. What must still hold is
    # the hard bound the pager itself promises, and the serialized size.
    import references

    for topic in sorted(set(TOPICS) | set(references.GENERATED_TOPICS)):
        page = references.page_reference(topic, 0, None)
        rendered_lines = (
            json.dumps(page, ensure_ascii=False, separators=(",", ":")).count("\n") + 1
        )
        assert rendered_lines == 1
        assert page["page"]["returned_lines"] <= references.REFERENCE_PAGE_LINES
        assert references._serialized_size(page) <= references.REFERENCE_PAGE_MAX_SERIALIZED


def test_single_line_longer_than_the_page_budget_is_returned_whole():
    # A single over-budget line cannot be split without destroying the JSON, so it is returned
    # whole (flagged line_truncated) rather than cut. The changelog used to be such a line; it
    # now has its own topic, so this drives the rule directly with a synthetic oversized line
    # instead of depending on the schema's prose length.
    import references

    huge = "x" * (references.REFERENCE_PAGE_CHARS + 500)
    text = "first\n" + huge + "\nlast"
    page = references._page(text, 1, None, references.REFERENCE_PAGE_CHARS)
    assert page["page"]["returned_lines"] == 1
    assert page["page"]["line_truncated"] is True
    assert page["content"] == huge  # NOT truncated mid-line
    assert page["page"]["has_more"] is True
    assert page["page"]["next_offset"] == 2


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
    import references as _references

    literal = typing.get_args(typing.get_type_hints(server.inspect_state)["reference_topic"])
    assert literal, "reference_topic Literal failed to resolve"
    # Every topic references.py serves must be addressable, including the GENERATED ones
    # (quickref / changelog), so the model can never be told about a topic it cannot read.
    assert set(literal) == set(_references.ALL_TOPICS)
    for topic in literal:
        assert read_reference(topic) is not None


def test_inspect_state_exposes_the_reference_paging_parameters():
    # The paging knobs must be visible to the model in the registered tool schema, and the
    # tool surface must still be exactly the ten high-level tools.
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


# ---------------------------------------------------------------------------------------------
# Pre-build reading cost: the generated quickref, the changelog split out of the paged body,
# and the enlarged page budget. These are the guards for the "read the references before the
# first build" round trips, which used to cost ~24 tool calls.
# ---------------------------------------------------------------------------------------------
def _walk_pages(topic):
    """Every page of `topic`, in order."""
    import references

    pages = []
    offset = 0
    while True:
        page = references.page_reference(topic, offset, None)
        pages.append(page)
        if not page["page"]["has_more"]:
            return pages
        offset = page["page"]["next_offset"]
        assert len(pages) < 500


def test_quickref_is_generated_and_covers_every_schema_node_type():
    # Generated from the schema, so it CANNOT drift: a node type added to feature_types shows up
    # here without a second place to edit. This is the guard for that claim.
    import references

    schema = references._load_schema()
    declared = [
        name for name in (schema.get("feature_types") or {})
        if not name.startswith("_")
    ]
    assembly = [
        name for name in (schema.get("assembly_types_v06") or {})
        if not name.startswith("_")
    ]
    assert declared, "schema declares no feature types"
    quickref = references.render_reference("feature_graph_quickref")
    for name in declared + assembly:
        assert ("\n%s — " % name) in quickref, name
    # and it lists fields, with the required/optional marker
    assert "  R " in quickref and "  O " in quickref
    # every required field of every node type appears under that node type
    for name in declared:
        spec = schema["feature_types"][name]
        section = quickref.split("\n%s — " % name, 1)[1]
        for field, field_spec in spec.items():
            if field in ("description", "_note"):
                continue
            assert field in section.split("\n\n")[0] or field in section[:2000], (name, field)


def test_quickref_fits_in_the_page_budget_and_every_page_is_under_the_cap():
    import references

    pages = _walk_pages("feature_graph_quickref")
    assert 1 <= len(pages) <= references.QUICKREF_MAX_PAGES, len(pages)
    for page in pages:
        assert references._serialized_size(page) <= references.REFERENCE_PAGE_MAX_SERIALIZED


def test_every_page_of_every_topic_is_under_the_serialized_cap():
    # The cap is the real contract: the host pruner only trims results ABOVE 8192 characters,
    # so a page must stay below the smaller 7000 cap to be safe with margin.
    import references

    for topic in references.ALL_TOPICS:
        for page in _walk_pages(topic):
            size = references._serialized_size(page)
            assert size <= references.REFERENCE_PAGE_MAX_SERIALIZED, (topic, size)
            assert size < HOST_ELISION_CHARS, (topic, size)


def test_schema_body_no_longer_carries_the_changelog_prose():
    import references

    body = references.render_reference("feature_graph_schema")
    changelog = references.render_reference("feature_graph_changelog")
    # the long revision history lives in its own topic...
    assert "v0.7.1 (audit-A1)" in changelog
    assert len(changelog) > 3000
    # ...and the paged body only points at it
    assert "v0.7.1 (audit-A1)" not in body
    assert "feature_graph_changelog" in body  # the pointer the model follows
    # nothing is lost: the full status prose is recoverable from the changelog topic. Compare
    # on collapsed whitespace, since the changelog topic re-wraps it for normal paging.
    def _flat(value):
        return " ".join(str(value).split())

    assert _flat(references._load_schema()["_meta"]["status"]) in _flat(changelog)
    # the schema still parses and keeps every other _meta key
    import json

    parsed = json.loads(body)
    assert parsed["_meta"]["name"] == "feature-graph"
    assert "version" in parsed["_meta"]


def test_changelog_topic_is_reachable_and_paged_normally():
    import references

    pages = _walk_pages("feature_graph_changelog")
    assert pages
    # wrapped, so no page has to flag an unbreakable over-budget line
    assert not any(page["page"]["line_truncated"] for page in pages)


def test_default_pre_build_reading_is_a_small_number_of_calls():
    # The measured contract for the Skill's default reading: quickref + one mapping topic +
    # verification (+ the schema sections the plan actually needs). Pin an upper bound so a
    # regression that re-inflates the reading (e.g. by making the quickref huge again) fails.
    import references

    topics = ("feature_graph_quickref", "mapping_part", "verification")
    calls = sum(len(_walk_pages(topic)) for topic in topics)
    assert calls <= 6, calls


_SHELL_SHAPED_NODE = {
    # Deliberately the DENSEST field shape in the schema (shell's), not a tiny probe: four
    # more nodes of this size must still fit the paged budget (QUICKREF_MAX_PAGES).
    "description": (
        "Synthetic node type {i}. Hollow-like capability with the same field shape as shell. "
        "COVERED since v9.9.9 — grown for the headroom proof."
    ),
    "body": (
        "string — id of the earlier body-producing node whose solid is hollowed "
        "(body_producers: box/extrude/revolve/sweep)"
    ),
    "thickness": "number — meters (> 0), smaller than the thinnest section of the body",
    "faces_to_remove": (
        "array of face anchors (optional, >= 0): [{ near: [x,y,z], hint? }] — one per face "
        "left open, `near` a point ON its plane"
    ),
    "outward": (
        "boolean (optional, default false) — false grows the wall INWARD, true adds material "
        "OUTSIDE"
    ),
}


@pytest.mark.parametrize("count", [4])
def test_quickref_leaves_headroom_for_four_more_node_types(count):
    # The growth contract: FOUR more node types of the schema's densest shape must still fit
    # the QUICKREF_MAX_PAGES budget, so the quickref does not have to be re-tuned (and its
    # field prose trimmed) at every capability batch. The page SIZE caps are unchanged — the
    # page COUNT is only how many reads a planner spends. The schema is extended in memory
    # (never written back) with `count` synthetic node types and the pager is walked.
    import copy

    import references

    schema = copy.deepcopy(references._load_schema())
    for i in range(count):
        node = copy.deepcopy(_SHELL_SHAPED_NODE)
        node["description"] = node["description"].format(i=i)
        schema["feature_types"]["synthetic_node_%d" % i] = node

    original_loader = references._load_schema
    references._load_schema = lambda: schema
    try:
        pages = _walk_pages("feature_graph_quickref")
    finally:
        references._load_schema = original_loader

    assert len(pages) <= references.QUICKREF_MAX_PAGES, (
        f"{count} synthetic node types pushed the quickref to {len(pages)} pages "
        f"(budget {references.QUICKREF_MAX_PAGES})"
    )
    for page in pages:
        size = references._serialized_size(page)
        assert size <= references.REFERENCE_PAGE_MAX_SERIALIZED, size
    # and the synthetic types really are on the card (the headroom is not an empty walk)
    rendered = "\n".join(page["content"] for page in pages)
    for i in range(count):
        assert f"\nsynthetic_node_{i} — " in rendered
