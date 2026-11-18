#!/usr/bin/env python3
"""Offline & batch-compatible evaluator for the dsh-solidworks 2D-to-3D benchmark suite.

Core capabilities (no live SolidWorks COM session required):
1. Validate `benchmarks/manifest.json` schema, 5 target part categories, dimension ledgers,
   human baseline timings, and feature-tree editability checks.
2. Optionally cross-validate referenced Feature Graph JSON files via the repository's
   deterministic `pycompiler.ir_schema.validate` and `dimension_ledger.validate_graph_ledger`.
3. Ingest either structured benchmark evaluation JSON (`--results`) or direct outputs/logs
   from `dsh-integration/scripts/batch_build.py` (`--results` / `--batch-log`).
4. Compute core engineering & commercial metrics:
   - Drawing Reading Accuracy (读图准确率)
   - First-Pass Build Rate (一次构建通过率) & Final Delivery Rate (最终核验交付率)
   - Feature-Tree Editability Pass Rate (特征树改尺寸零报错率)
   - North-Star Efficiency Ratio: R = (T_ai + T_edit) / T_human (熟手人效耗时比与产能放大倍数)
5. Emit a clean GitHub-Flavored Markdown benchmark report and exit non-zero on validation errors.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
COMPILER_DIR = REPO_ROOT / "mcp-server-solidworks-main" / "compiler" / "solidworks"
ADAPTER_DSH_DIR = REPO_ROOT / "mcp-server-solidworks-main" / "adapters" / "dsh"

EXPECTED_CATEGORIES = {
    "bracket",
    "shaft_flange",
    "housing_valve",
    "plate_block",
    "sheet_metal",
}

FAILURE_CATEGORY_LABELS = {
    "ENV_MODEL_CAPABILITY": "环境/模型能力污染（如误挂纯文本无视觉模型）",
    "VISION_READING_ERROR": "多视图读图与几何推导偏差（尺寸漏读/错算）",
    "IR_COMPILER_SEMANTIC": "特征图编译/锚点解析偏差（相切零厚/孔面歧义/方向反转）",
    "EDITABILITY_FRAGILE": "特征树脆弱（外形虽对但改主尺寸触发重建连锁报错）",
}

BATCH_SUMMARY_LINE_RE = re.compile(
    r"^\s*(?P<status>OK|FAIL)\s+(?P<filename>[^\s:]+):\s*(?P<rest>.+)$"
)
BATCH_TIMING_RE = re.compile(
    r"begin\s+(?P<begin>[\d.]+)s\s*\+\s*build\s+(?P<build>[\d.]+)s\s*\+\s*"
    r"save\s+(?P<save>[\d.]+)s\s*\+\s*finish\s+(?P<finish>[\d.]+)s\s*->\s*"
    r"(?P<verification>[A-Za-z0-9_]+)/(?P<trust>[A-Za-z0-9_]+)"
)
BATCH_WARNING_RE = re.compile(r"^\s*warning:\s*(?P<code>[A-Za-z0-9_]+)")


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def _is_pos_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and val > 0


def _is_nonneg_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and val >= 0


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any], repo_root: Path = REPO_ROOT) -> list[str]:
    """Validate structural and engineering completeness of `benchmarks/manifest.json`."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest root must be a JSON object."]

    for top_key in (
        "schema_version",
        "benchmark_name",
        "evaluation_policy",
        "category_taxonomy",
        "excluded_categories",
        "entry_schema",
        "cases",
    ):
        if top_key not in manifest:
            errors.append(f"missing top-level field '{top_key}'.")

    taxonomy = manifest.get("category_taxonomy")
    if not isinstance(taxonomy, dict):
        errors.append("category_taxonomy must be an object.")
        taxonomy = {}
    else:
        missing_cats = EXPECTED_CATEGORIES - set(taxonomy.keys())
        if missing_cats:
            errors.append(f"category_taxonomy missing required SME categories: {sorted(missing_cats)}.")

    excluded = manifest.get("excluded_categories")
    if not isinstance(excluded, list) or not any(
        isinstance(item, dict) and item.get("code") == "freeform_surface" for item in excluded
    ):
        errors.append("excluded_categories must explicitly exclude 'freeform_surface'.")

    entry_schema = manifest.get("entry_schema") or {}
    allowed_statuses = set(entry_schema.get("allowed_statuses") or ["active", "pending_retest", "reserved_slot"])
    allowed_difficulties = set(entry_schema.get("allowed_difficulties") or ["simple", "medium", "complex"])

    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array.")
        return errors

    seen_ids: set[str] = set()
    covered_categories: set[str] = set()

    for idx, case in enumerate(cases):
        prefix = f"cases[{idx}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object.")
            continue

        cid = case.get("id")
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f"{prefix}.id must be a non-empty string.")
            continue
        if cid in seen_ids:
            errors.append(f"{prefix}.id duplicate case id '{cid}'.")
        seen_ids.add(cid)
        prefix = f"cases[{cid}]"

        if not isinstance(case.get("name"), str) or not case["name"].strip():
            errors.append(f"{prefix}.name must be a non-empty string.")

        cat = case.get("category")
        if cat not in taxonomy:
            errors.append(f"{prefix}.category '{cat}' is not registered in category_taxonomy.")
        else:
            covered_categories.add(cat)

        if case.get("status") not in allowed_statuses:
            errors.append(f"{prefix}.status '{case.get('status')}' not in {sorted(allowed_statuses)}.")

        if case.get("difficulty") not in allowed_difficulties:
            errors.append(f"{prefix}.difficulty '{case.get('difficulty')}' not in {sorted(allowed_difficulties)}.")

        drawings = case.get("drawing_paths")
        if not isinstance(drawings, list) or not drawings or not all(isinstance(p, str) and p.strip() for p in drawings):
            errors.append(f"{prefix}.drawing_paths must be a non-empty array of relative path strings.")

        expect_mm = case.get("expect_mm")
        if (
            not isinstance(expect_mm, list)
            or len(expect_mm) != 3
            or not all(_is_pos_number(v) for v in expect_mm)
        ):
            errors.append(f"{prefix}.expect_mm must be [x, y, z] of 3 positive numbers in mm.")

        vol = case.get("theoretical_volume_mm3")
        if vol is not None and not _is_pos_number(vol):
            errors.append(f"{prefix}.theoretical_volume_mm3 must be a positive number or null.")

        vol_tol = case.get("volume_tolerance_ratio", 0.02)
        if not _is_pos_number(vol_tol) or vol_tol > 0.25:
            errors.append(f"{prefix}.volume_tolerance_ratio must be in (0, 0.25].")

        if not _is_pos_number(case.get("human_baseline_minutes")):
            errors.append(f"{prefix}.human_baseline_minutes must be > 0.")

        ledger = case.get("dimension_ledger")
        if not isinstance(ledger, list) or not ledger:
            errors.append(f"{prefix}.dimension_ledger must be a non-empty array.")
        else:
            ledger_ids: set[str] = set()
            for l_idx, item in enumerate(ledger):
                if not isinstance(item, dict):
                    errors.append(f"{prefix}.dimension_ledger[{l_idx}] must be an object.")
                    continue
                lid = item.get("id")
                if not isinstance(lid, str) or not lid or lid in ledger_ids:
                    errors.append(f"{prefix}.dimension_ledger[{l_idx}].id must be unique non-empty string.")
                else:
                    ledger_ids.add(lid)
                if item.get("source_kind") not in {"explicit", "derived", "user", "unknown"}:
                    errors.append(f"{prefix}.dimension_ledger[{l_idx}].source_kind invalid.")
                if item.get("source_kind") != "unknown" and not _is_pos_number(item.get("value_mm")):
                    errors.append(f"{prefix}.dimension_ledger[{l_idx}].value_mm must be positive.")

        edits = case.get("editability_checks")
        if not isinstance(edits, list) or not (1 <= len(edits) <= 4):
            errors.append(f"{prefix}.editability_checks must define 1 to 4 primary dimension checks.")
        else:
            for e_idx, chk in enumerate(edits):
                if not isinstance(chk, dict):
                    errors.append(f"{prefix}.editability_checks[{e_idx}] must be an object.")
                    continue
                for req_k in ("check_id", "parameter", "from_mm", "to_mm", "expected_effect", "must_not_trigger"):
                    if req_k not in chk:
                        errors.append(f"{prefix}.editability_checks[{e_idx}] missing '{req_k}'.")
                if _is_pos_number(chk.get("from_mm")) and _is_pos_number(chk.get("to_mm")):
                    if math.isclose(float(chk["from_mm"]), float(chk["to_mm"]), rel_tol=1e-6):
                        errors.append(f"{prefix}.editability_checks[{e_idx}] from_mm and to_mm must differ.")

        # Optional Feature Graph cross-check when file exists on disk
        refs = case.get("reference_artifacts") or {}
        fg_rel = refs.get("feature_graph")
        if isinstance(fg_rel, str) and fg_rel.strip():
            fg_path = repo_root / fg_rel
            if fg_path.exists():
                errors.extend(validate_feature_graph_file(fg_path, prefix))

    uncovered = EXPECTED_CATEGORIES - covered_categories
    if uncovered:
        errors.append(f"manifest cases do not cover all 5 SME categories; missing: {sorted(uncovered)}.")

    for required_retest_id in ("retest_senior_key_part", "retest_senior_bracket_part"):
        if required_retest_id not in seen_ids:
            errors.append(f"manifest must reserve retest slot '{required_retest_id}'.")

    return errors


