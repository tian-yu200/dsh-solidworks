"""Repository-level consistency guards for the dsh-solidwork fork.

These invariants span the whole repository, not just the MCP server, so they
live one level above the adapters:

1. `.dsh/skills/solidworks-cad` is a byte-identical mirror of the install
   source `dsh-integration/presets/solidworks-cad/skills/solidworks-cad`
   (README.md: "两份内容必须保持一致").
2. SKILL.md stays within its documented budget (<= 550 lines), and every local
   reference under `references/` is linked from SKILL.md and exists in BOTH
   copies — no dead knowledge files, no dangling links.
3. The model surface registered by `adapters/dsh/server.py` is exactly the set
   the `solidworks-policy` plugin allowlists, and is exactly the documented
   twelve tools (README, docs/SolidWorks-MCP工具清单.md, SKILL Tool Boundary).
4. The `SKILL.md` frontmatter parses as YAML with a REAL YAML parser and yields a string
   `name` and a string `description`. DSH's skill loader ignores a file whose frontmatter does
   not parse (it logs "ignored: invalid YAML frontmatter" and the skill never reaches any
   catalog), and a plain scalar containing ": " is exactly how that happens — silently, in every
   mode. A stdlib-only check cannot see this class of bug.
5. The `solidworks-policy` plugin's support-tool set is the fixed approved set (no shell,
   browser, fetch, git or file-write name can appear), its `peerDependencies` admit BOTH the CLI
   core and the desktop core — otherwise dsh's profile preflight disables the row silently — and
   its own node:test suite passes.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest`
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml  # PyYAML (requirements-dev.txt): the same parser family DSH itself uses

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIRROR = os.path.join(REPO_ROOT, ".dsh", "skills", "solidworks-cad")
PRESET = os.path.join(
    REPO_ROOT, "dsh-integration", "presets", "solidworks-cad", "skills", "solidworks-cad"
)
SKILL_MAX_LINES = 550
# DSH's loader only requires a non-empty string, so this is OUR bound: the 1024-char convention a
# skill catalog is built to, kept so a future cap cannot silently drop the skill.
SKILL_MAX_DESCRIPTION = 1024

EXPECTED_TOOLS = frozenset(
    {
        "start_job",
        "inspect_state",
        "submit_feature_graph",
        "apply_document_edits",
        "drawing_workflow",
        "save_or_export",
        "request_confirmation",
        "confirm_action",
        "cancel_action",
        "finish_job",
        "read_drawing_text",
    "read_drawing_region",
    }
)

SURFACE_DRIFT_HINT = (
    "Update ALL of: adapters/dsh/server.py, "
    "dsh-integration/plugins/solidworks-policy/index.js, SKILL.md Tool Boundary, "
    "README.md 模型可见工具, docs/SolidWorks-MCP工具清单.md"
)


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _skill_files(root):
    """{relative_path: bytes} for every file under a skill copy."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root).replace("\\", "/")] = _read(full)
    return out


def test_skill_mirror_and_preset_are_identical():
    mirror = _skill_files(MIRROR)
    preset = _skill_files(PRESET)
    only_mirror = sorted(set(mirror) - set(preset))
    only_preset = sorted(set(preset) - set(mirror))
    assert not only_mirror and not only_preset, (
        f".dsh mirror and install preset have different file sets "
        f"(mirror-only={only_mirror}, preset-only={only_preset}); sync both copies"
    )
    drifted = sorted(name for name in mirror if mirror[name] != preset[name])
    assert not drifted, f"mirror/preset content differs in {drifted}; sync both copies"


def test_skill_budget_and_reference_links():
    mirror = _skill_files(MIRROR)
    preset = _skill_files(PRESET)
    for root, files in ((MIRROR, mirror), (PRESET, preset)):
        skill = files["SKILL.md"].decode("utf-8")
        lines = len(skill.splitlines())
        assert lines <= SKILL_MAX_LINES, (
            f"SKILL.md at {root} is {lines} lines (budget {SKILL_MAX_LINES}); "
            "move detail into a reference file"
        )
        linked = set(re.findall(r"references/([A-Za-z0-9_-]+\.md)", skill))
        present = {
            name[len("references/"):]
            for name in files
            if name.startswith("references/") and name.endswith(".md")
        }
        dangling = sorted(linked - present)
        orphans = sorted(present - linked)
        assert not dangling, (
            f"SKILL.md at {root} links missing references {dangling}"
        )
        assert not orphans, (
            f"reference files {orphans} at {root} are not linked from SKILL.md; "
            "link them or remove them"
        )


