"""Diagnostic attribution: separate 'drawing-intake reading error' from 'CAD modeling/execution error'.

Pure, side-effect-free helper consumed by `CadOrchestrator` during `submit_feature_graph` and
`finish_job`. Compares:
  - `planned_by_model`: what the model planned after reading the drawing (with SI meters/radians
    automatically converted to human-readable `mm` and `deg`), and
  - `measured_in_cad`: what SolidWorks actually built and read back (`bbox_size`, `volume`,
    `bodies`, feature counts, and `dimension_ledger` geometric checks).

Emits one of four deterministic attribution categories:
  - `cad_execution_or_rebuild_error`: compiler validation, feature creation, rebuild, or objective
    post-verification failed before producing valid geometry.
  - `plan_vs_cad_mismatch`: SolidWorks built geometry, but measured envelope, volume, ledger
    dimensions, or feature counts deviate from the model's own plan (modeling/lowering bug).
  - `matches_model_plan_check_drawing_intake`: SolidWorks faithfully reproduced the model's
    submitted plan within tolerance — if the part still disagrees with the paper drawing, the
    root cause is in drawing intake (misread callout, wrong view correspondence, or omitted detail).
  - `unverified_or_partial_telemetry`: neither `expect` nor a measured `dimension_ledger` check
    was present to anchor a plan-vs-CAD comparison.
"""
import json
import math

CATEGORY_EXECUTION_ERROR = "cad_execution_or_rebuild_error"
CATEGORY_PLAN_MISMATCH = "plan_vs_cad_mismatch"
CATEGORY_MATCHES_PLAN = "matches_model_plan_check_drawing_intake"
CATEGORY_PARTIAL_TELEMETRY = "unverified_or_partial_telemetry"

_MAX_NODE_SUMMARIES = 20
_MAX_LEDGER_SUMMARIES = 20


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mm(meters_val):
    if not _is_num(meters_val):
        return None
    return round(float(meters_val) * 1000.0, 3)


def _deg(rad_val):
    if not _is_num(rad_val):
        return None
    return round(math.degrees(float(rad_val)), 3)


def _sketch_profile_summary_mm(profile):
    """Compact human-readable mm summary of a sketch profile's primitives and overall span."""
    if not isinstance(profile, list) or not profile:
        return None
    xs, ys = [], []
    highlights = []
    for prim in profile:
        if not isinstance(prim, dict) or prim.get("construction") is True:
            continue
        kind = prim.get("kind")
        if kind == "rectangle" and _is_num(prim.get("width")) and _is_num(prim.get("height")):
            w, h = _mm(prim["width"]), _mm(prim["height"])
            cx = float(prim.get("cx") or 0.0)
            cy = float(prim.get("cy") or 0.0)
            hw, hh = float(prim["width"]) / 2.0, float(prim["height"]) / 2.0
            xs.extend((cx - hw, cx + hw))
            ys.extend((cy - hh, cy + hh))
            if len(highlights) < 4:
                highlights.append("rect %gx%gmm" % (w, h))
        elif kind == "rounded_rect" and _is_num(prim.get("width")) and _is_num(prim.get("height")):
            w, h, r = _mm(prim["width"]), _mm(prim["height"]), _mm(prim.get("radius"))
            cx = float(prim.get("cx") or 0.0)
            cy = float(prim.get("cy") or 0.0)
            hw, hh = float(prim["width"]) / 2.0, float(prim["height"]) / 2.0
            xs.extend((cx - hw, cx + hw))
            ys.extend((cy - hh, cy + hh))
            if len(highlights) < 4:
                highlights.append("rrect %gx%gmm R%s" % (w, h, r))
        elif kind == "circle" and _is_num(prim.get("diameter")):
            d = _mm(prim["diameter"])
            cx = float(prim.get("cx") or 0.0)
            cy = float(prim.get("cy") or 0.0)
            rad = float(prim["diameter"]) / 2.0
            xs.extend((cx - rad, cx + rad))
            ys.extend((cy - rad, cy + rad))
            if len(highlights) < 4:
                highlights.append("Ø%gmm" % d)
        elif kind == "slot" and _is_num(prim.get("length")) and _is_num(prim.get("width")):
            length_mm, width_mm = _mm(prim["length"]), _mm(prim["width"])
            if len(highlights) < 4:
                highlights.append("slot %gx%gmm" % (length_mm, width_mm))
        elif kind in ("line", "arc"):
            for kx, ky in (("x1", "y1"), ("x2", "y2")):
                if _is_num(prim.get(kx)) and _is_num(prim.get(ky)):
                    xs.append(float(prim[kx]))
                    ys.append(float(prim[ky]))
            if kind == "arc" and _is_num(prim.get("cx")) and _is_num(prim.get("cy")) and _is_num(prim.get("x1")) and _is_num(prim.get("y1")):
                r_m = math.hypot(float(prim["x1"]) - float(prim["cx"]), float(prim["y1"]) - float(prim["cy"]))
                if len(highlights) < 4:
                    highlights.append("R%gmm" % _mm(r_m))
    summary = {"primitives": len(profile)}
    if highlights:
        summary["items"] = highlights
    if xs and ys:
        summary["span_uv_mm"] = [round((max(xs) - min(xs)) * 1000.0, 3),
                                 round((max(ys) - min(ys)) * 1000.0, 3)]
    return summary