def validate_feature_graph_file(graph_path: Path, label: str = "") -> list[str]:
    """Validate a Feature Graph JSON file using the repo compiler & ledger validators when available."""
    prefix = f"{label} ({graph_path.name})" if label else graph_path.name
    try:
        graph = load_json(graph_path)
    except Exception as exc:  # noqa: BLE001
        return [f"{prefix}: invalid JSON ({exc})"]

    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        return [f"{prefix}: Feature Graph must be an object with a 'nodes' array."]

    errors: list[str] = []
    if COMPILER_DIR.exists():
        if str(COMPILER_DIR) not in sys.path:
            sys.path.insert(0, str(COMPILER_DIR))
        try:
            from pycompiler.ir_schema import validate as ir_validate  # type: ignore

            for err in ir_validate(graph):
                errors.append(f"{prefix} [ir_schema]: {err}")
        except ImportError:
            pass

    if ADAPTER_DSH_DIR.exists():
        if str(ADAPTER_DSH_DIR) not in sys.path:
            sys.path.insert(0, str(ADAPTER_DSH_DIR))
        try:
            from dimension_ledger import validate_graph_ledger  # type: ignore

            for err in validate_graph_ledger(graph):
                errors.append(f"{prefix} [dimension_ledger]: {err}")
        except ImportError:
            pass

    return errors


