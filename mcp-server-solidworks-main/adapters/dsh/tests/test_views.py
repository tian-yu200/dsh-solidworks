"""inspect_state(scope="views"): the built part as images, to compare with the drawing (2026-11-09, M5).

A 缺口块 build without its ramp and front step still measured 42x30x30 and passed. Numbers that
agree can hide a missing feature; these images let the planner compare view by view.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image

import orchestrator as orchestrator_module


def _dsh_server():
    """The DSH adapter's server.py, loaded by path: a bare `import server` can resolve to another one."""
    spec = importlib.util.spec_from_file_location("dsh_adapter_server_views",
                                                  Path(__file__).resolve().parents[1] / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(tmp_path, monkeypatch, cad_env, missing=()):
    monkeypatch.setattr(orchestrator_module, "VIEWS_ROOT", tmp_path / "views")

    def handler(tool, params):
        if tool != "render_views":
            return None
        out = []
        for name in params["views"]:
            item = {"view": name, "saved": name not in missing}
            if name in missing:
                item["error"] = "SaveAs returned False, errors=1, warnings=0"
            else:
                path = tmp_path / ("%s.png" % name)
                Image.new("RGB", (2400, 1200), (200, 210, 230)).save(path)
                item["path"] = str(path)
            out.append(item)
        return {"status": "COMPLETED", "stateVersion": 1,
                "cadState": {"features": [json.dumps({"views": out})]}}

    cad_env.backend.call_handler = handler


def test_views_render_into_the_job_folder_and_say_what_each_shows(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env)
    result = cad_env.orchestrator.render_views("job-1", cad_env.token)
    tool, params = [c for c in cad_env.backend.calls if c[0] == "render_views"][-1]
    assert params["views"] == ["front", "top", "left", "isometric"]
    assert params["output_dir"].startswith(str(tmp_path / "views" / "job-1"))
    assert [v["view"] for v in result["views"]] == ["front", "top", "left", "isometric"]
    assert "沿 -Z" in result["views"][0]["shows"] and "第一角" in result["views"][2]["shows"]


def test_the_tool_returns_one_scaled_image_per_saved_view(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env, missing=("top",))
    server = _dsh_server()
    blocks, text = server.inspect_state("job-1", cad_env.token, "views")
    payload = json.loads(text)
    assert len(blocks) == 3
    with Image.open(io.BytesIO(blocks[0].data)) as im:
        assert max(im.size) == server.VIEW_MAX_SIDE
    by_view = {v["view"]: v for v in payload["views"]}
    assert by_view["front"]["image"] == 1 and "path" not in by_view["front"]
    assert "image" not in by_view["top"] and "SaveAs returned False" in by_view["top"]["error"]
    assert "FAIL" in payload["compare"]


def test_views_refuse_unknown_names_and_need_a_job(cad_env):
    with pytest.raises(ValueError):
        cad_env.orchestrator.render_views("job-1", cad_env.token, "front,section")
    with pytest.raises(ValueError):
        cad_env.orchestrator.render_views("", "", "front")


def test_views_need_a_part_or_assembly(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env)
    cad_env.backend.state["documentType"] = "DRAWING"
    with pytest.raises(ValueError):
        cad_env.orchestrator.render_views("job-1", cad_env.token)
    assert not [c for c in cad_env.backend.calls if c[0] == "render_views"]


def _graph():
    return json.dumps({"units": "meters", "expect": {"bbox_mm": [20, 20, 20]},
                       "nodes": [{"id": "n1", "type": "box", "width": .02, "depth": .02, "height": .02}]})


def test_a_completed_build_returns_the_part_views_with_its_result(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env)
    server = _dsh_server()
    result = server.submit_feature_graph("job-1", cad_env.token, _graph(), True)
    assert isinstance(result, tuple), result
    blocks, text = result
    payload = json.loads(text)
    assert payload["status"] == "completed" and len(blocks) == 4
    assert [v["view"] for v in payload["views"]] == ["front", "top", "left", "isometric"]
    assert "FAIL" in payload["views_compare"]


def test_a_render_problem_never_fails_the_build(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env)
    cad_env.backend.call_handler = lambda tool, params: (
        {"status": "FAILED", "error": {"code": "COM_ERROR", "message": "no window"}}
        if tool == "render_views" else None)
    server = _dsh_server()
    payload = json.loads(server.submit_feature_graph("job-1", cad_env.token, _graph(), True))
    assert payload["status"] == "completed" and "RENDER_FAILED" in payload["views_error"]


def test_a_failed_build_renders_nothing(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env)
    cad_env.backend.feature_graph_handler = lambda graph, fresh: {
        "status": "FAILED", "stateVersion": 2, "compiler": {"nodes_completed": 0, "node_log": []},
        "error": {"code": "EXTRUSION_FAILED", "message": "null"}}
    server = _dsh_server()
    payload = json.loads(server.submit_feature_graph("job-1", cad_env.token, _graph(), True))
    assert payload["status"] != "completed"
    assert not [c for c in cad_env.backend.calls if c[0] == "render_views"]


GOOD_REVIEW = json.dumps([
    {"view": "front", "drawing": "20x20 square outline, no holes", "model": "20x20 square outline",
     "differences": "none", "verdict": "PASS"},
    {"view": "top", "drawing": "20x20 square, plain top face", "model": "20x20 square face",
     "differences": "none", "verdict": "PASS"},
    {"view": "left", "drawing": "20x20 square side outline", "model": "20x20 square side",
     "differences": "none", "verdict": "PASS"}])


def _built(tmp_path, monkeypatch, cad_env):
    _render(tmp_path, monkeypatch, cad_env)
    server = _dsh_server()
    server.submit_feature_graph("job-1", cad_env.token, _graph(), True)
    return server


def test_finishing_a_built_part_needs_a_per_view_review(tmp_path, monkeypatch, cad_env):
    # zct01 bc30aa2e (2026-11-09): views were "checked" while a solid block stood for a domed housing.
    server = _built(tmp_path, monkeypatch, cad_env)
    missing = json.loads(server.finish_job("job-1", cad_env.token))
    assert missing["error"]["code"] == "VIEW_REVIEW_REQUIRED", missing
    thin = json.dumps([{"view": v, "drawing": "ok", "model": "ok", "differences": "none", "verdict": "PASS"}
                       for v in ("front", "top", "left")])
    assert json.loads(server.finish_job("job-1", cad_env.token, False, thin))["error"]["code"] == "VIEW_REVIEW_REQUIRED"
    two = json.dumps(json.loads(GOOD_REVIEW)[:2])
    assert json.loads(server.finish_job("job-1", cad_env.token, False, two))["error"]["code"] == "VIEW_REVIEW_REQUIRED"


def test_a_failed_view_refuses_the_finish(tmp_path, monkeypatch, cad_env):
    server = _built(tmp_path, monkeypatch, cad_env)
    review = json.loads(GOOD_REVIEW)
    review[0].update(verdict="FAIL", differences="drawing shows a domed housing, the model is a block")
    payload = json.loads(server.finish_job("job-1", cad_env.token, False, json.dumps(review)))
    assert payload["error"]["code"] == "VIEW_REVIEW_FAILED" and "domed housing" in payload["error"]["message"]


def test_a_complete_review_passes_the_gate_and_is_kept(tmp_path, monkeypatch, cad_env):
    server = _built(tmp_path, monkeypatch, cad_env)
    payload = json.loads(server.finish_job("job-1", cad_env.token, False, GOOD_REVIEW))
    assert (payload.get("error") or {}).get("code") not in ("VIEW_REVIEW_REQUIRED", "VIEW_REVIEW_FAILED"), payload
    assert cad_env.store.load("job-1", cad_env.token)["view_review"][0]["view"] == "front"


def test_a_job_that_built_nothing_needs_no_review(cad_env):
    payload = json.loads(_dsh_server().finish_job("job-1", cad_env.token))
    assert (payload.get("error") or {}).get("code") not in ("VIEW_REVIEW_REQUIRED", "VIEW_REVIEW_FAILED"), payload
