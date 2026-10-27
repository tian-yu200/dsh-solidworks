import json
import re

from settings import FEATURE_GRAPH_SCHEMA_PATH, RECIPE_PATH


TOPICS = {
    "contract": "contract",
    "canonicalization": "canonicalization",
    "forward": "forward",
    "mapping": "mapping",
    "mapping_part": "mapping_part",
    "mapping_sheet_metal": "mapping_sheet_metal",
    "mapping_assembly": "mapping_assembly",
    "verification": "verification",
    "reverse": "reverse",
    "coverage": "coverage",
}

# Topics that are GENERATED from feature-graph.schema.json at read time rather than being a
# recipe section. Generated means they cannot drift: a node type added to the schema appears in
# the quickref on the next read, with no second place to update.
SCHEMA_TOPIC = "feature_graph_schema"
QUICKREF_TOPIC = "feature_graph_quickref"
CHANGELOG_TOPIC = "feature_graph_changelog"
GENERATED_TOPICS = (SCHEMA_TOPIC, QUICKREF_TOPIC, CHANGELOG_TOPIC)

ALL_TOPICS = tuple(sorted([*TOPICS, *GENERATED_TOPICS]))

# Paging defaults for scope=reference. A reference is returned ONE PAGE AT A TIME so the
# result always survives the host's tool-result elision rule (which cuts a result over a
# line/character budget down to head + marker + tail, hiding the middle — that is what used
# to swallow the 498-line feature_graph_schema whole).
#
# The host pruner only trims results ABOVE 8192 characters, so pages carry up to ~6700
# characters of content while the SERIALIZED page (envelope + section index + content) is
# kept under REFERENCE_PAGE_MAX_SERIALIZED = 7000 — a hard cap that leaves ~1200 characters
# of margin against the pruner and is asserted per page, not assumed. The budgets are sized
# so the generated feature_graph_quickref fits in TWO pages WITH room for four more node
# types (asserted by test_quickref_leaves_headroom_for_four_more_node_types).
REFERENCE_PAGE_LINES = 150
REFERENCE_PAGE_CHARS = 6820
REFERENCE_PAGE_MAX_SERIALIZED = 7000
# Room reserved for the page envelope itself (keys, counters) once the section index is
# subtracted, so the serialized page stays under the host budget.
REFERENCE_PAGE_HEADROOM = 400


def _recipe_sections():
    text = RECIPE_PATH.read_text(encoding="utf-8")
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text))
    sections = {}
    for index, match in enumerate(headings):
        title = match.group(2).strip().casefold()
        start = match.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _load_schema():
    """The parsed contract schema — the ONE source both generated topics read."""
    return json.loads(FEATURE_GRAPH_SCHEMA_PATH.read_text(encoding="utf-8"))


def _schema_without_changelog(schema):
    """The schema as the paged body should show it: `_meta.status` removed.

    `_meta.status` is a ~4 KB changelog paragraph. It is useful provenance, but inside the
    paged body it cost a whole page on its own (and, being one unbreakable line, forced the
    `line_truncated` path). It now has its own topic; the body keeps a pointer instead of the
    prose, so nothing is lost and the paging walk is all schema.
    """
    body = json.loads(json.dumps(schema))
    meta = body.get("_meta")
    if isinstance(meta, dict) and "status" in meta:
        meta["status"] = (
            "CHANGELOG MOVED — read reference_topic 'feature_graph_changelog' for the full "
            "revision history of this contract."
        )
    return body


def _changelog_text(schema):
    """The changelog prose as its own (paged) reference."""
    meta = schema.get("_meta") or {}
    lines = [
        "# feature-graph contract changelog",
        "",
        "name: %s" % meta.get("name", ""),
        "version: %s" % meta.get("version", ""),
        "",
        "## status / revision history",
        "",
    ]
    status = meta.get("status") or ""
    # Wrap the paragraph into readable lines so this topic pages normally instead of being
    # one unbreakable 4 KB line.
    width = 110
    for paragraph in status.split("\n"):
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = word
            else:
                current = (current + " " + word).strip()
        lines.append(current)
    return "\n".join(lines) + "\n"


def _field_is_optional(spec):
    """A schema field is optional when its prose says so (that is the only marker there is)."""
    text = str(spec or "").lower()
    return "optional" in text or "default" in text