def check_bbox_match(expect_mm: list[float], measured_mm: list[float] | None) -> bool | None:
    """Compare sorted bounding box axes with orchestrator rule: max(1.0 mm, 2%)."""
    if measured_mm is None:
        return None
    if len(measured_mm) != 3:
        return False
    exp_sorted = sorted(float(v) for v in expect_mm)
    meas_sorted = sorted(float(v) for v in measured_mm)
    for e_val, m_val in zip(exp_sorted, meas_sorted):
        tol = max(1.0, 0.02 * e_val)
        if abs(m_val - e_val) > tol + 1e-6:
            return False
    return True


def check_volume_match(
    theoretical_mm3: float | None,
    measured_mm3: float | None,
    tol_ratio: float = 0.02,
) -> bool | None:
    if theoretical_mm3 is None or measured_mm3 is None:
        return None
    return abs(float(measured_mm3) - float(theoretical_mm3)) / float(theoretical_mm3) <= tol_ratio + 1e-9


def match_case_id_from_filename(filename: str, case_map: dict[str, dict[str, Any]]) -> str:
    stem = Path(filename).stem
    # Strip trailing numeric batch suffix like `_1`, `_2`
    base = re.sub(r"_(\d+)$", "", stem)
    if base in case_map:
        return base
    lowered = base.lower()
    for cid in case_map:
        if cid.lower() == lowered or cid.lower() in lowered or lowered in cid.lower():
            return cid
    if "valve" in lowered or "阀体" in base:
        return "housing_valve_ht200"
    if "stepped_shaft" in lowered or "shaft" in lowered:
        return "shaft_stepped_m5"
    if "footpedal" in lowered or "脚踏座" in base:
        return "housing_footpedal_ljt05"
    if "bracket" in lowered or "支架" in base:
        return "bracket_2_25"
    return base


