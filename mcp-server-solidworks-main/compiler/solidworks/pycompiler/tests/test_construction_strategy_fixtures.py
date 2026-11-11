"""Construction probes, with drawing-component scope explicit; no SolidWorks IO.

These fixtures do not represent complete ZCT01 or valve-body reference parts.
"""
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _COMPILER_ROOT not in sys.path:
    sys.path.insert(0, _COMPILER_ROOT)

from pycompiler.compiler import compile_and_run
from pycompiler import contours
from pycompiler.ir_schema import canonical_sketch_frame, validate


def line(a, b):
    return {"kind": "line", "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}


def closed_lines(points):
    return [line(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]


def zct01_left_bearing_seat_graph():
    # Only the first 22 mm seat: axial/end-view OD60, explicitly called OD47 bore.
    # The exterior through the remaining 85 mm bore chain enters a cast transition.
    # The X40 top port and separate G3/8 bottom thread are outside this component.
    return {
        "schema_version": "0.8.0-draft", "units": "meters",
        "expect": {"bbox_mm": [22, 60, 60]},
        "nodes": [
            {"id": "seat_section", "type": "sketch", "ref": {"datum": "front"},
             "profile": closed_lines([(0, .030), (.022, .030),
                                      (.022, .0235), (0, .0235)])},
            {"id": "seat_revolve", "type": "revolve", "sketch": "seat_section",
             "axis": {"x1": 0, "y1": 0, "x2": .022, "y2": 0}},
        ],
    }


def fati_main_chamber_graph():
    # A-A X75/Y80 locates the main Z axis. B-B Z130/95/25 locates its steps.
    # A-A R38 = OD76; B-B end OD60 starts at Z105 = 130-25.
    # The M42x2 end bore is represented by its 40 mm pilot, without annotation.
    # This component omits the C-C base chamber, side branches and their ports.
    # Right-plane local x points toward -Z; its normal offsets to global X75.
    section = closed_lines([
        (0, .118), (-.105, .118), (-.105, .110), (-.130, .110),
        (-.130, .100), (-.095, .100), (-.095, .110), (0, .110),
    ])
    holes = [{"kind": "circle", "diameter": .010, "cx": x, "cy": y}
             for x in (.040, .110) for y in (.045, .115)]
    return {
        "schema_version": "0.8.0-draft", "units": "meters",
        "expect": {"bbox_mm": [90, 90, 130]},
        "nodes": [
            {"id": "main_section", "type": "sketch",
             "ref": {"datum": "right", "offset": .075}, "profile": section},
            {"id": "main_chamber", "type": "revolve", "sketch": "main_section",
             "axis": {"x1": 0, "y1": .080, "x2": -.130, "y2": .080}},
            {"id": "flange_profile", "type": "sketch", "ref": {"datum": "front"},
             "profile": [{"kind": "rounded_rect", "width": .090, "height": .090,
                          "radius": .010, "cx": .075, "cy": .080}]},
            {"id": "flange", "type": "extrude", "sketch": "flange_profile",
             "operation": "boss", "depth": .012},
            {"id": "flange_holes_profile", "type": "sketch", "ref": {"datum": "front"},
             "profile": holes},
            {"id": "flange_holes", "type": "extrude", "sketch": "flange_holes_profile",
             "operation": "cut", "end": "mid_plane", "depth": .026},
            {"id": "flange_bore_profile", "type": "sketch", "ref": {"datum": "front"},
             "profile": [{"kind": "circle", "diameter": .060, "cx": .075, "cy": .080}]},
            {"id": "flange_bore", "type": "extrude", "sketch": "flange_bore_profile",
             "operation": "cut", "end": "mid_plane", "depth": .026},
        ],
    }


def component_theoretical_volumes_mm3():
    seat = math.pi * (30**2 - 23.5**2) * 22
    rounded_flange_area = 90**2 - (4 - math.pi) * 10**2
    chamber = (math.pi * (38**2 * 105 + 30**2 * 25 - 30**2 * 95 - 20**2 * 35)
               + (rounded_flange_area - math.pi * 38**2) * 12
               - 4 * math.pi * 5**2 * 12)
    return {"zct01_left_bearing_seat": seat, "fati_main_chamber": chamber}


def nested_loop_trim_graph():
    # Synthetic intersection, not a drawing reference: cut across [-20,+20] Z.
    return {
        "schema_version": "0.8.0-draft", "units": "meters",
        "expect": {"bbox_mm": [30, 30, 40]},
        "nodes": [
            {"id": "stock_profile", "type": "sketch", "ref": {"datum": "front"},
             "profile": [{"kind": "rectangle", "width": .100, "height": .080}]},
            {"id": "stock", "type": "extrude", "sketch": "stock_profile",
             "operation": "boss", "end": "mid_plane", "depth": .040},
            {"id": "trim_profile", "type": "sketch", "ref": {"datum": "front"},
             "profile": [{"kind": "rectangle", "width": .120, "height": .100},
                         {"kind": "circle", "diameter": .030}]},
            {"id": "trim", "type": "extrude", "sketch": "trim_profile",
             "operation": "cut", "end": "mid_plane", "depth": .050},
        ],
    }


def curved_outline_graph():
    # Generic exact quarter-circle blending into a vertical edge.
    return {
        "schema_version": "0.8.0-draft", "units": "meters",
        "expect": {"bbox_mm": [8, 20, 40]},
        "nodes": [
            {"id": "outline", "type": "sketch", "ref": {"datum": "front"},
             "profile": [{"kind": "arc", "cx": 0, "cy": .020,
                          "x1": 0, "y1": 0, "x2": .020, "y2": .020, "dir": 1},
                         line((.020, .020), (.020, .040)),
                         line((.020, .040), (0, .040)), line((0, .040), (0, 0))]},
            {"id": "web", "type": "extrude", "sketch": "outline",
             "operation": "boss", "end": "mid_plane", "depth": .008},
        ],
    }


class FeasibilityPort:
    def __init__(self):
        self.state_version = 0
        self.feature_index = 0
        self.sketch_index = 0
        self.calls = []
        self.pending_sketch_frame = None

    def get_state(self):
        return self.state_version

    def execute(self, tool, params, state_version):
        self.calls.append((tool, params))
        self.state_version += 1
        state = {"features": ["Feature%d" % self.feature_index]}
        geometry = None
        if tool == "add_reference_geometry" and params.get("type") == "plane":
            datum = params.get("ref_plane_name", "Top Plane").split()[0].lower()
            self.pending_sketch_frame = canonical_sketch_frame(
                {"datum": datum, "offset": params.get("offset", 0)})
        if tool == "create_sketch":
            self.sketch_index += 1
            plane = params.get("plane", "Top Plane").casefold()
            datum = "right" if "right" in plane else "front" if "front" in plane else "top"
            frame = self.pending_sketch_frame or canonical_sketch_frame({"datum": datum})
            self.pending_sketch_frame = None
            geometry = {"frame": frame}
            state["activeSketch"] = "Sketch%d" % self.sketch_index
        else:
            self.feature_index += 1
        return {"status": "COMPLETED", "stateVersion": self.state_version,
                "cadState": state, "result_geometry": geometry, "error": None}


def compile_probe(graph):
    assert validate(graph) == []
    result = compile_and_run(FeasibilityPort(), graph)
    assert result.status == "COMPLETED", result.to_dict()
    assert result.nodes_completed == len(graph["nodes"])


def test_zct01_first_bearing_seat_uses_dimensioned_diameters_only():
    graph = zct01_left_bearing_seat_graph()
    compile_probe(graph)
    assert graph["expect"]["bbox_mm"] == [22, 60, 60]
    assert len(graph["nodes"]) == 2
    assert graph["nodes"][0]["profile"] == closed_lines([
        (0, .030), (.022, .030), (.022, .0235), (0, .0235)])


def test_fati_chamber_keeps_aa_location_separate_from_bb_axial_steps():
    graph = fati_main_chamber_graph()
    compile_probe(graph)
    frame = canonical_sketch_frame(graph["nodes"][0]["ref"])
    assert frame["origin"] == [.075, 0, 0]
    assert frame["xdir"] == [0, 0, -1]
    assert graph["nodes"][1]["axis"]["y1"] == .080
    assert graph["nodes"][2]["profile"][0]["cy"] == .080
    assert graph["expect"]["bbox_mm"] == [90, 90, 130]
    assert len(graph["nodes"][4]["profile"]) == 4


def test_nested_loop_trim_covers_both_sides_of_centered_stock():
    graph = nested_loop_trim_graph()
    compile_probe(graph)
    assert len(contours.profile_rings(graph["nodes"][2]["profile"])) == 2
    stock_half_extent = graph["nodes"][1]["depth"] / 2
    cut_half_extent = graph["nodes"][3]["depth"] / 2
    assert cut_half_extent > stock_half_extent


def test_curved_outline_compiles_without_rectangular_substitution():
    graph = curved_outline_graph()
    compile_probe(graph)
    assert graph["nodes"][0]["profile"][0]["kind"] == "arc"
