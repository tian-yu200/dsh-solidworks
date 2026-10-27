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
   nine tools (README, docs/SolidWorks-MCP工具清单.md, SKILL Tool Boundary).

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest`
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIRROR = os.path.join(REPO_ROOT, ".dsh", "skills", "solidworks-cad")
PRESET = os.path.join(
    REPO_ROOT, "dsh-integration", "presets", "solidworks-cad", "skills", "solidworks-cad"
)
SKILL_MAX_LINES = 550

EXPECTED_NINE_TOOLS = frozenset(
    {
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


def test_nine_tool_surface_is_registered_and_allowlisted():
    registered = _server_tool_names()
    allowlisted = _policy_tool_names()
    assert registered == allowlisted, (
        f"server.py registers {sorted(registered ^ allowlisted)} differently from "
        f"the policy allowlist; {SURFACE_DRIFT_HINT}"
    )
    assert registered == EXPECTED_NINE_TOOLS, (
        f"model surface drifted from the documented nine tools "
        f"(diff={sorted(registered ^ EXPECTED_NINE_TOOLS)}); {SURFACE_DRIFT_HINT}"
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
        # and it must point at the quickref as the default reading
        assert "feature_graph_quickref` (1-2" in skill or "feature_graph_quickref`" in skill


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
