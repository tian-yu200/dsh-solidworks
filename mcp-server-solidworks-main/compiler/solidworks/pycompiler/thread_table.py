"""ISO metric thread data — designations, the tap-drill table, and callout text.

WHY THIS MODULE EXISTS (and why it is data, not code): a tapped hole's pilot diameter is
NOT derivable from the designation. `D - P` (major minus pitch) is the folk formula and it is
WRONG on real standard drills — M8x1.25 would give 6.75 mm where the standard drill is 6.8,
and M12x1.75 would give 10.25 where the standard is 10.2. A compiler that recomputes the
pilot instead of looking it up silently accepts wrong drawings on exactly the sizes people
use most. So the pilot lives in a TABLE, the table carries its own provenance, and every
entry says whether a human has confirmed it.

PROVENANCE / ATTRIBUTION
  The tap-drill values, the "out-of-table sizes need an explicit pilot" rule and the
  `D - P`-is-not-enough warning come from the MIT-licensed reference repo
      https://github.com/wzyn20051216/solidworks-automation-skill
      Copyright (c) 2026 SolidWorks Automation Skill Contributors
      subskills/solidworks-threaded-holes/scripts/create_threaded_hole_template.py
      (THREAD_TABLE / THREAD_ALIASES / normalize_thread_key / resolve_thread_spec)
      and subskills/solidworks-threaded-holes/references/threaded-hole-lessons.md.
  See NOTICE.md. The values themselves are the published ISO 261 (general-purpose metric
  thread) / ISO 965-1 (tolerances) series and the customary ~75-80 % engagement drill chart;
  they are facts, not the repo's invention. Rows independently corroborated by that repo's own
  table are marked "repo-verified" below; the rest are marked "standard" and have NOT been
  checked against a physical copy of the standard inside this project.

MUST_BE_HUMAN_CONFIRMED
  The reference repo's own roadmap demands that every thread datum carry a standard revision,
  a source, a unit, a scope and a "must be human-confirmed" marker (its P2 item). This module
  keeps that promise in machine-readable form: `MUST_BE_HUMAN_CONFIRMED` is True for the whole
  table and `review_status()` answers per entry. The marker does NOT block compilation — a
  planner that copied "M6" off a drawing must still be able to build — it rides the lowering
  note and the node log so a reviewer sees which rows were taken on faith.

UNITS: the table is in MILLIMETRES (that is how thread standards are published, and converting
the table itself would invite rounding drift). Every function that hands a value to the IR
converts to METRES at the boundary, because the Feature Graph is SI metres everywhere (C4).
"""

# ---------------------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------------------

# (major_mm, pitch_mm) -> tap drill mm. Coarse series first, then the fine pitches these
# drawings need. Kept as a flat dict on purpose: a nested {major: {pitch: drill}} would hide
# the "this exact pair is not in the table" answer that PILOT_MISMATCH depends on.
TAP_DRILL_MM = {
    # --- coarse (ISO 261 first choice) -------------------------------------------------
    (1.6, 0.35): 1.25,
    (2.0, 0.4): 1.6,
    (2.5, 0.45): 2.05,
    (3.0, 0.5): 2.5,
    (4.0, 0.7): 3.3,
    (5.0, 0.8): 4.2,
    (6.0, 1.0): 5.0,
    (8.0, 1.25): 6.8,
    (10.0, 1.5): 8.5,
    (12.0, 1.75): 10.2,
    (14.0, 2.0): 12.0,
    (16.0, 2.0): 14.0,
    (18.0, 2.5): 15.5,
    (20.0, 2.5): 17.5,
    (22.0, 2.5): 19.5,
    (24.0, 3.0): 21.0,
    (27.0, 3.0): 24.0,
    (30.0, 3.5): 26.5,
    (33.0, 3.5): 29.5,
    (36.0, 4.0): 32.0,
    (39.0, 4.0): 35.0,
    (42.0, 4.5): 37.5,
    (45.0, 4.5): 40.5,
    (48.0, 5.0): 43.0,
    (52.0, 5.0): 47.0,
    (56.0, 5.5): 50.5,
    (60.0, 5.5): 54.5,
    (64.0, 6.0): 58.0,
    # --- fine (the pitches these drawings actually use, plus close neighbours) ----------
    (8.0, 1.0): 7.0,
    (10.0, 1.25): 8.8,
    (12.0, 1.25): 10.8,
    (12.0, 1.5): 10.5,
    (14.0, 1.5): 12.5,
    (16.0, 1.5): 14.5,
    (20.0, 1.5): 18.5,
    (20.0, 2.0): 18.0,
    (24.0, 2.0): 22.0,
    (30.0, 2.0): 28.0,
    (33.0, 2.0): 31.0,
    (36.0, 2.0): 34.0,
    (36.0, 3.0): 33.0,
    (39.0, 3.0): 36.0,
    (40.0, 2.0): 38.0,
    (40.0, 3.0): 37.0,
    (42.0, 2.0): 40.0,
    (42.0, 3.0): 39.0,
    (45.0, 2.0): 43.0,
    (45.0, 3.0): 42.0,
    (48.0, 2.0): 46.0,
    (48.0, 3.0): 45.0,
    (52.0, 2.0): 50.0,
    (52.0, 3.0): 49.0,
}

