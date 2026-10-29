"""The solidworks-cad skill's own reference files, served as read-only MCP resources.

WHY THIS EXISTS. The SolidWorks CAD preset has no local file tool — deliberately: the
solidworks-policy allowlist is the twelve `mcp__solidworks__*` tools plus four support tools. So a
`references/<file>.md` link inside SKILL.md was a pointer the model could not follow. A live
desktop session tried `mcp__fetch__fetch` with a `file://` URL (rejected: local paths only, the
reverse of every other rule here), then `list_mcp_resources` and `list_mcp_resource_templates`
(found nothing), and gave up. `read_mcp_resource` IS in the preset's tool set, so the references
are served as resources on this server and that tool becomes the route.

NOT A COPY. A URI resolves through the skill directory, which the installed preset keeps as a
junction to the repository (`dsh-integration/presets/solidworks-cad/skills`), so the repo stays the
single source and an edited reference answers on its next read. The listing, by contrast, is taken
once when the server imports: a file added after startup is readable by URI (the template re-lists
at read time) but appears in `list_resources` only after a restart.

CONTAINMENT. The serving directory is a real repo path that happens to be reachable through
junctions, so an unguarded `join(root, user_input)` would let `..` walk out of the skill. The name
is therefore checked against an allow-shaped pattern AND re-resolved inside the root, and the
framework's own template security is deliberately exempted for this parameter — otherwise a
`..` URI is answered with the transport's generic "unknown resource" and a model that sees that
retries variants, while a coded refusal tells it the rule exists.
"""
import os
import re
from pathlib import Path

from mcp.server.mcpserver.exceptions import ResourceError

import settings


URI_PREFIX = "solidworks://skill/references/"
# One template covers every file, so a URI a model copies out of SKILL.md resolves even for a
# reference added since this server imported.
TEMPLATE_URI = URI_PREFIX + "{name}"
TEMPLATE_NAME = "solidworks_skill_reference"

ROOT_MISSING = "SKILL_RESOURCE_ROOT_MISSING"
TRAVERSAL_REFUSED = "SKILL_RESOURCE_TRAVERSAL_REFUSED"
NOT_FOUND = "SKILL_RESOURCE_NOT_FOUND"
NOT_TEXT = "SKILL_RESOURCE_NOT_TEXT"

# A bare file name and nothing else: no separator, no parent segment, no scheme or drive colon,
# no leading dot (which would reach a dotfile or, with `.`, the directory itself).
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,126}\Z")
_TRAVERSAL_MARKERS = ("..", "/", "\\", ":", "\x00")

_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".csv": "text/csv",
}

# Titles come from the document, so they cannot drift from it. The filename stem answers only when
# a file has no heading at all.
DESCRIPTION_LIMIT = 110


