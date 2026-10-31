"""Regression from the 2026-10-31 P1 probe; no live CAD or service calls."""
import copy
import json

import pytest

from .test_compiler import FakePort, _arc_points
from pycompiler.compiler import compile_and_run
from pycompiler.ir_schema import canonical_sketch_frame


FRONT = {"origin": [0, 0, 0], "xdir": [1, 0, 0], "ydir": [0, 1, 0]}
BACK = {"origin": [0, 0, -.03], "xdir": [-1, 0, 0], "ydir": [0, 1, 0]}
P1 = {"schema_version": "0.0.1-draft-ir-exp", "units": "meters", "nodes": [
    {"id": "sleeve_profile", "type": "sketch", "ref": {"datum": "front"},
     "profile": [{"kind": "circle", "diameter": .038, "cx": .074, "cy": .095}]},
    {"id": "sleeve", "type": "extrude", "sketch": "sleeve_profile",
     "operation": "boss", "end": "mid_plane", "depth": .06},
    {"id": "bore_profile", "type": "sketch", "ref": {"datum": "front", "offset": -.03},
     "profile": [{"kind": "circle", "diameter": .02, "cx": .074, "cy": .095}]},
    {"id": "shaft_bore", "type": "extrude", "sketch": "bore_profile",
     "operation": "cut", "end": "through_all"},
]}


class MeasuredPort(FakePort):
    def __init__(self, frame=BACK):
        super().__init__(faces=json.dumps({"faces": [
            {"i": 0, "planar": True, "normal": [0, 0, -1], "point": [0, 0, -.03], "area": 1}
        ]}))
        self.face_frame = frame
        self.frame = FRONT
        self.model_centers = []
        self.model_ends = []

    def execute(self, tool, params, sv):
        response = super().execute(tool, params, sv)
        if tool == "create_sketch":
            self.frame = self.face_frame if params.get("on_face") else FRONT
            response["result_geometry"] = {"frame": self.frame}
        if tool == "add_sketch_entity" and params.get("entity_type") == "circle":
            f = self.frame
            self.model_centers.append([f["origin"][i] + params["cx"]*f["xdir"][i]
                                       + params["cy"]*f["ydir"][i] for i in range(3)])
        if tool == "extrude_feature" and not params.get("through") and not params.get("mid_plane"):
            f = self.frame
            x, y = f["xdir"], f["ydir"]
            n = [x[1]*y[2]-x[2]*y[1], x[2]*y[0]-x[0]*y[2], x[0]*y[1]-x[1]*y[0]]
            sign = -1 if params.get("reverse") else 1
            self.model_ends.append([self.model_centers[-1][i]+sign*params["depth"]*n[i] for i in range(3)])
        return response


def test_p1_no_frame_nonzero_circle_keeps_model_center_on_back_face():
    port = MeasuredPort()
    graph = copy.deepcopy(P1)
    result = compile_and_run(port, graph)
    assert result.status == "COMPLETED", result.error
    assert any(t == "create_sketch" and p.get("on_face") for t, p in port.calls)
    assert port.model_centers[-1] == pytest.approx([.074, .095, -.03])
    assert graph == P1, "default frame must not mutate the submitted graph"


@pytest.mark.parametrize("operation", ["boss", "cut"])
@pytest.mark.parametrize("reversed", [False, True])
def test_blind_signed_direction_on_mirrored_support_is_asymmetric(operation, reversed):
    graph = copy.deepcopy(P1)
    graph["nodes"][-1].update(operation=operation, end="blind", depth=.007, reversed=reversed)
    port = MeasuredPort()
    result = compile_and_run(port, graph)
    assert result.status == "COMPLETED", result.error
    assert port.model_ends[-1] == pytest.approx([.074, .095, -.03 + (-.007 if reversed else .007)])
    params = [p for t, p in port.calls if t == "extrude_feature"][-1]
    assert (params.get("strict_direction") is True) == (operation == "boss")
    if operation == "cut":
        assert "strict_direction" not in params


@pytest.mark.parametrize("flipped", [False, True])
def test_cut_direction_flip_is_visible_in_node_operations_and_warnings(flipped):
    class Port(MeasuredPort):
        def execute(self, tool, params, sv):
            response = super().execute(tool, params, sv)
            if tool == "extrude_feature" and params.get("feature_type") == "cut" and flipped:
                response["result_geometry"] = {"direction_flipped": True}
            return response

    result = compile_and_run(Port(), copy.deepcopy(P1))
    assert result.status == "COMPLETED", result.error
    node = result.node_log[-1]
    assert bool(node.get("direction_flipped")) == flipped
    assert any(op.get("direction_flipped") for op in node["ops"]) == flipped
    warnings = [w for w in result.warnings if w["code"] == "DIRECTION_FLIPPED"]
    assert len(warnings) == int(flipped)
    if flipped:
        assert warnings[0]["node_id"] == "shaft_bore"
        assert "三视图" in warnings[0]["message"]
        assert "DIRECTION_FLIPPED" in result.summary()


