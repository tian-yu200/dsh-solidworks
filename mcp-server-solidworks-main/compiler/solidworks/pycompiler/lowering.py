"""Lowering — IR node -> ordered calls to the EXISTING low-level execution tools.

Pure functions: given a node (and, where a reference is semantic, the values the resolver
produced from live geometry), return an ordered list of Op(tool, params). The params use the
execution layer's own key names (same as tool-schemas.json / the adapter), since they are POSTed
straight to /api/tool/execute. No new execution tool is introduced — a single IR node becomes
many tool calls, which is the whole point of the IR (one model call -> many ops).

Conventions reused from the low-level tools:
  - lengths in meters; box centred on the sketch origin, a rectangle profile centred on its
    optional cx/cy (default the origin).
  - through-all cut relies on the universal flipped-direction auto-retry (ADR-033), so the
    compiler does not compute 'reverse'.
  - path profiles (line/arc) carry EXACT frozen coordinates; no constraints are added — segments
    sharing identical endpoints already close the contour (add_sketch_entity guidance).

Runtime-value placeholder: an offset-datum sketch first creates a reference plane whose NAME only
exists in that op's RESPONSE (and is localized, e.g. 'Düzlem1' — always use the
returned name, never a guessed one). Such a param carries the FROM_PREV_FEATURE sentinel and the
compiler substitutes it with the previous response's cadState.features[0] before executing.
"""
import json
import math
from collections import namedtuple

from . import contours
from . import thread_table
from .errors import FeatureError
# Shared vocabulary, NOT re-declared here: a second copy of "what counts as a through depth"
# would let the validator and the lowering drift apart on exactly the token that decides a cut's
# end condition. ir_schema imports only contours/thread_table, so this is not a cycle.
from .ir_schema import THROUGH_DEPTHS, THREAD_STANDARD_ENUM

Op = namedtuple("Op", ["tool", "params", "note"])

# Sentinel (JSON-safe string, impossible as a real plane name) — see module docstring.
FROM_PREV_FEATURE = "__FROM_PREV_FEATURE__"

# Node-scoped runtime-name sentinel: node_feature('n6') -> "__NODE_FEATURE__:n6". The compiler
# replaces it with the CREATED feature name it recorded when node n6 executed (e.g. the cut the
# circular pattern seeds on — 'Cut-Extrude1' / localized). Same principle as FROM_PREV_FEATURE,
# but reaching back to ANY earlier node instead of the immediately preceding response.
_NODE_FEATURE_PREFIX = "__NODE_FEATURE__:"


def node_feature(node_id):
    return _NODE_FEATURE_PREFIX + node_id


def parse_node_feature(value):
    """Return the node id if value is a node-feature sentinel, else None."""
    if isinstance(value, str) and value.startswith(_NODE_FEATURE_PREFIX):
        return value[len(_NODE_FEATURE_PREFIX):]
    return None


# Deferred-reference sentinel for a thread's bore-mouth edge. A `hole`'s mouth circle does not
# exist when its ops are PLANNED (the cut that creates it has not run), so the lowering cannot
# put an index in the op. It puts this marker plus the anchor the edge must be found by, and the
# compiler resolves it between the last cut and the add_cosmetic_thread call — one resolution,
# one call (the tool is ADDITIVE: a second call would create a second thread).
THREAD_EDGE_PENDING = "__THREAD_EDGE_PENDING__"
# The op param that carries that anchor. Popped before the op is executed — the tool never sees it.
THREAD_REF_PARAM = "thread_ref"


def thread_ref(anchor):
    """Normalize a thread anchor into a resolver ref ({edge_anchor} or {bore_face}).

    `anchor` is whatever the caller knows: a cosmetic_thread node's `ref`, or the synthetic
    {bore_face: {near, radius}} the compiler builds for a `hole`'s own bore. Passing an already
    valid ref through unchanged keeps this a pure normalization, never a reinterpretation.
    """
    if not isinstance(anchor, dict):
        return {}
    if anchor.get("edge_anchor") is not None or anchor.get("bore_face") is not None:
        return anchor
    return {"bore_face": anchor} if anchor.get("near") is not None else {}


_PLANE = {"top": "Top Plane", "front": "Front Plane", "right": "Right Plane"}


def _datum(node):
    ref = node.get("ref") or {}
    return ref.get("datum", "top")


def _offset(node):
    ref = node.get("ref") or {}
    return float(ref.get("offset") or 0.0)


def _rectangle_op(width, height, note, construction=False, cx=0.0, cy=0.0):
    # cx/cy (v0.8.0 batch 7) shift the rectangle in the sketch plane, exactly like a circle's
    # centre: the corners are EMITTED (the tool's rectangle is two opposite corners), so a graph
    # without cx/cy sends 0.0 - w and 0.0 + w — bit-for-bit -w and +w — and lowers unchanged.
    w = float(width) / 2.0
    h = float(height) / 2.0
    cx, cy = float(cx or 0.0), float(cy or 0.0)
    params = {"entity_type": "rectangle", "x1": cx - w, "y1": cy - h,
              "x2": cx + w, "y2": cy + h}
    if construction:
        params["construction"] = True
    return Op("add_sketch_entity", params, note)


def lower_box(node):
    """box -> create_sketch(plane) + centred rectangle + boss extrude."""
    plane = _PLANE[_datum(node)]
    return [
        Op("create_sketch", {"plane": plane, "on_face": False}, "box: sketch on " + plane),
        _rectangle_op(node["width"], node["depth"], "box: centred rectangle"),
        Op("extrude_feature", {"feature_type": "boss", "depth": float(node["height"])}, "box: boss extrude"),
    ]


