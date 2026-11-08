"""diagnosis.MaterialLog: what an EXTRUSION_FAILED says about where the profile was (2026-11-08, H11).

The two graphs are the bench failures verbatim (ZCT01 ff77d37a, 阀体 c7c6485a). Both were repaired
by flipping the cut direction, which the execution layer had already tried.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pycompiler.compiler import compile_and_run            # noqa: E402
from pycompiler.execution_port import ExecutionPort        # noqa: E402
from pycompiler.ir_schema import canonical_sketch_frame    # noqa: E402

_DATUM = {"Front Plane": "front", "Top Plane": "top", "Right Plane": "right"}


class FramePort(ExecutionPort):
    """Echoes each sketch's real frame (offset planes included) and fails the Nth extrude."""

    def __init__(self, fail_extrude):
        self._sv = 0
        self._planes = {}
        self._extrudes = 0
        self._fail = fail_extrude

    def get_state(self):
        return self._sv

    def execute(self, tool, params, state_version):
        self._sv += 1
        ok = {"status": "COMPLETED", "stateVersion": self._sv, "cadState": {"features": []}}
        if tool == "add_reference_geometry":
            name = "Plane%d" % (len(self._planes) + 1)
            self._planes[name] = (_DATUM[params["ref_plane_name"]], params["offset"])
            ok["cadState"]["features"] = [name]
        elif tool == "create_sketch":
            datum, offset = self._planes.get(params["plane"]) or (_DATUM[params["plane"]], 0)
            ok["result_geometry"] = {"frame": canonical_sketch_frame({"datum": datum, "offset": offset})}
        elif tool == "extrude_feature":
            self._extrudes += 1
            if self._extrudes == self._fail:
                return {"status": "FAILED", "stateVersion": self._sv,
                        "error": {"code": "EXTRUSION_FAILED", "message": "Feature creation returned null "
                                  "for feature_type '%s'." % params.get("feature_type")}}
            ok["cadState"]["features"] = ["Feature%d" % self._extrudes]
        return ok


def _graph(nodes):
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": nodes}


ZCT01 = _graph([
    {"id": "flange", "type": "box", "ref": {"datum": "top"}, "width": 0.145, "depth": 0.145, "height": 0.023},
    {"id": "housing_profile", "type": "sketch", "ref": {"datum": "front", "offset": 0.023},
     "profile": [{"kind": "circle", "diameter": 0.135}]},
    {"id": "housing", "type": "extrude", "sketch": "housing_profile", "operation": "boss", "depth": 0.142},
    {"id": "main_bore_profile", "type": "sketch", "ref": {"datum": "front", "offset": 0},
     "profile": [{"kind": "circle", "diameter": 0.047}]},
    {"id": "main_bore", "type": "extrude", "sketch": "main_bore_profile", "operation": "cut", "depth": 0.165},
    {"id": "mounting_holes_profile", "type": "sketch", "ref": {"datum": "front", "offset": 0},
     "profile": [{"kind": "circle", "diameter": 0.013, "cx": x, "cy": y}
                 for x, y in ((-0.055, -0.055), (-0.055, 0.055), (0.055, -0.055), (0.055, 0.055))]},
    {"id": "mounting_holes", "type": "extrude", "sketch": "mounting_holes_profile", "operation": "cut", "depth": 0.023},
])

FATI = _graph([
    {"id": "housing_envelope", "type": "box", "ref": {"datum": "top"}, "width": 0.13, "depth": 0.12, "height": 0.08},
    {"id": "axial_bores", "type": "sketch", "ref": {"datum": "right", "offset": 0.065}, "profile": [
        {"kind": "circle", "diameter": 0.06, "cx": 0, "cy": 0.04},
        {"kind": "circle", "diameter": 0.01, "cx": -0.035, "cy": 0.075},
        {"kind": "circle", "diameter": 0.01, "cx": 0.035, "cy": 0.075},
        {"kind": "circle", "diameter": 0.01, "cx": -0.035, "cy": 0.005},
        {"kind": "circle", "diameter": 0.01, "cx": 0.035, "cy": 0.005}]},
    {"id": "axial_through_cuts", "type": "extrude", "sketch": "axial_bores", "operation": "cut",
     "depth": 0.13, "end": "through_all"},
])


