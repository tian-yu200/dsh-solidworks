"""Structural (plan-time) validation of a Feature Graph IR — v0.5 subset.

Validates STRUCTURE only (registered types, required params present, node references resolvable,
units correct, anchors well-formed) — NOT geometry. Geometric validity (does the top face exist,
does an anchor resolve to exactly one edge) is the compiler/resolver/execution's job at
compile/exec time (ADR-004). If this returns errors, the compiler performs ZERO execution calls.

v0.5 covered vocabulary (grown from the 1-1 bracket's recorded VOCABULARY_GAP, IR-ADR-007):
  box, sketch (profiles rectangle/circle/line/arc, construction flag, datum + signed offset,
  or — v0.5.5 — a face anchor for non-axis-aligned support), extrude (boss|cut; end
  blind/through_all/up_to_surface with a face anchor), hole-on-face (selector 'top', position
  'center', depth 'through_all'), fillet (radius + edge anchors), chamfer (distance_angle |
  distance_distance + edge anchors), loft (multi-profile boss over >= 2 sketch nodes), revolve,
  rib, circular_pattern, sheet_metal (base flange), sketched_bend, mirror (v0.5.5 — grown for
  4-1's sheet-metal enclosure), edge_flange (custom profile, v0.5.6 — 4-1's last gap).
v0.7 forward-vocabulary session (IR-ADR-017 — full part-tool-surface coverage, the
grow-from-real-parts mantra deliberately overridden once): sweep (profile = the immediately
preceding sketch, path = an earlier sketch node by id), linear_pattern (canonical axis x|y|z +
flip; single direction — the tool's D2 is unwired in C#), ellipse/spline profile primitives,
graph-level `material` (part graphs only, applied after the last node), edge_flange SIMPLE
length mode (length XOR frame+profile).
v0.8 (capability batch 4) adds `shell` — hollow an EXISTING body (a body_producer node) to a
uniform wall, leaving zero or more faces open. The open faces are FACE anchors on the body being
consumed, the same pre-consumption anchor rule fillet/chamfer use for edges.
Anything outside the covered set is rejected here with an explicit 'unsupported' message —
never silently ignored (recipe.md C5).

Contour closure (v0.7.3) is also plan-time: a sketch consumed by an extrude/revolve/
sheet_metal base flange/sweep PROFILE/loft profile must CLOSE. The lowering welds chain
endpoints within CONTOUR_WELD_TOL, so anything still open here is a real coordinate error —
reported with the exact gap before any execution call, instead of surfacing mid-build as an
EXTRUSION_FAILED with rollback (the 阀体 2026-08-17 failure class). rib/sketched_bend and
sweep-path sketches are open by design and never checked.
"""

import math

from . import contours
from . import thread_table

DATUMS = frozenset(("front", "top", "right"))
# Assembly sub-vocabulary (v0.6.0 — Phase B, ADR-047): component + mate. An assembly graph is
# components first (tree order) then mates (creation order); part and assembly node types never
# mix in one graph (a component references its part FILE — part IRs are not embedded).
ASSEMBLY_NODE_TYPES = frozenset(("component", "mate"))
MATE_TYPES = frozenset(("coincident", "concentric", "perpendicular", "parallel", "tangent",
                        "distance", "angle", "lock"))
MATE_ALIGNMENTS = frozenset(("aligned", "anti_aligned", "closest"))
# Anchor kinds mirror the reader's swMateEntity2ReferenceType_e names verbatim (C7). The kind
# decides WHERE the resolver searches: face kinds vs edge kinds.
ANCHOR_KINDS_FACE = frozenset(("plane", "cylinder", "cone", "sphere"))
ANCHOR_KINDS_EDGE = frozenset(("line", "circle"))
ANCHOR_KINDS = ANCHOR_KINDS_FACE | ANCHOR_KINDS_EDGE
NODE_TYPES = frozenset(("box", "sketch", "extrude", "revolve", "sweep", "rib", "hole", "fillet",
                        "chamfer", "shell", "loft", "linear_pattern", "circular_pattern",
                        "sheet_metal", "sketched_bend", "mirror", "edge_flange",
                        "cosmetic_thread")) \
    | ASSEMBLY_NODE_TYPES
PROFILE_KINDS = frozenset(("rectangle", "circle", "line", "arc", "ellipse", "spline",
                           "slot", "rounded_rect"))
# Optional boolean flags any profile primitive may carry (mirrors analyze_model's per-segment flag).
PROFILE_FLAGS = frozenset(("construction",))
FACE_SELECTORS = frozenset(("top",))          # v0.5: only 'top'
POSITIONS = frozenset(("center", "point"))       # 'point' + ref.at: batch 10a off-centre holes
THROUGH_DEPTHS = frozenset(("through_all", "through"))
EXTRUDE_ENDS = frozenset(("blind", "through_all", "up_to_surface", "mid_plane"))
# Canonical model axes for linear_pattern's direction (the tool selects the default plane whose
# normal is that axis; 'flip' patterns toward the negative axis).
PATTERN_DIRECTIONS = frozenset(("x", "y", "z"))
BODY_PRODUCERS = frozenset(("box", "extrude", "revolve", "sweep"))  # node types whose body a 'hole' may reference
# Node types that CREATE a named feature — valid seeds for a linear/circular pattern / mirror
# (the compiler substitutes the seed's runtime-created feature name). Patterns themselves are
# producers too: a grid composes as a pattern OF a pattern (the D2-less linear tool).
FEATURE_PRODUCERS = frozenset(("box", "extrude", "revolve", "sweep", "rib", "hole", "fillet", "chamfer",
                               "shell", "loft", "linear_pattern", "circular_pattern", "sheet_metal",
                               "sketched_bend", "mirror", "edge_flange", "cosmetic_thread"))
CHAMFER_TYPES = frozenset(("distance_angle", "distance_distance"))
# --- Threads (v0.9.0) ---------------------------------------------------------------------
# The COSMETIC representation is the only one wired end to end: it is an annotation the drawing
# reads and it changes no topology, so it is the safe default. 'real' and 'auto' are RESERVED
# vocabulary — accepted by the schema (so a graph can record the intent) and REJECTED by
# validation with an explicit message, because the real (cut) Thread path needs a Metric
# Tap.SLDLFP profile library, a different selection mark (1, not 0) and a different end-condition
# enum. Rejecting is the module's C5 rule: never silently ignore, never silently substitute.
THREAD_REPRESENTATIONS = frozenset(("cosmetic", "real", "auto"))
THREAD_REPRESENTATION_IMPLEMENTED = frozenset(("cosmetic",))
THREAD_SERIES = frozenset(("ISO_metric",))
# ISO 965-1 internal tolerance classes: grades 4-8 in positions G (loose) and H (basic).
THREAD_CLASSES = frozenset(("4H", "5H", "6H", "7H", "8H", "4G", "5G", "6G", "7G", "8G"))
# Drawing-standard family -> the swCosmeticStandardType_e enum the tool takes. Only the metric
# internal families are offered: an inch/pipe standard would need its own Size strings AND its
# own tap-drill data, and the reference repo's roadmap explicitly refuses to reuse Metric Tap.
THREAD_STANDARDS = frozenset(("ISO", "GB", "DIN", "JIS"))
THREAD_STANDARD_ENUM = {"ISO": 8, "GB": 13, "DIN": 4, "JIS": 9}
THREAD_HANDEDNESS = frozenset(("right", "left"))
# Node types whose geometry can carry a cosmetic thread (a bore, i.e. an internal cylinder):
# a drilled hole, or a revolve/sweep/extrude CUT. Deliberately NOT box/rib/loft: those produce
# no bore the thread could sit on, and accepting them would let a graph validate and then fail
# at resolve time with a confusing REFERENCE_UNRESOLVED.
THREADABLE_PRODUCERS = frozenset(("hole", "extrude", "revolve", "sweep"))
BEND_POSITIONS = frozenset(("centerline", "material_inside", "material_outside", "bend_outside"))
# Edge flange adds 'bend_sharp' (swFlangePositionTypes_e 5) to the shared position set.
EDGE_FLANGE_POSITIONS = BEND_POSITIONS | frozenset(("bend_sharp",))


def _is_pos_number(v):
    # isfinite as well as > 0: +inf satisfies `v > 0`, so it must be excluded explicitly.
    # Only FLOATS can be non-finite: an int is always finite, and math.isfinite() on an int
    # beyond float range (10**400 — json.loads happily produces those) raises OverflowError
    # instead of answering, so a plain int must never be handed to it.
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and (not isinstance(v, float) or math.isfinite(v)) and v > 0)


def _is_number(v):
    # isfinite rejects NaN and +/-Inf: JSON allows them only as non-standard literals (Python's
    # json.loads ACCEPTS NaN/Infinity/-Infinity by default), and an infinite coordinate would be
    # emitted verbatim into the execution layer's params. NaN is worse than inf: every comparison
    # against it is False, so a NaN endpoint silently counts as "paired" in the closure walk.
    # Ints are always finite and NEVER go through math.isfinite (a huge int raises
    # OverflowError there) — only floats can be NaN/Inf.
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and (not isinstance(v, float) or math.isfinite(v)))


def _is_point3(v):
    return isinstance(v, list) and len(v) == 3 and all(_is_number(c) for c in v)