def summarize_node_mm(node):
    """Convert one Feature Graph node's key SI parameters into a compact mm/deg dict."""
    if not isinstance(node, dict):
        return None
    nid = node.get("id")
    ntype = node.get("type")
    out = {"id": nid, "type": ntype}
    if ntype == "box":
        out["size_mm"] = [_mm(node.get("width")), _mm(node.get("depth")), _mm(node.get("height"))]
    elif ntype == "sketch":
        ref = node.get("ref") or {}
        if _is_num(ref.get("offset")) and float(ref.get("offset")) != 0.0:
            out["offset_mm"] = _mm(ref.get("offset"))
        if ref.get("datum"):
            out["datum"] = ref.get("datum")
        prof = _sketch_profile_summary_mm(node.get("profile"))
        if prof:
            out["profile_mm"] = prof
    elif ntype == "extrude":
        out["operation"] = node.get("operation")
        end = node.get("end")
        if end:
            out["end"] = end
        if _is_num(node.get("depth")):
            out["depth_mm"] = _mm(node.get("depth"))
        elif node.get("through") is True or node.get("depth") in ("through_all", "through") or end == "through_all":
            out["depth_mm"] = "through_all"
        if node.get("reversed") is True:
            out["reversed"] = True
    elif ntype == "revolve":
        out["operation"] = node.get("operation", "boss")
        out["angle_deg"] = _deg(node.get("angle")) if _is_num(node.get("angle")) else 360.0
    elif ntype == "hole":
        out["diameter_mm"] = _mm(node.get("diameter"))
        depth = node.get("depth")
        out["depth_mm"] = _mm(depth) if _is_num(depth) else depth
        cb = node.get("counterbore")
        if isinstance(cb, dict):
            out["counterbore_mm"] = {
                "diameter_mm": _mm(cb.get("diameter")),
                "depth_mm": _mm(cb.get("depth")),
            }
        th = node.get("thread")
        if isinstance(th, dict) and th.get("designation"):
            out["thread"] = th.get("designation")
    elif ntype == "cosmetic_thread":
        out["designation"] = node.get("designation")
        depth = node.get("depth")
        out["depth_mm"] = _mm(depth) if _is_num(depth) else depth
    elif ntype == "fillet":
        out["radius_mm"] = _mm(node.get("radius"))
        out["edge_count"] = len(node.get("edges") or [])
    elif ntype == "chamfer":
        out["distance_mm"] = _mm(node.get("distance"))
        if _is_num(node.get("distance2")):
            out["distance2_mm"] = _mm(node.get("distance2"))
        elif _is_num(node.get("angle")):
            out["angle_deg"] = _deg(node.get("angle"))
        out["edge_count"] = len(node.get("edges") or [])
    elif ntype in ("shell", "rib", "sheet_metal"):
        out["thickness_mm"] = _mm(node.get("thickness"))
    elif ntype == "sketched_bend":
        out["angle_deg"] = _deg(node.get("angle")) if _is_num(node.get("angle")) else 90.0
        if _is_num(node.get("radius")):
            out["radius_mm"] = _mm(node.get("radius"))
    elif ntype == "edge_flange":
        out["angle_deg"] = _deg(node.get("angle")) if _is_num(node.get("angle")) else 90.0
        if _is_num(node.get("length")):
            out["length_mm"] = _mm(node.get("length"))
    elif ntype == "linear_pattern":
        out["count"] = node.get("count")
        out["spacing_mm"] = _mm(node.get("spacing"))
        out["direction"] = node.get("direction")
    elif ntype == "circular_pattern":
        out["count"] = node.get("count")
        out["angle_deg"] = node.get("angle_deg")
    return out