def test_holes_on_a_plane_the_flange_is_not_on_are_called_a_placement_error():
    r = compile_and_run(FramePort(fail_extrude=4), ZCT01)
    assert r.status == "FAILED" and r.error["node_id"] == "mounting_holes", r.to_dict()
    msg = r.error["message"]
    assert "already tried BOTH directions" in msg, msg
    assert "None of the 4 profile region(s) reaches material in either direction" in msg, msg
    assert "'flange' X -72.5..72.5 Y 0..23 Z -72.5..72.5" in msg, msg   # the plate lies flat
    assert "placement error" in msg, msg


def test_circles_that_only_touch_the_box_faces_are_named():
    r = compile_and_run(FramePort(fail_extrude=2), FATI)
    assert r.status == "FAILED" and r.error["node_id"] == "axial_through_cuts", r.to_dict()
    msg = r.error["message"]
    assert "already tried BOTH directions" in msg, msg
    assert "placement error" not in msg, msg
    for entity, y in ((1, "80"), (2, "80"), (3, "0"), (4, "0")):
        assert "profile entity %d (circle) touches the face of 'housing_envelope' at Y = %s mm" % (entity, y) in msg, msg
    assert "profile entity 0 (circle) touches" not in msg, msg   # the bore sits well inside


def test_a_boss_failure_reports_where_the_profile_sweeps():
    graph = _graph([
        {"id": "plate", "type": "box", "ref": {"datum": "top"}, "width": 0.04, "depth": 0.04, "height": 0.01},
        {"id": "s", "type": "sketch", "ref": {"datum": "front"},
         "profile": [{"kind": "rectangle", "width": 0.02, "height": 0.02, "cx": 0, "cy": 0.02}]},
        {"id": "rib", "type": "extrude", "sketch": "s", "operation": "boss", "depth": 0.005}])
    r = compile_and_run(FramePort(fail_extrude=2), graph)
    msg = r.error["message"]
    assert "profile entity 0 (rectangle) X -10..10 Y 10..30 Z 0..5" in msg, msg
    assert "BOTH directions" not in msg, msg


def test_an_unmodelled_material_node_weakens_the_conclusion():
    nodes = [dict(n) for n in ZCT01["nodes"]]
    nodes.insert(3, {"id": "boss_pattern", "type": "linear_pattern", "feature": "housing",
                     "direction": {"datum": "right"}, "count": 2, "spacing": 0.05})
    from pycompiler import diagnosis
    log = diagnosis.MaterialLog(nodes)
    log.completed("create_pattern", {}, {}, nodes[3])
    assert log.unmodelled == ["'boss_pattern' (linear_pattern)"]
    cut_pattern = {"id": "hole_pattern", "type": "linear_pattern", "feature": "main_bore"}
    log.completed("create_pattern", {}, {}, cut_pattern)
    assert log.unmodelled == ["'boss_pattern' (linear_pattern)"]   # a pattern of cuts adds nothing


def test_the_record_adds_no_execution_calls(monkeypatch):
    from pycompiler import compiler, diagnosis

    class Counting(FramePort):
        def __init__(self):
            FramePort.__init__(self, fail_extrude=0)
            self.tools = []

        def execute(self, tool, params, state_version):
            self.tools.append(tool)
            return FramePort.execute(self, tool, params, state_version)

    with_log = Counting()
    assert compile_and_run(with_log, FATI).status == "COMPLETED"

    class Silent(diagnosis.MaterialLog):
        def sending(self, *a):
            pass

        def completed(self, *a):
            pass

    monkeypatch.setattr(compiler.diagnosis, "MaterialLog", Silent)
    without = Counting()
    assert compile_and_run(without, FATI).status == "COMPLETED"
    assert with_log.tools == without.tools
