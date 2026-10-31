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
    assert set(region.input_schema["properties"]) == {"image_path", "region", "zoom", "regions"}
    assert region.input_schema["additionalProperties"] is False
    payload = server.orchestrator.inspect_state(scope="reference", reference_topic="feature_graph_part_card")
    assert len(json.dumps(payload, ensure_ascii=False)) < 7000
    assert payload["verification"]["page"]["has_more"] is False
