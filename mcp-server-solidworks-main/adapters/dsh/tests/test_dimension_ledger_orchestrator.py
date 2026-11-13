import json

import pytest


def _graph(ledger):
    return {
        "schema_version": "0.8.0-draft",
        "units": "meters",
        "nodes": [{"id": "base", "type": "box", "width": .02, "depth": .02, "height": .02}],
        "dimension_ledger": ledger,
    }


def _dimension(dim_id, value, check):
    return {"id": dim_id, "label": dim_id, "value": value, "source_kind": "explicit",
            "view": "front", "node_ids": ["base"], "check": check}


def test_ledger_warnings_are_returned_after_commit_without_rollback(cad_env):
    def reads(tool, params):
        if tool != "analyze_model":
            return None
        state = dict(cad_env.backend.state)
        if params["analysis_type"] == "geometry":
            state["features"] = ["bodies=1", "bbox_size=[0.02,0.02,0.02]"]
        elif params["analysis_type"] == "mass_properties":
            state["features"] = ["volume=0.000008"]
        elif params["analysis_type"] == "faces":
            state["features"] = [json.dumps({"face_count": 1, "faces": [
                {"kind": "cylinder", "origin": [0.01, 0.01, 0],
                 "axis": [0, 0, 1], "radius": 0.003},
            ]})]
        return {"status": "COMPLETED", "stateVersion": state["stateVersion"],
                "cadState": state, "error": None}

    cad_env.backend.call_handler = reads
    graph = _graph([
        _dimension("D-width", 30, {"kind": "bbox_extent", "axis": "x"}),
        _dimension("D-bore", 10, {"kind": "cylinder", "axis": [0, 0, 1],
                                  "through": [0.01, 0.01, 0]}),
    ])

    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True,
    )

    assert result["status"] == "completed", result
    assert [warning["code"] for warning in result["warnings"]] == [
        "LEDGER_MISMATCH", "LEDGER_MISMATCH",
    ]
    assert result["warnings"][0]["dimension_id"] == "D-width"
    assert result["warnings"][1]["dimension_id"] == "D-bore"
    calls = [name for name, _ in cad_env.backend.calls]
    assert "commit_undo_scope" in calls
    assert "rollback_undo_scope" not in calls
    assert calls.count("analyze_model") == 3  # geometry, mass properties, one batched faces read

    finished = cad_env.orchestrator.finish_job(cad_env.job["job_id"], cad_env.token)
    assert finished["status"] == "completed", finished
    assert finished["trust_level"] == "built_unverified"
    assert [w["code"] for w in finished["warnings"] if w["code"].startswith("LEDGER_")] == [
        "LEDGER_MISMATCH", "LEDGER_MISMATCH",
    ]


def _plane_distance_reads(cad_env):
    def reads(tool, params):
        if tool != "analyze_model":
            return None
        state = dict(cad_env.backend.state)
        if params["analysis_type"] == "geometry":
            state["features"] = ["bodies=1", "bbox_size=[0.02,0.02,0.02]"]
        elif params["analysis_type"] == "mass_properties":
            state["features"] = ["volume=0.000008"]
        elif params["analysis_type"] == "faces":
            state["features"] = [json.dumps({"faces": [
                {"planar": True, "normal": [1, 0, 0], "point": [0, .01, .01], "area": .0004},
                {"planar": True, "normal": [-1, 0, 0], "point": [.02, .01, .01], "area": .0004},
            ]})]
        elif params["analysis_type"] == "edges":
            edges = []
            for x in (0, .02):
                points = [[x, 0, 0], [x, .02, 0], [x, .02, .02], [x, 0, .02]]
                edges.extend({"start": points[i], "end": points[(i + 1) % 4],
                              "mid": [(points[i][j] + points[(i + 1) % 4][j]) / 2 for j in range(3)]}
                             for i in range(4))
            state["features"] = [json.dumps({"edges": edges})]
        return {"status": "COMPLETED", "stateVersion": state["stateVersion"],
                "cadState": state, "error": None}

    return reads


_THICKNESS = _dimension("D-thickness", 20, {
    "kind": "plane_distance", "normal": [1, 0, 0],
    "point_a": [0, .01, .01], "point_b": [.02, .01, .01],
})


def test_plane_distance_reads_edges_and_commits_as_a_warning_only_check(cad_env):
    cad_env.backend.call_handler = _plane_distance_reads(cad_env)
    graph = _graph([_THICKNESS])

    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True,
    )

    assert result["status"] == "completed", result
    assert "warnings" not in result
    analyses = [params["analysis_type"] for name, params in cad_env.backend.calls
                if name == "analyze_model"]
    assert analyses == ["geometry", "mass_properties", "faces", "edges"]


