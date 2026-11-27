"""Unit tests for Feature Graph editability: semantic FeatureManager tree naming,
character sanitization, collision handling, non-fatal rename fallback, and dependency mapping.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _COMPILER_ROOT not in sys.path:
    sys.path.insert(0, _COMPILER_ROOT)

from pycompiler.compiler import compile_and_run  # noqa: E402
from pycompiler.editability import (  # noqa: E402
    extract_node_dependencies,
    sanitize_feature_name,
    semantic_base_name,
)
from pycompiler.tests.test_compiler import EXAMPLE, FakePort, _BOX_FACES  # noqa: E402


class RenameTrackingPort(FakePort):
    """Extends FakePort to record and honour `edit_feature(action='rename')` like SolidWorks."""

    def __init__(self, *args, reject_renames_for=(), raise_on_rename=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.renames = []  # [(old_name, new_name)]
        self._reject_for = set(reject_renames_for)
        self._raise_on_rename = raise_on_rename

    def execute(self, tool, params, state_version):
        if tool == "edit_feature" and params.get("action") == "rename":
            self.calls.append((tool, dict(params)))
            if self._raise_on_rename:
                raise RuntimeError("simulated transport hiccup during rename")
            old_name = params["feature_name"]
            new_name = params["new_name"]
            if old_name in self._reject_for or new_name in self._reject_for:
                return {
                    "status": "FAILED",
                    "stateVersion": self._sv,
                    "error": {"code": "FEATURE_RENAME_REJECTED", "message": "rejected"},
                }
            self._sv += 1
            self.renames.append((old_name, new_name))
            return {
                "status": "COMPLETED",
                "stateVersion": self._sv,
                "cadState": {"features": [new_name]},
            }
        if tool == "add_cosmetic_thread":
            self.calls.append((tool, dict(params)))
            self._sv += 1
            tname = "CosmeticThread_%s" % (params.get("size") or "M5x0.8")
            return {
                "status": "COMPLETED",
                "stateVersion": self._sv,
                "cadState": {"features": [tname]},
                "result_geometry": {"cosmetic_thread": tname},
            }
        return super().execute(tool, params, state_version)


def test_default_compile_and_run_emits_zero_extra_rename_calls():
    port = RenameTrackingPort()
    res = compile_and_run(port, EXAMPLE)
    assert res.status == "COMPLETED"
    assert port.renames == []
    assert "feature_map" not in res.to_dict()
    assert res.exec_calls == 6


def test_semantic_feature_names_renames_sketches_planes_axes_and_counterbores():
    edges = json.dumps({
        "edge_count": 1,
        "edges": [{
            "i": 0,
            "closed": True,
            "start": [0.0021, 0.048, 0.0],
            "end": [0.0021, 0.048, 0.0],
            "mid": [-0.0021, 0.048, 0.0],
            "length": 0.013195,
            "kind": "circle",
            "center": [0.0, 0.048, 0.0],
            "normal": [0.0, 1.0, 0.0],
            "radius": 0.0021,
        }],
    })
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "nodes": [
            {"id": "base_plate", "type": "box", "ref": {"datum": "top"},
             "width": 0.06, "depth": 0.06, "height": 0.05},
            {"id": "pedestal_profile", "type": "sketch", "ref": {"datum": "front", "offset": 0.03},
             "profile": [{"kind": "circle", "diameter": 0.02}]},
            {"id": "front_boss", "type": "extrude", "sketch": "pedestal_profile",
             "operation": "boss", "depth": 0.01},
            {"id": "m5_mounting_hole", "type": "hole",
             "ref": {"node_face": {"node": "base_plate", "selector": "top"}, "position": "center"},
             "diameter": 0.0042, "depth": 0.012,
             "counterbore": {"diameter": 0.009, "depth": 0.002},
             "thread": {"designation": "M5", "depth": 0.010}},
            {"id": "bolt_circle", "type": "circular_pattern",
             "feature": "m5_mounting_hole",
             "axis": {"datums": ["front", "right"]},
             "count": 4, "angle_deg": 360},
        ],
    }
    # Ensure pedestal_profile's offset plane doesn't match an existing +Z face at 0.03 so Plane1 is created.
    port = RenameTrackingPort(faces=_BOX_FACES, edges=edges)
    res = compile_and_run(port, graph, semantic_feature_names=True)
    assert res.status == "COMPLETED", res.to_dict()

    fmap = res.to_dict()["feature_map"]
    assert fmap["base_plate"]["tree_features"] == ["base_plate_sketch", "base_plate"]
    assert fmap["pedestal_profile"]["tree_features"] == ["pedestal_profile_plane", "pedestal_profile"]
    assert fmap["front_boss"]["tree_features"] == ["front_boss"]
    assert fmap["front_boss"]["depends_on"] == ["pedestal_profile"]

    # Counterbored threaded hole gets distinct cbore/pilot names while keeping CosmeticThread intact:
    assert fmap["m5_mounting_hole"]["tree_features"] == [
        "m5_mounting_hole_cbore_sketch",
        "m5_mounting_hole_cbore",
        "m5_mounting_hole_sketch",
        "m5_mounting_hole",
        "CosmeticThread_M5x0.8",
    ]
    assert fmap["m5_mounting_hole"]["primary_feature"] == "m5_mounting_hole"
    assert fmap["m5_mounting_hole"]["depends_on"] == ["base_plate"]

    assert fmap["bolt_circle"]["tree_features"] == ["bolt_circle_axis", "bolt_circle"]
    assert fmap["bolt_circle"]["depends_on"] == ["m5_mounting_hole"]


def test_generic_numbered_ids_gain_readable_type_suffixes():
    assert semantic_base_name({"id": "n1", "type": "box"}) == "n1_box"
    assert semantic_base_name({"id": "n2", "type": "hole"}) == "n2_hole"
    assert semantic_base_name({"id": "e1", "type": "extrude", "operation": "cut"}) == "e1_cut_extrude"
    # Already-meaningful IDs stay concise without redundant suffixes:
    assert semantic_base_name({"id": "flange_base", "type": "box"}) == "flange_base"
    assert semantic_base_name({"id": "keyway_slot", "type": "extrude", "operation": "cut"}) == "keyway_slot"


def test_sanitization_strips_solidworks_forbidden_at_sign_and_reserved_names():
    # '@' breaks SolidWorks `D1@Feature@Part.Part` dimension addressing!
    cleaned = sanitize_feature_name("M5@Hole / Top:Face*1?")
    assert "@" not in cleaned and "/" not in cleaned and ":" not in cleaned
    assert cleaned == "M5_Hole_Top_Face_1"

    # Reserved plane names are deduplicated automatically:
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "nodes": [
            {"id": "Top Plane", "type": "box", "ref": {"datum": "top"},
             "width": 0.04, "depth": 0.04, "height": 0.02},
        ],
    }
    port = RenameTrackingPort()
    res = compile_and_run(port, graph, semantic_feature_names=True)
    assert res.status == "COMPLETED"
    # "Top Plane" becomes "Top_Plane" (not colliding with "Top Plane")
    assert res.feature_map["Top Plane"]["primary_feature"] == "Top_Plane"


def test_rename_rejection_or_exception_is_non_fatal_and_preserves_runtime_name():
    port = RenameTrackingPort(reject_renames_for={"base_plate"})
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "nodes": [
            {"id": "base_plate", "type": "box", "ref": {"datum": "top"},
             "width": 0.05, "depth": 0.05, "height": 0.02},
        ],
    }
    res = compile_and_run(port, graph, semantic_feature_names=True)
    assert res.status == "COMPLETED"
    # Sketch succeeded, solid feature rename was rejected -> falls back honestly to runtime name:
    assert res.feature_map["base_plate"]["tree_features"][0] == "base_plate_sketch"
    assert res.feature_map["base_plate"]["primary_feature"].startswith("Ekstrüzyon")

    # Even a transport exception during rename never fails the finished geometry:
    throwing_port = RenameTrackingPort(raise_on_rename=True)
    res2 = compile_and_run(throwing_port, graph, semantic_feature_names=True)
    assert res2.status == "COMPLETED"


def test_extract_node_dependencies_covers_complex_feature_chains():
    nodes = [
        {"id": "s_path", "type": "sketch"},
        {"id": "s_prof", "type": "sketch"},
        {"id": "pipe", "type": "sweep", "path": "s_path"},
        {"id": "hollow", "type": "shell", "body": "pipe"},
        {"id": "mir", "type": "mirror", "features": ["pipe", "hollow"]},
    ]
    deps = extract_node_dependencies(nodes)
    assert deps == {
        "s_path": [],
        "s_prof": [],
        "pipe": ["s_prof", "s_path"],
        "hollow": ["pipe"],
        "mir": ["pipe", "hollow"],
    }


def test_adapter_ir_execution_port_preserves_native_feature_names_by_default(monkeypatch):
    claude_dir = os.path.normpath(os.path.join(_COMPILER_ROOT, "..", "..", "adapters", "claude"))
    if claude_dir not in sys.path:
        sys.path.insert(0, claude_dir)
    import ir_execution_port

    monkeypatch.delenv("DSH_SEMANTIC_FEATURE_NAMES", raising=False)
    captured = {}

    def fake_compile_and_run(port, graph, *, semantic_feature_names=False):
        captured["semantic_feature_names"] = semantic_feature_names
        return "ok"

    monkeypatch.setattr(ir_execution_port, "compile_and_run", fake_compile_and_run)
    assert ir_execution_port.run_feature_graph({"nodes": []}) == "ok"
    assert captured["semantic_feature_names"] is False

    # Explicit opt-in (via kwarg or env var) still works when requested:
    assert ir_execution_port.run_feature_graph({"nodes": []}, semantic_feature_names=True) == "ok"
    assert captured["semantic_feature_names"] is True

    monkeypatch.setenv("DSH_SEMANTIC_FEATURE_NAMES", "1")
    assert ir_execution_port.run_feature_graph({"nodes": []}) == "ok"
    assert captured["semantic_feature_names"] is True
