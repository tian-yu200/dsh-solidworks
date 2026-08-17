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
        if heading == title or heading.startswith(title + " ") or title in heading:
            return body
    raise RuntimeError(f"Recipe section '{title}' was not found in {RECIPE_PATH}")