def lower_sketch(node, face_index=None):
    """sketch -> support op(s) + create_sketch + one entity per profile primitive (leaves the
    sketch active). Support preference for offset != 0 (decided with the user, 2026-07-05):
      1. face_index given (the resolver found an EXISTING face on that plane) -> sketch ON the
         face — matches original authoring, no scaffolding plane;
      2. else create a reference plane; its runtime name flows via FROM_PREV_FEATURE
         (localization-independent — 'Düzlem1'/'Plane1').
    Either way, coordinate correctness does NOT depend on the support: when the node carries the
    ORIGINAL sketch's `frame`, the compiler transforms every 2D coordinate into the frame
    create_sketch MEASURES on the rebuild.

    Contour welding (v0.7.3): chain endpoints within CONTOUR_WELD_TOL are snapped onto the first
    matching earlier anchor before the entities are emitted — validation already rejected
    anything still open, so the weld only closes micron-level arithmetic drift. The weld count
    rides the create_sketch op's note (visible in the node_log)."""
    profile, welds = contours.weld_profile(node.get("profile") or [])
    weld_note = _weld_note(welds)
    offset = _offset(node)
    ops = []
    if face_index is not None:
        ops.append(Op("create_sketch", {"on_face": True, "face_index": int(face_index)},
                      "sketch on the existing face #%d at %s %+g m%s"
                      % (int(face_index), _datum(node), offset, weld_note)))
    elif offset != 0.0:
        plane = _PLANE[_datum(node)]
        ops.append(Op("add_reference_geometry",
                      {"type": "plane", "ref_plane_name": plane, "offset": offset},
                      "sketch: offset plane %s %+g m" % (plane, offset)))
        ops.append(Op("create_sketch", {"plane": FROM_PREV_FEATURE, "on_face": False},
                      "sketch on the created offset plane" + weld_note))
    else:
        plane = _PLANE[_datum(node)]
        ops.append(Op("create_sketch", {"plane": plane, "on_face": False},
                      "sketch on " + plane + weld_note))
    for prim in profile:
        ops.extend(_profile_ops(prim))
    return ops


def _weld_note(welds):
    if not welds:
        return ""
    return " (%d endpoint(s) welded within %g m)" % (len(welds), contours.CONTOUR_WELD_TOL)