def _normalize_class(value):
    """The canonical tolerance class, or None when it is not a known INTERNAL class.

    Only the exact uppercase spelling is accepted. Lowercase is NOT a case variant here: ISO 965
    writes internal thread classes in UPPERCASE (6H) and EXTERNAL ones in lowercase (6g, 6h).
    Upper-casing first (as this function used to) turned an external `6h` into the internal `6H`
    and silently accepted a graph that asked for something v1 cannot build — the callout then
    claimed an internal tolerance on a hole nobody had validated. So lowercase is rejected, and
    the caller's message explains why (see _class_error).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text in THREAD_CLASSES else None


def _class_error(value):
    """The message for a rejected `class`, distinguishing an EXTERNAL class from a typo.
    Returns None when `value` is a valid internal class (nothing to report).
    """
    if _normalize_class(value) is not None:
        return None
    text = value.strip() if isinstance(value, str) else value
    if isinstance(text, str) and text.lower() in {c.lower() for c in THREAD_CLASSES}:
        # Right number, wrong case: that is an EXTERNAL thread class (ISO 965-1 writes internal
        # classes uppercase, external ones lowercase).
        return ("%r is an EXTERNAL thread tolerance class (ISO 965-1 writes external "
                "classes in lowercase: 6g, 6h). v1 supports INTERNAL threads only — pass the "
                "uppercase internal class %r, or omit 'class' to leave it off the callout."
                % (value, text.upper()))
    return ("%r is not an ISO 965-1 internal tolerance class (one of %s)."
            % (value, sorted(THREAD_CLASSES)))


def _resolve_thread_size(designation, pitch):
    """(major_m, pitch_m, tap_drill_m or None, pitch_given) for a designation, in METRES.

    Raises ValueError with a human message on anything unresolvable — the caller turns that
    into an error string. A bare 'M6' resolves its coarse pitch from the table; an explicit
    pitch always wins (that is how a fine pitch is declared). `pitch_given` says whether a
    pitch came from ANYWHERE (the `pitch` field or the designation text), which is what decides
    between "you must state a pitch" and "you must state a tap_drill" for an out-of-table size.
    """
    major_mm, parsed_pitch, _canonical = thread_table.parse_designation(designation)
    effective = pitch if pitch is not None else parsed_pitch
    pitch_mm = thread_table.resolve_pitch(major_mm, effective)
    return (major_mm / 1000.0, pitch_mm / 1000.0,
            thread_table.tap_drill_m(major_mm, pitch_mm), effective is not None)


def _check_thread_block(block, label, errors, declared_pilot=None, declared_pilot_depth=None,
                        where="thread"):
    """Shared validation for a thread declaration (hole.thread / a cosmetic_thread node).

    `declared_pilot` (metres) is the hole's `diameter` when the block lives on a hole; for a
    standalone cosmetic_thread node there is no pilot to cross-check, so pass None and only the
    table-independent rules run. Returns the resolved (major_m, pitch_m) or None when the block
    could not be resolved — the caller must not use it then.
    """
    if not isinstance(block, dict):
        errors.append("%s: '%s' must be an object." % (label, where))
        return None

    desig = block.get("designation")
    if not isinstance(desig, str) or not desig.strip():
        errors.append("%s.%s: 'designation' is required — an ISO metric size like 'M6' or "
                      "'M42x2'." % (label, where))
        return None

    series = block.get("series")
    if series is not None and series not in THREAD_SERIES:
        errors.append("%s.%s: 'series' %r unsupported (only %s in v1 — no UNC/UNF/NPT/BSP; "
                      "an inch or pipe thread must not reuse the metric Size table)."
                      % (label, where, series, sorted(THREAD_SERIES)))

    standard = block.get("standard")
    if standard is not None and standard not in THREAD_STANDARDS:
        errors.append("%s.%s: 'standard' %r unsupported (only %s; each maps to a "
                      "swCosmeticStandardType_e member)."
                      % (label, where, standard, sorted(THREAD_STANDARDS)))

    representation = block.get("representation")
    if representation is not None:
        if representation not in THREAD_REPRESENTATIONS:
            errors.append("%s.%s: 'representation' %r unsupported (only %s)."
                          % (label, where, representation, sorted(THREAD_REPRESENTATIONS)))
        elif representation not in THREAD_REPRESENTATION_IMPLEMENTED:
            errors.append("%s.%s: representation '%s' is RESERVED, not implemented — v1 wires "
                          "only 'cosmetic' (an annotation the drawing reads, zero topology "
                          "change). A real (cut) thread needs the Metric Tap.SLDLFP profile "
                          "library, selection mark 1 and swThreadEndCondition_e; write "
                          "representation 'cosmetic' or omit it (the default)."
                          % (label, where, representation))

    handedness = block.get("handedness")
    if handedness is not None and handedness not in THREAD_HANDEDNESS:
        errors.append("%s.%s: 'handedness' %r unsupported (only %s)."
                      % (label, where, handedness, sorted(THREAD_HANDEDNESS)))

    thread_class = block.get("class")
    if thread_class is not None and _normalize_class(thread_class) is None:
        errors.append("%s.%s: %s" % (label, where, _class_error(thread_class)))

    if block.get("internal") is False:
        errors.append("%s.%s: external threads ('internal': false) are NOT supported in v1 — "
                      "InsertCosmeticThread3's DiameterType for a boss is the MINOR diameter "
                      "and the whole tap-drill cross-check inverts. Remove 'internal' or set it "
                      "true." % (label, where))

    if block.get("callout") is not None and not isinstance(block.get("callout"), str):
        errors.append("%s.%s: 'callout' must be a string when present." % (label, where))
    if block.get("callout_style") is not None and block.get("callout_style") not in thread_table.CALLOUT_STYLES:
        errors.append("%s.%s: 'callout_style' %r unsupported (only %s)."
                      % (label, where, block.get("callout_style"),
                         sorted(thread_table.CALLOUT_STYLES)))

    # depth: 'through_all' or a finite positive length. Absent means "follow the hole" — but
    # only for a THROUGH hole, where the thread is through as well. A blind hole with a thread
    # and no thread depth is ambiguous (does the thread stop at the floor or 2 mm above it?),
    # and the drawing always states it, so demand it rather than guessing.
    depth = block.get("depth")
    if depth is not None and depth not in THROUGH_DEPTHS and not _is_pos_number(depth):
        errors.append("%s.%s: 'depth' must be a positive number of meters (blind thread length) "
                      "or 'through_all' (got %r)." % (label, where, depth))
    elif depth is None and _is_pos_number(declared_pilot_depth):
        errors.append("%s.%s: 'depth' is required for a thread in a BLIND hole (the pilot is "
                      "%s mm deep) — state the thread depth in meters, or 'through_all' if the "
                      "thread runs the whole pilot."
                      % (label, where, thread_table.format_mm(float(declared_pilot_depth))))

    # --- resolve the size -------------------------------------------------------------
    pitch = block.get("pitch")
    if pitch is not None and not _is_pos_number(pitch):
        errors.append("%s.%s: 'pitch' must be a positive number of meters (e.g. 0.002 for 2 mm)."
                      % (label, where))
        return None
    try:
        major_m, pitch_m, tap_m, pitch_given = _resolve_thread_size(desig, pitch)
    except ValueError as ex:
        errors.append("%s.%s: %s" % (label, where, ex))
        return None

    nominal = block.get("nominal")
    if nominal is not None and not _is_pos_number(nominal):
        errors.append("%s.%s: 'nominal' must be a positive number of meters (the MAJOR "
                      "diameter the cosmetic thread is drawn on)." % (label, where))
    elif nominal is not None and abs(float(nominal) - major_m) > 1e-6:
        errors.append("%s.%s: 'nominal' %g mm contradicts the designation %r (%g mm) — the "
                      "cosmetic thread's Diameter is the NOMINAL MAJOR diameter."
                      % (label, where, float(nominal) * 1000.0, desig, major_m * 1000.0))

    # --- the tap-drill rule: the whole reason thread_table.py exists -------------------
    explicit_pilot = block.get("tap_drill")
    if explicit_pilot is not None and not _is_pos_number(explicit_pilot):
        errors.append("%s.%s: 'tap_drill' must be a positive number of meters." % (label, where))
        explicit_pilot = None

    if tap_m is None and not pitch_given:
        errors.append("%s.%s: %r is not in the ISO metric tap-drill table and no 'pitch' was "
                      "given — an out-of-table size needs BOTH an explicit 'pitch' and an "
                      "explicit 'tap_drill' (the pilot cannot be guessed; D-P is not the "
                      "standard drill)." % (label, where, desig))
        return major_m, pitch_m
    if tap_m is None and explicit_pilot is None:
        errors.append("%s.%s: M%gx%g is not in the tap-drill table — give 'tap_drill' "
                      "explicitly (meters). The table covers the ISO first-choice coarse and "
                      "fine series; anything else must state its pilot."
                      % (label, where, major_m * 1000.0, pitch_m * 1000.0))
        return major_m, pitch_m

    if declared_pilot is not None:
        if tap_m is not None:
            delta = abs(float(declared_pilot) - tap_m)
            if delta > thread_table.PILOT_TOL_M:
                errors.append("%s: PILOT_MISMATCH — diameter is %s mm but %r taps at %s mm "
                              "(table, ISO 261/965-1; off by %s mm > %g mm). Either correct "
                              "'diameter' or state an explicit 'tap_drill' if this is a "
                              "form-tapped or non-standard pilot."
                              % (label, thread_table.format_dia_mm(float(declared_pilot)), desig,
                                 thread_table.format_dia_mm(tap_m),
                                 thread_table.format_dia_mm(delta),
                                 thread_table.PILOT_TOL_M * 1000.0))
        elif explicit_pilot is not None:
            delta = abs(float(declared_pilot) - float(explicit_pilot))
            if delta > thread_table.PILOT_TOL_EXPLICIT_M:
                errors.append("%s: PILOT_MISMATCH — diameter is %s mm but 'tap_drill' is %s mm "
                              "(off by %s mm > %g mm)."
                              % (label, thread_table.format_dia_mm(float(declared_pilot)),
                                 thread_table.format_dia_mm(float(explicit_pilot)),
                                 thread_table.format_dia_mm(delta),
                                 thread_table.PILOT_TOL_EXPLICIT_M * 1000.0))
        # The floor the reference repo enforces: a pilot at or above the major is not a thread.
        if float(declared_pilot) >= major_m:
            errors.append("%s: diameter %s mm is not smaller than the %r major diameter %s mm — "
                          "a tapped hole is drilled UNDER the nominal size."
                          % (label, thread_table.format_dia_mm(float(declared_pilot)), desig,
                             thread_table.format_dia_mm(major_m)))

    # Thread depth must fit inside the pilot: a thread cannot be longer than the hole.
    if declared_pilot_depth is not None and _is_pos_number(declared_pilot_depth):
        if depth is not None and depth not in THROUGH_DEPTHS:
            if float(depth) > float(declared_pilot_depth) + 1e-9:
                errors.append("%s.%s: thread 'depth' %s mm exceeds the pilot depth %s mm — the "
                              "thread cannot run past the bottom of the hole."
                              % (label, where, thread_table.format_mm(float(depth)),
                                 thread_table.format_mm(float(declared_pilot_depth))))

    # Engagement sanity: 75-85 % is the chart's band; outside 70-90 % is accepted with the
    # table's warning only when the pilot was stated explicitly (a form tap legitimately runs
    # a larger pilot). The hard numbers are checked above; this is a documented non-error.
    return major_m, pitch_m


def validate(graph):
    """Return a list of human-readable error strings ([] == structurally valid)."""
    errors = []
    if not isinstance(graph, dict):
        return ["graph must be a JSON object."]

    if graph.get("units") != "meters":
        errors.append("units must be 'meters' (got %r)." % graph.get("units"))

    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0:
        errors.append("graph.nodes must be a non-empty array.")
        return errors  # nothing more to check without nodes

    # Part vs assembly vocabularies never mix in one graph: an assembly's parts are separate
    # FILES (verified by their own V1 loop, ADR-047b), never inline sub-graphs.
    kinds = set()
    for node in nodes:
        if isinstance(node, dict) and node.get("type") in NODE_TYPES:
            kinds.add("assembly" if node.get("type") in ASSEMBLY_NODE_TYPES else "part")
    if len(kinds) > 1:
        errors.append("graph mixes part-vocabulary and assembly-vocabulary nodes — an assembly "
                      "IR references part FILES (component.source), it never embeds part nodes.")

    # Graph-level material (v0.7.0): document state, not a tree feature — PART graphs only.
    material = graph.get("material")
    if material is not None:
        if not isinstance(material, dict) or not isinstance(material.get("name"), str) \
                or not material.get("name"):
            errors.append("graph.material must be {name: '<exact library material name>', "
                          "library?: '<database>'} when present.")
        elif material.get("library") is not None and not isinstance(material.get("library"), str):
            errors.append("graph.material.library must be a string when present.")
        if "assembly" in kinds:
            errors.append("graph.material is PART-only — set_part_material requires a part "
                          "document, not an assembly.")

    # Graph-level thread defaults (v0.9.0): DOCUMENT state, exactly like `material` — a drawing
    # whose holes are all ISO 6H should not repeat that on every node. Node-level fields win;
    # these only fill what a node leaves out. Prose keys are the vocabulary the node fields use,
    # so a bad default is caught here rather than silently propagating into every node.
    thread_defaults = graph.get("thread_defaults")
    if thread_defaults is not None:
        if not isinstance(thread_defaults, dict):
            errors.append("graph.thread_defaults must be an object "
                          "{series?, standard?, class?, handedness?, representation?, "
                          "callout_style?} when present.")
        else:
            if "assembly" in kinds:
                errors.append("graph.thread_defaults is PART-only — a cosmetic thread is a "
                              "feature of a part document, not of an assembly.")
            allowed = ("series", "standard", "class", "handedness", "representation",
                       "callout_style")
            for key in sorted(thread_defaults):
                if key not in allowed:
                    errors.append("graph.thread_defaults.%s is not a thread default (allowed: %s)."
                                  % (key, ", ".join(allowed)))
            series = thread_defaults.get("series")
            if series is not None and series not in THREAD_SERIES:
                errors.append("graph.thread_defaults.series %r unsupported (only %s)."
                              % (series, sorted(THREAD_SERIES)))
            standard = thread_defaults.get("standard")
            if standard is not None and standard not in THREAD_STANDARDS:
                errors.append("graph.thread_defaults.standard %r unsupported (only %s)."
                              % (standard, sorted(THREAD_STANDARDS)))
            default_class = thread_defaults.get("class")
            if default_class is not None and _normalize_class(default_class) is None:
                errors.append("graph.thread_defaults.class: %s" % _class_error(default_class))
            handedness = thread_defaults.get("handedness")
            if handedness is not None and handedness not in THREAD_HANDEDNESS:
                errors.append("graph.thread_defaults.handedness %r unsupported (only %s)."
                              % (handedness, sorted(THREAD_HANDEDNESS)))
            representation = thread_defaults.get("representation")
            if representation is not None and representation not in THREAD_REPRESENTATIONS:
                errors.append("graph.thread_defaults.representation %r unsupported (only %s)."
                              % (representation, sorted(THREAD_REPRESENTATIONS)))
            elif representation is not None \
                    and representation not in THREAD_REPRESENTATION_IMPLEMENTED:
                errors.append("graph.thread_defaults.representation '%s' is RESERVED, not "
                              "implemented — v1 wires only 'cosmetic'." % representation)
            style = thread_defaults.get("callout_style")
            if style is not None and style not in thread_table.CALLOUT_STYLES:
                errors.append("graph.thread_defaults.callout_style %r unsupported (only %s)."
                              % (style, sorted(thread_table.CALLOUT_STYLES)))

    seen = {}  # id -> type (in declaration order)
    seen_mate = False
    for pos, node in enumerate(nodes):
        where = "nodes[%d]" % pos
        if not isinstance(node, dict):
            errors.append("%s must be an object." % where)
            continue

        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append("%s.id is required (non-empty string)." % where)
            nid = None
        elif nid in seen:
            errors.append("%s.id '%s' is duplicated." % (where, nid))

        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            errors.append("%s.type '%s' is not a registered v0.5 type %s."
                          % (where, ntype, sorted(NODE_TYPES)))
            if nid:
                seen[nid] = ntype
            continue

        label = "%s (%s '%s')" % (where, ntype, nid)

        if ntype == "box":
            _check_datum(node, label, errors)
            for k in ("width", "depth", "height"):
                if not _is_pos_number(node.get(k)):
                    errors.append("%s: '%s' must be a positive number (meters)." % (label, k))

        elif ntype == "sketch":
            _check_datum(node, label, errors)
            profile = node.get("profile")
            if not isinstance(profile, list) or len(profile) == 0:
                errors.append("%s: 'profile' must be a non-empty array of primitives." % label)
            else:
                for j, prim in enumerate(profile):
                    _check_profile_prim(prim, "%s.profile[%d]" % (label, j), errors)
            frame = node.get("frame")
            if frame is not None:
                if (not isinstance(frame, dict)
                        or not all(_is_point3(frame.get(k)) for k in ("origin", "xdir", "ydir"))):
                    errors.append("%s: 'frame' must be {origin:[x,y,z], xdir:[x,y,z], ydir:[x,y,z]} "
                                  "— the ORIGINAL sketch's frame from analyze_model(sketch)." % label)

        elif ntype == "extrude":
            sk = node.get("sketch")
            prev = nodes[pos - 1] if pos > 0 else None
            if not isinstance(sk, str) or not sk:
                errors.append("%s: 'sketch' (id of a sketch node) is required." % label)
            elif seen.get(sk) != "sketch":
                errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
            elif not (isinstance(prev, dict) and prev.get("id") == sk):
                # An extrude consumes the ACTIVE sketch, so it must immediately follow its sketch.
                errors.append("%s: an extrude must immediately follow its sketch node '%s'." % (label, sk))
            op = node.get("operation")
            if op not in ("boss", "cut"):
                errors.append("%s: 'operation' must be 'boss' or 'cut' (got %r)." % (label, op))
            if node.get("reversed") is not None and not isinstance(node.get("reversed"), bool):
                errors.append("%s: 'reversed' must be a boolean (the recipe's flipped-direction flag)." % label)
            _check_extrude_end(node, op, label, errors)

        elif ntype == "hole":
            ref = node.get("ref") or {}
            nf = ref.get("node_face") or {}
            tgt = nf.get("node")
            if not isinstance(tgt, str) or not tgt:
                errors.append("%s: ref.node_face.node (an earlier node id) is required." % label)
            elif seen.get(tgt) not in BODY_PRODUCERS:
                errors.append("%s: ref.node_face.node '%s' must reference an earlier box/extrude node." % (label, tgt))
            if nf.get("selector") not in FACE_SELECTORS:
                errors.append("%s: ref.node_face.selector %r unsupported (only %s)."
                              % (label, nf.get("selector"), sorted(FACE_SELECTORS)))
            # --- placement (batch 10a) — off-centre holes ---------------------------------
            # `position: 'point'` + `at: [x,y,z]` (MODEL space, metres) puts the hole anywhere on
            # the referenced face instead of its centre. The 3-D point is mapped into the FACE
            # SKETCH's own 2-D frame at compile time (after create_sketch echoes the measured
            # frame), so a face whose sketch x-axis is not model X needs no author guesswork.
            # Plan-time checks here are structural only; the on-face/inside-face tests run
            # against the resolved face in the compiler (they need the live plane).
            position = ref.get("position")
            if position not in POSITIONS:
                errors.append("%s: ref.position %r unsupported (only %s)."
                              % (label, position, sorted(POSITIONS)))
            at = ref.get("at")
            if position == "point":
                if not _is_point3(at):
                    errors.append("%s: ref.at must be a 3-D point [x, y, z] in METERS when "
                                  "ref.position is 'point' (got %r)." % (label, at))
            elif at is not None:
                errors.append("%s: ref.at is only read when ref.position is 'point' "
                              "(got position %r)." % (label, position))
            if not _is_pos_number(node.get("diameter")):
                errors.append("%s: 'diameter' must be a positive number (meters)." % label)
            # depth is either the through-all token or a BLIND hole depth in metres
            # (finite, > 0) — the same positive-number rule as 'diameter'.
            depth = node.get("depth")
            if depth not in THROUGH_DEPTHS and not _is_pos_number(depth):
                errors.append("%s: 'depth' must be a positive number of meters (blind hole) or "
                              "'through_all' (got %r)." % (label, depth))

            # --- counterbore (v0.9.0) — the M6 counterbored holes -------------------------
            # A coaxial counterbore is cut FIRST by the lowering (see lowering.lower_hole): it
            # changes the entry face, and cutting it after the pilot would re-intersect the
            # already-threaded mouth edge, dropping the cosmetic thread (a subfeature of the
            # cut). `hole.depth` is measured from the COUNTERBORE FLOOR when present, so the
            # pilot's absolute cut depth is counterbore.depth + hole.depth.
            counterbore = node.get("counterbore")
            if counterbore is not None:
                if not isinstance(counterbore, dict):
                    errors.append("%s: 'counterbore' must be an object {diameter, depth}." % label)
                else:
                    cb_dia = counterbore.get("diameter")
                    cb_depth = counterbore.get("depth")
                    if not _is_pos_number(cb_dia):
                        errors.append("%s.counterbore: 'diameter' must be a positive number "
                                      "(meters)." % label)
                    elif _is_pos_number(node.get("diameter")) \
                            and float(cb_dia) <= float(node["diameter"]):
                        errors.append("%s.counterbore: 'diameter' %s mm must be LARGER than the "
                                      "hole diameter %s mm — a counterbore is the wider, "
                                      "shallower cut at the entry."
                                      % (label, thread_table.format_dia_mm(float(cb_dia)),
                                         thread_table.format_dia_mm(float(node["diameter"]))))
                    if not _is_pos_number(cb_depth):
                        errors.append("%s.counterbore: 'depth' must be a positive number "
                                      "(meters)." % label)
                    elif _is_pos_number(depth) and float(cb_depth) >= float(depth):
                        errors.append("%s.counterbore: 'depth' %s mm must be SHALLOWER than the "
                                      "hole depth %s mm (measured from the counterbore floor)."
                                      % (label, thread_table.format_mm(float(cb_depth)),
                                         thread_table.format_mm(float(depth))))
                    for key in sorted(counterbore):
                        if key not in ("diameter", "depth"):
                            errors.append("%s.counterbore.%s is not a counterbore field "
                                          "(allowed: diameter, depth — a spot-face or countersink "
                                          "is not modelled in v1)." % (label, key))

            # --- thread (v0.9.0) — the M5-8H style tapped hole ---------------------------
            thread = node.get("thread")
            if thread is not None:
                if thread.get("depth") is None and depth in THROUGH_DEPTHS:
                    # A tapped THROUGH hole needs no thread depth (it is through); a blind one
                    # does, and _check_thread_block reports it.
                    pass
                _check_thread_block(thread, label, errors,
                                    declared_pilot=node.get("diameter"),
                                    declared_pilot_depth=depth, where="thread")

        elif ntype == "cosmetic_thread":
            # A thread on a bore that already exists (v0.9.0) — the M42x2 / M40x2 valve-body
            # bores come from `revolve` cuts, so there is no `hole` node to hang a `thread`
            # block on. One node, one op: add_cosmetic_thread on a resolver-matched circular
            # edge. It consumes NO geometry (a cosmetic thread adds zero faces/edges), so its
            # ref is resolved against the geometry as it stands — no pre-consumption rule.
            ref = node.get("ref")
            if not isinstance(ref, dict):
                errors.append("%s: 'ref' is required — {node, edge_anchor|bore_face}." % label)
                ref = {}
            tgt = ref.get("node")
            if not isinstance(tgt, str) or not tgt:
                errors.append("%s: ref.node (an earlier node id) is required — the node that "
                              "created the bore." % label)
            elif seen.get(tgt) not in THREADABLE_PRODUCERS:
                errors.append("%s: ref.node '%s' must be an earlier %s node (a node whose "
                              "geometry can carry a bore)."
                              % (label, tgt, "/".join(sorted(THREADABLE_PRODUCERS))))
            edge_anchor = ref.get("edge_anchor")
            bore_face = ref.get("bore_face")
            if edge_anchor is None and bore_face is None:
                errors.append("%s: exactly ONE of ref.edge_anchor / ref.bore_face is required — "
                              "the circular edge at the bore mouth (exact, from "
                              "analyze_model(edges)) or the cylindrical face to derive it from."
                              % label)
            elif edge_anchor is not None and bore_face is not None:
                errors.append("%s: ref.edge_anchor and ref.bore_face are mutually exclusive — "
                              "give the mouth edge, or the bore face it is derived from." % label)
            if edge_anchor is not None:
                if not isinstance(edge_anchor, dict) or not _is_point3(edge_anchor.get("near")):
                    errors.append("%s: ref.edge_anchor must be {near:[x,y,z], radius?, hint?} — "
                                  "`near` is the mouth circle's CENTRE (analyze_model(edges) "
                                  "reports it as `center` for kind='circle'), NOT a point on the "
                                  "rim and NOT an edge midpoint." % label)
                else:
                    # radius is a DISAMBIGUATOR: a bore mouth can be concentric and coplanar with
                    # an outer rim (a revolve end face) or with a counterbore-floor rim, and only
                    # the radius tells them apart.
                    anchor_radius = edge_anchor.get("radius")
                    if anchor_radius is not None and not _is_pos_number(anchor_radius):
                        errors.append("%s: ref.edge_anchor.radius must be a positive number of "
                                      "meters (it separates the mouth from a concentric rim)."
                                      % label)
                    if edge_anchor.get("hint") is not None \
                            and not isinstance(edge_anchor.get("hint"), str):
                        errors.append("%s: ref.edge_anchor.hint must be a string when present."
                                      % label)
                    for key in sorted(edge_anchor):
                        if key not in ("near", "radius", "hint"):
                            errors.append("%s: ref.edge_anchor.%s is not an edge-anchor field "
                                          "(allowed: near, radius, hint)." % (label, key))
            if bore_face is not None:
                if not isinstance(bore_face, dict) or not _is_point3(bore_face.get("near")):
                    errors.append("%s: ref.bore_face must be {near:[x,y,z], radius?, hint?} — "
                                  "a point ON the bore's cylindrical surface." % label)
                else:
                    radius = bore_face.get("radius")
                    if radius is not None and not _is_pos_number(radius):
                        errors.append("%s: ref.bore_face.radius must be a positive number of "
                                      "meters (it pins WHICH coaxial cylinder is meant)."
                                      % label)
                    for key in sorted(bore_face):
                        if key not in ("near", "radius", "hint"):
                            errors.append("%s: ref.bore_face.%s is not a bore-face field "
                                          "(allowed: near, radius, hint)." % (label, key))
            # The thread block IS the node body: designation/depth/class/... live at the top
            # level, so the same validator serves hole.thread and this node unchanged.
            _check_thread_block(node, label, errors, where="cosmetic_thread")
            for key in ("edge_index", "diameter"):
                if node.get(key) is not None:
                    errors.append("%s: '%s' is not a cosmetic_thread field — the edge is "
                                  "resolved from ref (never given as an index in the IR), and "
                                  "the Diameter is the designation's nominal major."
                                  % (label, key))

        elif ntype == "revolve":
            sk = node.get("sketch")
            prev = nodes[pos - 1] if pos > 0 else None
            if not isinstance(sk, str) or not sk:
                errors.append("%s: 'sketch' (id of a sketch node) is required." % label)
            elif seen.get(sk) != "sketch":
                errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
            elif not (isinstance(prev, dict) and prev.get("id") == sk):
                # Like extrude: a revolve consumes the ACTIVE sketch (the executor resolves the axis
                # against the sketch's own segments, which only works while the sketch is open).
                errors.append("%s: a revolve must immediately follow its sketch node '%s'." % (label, sk))
            axis = node.get("axis")
            if not isinstance(axis, dict) or not all(_is_number(axis.get(k)) for k in ("x1", "y1", "x2", "y2")):
                errors.append("%s: 'axis' must be {x1, y1, x2, y2} — the axis SEGMENT's exact 2D "
                              "sketch coords (normally the construction centerline)." % label)
            elif axis.get("x1") == axis.get("x2") and axis.get("y1") == axis.get("y2"):
                errors.append("%s: axis start and end coincide — not a segment." % label)
            angle = node.get("angle")
            if angle is not None and not _is_pos_number(angle):
                errors.append("%s: 'angle' must be a positive number (RADIANS; omit for a full 360°)." % label)
            # operation is optional; omitting it keeps the historical boss behaviour.
            if node.get("operation", "boss") not in ("boss", "cut"):
                errors.append("%s: 'operation' must be 'boss' or 'cut' (got %r; omit for boss)."
                              % (label, node.get("operation")))

        elif ntype == "rib":
            sk = node.get("sketch")
            prev = nodes[pos - 1] if pos > 0 else None
            if not isinstance(sk, str) or not sk:
                errors.append("%s: 'sketch' (id of a sketch node) is required." % label)
            elif seen.get(sk) != "sketch":
                errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
            elif not (isinstance(prev, dict) and prev.get("id") == sk):
                errors.append("%s: a rib must immediately follow its sketch node '%s'." % (label, sk))
            if not _is_pos_number(node.get("thickness")):
                errors.append("%s: 'thickness' must be a positive number (meters)." % label)
            for k in ("two_sided", "reverse_thickness_dir", "reverse_material_dir", "is_norm_to_sketch"):
                if node.get(k) is not None and not isinstance(node.get(k), bool):
                    errors.append("%s: '%s' must be a boolean." % (label, k))

        elif ntype == "sweep":
            # Swept boss (v0.7.0): the PROFILE sketch is consumed as the ACTIVE sketch (extrude's
            # grammar — must immediately precede); the PATH is an EARLIER sketch node selected by
            # its runtime NAME (node_feature substitution, like loft's profiles).
            sk = node.get("sketch")
            prev = nodes[pos - 1] if pos > 0 else None
            if not isinstance(sk, str) or not sk:
                errors.append("%s: 'sketch' (id of the PROFILE sketch node) is required." % label)
            elif seen.get(sk) != "sketch":
                errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
            elif not (isinstance(prev, dict) and prev.get("id") == sk):
                errors.append("%s: a sweep must immediately follow its PROFILE sketch node '%s'." % (label, sk))
            path = node.get("path")
            if not isinstance(path, str) or not path:
                errors.append("%s: 'path' (id of an EARLIER sketch node holding the sweep path) "
                              "is required." % label)
            elif seen.get(path) != "sketch":
                errors.append("%s: 'path' must reference an earlier sketch node (got '%s')." % (label, path))
            elif path == sk:
                errors.append("%s: 'path' and 'sketch' (profile) must be two DIFFERENT sketch nodes." % label)

        elif ntype == "linear_pattern":
            # Linear pattern (v0.7.0): canonical axis direction + flip; SINGLE direction by
            # design (the tool's documented count2/spacing2 second direction is unwired in C# —
            # a grid composes as a pattern OF a pattern instead).
            tgt = node.get("feature")
            if not isinstance(tgt, str) or not tgt:
                errors.append("%s: 'feature' (id of an earlier feature-producing node — the pattern seed) "
                              "is required." % label)
            elif seen.get(tgt) not in FEATURE_PRODUCERS:
                errors.append("%s: 'feature' must reference an earlier %s node (got '%s')."
                              % (label, "/".join(sorted(FEATURE_PRODUCERS)), tgt))
            if node.get("direction") not in PATTERN_DIRECTIONS:
                errors.append("%s: 'direction' must be one of %s (got %r)."
                              % (label, sorted(PATTERN_DIRECTIONS), node.get("direction")))
            if not _is_pos_number(node.get("spacing")):
                errors.append("%s: 'spacing' must be a positive number (meters between instances)." % label)
            count = node.get("count")
            if not (isinstance(count, int) and not isinstance(count, bool) and count >= 2):
                errors.append("%s: 'count' must be an integer >= 2 (total instances incl. the seed)." % label)
            if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
                errors.append("%s: 'flip' must be a boolean (pattern toward the negative axis)." % label)

        elif ntype == "circular_pattern":
            tgt = node.get("feature")
            if not isinstance(tgt, str) or not tgt:
                errors.append("%s: 'feature' (id of an earlier feature-producing node — the pattern seed) "
                              "is required." % label)
            elif seen.get(tgt) not in FEATURE_PRODUCERS:
                errors.append("%s: 'feature' must reference an earlier %s node (got '%s')."
                              % (label, "/".join(sorted(FEATURE_PRODUCERS)), tgt))
            axis = node.get("axis")
            datums = axis.get("datums") if isinstance(axis, dict) else None
            if (not isinstance(datums, list) or len(datums) != 2
                    or any(d not in DATUMS for d in datums) or datums[0] == datums[1]):
                errors.append("%s: axis.datums must be two DISTINCT canonical datums out of %s."
                              % (label, sorted(DATUMS)))
            count = node.get("count")
            if not (isinstance(count, int) and not isinstance(count, bool) and count >= 2):
                errors.append("%s: 'count' must be an integer >= 2 (total instances incl. the seed)." % label)
            angle = node.get("angle_deg")
            if not _is_pos_number(angle) or angle > 360:
                errors.append("%s: 'angle_deg' must be a number in (0, 360]." % label)
            eq = node.get("equal_spacing")
            if eq is not None and not isinstance(eq, bool):
                errors.append("%s: 'equal_spacing' must be a boolean." % label)

        elif ntype == "fillet":
            if not _is_pos_number(node.get("radius")):
                errors.append("%s: 'radius' must be a positive number (meters)." % label)
            edges = node.get("edges")
            if not isinstance(edges, list) or len(edges) == 0:
                errors.append("%s: 'edges' must be a non-empty array of edge anchors "
                              "({near:[x,y,z], hint?})." % label)
            else:
                for j, e in enumerate(edges):
                    if not isinstance(e, dict) or not _is_point3(e.get("near")):
                        errors.append("%s.edges[%d]: 'near' must be a 3D point [x, y, z] in meters."
                                      % (label, j))

        elif ntype == "chamfer":
            # Same hybrid edge anchors as fillet (IR-ADR-008). Two modes: distance_angle
            # (setback D1 + angle in RADIANS, C4) and distance_distance (D1 + D2, directional).
            ctype = node.get("chamfer_type", "distance_angle")
            if ctype not in CHAMFER_TYPES:
                errors.append("%s: 'chamfer_type' must be one of %s (got %r)."
                              % (label, sorted(CHAMFER_TYPES), ctype))
            if not _is_pos_number(node.get("distance")):
                errors.append("%s: 'distance' (first-face setback D1) must be a positive number (meters)." % label)
            if ctype == "distance_distance":
                if not _is_pos_number(node.get("distance2")):
                    errors.append("%s: chamfer_type 'distance_distance' requires 'distance2' "
                                  "(second-face setback D2) as a positive number (meters)." % label)
            else:  # distance_angle
                angle = node.get("angle")
                if angle is not None and not _is_pos_number(angle):
                    errors.append("%s: 'angle' must be a positive number (RADIANS; omit for 45°)." % label)
            if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
                errors.append("%s: 'flip' must be a boolean (distance_distance side swap)." % label)
            edges = node.get("edges")
            if not isinstance(edges, list) or len(edges) == 0:
                errors.append("%s: 'edges' must be a non-empty array of edge anchors "
                              "({near:[x,y,z], hint?})." % label)
            else:
                for j, e in enumerate(edges):
                    if not isinstance(e, dict) or not _is_point3(e.get("near")):
                        errors.append("%s.edges[%d]: 'near' must be a 3D point [x, y, z] in meters."
                                      % (label, j))

        elif ntype == "shell":
            # Hollow an EXISTING solid to a uniform wall (v0.8.0). `body` fixes the ORDER and
            # records which solid is hollowed; the faces left open are face anchors resolved on
            # the PRE-shell geometry (the shell consumes them) — the same pre-consumption rule
            # as fillet/chamfer's edge anchors. Zero or one removal faces; none = closed shell.
            tgt = node.get("body")
            if not isinstance(tgt, str) or not tgt:
                errors.append("%s: 'body' (id of an earlier body-producing node) is required." % label)
            elif seen.get(tgt) not in BODY_PRODUCERS:
                errors.append("%s: 'body' must reference an earlier %s node (got '%s')."
                              % (label, "/".join(sorted(BODY_PRODUCERS)), tgt))
            if not _is_pos_number(node.get("thickness")):
                errors.append("%s: 'thickness' must be a positive number (meters), smaller than "
                              "the thinnest section of the body." % label)
            faces = node.get("faces_to_remove")
            if faces is not None:
                if not isinstance(faces, list):
                    errors.append("%s: 'faces_to_remove' must be an array of face anchors "
                                  "({near:[x,y,z], hint?}) — omit it or pass [] for a closed shell."
                                  % label)
                else:
                    for j, f in enumerate(faces):
                        if not isinstance(f, dict) or not _is_point3(f.get("near")):
                            errors.append("%s.faces_to_remove[%d]: 'near' must be a 3D point "
                                          "[x, y, z] in meters." % (label, j))
            if node.get("outward") is not None and not isinstance(node.get("outward"), bool):
                errors.append("%s: 'outward' must be a boolean." % label)

        elif ntype == "sheet_metal":
            # Base flange: consumes the ACTIVE sketch, so it must immediately follow its sketch
            # (same grammar as extrude/revolve/rib).
            sk = node.get("sketch")
            prev = nodes[pos - 1] if pos > 0 else None
            if not isinstance(sk, str) or not sk:
                errors.append("%s: 'sketch' (id of a sketch node) is required." % label)
            elif seen.get(sk) != "sketch":
                errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
            elif not (isinstance(prev, dict) and prev.get("id") == sk):
                errors.append("%s: a sheet_metal base flange must immediately follow its sketch node '%s'." % (label, sk))
            if not _is_pos_number(node.get("thickness")):
                errors.append("%s: 'thickness' must be a positive number (meters)." % label)
            if node.get("bend_radius") is not None and not _is_pos_number(node.get("bend_radius")):
                errors.append("%s: 'bend_radius' must be a positive number (meters) when present." % label)
            kf = node.get("k_factor")
            if kf is not None and not (_is_number(kf) and 0 < kf < 1):
                errors.append("%s: 'k_factor' must be a number in (0, 1) when present." % label)
            if node.get("reverse_thickness") is not None and not isinstance(node.get("reverse_thickness"), bool):
                errors.append("%s: 'reverse_thickness' must be a boolean." % label)
            if node.get("symmetric_thickness") is not None and not isinstance(node.get("symmetric_thickness"), bool):
                errors.append("%s: 'symmetric_thickness' must be a boolean." % label)

        elif ntype == "sketched_bend":
            # Bend line(s) come from the ACTIVE sketch — must immediately follow its sketch.
            sk = node.get("sketch")
            prev = nodes[pos - 1] if pos > 0 else None
            if not isinstance(sk, str) or not sk:
                errors.append("%s: 'sketch' (id of the bend-line sketch node) is required." % label)
            elif seen.get(sk) != "sketch":
                errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
            elif not (isinstance(prev, dict) and prev.get("id") == sk):
                errors.append("%s: a sketched_bend must immediately follow its sketch node '%s'." % (label, sk))
            angle = node.get("angle")
            if angle is not None and not _is_pos_number(angle):
                errors.append("%s: 'angle' must be a positive number (RADIANS; omit for 90°)." % label)
            if node.get("radius") is not None and not _is_pos_number(node.get("radius")):
                errors.append("%s: 'radius' must be a positive number (meters); OMIT it to use the "
                              "sheet's default bend radius." % label)
            posn = node.get("position")
            if posn is not None and posn not in BEND_POSITIONS:
                errors.append("%s: 'position' must be one of %s (got %r)."
                              % (label, sorted(BEND_POSITIONS), posn))
            if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
                errors.append("%s: 'flip' must be a boolean." % label)
            fixed = node.get("fixed")
            if not isinstance(fixed, dict) or not _is_point3(fixed.get("near")):
                errors.append("%s: 'fixed' must be {near:[x,y,z], hint?} — a 3D point ON the sheet "
                              "face, on the SIDE that stays put (PRE-bend geometry)." % label)

        elif ntype == "edge_flange":
            # TWO MODES (v0.7.0): (A) custom profile — frame+profile given, the reverse-replay
            # three-phase flow; (B) simple LENGTH — `length` given instead, ONE tool call, a
            # full-edge-width flange (the forward mode: a forward AI cannot know the generated
            # sketch's frame). The pair and `length` are mutually exclusive.
            edge = node.get("edge")
            if not isinstance(edge, dict) or not _is_point3(edge.get("near")):
                errors.append("%s: 'edge' must be {near:[x,y,z], hint?} — a 3D point ON the attach "
                              "edge (the reader's edge `mid`)." % label)
            angle = node.get("angle")
            if angle is not None and not _is_pos_number(angle):
                errors.append("%s: 'angle' must be a positive number (RADIANS; omit for 90°)." % label)
            length = node.get("length")
            frame = node.get("frame")
            profile = node.get("profile")
            if length is not None:
                # MODE B — simple length flange.
                if not _is_pos_number(length):
                    errors.append("%s: 'length' must be a positive number (meters)." % label)
                if frame is not None or profile is not None:
                    errors.append("%s: 'length' (simple mode) and 'frame'/'profile' (custom-profile "
                                  "mode) are mutually exclusive." % label)
                for k in ("radius", "position", "flip"):
                    if node.get(k) is not None:
                        errors.append("%s: '%s' is not supported in length mode (the simple flange "
                                      "uses the sheet default radius, position material_inside, no "
                                      "flip) — use the frame+profile mode for it." % (label, k))
            else:
                # MODE A — custom profile (the original v0.5.6 contract).
                if (not isinstance(frame, dict)
                        or not all(_is_point3(frame.get(k)) for k in ("origin", "xdir", "ydir"))):
                    errors.append("%s: 'frame' is REQUIRED {origin, xdir, ydir} — the ORIGINAL profile "
                                  "sketch's frame; the generated sketch's frame is unpredictable, so "
                                  "the coordinate transform is mandatory (or give 'length' for the "
                                  "simple full-width flange)." % label)
                if not isinstance(profile, list) or len(profile) == 0:
                    errors.append("%s: 'profile' must be a non-empty array of primitives (the ORIGINAL "
                                  "profile sketch's segments, original 2D coords)." % label)
                else:
                    for j, prim in enumerate(profile):
                        _check_profile_prim(prim, "%s.profile[%d]" % (label, j), errors)
                if node.get("radius") is not None and not _is_pos_number(node.get("radius")):
                    errors.append("%s: 'radius' must be a positive number (meters); OMIT it to use the "
                                  "sheet's default bend radius." % label)
                posn = node.get("position")
                if posn is not None and posn not in EDGE_FLANGE_POSITIONS:
                    errors.append("%s: 'position' must be one of %s (got %r)."
                                  % (label, sorted(EDGE_FLANGE_POSITIONS), posn))
                if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
                    errors.append("%s: 'flip' must be a boolean." % label)

        elif ntype == "mirror":
            plane = node.get("plane")
            datum = plane.get("datum") if isinstance(plane, dict) else None
            if datum not in DATUMS:
                errors.append("%s: plane.datum must be one of %s (got %r)."
                              % (label, sorted(DATUMS), datum))
            feats = node.get("features")
            if not isinstance(feats, list) or len(feats) == 0:
                errors.append("%s: 'features' must be a non-empty array of earlier "
                              "feature-producing node ids." % label)
            else:
                for fid in feats:
                    if not isinstance(fid, str) or not fid:
                        errors.append("%s: each mirrored feature must be a node id (non-empty string)." % label)
                    elif seen.get(fid) not in FEATURE_PRODUCERS:
                        errors.append("%s: feature '%s' must reference an earlier %s node."
                                      % (label, fid, "/".join(sorted(FEATURE_PRODUCERS))))

        elif ntype == "component":
            # Component occurrence: references its part FILE (path + hash — the V1 cache
            # discipline extends, ADR-047b). `transform` (13 numbers: 3x3 rotation row-major +
            # translation + scale — exactly the reader's layout) is REQUIRED: it is the
            # PLACEMENT input for a fixed component and the initial placement + verification
            # ground truth for a floating one (mates do the constraining).
            src = node.get("source")
            if not isinstance(src, dict) or not isinstance(src.get("path"), str) or not src.get("path"):
                errors.append("%s: 'source' must be {path: '<part file>', hash?: 'sha256:...'}." % label)
            elif src.get("hash") is not None and not isinstance(src.get("hash"), str):
                errors.append("%s: source.hash must be a string when present." % label)
            tr = node.get("transform")
            if (not isinstance(tr, list) or len(tr) != 13
                    or not all(_is_number(v) for v in tr)):
                errors.append("%s: 'transform' must be 13 numbers (9 rotation row-major + "
                              "3 translation (meters) + scale) — the reader's exact layout." % label)
            if node.get("fixed") is not None and not isinstance(node.get("fixed"), bool):
                errors.append("%s: 'fixed' must be a boolean." % label)
            if node.get("config") is not None and not isinstance(node.get("config"), str):
                errors.append("%s: 'config' must be a string (referenced configuration name)." % label)
            if seen_mate:
                errors.append("%s: components must come BEFORE all mates (components in tree "
                              "order, then mates in creation order)." % label)

        elif ntype == "mate":
            seen_mate = True
            mt = node.get("mate_type")
            if mt not in MATE_TYPES:
                errors.append("%s: 'mate_type' must be one of %s (got %r)."
                              % (label, sorted(MATE_TYPES), mt))
            al = node.get("alignment")
            if al is not None and al not in MATE_ALIGNMENTS:
                errors.append("%s: 'alignment' must be one of %s (got %r)."
                              % (label, sorted(MATE_ALIGNMENTS), al))
            if mt in ("distance", "angle") and not _is_number(node.get("value")):
                errors.append("%s: mate_type '%s' requires 'value' (SI — meters / RADIANS, C4)."
                              % (label, mt))
            if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
                errors.append("%s: 'flip' must be a boolean." % label)
            sides = node.get("sides")
            if not isinstance(sides, list) or len(sides) != 2:
                errors.append("%s: 'sides' must be exactly 2 entries "
                              "[{component: <node id>, anchor: {kind, near, hint?}}, ...]." % label)
            else:
                for j, side in enumerate(sides):
                    swhere = "%s.sides[%d]" % (label, j)
                    if not isinstance(side, dict):
                        errors.append("%s must be an object." % swhere)
                        continue
                    cid = side.get("component")
                    if not isinstance(cid, str) or not cid:
                        errors.append("%s: 'component' (an earlier component node id) is required." % swhere)
                    elif seen.get(cid) != "component":
                        errors.append("%s: component '%s' must reference an earlier component node." % (swhere, cid))
                    anchor = side.get("anchor")
                    if not isinstance(anchor, dict) or not _is_point3(anchor.get("near")):
                        errors.append("%s: anchor must be {kind, near:[x,y,z] (COMPONENT-LOCAL "
                                      "coords, meters), dir?, radius?, hint?}." % swhere)
                    else:
                        if anchor.get("kind") not in ANCHOR_KINDS:
                            errors.append("%s: anchor.kind must be one of %s (the reader's entity "
                                          "kind, verbatim — got %r)."
                                          % (swhere, sorted(ANCHOR_KINDS), anchor.get("kind")))
                        # dir = the stored plane NORMAL / cylinder AXIS (C7 ground truth) —
                        # disambiguates distinct surfaces through the same point (the 4-1
                        # interior-anchor lesson, hit live on 1-1's shaft flat).
                        if anchor.get("dir") is not None and not _is_point3(anchor.get("dir")):
                            errors.append("%s: anchor.dir must be a 3D unit vector when present." % swhere)
                        if anchor.get("radius") is not None and not _is_pos_number(anchor.get("radius")):
                            errors.append("%s: anchor.radius must be a positive number when present." % swhere)

        elif ntype == "loft":
            # Multi-profile boss loft. Unlike extrude/revolve/rib, the profile sketches are
            # selected BY NAME (not the active sketch), so they need NOT immediately precede the
            # loft — they only need to be EARLIER sketch nodes (the compiler threads each one's
            # runtime name). >= 2 profiles, in loft order.
            profiles = node.get("profiles")
            if not isinstance(profiles, list) or len(profiles) < 2:
                errors.append("%s: 'profiles' must be an array of >= 2 sketch node ids "
                              "(the loft profiles, in order)." % label)
            else:
                for pid in profiles:
                    if not isinstance(pid, str) or not pid:
                        errors.append("%s: each profile must be a sketch node id (non-empty string)." % label)
                    elif seen.get(pid) != "sketch":
                        errors.append("%s: profile '%s' must reference an earlier sketch node." % (label, pid))

        if nid:
            seen[nid] = ntype

    errors.extend(_check_contour_closure(nodes))
    return errors


# Consumers that need the ACTIVE sketch's contour to CLOSE (the sketch must immediately
# precede extrude/revolve/sheet_metal/sweep-profile; loft profiles are referenced by id).
# rib and sketched_bend consume OPEN profiles by design; a sweep's `path` sketch is an
# open chain by design — none of them impose closure.
_CLOSURE_CONSUMERS = frozenset(("extrude", "revolve", "sheet_metal", "sweep"))


def _check_contour_closure(nodes):
    """Plan-time contour checks for sketches whose consumer needs a closed region.

    Zero execution calls. Two independent jobs, because the two failure classes differ:
      - the CHAIN walk (line/arc): every still-open endpoint is reported with the exact gap and
        its nearest neighbour, so the planner fixes coordinates instead of discovering the
        failure as a mid-build EXTRUSION_FAILED + rollback.
      - the CLOSED-primitive walk (contours.loop_conflicts): the five kinds that enclose a region
        by construction, which have no endpoints to weld and so are invisible to the chain walk.
    """
    consumer_of = {}  # sketch node id -> "extrude 'e1'"-style consumer label
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        nid = node.get("id")
        if ntype in _CLOSURE_CONSUMERS:
            sk = node.get("sketch")
            if isinstance(sk, str):
                consumer_of.setdefault(sk, []).append("%s '%s'" % (ntype, nid))
        elif ntype == "loft":
            for pid in node.get("profiles") or ():
                if isinstance(pid, str):
                    consumer_of.setdefault(pid, []).append("loft '%s'" % nid)
    errors = []
    for pos, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("type") != "sketch":
            continue
        nid = node.get("id")
        if nid not in consumer_of:
            continue
        key = nid
        label = "sketch '%s'" % nid
        profile = node.get("profile")
        if not isinstance(profile, list):
            continue  # shape errors already reported in the per-node pass
        consumers = "/".join(consumer_of[key])
        # P1-1: the weld tolerance can collapse a genuinely thin feature (or a stray spike)
        # to a zero-length entity, which SolidWorks cannot build. Report it here rather than
        # letting the build fail mid-flight. Checked BEFORE the open-gap walk because a
        # collapsed contour's endpoints all coincide, so it would otherwise look closed.
        collapsed = contours.collapsed_entities(profile)
        if collapsed:
            parts = ["profile[%d] (%s), span %.6g m" % (c["prim"], c["kind"], c["span"])
                     for c in collapsed[:4]]
            if len(collapsed) > 4:
                parts.append("+%d more" % (len(collapsed) - 4))
            errors.append(
                "nodes[%d] (%s): %s — the contour weld (gaps <= %g m) collapsed the entity to "
                "a zero-length segment. Move the endpoints apart or widen the feature."
                % (pos, label, "; ".join(parts), contours.CONTOUR_WELD_TOL))
            continue
        gaps = contours.open_gaps(profile)
        if gaps:
            parts = []
            for g in gaps[:4]:
                if g["nearest_prim"] is not None:
                    nearest = (" — no partner within %g m (nearest: profile[%d] (%s) %s, "
                               "%.6g m away)" % (contours.CONTOUR_WELD_TOL, g["nearest_prim"],
                                                 g["nearest_kind"], g["nearest_which"],
                                                 g["nearest_gap"]))
                else:
                    nearest = " — no other chain endpoint in the profile"
                parts.append("profile[%d] (%s) %s at (%.6g, %.6g)%s"
                             % (g["prim"], g["kind"], g["endpoint"], g["x"], g["y"], nearest))
            if len(gaps) > 4:
                parts.append("+%d more open endpoint(s)" % (len(gaps) - 4))
            errors.append(
                "nodes[%d] (%s): open contour for %s — %s. Chain segments close ONLY "
                "through exact shared endpoint coordinates (the compiler welds gaps <= %g m); "
                "correct the endpoints, close the loop, or mark non-contour segments "
                "construction:true."
                % (pos, label, consumers, "; ".join(parts), contours.CONTOUR_WELD_TOL))
        elif contours.collapsed_profile(profile):
            # Closed (every endpoint paired) yet the outline has no enclosed area: the weld
            # merged a thin feature shut. Only meaningful once closure holds.
            ext = contours.profile_extent(profile)
            errors.append(
                "nodes[%d] (%s): closed contour for %s has collapsed to a line/point "
                "(bounding span %.6g x %.6g m <= weld tolerance %g m) — no enclosed area "
                "remains. Widen the profile."
                % (pos, label, consumers, ext[2] - ext[0], ext[3] - ext[1],
                   contours.CONTOUR_WELD_TOL))
        # v0.8.0 batch 6: the chain walk above is blind to the CLOSED-by-construction kinds
        # (slot, rounded_rect, rectangle, circle, ellipse) — they enclose a region on their own,
        # so they have no endpoints to weld and never show up as a gap. Two of them that overlap
        # in one sketch passed validation and only failed in SolidWorks as a null cut
        # (EXTRUSION_FAILED, 2026-10-26: the five-slot fan, all drawn horizontal). Run the
        # polygon overlap check on every closure-consuming sketch, in ADDITION to the chain
        # checks — a profile may mix chains and composites.
        errors.extend("nodes[%d] (%s): %s" % (pos, label, msg)
                      for msg in _loop_conflict_errors(profile, consumers))
    return errors


def _loop_conflict_errors(profile, consumers):
    """VALIDATION_FAILED text for closed loops in one sketch that cannot both be built."""
    conflicts = contours.loop_conflicts(profile)
    if not conflicts:
        return []
    parts = []
    for c in conflicts[:4]:
        # "cross"/"self-cross" name the exact crossing; "collinear"/"mutual" have no single
        # crossing to point at, so the coordinate is the shared region's centre and is labelled
        # as near rather than at.
        at = "near" if c["how"] in ("collinear", "mutual") else "at"
        where = "" if c["point"] is None else " %s (%.6g, %.6g)" % (at, c["point"][0],
                                                                    c["point"][1])
        if c["how"] == "degenerate":
            parts.append("profile[%d] (%s) encloses no area" % (c["a"], c["a_kind"]))
        elif c["how"] == "self-cross":
            parts.append("profile[%d] (%s) crosses itself%s" % (c["a"], c["a_kind"], where))
        elif c["how"] == "collinear":
            parts.append("profile[%d] (%s) runs along profile[%d] (%s) with coincident edges%s"
                         " — the same contour written twice?"
                         % (c["a"], c["a_kind"], c["b"], c["b_kind"], where))
        elif c["how"] == "mutual":
            parts.append("profile[%d] (%s) overlaps profile[%d] (%s) — each reaches inside the "
                         "other%s" % (c["a"], c["a_kind"], c["b"], c["b_kind"], where))
        else:
            parts.append("profile[%d] (%s) crosses profile[%d] (%s)%s"
                         % (c["a"], c["a_kind"], c["b"], c["b_kind"], where))
        if c["more"]:
            parts[-1] += " (+%d more crossings)" % c["more"]
    if len(conflicts) > 4:
        parts.append("+%d more overlapping pair(s)" % (len(conflicts) - 4))
    return [
        "overlapping closed contours for %s — %s. A closed primitive (slot, rounded_rect, "
        "rectangle, circle, ellipse) is a contour by itself, so two of them in one sketch must "
        "be DISJOINT or properly NESTED (a hole inside an outline) — never crossing, touching "
        "along an edge, or running through each other, and never a loop that crosses itself. "
        "Separate them, delete the duplicate, or if they must share a boundary merge them into "
        "one chain of line/arc segments."
        % (consumers, "; ".join(parts))]


def _check_extrude_end(node, op, label, errors):
    """End-condition rules. Back-compat: no 'end' field behaves exactly like v0-exp
    (boss => blind depth; cut => through:true / depth:'through_all' / blind depth)."""
    end = node.get("end")
    if end is not None and end not in EXTRUDE_ENDS:
        errors.append("%s: 'end' %r unsupported (only %s)." % (label, end, sorted(EXTRUDE_ENDS)))
        return
    if end == "up_to_surface":
        up_to = node.get("up_to") or {}
        face = up_to.get("face") if isinstance(up_to, dict) else None
        if not isinstance(face, dict) or not _is_point3(face.get("near")):
            errors.append("%s: end 'up_to_surface' requires up_to.face.near = [x, y, z] "
                          "(a point ON the terminating face's plane, meters)." % label)
        return
    if end == "through_all":
        return
    if end == "mid_plane":
        if not _is_pos_number(node.get("depth")):
            errors.append("%s: end 'mid_plane' requires 'depth' = the TOTAL symmetric width "
                          "(positive meters)." % label)
        return
    # end == 'blind' or omitted -> the v0-exp rules
    if end == "blind" or end is None:
        is_through = (node.get("through") is True or node.get("depth") in THROUGH_DEPTHS)
        if end == "blind" and is_through:
            errors.append("%s: end 'blind' contradicts through/through_all depth." % label)
            return
        if op == "boss" and not is_through and not _is_pos_number(node.get("depth")):
            errors.append("%s: 'depth' (positive meters) is required for a blind boss extrude." % label)
        if op == "cut" and not (is_through or _is_pos_number(node.get("depth"))):
            errors.append("%s: a cut needs 'through': true, depth 'through_all', or a positive blind depth." % label)


def _check_datum(node, label, errors):
    ref = node.get("ref")
    if ref is None:
        return  # default datum 'top', offset 0
    if not isinstance(ref, dict):
        errors.append("%s: 'ref' must be an object." % label)
        return
    # v0.5.5: a sketch may sit on a NON-axis-aligned support (a bent flange's face) via a face
    # anchor instead of datum+offset — {face: {near:[x,y,z], hint?}}. Mutually exclusive with datum.
    face = ref.get("face")
    if face is not None:
        if not isinstance(face, dict) or not _is_point3(face.get("near")):
            errors.append("%s: ref.face must be {near:[x,y,z], hint?} — a point ON the support "
                          "face's plane (from the ORIGINAL's analyze read)." % label)
        if ref.get("datum") is not None or ref.get("offset") is not None:
            errors.append("%s: ref.face and ref.datum/offset are mutually exclusive." % label)
        return
    datum = ref.get("datum", "top")
    if datum not in DATUMS:
        errors.append("%s: ref.datum '%s' must be one of %s." % (label, datum, sorted(DATUMS)))
    offset = ref.get("offset")
    if offset is not None and not _is_number(offset):
        errors.append("%s: ref.offset must be a signed number (meters)." % label)


def _check_profile_prim(prim, label, errors):
    if not isinstance(prim, dict):
        errors.append("%s must be an object." % label)
        return
    kind = prim.get("kind")
    if kind not in PROFILE_KINDS:
        errors.append("%s.kind '%s' unsupported (only %s)." % (label, kind, sorted(PROFILE_KINDS)))
        return
    for flag in sorted(PROFILE_FLAGS):
        if prim.get(flag) is not None and not isinstance(prim.get(flag), bool):
            errors.append("%s: '%s' must be a boolean." % (label, flag))
    if kind == "rectangle":
        for k in ("width", "height"):
            if not _is_pos_number(prim.get(k)):
                errors.append("%s: rectangle '%s' must be a positive number." % (label, k))
        # v0.8.0 batch 7: an optional centre, the same idiom circle/slot/rounded_rect use. Before
        # it lowering IGNORED cx/cy and drew the rectangle on the origin anyway — a planner that
        # wrote a plausible-looking centre got wrong geometry with no error at all.
        for k in ("cx", "cy"):
            if prim.get(k) is not None and not _is_number(prim.get(k)):
                errors.append("%s: rectangle '%s' must be a number." % (label, k))
    elif kind == "circle":
        if not _is_pos_number(prim.get("diameter")):
            errors.append("%s: circle 'diameter' must be a positive number." % label)
        for k in ("cx", "cy"):
            if prim.get(k) is not None and not _is_number(prim.get(k)):
                errors.append("%s: circle '%s' must be a number." % (label, k))
    elif kind == "line":
        for k in ("x1", "y1", "x2", "y2"):
            if not _is_number(prim.get(k)):
                errors.append("%s: line '%s' must be a number (exact 2D sketch coords, meters)." % (label, k))
    elif kind == "arc":
        for k in ("cx", "cy", "x1", "y1", "x2", "y2"):
            if not _is_number(prim.get(k)):
                errors.append("%s: arc '%s' must be a number (exact 2D sketch coords, meters)." % (label, k))
        if prim.get("dir") not in (1, -1):
            errors.append("%s: arc 'dir' must be 1 (CCW) or -1 (CW) — centre+start+end alone "
                          "describe two arcs." % label)
    elif kind == "ellipse":
        # Centre + a point on the MAJOR axis + a point on the MINOR axis — exactly the reader's
        # shape and add_sketch_entity(ellipse)'s inputs (round-trips verbatim).
        for k in ("cx", "cy", "x1", "y1", "x2", "y2"):
            if not _is_number(prim.get(k)):
                errors.append("%s: ellipse '%s' must be a number (centre / major-axis point / "
                              "minor-axis point, exact 2D sketch coords)." % (label, k))
    elif kind == "spline":
        # Flat through-point list [x1, y1, x2, y2, ...] — mirrors the reader's spline 'points'.
        pts = prim.get("points")
        if (not isinstance(pts, list) or len(pts) < 4 or len(pts) % 2 != 0
                or not all(_is_number(v) for v in pts)):
            errors.append("%s: spline 'points' must be a FLAT number list [x1, y1, x2, y2, ...] "
                          "with >= 2 points (even count)." % label)
    elif kind == "slot":
        # v0.7.4 convenience primitive (expanded to exact line/arc entities at lowering):
        # overall length > width, optional cx/cy centre like a circle. length == width is a
        # circle — model it as one.
        if not _is_pos_number(prim.get("length")):
            errors.append("%s: slot 'length' (overall) must be a positive number." % label)
        if not _is_pos_number(prim.get("width")):
            errors.append("%s: slot 'width' must be a positive number." % label)
        if (_is_pos_number(prim.get("length")) and _is_pos_number(prim.get("width"))
                and prim["length"] <= prim["width"]):
            errors.append("%s: slot 'length' must be > 'width' (length == width is a circle)." % label)
        for k in ("cx", "cy"):
            if prim.get(k) is not None and not _is_number(prim.get(k)):
                errors.append("%s: slot '%s' must be a number." % (label, k))
        # v0.8.0 batch 5: 'angle' turns the composite about (cx, cy) — CCW in the sketch plane, RADIANS
        # like every other angular IR field (revolve / chamfer / bend). Bounded to one turn
        # because rotation is PERIODIC: a larger magnitude cannot mean anything the author
        # intended, and in practice it means degrees were passed (90 instead of pi/2), which
        # would silently draw a near-horizontal slot again — the exact defect the field fixes.
        angle = prim.get("angle")
        if angle is not None:
            if not _is_number(angle):
                errors.append("%s: slot 'angle' must be a number (RADIANS, CCW about cx/cy; "
                              "omit for length along +X)." % label)
            elif abs(angle) > 2 * math.pi:
                errors.append("%s: slot 'angle' must be within +/-2*pi RADIANS (got %r) — a "
                              "vertical slot is angle = pi/2 = 1.5707963267948966, not 90."
                              % (label, angle))
    elif kind == "rounded_rect":
        # v0.7.4 convenience primitive: centred like rectangle, corner radius must leave all
        # four edges non-degenerate.
        for k in ("width", "height"):
            if not _is_pos_number(prim.get(k)):
                errors.append("%s: rounded_rect '%s' must be a positive number." % (label, k))
        if not _is_pos_number(prim.get("radius")):
            errors.append("%s: rounded_rect 'radius' must be a positive number." % label)
        if (_is_pos_number(prim.get("width")) and _is_pos_number(prim.get("height"))
                and _is_pos_number(prim.get("radius"))
                and prim["radius"] * 2 >= min(prim["width"], prim["height"])):
            errors.append("%s: rounded_rect 'radius' must be < min(width, height)/2 — at the "
                          "limit the straight edges degenerate (use a slot or circle)." % label)
        for k in ("cx", "cy"):
            if prim.get(k) is not None and not _is_number(prim.get(k)):
                errors.append("%s: rounded_rect '%s' must be a number." % (label, k))