def _frontmatter(path):
    """The YAML block between the leading `---` pair, exactly as DSH's loader takes it."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert lines and lines[0].strip() == "---", f"{path}: SKILL.md must open with ---"
    assert "---" in lines[1:], f"{path}: SKILL.md frontmatter has no closing ---"
    return "\n".join(lines[1:lines.index("---", 1)])


def test_skill_frontmatter_is_valid_yaml_with_string_fields():
    # DSH logs `skill file ... ignored: invalid YAML frontmatter` and the skill then exists in NO
    # catalog, in any mode — no error reaches the model, the skill is simply absent. The bug that
    # did it here was a plain-scalar description containing "SolidWorks: never", which YAML reads
    # as a nested mapping. Only a real YAML parser can catch that.
    for root, label in ((PRESET, "install preset"), (MIRROR, ".dsh mirror")):
        path = os.path.join(root, "SKILL.md")
        block = _frontmatter(path)
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as ex:
            raise AssertionError(
                f"{label} SKILL.md frontmatter is not valid YAML, so DSH drops the skill: {ex}"
            ) from ex
        assert isinstance(data, dict), f"{label}: frontmatter must be a mapping, got {type(data)}"
        name, description = data.get("name"), data.get("description")
        # DSH's stringField() requires a non-empty STRING for both, or the file is ignored.
        assert isinstance(name, str) and name == "solidworks-cad", (
            f"{label}: name must be the string 'solidworks-cad', got {name!r}")
        assert isinstance(description, str) and description.strip(), (
            f"{label}: description must be a non-empty string, got {type(description)} — a colon "
            "inside an unquoted plain scalar turns it into a mapping and DSH ignores the skill")
        assert len(description) <= SKILL_MAX_DESCRIPTION, (
            f"{label}: description is {len(description)} chars "
            f"(bound {SKILL_MAX_DESCRIPTION})")


def test_frontmatter_guard_rejects_the_colon_space_shape():
    # The guard must have teeth: the exact bug shape may not come back as a usable string.
    buggy = "name: solidworks-cad\ndescription: use when the target is SolidWorks: never install\n"
    try:
        parsed = yaml.safe_load(buggy)
    except yaml.YAMLError:
        return
    assert not isinstance(parsed.get("description"), str), (
        "a colon-space plain scalar parsed as a string — this test no longer detects the bug")
    # (it parses as a mapping instead, which the skill test above also rejects)


class _JsTagTolerant(yaml.SafeLoader):
    """The composition carries `!!js` expressions (the skills dir path). They are opaque to a
    consistency check and unknown to SafeLoader, so resolve them to their literal text. Still a
    SafeLoader: the added constructor returns the scalar STRING and can never instantiate an
    object, which is the only reason the tag is accepted at all."""


def _js_tag(loader, suffix, node):
    return getattr(node, "value", None)


_JsTagTolerant.add_multi_constructor("tag:yaml.org,2002:js", _js_tag)


def _composition_rows(path):
    with open(path, encoding="utf-8") as fh:
        rows = yaml.load(fh, Loader=_JsTagTolerant)
    assert isinstance(rows, list) and rows, f"{path}: composition must be a non-empty list"
    return rows


def test_preset_persona_loads_the_skill_before_any_tool_call():
    # A session that skips the skill never reads the intake procedure or the Feature Graph rules
    # and goes straight to the tools; the catalog entry alone does not make the model use it.
    path = os.path.join(
        REPO_ROOT, "dsh-integration", "presets", "solidworks-cad", "agent.cordis.yml"
    )
    rows = _composition_rows(path)
    persona = next((row for row in rows if row.get("id") == "persona"), None)
    assert persona is not None, "composition has no persona row"
    text = (persona.get("config") or {}).get("text") or ""
    assert text.strip(), "persona text is empty"
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    # "near the top": the rule must sit in the second paragraph (right after the greeting), so a
    # model that skims cannot reach a tool instruction before reading it.
    head = "\n\n".join(paragraphs[:2])
    assert "skill" in head, "the persona must name the skill tool in its opening rules"
    assert "solidworks-cad" in head, "the persona must name the skill to load"
    assert "工具" in head and "第一步" in head, (
        "the persona must state that loading the skill is the FIRST step of a new task, before "
        "any other tool call")
    assert "停止" in head, (
        "the persona must say: if the skill cannot be loaded, state it and stop")
    first_tools_para = next((i for i, p in enumerate(paragraphs)
                             if "mcp__solidworks__" in p), None)
    assert first_tools_para is not None and first_tools_para >= 2, (
        "the skill-first rule must come before the paragraph that tells the model to use the "
        "mcp__solidworks__ tools, or the tools win the race it is meant to lose")


def test_persona_merges_reading_plan_and_all_missing_dimensions_once():
    path = os.path.join(REPO_ROOT, "dsh-integration", "presets", "solidworks-cad", "agent.cordis.yml")
    persona = next(row for row in _composition_rows(path) if row.get("id") == "persona")
    text = persona["config"]["text"]
    flat = "".join(text.split())  # pins survive the block scalar's line wrapping
    for token in ("EXPLICIT", "DERIVED", "USER", "UNKNOWN", "直接看原图", "OCR与裁图按需", "全部真正缺失的尺寸", "只问一次ask_user_question", "答案引出新矛盾", "不设题数上限", "禁止按像素比例", "接受/继续", "小圆角"):
        assert token in flat, token
    # 2026-10-31 approve-then-build: preparation before the question, submission right after it.
    for token in ("同一步里并行发出", "必须在提问之前", "下一个调用必须是submit_feature_graph(fresh_document=true)", "不读参考资料", "不重写计划或todo", "不再次确认", "不结束本轮", "新建零件不需要批量确认", "不许提供“示意模型", "按整个视图框选", "不要用网格单元格", "按工程图惯例直接采用", "写出你的解读作为推荐", "看图后先定难度", "“简单图/复杂图”就按用户的", "边内部任意一点", "PLANNED_FEATURES_MISSING", "拆出去的每个节点都必须补回", "save_or_export(save)另存为工作目录下的新文件", "计划里写出预期包络和体积", "不问用户、不结束本轮", "紧接着调用confirm_action", "不要在聊天里让用户打字确认", "上一个建错的新零件留着不动"):
        assert token in flat, token
    assert "最多再补一次" not in text and "同一区域已读过再读会被拒绝" not in text
    assert "在回复中重新给出计划" not in text


def _server_tool_names():
    path = os.path.join(REPO_ROOT, "mcp-server-solidworks-main", "adapters", "dsh", "server.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return set(re.findall(r"@mcp\.tool\(structured_output=False\)\s*\ndef\s+(\w+)\(", src))


def _policy_tool_names():
    path = os.path.join(
        REPO_ROOT, "dsh-integration", "plugins", "solidworks-policy", "index.js"
    )
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"const SOLIDWORKS_TOOLS = \[(.*?)\];", src, re.S)
    assert match, "SOLIDWORKS_TOOLS array not found in solidworks-policy/index.js"
    names = re.findall(r'"(?:mcp__solidworks__)?([a-z_]+)"', match.group(1))
    return {name for name in names if not name.startswith("mcp__")}


def test_twelve_tool_surface_is_registered_and_allowlisted():
    registered = _server_tool_names()
    allowlisted = _policy_tool_names()
    assert registered == allowlisted, (
        f"server.py registers {sorted(registered ^ allowlisted)} differently from "
        f"the policy allowlist; {SURFACE_DRIFT_HINT}"
    )
    assert registered == EXPECTED_TOOLS, (
        f"model surface drifted from the documented twelve tools "
        f"(diff={sorted(registered ^ EXPECTED_TOOLS)}); {SURFACE_DRIFT_HINT}"
    )


def test_skill_names_every_generated_reference_topic():
    """The Skill must advertise the topics references.py actually serves.

    The pre-build reading got cheaper by adding generated topics (quickref, changelog). If the
    Skill keeps telling the model to page the whole schema, the saving is invisible to it — so
    the topic list and the reading advice are asserted against the live adapter, not against a
    hand-copied list.
    """
    import sys

    adapter = os.path.join(REPO_ROOT, "mcp-server-solidworks-main", "adapters", "dsh")
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import references as _references

    for root in (MIRROR, PRESET):
        skill = _read(os.path.join(root, "SKILL.md")).decode("utf-8")
        for topic in _references.GENERATED_TOPICS:
            assert f"`{topic}`" in skill, f"{root}/SKILL.md never names reference topic {topic}"
        # the stale page-size / page-count wording must be gone
        assert "about 15" not in skill, f"{root}/SKILL.md still says the schema is ~15 pages"
        assert "40\nlines" not in skill and "at most 40 lines" not in skill
        # and it must put the card + mapping reads before the question, next to start_job
        assert "BEFORE the one question, in ONE parallel step: `start_job`, feature_graph_part_card" in skill
        assert "After approval the NEXT call is `submit_feature_graph(..., fresh_document=true)`" in skill


def test_quickref_covers_every_node_type_in_the_schema():
    """Generated from the schema, so the quickref cannot silently miss a node type."""
    import sys

    adapter = os.path.join(REPO_ROOT, "mcp-server-solidworks-main", "adapters", "dsh")
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import references as _references

    schema = _references._load_schema()
    quickref = _references.render_reference("feature_graph_quickref")
    expected = [
        name
        for name in (schema.get("feature_types") or {})
        if isinstance(name, str) and not name.startswith("_")
    ]
    expected += [
        name
        for name in (schema.get("assembly_types_v06") or {})
        if isinstance(name, str) and not name.startswith("_")
    ]
    assert expected
    missing = [name for name in expected if f"\n{name} — " not in quickref]
    assert not missing, f"quickref is missing node types {missing}"
    for token in ("meters", "radians"):
        assert token in quickref


def test_quickref_field_lines_are_whole_and_balanced():
    """The quickref's own compactness contract: no clipped sentence, no broken markup.

    A field line shortened by cutting instead of by the shared legend silently changes the
    instruction (the shell faces_to_remove line used to be clipped mid-sentence), and a
    dangling ")" from an abbreviation that consumed only its opener mangles the line. Guard
    both: every field line's parentheses balance, no rationale sentence survived the drop,
    and every line ends on punctuation, a legend token, or a whole word from the schema.
    """
    import re
    import sys

    adapter = os.path.join(REPO_ROOT, "mcp-server-solidworks-main", "adapters", "dsh")
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import references as _references

    quickref = _references.render_reference("feature_graph_quickref")
    schema = _references._load_schema()
    sources = {}
    for group in ("feature_types", "assembly_types_v06"):
        for name, node in (schema.get(group) or {}).items():
            if not isinstance(node, dict):
                continue
            for field, field_spec in node.items():
                if field not in ("description", "_note") and isinstance(field_spec, str):
                    sources[(name, field)] = field_spec
    # Legend tokens a line may legitimately end on after abbreviation: units/verbs the
    # legend defines, the type words the legend abbreviates, and marker words like the
    # mode letters in edge_flange's MODE A / MODE B.
    legend_tokens = {
        "m", "rad", "deg", "IP", "AM", "AA", "C4", "SI", "true", "false",
        "bool", "str", "num", "int", "obj", "A", "B", "ON", "FROM", "opt", "dflt",
    }
    current = None
    for raw in quickref.split("\n"):
        if raw and not raw.startswith(" "):
            current = raw.split(" — ", 1)[0]
            continue
        line = raw.strip()
        if not line or ": " not in line:
            continue
        marker, _, rest = line.partition(" ")
        field, spec = rest.split(": ", 1)
        opens, closes = spec.count("("), spec.count(")")
        assert opens == closes, f"unbalanced parens in {current}.{field!r}: {spec!r}"
        # no rationale sentence survived the drop
        match = _references._PROVENANCE_RE.search(spec)
        assert match is None, f"provenance survived in {current}.{field!r}: {spec!r}"
        # no mid-WORD cut: the line ends on punctuation, a legend token, or a word that the
        # original schema spec contains whole (a clip like "...ON its pla" passes none).
        last = spec.split()[-1].strip(".,;:)]}'\"`")
        source = sources.get((current, field), "")
        ok = spec[-1] in ".,;:)]}'\"`|>" or last in legend_tokens or (
            source and re.search(r"\b%s\b" % re.escape(last), source)
        )
        assert ok, f"mid-sentence cut in {current}.{field!r}: {spec!r}"


# --- the solidworks-policy plugin (tool restriction) --------------------------

POLICY_DIR = os.path.join(REPO_ROOT, "dsh-integration", "plugins", "solidworks-policy")
POLICY_JS = os.path.join(POLICY_DIR, "index.js")
POLICY_MANIFEST = os.path.join(POLICY_DIR, "package.json")

# What a SolidWorks CAD session may use besides the twelve CAD tools. The desktop (tauri) profile
# has no modlens package, so that entry is inert there; it stays because SKILL.md and README.md
# document it as approved for the CLI profile, where whole-sheet reading has no other route.
# list_mcp_resources was removed 2026-10-31: SKILL.md links every reference URI, and its one call
# in a measured session cost a 135 s model step. The desktop therefore sees exactly 16 tools.
POLICY_SUPPORT_TOOLS = {
    "ask_user_question",
    "todo_write",
    "skill",
    "modlens_read_image",
    "read_mcp_resource",
}


def _policy_source():
    with open(POLICY_JS, encoding="utf-8") as fh:
        return fh.read()


def _policy_array(name):
    match = re.search(r"const %s = \[(.*?)\];" % name, _policy_source(), re.S)
    assert match, "%s array not found in solidworks-policy/index.js" % name
    return {n for n in re.findall(r'"([a-z_]+)"', match.group(1))}


def test_policy_support_tools_are_exactly_the_approved_set():
    """The allowlist is closed: the twelve CAD tools plus this fixed support set, and nothing else.

    Equality is the whole check — a future `bash`/`fetch`/`playwright` entry can only arrive by
    editing this test, which is the point. What the guard refuses is covered by the plugin's own
    node suite (test/policy.test.mjs), where the denial runs against a stub registry.
    """
    support = _policy_array("SUPPORT_TOOLS")
    assert support == POLICY_SUPPORT_TOOLS, (
        "solidworks-policy SUPPORT_TOOLS drifted: +%s -%s"
        % (sorted(support - POLICY_SUPPORT_TOOLS), sorted(POLICY_SUPPORT_TOOLS - support)))
    cad = _policy_array("SOLIDWORKS_TOOLS")
    assert {n.replace("mcp__solidworks__", "") for n in cad} == EXPECTED_TOOLS, (
        "the CAD half must stay the twelve documented tools")


def test_policy_peer_dependencies_admit_the_cli_and_the_desktop_core():
    """dsh's own profile preflight DISABLES a plugin row whose dsh peers the runtime fails.

    `@deepseek-ai/dsh-app-boot` evaluates every `@deepseek-ai/dsh*` peer against the running dsh
    version and writes `disabled: true` on the row otherwise, so a range pinned to the 0.1 line
    silently un-registers the policy in the desktop (0.2.0-rc.2) — with no tool restriction and no
    error. Both installed cores must be named explicitly.
    """
    import json

    with open(POLICY_MANIFEST, encoding="utf-8") as fh:
        peers = json.load(fh)["peerDependencies"]
    assert set(peers) == {"@deepseek-ai/cordis", "@deepseek-ai/dsh-tools"}
    assert peers["@deepseek-ai/dsh-tools"] == "^0.1.0-rc.6 || ^0.2.0-rc.2", (
        "the range must keep admitting the CLI core (0.1.x) and the desktop core (0.2.x); "
        "dropping an alternative disables the policy in that profile without any error")
    assert peers["@deepseek-ai/cordis"] == "^4.0.1"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_policy_plugin_behaviour_tests_pass():
    """Run the plugin's own node:test suite (restrict tolerance, guard, pre-execute)."""
    result = subprocess.run(
        ["node", "--test", os.path.join(POLICY_DIR, "test", "policy.test.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        "node --test failed:\n%s\n%s" % (result.stdout, result.stderr))