# The coarse pitch of each major diameter — what a bare "M6" means. Absent here (M7, M11,
# M13, M15, M17 ...) means "this is not a first-choice ISO metric size": the author must give
# the pitch explicitly, exactly as the reference repo's normalize_thread_key refuses to guess.
COARSE_PITCH_MM = {
    1.6: 0.35, 2.0: 0.4, 2.5: 0.45, 3.0: 0.5, 4.0: 0.7, 5.0: 0.8, 6.0: 1.0, 8.0: 1.25,
    10.0: 1.5, 12.0: 1.75, 14.0: 2.0, 16.0: 2.0, 18.0: 2.5, 20.0: 2.5, 22.0: 2.5, 24.0: 3.0,
    27.0: 3.0, 30.0: 3.5, 33.0: 3.5, 36.0: 4.0, 39.0: 4.0, 42.0: 4.5, 45.0: 4.5, 48.0: 5.0,
    52.0: 5.0, 56.0: 5.5, 60.0: 5.5, 64.0: 6.0,
}

# Rows the reference repo's own THREAD_TABLE corroborates (its table is M3-M12 coarse plus the
# M8x1 worked example). Everything else is the published standard but unverified in-project.
_REPO_VERIFIED = frozenset((
    (3.0, 0.5), (4.0, 0.7), (5.0, 0.8), (6.0, 1.0), (8.0, 1.25), (10.0, 1.5), (12.0, 1.75),
    (8.0, 1.0),
))

SOURCE = ("ISO 261 metric thread series / ISO 965-1 tolerances, customary ~75-80% engagement "
          "tap-drill chart; cross-checked against the MIT reference repo's THREAD_TABLE")
STANDARD_REVISION = "ISO 261:1998 / ISO 965-1:2013"

# The whole table is advisory until a human signs off on the rows a given build used. This is
# the reference repo's "必须人工确认" marker, kept as a module-level flag so it can never be
# silently dropped: thread_table.MUST_BE_HUMAN_CONFIRMED is asserted True by the test suite.
MUST_BE_HUMAN_CONFIRMED = True

# Tolerances used by the validation rules (metres). 0.15 mm is the "did you type the right
# drill?" window: it is wide enough to absorb a hand-rounded chart and narrow enough that a
# nominal diameter (5.0 vs 4.2 = 0.8 mm off) can never slip through.
PILOT_TOL_M = 0.00015
PILOT_TOL_EXPLICIT_M = 0.00005


def review_status(major_mm, pitch_mm):
    """"repo-verified" | "standard" — how much trust this row has earned in this project."""
    key = (round(float(major_mm), 4), round(float(pitch_mm), 4))
    return "repo-verified" if key in _REPO_VERIFIED else "standard"