def extract_planned_by_model(graph):
    """Extract a readable mm-scale summary of what the model submitted in a Feature Graph."""
    if not isinstance(graph, dict):
        return {}
    planned = {}
    expect = graph.get("expect")
    if isinstance(expect, dict):
        bbox = expect.get("bbox_mm")
        if isinstance(bbox, list) and len(bbox) == 3 and all(_is_num(v) for v in bbox):
            planned["envelope_mm"] = sorted(round(float(v), 3) for v in bbox)
        vol = expect.get("volume_mm3")
        if _is_num(vol):
            planned["volume_mm3"] = round(float(vol), 2)
        feats = expect.get("features")
        if isinstance(feats, dict) and feats:
            planned["expected_features"] = dict(feats)

    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    if nodes:
        planned["node_count"] = len(nodes)
        planned["node_dimensions_mm"] = [
            summarize_node_mm(n) for n in nodes[:_MAX_NODE_SUMMARIES]
        ]
        if len(nodes) > _MAX_NODE_SUMMARIES:
            planned["nodes_truncated"] = len(nodes) - _MAX_NODE_SUMMARIES

    ledger = graph.get("dimension_ledger")
    if isinstance(ledger, list) and ledger:
        items = []
        for entry in ledger[:_MAX_LEDGER_SUMMARIES]:
            if not isinstance(entry, dict):
                continue
            items.append({
                "id": entry.get("id"),
                "label": entry.get("label"),
                "view": entry.get("view"),
                "source_kind": entry.get("source_kind"),
                "planned_mm": entry.get("value"),
                "has_cad_check": isinstance(entry.get("check"), dict),
            })
        if items:
            planned["ledger_dimensions_mm"] = items
    return planned


def _readback_kv(verification):
    kv = {}
    for check in (verification or {}).get("deep_checks") or []:
        if not isinstance(check, dict):
            continue
        for item in ((check.get("cadState") or {}).get("features")) or []:
            if isinstance(item, str) and "=" in item:
                key, _, raw = item.partition("=")
                kv.setdefault(key.strip(), raw.strip())
    return kv


def extract_measured_in_cad(
    verification,
    *,
    compiler=None,
    built_feature_types=None,
    planned_summary=None,
    warnings=None,
):
    """Extract SolidWorks readback geometry in mm / mm³ alongside ledger verification outcomes."""
    measured = {}
    kv = _readback_kv(verification)
    raw_bbox = kv.get("bbox_size")
    if raw_bbox is not None:
        try:
            parsed = json.loads(raw_bbox)
            if isinstance(parsed, list) and len(parsed) == 3 and all(_is_num(v) for v in parsed):
                axes_mm = [round(float(v) * 1000.0, 3) for v in parsed]
                measured["envelope_axes_xyz_mm"] = axes_mm
                measured["envelope_mm"] = sorted(axes_mm)
        except (TypeError, ValueError):
            pass

    if "volume" in kv:
        try:
            vol_m3 = float(kv["volume"].replace(",", "."))
            if math.isfinite(vol_m3):
                measured["volume_mm3"] = round(vol_m3 * 1e9, 2)
        except ValueError:
            pass

    if "bodies" in kv:
        try:
            measured["body_count"] = int(kv["bodies"])
        except ValueError:
            pass

    if isinstance(compiler, dict) and compiler:
        if compiler.get("nodes_completed") is not None:
            measured["nodes_completed"] = compiler.get("nodes_completed")
        if compiler.get("nodes_total") is not None:
            measured["nodes_total"] = compiler.get("nodes_total")

    if isinstance(built_feature_types, dict) and built_feature_types:
        measured["built_feature_types"] = dict(built_feature_types)

    warn_by_dim = {}
    for w in warnings or []:
        if isinstance(w, dict) and w.get("code") in ("LEDGER_MISMATCH", "LEDGER_UNMEASURED") and w.get("dimension_id"):
            warn_by_dim[w["dimension_id"]] = w

    ledger_plan = (planned_summary or {}).get("ledger_dimensions_mm") or []
    ledger_Invalid = any(isinstance(w, dict) and w.get("code") == "LEDGER_INVALID" for w in (warnings or []))
    if ledger_plan and not ledger_Invalid:
        comparisons = []
        for item in ledger_plan:
            dim_id = item.get("id")
            if dim_id in warn_by_dim:
                w = warn_by_dim[dim_id]
                if w.get("code") == "LEDGER_MISMATCH":
                    actual = round(float(w["actual"]), 3) if _is_num(w.get("actual")) else w.get("actual")
                    expected = w.get("expected")
                    delta = (round(float(w["actual"]) - float(expected), 3)
                             if _is_num(w.get("actual")) and _is_num(expected) else None)
                    comparisons.append({
                        "dimension_id": dim_id,
                        "label": item.get("label"),
                        "planned_mm": expected,
                        "measured_mm": actual,
                        "delta_mm": delta,
                        "tolerance_mm": w.get("tolerance_mm"),
                        "status": "mismatch",
                    })
                else:
                    comparisons.append({
                        "dimension_id": dim_id,
                        "label": item.get("label"),
                        "planned_mm": item.get("planned_mm"),
                        "measured_mm": None,
                        "reason": w.get("reason"),
                        "status": "unmeasured",
                    })
            elif item.get("has_cad_check") and item.get("source_kind") != "unknown":
                comparisons.append({
                    "dimension_id": dim_id,
                    "label": item.get("label"),
                    "planned_mm": item.get("planned_mm"),
                    "status": "matched_within_tolerance",
                })
        if comparisons:
            measured["ledger_comparisons"] = comparisons

    if (verification or {}).get("target_closed"):
        measured["target_closed"] = True
    return measured