class SkillResourceError(ValueError):
    """A refusal with a stable code, so the model gets the rule rather than a stack trace."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def references_root():
    """The directory being served, resolved to its real path (junctions followed)."""
    override = os.getenv("DSH_SOLIDWORKS_SKILL_REFERENCES")
    base = Path(override) if override else settings.SKILL_REFERENCES_DIR
    root = Path(base).resolve()
    if not root.is_dir():
        raise SkillResourceError(
            ROOT_MISSING,
            "the skill reference directory '%s' is not readable. The solidworks-cad skill ships "
            "its references under <repo>/dsh-integration/presets/solidworks-cad/skills/"
            "solidworks-cad/references; set DSH_SOLIDWORKS_SKILL_REFERENCES if this checkout "
            "lives elsewhere." % root,
        )
    return root


def uri_for(name):
    return URI_PREFIX + name


def name_for(uri):
    """The file name a URI asks for, or a coded refusal if the URI is not one of ours."""
    text = str(uri or "")
    if not text.startswith(URI_PREFIX):
        raise SkillResourceError(
            NOT_FOUND,
            "'%s' is not a solidworks skill reference URI. They look like %s<file>.md — run "
            "list_mcp_resources on server solidworks for the current set." % (text, URI_PREFIX),
        )
    return text[len(URI_PREFIX):]


def _check_name(name, root):
    """Return the contained path for `name`, refusing anything that is not one bare file in root."""
    if not isinstance(name, str) or not name:
        raise SkillResourceError(NOT_FOUND, "an empty reference name is not a resource")
    lowered = name.lower()
    if any(marker in lowered for marker in _TRAVERSAL_MARKERS) or not _NAME_RE.match(name):
        raise SkillResourceError(
            TRAVERSAL_REFUSED,
            "'%s' is refused: only a bare file name directly under %s can be read, never a path. "
            "Run list_mcp_resources on server solidworks and use one of those URIs."
            % (name, root),
        )
    target = (root / name)
    resolved = target.resolve()
    # Second gate, on the real path: a name that passes the pattern but resolves out of the
    # served directory (a symlink placed inside it, a stray junction) still stops here.
    if resolved.parent != root or not resolved.is_file():
        raise SkillResourceError(
            NOT_FOUND,
            "there is no skill reference named '%s'. Run list_mcp_resources on server solidworks "
            "for the exact names." % name,
        )
    return resolved


def _mime_for(path):
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "text/plain")


def _read_text(path):
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8"), raw
    except UnicodeDecodeError:
        raise SkillResourceError(
            NOT_TEXT,
            "'%s' is not UTF-8 text, so it cannot be served as a reference document."
            % path.name,
        )


def _summarize(text, fallback):
    """Title from the first heading, description from the first real prose line.

    Both come out of the document, so they cannot disagree with it. Lines that are a table row,
    a code fence, or a list of link targets are skipped: they read as noise, not as a summary.
    """
    title = fallback
    description = ""
    seen_heading = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if not seen_heading:
                seen_heading = True
                if heading:
                    title = heading
            continue
        if description or line[0] in "|>`-+*_" or line.startswith("[["):
            continue
        candidate = " ".join(line.split())
        if len(candidate) < 12:
            continue
        description = candidate[:DESCRIPTION_LIMIT].rstrip()
        break
    return title, (description or ("%s reference for the solidworks-cad skill" % fallback))


def entries(root=None):
    """Every servable file, one entry each, in name order (the resource listing)."""
    root = root or references_root()
    found = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if not _NAME_RE.match(path.name):
            continue
        text, raw = _read_text(path)
        stem = Path(path.name).stem
        title, description = _summarize(text, stem)
        found.append({
            "name": path.name,
            "uri": uri_for(path.name),
            "title": title,
            "description": description,
            "mime_type": _mime_for(path),
            "bytes": len(raw),
        })
    if not found:
        raise SkillResourceError(
            NOT_FOUND, "no reference files were found under %s" % root)
    return found


def read(name, root=None):
    """Exact bytes of one reference, as text."""
    root = root or references_root()
    path = _check_name(name, root)
    text, _raw = _read_text(path)
    return text


def read_uri(uri, root=None):
    """Exact bytes of one reference, addressed by its solidworks:// URI."""
    return read(name_for(uri), root=root)


def refuse_as_resource_error(readable, name, root=None):
    """Read through the transport's own error type, so the refusal text survives.

    A template function that raises anything but ResourceError/MCPError is re-wrapped by the
    manager as "Error creating resource from template <uri>", which costs the reader the code and
    the guidance — exactly the thing that made the original session retry variants instead of
    stopping. ResourceError passes through unchanged.
    """
    try:
        return readable(name, root=root)
    except SkillResourceError as exc:
        raise ResourceError(str(exc)) from exc


def register_on(mcp, root=None, warn=None):
    """Add one static resource per reference file, so list_mcp_resources answers completely.

    Called at server import. A missing skill directory must not take the CAD tools down with it,
    so failures are reported through `warn` (stderr by default) and simply produce no resources.
    """
    import sys

    if warn is None:
        def warn(message):
            sys.stderr.write("solidworks-adapter: skill resources unavailable: %s\n" % message)
    added = []
    try:
        items = entries(root)
    except SkillResourceError as exc:
        warn(exc.message)
        return added
    for item in items:
        # A static resource must be a ZERO-parameter callable (the framework inspects the
        # signature), so the URI it serves is captured in a closure rather than defaulted.
        def _make(uri):
            def _serve() -> str:
                return refuse_as_resource_error(read_uri, uri, root=root)
            return _serve

        _serve = _make(item["uri"])
        _serve.__name__ = "skill_reference_%s" % re.sub(r"\W+", "_", item["name"])
        mcp.resource(
            item["uri"],
            name=item["name"],
            title=item["title"],
            description=item["description"],
            mime_type=item["mime_type"],
        )(_serve)
        added.append(item["uri"])
    return added