# Rationale/provenance sentences. These say WHY a rule exists or WHEN it landed — valuable in
# the schema topic, but they are not the instruction, and the quickref has to stay inside two
# pages. They are dropped as WHOLE SENTENCES (or whole parenthetical groups), never cut
# mid-clause: a clipped sentence silently changes the instruction, a dropped rationale does
# not. The full prose is one `reference_topic 'feature_graph_schema'` call away.
_PROVENANCE_MARKERS = (
    "COVERED since",
    "grown for",
    "passthrough added",
    "observed for real",
    "observed on",
    "exposed v",
    "added v",
    "IR-ADR",
    "ADR-",
    "KNOWN-LIMITATIONS",
    "recipe R",
    "e.g.",
    "4-1's",
    "matching a recipe",
    "for why",
    "back-compat",
    "Back-compat",
    "v0.",
    "historical",
    "unaffected",
    "machine-diffed",
)
_PROVENANCE_RE = re.compile("|".join(re.escape(m) for m in _PROVENANCE_MARKERS))


def _drop_rationale(text):
    """Drop provenance/rationale WITHOUT ever cutting a sentence in half.

    Two passes, both aligned to real boundaries:
      1. parenthetical groups `( ... )` that are nothing but provenance (the group is removed
         whole, so the sentence around it stays grammatical);
      2. trailing sentences that are nothing but provenance. The FIRST sentence is always
         kept — it carries the type and the meaning — so a field can never come out empty.
    """
    def _paren(match):
        return "" if _PROVENANCE_RE.search(match.group(0)) else match.group(0)

    text = re.sub(r"\s*\([^()]*\)", _paren, text)
    # A "since vX.Y.Z" version stamp introducing a clause is a release note, not a rule.
    # (The version's own dots rule out folding this into a broader em-dash deletion.)
    text = re.sub(r"\bsince v\d+(?:\.\d+)+,?\s*", "", text)
    sentences = re.split(r"(?<=[.;])\s+", text)
    if len(sentences) > 1:
        kept = [sentences[0]] + [
            s for s in sentences[1:] if not _PROVENANCE_RE.search(s)
        ]
        text = " ".join(kept)
    return " ".join(text.split())


