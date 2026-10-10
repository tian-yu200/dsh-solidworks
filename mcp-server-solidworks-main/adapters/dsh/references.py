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

# Paging defaults for scope=reference. A reference is returned ONE PAGE AT A TIME so the
# result always survives the host's tool-result elision rule (which cuts a result over a
# line/character budget down to head + marker + tail, hiding the middle — that is what used
# to swallow the 498-line feature_graph_schema whole). The budget is deliberately well under
# every observed rule (host pruner: thresholdChars 8192 / headChars 4096 / tailChars 1024;
# conservative assumed rule: >20 lines may be elided).
REFERENCE_PAGE_LINES = 40
REFERENCE_PAGE_CHARS = 4000
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


def read_reference(topic):
    """The COMPLETE reference body for `topic` (unpaged).

    Kept as the single source of truth: `page_reference` slices its result, and the
    orchestrator's `reference` capability publishes it whole for programmatic callers.
    """
    if topic == "feature_graph_schema":
        return json.loads(FEATURE_GRAPH_SCHEMA_PATH.read_text(encoding="utf-8"))
    title = TOPICS.get(topic)
    if title is None:
        raise ValueError(
            "Unknown topic. Allowed: "
            + ", ".join(sorted([*TOPICS, "feature_graph_schema"]))
        )
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


def page_reference(topic, offset=None, limit=None):
    """One budget-bounded page of `topic`, with paging metadata and a section index."""
    text = render_reference(topic)
    index = reference_schema_index(topic, text)
    budget = REFERENCE_PAGE_CHARS
    if index is not None:
        # Reserve the index's own footprint so the SERIALIZED page stays under the budget.
        index_chars = len(json.dumps(index, ensure_ascii=False, separators=(",", ":")))
        budget = max(600, REFERENCE_PAGE_CHARS - index_chars - REFERENCE_PAGE_HEADROOM)
    result = _page(text, offset, limit, budget)
    if index is not None:
        result["page"]["index"] = index
    return result