def _profile_op(prim):
    kind = prim.get("kind")
    construction = prim.get("construction") is True
    if kind == "rectangle":
        return _rectangle_op(prim["width"], prim["height"], "rectangle", construction,
                             prim.get("cx"), prim.get("cy"))
    if kind == "circle":
        params = {"entity_type": "circle",
                  "cx": float(prim.get("cx") or 0.0), "cy": float(prim.get("cy") or 0.0),
                  "radius": float(prim["diameter"]) / 2.0}
        if construction:
            params["construction"] = True
        return Op("add_sketch_entity", params, "circle")
    if kind == "line":
        params = {"entity_type": "line",
                  "x1": float(prim["x1"]), "y1": float(prim["y1"]),
                  "x2": float(prim["x2"]), "y2": float(prim["y2"])}
        if construction:
            params["construction"] = True
        return Op("add_sketch_entity", params, "construction line" if construction else "line")
    if kind == "arc":
        # arc_center: exact centre + endpoints + sweep sense — radius-exact and stable for
        # shallow arcs (created with AddToDB, ADR on arc_center); mirrors analyze's 'dir'.
        params = {"entity_type": "arc_center",
                  "cx": float(prim["cx"]), "cy": float(prim["cy"]),
                  "x1": float(prim["x1"]), "y1": float(prim["y1"]),
                  "x2": float(prim["x2"]), "y2": float(prim["y2"]),
                  "direction": int(prim["dir"])}
        if construction:
            params["construction"] = True
        return Op("add_sketch_entity", params, "construction arc" if construction else "arc")
    if kind == "ellipse":
        # Centre + major-axis point + minor-axis point — the reader's exact shape, verbatim.
        params = {"entity_type": "ellipse",
                  "cx": float(prim["cx"]), "cy": float(prim["cy"]),
                  "x1": float(prim["x1"]), "y1": float(prim["y1"]),
                  "x2": float(prim["x2"]), "y2": float(prim["y2"])}
        if construction:
            params["construction"] = True
        return Op("add_sketch_entity", params, "construction ellipse" if construction else "ellipse")
    if kind == "spline":
        # Flat through-points as a REAL JSON array. UNLIKE edge_indices/profiles/features_json
        # (JSON-string params parsed C#-side), the C# spline case reads `points` as a JArray —
        # the MCP adapter pre-parses its string param before POSTing, and the IR door posts
        # straight to /api/tool/execute, so it must send the parsed form (live-caught 2026-07-16).
        params = {"entity_type": "spline",
                  "points": [float(v) for v in prim["points"]]}
        if construction:
            params["construction"] = True
        return Op("add_sketch_entity", params, "construction spline" if construction else "spline")
    if kind == "slot":
        # v0.7.4 convenience primitive: ONE closed slot -> 4 exact line/arc entities (overall
        # length x width, centred on cx/cy). Traversal order left cap -> top -> right cap ->
        # bottom; endpoints are computed exact, so the chain closes without welding.
        #
        # `angle` (v0.8.0 batch 5, RADIANS, CCW about cx/cy) rotates that whole chain as a RIGID BODY.
        # Without it the composite could only ever lie along +X, so a vertical or radial slot
        # was silently drawn horizontal: the 3D01 fan's five angled slots came out as five
        # OVERLAPPING horizontal ones running off the body, and their through_all cut returned
        # null (live 2026-10-26 — a1 and graph_diag_slotsonly failed at n7 while the identical
        # shape drawn as explicit arc/line chains cut first try).
        length = float(prim["length"])
        width = float(prim["width"])
        cx = float(prim.get("cx") or 0.0)
        cy = float(prim.get("cy") or 0.0)
        angle = float(prim.get("angle") or 0.0)
        r = width / 2.0
        half = (length - width) / 2.0
        cos_t, sin_t = math.cos(angle), math.sin(angle)

        def frame(dx, dy):
            """A slot-frame point (local: axis along +X, centre at the origin) in sketch coords.
            Rounded ONCE here and then reused verbatim by both entities that meet at it, so the
            contour still closes with NO weld at any angle. The `+ 0.0` drops a signed -0.0
            (a rotated zero comes out negative for some angles): numerically identical, but it
            keeps a shared endpoint the SAME TEXT in both entities' params."""
            return (round(cx + dx * cos_t - dy * sin_t, 9) + 0.0,
                    round(cy + dx * sin_t + dy * cos_t, 9) + 0.0)

        lc, rc = frame(-half, 0.0), frame(half, 0.0)   # cap centres, on the slot axis
        a, b = frame(-half, -r), frame(-half, r)       # left cap: bottom -> top
        c, d = frame(half, r), frame(half, -r)         # right cap: top -> bottom
        return [
            _arc_op(lc[0], lc[1], a[0], a[1], b[0], b[1], -1,
                    "slot: left cap", construction),
            _line_op(b[0], b[1], c[0], c[1], "slot: top edge", construction),
            # Both caps sweep CW (-1): the left cap's A->B passes through -X (its outward bulge)
            # and the right cap's C->D passes through +X. +1 would sweep the 180 deg complement
            # inward (the right end would sit at cx+half-r instead of cx+half+r) — a silently
            # wrong closed profile. A rotation preserves sweep SENSE, so these signs hold at
            # every angle; the outward-bulge test samples the arcs to prove it.
            _arc_op(rc[0], rc[1], c[0], c[1], d[0], d[1], -1,
                    "slot: right cap", construction),
            _line_op(d[0], d[1], a[0], a[1], "slot: bottom edge", construction),
        ]
    if kind == "rounded_rect":
        # v0.7.4 convenience primitive: centred rectangle with corner radius -> 8 exact
        # line/arc entities, CCW from the bottom edge. Validation keeps r < min(W,H)/2, so
        # every straight edge is non-degenerate.
        width = float(prim["width"])
        height = float(prim["height"])
        r = float(prim["radius"])
        cx = float(prim.get("cx") or 0.0)
        cy = float(prim.get("cy") or 0.0)
        hw, hh = width / 2.0, height / 2.0
        xl, xr = cx - hw + r, cx + hw - r
        yb, yt = cy - hh + r, cy + hh - r
        L, R_, B, T = cx - hw, cx + hw, cy - hh, cy + hh
        return [
            _line_op(xl, B, xr, B, "rounded_rect: bottom edge", construction),
            _arc_op(xr, yb, xr, B, R_, yb, 1, "rounded_rect: BR corner", construction),
            _line_op(R_, yb, R_, yt, "rounded_rect: right edge", construction),
            _arc_op(xr, yt, R_, yt, xr, T, 1, "rounded_rect: TR corner", construction),
            _line_op(xr, T, xl, T, "rounded_rect: top edge", construction),
            _arc_op(xl, yt, xl, T, L, yt, 1, "rounded_rect: TL corner", construction),
            _line_op(L, yt, L, yb, "rounded_rect: left edge", construction),
            _arc_op(xl, yb, L, yb, xl, B, 1, "rounded_rect: BL corner", construction),
        ]
    raise ValueError("unsupported profile kind %r (ir_schema should have rejected it)" % kind)


def _profile_ops(prim):
    """[_profile_op] as a LIST — slot/rounded_rect expand to several exact entities."""
    op = _profile_op(prim)
    return op if isinstance(op, list) else [op]


def _line_op(x1, y1, x2, y2, note, construction=False):
    # Coordinates rounded to 9 decimals (the _sketch_frame_transform convention): pure
    # cosmetics for decimal arithmetic (-0.025 + 0.01 -> -0.015000000000000001); shared
    # endpoints stay exactly equal because both sides round the same computed value.
    params = {"entity_type": "line",
              "x1": round(float(x1), 9), "y1": round(float(y1), 9),
              "x2": round(float(x2), 9), "y2": round(float(y2), 9)}
    if construction:
        params["construction"] = True
    return Op("add_sketch_entity", params, note)


def _arc_op(cx, cy, x1, y1, x2, y2, direction, note, construction=False):
    params = {"entity_type": "arc_center",
              "cx": round(float(cx), 9), "cy": round(float(cy), 9),
              "x1": round(float(x1), 9), "y1": round(float(y1), 9),
              "x2": round(float(x2), 9), "y2": round(float(y2), 9),
              "direction": direction}
    if construction:
        params["construction"] = True
    return Op("add_sketch_entity", params, note)