# Legend tokens. Pure substitutions: each one replaces boilerplate that the legend line
# defines, so the field keeps its full meaning. This is the main lever — 84% of the card is
# field prose, and the schema restates the same phrases dozens of times.
_FIELD_ABBREVIATIONS = (
    # The "consumes the ACTIVE sketch" family: six node types carry it verbatim (~110 chars).
    (
        r"string — id of a sketch node holding the bend line\(s\) \(must be the immediately "
        r"preceding node — the (\w+) consumes the ACTIVE sketch\)",
        r"str: bend-line sketch id, IP (\1 eats ACTIVE sketch)",
    ),
    (
        r"string — id of a sketch node \(must be the immediately preceding node — the "
        r"(\w+) consumes the ACTIVE sketch\)",
        r"str: sketch id, IP (\1 eats ACTIVE sketch)",
    ),
    (
        r"string — id of the PROFILE sketch node \(must be the immediately preceding node\)",
        "str: profile sketch id, IP",
    ),
    (r"\(must be the immediately preceding node — the (\w+) consumes the ACTIVE sketch\)",
     r"(IP; \1 eats ACTIVE sketch)"),
    (r"must be the immediately preceding node", "IP"),
    (r"the immediately preceding node", "IP"),
    # Units and the SI->tool-boundary conversion note.
    (r"; the compiler converts to the tool boundary's degrees, C4\)",
     "; compiler→tool deg, C4)"),
    (r"the compiler converts to the tool boundary's degrees, C4",
     "compiler→tool deg, C4"),
    (r"the compiler converts to the tool boundary's degrees", "compiler→tool deg"),
    (r"matching the recipe's SI dimension", "SI"),
    (r"the recipe's SI dimension", "SI"),
    # Helper names: the quickref names the mechanism's VOCABULARY (resolve_face_by_anchor /
    # create_sketch), not the call sites — the parenthesised call targets are decoration.
    # Thread (v0.9.0) vocabulary: the quickref must name the MECHANISM, not spell out
    # analyze_model(edges) or a swconst enum name on every one of fourteen field lines.
    (r"analyze_model\(edges\)", "AM(edges)"),
    (r"swCosmeticStandardType_e", "swStandardType_e"),
    (r"swCosmeticEndConditions_e", "swCosmeticEnd_e"),
    (r"swCosmeticThread_MajorDiameter", "major-dia"),
    (r"\s*\(resolve_face_by_anchor\)", ""),
    (r"\s*\(create_sketch on_face\)", ""),
    (r"^'coincident' \| 'concentric' \| 'perpendicular' \| 'parallel' \| 'tangent' \| 'distance' \| 'angle' \| 'lock'$",
     "one of `mates` (top of card)"),
    # Same de-duplication for the alignment vocabulary, which the mate_type line used to repeat
    # in full on a second line (the words are already on the card, the legend covers 'opt').
    (r"^'aligned' \| 'anti_aligned' \| 'closest' \(optional; record the reader's RESOLVED alignment verbatim\)$",
     "aligned | anti_aligned | closest; record the reader's RESOLVED alignment verbatim"),
    # The C4 note is the LEGEND's job (the legend line states the SI->tool-boundary rule), so the
    # per-field restatement of it is duplication on every angle/distance value field.
    (r"; (?:the )?compiler converts to the tool boundary\)", ")"),
    # `sketch.profile`'s parenthetical restates the `profile_primitives` line printed right below
    # it on the card (which kinds exist). Keep the requirement, drop the duplicated kind list.
    (r" \u2014 the ORIGINAL sketch's segments in original 2D coords \(line/arc/circle \+ construction flag\)",
     " \u2014 ORIGINAL sketch segments, original 2D coords"),
    (r"^EXACTLY 2 \u2014 \{ component: <earlier component node id>, anchor: \{ kind, near: \[x,y,z\] COMPONENT-LOCAL \(m\), hint\? \} \}$",
     "EXACTLY 2 \u2014 {component: <earlier component node id>, anchor: {kind, near: [x,y,z] COMPONENT-LOCAL (m), hint?}}"),
    (r"\s*\(maps to the extrude tool's `reverse` param\)", ""),
    # Thread fields (v0.9.0): the callout/tolerance prose repeats the same three phrases on
    # every thread-bearing node. The legend already defines AM; these name the mechanism's
    # VOCABULARY, never drop a normative clause.
    (r"\(analyze_model\(edges\) `center` for kind='circle'\)", "(AM(edges) `center`, kind='circle')"),
    (r"analyze_model\(edges\)", "AM(edges)"),
    # The sheet-metal "stay put" family: four node types restate the same 120-char clause. The
    # legend carries the meaning once; each field keeps its own side/support rule.
    (r", on the SIDE that must stay put, in the PRE-bend geometry \(the state the rebuild is in when the bend executes\)", ", on the side that stays put, PRE-bend"),
    (r", on the SIDE that stays put, in the PRE-flange geometry", ", on the side that stays put, PRE-flange"),
    (r"Passed straight through as the tool's fixed_x/y/z coordinate pick \(no index resolution \u2014 the point IS the side selector\)", "Passed through as fixed_x/y/z (the point IS the side selector)"),
    (r"swCosmeticStandardType_e", "swStandardType_e"),
    (r"swCosmeticEndConditions_e", "swCosmeticEnd_e"),
    # Legend-backed token compression (v0.9.0): the legend ALREADY defines AM/AA/IP, so every
    # spelled-out occurrence is pure duplication. Meaning-preserving, no clause dropped.
    (r"analyze_model", "AM"),
    (r"analyze_assembly", "AA"),
    (r"\bthe compiler\b", "compiler"),
    (r"\be\.g\. ", "eg "),
    (r"positive number", "pos num"),
    (r"when present", "if given"),
    (r"mutually exclusive", "excl."),
    (r"\bREQUIRED\b", "required"),
    # NOT "req": a sentence-final "always required" would become "always req", which the
    # whole-line guard reads as a mid-word cut (the line then ends on a fragment that appears
    # in no schema spec). The 3 characters are worth keeping the instruction intact.
    (r"\brequired\b", "required"),
    (r"number — meters", "num m"),
    (r"number — RADIANS", "num rad"),
    (r"number — DEGREES", "num deg"),
    (r"number \(m\)", "num m"),
    (r"\(meters\)", "(m)"),
    (r"in meters", "in m"),
    (r"RADIANS", "rad"),
    (r"DEGREES", "deg"),
    # Type + optionality boilerplate (12x "boolean (optional, default").
    (r"boolean \(optional, default false", "bool opt, dflt false"),
    (r"boolean \(optional, default true", "bool opt, dflt true"),
    (r"boolean \(optional, default ", "bool opt, dflt "),
    (r"boolean \(optional\)", "bool opt"),
    (r"boolean \(default true\)", "bool dflt true"),
    (r"boolean \(default ", "bool dflt "),
    (r"string \(optional, default ", "str opt, dflt "),
    (r"string \(optional, ", "str opt, "),
    (r"string \(optional\)", "str opt"),
    (r"\(optional, default ", "(opt, dflt "),
    (r"\(optional\)", "(opt)"),
    (r"\(optional", "(opt"),
    (r"optional — ", "opt — "),
    (r"array of", "arr of"),
    (r"boolean", "bool"),
    (r"string", "str"),
    (r"integer", "int"),
    (r"number", "num"),
    (r"object", "obj"),
    # Vocabulary the legend defines.
    (r"analyze_model", "AM"),
    (r"analyze_assembly", "AA"),
    (r"\bdefault ", "dflt "),
    (r"the ORIGINAL part's ", ""),
    (r"the ORIGINAL's ", ""),
    (r"ORIGINAL's ", ""),
    (r"the original's ", ""),
    # Dangling closers: some optional/abbreviation patterns above consume the opening paren
    # of "(optional, default X" but not its matching ")" (the closing paren belongs to the
    # whole field spec, e.g. "boolean (optional, default true — thicken ...sketch)"). After
    # abbreviation the paren pair would be broken, so _rebalance_parens drops any closer
    # that no longer matches an opener, wherever it sits (leading, mid-line, trailing).
    # Never touches a paren inside a balanced group like "(two_sided=false)".
)


