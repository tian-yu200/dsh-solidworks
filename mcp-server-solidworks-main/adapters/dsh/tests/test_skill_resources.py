"""The solidworks-cad skill references, served as read-only MCP resources.

The preset has no local file tool, so these resources are the only route a SolidWorks session has
to the documents SKILL.md links. Coverage: the listing is complete, a read returns the exact bytes,
a path is refused instead of served, an unknown name is refused, and every link in BOTH SKILL.md
copies resolves to a listed resource.
"""
import asyncio
import importlib.util
import os
import re
from pathlib import Path

import pytest

import skill_resources
from settings import SKILL_REFERENCES_DIR


TESTS_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = TESTS_DIR.parent
REPO_ROOT = TESTS_DIR.parents[2].parent  # adapters/dsh/tests -> adapters/dsh -> adapters -> repo
SERVER_PATH = ADAPTER_DIR / "server.py"
PRESET_SKILL = (
    REPO_ROOT / "dsh-integration" / "presets" / "solidworks-cad" / "skills"
    / "solidworks-cad" / "SKILL.md"
)
MIRRORED_SKILL = REPO_ROOT / ".dsh" / "skills" / "solidworks-cad" / "SKILL.md"

URI_RE = re.compile(r"solidworks://skill/references/([A-Za-z0-9._-]+)")
RELATIVE_LINK_RE = re.compile(r"\]\((?:\./)?references/([A-Za-z0-9._-]+\.md)\)")
# Any markdown link to a .md that is NOT a URI and not an in-document anchor.
PATH_LINK_RE = re.compile(
    r"\]\((?!solidworks://|[a-z][a-z0-9+.-]*://|#)([^)\s]+\.md)\)")


