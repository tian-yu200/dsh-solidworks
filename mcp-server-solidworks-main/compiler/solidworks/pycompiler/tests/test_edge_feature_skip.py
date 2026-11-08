"""A fillet/chamfer whose edge cannot be resolved or made is skipped, not fatal (2026-11-09, H12/H6).

Such nodes come last and nothing is built on them, yet one unresolved anchor used to discard the
whole part: 脚踏座 1c5d366c gave up after three, d6bf7e51 lost 13 minutes of features to one null
fillet. The part is now kept with an EDGE_FEATURE_SKIPPED warning, unless a later node names it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pycompiler import compiler as compiler_module                 # noqa: E402
from pycompiler.compiler import compile_and_run                   # noqa: E402
from pycompiler.errors import FeatureError                        # noqa: E402
from pycompiler.execution_port import ExecutionPort               # noqa: E402
from pycompiler.ir_schema import canonical_sketch_frame           # noqa: E402


class Port(ExecutionPort):
    """Completes every op; add_edge_feature fails when told to."""

    def __init__(self, edge_feature_fails=False):
        self._sv = 0
        self.tools = []
        self._edge_fails = edge_feature_fails

    def get_state(self):
        return self._sv

    def execute(self, tool, params, state_version):
        self.tools.append(tool)
        if tool == "add_edge_feature" and self._edge_fails:
            return {"status": "FAILED", "stateVersion": self._sv,
                    "error": {"code": "FEATURE_CREATION_FAILED", "message": "add_edge_feature 'fillet' returned null."}}
        self._sv += 1
        resp = {"status": "COMPLETED", "stateVersion": self._sv, "cadState": {"features": ["Feature%d" % self._sv]}}
        if tool == "create_sketch":
            resp["result_geometry"] = {"frame": canonical_sketch_frame({"datum": "top"})}
        return resp


def _graph(*extra):
    return {"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
        {"id": "plate", "type": "box", "ref": {"datum": "top"}, "width": 0.05, "depth": 0.05, "height": 0.01},
        {"id": "round", "type": "fillet", "radius": 0.002, "edges": [{"near": [0.025, 0.01, 0.0]}]},
        {"id": "s", "type": "sketch", "ref": {"datum": "top"},
         "profile": [{"kind": "circle", "diameter": 0.01}]},
        {"id": "boss", "type": "extrude", "sketch": "s", "operation": "boss", "depth": 0.005},
    ] + list(extra)}


@pytest.fixture
def edges(monkeypatch):
    """Resolve fillet anchors without a live part: raise `code`, or return edge #1."""
    state = {"code": None}

    def resolve(port, sv, anchors, node_id=None):
        if state["code"]:
            raise FeatureError("edge anchor 0 lies on no edge", code=state["code"], node_id=node_id)
        return [1], {}
    monkeypatch.setattr(compiler_module, "resolve_edges_by_anchor", resolve)
    return state


@pytest.mark.parametrize("code", ["REFERENCE_UNRESOLVED", "REFERENCE_AMBIGUOUS"])
def test_an_unresolved_fillet_is_skipped_and_the_rest_is_built(edges, code):
    edges["code"] = code
    port = Port()
    r = compile_and_run(port, _graph())
    assert r.status == "COMPLETED", r.to_dict()
    status = {n["id"]: n["status"] for n in r.node_log}
    assert status == {"plate": "COMPLETED", "round": "SKIPPED", "s": "COMPLETED", "boss": "COMPLETED"}
    warning = next(w for w in r.warnings if w["code"] == "EDGE_FEATURE_SKIPPED")
    assert warning["node_id"] == "round" and warning["error_code"] == code
    assert "fresh_document=false" in warning["message"] and "lies on no edge" in warning["detail"]
    assert "add_edge_feature" not in port.tools


def test_a_null_fillet_is_skipped_too(edges):
    port = Port(edge_feature_fails=True)
    r = compile_and_run(port, _graph())
    assert r.status == "COMPLETED", r.to_dict()
    assert [n["status"] for n in r.node_log if n["id"] == "round"] == ["SKIPPED"]
    assert port.tools.count("extrude_feature") == 2  # the boss after it still ran


def test_a_fillet_a_later_node_names_still_fails(edges):
    edges["code"] = "REFERENCE_UNRESOLVED"
    graph = _graph({"id": "rounds", "type": "linear_pattern", "feature": "round",
                    "direction": "x", "count": 2, "spacing": 0.02})
    r = compile_and_run(Port(), graph)
    assert r.status == "FAILED" and r.error["node_id"] == "round", r.to_dict()


def test_other_failures_stay_fatal(edges):
    class NullBoss(Port):
        def execute(self, tool, params, state_version):
            if tool == "extrude_feature" and self.tools.count("extrude_feature") == 1:
                self.tools.append(tool)
                return {"status": "FAILED", "stateVersion": self._sv,
                        "error": {"code": "EXTRUSION_FAILED", "message": "null"}}
            return Port.execute(self, tool, params, state_version)

    r = compile_and_run(NullBoss(), _graph())
    assert r.status == "FAILED" and r.error["code"] == "EXTRUSION_FAILED", r.to_dict()