def lower_extrude(node, up_to_face_index=None):
    """extrude -> extrude_feature on the active sketch.
    End condition: explicit 'end' field first (blind | through_all | up_to_surface — the latter
    needs the resolver-produced face index), else the v0-exp back-compat forms.
    'reversed': true (the recipe's flipped-direction flag) maps to the tool's 'reverse' param."""
    op = node["operation"]
    end = node.get("end")
    extra = {"reverse": True} if node.get("reversed") is True else {}
    if end == "up_to_surface":
        if up_to_face_index is None:
            raise ValueError("up_to_surface lowering requires a resolved face index")
        params = {"feature_type": op, "up_to_face_index": int(up_to_face_index)}
        params.update(extra)
        return [Op("extrude_feature", params,
                   "extrude %s (up to surface, face #%d)" % (op, up_to_face_index))]
    if end == "mid_plane":
        params = {"feature_type": op, "depth": float(node["depth"]), "mid_plane": True}
        params.update(extra)
        return [Op("extrude_feature", params, "extrude %s (mid-plane, total %g)" % (op, node["depth"]))]
    is_through = (end == "through_all" or node.get("through") is True
                  or node.get("depth") in ("through_all", "through"))
    if is_through:
        params = {"feature_type": op, "through": True}
        params.update(extra)
        return [Op("extrude_feature", params, "extrude %s (through-all)" % op)]
    params = {"feature_type": op, "depth": float(node["depth"])}
    params.update(extra)
    return [Op("extrude_feature", params, "extrude %s (blind)" % op)]


def lower_rib(node):
    """rib -> create_rib on the active sketch (single-line open profile, classic mid-plane rib).
    Non-default flags are emitted only when set — deterministic minimal params."""
    params = {"thickness": float(node["thickness"])}
    if node.get("two_sided") is False:
        params["two_sided"] = False
    if node.get("reverse_thickness_dir") is True:
        # Single-sided ribs only (two_sided=false): thicken the opposite side of the sketch
        # normal. Passthrough added v0.7.0 for tool-surface completeness.
        params["reverse_thickness_dir"] = True
    if node.get("reverse_material_dir") is True:
        params["reverse_material_dir"] = True
    if node.get("is_norm_to_sketch") is True:
        params["is_norm_to_sketch"] = True
    return [Op("create_rib", params, "rib t=%g" % float(node["thickness"]))]


def lower_sweep(node):
    """sweep -> ONE extrude_feature(sweep). The PROFILE is the ACTIVE sketch (the immediately
    preceding sketch node — the tool captures its name and exits it, like extrude); the PATH
    sketch is selected BY NAME, so `path_sketch` carries the node_feature sentinel and the
    compiler substitutes the path node's RUNTIME sketch name (recorded from create_sketch's
    activeSketch — localization/numbering-proof, same principle as loft's profiles).
    Boss-only: the execution surface has no sweep cut."""
    return [Op("extrude_feature",
               {"feature_type": "sweep", "path_sketch": node_feature(node["path"])},
               "sweep along path node %s" % node["path"])]


def lower_linear_pattern(node):
    """linear_pattern -> ONE create_pattern(linear). The seed feature's RUNTIME name flows via
    node_feature (circular_pattern's mechanism). direction is the canonical axis letter (the
    tool selects the default plane whose normal is that axis); 'flip' (v0.7.0 tool extension)
    patterns toward the NEGATIVE axis. Single direction by design — the tool's documented D2 is
    unwired in C# (recorded gap); grids compose as a pattern OF a pattern."""
    params = {"pattern_type": "linear",
              "feature_name": node_feature(node["feature"]),
              "direction": node["direction"].upper(),
              "spacing": float(node["spacing"]),
              "count": int(node["count"])}
    if node.get("flip") is True:
        params["flip"] = True
    return [Op("create_pattern", params,
               "linear pattern %dx @ %g m along %s%s (seed = node %s)"
               % (int(node["count"]), float(node["spacing"]), node["direction"].upper(),
                  " (flipped)" if node.get("flip") is True else "", node["feature"]))]


DEFAULT_MATERIAL_LIBRARY = "SolidWorks Materials"   # the tool's own default (tool-schemas.json)


def lower_material(material):
    """graph-level material -> ONE set_part_material AFTER the last node (material is document
    state, not a tree feature; part graphs only — validation enforces it).

    `library` is OMITTED, not defaulted, when the graph names none: the tool applies its own
    default, so the request stays byte-identical to the historical path. Use
    DEFAULT_MATERIAL_LIBRARY only to describe what the call resolves to (a warning), never to send
    a library the author did not ask for."""
    params = {"material_name": material["name"]}
    if material.get("library"):
        params["library"] = material["library"]
    return [Op("set_part_material", params, "material '%s'" % material["name"])]


def lower_revolve(node):
    """revolve -> extrude_feature(revolve) on the active sketch. The axis (normally the
    profile's construction centerline) is passed as its exact 2D endpoints; the tool locates the
    centerline ON that line among the sketch's own segments — matching endpoints, then collinearity
    — and adds a dedicated construction centerline there if the sketch has none. It never converts
    a profile edge, so a shaft drawn with its edge ON the axis keeps a closed contour.
    Angle: IR carries RADIANS (the recipe's SI dimension, C4); the tool boundary takes DEGREES
    (the execution layer snaps near-360° to an exact full revolve).
    operation: 'boss' (default) adds material; 'cut' removes it — the tool flips the revolve's
    IsCut flag. Omitted operation emits byte-identical params to the boss-only historical path."""
    axis = node["axis"]
    angle_rad = float(node.get("angle") or (2.0 * math.pi))
    is_cut = node.get("operation", "boss") == "cut"
    params = {"feature_type": "revolve",
              "angle": round(math.degrees(angle_rad), 6),
              "axis_x1": float(axis["x1"]), "axis_y1": float(axis["y1"]),
              "axis_x2": float(axis["x2"]), "axis_y2": float(axis["y2"])}
    if is_cut:
        params["is_cut"] = True
    return [Op("extrude_feature", params,
               "revolve %s %.6g rad about (%g,%g)-(%g,%g)"
               % ("cut" if is_cut else "boss", angle_rad,
                  axis["x1"], axis["y1"], axis["x2"], axis["y2"]))]