@pytest.mark.parametrize("frame,code", [
    ({**BACK, "origin": [0, 0, -.031]}, "FRAME_PLANE_MISMATCH"),
    ({**BACK, "xdir": [2, 0, 0]}, "FRAME_INVALID"),
    ({**BACK, "ydir": [-1, 0, 0]}, "FRAME_INVALID"),
])
def test_bad_measured_frame_stops_before_drawing(frame, code):
    port = MeasuredPort(frame)
    result = compile_and_run(port, copy.deepcopy(P1))
    assert result.status == "FAILED"
    assert result.error["code"] == code
    assert len(port.model_centers) == 1


@pytest.mark.parametrize("datum", ["front", "top", "right"])
@pytest.mark.parametrize("offset", [-.03, .03])
@pytest.mark.parametrize("support", ["positive_face", "negative_face", "offset_plane"])
@pytest.mark.parametrize("explicit", [False, True])
def test_signed_offsets_preserve_nonzero_model_coordinates(datum, offset, support, explicit):
    declared = canonical_sketch_frame({"datum": datum, "offset": offset})
    measured = copy.deepcopy(declared)
    if support == "negative_face":
        measured["xdir"] = [-x for x in measured["xdir"]]
    axis = {"right": 0, "top": 1, "front": 2}[datum]
    normal = [0, 0, 0]
    normal[axis] = -1 if support == "negative_face" else 1
    faces = [] if support == "offset_plane" else [{"i": 0, "planar": True,
        "normal": normal, "point": declared["origin"], "area": 1}]
    class Port(FakePort):
        def execute(self, tool, params, sv):
            response = super().execute(tool, params, sv)
            if tool == "create_sketch":
                response["result_geometry"] = {"frame": measured}
            return response
    port = Port(faces=json.dumps({"faces": faces}))
    graph = copy.deepcopy(P1)
    graph["nodes"] = graph["nodes"][2:]
    sketch = graph["nodes"][0]
    sketch["ref"] = {"datum": datum, "offset": offset}
    if explicit:
        sketch["frame"] = declared
    result = compile_and_run(port, graph)
    assert result.status == "COMPLETED", result.error
    circle = next(p for t, p in port.calls if t == "add_sketch_entity")
    actual = [measured["origin"][i] + circle["cx"]*measured["xdir"][i]
              + circle["cy"]*measured["ydir"][i] for i in range(3)]
    expected = [declared["origin"][i] + .074*declared["xdir"][i]
                + .095*declared["ydir"][i] for i in range(3)]
    assert actual == pytest.approx(expected)
    assert any(t == "add_reference_geometry" for t, _ in port.calls) == (support == "offset_plane")
    if support == "offset_plane":
        assert next(p for t, p in port.calls if t == "add_reference_geometry")["offset"] == offset


def test_no_frame_asymmetric_slot_keeps_model_curve_and_arc_sweep():
    graph = copy.deepcopy(P1)
    graph["nodes"][2]["profile"] = [{"kind": "slot", "length": .018, "width": .004,
        "cx": .074, "cy": .095, "angle": .7}]
    mirrored = MeasuredPort()
    ordinary_frame = {**BACK, "xdir": [1, 0, 0]}
    ordinary = MeasuredPort(ordinary_frame)
    assert compile_and_run(mirrored, copy.deepcopy(graph)).status == "COMPLETED"
    assert compile_and_run(ordinary, copy.deepcopy(graph)).status == "COMPLETED"
    def curves(port, frame):
        entities = [p for t, p in port.calls if t == "add_sketch_entity"][1:]
        result = []
        for p in entities:
            points = _arc_points(p, n=20) if p["entity_type"] == "arc_center" else [
                (p["x1"], p["y1"]), (p["x2"], p["y2"])]
            result.extend(tuple(frame["origin"][i] + u*frame["xdir"][i] + v*frame["ydir"][i]
                                for i in range(3)) for u, v in points)
        return result
    for actual, expected in zip(curves(mirrored, BACK), curves(ordinary, ordinary_frame)):
        assert actual == pytest.approx(expected, abs=2e-9)


@pytest.mark.parametrize("frame", [
    {**FRONT, "xdir": [0, 0, 0]}, {**FRONT, "ydir": [1, 0, 0]},
    {**FRONT, "origin": [float("nan"), 0, 0]},
])
def test_invalid_declared_frame_rejected_before_any_operation(frame):
    graph = copy.deepcopy(P1)
    graph["nodes"][0]["frame"] = frame
    port = MeasuredPort()
    result = compile_and_run(port, graph)
    assert result.error["code"] == "VALIDATION_FAILED"
    assert port.calls == []
