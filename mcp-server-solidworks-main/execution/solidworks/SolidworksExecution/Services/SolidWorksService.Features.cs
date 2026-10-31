using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Xml;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;


namespace SolidworksExecution.Services
{
    // SolidWorksService partial: part-modeling features (extrude/revolve/sweep/loft, rib, shell, edge fillet/chamfer, reference geometry, patterns, material, dimension/feature edits).
    public partial class SolidWorksService
    {

        public ExecutionResponse ExtrudeFeature(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                double? depth = p?.Value<double?>("depth");

                string featureType = (p?.Value<string>("feature_type") ?? "boss").ToLowerInvariant();
                if (featureType != "boss" && featureType != "cut" && featureType != "revolve" && featureType != "sweep" && featureType != "loft")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "UNSUPPORTED_FEATURE_TYPE",
                        $"feature_type '{featureType}' is not supported. Supported: boss, cut, revolve, sweep, loft.");

                // is_cut = "the swept profile / revolved profile REMOVES material". It is honoured by the
                // revolve branch (FeatureRevolve2's IsCut argument) and the sweep branch
                // (InsertCutSwept5 instead of InsertProtrusionSwept4); the token is branch-local, so it is
                // refused where it would be silently ignored rather than accepted and dropped.
                // feature_type='cut' + is_cut=true is allowed: the extrude is already a removal, so the
                // flag is redundant-but-agreeing, and a caller that sets it from a generic
                // operation='cut' field must not fail for that.
                bool isCut = p?.Value<bool?>("is_cut") ?? false;
                if (isCut && (featureType == "boss" || featureType == "loft"))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"is_cut=true contradicts feature_type='{featureType}': a boss adds material, and " +
                        "there is no loft-cut variant in this API surface (InsertLoft2 has no cut form here). " +
                        "Use feature_type='cut' for a cut extrude, is_cut=true with feature_type='sweep' for a " +
                        "swept cut, or is_cut=false. Nothing was changed.");