@pytest.fixture(scope="module")
def adapter():
    spec = importlib.util.spec_from_file_location("dsh_server_skill_resources", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _names(resources):
    return [str(item.uri).rsplit("/", 1)[-1] for item in resources]


def _read(module, uri):
    chunks = list(asyncio.run(module.mcp.read_resource(uri)))
    return chunks[0].content


# --- the listing ---------------------------------------------------------------

def test_every_reference_file_is_listed(adapter):
    on_disk = sorted(p.name for p in SKILL_REFERENCES_DIR.iterdir() if p.is_file())
    listed = sorted(_names(asyncio.run(adapter.mcp.list_resources())))
    assert listed == on_disk, (
        "SKILL.md links a file the server does not serve, or a file is hidden from the roster")
    # Nothing may be dropped by the name pattern without the test knowing about it.
    assert [n for n in on_disk if not skill_resources._NAME_RE.match(n)] == []


def test_each_resource_carries_a_title_and_one_line_description(adapter):
    for item in asyncio.run(adapter.mcp.list_resources()):
        assert item.title and item.title.strip() == item.title
        assert item.description and "\n" not in item.description
        assert len(item.description) <= skill_resources.DESCRIPTION_LIMIT
        assert item.mime_type == "text/markdown"
        assert str(item.uri).startswith(skill_resources.URI_PREFIX)


def test_titles_and_descriptions_come_from_the_document():
    root = SKILL_REFERENCES_DIR
    for item in skill_resources.entries(root):
        text = (root / item["name"]).read_text(encoding="utf-8")
        heading = next(
            (ln.lstrip("#").strip() for ln in text.splitlines() if ln.startswith("#")), None)
        if heading:
            assert item["title"] == heading
        else:
            assert item["title"] == Path(item["name"]).stem
        assert item["description"].split(" ")[0] in text


def test_the_policy_still_allows_the_resource_tools():
    """A resource the preset cannot call is not a fix.

    solidworks-policy allowlists by name, so `read_mcp_resource` must be in SUPPORT_TOOLS or the
    guard would refuse the very call SKILL.md tells the model to make. The roster tool is
    deliberately off the list (2026-10-31: its one call cost a 135 s step), so no message or skill
    text may send the model to it.
    """
    source = (
        REPO_ROOT / "dsh-integration" / "plugins" / "solidworks-policy" / "index.js"
    ).read_text(encoding="utf-8")
    match = re.search(r"const SUPPORT_TOOLS = \[(.*?)\];", source, re.S)
    assert match, "SUPPORT_TOOLS array not found in solidworks-policy/index.js"
    allowed = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert "read_mcp_resource" in allowed, (
        "the policy must keep the read-only resource tool allowed: %s" % sorted(allowed))
    assert "list_mcp_resources" not in allowed
    # Model-visible text only: error messages, tool descriptions and the skill.
    errors = (Path(__file__).resolve().parents[1] / "skill_resources.py").read_text(encoding="utf-8")
    assert "list_mcp_resources on server" not in errors
    for path in (Path(__file__).resolve().parents[1] / "server.py",
                 REPO_ROOT / ".dsh" / "skills" / "solidworks-cad" / "SKILL.md"):
        assert "list_mcp_resources" not in path.read_text(encoding="utf-8"), path


def test_the_template_is_advertised_once(adapter):
    templates = asyncio.run(adapter.mcp.list_resource_templates())
    assert [str(t.uri_template) for t in templates] == [skill_resources.TEMPLATE_URI]


# --- reads ---------------------------------------------------------------------

def test_read_returns_the_exact_bytes(adapter):
    resources = asyncio.run(adapter.mcp.list_resources())
    assert len(resources) >= 5, "the skill has five references; the roster shrank"
    for item in resources:
        name = _names([item])[0]
        raw = (SKILL_REFERENCES_DIR / name).read_bytes()
        served = _read(adapter, str(item.uri))
        assert isinstance(served, str)
        assert served.encode("utf-8") == raw, "%s was not served byte-exact" % name


def test_template_read_matches_the_static_read(adapter):
    static = asyncio.run(adapter.mcp.list_resources())[0]
    name = _names([static])[0]
    assert _read(adapter, str(static.uri)) == _read(adapter, skill_resources.uri_for(name))


def test_a_reference_added_after_import_is_still_readable(adapter, tmp_path, monkeypatch):
    """The roster is frozen at import; the template is not, and it is the containment that gates."""
    root = tmp_path / "references"
    root.mkdir()
    (root / "first.md").write_text("# First\nExisting content line here.\n", encoding="utf-8")
    monkeypatch.setenv("DSH_SOLIDWORKS_SKILL_REFERENCES", str(root))
    added = root / "second.md"
    added.write_text("# Second\nWritten after the listing was taken.\n", encoding="utf-8")
    # Compared against the bytes on disk: on Windows write_text() may store CRLF, and serving
    # exactly what the file holds is the behaviour under test, not a defect.
    assert skill_resources.read("second.md") == added.read_bytes().decode("utf-8")
    assert [item["name"] for item in skill_resources.entries()] == ["first.md", "second.md"]


# --- refusals ------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("..", skill_resources.TRAVERSAL_REFUSED),
    ("../SKILL.md", skill_resources.TRAVERSAL_REFUSED),
    ("..\\SKILL.md", skill_resources.TRAVERSAL_REFUSED),
    ("C:\\Windows\\win.ini", skill_resources.TRAVERSAL_REFUSED),
    ("/etc/passwd", skill_resources.TRAVERSAL_REFUSED),
    ("references/drawing-intake.md", skill_resources.TRAVERSAL_REFUSED),
    (".hidden.md", skill_resources.TRAVERSAL_REFUSED),
    (".", skill_resources.TRAVERSAL_REFUSED),
    ("", skill_resources.NOT_FOUND),
    ("nope.md", skill_resources.NOT_FOUND),
])
def test_a_path_is_refused_with_a_code(name, expected):
    with pytest.raises(skill_resources.SkillResourceError) as caught:
        skill_resources.read(name)
    assert caught.value.code == expected


@pytest.mark.parametrize("suffix", [
    "..%2Fnope.md",
    "%2e%2e/nope.md",
    "nope.md",
])
def test_traversal_and_unknown_are_refused_through_the_transport(adapter, suffix):
    uri = skill_resources.URI_PREFIX + suffix
    with pytest.raises(Exception) as caught:
        _read(adapter, uri)
    text = str(caught.value)
    # A percent-encoded parent segment reaches the check and reports the rule; the plain-name
    # cases are refused by the transport as unknown, which is still a refusal.
    assert "SKILL_RESOURCE" in text or "Unknown resource" in text or "template" in text.lower()


def test_escaping_the_served_directory_is_refused_even_for_a_real_file(tmp_path, monkeypatch):
    root = tmp_path / "skills" / "references"
    root.mkdir(parents=True)
    secret = tmp_path / "skills" / "SECRET.md"
    secret.write_text("not a reference\n", encoding="utf-8")
    link = root / "link.md"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):  # pragma: no cover - needs Windows developer mode
        pytest.skip("this host cannot create symlinks for tests")
    monkeypatch.setenv("DSH_SOLIDWORKS_SKILL_REFERENCES", str(root))
    with pytest.raises(skill_resources.SkillResourceError) as caught:
        skill_resources.read("link.md")
    # The bare name passes the pattern, so it is the re-resolved containment check that stops a
    # link pointing out of the served directory.
    assert caught.value.code == skill_resources.NOT_FOUND