# ---------------------------------------------------------------------------------------
# Designation parsing
# ---------------------------------------------------------------------------------------

def parse_designation(text):
    """'M6' / 'm6x1' / 'M5x0.8' / 'M42*2' -> (major_mm, pitch_mm_or_None, canonical).

    Returns pitch None when the text carries none (the caller then resolves the coarse pitch,
    and fails loudly if the size has no first-choice pitch). Raises ValueError with the
    offending text — validation turns that into an error string, lowering never sees it.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("designation must be a non-empty string like 'M6' or 'M5x0.8'")
    raw = text.strip().upper().replace(" ", "").replace("*", "X").replace("\u00d7", "X")
    if not raw.startswith("M"):
        raise ValueError("designation %r must start with 'M' (ISO metric only in v1)" % text)
    body = raw[1:]
    if not body:
        raise ValueError("designation %r has no size after 'M'" % text)
    major_text, _, pitch_text = body.partition("X")
    try:
        major = float(major_text)
    except ValueError:
        raise ValueError("designation %r: '%s' is not a number" % (text, major_text))
    if major <= 0:
        raise ValueError("designation %r: the major diameter must be > 0" % text)
    pitch = None
    if pitch_text:
        try:
            pitch = float(pitch_text)
        except ValueError:
            raise ValueError("designation %r: pitch '%s' is not a number" % (text, pitch_text))
        if pitch <= 0:
            raise ValueError("designation %r: the pitch must be > 0" % text)
    # The canonical `Size` needs a pitch; a bare 'M6' has none until the caller resolves the
    # coarse pitch, so it canonicalizes to 'M6' here and to 'M6x1' once resolved.
    canonical = "M%g" % major if pitch is None else canonical_size(major, pitch)
    return major, pitch, canonical


def canonical_size(major_mm, pitch_mm):
    """The `Size` string handed to SolidWorks, e.g. 'M5x0.8' / 'M6x1' / 'M42x2'.

    `%g` (not `%.1f`) so 1.0 mm prints '1' and 1.25 prints '1.25' — the compact form most
    thread databases use. It is matched as TEXT against the install's database row, so
    `size_forms()` also offers the trailing-zero spelling for the retry a null return invites.
    """
    return "M%gx%g" % (float(major_mm), float(pitch_mm))


def size_forms(major_mm, pitch_mm):
    """Candidate `Size` spellings, best first — the P12 probe order, for a human retry.

    SolidWorks matches `InsertCosmeticThread3`'s Size against a database row as TEXT. A
    localized Toolbox may spell M6x1 as 'M6x1.0' or 'M6'; a null return from the tool is the
    signal to try the next form (the compiler cannot retry for you: the tool takes ONE string).
    """
    major, pitch = float(major_mm), float(pitch_mm)
    forms = [canonical_size(major, pitch)]
    if abs(pitch - round(pitch)) < 1e-9:
        forms.append("M%gx%.1f" % (major, pitch))       # 'M6x1' -> 'M6x1.0'
    forms.append("M%g" % major)                          # 'M6'
    seen, out = set(), []
    for form in forms:
        if form not in seen:
            seen.add(form)
            out.append(form)
    return out


def resolve_pitch(major_mm, pitch_mm=None):
    """Explicit pitch wins; otherwise the coarse pitch for that major. Raises when neither."""
    if pitch_mm is not None:
        return float(pitch_mm)
    coarse = COARSE_PITCH_MM.get(round(float(major_mm), 4))
    if coarse is None:
        raise ValueError(
            "M%g has no first-choice coarse pitch — give 'pitch' explicitly (ISO metric only "
            "defines coarse pitches for the first-choice diameters)" % float(major_mm))
    return float(coarse)


def tap_drill_mm(major_mm, pitch_mm):
    """The table's pilot for this exact (major, pitch), or None when the pair is out of table."""
    return TAP_DRILL_MM.get((round(float(major_mm), 4), round(float(pitch_mm), 4)))