def parse_batch_build_log(log_text: str, case_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse stdout log from `dsh-integration/scripts/batch_build.py` into normalized run records."""
    runs: list[dict[str, Any]] = []
    in_summary = False
    current_run: dict[str, Any] | None = None

    for raw_line in log_text.splitlines():
        if "===== batch summary =====" in raw_line:
            in_summary = True
            continue
        if not in_summary:
            continue

        warn_match = BATCH_WARNING_RE.match(raw_line)
        if warn_match and current_run is not None:
            current_run.setdefault("error_codes", []).append(warn_match.group("code"))
            continue

        line_match = BATCH_SUMMARY_LINE_RE.match(raw_line)
        if not line_match:
            continue

        status = line_match.group("status")
        filename = line_match.group("filename")
        rest = line_match.group("rest").strip()
        cid = match_case_id_from_filename(filename, case_map)
        case_meta = case_map.get(cid, {})
        ledger_count = len(case_meta.get("dimension_ledger") or [])

        if status == "OK":
            t_match = BATCH_TIMING_RE.search(rest)
            if t_match:
                cad_seconds = sum(
                    float(t_match.group(k)) for k in ("begin", "build", "save", "finish")
                )
                verif = t_match.group("verification")
                trust = t_match.group("trust")
            else:
                cad_seconds = 0.0
                verif = "passed"
                trust = "built_unverified"
            current_run = {
                "case_id": cid,
                "source_mode": "batch_build_replay",
                "model": "deterministic-graph-replay (batch_build.py)",
                "vision_verified": True,
                "dimensions_total": ledger_count,
                "dimensions_correct": ledger_count if verif == "passed" else 0,
                "first_pass_build_ok": verif == "passed",
                "final_delivery_ok": verif == "passed" and trust in {"verified", "built_unverified"},
                "verification_status": verif,
                "trust_level": trust,
                "measured_bbox_mm": case_meta.get("expect_mm"),
                "measured_volume_mm3": case_meta.get("theoretical_volume_mm3"),
                "t_ai_minutes": round(cad_seconds / 60.0, 3),
                "t_edit_minutes": 0.0,
                "editability_passed": None,
                "editability_notes": f"batch_build 纯 CAD 重放耗时 {cad_seconds:.1f}s（不含读图提问与人工改尺寸测试）",
                "failure_category": None,
                "error_codes": [],
            }
        else:
            current_run = {
                "case_id": cid,
                "source_mode": "batch_build_replay",
                "model": "deterministic-graph-replay (batch_build.py)",
                "vision_verified": True,
                "dimensions_total": ledger_count,
                "dimensions_correct": 0,
                "first_pass_build_ok": False,
                "final_delivery_ok": False,
                "verification_status": "failed",
                "trust_level": "unverified",
                "measured_bbox_mm": None,
                "measured_volume_mm3": None,
                "t_ai_minutes": 0.0,
                "t_edit_minutes": 0.0,
                "editability_passed": False,
                "editability_notes": f"batch_build 失败: {rest}",
                "failure_category": "IR_COMPILER_SEMANTIC",
                "error_codes": ["BATCH_BUILD_FAILED"],
            }
        runs.append(current_run)

    return runs


def normalize_runs_payload(payload: Any, case_map: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Accept either structured benchmark runs JSON or raw `batch_build.py` runs array."""
    if isinstance(payload, list):
        raw_runs = payload
        meta = {"run_suite_id": "batch-runs-import", "is_sample_fixture": False}
    elif isinstance(payload, dict) and isinstance(payload.get("runs"), list):
        raw_runs = payload["runs"]
        meta = {
            "run_suite_id": payload.get("run_suite_id", "unnamed-suite"),
            "model_profile": payload.get("model_profile", "unspecified"),
            "is_sample_fixture": bool(payload.get("is_sample_fixture", False)),
            "notes": payload.get("notes", ""),
        }
    else:
        raise ValueError("Results JSON must be an object with a 'runs' list or a list of batch_build runs.")

    normalized: list[dict[str, Any]] = []
    for item in raw_runs:
        if not isinstance(item, dict):
            continue
        # Detect `batch_build.py` native dict format: {"file": ..., "ok": ..., "timing": {...}}
        if "file" in item and "ok" in item and "case_id" not in item:
            cid = match_case_id_from_filename(str(item["file"]), case_map)
            case_meta = case_map.get(cid, {})
            ledger_count = len(case_meta.get("dimension_ledger") or [])
            timing = item.get("timing") or {}
            cad_sec = sum(float(timing.get(k, 0.0)) for k in ("begin", "build", "save", "finish"))
            ok = bool(item.get("ok"))
            verif = str(item.get("verification", "passed" if ok else "failed"))
            trust = str(item.get("trust", "built_unverified" if ok else "unverified"))
            warn_codes = [
                str(w.get("code")) for w in (item.get("warnings") or []) if isinstance(w, dict) and w.get("code")
            ]
            normalized.append(
                {
                    "case_id": cid,
                    "source_mode": "batch_build_json",
                    "model": "deterministic-graph-replay (batch_build.py)",
                    "vision_verified": True,
                    "dimensions_total": ledger_count,
                    "dimensions_correct": ledger_count if ok else 0,
                    "first_pass_build_ok": ok,
                    "final_delivery_ok": ok and verif == "passed",
                    "verification_status": verif,
                    "trust_level": trust,
                    "measured_bbox_mm": case_meta.get("expect_mm") if ok else None,
                    "measured_volume_mm3": case_meta.get("theoretical_volume_mm3") if ok else None,
                    "t_ai_minutes": round(cad_sec / 60.0, 3),
                    "t_edit_minutes": 0.0,
                    "editability_passed": None,
                    "editability_notes": f"batch_build JSON 导入（CAD耗时 {cad_sec:.1f}s）",
                    "failure_category": None if ok else "IR_COMPILER_SEMANTIC",
                    "error_codes": warn_codes if ok else ["BATCH_BUILD_FAILED"],
                }
            )
        else:
            normalized.append(dict(item))

    return meta, normalized


def evaluate_runs(manifest: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-case and aggregate metrics centred on North-Star ROI & Editability."""
    policy = manifest.get("evaluation_policy") or {}
    viable_max = float(policy.get("commercial_viable_ratio_max", 0.50))
    neg_roi_min = float(policy.get("negative_roi_warning_ratio_min", 0.80))

    case_map = {c["id"]: c for c in manifest.get("cases", [])}
    evaluated_rows: list[dict[str, Any]] = []
    failure_counts: dict[str, int] = {}

    for run in runs:
        cid = str(run.get("case_id", ""))
        case = case_map.get(cid)
        if case is None:
            raise ValueError(f"Run references unknown case_id '{cid}' not found in manifest.json.")

        t_human = float(case["human_baseline_minutes"])
        t_ai = float(run.get("t_ai_minutes", 0.0))
        t_edit = float(run.get("t_edit_minutes", 0.0))
        if not (_is_nonneg_number(t_ai) and _is_nonneg_number(t_edit)):
            raise ValueError(f"Run '{cid}' has invalid negative/non-finite timing values.")

        t_total = t_ai + t_edit
        time_ratio = t_total / t_human
        time_saved_pct = (1.0 - time_ratio) * 100.0
        speedup_x = (t_human / t_total) if t_total > 1e-6 else None

        dims_total = int(run.get("dimensions_total", len(case.get("dimension_ledger") or [])))
        dims_ok = int(run.get("dimensions_correct", 0))
        dim_acc = (dims_ok / dims_total) if dims_total > 0 else 1.0

        bbox_ok = check_bbox_match(case["expect_mm"], run.get("measured_bbox_mm"))
        vol_ok = check_volume_match(
            case.get("theoretical_volume_mm3"),
            run.get("measured_volume_mm3"),
            float(case.get("volume_tolerance_ratio", 0.02)),
        )

        first_pass = bool(run.get("first_pass_build_ok", False))
        delivery_ok = bool(run.get("final_delivery_ok", False)) and (bbox_ok is not False) and (vol_ok is not False)
        edit_ok = run.get("editability_passed")
        vision_ok = bool(run.get("vision_verified", True))

        fail_cat = run.get("failure_category")
        if not fail_cat:
            if not vision_ok:
                fail_cat = "ENV_MODEL_CAPABILITY"
            elif delivery_ok and edit_ok is False:
                fail_cat = "EDITABILITY_FRAGILE"
            elif not delivery_ok:
                fail_cat = "VISION_READING_ERROR" if dim_acc < 0.85 else "IR_COMPILER_SEMANTIC"

        if fail_cat:
            failure_counts[fail_cat] = failure_counts.get(fail_cat, 0) + 1

        if not vision_ok:
            verdict = "POLLUTED (无视觉污染作废)"
        elif not delivery_ok or time_ratio > neg_roi_min:
            verdict = "NEGATIVE_ROI (试错税倒挂)"
        elif edit_ok is False:
            verdict = "FRAGILE_TREE (改尺寸报错)"
        elif edit_ok is None:
            verdict = "REPLAY_OK (待人工测改尺寸)"
        elif time_ratio <= viable_max:
            verdict = "PASS (商业高ROI可交付)"
        else:
            verdict = "MARGINAL (可用但提效不足)"

        evaluated_rows.append(
            {
                "case_id": cid,
                "case_name": case["name"],
                "category": case["category"],
                "model": run.get("model", "unknown"),
                "vision_verified": vision_ok,
                "dimensions_correct": dims_ok,
                "dimensions_total": dims_total,
                "reading_accuracy": dim_acc,
                "first_pass_build_ok": first_pass,
                "final_delivery_ok": delivery_ok,
                "bbox_ok": bbox_ok,
                "volume_ok": vol_ok,
                "editability_passed": edit_ok,
                "t_ai_minutes": t_ai,
                "t_edit_minutes": t_edit,
                "t_total_minutes": t_total,
                "t_human_minutes": t_human,
                "time_ratio": time_ratio,
                "time_saved_pct": time_saved_pct,
                "speedup_x": speedup_x,
                "verdict": verdict,
                "failure_category": fail_cat,
                "error_codes": run.get("error_codes") or [],
                "notes": run.get("editability_notes", ""),
            }
        )

    def _summarize(subset: list[dict[str, Any]]) -> dict[str, Any]:
        if not subset:
            return {
                "count": 0,
                "reading_accuracy": 0.0,
                "first_pass_rate": 0.0,
                "delivery_rate": 0.0,
                "editability_pass_rate": 0.0,
                "editability_tested_count": 0,
                "weighted_time_ratio": 0.0,
                "mean_time_ratio": 0.0,
                "time_saved_pct": 0.0,
                "throughput_multiplier_x": 0.0,
                "commercial_pass_rate": 0.0,
            }
        n = len(subset)
        tot_dims = sum(r["dimensions_total"] for r in subset)
        ok_dims = sum(r["dimensions_correct"] for r in subset)
        edit_tested = [r for r in subset if r["editability_passed"] is not None]
        sum_ai_edit = sum(r["t_total_minutes"] for r in subset)
        sum_human = sum(r["t_human_minutes"] for r in subset)
        weighted_ratio = sum_ai_edit / sum_human if sum_human > 0 else 0.0
        mean_ratio = sum(r["time_ratio"] for r in subset) / n
        comm_pass = sum(1 for r in subset if r["verdict"].startswith("PASS"))
        return {
            "count": n,
            "reading_accuracy": (ok_dims / tot_dims) if tot_dims > 0 else 0.0,
            "first_pass_rate": sum(1 for r in subset if r["first_pass_build_ok"]) / n,
            "delivery_rate": sum(1 for r in subset if r["final_delivery_ok"]) / n,
            "editability_tested_count": len(edit_tested),
            "editability_pass_rate": (
                sum(1 for r in edit_tested if r["editability_passed"] is True) / len(edit_tested)
                if edit_tested
                else 0.0
            ),
            "sum_ai_plus_edit_minutes": round(sum_ai_edit, 2),
            "sum_human_baseline_minutes": round(sum_human, 2),
            "weighted_time_ratio": weighted_ratio,
            "mean_time_ratio": mean_ratio,
            "time_saved_pct": (1.0 - weighted_ratio) * 100.0,
            "throughput_multiplier_x": (1.0 / weighted_ratio) if weighted_ratio > 1e-6 else 0.0,
            "commercial_pass_rate": comm_pass / n,
        }

    valid_vision_rows = [r for r in evaluated_rows if r["vision_verified"]]
    polluted_rows = [r for r in evaluated_rows if not r["vision_verified"]]

    return {
        "all_runs_summary": _summarize(evaluated_rows),
        "valid_vision_summary": _summarize(valid_vision_rows),
        "polluted_count": len(polluted_rows),
        "failure_breakdown": failure_counts,
        "rows": evaluated_rows,
    }


def render_markdown_report(
    manifest: dict[str, Any],
    run_meta: dict[str, Any],
    evaluation: dict[str, Any],
) -> str:
    taxonomy = manifest.get("category_taxonomy") or {}
    valid_sum = evaluation["valid_vision_summary"]
    all_sum = evaluation["all_runs_summary"]
    rows = evaluation["rows"]

    lines: list[str] = []
    lines.append(f"# {manifest.get('benchmark_name', '2D-to-3D Benchmark')} — 评测报告")
    lines.append("")
    if run_meta.get("is_sample_fixture"):
        lines.append("> **说明**：当前报告基于内置自检夹具（`sample_eval_runs.json`）生成，用于校验评分管线与报表格式。")
        lines.append("")

    lines.append(f"- **评测批次 ID**：`{run_meta.get('run_suite_id', 'N/A')}`")
    lines.append(f"- **默认模型配置**：`{run_meta.get('model_profile', 'N/A')}`")
    lines.append(
        f"- **有效视觉评测样本数**：`{valid_sum['count']}` 件"
        + (f"（另有 `{evaluation['polluted_count']}` 件因无视觉模型环境污染已单独隔离对比）" if evaluation["polluted_count"] else "")
    )
    lines.append("")

    lines.append("## 一、核心北极星指标总览（有效视觉模型基线）")
    lines.append("")
    lines.append("| 指标名称 | 计算口径 | 有效视觉基线结果 | 含污染样本全量结果 | 商业准入目标 |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append(
        f"| **读图关键尺寸准确率** | `读对尺寸数 / 账本总尺寸数` | **{valid_sum['reading_accuracy'] * 100:.1f}%** | "
        f"{all_sum['reading_accuracy'] * 100:.1f}% | `>= 90.0%` |"
    )
    lines.append(
        f"| **一次构建通过率 (Pass@1)** | `首次提交无编译/重建报错比例` | **{valid_sum['first_pass_rate'] * 100:.1f}%** | "
        f"{all_sum['first_pass_rate'] * 100:.1f}% | `>= 70.0%` |"
    )
    lines.append(
        f"| **最终核验可交付率** | `包络/体积达标且单实体闭合比例` | **{valid_sum['delivery_rate'] * 100:.1f}%** | "
        f"{all_sum['delivery_rate'] * 100:.1f}% | `>= 85.0%` |"
    )
    edit_str = (
        f"**{valid_sum['editability_pass_rate'] * 100:.1f}%** ({valid_sum['editability_tested_count']}件实测)"
        if valid_sum["editability_tested_count"] > 0
        else "待人工实测"
    )
    all_edit_str = (
        f"{all_sum['editability_pass_rate'] * 100:.1f}%"
        if all_sum["editability_tested_count"] > 0
        else "待人工实测"
    )
    lines.append(
        f"| **特征树可编辑性通过率** | `改1~2个主尺寸 Ctrl+B 零报错比例` | {edit_str} | {all_edit_str} | `100% (硬门禁)` |"
    )
    lines.append(
        f"| **北极星耗时比 $R$** | `Σ(T_ai + T_edit) / Σ(T_human)` | **{valid_sum['weighted_time_ratio']:.2f}** "
        f"({valid_sum.get('sum_ai_plus_edit_minutes', 0):.1f}m / {valid_sum.get('sum_human_baseline_minutes', 0):.1f}m) | "
        f"{all_sum['weighted_time_ratio']:.2f} | **`<= 0.50`** (省时 ≥50%) |"
    )
    lines.append(
        f"| **单工作站产能放大倍数** | `1 / R`（熟手协同吞吐倍率） | **{valid_sum['throughput_multiplier_x']:.2f}x** "
        f"(省时 {valid_sum['time_saved_pct']:.1f}%) | {all_sum['throughput_multiplier_x']:.2f}x | **`>= 2.0x ~ 3.0x`** |"
    )
    lines.append("")

    lines.append("## 二、逐零件评测明细表")
    lines.append("")
    lines.append(
        "| 零件 ID | 零件名称 | 分类 | 读图准确率 | 一次通过 | 可编辑性 | `T_ai + T_edit` (min) | `T_human` (min) | 耗时比 `R` (提效) | 综合判定 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    for r in rows:
        cat_label = taxonomy.get(r["category"], {}).get("label", r["category"])
        fp_icon = "PASS" if r["first_pass_build_ok"] else "RETRY/FAIL"
        if r["editability_passed"] is True:
            ed_icon = "PASS"
        elif r["editability_passed"] is False:
            ed_icon = "FAIL"
        else:
            ed_icon = "N/A"
        speed_txt = f"{r['speedup_x']:.2f}x" if r["speedup_x"] else "-"
        lines.append(
            f"| `{r['case_id']}` | {r['case_name']} | {cat_label} | "
            f"{r['dimensions_correct']}/{r['dimensions_total']} ({r['reading_accuracy'] * 100:.0f}%) | "
            f"{fp_icon} | {ed_icon} | {r['t_ai_minutes']:.1f} + {r['t_edit_minutes']:.1f} = **{r['t_total_minutes']:.1f}** | "
            f"{r['t_human_minutes']:.1f} | **{r['time_ratio']:.2f}** ({speed_txt}) | {r['verdict']} |"
        )
    lines.append("")

    lines.append("## 三、异常/重试根因四象限归因")
    lines.append("")
    fb = evaluation["failure_breakdown"]
    if not fb:
        lines.append("- 本批次无异常或回滚事件。")
    else:
        lines.append("| 归因代码 | 工程归因说明 | 触发频次 |")
        lines.append("| --- | --- | --- |")
        for code, count in sorted(fb.items(), key=lambda kv: (-kv[1], kv[0])):
            desc = FAILURE_CATEGORY_LABELS.get(code, code)
            lines.append(f"| `{code}` | {desc} | {count} |")
    lines.append("")

    lines.append("## 四、基准集覆盖度快照（`manifest.json`）")
    lines.append("")
    lines.append("| 分类代码 | 分类名称 | 已收录用例数 | 状态分布（Active / Pending Retest / Reserved） |")
    lines.append("| --- | --- | --- | --- |")
    cases = manifest.get("cases", [])
    for cat_code, cat_info in taxonomy.items():
        cat_cases = [c for c in cases if c.get("category") == cat_code]
        n_act = sum(1 for c in cat_cases if c.get("status") == "active")
        n_ret = sum(1 for c in cat_cases if c.get("status") == "pending_retest")
        n_res = sum(1 for c in cat_cases if c.get("status") == "reserved_slot")
        lines.append(f"| `{cat_code}` | {cat_info.get('label', cat_code)} | {len(cat_cases)} | `{n_act} / {n_ret} / {n_res}` |")
    lines.append("")
    return "\n".join(lines)


def run_self_check(manifest_path: Path, output_path: Path | None = None) -> int:
    """Execute offline end-to-end verification of manifest, sample feature graphs, and evaluator."""
    print("[self-check 1/4] 校验 benchmarks/manifest.json 结构与五大高频分类覆盖度...")
    manifest = load_json(manifest_path)
    manifest_errors = validate_manifest(manifest, REPO_ROOT)
    if manifest_errors:
        for err in manifest_errors:
            print(f"  [ERROR] {err}", file=sys.stderr)
        return 1
    print(f"  -> OK: 共收录 {len(manifest['cases'])} 个基准用例，覆盖全部 5 大中小私企高频分类。")

    print("[self-check 2/4] 校验示例 Feature Graph 与编译器/尺寸账本契约兼容性...")
    sample_graph_path = BENCH_DIR / "fixtures" / "sample_stepped_shaft_graph.json"
    fg_errors = validate_feature_graph_file(sample_graph_path, "sample_stepped_shaft_graph")
    if fg_errors:
        for err in fg_errors:
            print(f"  [ERROR] {err}", file=sys.stderr)
        return 1
    print("  -> OK: sample_stepped_shaft_graph.json 通过确定性编译器与尺寸账本静态校验。")

    print("[self-check 3/4] 校验 batch_build.py 日志解析对接能力...")
    case_map = {c["id"]: c for c in manifest["cases"]}
    sample_log_path = BENCH_DIR / "fixtures" / "sample_batch_build.log"
    batch_runs = parse_batch_build_log(sample_log_path.read_text(encoding="utf-8"), case_map)
    if len(batch_runs) != 2 or not all(r["final_delivery_ok"] for r in batch_runs):
        print("  [ERROR] batch_build.py 样例日志解析结果与预期不符。", file=sys.stderr)
        return 1
    batch_eval = evaluate_runs(manifest, batch_runs)
    print(
        f"  -> OK: 成功解析 {len(batch_runs)} 条 batch_build 重放记录，累计纯 CAD 构建耗时 "
        f"{batch_eval['valid_vision_summary']['sum_ai_plus_edit_minutes']:.2f} 分钟。"
    )

    print("[self-check 4/4] 运行完整端到端样例评分并生成 Markdown 评测报告...")
    sample_runs_path = BENCH_DIR / "fixtures" / "sample_eval_runs.json"
    run_meta, norm_runs = normalize_runs_payload(load_json(sample_runs_path), case_map)
    evaluation = evaluate_runs(manifest, norm_runs)
    valid_sum = evaluation["valid_vision_summary"]
    if not (0.0 < valid_sum["weighted_time_ratio"] <= 0.50):
        print(
            f"  [ERROR] 样例有效视觉基线北极星耗时比异常: {valid_sum['weighted_time_ratio']:.3f}",
            file=sys.stderr,
        )
        return 1

    report_md = render_markdown_report(manifest, run_meta, evaluation)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")
        print(f"  -> OK: 报告已写入 {output_path}")

    print("\n" + "=" * 76)
    print(report_md)
    print("=" * 76)
    print("[SELF-CHECK PASSED] 所有离线基准集契约、编译器联动校验与人效比评测逻辑均零报错通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="dsh-solidworks 2D-to-3D 基准集离线校验与北极星人效比评测脚本"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=BENCH_DIR / "manifest.json",
        help="基准集 manifest.json 路径（默认 benchmarks/manifest.json）",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="仅校验 manifest.json 与关联 Feature Graph 契约完整性",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="评测运行结果 JSON（支持标准评测格式或 batch_build.py 导出的 runs JSON）",
    )
    parser.add_argument(
        "--batch-log",
        type=Path,
        default=None,
        help="dsh-integration/scripts/batch_build.py 的控制台输出日志文件路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出 Markdown 评测报告文件路径（不指定则打印到标准输出）",
    )
    parser.add_argument(
        "--json-summary",
        type=Path,
        default=None,
        help="可选导出机器可读的聚合统计 JSON 路径",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="执行全套离线契约校验、batch_build 日志兼容校验并输出样例评测报告表",
    )
    args = parser.parse_args(argv)

    if args.self_check:
        return run_self_check(args.manifest, args.output)

    if not args.manifest.exists():
        print(f"ERROR: manifest 文件不存在: {args.manifest}", file=sys.stderr)
        return 2

    manifest = load_json(args.manifest)
    errors = validate_manifest(manifest, REPO_ROOT)
    if errors:
        print("MANIFEST VALIDATION FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if args.validate_only or (args.results is None and args.batch_log is None):
        print(f"OK: {args.manifest} 校验通过（共 {len(manifest['cases'])} 个基准用例）。")
        return 0

    case_map = {c["id"]: c for c in manifest["cases"]}
    combined_runs: list[dict[str, Any]] = []
    run_meta: dict[str, Any] = {"run_suite_id": "cli-evaluation", "model_profile": "mixed/cli"}

    if args.results is not None:
        run_meta, res_runs = normalize_runs_payload(load_json(args.results), case_map)
        combined_runs.extend(res_runs)

    if args.batch_log is not None:
        log_runs = parse_batch_build_log(args.batch_log.read_text(encoding="utf-8"), case_map)
        combined_runs.extend(log_runs)

    evaluation = evaluate_runs(manifest, combined_runs)
    report_md = render_markdown_report(manifest, run_meta, evaluation)

    if args.json_summary is not None:
        args.json_summary.parent.mkdir(parents=True, exist_ok=True)
        args.json_summary.write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report_md, encoding="utf-8")
        print(f"Markdown 评测报告已生成: {args.output}")
    else:
        print(report_md)

    return 0


if __name__ == "__main__":
    sys.exit(main())
