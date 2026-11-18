"""Tests for drawing-vs-modeling diagnostic attribution and Windows OCR PlatformNotSupportedException fallback."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent
if str(ADAPTER) not in sys.path:
    sys.path.insert(0, str(ADAPTER))

import ocr  # noqa: E402
from diagnostic_attribution import (  # noqa: E402
    CATEGORY_EXECUTION_ERROR,
    CATEGORY_MATCHES_PLAN,
    CATEGORY_PARTIAL_TELEMETRY,
    CATEGORY_PLAN_MISMATCH,
    build_diagnostic_attribution,
)


def _load_dsh_server():
    server_path = ADAPTER / "server.py"
    spec = importlib.util.spec_from_file_location("dsh_adapter_server_diag_test", server_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample_drawing(tmp_path):
    path = tmp_path / "drawing.png"
    Image.new("RGB", (600, 400), (255, 255, 255)).save(path)
    return path


# ---------------------------------------------------------------------------
# 1. Windows OCR PlatformNotSupportedException & language-pack fallback tests
# ---------------------------------------------------------------------------


def test_ocr_helper_script_guards_platform_not_supported_and_falls_back_languages():
    assert "catch [System.PlatformNotSupportedException]" in ocr._HELPER
    assert "AvailableRecognizerLanguages" in ocr._HELPER
    assert "TryCreateFromLanguage" in ocr._HELPER


def test_windows_powershell_resolver_prefers_system32_v51_over_pwsh(monkeypatch, tmp_path):
    fake_win = tmp_path / "Windows"
    ps51 = fake_win / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    ps51.parent.mkdir(parents=True)
    ps51.write_bytes(b"fake-ps51")
    monkeypatch.setattr(ocr.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", str(fake_win))
    assert ocr._windows_powershell() == str(ps51)


def test_windows_powershell_resolver_fails_cleanly_on_non_windows(monkeypatch):
    monkeypatch.setattr(ocr.os, "name", "posix")
    with pytest.raises(ocr.OcrUnavailable) as exc_info:
        ocr._windows_powershell()
    assert str(exc_info.value).startswith("OCR_UNAVAILABLE:")
    assert "read_drawing_region" in str(exc_info.value)


def test_read_drawing_text_maps_platform_not_supported_to_ocr_unavailable_not_unexpected_error(
    sample_drawing, monkeypatch
):
    server = _load_dsh_server()

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=2,
            stdout="",
            stderr=(
                "PLATFORM_NOT_SUPPORTED: Operation is not supported on this platform. "
                "(System.PlatformNotSupportedException)"
            ),
        )

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr(ocr, "_windows_powershell", lambda: "powershell.exe")

    raw = server.read_drawing_text(str(sample_drawing))
    assert "\n" not in raw
    payload = json.loads(raw)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "OCR_UNAVAILABLE"
    assert "PlatformNotSupportedException" in payload["error"]["message"]
    assert "read_drawing_region" in payload["error"]["message"]


def test_read_drawing_text_maps_subprocess_oserror_to_ocr_unavailable(sample_drawing, monkeypatch):
    server = _load_dsh_server()

    def broken_run(*_args, **_kwargs):
        raise FileNotFoundError("powershell.exe missing")

    monkeypatch.setattr(ocr.subprocess, "run", broken_run)
    monkeypatch.setattr(ocr, "_windows_powershell", lambda: "powershell.exe")

    payload = json.loads(server.read_drawing_text(str(sample_drawing)))
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "OCR_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 2. Diagnostic Attribution: 'drawing reading bug' vs 'CAD modeling bug'
# ---------------------------------------------------------------------------


def _setup_part_reads(cad_env, bbox_m=(0.05, 0.04, 0.02), volume_m3=0.00004, bodies=1, cyl_radius_m=0.005):
    def reads(tool, params):
        if tool != "analyze_model":
            return None
        state = dict(cad_env.backend.state)
        if params["analysis_type"] == "geometry":
            state["features"] = [
                f"bodies={bodies}",
                f"bbox_size={json.dumps(list(bbox_m))}",
            ]
        elif params["analysis_type"] == "mass_properties":
            state["features"] = [f"volume={volume_m3}"]
        elif params["analysis_type"] == "faces":
            state["features"] = [json.dumps({
                "face_count": 1,
                "faces": [{
                    "kind": "cylinder",
                    "origin": [0.0, 0.01, 0.0],
                    "axis": [0, 1, 0],
                    "radius": cyl_radius_m,
                }],
            })]
        return {
            "status": "COMPLETED",
            "stateVersion": state["stateVersion"],
            "cadState": state,
            "error": None,
        }

    cad_env.backend.call_handler = reads


def test_attribution_identifies_cad_execution_error_and_converts_planned_units_to_mm(cad_env):
    def failing_graph(_graph, _fresh):
        return {
            "status": "FAILED",
            "stateVersion": 3,
            "compiler": {
                "nodes_completed": 1,
                "nodes_total": 2,
                "node_log": [
                    {"id": "base_plate", "type": "box", "status": "COMPLETED"},
                    {"id": "boss_arm", "type": "extrude", "status": "FAILED"},
                ],
            },
            "error": {
                "code": "EXTRUSION_FAILED",
                "message": "Feature creation returned null for feature_type 'boss'.",
                "node_id": "boss_arm",
                "node_type": "extrude",
                "step": "extrude_feature",
            },
        }

    cad_env.backend.feature_graph_handler = failing_graph
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "expect": {"bbox_mm": [50, 40, 35]},
        "nodes": [
            {"id": "base_plate", "type": "box", "width": 0.05, "depth": 0.04, "height": 0.02},
            {"id": "arm_sketch", "type": "sketch", "ref": {"datum": "top", "offset": 0.02},
             "profile": [{"kind": "circle", "diameter": 0.016}]},
            {"id": "boss_arm", "type": "extrude", "sketch": "arm_sketch", "operation": "boss", "depth": 0.015},
        ],
    }
    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True
    )
    assert result["status"] == "failed"
    # Failure top-level envelope keys remain untouched; attribution sits inside verification.
    assert "diagnostic_attribution" not in result
    attr = result["verification"]["diagnostic_attribution"]
    assert attr["category"] == CATEGORY_EXECUTION_ERROR
    assert attr["responsible_stage"] == "cad_compilation_or_execution"
    assert attr["discrepancies"][0]["code"] == "EXTRUSION_FAILED"
    assert attr["discrepancies"][0]["node_id"] == "boss_arm"
    # SI meters were converted into human-readable mm for instant inspection:
    nodes_mm = {n["id"]: n for n in attr["planned_by_model"]["node_dimensions_mm"]}
    assert nodes_mm["base_plate"]["size_mm"] == [50.0, 40.0, 20.0]
    assert nodes_mm["arm_sketch"]["offset_mm"] == 20.0
    assert "Ø16mm" in nodes_mm["arm_sketch"]["profile_mm"]["items"]
    assert nodes_mm["boss_arm"]["depth_mm"] == 15.0


def test_attribution_identifies_plan_vs_cad_mismatch_on_envelope_deviation(cad_env):
    # Approved plan expected 90x80x117 mm, but CAD actually built 60x90x103 mm.
    _setup_part_reads(cad_env, bbox_m=(0.06, 0.09, 0.103), volume_m3=0.0005)
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "expect": {"bbox_mm": [90, 80, 117]},
        "nodes": [
            {"id": "pedestal", "type": "box", "width": 0.06, "depth": 0.09, "height": 0.103},
        ],
    }
    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True
    )
    assert result["status"] == "verification_failed"
    assert result["error"]["code"] == "PLAN_MISMATCH"
    assert result["error"]["attribution_category"] == CATEGORY_PLAN_MISMATCH
    attr = result["verification"]["diagnostic_attribution"]
    assert attr["category"] == CATEGORY_PLAN_MISMATCH
    assert attr["responsible_stage"] == "feature_graph_modeling"
    assert attr["planned_by_model"]["envelope_mm"] == [80.0, 90.0, 117.0]
    assert attr["measured_in_cad"]["envelope_mm"] == [60.0, 90.0, 103.0]
    assert attr["discrepancies"][0]["kind"] == "envelope_mismatch"


def test_attribution_identifies_plan_vs_cad_mismatch_on_ledger_bore_error(cad_env, tmp_path):
    # Envelope matches (50x40x20 mm), but bore is Ø6 mm in CAD instead of planned Ø10 mm!
    _setup_part_reads(cad_env, bbox_m=(0.05, 0.04, 0.02), volume_m3=0.000039, cyl_radius_m=0.003)
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "expect": {"bbox_mm": [50, 40, 20]},
        "nodes": [
            {"id": "plate", "type": "box", "width": 0.05, "depth": 0.04, "height": 0.02},
        ],
        "dimension_ledger": [
            {
                "id": "D_bore",
                "label": "Ø10 center bore",
                "value": 10,
                "source_kind": "explicit",
                "view": "top",
                "node_ids": ["plate"],
                "check": {"kind": "cylinder", "axis": [0, 1, 0], "through": [0, 0.01, 0]},
            }
        ],
    }
    built = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True
    )
    assert built["status"] == "completed"
    attr = built["diagnostic_attribution"]
    assert attr["category"] == CATEGORY_PLAN_MISMATCH
    assert attr["discrepancies"][0]["code"] == "LEDGER_MISMATCH"
    assert attr["discrepancies"][0]["planned_mm"] == 10
    assert attr["discrepancies"][0]["measured_mm"] == 6.0
    assert attr["discrepancies"][0]["delta_mm"] == -4.0

    saved = cad_env.orchestrator.save_or_export(
        cad_env.job["job_id"], cad_env.token, "save", file_path=str(tmp_path / "plate.SLDPRT")
    )
    assert saved["status"] == "completed"
    finished = cad_env.orchestrator.finish_job(cad_env.job["job_id"], cad_env.token)
    assert finished["status"] == "completed"
    assert finished["diagnostic_attribution"]["category"] == CATEGORY_PLAN_MISMATCH


def test_attribution_confirms_matches_model_plan_pointing_to_drawing_intake_if_wrong(cad_env, tmp_path):
    # CAD matches both envelope (50x40x20 mm) and bore Ø10 mm (radius 0.005 m).
    # If the real drawing actually said Ø12 mm and the model submitted Ø10 mm, this tells the
    # engineer immediately that CAD built the model's plan faithfully -> check drawing intake!
    _setup_part_reads(cad_env, bbox_m=(0.05, 0.04, 0.02), volume_m3=0.0000384, cyl_radius_m=0.005)
    graph = {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "expect": {"bbox_mm": [50, 40, 20], "volume_mm3": 38430},
        "nodes": [
            {"id": "plate", "type": "box", "width": 0.05, "depth": 0.04, "height": 0.02},
            {"id": "center_bore", "type": "hole",
             "ref": {"node_face": {"node": "plate", "selector": "top"}, "position": "center"},
             "diameter": 0.01, "depth": "through_all"},
        ],
        "dimension_ledger": [
            {
                "id": "D_bore",
                "label": "Ø10 center bore",
                "value": 10,
                "source_kind": "explicit",
                "view": "top",
                "node_ids": ["center_bore"],
                "check": {"kind": "cylinder", "axis": [0, 1, 0], "through": [0, 0.01, 0]},
            }
        ],
    }
    built = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True
    )
    assert built["status"] == "completed"
    attr = built["diagnostic_attribution"]
    assert attr["category"] == CATEGORY_MATCHES_PLAN
    assert attr["responsible_stage"] == "drawing_intake_if_wrong"
    assert attr["discrepancies"] == []
    assert attr["measured_in_cad"]["ledger_comparisons"][0]["status"] == "matched_within_tolerance"
    # Verify single-line compact JSON stays comfortably below the 8192-char host elision budget.
    compact = json.dumps(built, ensure_ascii=False, separators=(",", ":"))
    assert len(compact) < 6000

    saved = cad_env.orchestrator.save_or_export(
        cad_env.job["job_id"], cad_env.token, "save", file_path=str(tmp_path / "plate_ok.SLDPRT")
    )
    assert saved["status"] == "completed"
    finished = cad_env.orchestrator.finish_job(cad_env.job["job_id"], cad_env.token)
    assert finished["status"] == "completed"
    assert finished["trust_level"] == "verified"
    assert finished["diagnostic_attribution"]["category"] == CATEGORY_MATCHES_PLAN


def test_attribution_partial_telemetry_when_no_expect_or_ledger_present():
    attr = build_diagnostic_attribution(
        graph={"nodes": [{"id": "base", "type": "box", "width": 0.02, "depth": 0.02, "height": 0.01}]},
        verification={"deep_checks": []},
    )
    assert attr["category"] == CATEGORY_PARTIAL_TELEMETRY