def lower_circular_pattern(node):
    """circular_pattern -> reference axis from two datum planes + create_pattern(circular).
    Two runtime names flow through sentinels: the created AXIS (FROM_PREV_FEATURE — the
    immediately preceding op's response) and the SEED feature (node_feature(id) — recorded when
    that node executed). Both are localization-proof by construction."""
    d1, d2 = node["axis"]["datums"]
    return [
        Op("add_reference_geometry",
           {"type": "axis",
            "entity1_name": _PLANE[d1], "entity1_type": "PLANE",
            "entity2_name": _PLANE[d2], "entity2_type": "PLANE"},
           "pattern axis: %s ∩ %s" % (_PLANE[d1], _PLANE[d2])),
        Op("create_pattern",
           {"pattern_type": "circular",
            "feature_name": node_feature(node["feature"]),
            "axis_name": FROM_PREV_FEATURE,
            "count": int(node["count"]),
            "angle": float(node["angle_deg"]),
            "equal_spacing": node.get("equal_spacing", True) is not False},
           "circular pattern %dx @ %g deg (seed = node %s)"
           % (int(node["count"]), float(node["angle_deg"]), node["feature"])),
    ]


def lower_loft(node, profile_names):
    """loft -> ONE extrude_feature(loft) over 2+ profile sketches (COVERED since v0.5.4 — grown for
    level-2-2's Loft2 frustum). The profiles are referenced by their RUNTIME sketch names, which the
    compiler resolves from node_features (localization/numbering-proof, same principle as
    circular_pattern's seed) and passes here. They are serialized as a JSON-array STRING — the tool's
    contract. The profile sketches are ordinary earlier sketch nodes; the loft selects them by name,
    so they need not immediately precede it (grammar relaxation vs extrude/revolve/rib)."""
    return [Op("extrude_feature",
               {"feature_type": "loft", "profiles": json.dumps(profile_names)},
               "loft over %d profiles %s" % (len(profile_names), profile_names))]


def lower_sheet_metal(node):
    """sheet_metal (base flange) -> sheet_metal_feature(base_flange) on the active sketch (the
    profile sketch must immediately precede, like extrude). bend_radius omitted => the tool's
    default (= thickness); k_factor omitted => the tool default 0.5."""
    params = {"feature_type": "base_flange", "thickness": float(node["thickness"])}
    if node.get("bend_radius") is not None:
        params["bend_radius"] = float(node["bend_radius"])
    if node.get("k_factor") is not None:
        params["k_factor"] = float(node["k_factor"])
    if node.get("reverse_thickness") is True:
        # Thickness DIRECTION: reproduce the ORIGINAL base flange's own flag (the reader reports
        # it) — a wrong side puts every later bend-sketch plane off the sheet.
        params["reverse_thickness"] = True
    if node.get("symmetric_thickness") is True:
        # Material both ways off the sketch plane (±t/2). Reproduce the ORIGINAL's own flag: the
        # thickness-direction flags also define the intrinsic sheet orientation that downstream
        # bend directions are measured against (the 4-1 Bend1 mirror-fold lesson).
        params["symmetric_thickness"] = True
    return [Op("sheet_metal_feature", params,
               "base flange t=%g" % float(node["thickness"]))]


def lower_sketched_bend(node):
    """sketched_bend -> sheet_metal_feature(sketched_bend) on the active bend-line sketch.
    Angle: IR carries RADIANS (recipe SI, C4); the tool boundary takes DEGREES. radius OMITTED
    in the node => use_default_radius=true (the sheet's default — matches a recipe sketched_bend
    block with no radius). The fixed anchor's 3D point passes STRAIGHT THROUGH as fixed_x/y/z:
    the coordinate IS the side selector (SolidWorks derives which half stays put from the pick
    point), so unlike edge/face anchors there is NO index-resolution step."""
    angle_rad = float(node.get("angle") or (math.pi / 2.0))
    near = node["fixed"]["near"]
    params = {"feature_type": "sketched_bend",
              "angle": round(math.degrees(angle_rad), 6),
              "fixed_x": float(near[0]), "fixed_y": float(near[1]), "fixed_z": float(near[2])}
    if node.get("radius") is not None:
        params["bend_radius"] = float(node["radius"])
    else:
        params["use_default_radius"] = True
    if node.get("position") not in (None, "centerline"):
        params["bend_position"] = node["position"]
    if node.get("flip") is True:
        params["flip"] = True
    return [Op("sheet_metal_feature", params,
               "sketched bend %.6g rad, fixed near (%g,%g,%g)"
               % (angle_rad, near[0], near[1], near[2]))]