def _rebalance_parens(text):
    """Drop orphaned ")" whose opener an abbreviation consumed (depth would go negative)."""
    if text.count(")") <= text.count("("):
        return text
    out = []
    depth = 0
    for char in text:
        if char == ")":
            if depth == 0:
                continue
            depth -= 1
        elif char == "(":
            depth += 1
        out.append(char)
    return "".join(out)


def _abbreviate(text):
    """Apply the shared-legend substitutions to one field's prose."""
    for pattern, replacement in _FIELD_ABBREVIATIONS:
        text = re.sub(pattern, replacement, text)
    return " ".join(_rebalance_parens(text).split())


def _compact_field_spec(spec):
    """One field's schema prose on a single line — abbreviated, NEVER truncated.

    A clipped field is worse than a long one: the cut lands mid-clause and silently changes
    the instruction ("... `near` a point ON its pla"). So the prose is only ever shortened by
    (a) the shared-legend substitutions and (b) dropping whole rationale sentences. Every
    normative sentence survives.

    Dict-valued specs (sketch.profile_primitives) are the one place the per-kind prose is not
    carried — it is ~2.9 KB of detail. The card shows the primitive VOCABULARY with each
    kind's field names, so every kind and every field name still appears.
    """
    if isinstance(spec, dict):
        kinds = []
        for kind, inner in spec.items():
            if kind.startswith("_"):
                continue
            if isinstance(inner, dict):
                names = [k for k in inner if not k.startswith("_") and k != "kind"]
                kinds.append("%s(%s)" % (kind, ",".join(names)) if names else kind)
            else:
                kinds.append(kind)
        return "obj: " + " ".join(kinds)
    return _abbreviate(_drop_rationale(" ".join(str(spec or "").split())))