def tap_drill_m(major_mm, pitch_mm):
    """Same, in METRES (the IR unit), or None."""
    value = tap_drill_mm(major_mm, pitch_mm)
    return None if value is None else value / 1000.0


def format_mm(value_m):
    """Metres -> a drawing-style millimetre string, trailing zeros dropped ('0.010' -> '10')."""
    text = ("%.3f" % (float(value_m) * 1000.0)).rstrip("0").rstrip(".")
    return text or "0"


def format_dia_mm(value_m):
    """Metres -> a diameter string with at least one decimal ('0.005' -> '5.0')."""
    text = format_mm(value_m)
    return text if "." in text else text + ".0"


# ---------------------------------------------------------------------------------------
# Callout text
# ---------------------------------------------------------------------------------------

# The Note handed to InsertCosmeticThread3 IS what the drawing shows (the tool verifies it by
# reading ICosmeticThreadFeatureData.ThreadCallout back). ASCII is the default on purpose:
# a font that lacks the depth arrow draws a box, and a boxed glyph in a delivered drawing is
# worse than a plain word. 'unicode' is opt-in for a house style that has the glyphs.
CALLOUT_STYLES = frozenset(("ascii", "unicode"))


def callout_designation(major_mm, pitch_mm):
    """The designation a DRAWING shows: the pitch is omitted when it is the coarse pitch.

    ISO drawing practice writes 'M6' for a coarse thread and 'M8x1' for a fine one, and the
    sample drawings follow it ('M5-8H', 'M42x2', 'M40x2'). So the coarse pitch is dropped and a
    fine (or non-first-choice) pitch is kept — never the other way round: a fine thread printed
    as 'M8' would be tapped at 1.25 mm by anyone reading the drawing.
    """
    major = float(major_mm)
    coarse = COARSE_PITCH_MM.get(round(major, 4))
    if coarse is not None and abs(float(pitch_mm) - coarse) < 1e-9:
        return "M%g" % major
    return canonical_size(major, pitch_mm)


def derive_callout(designation, thread_class=None, depth=None, pilot=None,
                   pilot_depth=None, counterbore=None, style="ascii", handedness="right"):
    """Build the drawing callout for one threaded hole.

    ascii (default):  M5-8H DEPTH 10 / DIA 4.2 DEPTH 12.4
    unicode:          M5-8H ↧10 / Ø4.2 ↧12.4

    `designation` is what the DRAWING shows — pass `callout_designation(major, pitch)` so a
    coarse thread prints 'M6' and a fine one 'M8x1'. `depth` / `pilot_depth` are METRES;
    'through_all' passes None for that depth and the word THROUGH is used instead.
    `counterbore` is {diameter, depth} in metres and is appended last, because that is the order
    a drawing reads (thread, pilot, then the counterbore).
    """
    if style not in CALLOUT_STYLES:
        raise ValueError("callout style %r must be one of %s" % (style, sorted(CALLOUT_STYLES)))
    unicode_style = style == "unicode"
    depth_word = "\u21a7" if unicode_style else "DEPTH"
    dia = "\u00d8" if unicode_style else "DIA "

    parts = [designation]
    if thread_class:
        parts[0] = "%s-%s" % (designation, thread_class)
    if handedness == "left":
        parts[0] += " LH"

    if depth is None:
        parts.append("THROUGH")
    else:
        parts.append("%s %s" % (depth_word, format_mm(depth)))

    text = " ".join(parts)
    if pilot is not None:
        pilot_text = "%s%s" % (dia, format_dia_mm(pilot))
        if pilot_depth is None:
            pilot_text += " THROUGH"
        else:
            pilot_text += " %s %s" % (depth_word, format_mm(pilot_depth))
        text += " / " + pilot_text
    if counterbore:
        cb = "C'BORE %s%s %s %s" % (dia, format_dia_mm(counterbore["diameter"]),
                                     depth_word, format_mm(counterbore["depth"]))
        text += " " + cb
    return text