def build_diagnostic_attribution(
    *,
    graph=None,
    planned_summary=None,
    verification=None,
    compiler=None,
    plan_check=None,
    warnings=None,
    error=None,
    built_feature_types=None,
):
    """Build a structured diagnostic attribution separating intake errors from CAD build errors."""
    planned = (
        dict(planned_summary)
        if isinstance(planned_summary, dict) and planned_summary
        else extract_planned_by_model(graph)
    )
    all_warnings = list(warnings or [])
    measured = extract_measured_in_cad(
        verification,
        compiler=compiler,
        built_feature_types=built_feature_types,
        planned_summary=planned,
        warnings=all_warnings,
    )

    discrepancies = []
    notes = []

    err_code = (error or {}).get("code") if isinstance(error, dict) else None
    if err_code and err_code != "PLAN_MISMATCH":
        detail = {
            "kind": "execution_or_verification_error",
            "code": err_code,
            "message": (error or {}).get("message"),
        }
        for key in ("node_id", "node_type", "step"):
            if (error or {}).get(key):
                detail[key] = error[key]
        discrepancies.append(detail)
        return {
            "category": CATEGORY_EXECUTION_ERROR,
            "responsible_stage": "cad_compilation_or_execution",
            "verdict_zh": (
                "CAD 编译或 SolidWorks 重建执行失败（建模执行阶段问题）：特征图未能完成几何构建或后置校验未通过，"
                "请优先排查报错节点的放置基准面、草图闭合性或切割方向，而非怀疑图纸识图。"
            ),
            "planned_by_model": planned,
            "measured_in_cad": measured,
            "discrepancies": discrepancies,
        }

    # Check objective plan-vs-CAD mismatches (envelope, volume, ledger, skipped/missing features).
    pcheck = plan_check if isinstance(plan_check, dict) else (verification or {}).get("plan_check")
    if err_code == "PLAN_MISMATCH" or (isinstance(pcheck, dict) and pcheck.get("bbox_ok") is False):
        discrepancies.append({
            "kind": "envelope_mismatch",
            "code": "PLAN_MISMATCH",
            "planned_bbox_mm": (pcheck or {}).get("expected_bbox_mm") or planned.get("envelope_mm"),
            "measured_bbox_mm": (pcheck or {}).get("built_bbox_mm") or measured.get("envelope_mm"),
            "axis_misses_mm": (pcheck or {}).get("axis_misses_mm"),
        })

    if isinstance(pcheck, dict) and pcheck.get("volume_ok") is False:
        discrepancies.append({
            "kind": "volume_mismatch",
            "code": "PLAN_VOLUME_MISMATCH",
            "planned_volume_mm3": pcheck.get("expected_volume_mm3"),
            "measured_volume_mm3": pcheck.get("built_volume_mm3"),
        })

    for w in all_warnings:
        if not isinstance(w, dict):
            continue
        wcode = w.get("code")
        if wcode == "LEDGER_MISMATCH":
            actual = round(float(w["actual"]), 3) if _is_num(w.get("actual")) else w.get("actual")
            expected = w.get("expected")
            delta = (round(float(w["actual"]) - float(expected), 3)
                     if _is_num(w.get("actual")) and _is_num(expected) else None)
            discrepancies.append({
                "kind": "dimension_ledger_mismatch",
                "code": "LEDGER_MISMATCH",
                "dimension_id": w.get("dimension_id"),
                "label": w.get("label"),
                "node_ids": w.get("node_ids") or [],
                "planned_mm": expected,
                "measured_mm": actual,
                "delta_mm": delta,
                "tolerance_mm": w.get("tolerance_mm"),
            })
        elif wcode == "PLANNED_FEATURES_MISSING":
            discrepancies.append({
                "kind": "planned_features_missing",
                "code": "PLANNED_FEATURES_MISSING",
                "missing": w.get("missing"),
                "planned_ids": w.get("planned_ids"),
            })
        elif wcode == "EDGE_FEATURE_SKIPPED":
            discrepancies.append({
                "kind": "edge_feature_skipped",
                "code": "EDGE_FEATURE_SKIPPED",
                "node_id": w.get("node_id"),
                "node_type": w.get("node_type"),
                "error_code": w.get("error_code"),
            })
        elif wcode == "DIRECTION_FLIPPED":
            notes.append(
                "节点 '%s' 触发切空自动翻向（DIRECTION_FLIPPED），需结合三视图核对其切除侧向。"
                % (w.get("node_id") or "?")
            )

    if measured.get("body_count") is not None and measured["body_count"] > 1:
        notes.append(
            "SolidWorks 实测存在 %d 个分离实体（bodies=%d）：若目标为单一零件，请检查是否有凸台未与主体相连或被贯通切除截断。"
            % (measured["body_count"], measured["body_count"])
        )

    if discrepancies:
        result = {
            "category": CATEGORY_PLAN_MISMATCH,
            "responsible_stage": "feature_graph_modeling",
            "verdict_zh": (
                "实测几何偏离模型计划（按数据建模阶段偏差）：SolidWorks 建出的实测包络/体积/尺寸或特征数与模型提交的计划不一致，"
                "属于 Feature Graph 坐标变换、拉伸/切除方向或特征漏建问题，请按 discrepancies 修正特征图。"
            ),
            "planned_by_model": planned,
            "measured_in_cad": measured,
            "discrepancies": discrepancies,
        }
        if notes:
            result["notes"] = notes
        return result

    # Determine whether at least one objective plan-vs-CAD check actively passed.
    bbox_verified = isinstance(pcheck, dict) and pcheck.get("bbox_ok") is True
    vol_verified = isinstance(pcheck, dict) and pcheck.get("volume_ok") is True
    ledger_verified = any(
        c.get("status") == "matched_within_tolerance"
        for c in (measured.get("ledger_comparisons") or [])
    )

    if bbox_verified or vol_verified or ledger_verified:
        result = {
            "category": CATEGORY_MATCHES_PLAN,
            "responsible_stage": "drawing_intake_if_wrong",
            "verdict_zh": (
                "CAD 实测几何与模型提交的尺寸计划一致：SolidWorks 已按 Feature Graph 还原计划尺寸。"
                "若生成的零件仍与工程图不符，根因在模型读图阶段（看错标注数值、视图投影对应错误或漏读局部结构），"
                "请对照原图复核 planned_by_model 中的计划值。"
            ),
            "planned_by_model": planned,
            "measured_in_cad": measured,
            "discrepancies": [],
        }
        if notes:
            result["notes"] = notes
        return result

    result = {
        "category": CATEGORY_PARTIAL_TELEMETRY,
        "responsible_stage": "insufficient_plan_expectation",
        "verdict_zh": (
            "缺少完整的计划预期（expect 包络或已测量的 dimension_ledger）或文档已关闭无法实测；"
            "已列出可观测的模型提交参数与 CAD 实测几何供人工对照。"
        ),
        "planned_by_model": planned,
        "measured_in_cad": measured,
        "discrepancies": [],
    }
    if notes:
        result["notes"] = notes
    return result