def lower_edge_flange(node, edge_index):
    """edge_flange — TWO MODES (v0.7.0):

    SIMPLE LENGTH mode (`length` given, no frame/profile — the FORWARD mode): ONE
    sheet_metal_feature(edge_flange) call with the anchor-resolved edge_index (v0.7.0 C#
    extension — the coordinate pick misses real edges, KNOWN-LIMITATIONS #6) — a
    full-edge-width flange of that length. Position/radius stay the tool defaults
    (material_inside / sheet default); validation already rejected overrides.

    CUSTOM PROFILE mode (frame+profile given — the reverse-replay mode) ->
    SolidWorks' documented three-phase flow:
    (1) sheet_metal_feature(edge_flange_sketch): select the attach edge BY INDEX (the compiler
        resolves the node's edge anchor via resolve_edges_by_anchor — the coordinate pick misses
        real edges, KNOWN-LIMITATIONS #6, proven live on 4-1's slanted EF2 edge), generate the
        edge-linked profile sketch (an arbitrary user sketch is NOT accepted by the flange API),
        CLEAR its default content, leave it active. The op echoes the generated sketch's MEASURED
        frame and the compiler transforms every following 2D coordinate into it (node `frame` =
        the ORIGINAL profile sketch's frame — REQUIRED: the generated frame is unpredictable).
    (2) one add_sketch_entity per `profile` primitive (original coordinates, frame-transformed).
    (3) sheet_metal_feature(edge_flange_finish): exit + InsertSheetMetalEdgeFlange2 from the
        edited sketch + the same edge (same index — the sketch-gen op adds no solid geometry, so
        edge enumeration is stable between the two). radius/position replay the ORIGINAL's
        read-back values (IR-ADR-014); radius OMITTED => the sheet default; position OMITTED =>
        material_inside (the tool default). Angle: IR RADIANS -> tool-boundary DEGREES (C4)."""
    angle_deg = math.degrees(float(node.get("angle") or (math.pi / 2.0)))
    if node.get("length") is not None:
        return [Op("sheet_metal_feature",
                   {"feature_type": "edge_flange", "edge_index": int(edge_index),
                    "flange_length": float(node["length"]), "angle": angle_deg},
                   "edge flange (simple): length %g on edge #%d"
                   % (float(node["length"]), int(edge_index)))]
    profile, welds = contours.weld_profile(node.get("profile") or [])
    gen = {"feature_type": "edge_flange_sketch", "edge_index": int(edge_index),
           "angle": angle_deg}
    if node.get("flip") is True:
        gen["flip"] = True
    ops = [Op("sheet_metal_feature", gen,
              "edge flange: generate+clear the profile sketch on edge #%d%s"
              % (int(edge_index), _weld_note(welds)))]
    for prim in profile:
        ops.extend(_profile_ops(prim))
    fin = {"feature_type": "edge_flange_finish", "edge_index": int(edge_index),
           "angle": angle_deg}
    if node.get("radius") is not None:
        fin["bend_radius"] = float(node["radius"])
    else:
        fin["use_default_radius"] = True
    if node.get("position") not in (None, "material_inside"):
        fin["bend_position"] = node["position"]
    ops.append(Op("sheet_metal_feature", fin, "edge flange: create from the edited profile"))
    return ops


def lower_mirror(node, feature_names):
    """mirror -> ONE create_pattern(mirror) over the referenced nodes' RUNTIME feature names
    (resolved by the compiler from node_features — localization/numbering-proof, same principle
    as circular_pattern's seed / loft's profiles). Serialized as a JSON-array STRING (ADR-022)."""
    d = node["plane"]["datum"]
    return [Op("create_pattern",
               {"pattern_type": "mirror",
                "features_json": json.dumps(feature_names),
                "plane": _PLANE[d]},
               "mirror %s about %s" % (feature_names, _PLANE[d]))]


def lower_component(node):
    """component -> ONE insert_component with the FULL transform (13 numbers, the reader's
    layout) as a JSON-array STRING (ADR-022). The transform is the placement input for a fixed
    component and the best deterministic initial placement for a floating one (mates then
    constrain it — starting at the original's final position keeps the solve trivial and the
    stop-check comparable). `fixed` is always sent explicitly (deterministic state, C7 spirit)."""
    src = node["source"]
    params = {"file_path": src["path"],
              "transform_json": json.dumps([float(v) for v in node["transform"]]),
              "fixed": node.get("fixed") is True}
    if node.get("config"):
        params["config"] = node["config"]
    name = src["path"].replace("\\", "/").rsplit("/", 1)[-1]
    return [Op("insert_component", params,
               "component %s%s" % (name, " (fixed)" if node.get("fixed") is True else ""))]


def lower_mate(node, sides):
    """mate -> ONE add_mate. sides = [(runtime_component_name, 'face'|'edge', index), ...2] —
    the resolver already turned each side's COMPONENT-LOCAL anchor into an entity index
    (index-first selection, KNOWN-LIMITATIONS #6). Values: distance passes SI meters through;
    angle converts IR RADIANS -> tool-boundary DEGREES (C4). alignment/flip verbatim (C7)."""
    (an, ak, ai), (bn, bk, bi) = sides
    params = {"mate_type": node["mate_type"],
              "a_component": an, "a_kind": ak, "a_index": int(ai),
              "b_component": bn, "b_kind": bk, "b_index": int(bi)}
    if node.get("alignment"):
        params["alignment"] = node["alignment"]
    if node.get("flip") is True:
        params["flip"] = True
    if node["mate_type"] == "distance":
        params["value"] = float(node["value"])
    elif node["mate_type"] == "angle":
        params["value"] = round(math.degrees(float(node["value"])), 6)
    return [Op("add_mate", params,
               "%s mate %s[%s#%d] ~ %s[%s#%d]"
               % (node["mate_type"], an, ak, int(ai), bn, bk, int(bi)))]


