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
