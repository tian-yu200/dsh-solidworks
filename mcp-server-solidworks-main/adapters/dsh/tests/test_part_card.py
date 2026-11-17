"""The entire default card survives host output limits and its example compiles offline."""
import importlib.util
import json
import re
from pathlib import Path
import references


def test_card_is_one_complete_host_visible_page():
    data = references.page_reference("feature_graph_part_card")
    assert not data["page"]["has_more"]
    assert not data["page"]["line_truncated"]
    assert len(json.dumps(data, ensure_ascii=False)) < 7000
    assert len(data["content"].encode("utf-8")) < 6000
    for word in ["box", "sketch", "extrude", "cut", "hole", "fillet", "ref", "meters", "radians"]:
        assert word in data["content"]


def test_complete_example_uses_real_compiler_with_fake_execution():
    import sys
    root = Path(__file__).resolve().parents[3]
    compiler_root = root / "compiler/solidworks"
    sys.path.insert(0, str(compiler_root))
    fixture_path = compiler_root / "pycompiler/tests/test_compiler.py"
    spec = importlib.util.spec_from_file_location("part_card_compiler_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    graph = json.loads(re.search(r"```json\n(.*?)\n```", references.read_reference("feature_graph_part_card"), re.S).group(1))
    edges = json.dumps({"edge_count": 1, "edges": [{"i": 0, "start": [0.025, 0.05, -0.025], "end": [0.025, 0.05, 0.025], "mid": [0.025, 0.05, 0]}]})
    port = fixture.FakePort(edges=edges)
    result = fixture.compile_and_run(port, graph)
    assert result.status == "COMPLETED", result.to_dict()
    tools = [name for name, args in port.calls]
    assert tools.count("extrude_feature") >= 3
    assert next(row for row in result.node_log if row["id"] == "pocket_cut")["status"] == "COMPLETED"
    assert "add_edge_feature" in tools
    assert len(result.node_log) == len(graph["nodes"])


def test_registered_tool_schema_and_complete_card_envelope():
    import asyncio
    path = Path(__file__).resolve().parents[1] / "server.py"
    spec = importlib.util.spec_from_file_location("part_card_registered_server", path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 12
    region = next(tool for tool in tools if tool.name == "read_drawing_region")
    assert set(region.input_schema["properties"]) == {"image_path", "region", "zoom", "regions", "page"}
    assert region.input_schema["additionalProperties"] is False
    payload = server.orchestrator.inspect_state(scope="reference", reference_topic="feature_graph_part_card")
    assert len(json.dumps(payload, ensure_ascii=False)) < 7000
    assert payload["verification"]["page"]["has_more"] is False


def test_dimension_ledger_contract_is_explicit_in_part_card_and_quickref():
    import dimension_ledger

    card = references.read_reference("feature_graph_part_card")
    quickref = references.read_reference("feature_graph_quickref")
    for doc_name, text in (("part_card", card), ("quickref", quickref)):
        assert "dimension_ledger" in text, doc_name
        for field in dimension_ledger.DIMENSION_FIELDS:
            assert field in text, (doc_name, field)
        for src in dimension_ledger.SOURCES:
            assert src in text, (doc_name, src)
        for check_kind, keys in dimension_ledger.CHECK_FIELDS.items():
            assert check_kind in text, (doc_name, check_kind)
            for key in keys:
                assert key in text, (doc_name, check_kind, key)

    sample_graph = {
        "nodes": [{"id": "base_extrude", "type": "box"}],
        "dimension_ledger": [
            {
                "id": "d_len", "label": "80", "value": 80,
                "source_kind": "explicit", "view": "front",
                "node_ids": ["base_extrude"],
                "check": {"kind": "bbox_extent", "axis": "x", "tolerance_mm": 0.5},
            },
            {
                "id": "d_hole", "label": "Ø12", "value": 12,
                "source_kind": "explicit", "view": "top",
                "node_ids": ["base_extrude"],
                "check": {"kind": "cylinder", "axis": [0, 1, 0], "through": [0, 0, 0]},
            },
            {
                "id": "d_step", "label": "15", "value": 15,
                "source_kind": "derived", "view": "left",
                "node_ids": ["base_extrude"],
                "check": {
                    "kind": "plane_distance",
                    "normal": [0, 1, 0],
                    "point_a": [0, 0, 0],
                    "point_b": [0, 0.015, 0],
                },
            },
        ],
    }
    assert dimension_ledger.validate_graph_ledger(sample_graph) == []


def test_add_reference_geometry_exits_active_sketch_before_plane_or_axis():
    root = Path(__file__).resolve().parents[3]
    cs_path = root / "execution/solidworks/SolidworksExecution/Services/SolidWorksService.Features.cs"
    source = cs_path.read_text(encoding="utf-8")
    start = source.index("public ExecutionResponse AddReferenceGeometry(")
    end = source.index("featureMgr.InsertRefPlane(", start)
    window = source[start:end]
    assert "modelDoc.SketchManager.ActiveSketch != null" in window
    assert "modelDoc.SketchManager.InsertSketch(true)" in window