                // depth (> 0) is required ONLY for a BLIND boss/cut. A through-all (through=true) ignores
                // depth, and revolve/sweep/loft don't use it — so don't demand it there (KNOWN-LIMITATIONS #13).
                bool throughAll = p?.Value<bool?>("through") ?? false;
                // up_to_face_index >= 0 ⇒ up-to-surface end condition: the extrude/cut terminates ON that
                // face (index from analyze_model(faces), same enumeration create_sketch(on_face) walks).
                // Depth is ignored, like through-all.
                int upToFaceIndex = p?.Value<int?>("up_to_face_index") ?? -1;
                bool upToSurface = upToFaceIndex >= 0;
                // mid_plane=true ⇒ swEndCondMidPlane: the feature extrudes SYMMETRICALLY about the
                // sketch plane; depth = the TOTAL width (SolidWorks midplane semantics).
                bool midPlane = p?.Value<bool?>("mid_plane") ?? false;
                if (upToSurface && featureType != "boss" && featureType != "cut")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "up_to_face_index is only valid for feature_type 'boss' or 'cut'.");
                if (midPlane && featureType != "boss" && featureType != "cut")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "mid_plane is only valid for feature_type 'boss' or 'cut'.");
                if (midPlane && (throughAll || upToSurface))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "mid_plane conflicts with through/up_to_face_index — pick ONE end condition.");
                if ((featureType == "boss" || featureType == "cut") && !throughAll && !upToSurface && (depth == null || depth.Value <= 0))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "depth (> 0 meters) is required for a blind or mid-plane boss/cut. Pass through=true for a through-all instead.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                // Direction + end-condition controls (boss/cut). reverse flips the feature direction
                // (needed e.g. for a cut sketched on a part FACE, where "into the body" is the opposite
                // side from a cut sketched on a boundary plane). through = through-all (depth ignored).
                bool reverse = p?.Value<bool?>("reverse") ?? false;
                bool strictDirection = p?.Value<bool?>("strict_direction") ?? false;
                bool directionFlipped = false;
                int endCond = upToSurface
                    ? (int)swEndConditions_e.swEndCondUpToSurface
                    : midPlane
                        ? (int)swEndConditions_e.swEndCondMidPlane
                        : throughAll
                            ? (int)swEndConditions_e.swEndCondThroughAll
                            : (int)swEndConditions_e.swEndCondBlind;
                // Through-all ignores depth; a blind boss/cut already validated depth > 0 above.
                // Default a missing depth to 0 so the API call is safe (e.g. a REST through-cut with no depth).
                double depthVal = depth ?? 0.0;

                // Capture the active sketch name before exiting — it is the PROFILE for sweep (and is
                // useful context generally). Re-selected by name after exit (names resolve post-exit).
                // The sweep profile could never be named while create_sketch echoed a "Sketch"
                // placeholder instead of the real feature name: the name reaching this line is the
                // same string the compiler recorded for the sketch node, so see ActiveSketchName for
                // how it is resolved.
                string activeProfileSketchName = ActiveSketchName(modelDoc);

                // Exit sketch mode if a sketch is still active. EXCEPTION: revolve resolves its axis
                // against the sketch's own segments (and may add a centerline to it), which only works
                // while the sketch is open — a closed sketch's segments are not enumerable through
                // SketchManager. Revolve handles the axis and the exit itself below.
                if (featureType != "revolve" && modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                var featureMgr = modelDoc.FeatureManager;
                IFeature feature;

                // A SWEPT CUT has to be verified against the before-state (the boss sweep never needed
                // it, since InsertProtrusionSwept4's non-null return was the whole claim). Holders are
                // declared here so the verification after the call can read them; only the cut branch
                // of the sweep fills them, so a boss sweep pays nothing for the extra reads.
                double? sweepCutVolumeBefore = null;
                int sweepCutFacesBefore = 0, sweepCutEdgesBefore = 0, sweepCutVertsBefore = 0;

                if (featureType == "revolve")
                {
                    // Requires depth param ignored; needs axis defined by two coordinate points in the sketch
                    double ax1 = p?.Value<double?>("axis_x1") ?? 0.0;
                    double ay1 = p?.Value<double?>("axis_y1") ?? 0.0;
                    double ax2 = p?.Value<double?>("axis_x2") ?? 0.0;
                    double ay2 = p?.Value<double?>("axis_y2") ?? 0.001;
                    // angle arrives in DEGREES (model-facing convention, like create_pattern circular /
                    // sheet_metal); convert to radians for FeatureRevolve2. Default 360 = full revolve.
                    double angleDeg = p?.Value<double?>("angle") ?? 360.0;
                    double angle = angleDeg * Math.PI / 180.0;
                    // Snap anything within ~0.06° of a full turn to an exact 2π so "full revolve" is clean
                    // (guards float rounding). Partial revolves (e.g. 180°) are unaffected.
                    if (Math.Abs(angle - 2.0 * Math.PI) < 1e-3) angle = 2.0 * Math.PI;

                    // is_cut (IR revolve.operation='cut'): FeatureRevolve2's 4TH argument is IsCut — true
                    // removes the swept material instead of adding a boss. Reflection-verified over
                    // SolidWorks.Interop.sldworks.dll: (1 SingleDir, 2 IsSolid, 3 IsThin, 4 IsCut,
                    // 5 ReverseDir, 6 BothDirectionUpToSameEntity, 7 Dir1Type, 8 Dir2Type, 9 Dir1Angle,
                    // 10 Dir2Angle, ...). Everything else is unchanged from the boss call.
                    bool revolveCut = isCut;

                    // The sketch is still active here (the generic exit above skips revolve), so its
                    // segments are enumerable and construction geometry can still be added to it.
                    if (modelDoc.SketchManager.ActiveSketch == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SKETCH_NOT_ACTIVE", "revolve needs the profile+axis sketch active. Call create_sketch and draw the profile + axis line first.");

                    // Make the sketch hold exactly ONE construction centerline on the requested axis,
                    // without editing any profile edge (see EnsureRevolveAxis for why this replaces the
                    // old midpoint pick). The call below selects NOTHING: FeatureRevolve2 runs with
                    // UseAutoSelect, and the selection is cleared again before it, so the axis can only
                    // come from the sketch's own centerline — which is what this guarantees.
                    string axisHow;
                    if (!EnsureRevolveAxis(modelDoc, ax1, ay1, ax2, ay2, out axisHow))
                    {
                        // Last resort: the pre-batch-2 behaviour — pick a segment at the axis midpoint and
                        // convert it. Kept so a sketch whose axis lives in a drawn edge still builds.
                        modelDoc.ClearSelection2(true);
                        bool axSel = modelDoc.Extension.SelectByID2("", "SKETCHSEGMENT", (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0, 0, false, 0, null, 0);
                        if (!axSel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "AXIS_NOT_FOUND", $"No sketch segment found on the axis ({ax1}, {ay1})-({ax2}, {ay2}) and no centerline could be added ({axisHow}). Provide axis_x1/y1/x2/y2 as two endpoints of the centerline.");

                        var revSelMgr = modelDoc.SelectionManager as ISelectionMgr;
                        var axisSeg = revSelMgr?.GetSelectedObject6(1, -1) as ISketchSegment;
                        if (axisSeg != null) axisSeg.ConstructionGeometry = true;
                        ExecLog.Write($"revolve axis: legacy midpoint pick fallback ({axisHow})");
                    }
                    else
                        ExecLog.Write($"revolve axis: {axisHow}");

                    // Remember the profile sketch, then exit it (post-exit, re-select it by name — names
                    // resolve after exit even though coordinates do not).
                    string profileSketchName = ActiveSketchName(modelDoc);
                    modelDoc.SketchManager.InsertSketch(true);
                    modelDoc.ClearSelection2(true);
                    if (!string.IsNullOrEmpty(profileSketchName))
                        modelDoc.Extension.SelectByID2(profileSketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);

                    feature = featureMgr.FeatureRevolve2(
                        true, true, false, revolveCut, false, false,
                        0, 0, angle, 0, false, false,
                        0.0, 0.0, 0, 0.0, 0.0, true, true, true) as IFeature;
                    if (feature == null && revolveCut)
                    {
                        // A null cut revolve is most often the WRONG direction (the cut swept away from
                        // material). Retry with ReverseDir (5th arg) flipped — the same recovery the
                        // blind cut-extrude path uses. Boss revolves never reach here (unchanged call).
                        // The rollback clears the selection, so re-select the profile sketch by name.
                        modelDoc.ClearSelection2(true);
                        if (!string.IsNullOrEmpty(profileSketchName))
                            modelDoc.Extension.SelectByID2(profileSketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                        feature = featureMgr.FeatureRevolve2(
                            true, true, false, true, true, false,
                            0, 0, angle, 0, false, false,
                            0.0, 0.0, 0, 0.0, 0.0, true, true, true) as IFeature;
                        ExecLog.Write($"revolve cut: flipped-direction retry → feature={(feature != null)}");
                    }
                }
                else if (featureType == "sweep")
                {
                    string pathSketch = p?.Value<string>("path_sketch");
                    if (string.IsNullOrEmpty(pathSketch))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER", "sweep requires path_sketch (name of the sketch containing the sweep path).");

                    // The profile sketch was already exited by the generic exit above. A sweep needs BOTH
                    // selected: the profile (selection mark 1) and the path (mark 4). Selecting only the
                    // path left InsertProtrusionSwept4 with no profile → null.
                    if (string.IsNullOrEmpty(activeProfileSketchName))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "PROFILE_NOT_FOUND", "sweep needs an active profile sketch when called. Create the profile sketch last (it stays active) and pass the path sketch name.");

                    modelDoc.ClearSelection2(true);
                    bool profSel = modelDoc.Extension.SelectByID2(activeProfileSketchName, "SKETCH", 0, 0, 0, false, 1, null, 0);
                    if (!profSel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "PROFILE_NOT_FOUND", $"Profile sketch '{activeProfileSketchName}' not found.");
                    bool pathSel = modelDoc.Extension.SelectByID2(pathSketch, "SKETCH", 0, 0, 0, true, 4, null, 0);
                    if (!pathSel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SKETCH_NOT_FOUND", $"Path sketch '{pathSketch}' not found. Ensure the sketch is named exactly as provided.");

                    if (isCut)
                    {
                        // is_cut + feature_type='sweep' — the SWEPT CUT. Same selection the boss sweep above
                        // uses (profile mark 1, path mark 4, UseAutoSelect), different manager call:
                        //   InsertProtrusionSwept4(20 args) -> Feature   [boss, in use since batch 5]
                        //   InsertCutSwept5(22 args)        -> Feature   [the cut, reflected here]
                        //   InsertCutSwept4(19 args)        -> Feature   [older; lacks the CircularProfile,
                        //                                                CircularProfileDiameter and the
                        //                                                obsolete Direction tail, and declares
                        //                                                TwistCtrlOption/StartMatchingType/
                        //                                                EndMatchingType/ThinType/PathAlign as
                        //                                                Int16 instead of Int32]
                        // The 5-form is used because it matches the boss form's argument TYPES (so the same
                        // literals mean the same thing) and it is the newest. Positions that differ between
                        // the two calls: boss 13 = Merge (a cut cannot merge, so the cut form has no Merge
                        // slot), and cut 17-19 = AssemblyFeatureScope / AutoSelectComponents /
                        // PropagateFeatureToParts (assembly-only, false in a part).
                        // Before-state, for the assertions after the rebuild.
                        sweepCutVolumeBefore = RawVolume(modelDoc);
                        TopologyCounts(modelDoc, out sweepCutFacesBefore, out sweepCutEdgesBefore, out sweepCutVertsBefore);

                        feature = featureMgr.InsertCutSwept5(
                            false, false, 0, false, false,
                            0, 0,
                            false, 0.0, 0.0, 0, 0,
                            true, true,
                            0.0, false,
                            false, false, false,
                            false, 0.0, 0) as IFeature;

                        if (feature == null)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "SWEEP_CUT_FAILED",
                                $"InsertCutSwept5 returned null for the profile sketch " +
                                $"'{activeProfileSketchName}' (mark 1) swept along path sketch '{pathSketch}' " +
                                "(mark 4): the usual causes are a path that never crosses existing material, " +
                                "a profile that is not closed, a path sketch that is not on/through the body, " +
                                "or no solid body at all. Nothing was added to the tree.");
                    }
                    else
                    {
                        feature = featureMgr.InsertProtrusionSwept4(
                            false, false, 0, false, false,
                            0, 0,
                            false, 0.0, 0.0, 0, 0,
                            true, true, true,
                            0.0, false, false, 0.0, 0) as IFeature;
                    }
                }
                else if (featureType == "loft")
                {
                    string profilesJson = p?.Value<string>("profiles");
                    if (string.IsNullOrEmpty(profilesJson))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER", "loft requires profiles (JSON array of sketch names, e.g. [\"Sketch1\",\"Sketch2\"]).");

                    JArray profileArray;
                    try { profileArray = JArray.Parse(profilesJson); }
                    catch { return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), "INVALID_PARAMETER", "profiles must be a valid JSON array of sketch name strings."); }

                    if (profileArray.Count < 2)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "INVALID_PARAMETER", "loft requires at least 2 profiles.");

                    if (modelDoc.SketchManager.ActiveSketch != null)
                        modelDoc.SketchManager.InsertSketch(true);

                    modelDoc.ClearSelection2(true);
                    for (int i = 0; i < profileArray.Count; i++)
                    {
                        string skName = profileArray[i].Value<string>();
                        bool sel = modelDoc.Extension.SelectByID2(skName, "SKETCH", 0, 0, 0, i > 0, 0, null, 0);
                        if (!sel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "SKETCH_NOT_FOUND", $"Profile sketch '{skName}' not found.");
                    }

                    feature = featureMgr.InsertProtrusionBlend2(
                        false, false, false, 0.1,
                        0, 0,
                        0.0, 0.0, false, false,
                        false, 0.0, 0.0, 0,
                        true, true, true, 0) as IFeature;
                }
                else if (featureType == "cut")
                {
                    // SHEET-METAL fix: a sheet-metal part keeps a (usually suppressed) Flat-Pattern as the
                    // LAST top-level feature, and the rollback bar sits AFTER it — so FeatureCut4 inserts
                    // the cut after Flat-Pattern (illegal; it must stay last) and silently returns null.
                    // Roll the bar to just before Flat-Pattern, make the cut (the profile sketch already
                    // sits before Flat-Pattern, so it stays available), then roll forward so Flat-Pattern
                    // re-applies on top of the cut — exactly what the SW UI does automatically.
                    IFeature flatPat = FindFlatPattern(modelDoc);
                    bool rolledBack = false;
                    if (flatPat != null)
                    {
                        try
                        {
                            rolledBack = modelDoc.FeatureManager.EditRollback(
                                (int)swMoveRollbackBarTo_e.swMoveRollbackBarToBeforeFeature, flatPat.Name);
                        }
                        catch { rolledBack = false; }
                    }

                    // Explicitly re-select the profile sketch by name. On a PLANE sketch the just-exited
                    // sketch stays implicitly selected as the profile, but on a FACE sketch the selected
                    // FACE remains the selection after exit, so FeatureCut4 found no profile → null.
                    // (Same post-exit re-select trick revolve uses.) Also required after a rollback, which
                    // clears the selection.
                    modelDoc.ClearSelection2(true);
                    if (!string.IsNullOrEmpty(activeProfileSketchName))
                        modelDoc.Extension.SelectByID2(activeProfileSketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                    if (upToSurface && !SelectFaceByIndexAppend(modelDoc, upToFaceIndex, 1))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FACE_NOT_FOUND",
                            $"up_to_face_index {upToFaceIndex} could not be selected. Call analyze_model(faces) for valid indices.");

                    // Vendor FeatureCut4 remarks: default cut direction is opposite the sketch
                    // normal; Dir=true reverses it. The graph contract is shared with boss:
                    // reverse=false means +sketch normal, hence cut Dir=!reverse.
                    // Strict directed extrusions must never silently reverse on a null result.
                    // Preserve legacy sheet-metal/through-all behavior outside that contract.
                    bool isSheetMetal = (flatPat != null);
                    bool normalCut = isSheetMetal;
                    bool dir = isSheetMetal && !strictDirection ? reverse : !reverse;
                    feature = FeatureCutOnce(featureMgr, dir, endCond, depthVal, normalCut);
                    if (feature == null && !strictDirection)
                    {
                        // A null cut is most often the WRONG direction: the default Dir sent the cut AWAY
                        // from material so nothing was removed. This happens whenever the sketch sits on a
                        // surface whose outward normal points into free space — an offset reference plane
                        // tangent to the body (e.g. a keyway plane) or a part FACE both behave like ADR-019's
                        // face cut, inverting the boundary-plane default. Retry the flipped direction before
                        // failing, for ANY body type (this generalizes the former sheet-metal-only retry;
                        // for sheet metal the flipped through-all also matters per the sketched face). The
                        // caller's explicit `reverse` is still tried FIRST — we only flip to recover a null,
                        // never to override intent. (KNOWN-LIMITATIONS #13 direction note.)
                        modelDoc.ClearSelection2(true);
                        if (!string.IsNullOrEmpty(activeProfileSketchName))
                            modelDoc.Extension.SelectByID2(activeProfileSketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                        if (upToSurface && !SelectFaceByIndexAppend(modelDoc, upToFaceIndex, 1))
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FACE_NOT_FOUND",
                                $"up_to_face_index {upToFaceIndex} could not be re-selected for the flipped-direction retry.");
                        feature = FeatureCutOnce(featureMgr, !dir, endCond, depthVal, normalCut);
                        directionFlipped = feature != null;
                        ExecLog.Write($"cut: flipped-direction retry (sheetMetal={isSheetMetal}) → feature={(feature != null)}");
                    }

                    // Restore the rollback bar to the end so Flat-Pattern (and its sub-features) re-apply.
                    if (rolledBack)
                    {
                        try { modelDoc.FeatureManager.EditRollback((int)swMoveRollbackBarTo_e.swMoveRollbackBarToEnd, ""); }
                        catch { }
                    }
                }
                else
                {
                    // Re-select the profile sketch by name (see cut branch — needed for FACE sketches,
                    // harmless for plane sketches).
                    modelDoc.ClearSelection2(true);
                    if (!string.IsNullOrEmpty(activeProfileSketchName))
                        modelDoc.Extension.SelectByID2(activeProfileSketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                    if (upToSurface && !SelectFaceByIndexAppend(modelDoc, upToFaceIndex, 1))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FACE_NOT_FOUND",
                            $"up_to_face_index {upToFaceIndex} could not be selected. Call analyze_model(faces) for valid indices.");

                    feature = featureMgr.FeatureExtrusion3(
                        true, false, reverse,
                        endCond, 0,
                        depthVal, 0,
                        false, false, false, false,
                        0, 0,
                        false, false, false, false,
                        true, true, true,
                        (int)swStartConditions_e.swStartSketchPlane, 0,
                        false) as IFeature;
                }

                if (feature == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "EXTRUSION_FAILED", $"Feature creation returned null for feature_type '{featureType}'. Check the profile is a closed/valid sketch, the cut direction hits existing material, and (cut) a solid body exists.");

                // ---- SWEPT CUT verification: tree entry + volume DECREASE ----
                // A non-null Feature is not persistence (create_shell and add_cosmetic_thread both learned
                // that), and for a cut the decisive fact is that material went away: InsertCutSwept5 happily
                // produces a feature that sweeps through air. The delta is measured on the UNROUNDED volume
                // because BuildBodySummary's "volume" echo is R6 — Math.Round(v, 6) of a m3 value, so a
                // 1e-6 m3 (1000 mm3) granularity. Measured on an 80x80x10 plate (6.4000e-5 m3): a d4 circle
                // swept 30 mm removes 3.77e-7 m3 (377 mm3), and BOTH sides of that echo round to 0.000064 —
                // the cut would be invisible on the rounded read, so it is asserted on RawVolume instead.
                JObject sweepCutReport = null;
                if (featureType == "sweep" && isCut)
                {
                    try { modelDoc.EditRebuild3(); } catch { }

                    string cutName = null;
                    try { cutName = feature.Name; } catch { }
                    var treeNames = new List<string>();
                    var inTree = string.IsNullOrEmpty(cutName) ? null : FindFeatureByName(modelDoc, cutName, treeNames);
                    if (inTree == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SWEEP_CUT_NOT_PERSISTED",
                            $"InsertCutSwept5 returned a Feature{(string.IsNullOrEmpty(cutName) ? "" : " named '" + cutName + "'")} " +
                            $"but after the rebuild it is not in the feature tree (walked {treeNames.Count} entries), " +
                            "so the swept cut is not verified and is not reported as done.");

                    string cutType = null;
                    try { cutType = inTree.GetTypeName2(); } catch { }

                    if (sweepCutVolumeBefore == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SWEEP_CUT_VOLUME_UNREADABLE",
                            $"'{cutName}' is in the tree, but the body volume could not be read BEFORE the call, " +
                            "so the removal cannot be confirmed. A cut whose volume did not move is not a cut.");

                    double? volAfter = RawVolume(modelDoc);
                    if (volAfter == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SWEEP_CUT_VOLUME_UNREADABLE",
                            $"'{cutName}' is in the tree, but the body volume could not be read after the " +
                            "rebuild, so the removal cannot be confirmed.");

                    double dVol = volAfter.Value - sweepCutVolumeBefore.Value;
                    if (dVol >= -1e-15)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "SWEEP_CUT_VOLUME_NOT_REDUCED",
                            $"The swept cut '{cutName}' is in the tree but the body volume did not decrease: " +
                            $"{sweepCutVolumeBefore.Value:E6} m3 -> {volAfter.Value:E6} m3 (dV = {dVol:E6}). A cut " +
                            "sweep that removes nothing is a profile swept through air or a path that never " +
                            "crosses the material. The feature is left in the (unsaved) document; discard the " +
                            "document rather than build on it.");

                    TopologyCounts(modelDoc, out int facesAfter, out int edgesAfter, out int vertsAfter);
                    sweepCutReport = new JObject
                    {
                        ["swept_cut"] = cutName,
                        ["swept_cut_type"] = cutType ?? "",
                        ["is_cut"] = true,
                        ["profile_sketch"] = activeProfileSketchName ?? "",
                        ["path_sketch"] = p?.Value<string>("path_sketch") ?? "",
                        ["volume_before"] = sweepCutVolumeBefore.Value,
                        ["volume_after"] = volAfter.Value,
                        ["volume_delta"] = dVol,
                        ["volume_direction"] = "decrease",
                        ["faces_before"] = sweepCutFacesBefore,
                        ["edges_before"] = sweepCutEdgesBefore,
                        ["vertices_before"] = sweepCutVertsBefore,
                        ["faces"] = facesAfter,
                        ["edges"] = edgesAfter,
                        ["vertices"] = vertsAfter
                    };
                    ExecLog.Write($"sweep cut: '{cutName}' ({cutType}) dV={dVol:E3} faces {sweepCutFacesBefore}->{facesAfter}");
                }

                var geom = BuildBodySummary(modelDoc);
                if (directionFlipped)
                {
                    if (geom == null) geom = new JObject();
                    geom["direction_flipped"] = true;
                }
                if (sweepCutReport != null)
                {
                    if (geom == null) geom = new JObject();
                    foreach (JProperty pr in sweepCutReport.Properties()) geom[pr.Name] = pr.Value;
                }

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { feature.Name },
                        Dimensions = new List<string>()
                    },
                    // In-band body summary so the caller can verify the step without a separate analyze.
                    ResultGeometry = geom,
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        public ExecutionResponse CreateRib(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                double? thickness = p?.Value<double?>("thickness");
                if (thickness == null || thickness.Value <= 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "thickness (> 0 meters) is required.");
                bool twoSided = p?.Value<bool?>("two_sided") ?? true;
                bool reverseThicknessDir = p?.Value<bool?>("reverse_thickness_dir") ?? false;
                bool reverseMaterialDir = p?.Value<bool?>("reverse_material_dir") ?? false;
                bool isNormToSketch = p?.Value<bool?>("is_norm_to_sketch") ?? false;

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                // The ACTIVE sketch is the rib profile (an OPEN sketch — typically a single line;
                // SolidWorks extends it to the surrounding walls). Same consume-the-active-sketch
                // contract as extrude_feature.
                var activeSketchFeat = modelDoc.SketchManager.ActiveSketch as IFeature;
                if (activeSketchFeat == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_NOT_ACTIVE", "No active sketch — the rib profile sketch must be active. Call create_sketch + add_sketch_entity first.");
                string profileName = activeSketchFeat.Name;

                // Snapshot the last feature so the void-returning InsertRib can be verified from
                // the tree afterwards (no return value to check — unlike FeatureExtrusion3).
                string lastBefore = LastFeatureName(modelDoc);

                modelDoc.SketchManager.InsertSketch(true);  // exit the sketch
                modelDoc.ClearSelection2(true);
                modelDoc.Extension.SelectByID2(profileName, "SKETCH", 0, 0, 0, false, 0, null, 0);

                // InsertRib(Is2Sided, ReverseThicknessDir, Thickness, ReferenceEdgeIndex,
                //           ReverseMaterialDir, IsDrafted, DraftOutward, DraftAngle,
                //           IsNormToSketch, IsDraftedFromWall) — signature + parameter semantics
                // verified against the LOCAL sldworksapi.chm + interop reflection (ADR-035).
                // Draft deliberately not exposed in v1 (2-1's rib is undrafted; grow on demand).
                modelDoc.FeatureManager.InsertRib(
                    twoSided, reverseThicknessDir, thickness.Value, 0,
                    reverseMaterialDir, false, false, 0.0,
                    isNormToSketch, false);
                modelDoc.EditRebuild3();

                string lastAfter = LastFeatureName(modelDoc);
                if (lastAfter == lastBefore)
                {
                    // Most likely the WRONG material direction (nothing to fill on that side) —
                    // retry flipped, mirroring the cut path's flipped-direction recovery. The
                    // caller's explicit reverse_material_dir is still tried FIRST.
                    modelDoc.ClearSelection2(true);
                    modelDoc.Extension.SelectByID2(profileName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                    modelDoc.FeatureManager.InsertRib(
                        twoSided, reverseThicknessDir, thickness.Value, 0,
                        !reverseMaterialDir, false, false, 0.0,
                        isNormToSketch, false);
                    modelDoc.EditRebuild3();
                    lastAfter = LastFeatureName(modelDoc);
                    ExecLog.Write($"create_rib: flipped-material retry → created={(lastAfter != lastBefore)}");
                }
                if (lastAfter == lastBefore)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "RIB_FAILED",
                        "InsertRib created no feature (tried both material directions). Check the profile sketch is open geometry whose extensions hit existing walls, and thickness is sensible.");

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { lastAfter },
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = BuildBodySummary(modelDoc),
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        public ExecutionResponse CreateShell(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                double? thickness = p?.Value<double?>("thickness");
                if (thickness == null || thickness.Value <= 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "thickness (> 0 meters) is required.");
                bool outward = p?.Value<bool?>("outward") ?? false;

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
                if (!(modelDoc is IPartDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "WRONG_DOCUMENT_TYPE", "create_shell hollows a solid BODY — the active document must be a part.");

                // The shell works on the solid, so any open sketch is exited first (same as
                // add_edge_feature): a sketch in edit mode would capture the face selection.
                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                // face_indices: a JSON-array STRING of indices into the SAME
                // GetBodies2(swSolidBody,true) → GetFaces() enumeration analyze_model(faces) reports
                // and create_sketch(on_face, face_index) walks — the add_edge_feature/edge_indices
                // idiom (ADR-022). Those faces are the ones LEFT OPEN. Absent or empty = a CLOSED
                // shell: InsertFeatureShell with nothing selected hollows the body keeping every face.
                string faceIndicesJson = p?.Value<string>("face_indices");
                JArray indexArray = null;
                if (!string.IsNullOrEmpty(faceIndicesJson))
                {
                    try { indexArray = JArray.Parse(faceIndicesJson); }
                    catch
                    {
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "INVALID_PARAMETER", "face_indices must be a valid JSON array of integers, e.g. \"[3]\".");
                    }
                }

                modelDoc.ClearSelection2(true);
                int removedCount = 0;
                if (indexArray != null)
                {
                    for (int i = 0; i < indexArray.Count; i++)
                    {
                        int fi = indexArray[i].Value<int>();
                        // Mark 0 (plain selection): the shell's faces carry no end-condition
                        // semantics, unlike the Mark 1 up-to-surface reference FeatureExtrusion3 wants.
                        if (fi < 0 || !SelectFaceByIndexAppend(modelDoc, fi, 0))
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FACE_NOT_FOUND",
                                $"Face index {fi} could not be selected on the live body (face #{i} of face_indices). Call analyze_model(faces) for valid indices.");
                        removedCount++;
                    }
                }

                // IModelDoc2.InsertFeatureShell(Thickness, Outward) is VOID — and there is no
                // shell method on IFeatureManager at all (reflected off the LOCAL
                // SolidWorks.Interop.sldworks.dll: only GetPlasticsShellType exists there), so there
                // is no return value to test. Creation is therefore confirmed from the feature tree,
                // exactly like create_rib confirms InsertRib.
                string lastBefore = LastFeatureName(modelDoc);
                // RAW volumes for the delta: BuildBodySummary's "volume" is R6-rounded (1e-6 m3),
                // which is coarser than the 1e-12 threshold below and would call a successful shell
                // of a small or thin part (dV < ~5e-7 m3) SHELL_FAILED. The R6 echo stays in
                // result_geometry.
                double? volBefore = RawVolume(modelDoc);

                modelDoc.InsertFeatureShell(thickness.Value, outward);
                modelDoc.EditRebuild3();

                string lastAfter = LastFeatureName(modelDoc);
                double? volAfter = RawVolume(modelDoc);
                if (lastAfter == lastBefore)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SHELL_FAILED",
                        $"InsertFeatureShell created no feature at thickness {thickness.Value} m " +
                        $"({removedCount} face(s) to remove, outward={outward}). Usually the wall is " +
                        "thicker than the thinnest section of the body, or a selected face cannot be removed.");
                if (volBefore != null && volAfter != null && Math.Abs(volAfter.Value - volBefore.Value) < 1e-12)
                    // SolidWorks can append an errored feature to the tree and leave the body alone,
                    // so a tree diff alone would report success on a shell that hollowed nothing.
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SHELL_FAILED",
                        $"InsertFeatureShell added '{lastAfter}' but the body volume is unchanged " +
                        $"({volBefore.Value:R} m3) — the shell feature is in error at thickness " +
                        $"{thickness.Value} m; reduce it below the thinnest wall of the body.");

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { lastAfter },
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = BuildBodySummary(modelDoc),
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        public ExecutionResponse AddEdgeFeature(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                string featureType = p?.Value<string>("feature_type")?.ToLowerInvariant();
                if (string.IsNullOrEmpty(featureType) || (featureType != "fillet" && featureType != "chamfer"))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "feature_type is required: 'fillet' or 'chamfer'.");

                double? radiusOrDist = p?.Value<double?>("radius_or_distance");
                if (radiusOrDist == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "radius_or_distance is required.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                // Ensure we are NOT in sketch mode (edge features work on solid body)
                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                modelDoc.ClearSelection2(true);

                // Selection priority: edge_indices (robust) → edges_json (coords) → ex/ey/ez (single coord).
                // edge_indices is a JSON array of integers matching the `i` field from analyze_model(edges).
                // It reuses the EXACT same enumeration (GetBodies2(swSolidBody,true) → GetEdges()), so the
                // index is stable and selection bypasses the coordinate pick tolerance entirely — the fix for
                // crowded / concave (inner-corner, small-radius) edges that SelectByID2 can't disambiguate
                // (KNOWN-LIMITATIONS #6).
                string edgeIndicesJson = p?.Value<string>("edge_indices");
                JArray indexArray = null;
                if (!string.IsNullOrEmpty(edgeIndicesJson))
                {
                    try { indexArray = JArray.Parse(edgeIndicesJson); }
                    catch { return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), "INVALID_PARAMETER", "edge_indices must be a valid JSON array of integers, e.g. \"[3,5]\"."); }
                }

                // Support single edge via ex/ey/ez or multiple via edges_json array
                string edgesJson = p?.Value<string>("edges_json");
                JArray edgeArray = null;
                if (!string.IsNullOrEmpty(edgesJson))
                {
                    try { edgeArray = JArray.Parse(edgesJson); }
                    catch { return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), "INVALID_PARAMETER", "edges_json must be a valid JSON array of {ex,ey,ez} objects."); }
                }

                if (indexArray != null && indexArray.Count > 0)
                {
                    // Flatten all solid-body edges in the SAME order analyze_model(edges) reports, then select
                    // the i-th IEdge directly (no coordinate pick).
                    var partDoc = modelDoc as IPartDoc;
                    var allEdges = new List<IEdge>();
                    object[] bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
                    if (bodies != null)
                        foreach (var b in bodies)
                        {
                            var body = b as IBody2;
                            if (body == null) continue;
                            object[] es = body.GetEdges() as object[];
                            if (es == null) continue;
                            foreach (var e in es) { var ed = e as IEdge; if (ed != null) allEdges.Add(ed); }
                        }
                    for (int i = 0; i < indexArray.Count; i++)
                    {
                        int ei = indexArray[i].Value<int>();
                        if (ei < 0 || ei >= allEdges.Count)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "EDGE_NOT_FOUND", $"Edge index {ei} out of range (part has {allEdges.Count} edges). Call analyze_model(edges) for valid indices.");
                        var edgeEnt = allEdges[ei] as IEntity;
                        bool sel = edgeEnt != null && edgeEnt.Select4(i > 0, null);
                        if (!sel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "EDGE_NOT_FOUND", $"Failed to select edge index {ei}.");
                    }
                }
                else if (edgeArray != null && edgeArray.Count > 0)
                {
                    for (int i = 0; i < edgeArray.Count; i++)
                    {
                        double ex = edgeArray[i].Value<double>("ex");
                        double ey = edgeArray[i].Value<double>("ey");
                        double ez = edgeArray[i].Value<double>("ez");
                        bool sel = modelDoc.Extension.SelectByID2("", "EDGE", ex, ey, ez, i > 0, 0, null, 0);
                        if (!sel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "EDGE_NOT_FOUND", $"No edge found at or near ({ex}, {ey}, {ez}) (edge index {i}).");
                    }
                }
                else
                {
                    double? ex = p?.Value<double?>("ex");
                    double? ey = p?.Value<double?>("ey");
                    double? ez = p?.Value<double?>("ez");
                    if (ex == null || ey == null || ez == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER", "ex, ey, ez are required (3D coordinates of a point on the target edge), or provide edges_json for multiple edges.");
                    bool sel = modelDoc.Extension.SelectByID2("", "EDGE", ex.Value, ey.Value, ez.Value, false, 0, null, 0);
                    if (!sel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "EDGE_NOT_FOUND", $"No edge found at or near ({ex.Value}, {ey.Value}, {ez.Value}).");
                }

                IFeature feature;
                if (featureType == "fillet")
                {
                    // Options must include swFeatureFilletUniformRadius (2) for a constant-radius fillet;
                    // Propagate (1) alone left R1 unapplied and FeatureFillet3 returned null on SW2026.
                    int filletOpts = (int)(swFeatureFilletOptions_e.swFeatureFilletPropagate
                                         | swFeatureFilletOptions_e.swFeatureFilletUniformRadius);
                    feature = modelDoc.FeatureManager.FeatureFillet3(
                        filletOpts,
                        radiusOrDist.Value,   // R1
                        0.0,                  // R2
                        0.0,                  // Rho
                        (int)swFeatureFilletType_e.swFeatureFilletType_Simple,
                        0,                    // OverflowType
                        0,                    // ConicRhoType
                        null, null, null,     // Radii, Dist2Arr, RhoArr
                        null, null, null, null // SetBackDistances, PointRadiusArray, PointDist2Array, PointRhoArray
                    ) as IFeature;
                    ExecLog.Write($"add_edge_feature fillet: opts={filletOpts} r={radiusOrDist.Value} -> {(feature == null ? "NULL" : feature.Name)}");
                }
                else // chamfer
                {
                    // Two chamfer modes (grown for level-2-2: Chamfer1 dist-angle @45°, Chamfer2 dist-dist 3×5).
                    // InsertFeatureChamfer(Options, ChamferType, Width, Angle, OtherDist, VC1, VC2, VC3):
                    //   Width = D1 (first-face setback), Angle = radians (AngleDistance), OtherDist = D2
                    //   (second-face setback, DistanceDistance). Enum values are compile-time constants,
                    //   inlined by the C# compiler -> no runtime swconst load (ADR-018):
                    //   swChamferAngleDistance=1, swChamferDistanceDistance=2, swFeatureChamferFlipDirection=1.
                    // A DistanceDistance chamfer is directional (which face gets D1 vs D2) — a wrong side is
                    // corrected by chamfer_flip (the FlipDirection option), same idea as rib/extrude reverse.
                    string chamferType = (p?.Value<string>("chamfer_type") ?? "distance_angle").ToLowerInvariant();
                    bool chamferFlip = p?.Value<bool?>("chamfer_flip") ?? false;
                    int chamferOpts = chamferFlip ? (int)swFeatureChamferOption_e.swFeatureChamferFlipDirection : 0;
                    if (chamferType == "distance_distance")
                    {
                        double? dist2 = p?.Value<double?>("distance2");
                        if (dist2 == null || dist2.Value <= 0)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "MISSING_PARAMETER", "chamfer_type 'distance_distance' requires distance2 > 0 (the second-face setback, meters).");
                        feature = modelDoc.FeatureManager.InsertFeatureChamfer(
                            chamferOpts,
                            (int)swChamferType_e.swChamferDistanceDistance,
                            radiusOrDist.Value,                          // D1 (Width — first-face setback)
                            0.0,                                         // Angle unused
                            dist2.Value,                                 // D2 (OtherDist — second-face setback)
                            0.0, 0.0, 0.0) as IFeature;
                        ExecLog.Write($"add_edge_feature chamfer(dist-dist): d1={radiusOrDist.Value} d2={dist2.Value} flip={chamferFlip} -> {(feature == null ? "NULL" : feature.Name)}");
                    }
                    else // distance_angle (default; angle in DEGREES at the tool boundary, default 45)
                    {
                        double angleDeg = p?.Value<double?>("angle") ?? 45.0;
                        double chamferAngle = angleDeg * Math.PI / 180.0;
                        feature = modelDoc.FeatureManager.InsertFeatureChamfer(
                            chamferOpts,
                            (int)swChamferType_e.swChamferAngleDistance,
                            radiusOrDist.Value,                          // Width (setback distance, D1)
                            chamferAngle,                                // angle in radians
                            0.0, 0.0, 0.0, 0.0) as IFeature;
                        ExecLog.Write($"add_edge_feature chamfer(dist-angle): dist={radiusOrDist.Value} angle={angleDeg} flip={chamferFlip} -> {(feature == null ? "NULL" : feature.Name)}");
                    }
                }

                if (feature == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "FEATURE_CREATION_FAILED", $"add_edge_feature '{featureType}' returned null. Ensure the selected edge(s) belong to an existing solid body.");

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { feature.Name },
                        Dimensions = new List<string>()
                    },
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        // -----------------------------------------------------------------------
        // M19 — add_reference_geometry
        // -----------------------------------------------------------------------
        public ExecutionResponse AddReferenceGeometry(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                string geoType = (p?.Value<string>("type") ?? "").ToLowerInvariant();

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var featureMgr = modelDoc.FeatureManager;
                IFeature feature = null;

                if (geoType == "plane")
                {
                    string refPlaneName = p?.Value<string>("ref_plane_name");
                    double offset = p?.Value<double?>("offset") ?? 0.0;
                    if (string.IsNullOrEmpty(refPlaneName))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER",
                            "plane requires ref_plane_name (e.g. 'Front Plane') and offset (distance in meters).");

                    modelDoc.ClearSelection2(true);
                    bool sel = SelectPlaneFlexible(modelDoc, refPlaneName, false, 0); // language-independent (ADR-007)
                    if (!sel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "PLANE_NOT_FOUND", $"Plane '{refPlaneName}' not found.");

                    // InsertRefPlane's Distance is UNSIGNED — a negative value is silently NOT
                    // applied (found live on 2-1: a -0.007 'offset plane' landed ON its parent at
                    // y=0; the earlier 1-2 'proof' was masked by through-all symmetry). The minus
                    // side needs the OptionFlip constraint bit + the absolute distance.
                    int planeConstraint = (int)swRefPlaneReferenceConstraints_e.swRefPlaneReferenceConstraint_Distance;
                    if (offset < 0)
                        planeConstraint |= (int)swRefPlaneReferenceConstraints_e.swRefPlaneReferenceConstraint_OptionFlip;
                    feature = featureMgr.InsertRefPlane(
                        planeConstraint, Math.Abs(offset), 0, 0, 0, 0) as IFeature;
                }
                else if (geoType == "axis")
                {
                    string entity1Name = p?.Value<string>("entity1_name");
                    string entity1Type = (p?.Value<string>("entity1_type") ?? "PLANE").ToUpperInvariant();
                    string entity2Name = p?.Value<string>("entity2_name");
                    string entity2Type = (p?.Value<string>("entity2_type") ?? "PLANE").ToUpperInvariant();

                    if (string.IsNullOrEmpty(entity1Name) || string.IsNullOrEmpty(entity2Name))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER",
                            "axis requires entity1_name and entity2_name (e.g. 'Top Plane' and 'Right Plane').");

                    modelDoc.ClearSelection2(true);
                    // For PLANE-type entities use the language-independent resolver (ADR-007);
                    // other types (EDGE, etc.) keep exact-name selection.
                    bool sel1 = entity1Type == "PLANE"
                        ? SelectPlaneFlexible(modelDoc, entity1Name, false, 1)
                        : modelDoc.Extension.SelectByID2(entity1Name, entity1Type, 0, 0, 0, false, 1, null, 0);
                    if (!sel1)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "ENTITY_NOT_FOUND",
                            $"Entity '{entity1Name}' (type '{entity1Type}') not found.");

                    bool sel2 = entity2Type == "PLANE"
                        ? SelectPlaneFlexible(modelDoc, entity2Name, true, 2)
                        : modelDoc.Extension.SelectByID2(entity2Name, entity2Type, 0, 0, 0, true, 2, null, 0);
                    if (!sel2)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "ENTITY_NOT_FOUND",
                            $"Entity '{entity2Name}' (type '{entity2Type}') not found.");

                    bool axisInserted = modelDoc.InsertAxis2(false);
                    if (!axisInserted)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "REFERENCE_GEOMETRY_FAILED",
                            "InsertAxis2 failed. Verify the two selected planes/edges are valid.");
                    // Get the last inserted feature (the reference axis just created)
                    object[] allFeaturesAxis = modelDoc.FeatureManager.GetFeatures(true) as object[];
                    feature = allFeaturesAxis != null && allFeaturesAxis.Length > 0
                        ? allFeaturesAxis[allFeaturesAxis.Length - 1] as IFeature
                        : null;
                }
                else if (geoType == "point")
                {
                    double px = p?.Value<double?>("px") ?? 0.0;
                    double py = p?.Value<double?>("py") ?? 0.0;
                    double pz = p?.Value<double?>("pz") ?? 0.0;

                    modelDoc.ClearSelection2(true);
                    bool sel = modelDoc.Extension.SelectByID2("", "VERTEX", px, py, pz, false, 0, null, 0);
                    if (!sel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "VERTEX_NOT_FOUND",
                            $"No vertex found at ({px}, {py}, {pz}). Coordinates must match an existing vertex exactly.");

                    feature = featureMgr.InsertReferencePoint(
                        (int)swRefPointType_e.swRefPointIntersection, 0, 0.0, 1) as IFeature;
                }
                else
                {
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "UNSUPPORTED_GEOMETRY_TYPE",
                        $"type '{geoType}' is not supported. Supported: plane, axis, point.");
                }

                if (feature == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "REFERENCE_GEOMETRY_FAILED",
                        $"Failed to create reference {geoType}. Verify the referenced entities are valid and the document has no active sketch.");

                // Hide the created plane/axis immediately (user rule 2026-07-05): reference geometry
                // is scaffolding for downstream features (offset sketches, pattern axes) and must not
                // clutter the modeling view. Selection by NAME still works on hidden ref geometry
                // (SelectByID2 / SelectPlaneFlexible), so downstream consumers are unaffected.
                // Best-effort: a hide failure never fails the create.
                if (geoType == "plane" || geoType == "axis")
                {
                    try
                    {
                        modelDoc.ClearSelection2(true);
                        if (feature.Select2(false, 0))
                            modelDoc.BlankRefGeom();
                        modelDoc.ClearSelection2(true);
                    }
                    catch { }
                }

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { feature.Name },
                        Dimensions = new List<string>()
                    },
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        // -----------------------------------------------------------------------
        // M20 — create_pattern
        // -----------------------------------------------------------------------
        public ExecutionResponse CreatePattern(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                string patternType = (p?.Value<string>("pattern_type") ?? "").ToLowerInvariant();
                string featureName = p?.Value<string>("feature_name");

                if (patternType != "linear" && patternType != "circular" && patternType != "mirror")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "pattern_type is required: 'linear', 'circular', or 'mirror'.");
                if (string.IsNullOrEmpty(featureName) && patternType != "mirror")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "feature_name is required (name of the feature to pattern, e.g. 'Boss-Extrude1').");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                var featureMgr = modelDoc.FeatureManager;
                IFeature feature = null;

                if (patternType == "linear")
                {
                    string direction = (p?.Value<string>("direction") ?? "X").ToUpperInvariant();
                    double spacing = p?.Value<double?>("spacing") ?? 0.01;
                    int count = p?.Value<int?>("count") ?? 2;
                    int count2 = p?.Value<int?>("count2") ?? 1;
                    double spacing2 = p?.Value<double?>("spacing2") ?? 0.01;
                    bool flip = p?.Value<bool?>("flip") ?? false;

                    // Select seed feature with mark=4
                    modelDoc.ClearSelection2(true);
                    bool featSel = modelDoc.Extension.SelectByID2(featureName, "BODYFEATURE", 0, 0, 0, false, 4, null, 0);
                    if (!featSel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FEATURE_NOT_FOUND",
                            $"Feature '{featureName}' not found. Ensure the name matches the feature tree entry exactly.");

                    // Select direction entity with mark=1:
                    // X→Right Plane (normal=X), Y→Top Plane (normal=Y), Z→Front Plane (normal=Z)
                    string dirPlane;
                    switch (direction)
                    {
                        case "Y": dirPlane = "Top Plane";   break;
                        case "Z": dirPlane = "Front Plane"; break;
                        default:  dirPlane = "Right Plane"; break; // X
                    }
                    // append=true keeps the seed feature (mark=4) selected; language-independent (ADR-007)
                    bool dirSel = SelectPlaneFlexible(modelDoc, dirPlane, true, 1);
                    if (!dirSel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DIRECTION_NOT_FOUND",
                            $"Direction plane '{dirPlane}' not found. Ensure default planes exist in the document.");

                    // FeatureLinearPattern3(Num1, Spacing1, Num2, Spacing2, FlipDir1, FlipDir2,
                    // DName1, DName2, GeometryPattern, VaryInstance) — reflection-verified
                    // (ADR-035). FlipDir1 exposed as 'flip' (v0.7.0): pattern toward the NEGATIVE
                    // axis. FlipDir2 stays false — the D2 direction entity is not wired (count2
                    // is accepted but has no direction-2 selection; see the contract note).
                    feature = featureMgr.FeatureLinearPattern3(
                        count, spacing,
                        count2, spacing2,
                        flip, false,
                        null, null,
                        false, false) as IFeature; // geometryPattern=false (SW default). NOTE: disjoint instances warn & are skipped — see KNOWN-LIMITATIONS.md; geometryPattern=true returns null here, not a safe blanket default.
                }
                else if (patternType == "mirror")
                {
                    // Mirror pattern (grown for 4-1's Mirror1): mirrors FEATURES about a plane.
                    // InsertMirrorFeature2 selection marks read from the local CHM (ADR-035):
                    // features to mirror = mark 1, mirror plane / planar face = mark 2.
                    string planeName = p?.Value<string>("plane");
                    if (string.IsNullOrEmpty(planeName)) planeName = "Right Plane";
                    bool geometryPattern = p?.Value<bool?>("geometry_pattern") ?? false;

                    var mirrorNames = new List<string>();
                    string featuresJson = p?.Value<string>("features_json");
                    if (!string.IsNullOrEmpty(featuresJson) && featuresJson != "[]")
                    {
                        try
                        {
                            var arr = JArray.Parse(featuresJson);
                            foreach (var t in arr) mirrorNames.Add(t.ToString());
                        }
                        catch (Exception ex)
                        {
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "INVALID_PARAMETER",
                                $"features_json must be a JSON array of feature tree names: {ex.Message}");
                        }
                    }
                    else if (!string.IsNullOrEmpty(featureName))
                        mirrorNames.Add(featureName);
                    if (mirrorNames.Count == 0)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER",
                            "mirror requires features_json (JSON array of feature tree names, e.g. '[\"Edge-Flange1\",\"Sketched Bend2\"]') or feature_name.");

                    // Sheet-metal parts keep a (suppressed) Flat-Pattern as the LAST feature with the
                    // rollback bar AFTER it; the generic InsertMirrorFeature2 would insert after
                    // Flat-Pattern (illegal → null; live-caught on the scratch test — the sheet-metal-
                    // aware bend API handles this itself). Same fix as the sheet-metal cut (ADR-026):
                    // roll the bar before Flat-Pattern, insert, restore.
                    IFeature mirrorFlatPat = FindFlatPattern(modelDoc);
                    bool mirrorRolled = false;
                    if (mirrorFlatPat != null)
                    {
                        try
                        {
                            mirrorRolled = modelDoc.FeatureManager.EditRollback(
                                (int)swMoveRollbackBarTo_e.swMoveRollbackBarToBeforeFeature, mirrorFlatPat.Name);
                        }
                        catch { mirrorRolled = false; }
                    }
                    try
                    {
                        modelDoc.ClearSelection2(true);
                        foreach (var nm in mirrorNames)
                        {
                            bool sel = modelDoc.Extension.SelectByID2(nm, "BODYFEATURE", 0, 0, 0, true, 1, null, 0);
                            if (!sel)
                            {
                                // Fallback for feature types BODYFEATURE won't match: the same tree walk
                                // analyze uses + IFeature.Select2 with the mark.
                                var mf = FindFeatureByName(modelDoc, nm, null);
                                sel = mf != null && mf.Select2(true, 1);
                            }
                            if (!sel)
                                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                    "FEATURE_NOT_FOUND",
                                    $"Feature '{nm}' not found for mirror. Names must match the feature tree exactly.");
                        }

                        if (!SelectPlaneFlexible(modelDoc, planeName, true, 2))
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "PLANE_NOT_FOUND",
                                $"Mirror plane '{planeName}' not found. Use a default plane name or a created reference plane's name.");

                        feature = featureMgr.InsertMirrorFeature2(false, geometryPattern, false, false, 0) as IFeature;
                        ExecLog.Write($"mirror: features=[{string.Join(",", mirrorNames)}] plane={planeName} rolled={mirrorRolled} -> {(feature == null ? "NULL" : feature.Name)}");
                    }
                    finally
                    {
                        if (mirrorRolled)
                        {
                            try { modelDoc.FeatureManager.EditRollback((int)swMoveRollbackBarTo_e.swMoveRollbackBarToEnd, ""); }
                            catch { }
                        }
                    }
                }
                else // circular
                {
                    string axisName = p?.Value<string>("axis_name");
                    int count = p?.Value<int?>("count") ?? 4;
                    double angleDeg = p?.Value<double?>("angle") ?? 90.0;
                    double angleRad = angleDeg * Math.PI / 180.0;
                    // equal_spacing=true (default): angle = TOTAL angle spread across all instances (pass
                    // 360 for a full ring). equal_spacing=false: angle = the angle BETWEEN adjacent
                    // instances (matches a model authored that way; can exceed 360 and wrap/overlap).
                    bool equalSpacing = p?.Value<bool?>("equal_spacing") ?? true;

                    if (string.IsNullOrEmpty(axisName))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER",
                            "circular pattern requires axis_name (name of the rotation axis, e.g. 'Axis1'). Create one first with add_reference_geometry.");

                    // Select seed feature with mark=4
                    modelDoc.ClearSelection2(true);
                    bool featSel = modelDoc.Extension.SelectByID2(featureName, "BODYFEATURE", 0, 0, 0, false, 4, null, 0);
                    if (!featSel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FEATURE_NOT_FOUND",
                            $"Feature '{featureName}' not found.");

                    // Select axis with mark=1 — try AXIS type first, then REFERENCECURVES
                    bool axisSel = modelDoc.Extension.SelectByID2(axisName, "AXIS", 0, 0, 0, true, 1, null, 0);
                    if (!axisSel)
                        axisSel = modelDoc.Extension.SelectByID2(axisName, "REFERENCECURVES", 0, 0, 0, true, 1, null, 0);
                    if (!axisSel)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "AXIS_NOT_FOUND",
                            $"Axis '{axisName}' not found. Create a reference axis with add_reference_geometry first.");

                    feature = featureMgr.FeatureCircularPattern4(
                        count, angleRad,
                        false,
                        null,
                        false, equalSpacing, false) as IFeature; // geometryPattern=false (SW default); see KNOWN-LIMITATIONS.md re: disjoint instances
                }

                if (feature == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "PATTERN_FAILED",
                        $"Pattern feature creation returned null. Verify seed feature and direction/axis are valid.");

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string> { feature.Name },
                        Dimensions = new List<string>()
                    },
                    // In-band body summary so the caller can verify the pattern without a separate analyze.
                    ResultGeometry = BuildBodySummary(modelDoc),
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        // -----------------------------------------------------------------------
        // M21 — set_part_material
        // -----------------------------------------------------------------------
        public ExecutionResponse SetPartMaterial(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                string materialName = p?.Value<string>("material_name");
                string library = p?.Value<string>("library") ?? "SolidWorks Materials";

                if (string.IsNullOrEmpty(materialName))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "material_name is required (e.g. 'AISI 1020 Steel (SS)', 'Aluminum 1060 Alloy', 'ABS').");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (!(modelDoc is IPartDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "WRONG_DOCUMENT_TYPE",
                        "set_part_material requires an active part document, not a drawing or assembly.");

                var partDoc = modelDoc as IPartDoc;
                if (partDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DOCUMENT_CAST_FAILED", "Active document is not castable to IPartDoc.");

                // What the part carries BEFORE the attempt. A rejected setter leaves the previous
                // material in place, and a report must never call that "no material" — the old code
                // only asked whether the readback was non-empty, so an already-materialized part
                // could report COMPLETED for a name that was never applied.
                string beforeMaterial, beforeDb;
                ReadPartMaterial(partDoc, out beforeMaterial, out beforeDb);

                // Positive existence check BEFORE applying, so MATERIAL_NOT_FOUND may only ever mean
                // "this name is provably absent from that database".
                MaterialExistence existence = LookUpMaterial(library, materialName, out string lookupDetail);

                if (existence == MaterialExistence.NotFound)
                {
                    // Positively absent: the setter is NOT called at all. SetMaterialPropertyName2
                    // returns void, so letting it run with a name the database does not contain
                    // could change or clear what the part carries while the guard's state_version
                    // stays put — a write the response cannot account for. Nothing is touched here,
                    // and the response reports the material currently on the part.
                    return WithMaterialState(
                        BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MATERIAL_NOT_FOUND",
                            $"Material '{materialName}' is not present in '{library}' "
                            + $"({lookupDetail}). Nothing was applied and the document was not "
                            + "changed."
                            + (string.IsNullOrEmpty(beforeMaterial)
                                ? " The part carries no material."
                                : $" The part still carries '{beforeMaterial}' (library '{beforeDb}').")),
                        modelDoc, beforeMaterial, beforeDb);
                }

                // Present, or unknown-but-worth-attempting: apply, and the readback is the only
                // proof it worked (the setter itself returns nothing).
                partDoc.SetMaterialPropertyName2("", library, materialName);

                string appliedMaterial, appliedDb;
                ReadPartMaterial(partDoc, out appliedMaterial, out appliedDb);
                bool applied = MaterialNameEquals(appliedMaterial, materialName)
                    && LibraryMatches(library, appliedDb);

                if (!applied)
                {
                    string actual = string.IsNullOrEmpty(appliedMaterial)
                        ? "<none>"
                        : $"'{appliedMaterial}' (library '{appliedDb}')";
                    string reason = existence == MaterialExistence.Present
                        ? $"'{materialName}' IS present in '{library}' ({lookupDetail}) but the setter did not apply it."
                        : $"existence of '{materialName}' in '{library}' could not be determined ({lookupDetail}), and the apply did not confirm either way.";
                    // A name the database has, which the setter refused, and a database that could
                    // not be read, are both real failures: MATERIAL_NOT_APPLIED stays fatal and the
                    // only non-fatal code is the positively-absent name handled above.
                    return WithMaterialState(
                        BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MATERIAL_NOT_APPLIED",
                            $"Material '{materialName}' was not applied: {reason} The part now carries {actual}"
                            + (string.IsNullOrEmpty(beforeMaterial)
                                ? ""
                                : $"; it already carried '{beforeMaterial}' before this call.")),
                        modelDoc, appliedMaterial, appliedDb);
                }

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        // The readback, not the request: what is actually on the part.
                        Features = new List<string> { $"material={appliedMaterial}", $"library={appliedDb}" },
                        Dimensions = new List<string>()
                    },
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        // -----------------------------------------------------------------------
        // set_part_material helpers — positive existence, exact readback, honest state
        // -----------------------------------------------------------------------

        private enum MaterialExistence { NotFound, Present, Undeterminable }

        private static void ReadPartMaterial(IPartDoc partDoc, out string name, out string database)
        {
            string db;
            string raw = partDoc.GetMaterialPropertyName2("", out db);
            name = (raw ?? "").Trim();
            database = (db ?? "").Trim();
        }

        // Exact material name, case and surrounding space insensitive — never a substring, so
        // "1060 Alloy" cannot be matched by "1060-H12".
        private static bool MaterialNameEquals(string left, string right)
        {
            return !string.IsNullOrWhiteSpace(left) && !string.IsNullOrWhiteSpace(right)
                && string.Equals(left.Trim(), right.Trim(), StringComparison.OrdinalIgnoreCase);
        }

        private static string NormalizeLibraryName(string value)
        {
            if (string.IsNullOrWhiteSpace(value)) return "";
            // "SolidWorks Materials", "solidworks materials.sldmat" and a full path to that file all
            // name the same database; the display name also appears with an apostrophe.
            string text = Path.GetFileNameWithoutExtension(value.Trim()).Replace("'", "").ToLowerInvariant();
            return new string(text.Where(c => !char.IsWhiteSpace(c) && c != '_' && c != '-').ToArray());
        }

        private static bool LibraryMatches(string requested, string reported)
        {
            string wanted = NormalizeLibraryName(requested);
            return wanted.Length > 0 && wanted == NormalizeLibraryName(reported);
        }

        // Attach the material the part ACTUALLY carries to a response, in the same
        // cadState.features shape the COMPLETED response uses, so a caller never has to guess
        // whether a failed call cleared the property or left an older one on the document.
        private ExecutionResponse WithMaterialState(ExecutionResponse response, IModelDoc2 modelDoc,
            string material, string database)
        {
            response.CadState = new CadState
            {
                StateVersion = response.StateVersion ?? _guard.GetCurrentStateVersion(),
                ActiveDocument = modelDoc.GetTitle(),
                DocumentType = "PART",
                ActiveSketch = null,
                Features = new List<string>
                {
                    $"material={(string.IsNullOrEmpty(material) ? "<none>" : material)}",
                    $"library={(string.IsNullOrEmpty(database) ? "<none>" : database)}"
                },
                Dimensions = new List<string>()
            };
            return response;
        }

        // Search roots for a material database file: the schema path's folder, the data folder, and
        // every language's sldmaterials folder under the install. Anything that could not be
        // enumerated is reported in `problems` — a folder that is provably absent is not a problem,
        // but one that exists and cannot be listed means the search was incomplete, and an
        // incomplete search may never answer "this material is absent".
        //
        // Absence has to be PROVEN, not inferred from a failed probe: `Directory.Exists` answers
        // false for access-denied, IO and path errors exactly as it does for a folder that is not
        // there, so it cannot tell "nothing to look at" from "not allowed to look". Enumerating and
        // reading the exception type is the only distinction that fails closed, and only a proven
        // absence may drop a folder from the search.
        private static bool CanEnumerateDirectory(string dir, List<string> problems)
        {
            if (string.IsNullOrEmpty(dir)) return false;
            try
            {
                Directory.GetFileSystemEntries(dir);
                return true;
            }
            catch (DirectoryNotFoundException) { return false; }
            catch (Exception ex)
            {
                problems.Add($"expected material search folder '{dir}' could not be listed: "
                    + $"{ex.GetType().Name}: {ex.Message}");
                return false;
            }
        }

        private List<string> MaterialSearchDirectories(List<string> problems)
        {
            var dirs = new List<string>();
            try
            {
                string schema = _solidWorks.GetMaterialSchemaPathName();
                if (!string.IsNullOrEmpty(schema))
                {
                    dirs.Add(Path.GetDirectoryName(schema));
                    dirs.Add(Path.Combine(Path.GetDirectoryName(schema), "sldmaterials"));
                }
            }
            catch (Exception ex) { problems.Add($"GetMaterialSchemaPathName failed: {ex.Message}"); }
            try
            {
                string exe = _solidWorks.GetExecutablePath();
                if (!string.IsNullOrEmpty(exe))
                {
                    string install = Path.GetDirectoryName(exe);
                    dirs.Add(Path.Combine(install, "data", "material"));
                    string lang = Path.Combine(install, "lang");
                    try
                    {
                        // Enumerate directly: a `Directory.Exists` gate would swallow the very
                        // access error this has to report.
                        foreach (string language in Directory.GetDirectories(lang))
                            dirs.Add(Path.Combine(language, "sldmaterials"));
                    }
                    catch (DirectoryNotFoundException) { /* this install ships no lang folder */ }
                    catch (Exception ex)
                    {
                        problems.Add($"cannot enumerate '{lang}': {ex.Message}");
                    }
                }
            }
            catch (Exception ex) { problems.Add($"GetExecutablePath failed: {ex.Message}"); }
            var usable = new List<string>();
            foreach (string dir in dirs)
            {
                if (string.IsNullOrEmpty(dir)) continue;
                if (CanEnumerateDirectory(dir, problems)) usable.Add(dir);
            }
            return usable.Distinct().ToList();
        }

        // A PATH is a database file the caller chose; anything else is a display name to be looked
        // up among the loaded databases and the install's own folders.
        private static bool IsExplicitDatabasePath(string library)
        {
            if (string.IsNullOrWhiteSpace(library)) return false;
            string text = library.Trim();
            if (text.EndsWith(".sldmat", StringComparison.OrdinalIgnoreCase)) return true;
            if (text.IndexOf('/') >= 0 || text.IndexOf('\\') >= 0) return true;
            try { return Path.IsPathRooted(text); } catch (Exception) { return false; }
        }

        // Locate the file(s) behind a library name. One library can exist in several language
        // copies; every readable copy is consulted, so NotFound is only ever claimed when the name is
        // absent from ALL of them.
        private bool TryResolveMaterialDatabaseFiles(string library, out List<string> files,
            out string detail)
        {
            files = new List<string>();

            // An explicit path is honoured EXACTLY. No installed database with the same basename may
            // stand in for it: if the caller's custom file has been moved or is unreadable, the
            // answer is "unknown" (fatal MATERIAL_NOT_APPLIED), never "this material is absent",
            // which the compiler would turn into a warning over a real library failure.
            if (IsExplicitDatabasePath(library))
            {
                string requested;
                try
                {
                    requested = Path.GetFullPath(library.Trim());
                }
                catch (Exception ex)
                {
                    detail = $"the requested material database path '{library}' is not usable: {ex.Message}";
                    return false;
                }
                if (!File.Exists(requested))
                {
                    detail = $"the requested material database '{requested}' does not exist, and no " +
                             $"database of the same name is substituted for it";
                    return false;
                }
                files.Add(requested);
                detail = $"the requested database file '{Path.GetFileName(requested)}'";
                return true;
            }

            string wanted = NormalizeLibraryName(library);
            var problems = new List<string>();
            object[] loaded = null;
            try
            {
                loaded = _solidWorks.GetMaterialDatabases() as object[];
            }
            catch (Exception ex)
            {
                detail = $"GetMaterialDatabases failed: {ex.Message}";
                return false;
            }
            if (loaded != null)
            {
                foreach (object entry in loaded)
                {
                    string text = entry as string;
                    if (string.IsNullOrWhiteSpace(text)) continue;
                    if (NormalizeLibraryName(text) != wanted) continue;
                    // A loaded database that names this library IS an expected source: if its file
                    // cannot be read, the search is incomplete and absence cannot be claimed.
                    bool readable;
                    try { readable = File.Exists(text); }
                    catch (Exception ex)
                    {
                        problems.Add($"loaded database '{text}' could not be probed: {ex.Message}");
                        continue;
                    }
                    if (readable) files.Add(text);
                    else problems.Add($"loaded database '{text}' is not readable");
                }
            }
            if (files.Count == 0)
            {
                foreach (string dir in MaterialSearchDirectories(problems))
                {
                    try
                    {
                        foreach (string candidate in Directory.GetFiles(dir, "*.sldmat"))
                        {
                            if (NormalizeLibraryName(candidate) == wanted) files.Add(candidate);
                        }
                    }
                    catch (Exception ex)
                    {
                        problems.Add($"cannot enumerate '{dir}': {ex.Message}");
                    }
                }
            }
            files = files.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
            // An incomplete search is never an answer about absence, even when some copies were read
            // — that is the case where a same-named library in another language could otherwise
            // declare a localised material "missing".
            if (problems.Count > 0)
            {
                detail = string.Join("; ", problems);
                return false;
            }
            if (files.Count > 0)
            {
                detail = $"{files.Count} database file(s) for '{library}'";
                return true;
            }
            int loadedCount = loaded == null ? 0 : loaded.Length;
            detail = $"no readable material database for '{library}' (loaded databases: {loadedCount})";
            return false;
        }

        private static bool TryReadMaterialNames(string file, out List<string> names, out string detail)
        {
            names = new List<string>();
            try
            {
                var doc = new XmlDocument();
                doc.Load(file);   // .sldmat is UTF-16 XML with a BOM; XmlDocument honours the declaration
                // local-name() because the root carries the sldmaterials namespace and its children
                // do not — a prefixed XPath would silently match nothing.
                foreach (XmlNode node in doc.SelectNodes("//*[local-name()='material']"))
                {
                    string name = node.Attributes != null && node.Attributes["name"] != null
                        ? node.Attributes["name"].Value : null;
                    if (!string.IsNullOrWhiteSpace(name)) names.Add(name.Trim());
                }
                detail = $"{names.Count} entries in {Path.GetFileName(file)}";
                return true;
            }
            catch (Exception ex)
            {
                detail = $"cannot parse {Path.GetFileName(file)}: {ex.Message}";
                return false;
            }
        }

        private MaterialExistence LookUpMaterial(string library, string materialName, out string detail)
        {
            List<string> files;
            string resolveDetail;
            if (!TryResolveMaterialDatabaseFiles(library, out files, out resolveDetail))
            {
                detail = resolveDetail;
                return MaterialExistence.Undeterminable;
            }
            int read = 0;
            foreach (string file in files)
            {
                List<string> names;
                string readDetail;
                if (!TryReadMaterialNames(file, out names, out readDetail))
                {
                    detail = readDetail;
                    return MaterialExistence.Undeterminable;
                }
                read += names.Count;
                if (names.Any(n => MaterialNameEquals(n, materialName)))
                {
                    detail = readDetail;
                    return MaterialExistence.Present;
                }
            }
            if (read == 0)
            {
                // Zero parsed entries means the file did not look like a material database at all;
                // calling the name "absent" on that basis would be a guess, so it is not a NotFound.
                detail = $"no material entries parsed from {files.Count} file(s)";
                return MaterialExistence.Undeterminable;
            }
            detail = $"{read} entries read from {files.Count} file(s), none named '{materialName}'";
            return MaterialExistence.NotFound;
        }

        // -----------------------------------------------------------------------
        // modify_dimension — UNIVERSAL parametric value edit (the variants keystone).
        // Sets a named display dimension's value (e.g. "D1@Boss-Extrude1@Part.Part") in SI units
        // (meters for length, radians for angle), rebuilds, then echoes the EFFECTIVE value back in
        // result_geometry for in-band verification (ADR-023 spirit). Bumps state_version (geometry
        // changes — unlike analyze). Flow: analyze_model(features) reports every feature's dimension
        // full-names + SI values → pick a name → modify_dimension(name, value).
        // -----------------------------------------------------------------------
        public ExecutionResponse ModifyDimension(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                string name = p != null ? p.Value<string>("name") : null;
                double? value = p != null ? p.Value<double?>("value") : null;

                if (string.IsNullOrEmpty(name))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "name is required (the dimension full-name, e.g. 'D1@Boss-Extrude1@Part.Part' — get it from analyze_model(features)).");
                if (value == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "value is required (SI units: meters for length, radians for angle).");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                var dim = modelDoc.Parameter(name) as IDimension;
                if (dim == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DIMENSION_NOT_FOUND",
                        $"Dimension '{name}' not found. Use the exact full-name reported by analyze_model(features) (e.g. 'D1@Boss-Extrude1@Part.Part').");

                // SI setter (meters/radians). SystemValue writes the document-units (SI) value.
                dim.SystemValue = value.Value;
                modelDoc.EditRebuild3(); // commit so analyze_model/verify_state read the new geometry

                // Read the effective value back (a rebuild may clamp it or an equation may drive it).
                // Same reader analyze uses: GetSystemValue3(1=swThisConfiguration, ...) — inlined int (ADR-018).
                double effective = value.Value;
                try
                {
                    var sv = dim.GetSystemValue3(1, null) as double[];
                    if (sv != null && sv.Length > 0) effective = sv[0];
                    else effective = dim.SystemValue;
                }
                catch { try { effective = dim.SystemValue; } catch { } }

                ExecLog.Write($"modify_dimension: '{name}' requested={value.Value} effective={effective}");

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = new List<string>(),
                        Dimensions = new List<string> { name }
                    },
                    ResultGeometry = new JObject
                    {
                        ["dimension"] = name,
                        ["requested"] = value.Value,
                        ["effective"] = effective
                    },
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        // -----------------------------------------------------------------------
        // edit_feature — discriminated STRUCTURAL edit of a named feature.
        // action: suppress | unsuppress | delete | rename (rename needs new_name). Each rebuilds and
        // bumps state_version. RESOLVER PROBE (P1.3): suppress/delete CHANGE topology, so selectors or
        // coordinates captured earlier may no longer resolve — re-query with analyze_model afterwards.
        // -----------------------------------------------------------------------
        public ExecutionResponse EditFeature(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            try
            {
                var p = request.Params as JObject;
                string featureName = p != null ? p.Value<string>("feature_name") : null;
                string action = ((p != null ? p.Value<string>("action") : null) ?? "").ToLowerInvariant();
                string newName = p != null ? p.Value<string>("new_name") : null;

                if (string.IsNullOrEmpty(featureName))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "feature_name is required (exact feature-tree name, e.g. 'Boss-Extrude1').");
                if (action != "suppress" && action != "unsuppress" && action != "delete" && action != "rename")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "action is required: 'suppress', 'unsuppress', 'delete', or 'rename'.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                var allNames = new List<string>();
                var feature = FindFeatureByName(modelDoc, featureName, allNames);
                if (feature == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "FEATURE_NOT_FOUND",
                        $"Feature '{featureName}' not found. Available features: [{string.Join(", ", allNames)}].");

                string resultName = featureName;
                switch (action)
                {
                    case "suppress":
                        // swSuppressFeature=0, swThisConfiguration=1 (inlined enum constants, ADR-018).
                        feature.SetSuppression2(
                            (int)swFeatureSuppressionAction_e.swSuppressFeature,
                            (int)swInConfigurationOpts_e.swThisConfiguration, null);
                        break;
                    case "unsuppress":
                        feature.SetSuppression2(
                            (int)swFeatureSuppressionAction_e.swUnSuppressFeature,
                            (int)swInConfigurationOpts_e.swThisConfiguration, null);
                        break;
                    case "rename":
                        if (string.IsNullOrEmpty(newName))
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "MISSING_PARAMETER", "new_name is required for action='rename'.");
                        feature.Name = newName;
                        resultName = newName;
                        break;
                    case "delete":
                        // Select the FOUND IFeature directly (type-agnostic) then EditDelete. The old
                        // SelectByID2(..., "BODYFEATURE", ...) only matched body features and FAILED on
                        // sketches / reference geometry (a sketch is "SKETCH", not "BODYFEATURE"); selecting
                        // the feature object we already resolved via FindFeatureByName deletes ANY type.
                        modelDoc.ClearSelection2(true);
                        bool sel = feature.Select2(false, 0);
                        if (!sel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FEATURE_NOT_FOUND",
                                $"Feature '{featureName}' could not be selected for deletion.");
                        modelDoc.EditDelete();
                        break;
                }

                modelDoc.EditRebuild3(); // commit so analyze_model/verify_state reflect the edit
                ExecLog.Write($"edit_feature: {action} '{featureName}'" + (action == "rename" ? $" -> '{newName}'" : ""));

                var response = new ExecutionResponse
                {
                    OperationId = request.OperationId,
                    Status = "COMPLETED",
                    Verified = true,
                    StateVersion = _guard.GetCurrentStateVersion() + 1,
                    CadState = new CadState
                    {
                        StateVersion = _guard.GetCurrentStateVersion() + 1,
                        ActiveDocument = modelDoc.GetTitle(),
                        DocumentType = "PART",
                        ActiveSketch = null,
                        Features = action == "delete" ? new List<string>() : new List<string> { resultName },
                        Dimensions = new List<string>()
                    },
                    Error = null
                };

                _guard.RegisterCompleted(request.OperationId, response);
                return response;
            }
            catch (COMException ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ERROR", ex.Message);
            }
            catch (Exception ex)
            {
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "UNEXPECTED_ERROR", ex.Message);
            }
        }

        private IFeature FeatureCutOnce(IFeatureManager featureMgr, bool dir, int endCond, double depthVal, bool normalCut)
        {
            return featureMgr.FeatureCut4(
                true, false, dir,
                endCond, 0,
                depthVal, 0,
                false, false, false, false,
                0, 0,
                false, false, false, false,
                normalCut, true, true,
                false, false, false,
                (int)swStartConditions_e.swStartSketchPlane, 0,
                false, false) as IFeature;
        }

        // -----------------------------------------------------------------------
        // Revolve axis
        // -----------------------------------------------------------------------

        // Metres, the same order as the compiler's contour weld tolerance: an axis named with the
        // segment's EXACT coordinates (what ir_schema demands) always matches, and a near-miss never
        // wins by rounding.
        private const double AXIS_MATCH_TOL = 1e-6;

        private static bool AxisClose(double a, double b) { return Math.Abs(a - b) <= AXIS_MATCH_TOL; }

        // How well the sketch line (bx1,by1)-(bx2,by2) serves as the revolution axis
        // (ax1,ay1)-(ax2,ay2), both in SKETCH-space coordinates:
        //   3 = endpoints coincide, either orientation — the documented idiom (the IR carries the
        //       axis SEGMENT's own exact coordinates, so the drawn centerline scores 3).
        //   2 = collinear and the segment spans both axis endpoints (a centerline drawn long).
        //   1 = collinear and the segment contains the axis midpoint (a centerline drawn short — still
        //       the same infinite axis line, which is all SolidWorks uses).
        //   0 = not the axis.
        private static int AxisLineScore(double ax1, double ay1, double ax2, double ay2,
                                         double bx1, double by1, double bx2, double by2)
        {
            if (AxisClose(bx1, ax1) && AxisClose(by1, ay1) && AxisClose(bx2, ax2) && AxisClose(by2, ay2)) return 3;
            if (AxisClose(bx1, ax2) && AxisClose(by1, ay2) && AxisClose(bx2, ax1) && AxisClose(by2, ay1)) return 3;

            double dx = bx2 - bx1, dy = by2 - by1;
            double sq = dx * dx + dy * dy;
            if (sq < 1e-24) return 0;
            double len = Math.Sqrt(sq);
            // perpendicular distance of each axis endpoint from the segment's INFINITE line
            if (Math.Abs((ax1 - bx1) * dy - (ay1 - by1) * dx) / len > AXIS_MATCH_TOL) return 0;
            if (Math.Abs((ax2 - bx1) * dy - (ay2 - by1) * dx) / len > AXIS_MATCH_TOL) return 0;

            double t1 = ((ax1 - bx1) * dx + (ay1 - by1) * dy) / sq;
            double t2 = ((ax2 - bx1) * dx + (ay2 - by1) * dy) / sq;
            if (t1 >= -AXIS_MATCH_TOL && t1 <= 1.0 + AXIS_MATCH_TOL &&
                t2 >= -AXIS_MATCH_TOL && t2 <= 1.0 + AXIS_MATCH_TOL) return 2;

            double tm = (((ax1 + ax2) / 2.0 - bx1) * dx + ((ay1 + ay2) / 2.0 - by1) * dy) / sq;
            return (tm >= -AXIS_MATCH_TOL && tm <= 1.0 + AXIS_MATCH_TOL) ? 1 : 0;
        }

        // Guarantee the active sketch carries exactly one construction centerline on the requested axis,
        // editing NO profile edge. Returns false only when nothing could be determined (no enumerable
        // sketch / the centerline could not be created), so the caller may fall back.
        //
        // WHY THIS EXISTS. FeatureRevolve2 is called with UseAutoSelect and the selection is cleared
        // right before it, so the axis of revolution can only ever be the sketch's own centerline —
        // the previous SelectByID2 at the axis midpoint never actually fed the axis. Its one real effect
        // was CONVERTING whatever it picked to construction geometry, and a shaft drawn the textbook way
        // has the profile's own edge ON the axis and a centerline on the same line (the 1-2 flange idiom
        // the compiler tests use). That midpoint names TWO segments, SolidWorks may return the profile
        // edge, and converting it opens the contour — the revolve then sweeps an open chain or fails.
        // Matching against the sketch's segments instead is deterministic, compares in sketch space
        // (SelectByID2 wants model space, so the same numbers were not even the same coordinates), and
        // never touches a profile edge: an existing on-axis centerline is used as it is, and when the
        // only thing on the axis is a real edge, a dedicated centerline is created at the given
        // coordinates instead of converting that edge.
        private bool EnsureRevolveAxis(IModelDoc2 modelDoc, double ax1, double ay1,
                                       double ax2, double ay2, out string how)
        {
            how = "";
            ISketch sk = modelDoc.SketchManager.ActiveSketch as ISketch;
            if (sk == null)
            {
                how = "active sketch is not an ISketch";
                return false;
            }

            object[] segs;
            try { segs = sk.GetSketchSegments() as object[]; }
            catch (Exception ex)
            {
                how = $"GetSketchSegments failed: {ex.Message}";
                return false;
            }

            ISketchSegment conMatch = null, realMatch = null;
            int conScore = 0, realScore = 0, offAxisCenterlines = 0;
            if (segs != null)
            {
                foreach (var o in segs)
                {
                    var seg = o as ISketchSegment;
                    if (seg == null) continue;
                    bool con = false;
                    try { con = seg.ConstructionGeometry; } catch { }
                    if (seg.GetType() != 0)          // swSketchSegments_e: 0 = line
                    {
                        if (con) offAxisCenterlines++;   // arcs/splines are never the axis line
                        continue;
                    }
                    var ln = seg as ISketchLine;
                    if (ln == null) continue;
                    ISketchPoint pa = ln.IGetStartPoint2(), pb = ln.IGetEndPoint2();
                    if (pa == null || pb == null) continue;

                    int score = AxisLineScore(ax1, ay1, ax2, ay2, pa.X, pa.Y, pb.X, pb.Y);
                    if (score == 0) { if (con) offAxisCenterlines++; continue; }
                    if (con) { if (score > conScore) { conScore = score; conMatch = seg; } }
                    else if (score > realScore) { realScore = score; realMatch = seg; }
                }
            }

            if (conMatch != null)
            {
                // Nothing to create and nothing to convert: the drawn centerline is the axis.
                how = $"using the sketch's own centerline (endpoint-match score {conScore})";
                if (realMatch != null)
                    how += $"; left the coincident profile edge (score {realScore}) as real geometry so the contour stays closed";
                return true;
            }

            // No centerline on the axis. Create a dedicated one at the exact requested coordinates —
            // whether or not a profile edge happens to lie there. Converting that edge (the old move)
            // would open the contour.
            ISketchSegment added;
            try { added = modelDoc.SketchManager.CreateLine(ax1, ay1, 0, ax2, ay2, 0) as ISketchSegment; }
            catch (Exception ex)
            {
                how = $"centerline create failed: {ex.Message}";
                return false;
            }
            if (added == null)
            {
                how = "centerline create returned null";
                return false;
            }
            try { added.ConstructionGeometry = true; }
            catch (Exception ex)
            {
                how = $"centerline created but not convertible to construction geometry: {ex.Message}";
                return false;
            }

            how = $"added a dedicated construction centerline at ({ax1}, {ay1})-({ax2}, {ay2})";
            if (realMatch != null) how += $" (profile edge on that line kept real, score {realScore})";
            if (offAxisCenterlines > 0)
                how += $" — WARNING: {offAxisCenterlines} other centerline(s) in the sketch, so the axis may be ambiguous";
            return true;
        }
    }
}
