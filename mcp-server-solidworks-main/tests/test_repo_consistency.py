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