def test_an_invalid_optional_ledger_warns_and_never_blocks_the_build(cad_env):
    # An extra field rejecting the whole graph would be the expect.features trap again (H1).
    item = _dimension("D-width", 20, {"kind": "bbox_extent", "axis": "x"})
    item["extra"] = "not in v4's seven-field entry"
    graph = _graph([item])

    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True,
    )

    assert result["status"] == "completed", result
    warning = next(w for w in result["warnings"] if w["code"] == "LEDGER_INVALID")
    assert "fields must be exactly" in warning["message"]
    assert not [p for name, p in cad_env.backend.calls
                if name == "analyze_model" and p.get("analysis_type") == "faces"]


def test_faces_readback_from_a_different_state_is_unmeasured(cad_env):
    def reads(tool, params):
        if tool != "analyze_model":
            return None
        state = dict(cad_env.backend.state)
        if params["analysis_type"] == "geometry":
            state["features"] = ["bodies=1", "bbox_size=[0.02,0.02,0.02]"]
        elif params["analysis_type"] == "mass_properties":
            state["features"] = ["volume=0.000008"]
        elif params["analysis_type"] == "faces":
            state["features"] = ['{"faces":[]}']
            return {"status": "COMPLETED", "stateVersion": state["stateVersion"] + 10,
                    "cadState": state, "error": None}
        return {"status": "COMPLETED", "stateVersion": state["stateVersion"],
                "cadState": state, "error": None}

    cad_env.backend.call_handler = reads
    graph = _graph([_dimension("D-bore", 10, {
        "kind": "cylinder", "axis": [0, 0, 1], "through": [0.01, 0.01, 0],
    })])

    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True,
    )

    assert result["status"] == "completed", result
    warning = next(w for w in result["warnings"] if w["code"] == "LEDGER_UNMEASURED")
    assert warning["reason"] == "document_state_changed_during_measurement"


def test_ledger_is_optional_and_adds_no_faces_read(cad_env):
    result = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token,
        json.dumps({"schema_version": "0.8.0-draft", "units": "meters", "nodes": [
            {"id": "n1", "type": "box", "width": .02, "depth": .02, "height": .02},
        ]}),
        fresh_document=True,
    )

    assert result["status"] == "completed", result
    assert "warnings" not in result
    analyses = [params["analysis_type"] for name, params in cad_env.backend.calls
                if name == "analyze_model"]
    assert analyses == ["geometry", "mass_properties"]


def _build_save_finish(cad_env, tmp_path, graph):
    built = cad_env.orchestrator.feature_graph(
        cad_env.job["job_id"], cad_env.token, json.dumps(graph), fresh_document=True,
    )
    assert built["status"] == "completed", built
    saved = cad_env.orchestrator.save_or_export(
        cad_env.job["job_id"], cad_env.token, "save", file_path=str(tmp_path / "out.SLDPRT"),
    )
    assert saved["status"] == "completed", saved
    finished = cad_env.orchestrator.finish_job(cad_env.job["job_id"], cad_env.token)
    assert finished["status"] == "completed", finished
    return finished


def test_a_matching_measured_ledger_finishes_verified(cad_env, tmp_path):
    cad_env.backend.call_handler = _plane_distance_reads(cad_env)

    finished = _build_save_finish(cad_env, tmp_path, _graph([_THICKNESS]))

    assert finished["trust_level"] == "verified"
    assert "verification_gaps" not in finished


def test_a_graph_without_a_ledger_finishes_built_unverified(cad_env, tmp_path):
    # Review 2026-11-13: without a measured dimension only the envelope and volume were compared.
    graph = _graph(None)
    del graph["dimension_ledger"]

    finished = _build_save_finish(cad_env, tmp_path, graph)

    assert finished["trust_level"] == "built_unverified"
    assert finished["verification_gaps"] == ["LEDGER_ABSENT"]
    assert "LEDGER_ABSENT" in finished["trust_note"]


def test_a_volume_mismatch_never_finishes_verified(cad_env, tmp_path):
    # Review 2026-11-12 F-01: PLAN_VOLUME_MISMATCH used to leave the verdict at `verified`.
    cad_env.backend.call_handler = _plane_distance_reads(cad_env)
    graph = _graph([_THICKNESS])
    graph["expect"] = {"bbox_mm": [20, 20, 20], "volume_mm3": 4000}

    finished = _build_save_finish(cad_env, tmp_path, graph)

    assert finished["trust_level"] == "built_unverified"
    assert finished["verification_gaps"] == ["PLAN_VOLUME_MISMATCH"]