def _thread_params(node, thread, edge_index, defaults=None, pilot=None, pilot_depth=None,
                   counterbore=None):
    """Build the add_cosmetic_thread params for one thread declaration.

    `node` carries the optional explicit `callout`; `thread` is the thread block (a hole's
    `thread` object or the cosmetic_thread node itself); `defaults` is graph.thread_defaults.
    Node fields beat defaults, defaults beat the module constants.

    THREE facts this function exists to keep straight:

    1. `diameter` is the NOMINAL MAJOR diameter, never the pilot. InsertCosmeticThread3's
       Diameter is the thread diameter and an internal thread's ICosmeticThreadFeatureData.
       DiameterType is swCosmeticThread_MajorDiameter, so passing the tap drill would draw the
       thread on the wrong circle — a silent, plausible-looking wrong model.
    2. `size` is a TEXT key into the install's thread database ('M5x0.8'), not a number, and
       the cosmetic tool has NO pitch argument: the pitch comes from that database row. The
       pitch is resolved here only to build the callout and to look up the tap drill.
    3. `end_type` is swCosmeticEndConditions_e: 0 blind, 2 through. 2 is NOT the extrude's
       through-all and there is no 3-way "through" in the real-Thread family — do not unify.
       For a through thread the depth argument is sent as 0 so a stale depth can never leak in.
    """
    defaults = defaults or {}
    desig = thread["designation"]
    major_mm, parsed_pitch, _canonical = thread_table.parse_designation(desig)
    pitch_mm = thread_table.resolve_pitch(
        major_mm, thread.get("pitch") if thread.get("pitch") is not None else parsed_pitch)
    standard = thread.get("standard") or defaults.get("standard") or "ISO"
    style = thread.get("callout_style") or defaults.get("callout_style") or "ascii"
    handedness = thread.get("handedness") or defaults.get("handedness") or "right"

    depth = thread.get("depth")
    is_through = depth is not None and depth in THROUGH_DEPTHS
    params = {
        # None = "resolved later": lower_hole overwrites it with THREAD_EDGE_PENDING. A concrete
        # index here is the cosmetic_thread node's already-resolved mouth circle.
        "edge_index": None if edge_index is None else int(edge_index),
        "standard": THREAD_STANDARD_ENUM[standard],
        "standard_type": "Tapped Hole",
        # The DATABASE spelling (always carries the pitch) — the drawing shows the short form.
        "size": thread_table.canonical_size(major_mm, pitch_mm),
        "diameter": major_mm / 1000.0,
        "end_type": 2 if is_through else 0,
        "note": (thread.get("callout")
                 or thread_table.derive_callout(
                     thread_table.callout_designation(major_mm, pitch_mm),
                     thread_class=_thread_class(thread, defaults),
                     depth=None if is_through else float(depth),
                     pilot=pilot, pilot_depth=pilot_depth,
                     counterbore=counterbore, style=style, handedness=handedness)),
    }
    if not is_through:
        params["depth"] = float(depth)
    return params


def _thread_class(thread, defaults):
    """The canonical tolerance class ('6H') from the block or the graph defaults, else None."""
    for source in (thread, defaults or {}):
        value = source.get("class")
        if value:
            return str(value).strip().upper()
    return None


def lower_cosmetic_thread(node, edge_index, defaults=None):
    """cosmetic_thread -> ONE add_cosmetic_thread op on a resolver-matched circular edge.

    The edge is the MOUTH of the bore, resolved on the geometry as it stands: a cosmetic thread
    consumes nothing, so unlike fillet/chamfer there is no pre-consumption anchor rule. SI
    metres in, metres out — the tool is defined in metres, so no conversion lives here (C4's
    rule: conversions happen at the tool boundary, and this boundary is already SI).

    A standalone node has no hole to cross-check, so `pilot`/`pilot_depth` are None and the
    callout names the thread only.
    """
    params = _thread_params(node, node, edge_index, defaults=defaults)
    return [Op("add_cosmetic_thread", params,
               "cosmetic thread %s on edge #%d (%s)"
               % (params["size"], int(edge_index), params["note"]))]