def test_a_foreign_uri_is_not_one_of_ours():
    with pytest.raises(skill_resources.SkillResourceError) as caught:
        skill_resources.read_uri("solidworks://other/x.md")
    assert caught.value.code == skill_resources.NOT_FOUND
    with pytest.raises(skill_resources.SkillResourceError) as caught:
        skill_resources.read_uri("file:///D:/x.md")
    assert caught.value.code == skill_resources.NOT_FOUND


def test_a_missing_reference_root_is_reported_with_a_code(monkeypatch, tmp_path):
    monkeypatch.setenv("DSH_SOLIDWORKS_SKILL_REFERENCES", str(tmp_path / "absent"))
    with pytest.raises(skill_resources.SkillResourceError) as caught:
        skill_resources.references_root()
    assert caught.value.code == skill_resources.ROOT_MISSING


def test_registration_survives_a_missing_skill_directory(tmp_path, monkeypatch):
    """The CAD tools must not go down because the skill folder moved."""
    monkeypatch.setenv("DSH_SOLIDWORKS_SKILL_REFERENCES", str(tmp_path / "absent"))
    warned = []
    from mcp.server import MCPServer

    server = MCPServer("probe")
    assert skill_resources.register_on(server, warn=warned.append) == []
    assert warned and "not readable" in warned[0]


def test_a_non_text_file_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "references"
    root.mkdir()
    (root / "blob.md").write_bytes(b"\xff\xfe not utf-8 at all\x00")
    monkeypatch.setenv("DSH_SOLIDWORKS_SKILL_REFERENCES", str(root))
    with pytest.raises(skill_resources.SkillResourceError) as caught:
        skill_resources.read("blob.md")
    assert caught.value.code == skill_resources.NOT_TEXT


# --- SKILL.md agreement --------------------------------------------------------

def test_both_skill_copies_are_identical_and_under_budget():
    a = PRESET_SKILL.read_bytes()
    b = MIRRORED_SKILL.read_bytes()
    assert a == b, "the two SKILL.md copies drifted apart"
    assert len(a.decode("utf-8").splitlines()) <= 550


def test_every_skill_reference_link_resolves_to_a_listed_resource(adapter):
    listed = set(_names(asyncio.run(adapter.mcp.list_resources())))
    for path in (PRESET_SKILL, MIRRORED_SKILL):
        text = path.read_text(encoding="utf-8")
        linked = set(URI_RE.findall(text))
        assert linked, "%s advertises no reference resource" % path
        assert linked == listed, (
            "%s links %s that the server does not serve; the roster is %s"
            % (path, sorted(linked ^ listed), sorted(listed)))
        # Every reference is addressed as a resource: no bare relative link survives, because the
        # preset cannot open a file path.
        bare = set(RELATIVE_LINK_RE.findall(text))
        assert bare == set(), "%s still links plain file paths: %s" % (path, sorted(bare))
        # And the file says how to read one: the tool, and which server to address.
        assert "read_mcp_resource" in text
        assert re.search(r"server [`\"']?solidworks", text), (
            "%s never names the server to read a resource from" % path)


def test_the_skill_never_links_a_file_path():
    """One rule for the whole skill, SKILL.md and every reference: a link is a resource URI.

    A reference that points at another reference by path is as unreadable as SKILL.md was, so the
    cross-links were rewritten too and this guard keeps them from creeping back.
    """
    listed = {item["name"] for item in skill_resources.entries(SKILL_REFERENCES_DIR)}
    documents = [
        PRESET_SKILL,
        MIRRORED_SKILL,
        *sorted(SKILL_REFERENCES_DIR.glob("*.md")),
        *sorted((REPO_ROOT / ".dsh" / "skills" / "solidworks-cad" / "references").glob("*.md")),
    ]
    assert len(documents) >= 12, "the skill copies were not both found"
    for document in documents:
        text = document.read_text(encoding="utf-8")
        paths = PATH_LINK_RE.findall(text)
        assert paths == [], "%s links file paths the preset cannot open: %s" % (
            document, paths)
        for name in URI_RE.findall(text):
            assert name in listed, "%s advertises %s, which the server does not serve" % (
                document, name)