def _one_line_meaning(description, limit=58):
    """First clause of a node type's description — the MEANING, without the history.

    Node descriptions are mostly provenance ("COVERED since v0.5.1 — grown for the 1-2
    flange"); the first clause is the part that says what the node IS. Only this one line per
    node type is shortened — field prose is never cut.
    """
    text = _drop_rationale(" ".join(str(description or "").split()))
    for stop in (". ", " — ", "; ", " ("):
        index = text.find(stop)
        if 0 < index <= limit:
            return text[: index + (1 if stop == ". " else 0)].strip()
    if len(text) <= limit:
        return text
    return text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:—(")


def render_quickref(schema=None):
    """A compact, GENERATED one-look capability card for the Feature Graph.

    Built from `feature_types` / `assembly_types_v06` / `covered_subset` at read time, so a
    node type or field added to the schema shows up here on the next read — it cannot drift
    the way a hand-written summary would.

    Terseness is a hard contract, not a style choice: the quickref must fit in TWO pages and
    stay there as node types are added, because each page is a model round trip. The budget
    is spent as:

      1. a shared LEGEND defines the units and the repeated tokens (IP, AM, AA, opt, dflt),
         so no field line restates them;
      2. one line per node type — its first clause only;
      3. one line per field — rationale dropped, boilerplate abbreviated, NEVER truncated;
      4. no blank line between node types, and no restatement of a vocabulary the field
         lines already enumerate.

    Every node type and every field is present. The full prose stays one
    `reference_topic 'feature_graph_schema'` call away.
    """
    schema = schema if schema is not None else _load_schema()
    subset = schema.get("covered_subset") or {}
    version = (schema.get("_meta") or {}).get("version", "?")
    lines = [
        "# Feature Graph quickref (GENERATED from feature-graph.schema.json) %s" % version,
        "legend: R=required O=optional | m=meters rad=radians deg=degrees |"
        " IP=immediately preceding node | AM=analyze_model | AA=analyze_assembly |"
        " dflt=default",
    ]
    # One line per vocabulary, `key=v1|v2|...` — a COMPACT block, because six "label: a, b, c"
    # lines cost ~90 characters of pure label/comma overhead and the card is char-bound at two
    # pages (see REFERENCE_PAGE_CHARS). Every value of every covered_subset list is still
    # emitted, in schema order; only the separators and the prose labels are compressed, so
    # nothing is dropped and the two-page + 4-node headroom budget holds.
    vocabulary = (
        ("profiles", "profile_kinds"),
        ("datums", "datums"),
        ("extrude", "extrude_ends"),
        ("chamfer", "chamfer_types"),
        ("pdirs", "pattern_directions"),
        ("mates", "mate_types"),
    )
    parts = []
    for label, key in vocabulary:
        values = subset.get(key) or []
        if not values:
            continue
        text = "%s=%s" % (label, "|".join(values))
        if key == "datums":
            text += " (+opt signed m offset)"
        parts.append(text)
    if parts:
        lines.append(" | ".join(parts))

    def _emit(title, specs):
        lines.append(title)
        for name, spec in specs.items():
            if name.startswith("_") or not isinstance(spec, dict):
                continue
            lines.append("%s — %s" % (name, _one_line_meaning(spec.get("description"))))
            for field, field_spec in spec.items():
                if field in ("description", "_note"):
                    continue
                marker = "O" if _field_is_optional(field_spec) else "R"
                lines.append(
                    "  %s %s: %s" % (marker, field, _compact_field_spec(field_spec))
                )

    _emit("## Part node types", schema.get("feature_types") or {})
    assembly = schema.get("assembly_types_v06") or {}
    if [n for n in assembly if not n.startswith("_")]:
        _emit("## Assembly node types", assembly)
    lines.append(
        "Full prose: reference_topic 'feature_graph_schema'. History: 'feature_graph_changelog'."
    )
    return "\n".join(lines) + "\n"


def read_reference(topic):
    """The COMPLETE reference body for `topic` (unpaged).

    Kept as the single source of truth: `page_reference` slices its result, and the
    orchestrator's `reference` capability publishes it whole for programmatic callers.
    """
    if topic == SCHEMA_TOPIC:
        return _schema_without_changelog(_load_schema())
    if topic == QUICKREF_TOPIC:
        return render_quickref()
    if topic == CHANGELOG_TOPIC:
        return _changelog_text(_load_schema())
    title = TOPICS.get(topic)
    if title is None:
        raise ValueError("Unknown topic. Allowed: " + ", ".join(ALL_TOPICS))
    sections = _recipe_sections()
    for heading, body in sections.items():
        # Exact heading, or the topic as a leading word ("mapping — ..."). A bare substring
        # match would let an unrelated heading ("Reverse-mapping notes") hijack a topic.
        if heading == title or heading.startswith(title + " "):
            return body
    raise RuntimeError(f"Recipe section '{title}' was not found in {RECIPE_PATH}")


def render_reference(topic):
    """The reference as the paged text form the model reads."""
    value = read_reference(topic)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def reference_schema_index(topic, text=None):
    """Section index for a JSON reference, so the model can jump to what it needs.

    Each entry names the section, its size and the 0-based LINE where it starts, so the
    caller can fetch exactly that part with `reference_offset`; `feature_types` also lists
    the node kinds, so the planner sees the vocabulary without reading the body. Kept
    deliberately compact — the index rides along on every page, so its size is part of the
    page budget. Returns None for text references (a recipe section is read whole).
    """
    value = read_reference(topic)
    if not isinstance(value, dict):
        return None
    if text is None:
        text = render_reference(topic)
    starts = {}
    for position, line in enumerate(text.split("\n")):
        match = re.match(r'^  "([^"]+)":', line)
        if match:
            starts.setdefault(match.group(1), position)
    index = {"sections": []}
    for key, section in value.items():
        entry = {"section": key, "line": starts.get(key)}
        if isinstance(section, (dict, list)):
            entry["entries"] = len(section)
        index["sections"].append(entry)
        if key == "feature_types" and isinstance(section, dict):
            index["feature_types"] = [
                name for name in section if isinstance(name, str) and not name.startswith("_")
            ]
    return index


def _escaped_length(line):
    """How many characters `line` costs once embedded in a JSON string."""
    return len(json.dumps(line, ensure_ascii=False)) - 2


def _page(text, offset, limit, char_budget):
    """One page of `text`, plus the facts the caller needs to ask for the next one.

    A page is bounded by BOTH the line budget and the character budget, measured on the
    JSON-ESCAPED text (so the serialized page really fits the host's elision budget), and
    `limit` is clamped to the defaults — a page can never grow large enough to be elided.
    At least one line is always returned so paging always makes progress; a single line
    longer than the budget is returned cut, with `line_truncated: true`.
    """
    if limit is None:
        limit = REFERENCE_PAGE_LINES
    else:
        limit = max(1, min(int(limit), REFERENCE_PAGE_LINES))
    if offset is None:
        offset = 0
    offset = max(0, int(offset))
    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    page = []
    used = 0
    truncated_line = False
    for line in lines[offset:offset + limit]:
        cost = _escaped_length(line) + (0 if not page else 2)  # 2 = the escaped newline
        if not page:
            if cost > char_budget:
                # A single over-budget line still has to be returned whole to make progress.
                page.append(line)
                truncated_line = True
                break
            page.append(line)
            used = cost
            continue
        if used + cost > char_budget:
            break
        page.append(line)
        used += cost
    returned_lines = len(page)
    next_offset = offset + returned_lines
    has_more = next_offset < total_lines
    return {
        "content": "\n".join(page),
        "page": {
            "offset": offset,
            "limit": limit,
            "returned_lines": returned_lines,
            "total_lines": total_lines,
            "total_chars": total_chars,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "line_truncated": truncated_line,
        },
    }


def _serialized_size(page):
    """Characters the page costs as the compact single-line JSON the model receives."""
    return len(json.dumps(page, ensure_ascii=False, separators=(",", ":")))


def page_reference(topic, offset=None, limit=None):
    """One budget-bounded page of `topic`, with paging metadata and a section index.

    Two budgets are enforced, in this order:
      1. the CONTENT budget (~6000 escaped chars), so a page carries real information;
      2. the SERIALIZED cap (REFERENCE_PAGE_MAX_SERIALIZED = 7000), measured on the exact
         compact JSON the model receives, including the section index that rides along on
         every page. The cap is checked, not assumed: if the envelope + index + content
         would exceed it, trailing lines are given back to the next page until it fits.
         That keeps every page comfortably under the host's 8192-character prune threshold
         without hand-tuning the budget per topic.
    """
    text = render_reference(topic)
    index = reference_schema_index(topic, text)
    budget = REFERENCE_PAGE_CHARS
    if index is not None:
        # Reserve the index's own footprint so the content budget accounts for it up front.
        index_chars = len(json.dumps(index, ensure_ascii=False, separators=(",", ":")))
        budget = max(600, REFERENCE_PAGE_CHARS - index_chars - REFERENCE_PAGE_HEADROOM)
    result = _page(text, offset, limit, budget)
    if index is not None:
        result["page"]["index"] = index
    # Hard cap on the SERIALIZED page: hand lines back until it fits.
    while _serialized_size(result) > REFERENCE_PAGE_MAX_SERIALIZED:
        lines = result["content"].split("\n")
        if len(lines) <= 1:
            break  # one unbreakable line: it must be returned whole to make progress
        result = _page(text, offset, len(lines) - 1, budget)
        if index is not None:
            result["page"]["index"] = index
    return result