def lower_hole(node, face_index, center2d, thread_edge=None, defaults=None, thread_anchor=None):
    """hole -> sketch + circle + cut, then optionally a counterbore and a cosmetic thread.

    ORDER IS LOAD-BEARING: the counterbore is cut FIRST. It changes the entry face, and a
    cosmetic thread is a SUBFEATURE of the cut it sits on — cutting the counterbore after the
    pilot would re-intersect the threaded mouth edge and the thread would not survive. The
    counterbore cut and the pilot cut are therefore separate sketch+cut pairs on the SAME
    resolved entry face (a coaxial counterbore), counterbore first.

    DEPTH ARITHMETIC: with a counterbore present, `hole.depth` is measured from the
    COUNTERBORE FLOOR, so the pilot's absolute cut depth is counterbore.depth + hole.depth.
    That is why the emitted cut depth is a sum and not `hole.depth` verbatim.

    `thread_edge` is the resolver's index for the mouth circle. It cannot be known when the ops
    are PLANNED (the circle does not exist until the cut runs), so the caller passes
    THREAD_EDGE_PENDING and the compiler resolves it between the last cut and the thread op; the
    anchor travels in the op params as `thread_ref` (a resolver ref, popped before the call).
    None means the node declares no thread.
    """
    r = float(node["diameter"]) / 2.0
    cx, cy = center2d
    is_through = node["depth"] in THROUGH_DEPTHS
    counterbore = node.get("counterbore")
    thread = node.get("thread")
    ops = []

    if counterbore:
        ops += [
            Op("create_sketch", {"on_face": True, "face_index": int(face_index)},
               "hole: counterbore sketch on face #%d" % int(face_index)),
            Op("add_sketch_entity",
               {"entity_type": "circle", "cx": cx, "cy": cy,
                "radius": float(counterbore["diameter"]) / 2.0},
               "hole: counterbore circle %s mm"
               % thread_table.format_dia_mm(float(counterbore["diameter"]))),
            Op("extrude_feature", {"feature_type": "cut", "depth": float(counterbore["depth"])},
               "hole: counterbore cut %.6g m deep" % float(counterbore["depth"])),
        ]

    if is_through:
        cut_params = {"feature_type": "cut", "through": True}
        cut_note = "hole: through-all cut"
    else:
        cut_depth = float(node["depth"])
        if counterbore:
            cut_depth += float(counterbore["depth"])
        cut_params = {"feature_type": "cut", "depth": cut_depth}
        # Metres, like every other op note in this module: the note is read by an engineer
        # diffing the node log, and a unit that appears ONLY here would be a trap.
        cut_note = "hole: blind cut %.6g m deep" % cut_depth
        if counterbore:
            cut_note += " (from the counterbore floor)"

    ops += [
        Op("create_sketch", {"on_face": True, "face_index": int(face_index)},
           "hole: sketch on top face #%d" % int(face_index)),
        Op("add_sketch_entity", {"entity_type": "circle", "cx": cx, "cy": cy, "radius": r},
           "hole: circle at centre"),
        Op("extrude_feature", cut_params, cut_note),
    ]

    if thread:
        if thread_edge is None:
            raise FeatureError(
                "hole '%s' declares a thread but the mouth edge was not resolved — the "
                "compiler must resolve it after the cuts and pass THREAD_EDGE_PENDING in"
                % node.get("id"),
                code="THREAD_EDGE_UNRESOLVED", node_id=node.get("id"))
        deferred = thread_edge == THREAD_EDGE_PENDING
        params = _thread_params(node, thread,
                                None if deferred else thread_edge,
                                defaults=defaults,
                                pilot=float(node["diameter"]),
                                pilot_depth=None if is_through else float(node["depth"]),
                                counterbore=counterbore)
        if deferred:
            # The circle does not exist yet. Put the sentinel in the op and carry the ANCHOR
            # beside it; the compiler resolves the edge after the cuts and pops the anchor
            # before the call, so the tool never sees either.
            params["edge_index"] = THREAD_EDGE_PENDING
            params[THREAD_REF_PARAM] = thread_ref(thread_anchor)
            note = ("hole: cosmetic thread %s on the mouth circle, resolved after the cut (%s)"
                    % (params["size"], params["note"]))
        else:
            note = ("hole: cosmetic thread %s on edge #%d (%s)"
                    % (params["size"], int(thread_edge), params["note"]))
        ops.append(Op("add_cosmetic_thread", params, note))
    return ops


def lower_fillet(node, edge_indices):
    """fillet -> ONE add_edge_feature over the resolver-matched edge indices.
    edge_indices is serialized as a JSON-array STRING — the tool's contract (ADR-022 idiom)."""
    idx = sorted(int(i) for i in edge_indices)
    return [Op("add_edge_feature",
               {"feature_type": "fillet",
                "radius_or_distance": float(node["radius"]),
                "edge_indices": "[%s]" % ",".join(str(i) for i in idx)},
               "fillet r=%g on edges %s" % (float(node["radius"]), idx))]


def lower_chamfer(node, edge_indices):
    """chamfer -> ONE add_edge_feature(chamfer) over the resolver-matched edge indices (same hybrid
    edge anchors as fillet, IR-ADR-008). Two modes:
      - distance_angle: setback D1 + angle. IR carries the angle in RADIANS (recipe SI, C4); the
        tool boundary takes DEGREES, so lowering converts (mirrors lower_revolve).
      - distance_distance: D1 + D2 (both meters). Directional — 'flip' swaps which face gets which
        setback (the tool's FlipDirection option, auto-recoverable like rib's reverse)."""
    idx = sorted(int(i) for i in edge_indices)
    ctype = node.get("chamfer_type", "distance_angle")
    params = {"feature_type": "chamfer",
              "chamfer_type": ctype,
              "radius_or_distance": float(node["distance"]),
              "edge_indices": "[%s]" % ",".join(str(i) for i in idx)}
    if node.get("flip") is True:
        params["chamfer_flip"] = True
    if ctype == "distance_distance":
        params["distance2"] = float(node["distance2"])
        note = "chamfer %g×%g on edges %s" % (float(node["distance"]), float(node["distance2"]), idx)
    else:
        angle_rad = float(node.get("angle") or (math.pi / 4.0))
        params["angle"] = round(math.degrees(angle_rad), 6)
        note = "chamfer d=%g @ %.6g rad on edges %s" % (float(node["distance"]), angle_rad, idx)
    return [Op("add_edge_feature", params, note)]


def lower_shell(node, face_indices):
    """shell -> ONE create_shell over the resolver-matched FACE indices (the faces left open).

    Indices are serialized as a JSON-array STRING — the add_edge_feature contract idiom (ADR-022).
    An EMPTY list is a closed (fully hollow) shell, so the param is omitted entirely rather than
    sent as "[]": the tool then calls InsertFeatureShell with nothing selected. Non-default flags
    are emitted only when set (lower_rib discipline)."""
    idx = sorted(set(int(i) for i in face_indices))
    params = {"thickness": float(node["thickness"])}
    if idx:
        params["face_indices"] = "[%s]" % ",".join(str(i) for i in idx)
    if node.get("outward") is True:
        params["outward"] = True
    return [Op("create_shell", params,
               "shell t=%g outward=%s removing faces %s"
               % (float(node["thickness"]), bool(node.get("outward")), idx))]
