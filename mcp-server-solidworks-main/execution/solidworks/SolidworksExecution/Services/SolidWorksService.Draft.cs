using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;

namespace SolidworksExecution.Services
{
    // ================================================================================
    // DRAFT — the casting/molding wall taper (InsertMultiFaceDraft, face draft).
    //
    // SIGNATURES AND ENUMS here were read off the local SolidWorks Interop assemblies
    // (api\redist\SolidWorks.Interop.sldworks.dll / SolidWorks.Interop.swconst.dll) and
    // cross-checked against the local API Help topic
    // SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IFeatureManager~InsertMultiFaceDraft:
    //   InsertMultiFaceDraft(Double Angle, Boolean FlipDir, Boolean EdgeDraft,
    //                        Int32 PropType, Boolean IsStepDraft, Boolean IsBodyDraft) -> Feature
    //   IModelDoc2 has two OLDER void-returning forms (InsertMfDraft / InsertMfDraft2) that cannot
    //     report what they made — this file uses the IFeatureManager form precisely because it
    //     returns the Feature, so "no silent null" is checkable.
    //   Angle           RADIANS (the Help states "Draft angle in radians"; IDraftFeatureData2.Angle
    //                    is radians too, so the read-back needs no conversion).
    //   Selection marks (the Help's own FlipDir remarks, verbatim): the direction / neutral entity
    //                    "could be a planar face, a reference plane, an edge, or two vertices …
    //                    these items have a selection mark of 1; the faces to draft have a
    //                    selection mark of 2, and the draft edges have a selection mark of 4".
    //   PropType        swDraftFacePropagationType_e: 0 none, 1 next tangent faces sharing the base
    //                    neutral plane, 2 all neighbours of the neutral plane, 3 neighbours on all
    //                    inner loops (pocket/boss), 4 neighbours on the outer loop — the Help's own
    //                    example for 4 is "the sides of a box with the bottom face as the neutral
    //                    plane", i.e. exactly the casting-wall case this tool was asked for.
    //   swDraftType_e   swNeutralPlaneDraft=0, swPartingLineDraft=1, swStepDraft=3.
    //
    // WHY EdgeDraft / IsStepDraft / IsBodyDraft ARE HARD-CODED false: EdgeDraft=true re-reads the
    // selection as EDGES on mark 4 (a different entity type and a different mark, so exposing it
    // here would silently mis-select the face list callers pass); a step draft needs the step/
    // pull-direction machinery (swDraftStepType_e) and a body draft drafts the WHOLE body, which
    // contradicts this tool's contract of "these face indices, that neutral face". Growing the
    // edge/step/body variants means new parameters AND new assertions, so they stay out until asked
    // for (same rule create_rib follows about its own draft arguments).
    //
    // VERIFICATION MODEL — a draft is a REAL body feature, unlike a cosmetic thread: it moves faces
    // and changes volume, but it must not re-face the body, so the assertions are
    //   1. the call returned non-null AND the feature tree gained that feature after the rebuild
    //      (matched by the returned handle's own name, falling back to its own internal type string —
    //      NOT a hard-coded "Draft", because GetTypeName2 is compared against what the install says);
    //   2. every requested face's plane normal actually tilted by the requested angle (measured
    //      before/after on the SAME face index — a null return is not the only way to do nothing);
    //   3. the NEUTRAL face's normal did NOT tilt (it is the datum; if it moved, the marks were
    //      swapped and the feature is not the draft that was asked for);
    //   4. the face count is unchanged (a draft tilts existing faces; splitting faces means it
    //      drafted something else);
    //   5. the unrounded volume changed at all, and in the direction the caller declared
    //      (volume_expect) — which is also what distinguishes flip_direction=true from false when
    //      the tilt magnitudes are identical.
    // Rejected input (bad index, non-planar face, neutral face in the draft list, angle out of
    // range) is refused BEFORE any selection or API call, so a rejection leaves the tree untouched.
    // A post-creation assertion failure is different and deliberate: like create_shell /
    // add_cosmetic_thread it returns FAILED with the feature already in the (unsaved) document, and
    // the caller discards the document instead of trying to undo a body feature blind.
    // ================================================================================
    public partial class SolidWorksService
    {
        // swDraftFacePropagationType_e members the contract may receive (see header).
        private static readonly HashSet<int> DraftPropagationTypes = new HashSet<int>
        {
            0, 1, 2, 3, 4
        };

        // A draft angle is a wall taper, not a rotation: below ~0.001 rad the tilt is at the limit of
        // what a normal read-back can resolve, and above 90 deg the "draft" is a re-orientation of the
        // face. Both ends are refused rather than trusted.
        private const double DraftAngleMinRad = 1e-4;
        private const double DraftAngleMaxRad = Math.PI / 2.0;

        // Tilt agreement, in radians: ~0.006 deg. Loose enough to absorb the acos-of-dot error of a
        // normal read-back, tight enough that a unit mistake (degrees handed in as radians is a
        // 57x error) or a wrong-face selection fails instead of passing.
        private const double DraftTiltToleranceRad = 1e-4;

        public ExecutionResponse AddDraft(ToolRequest request)
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

                // ---- parameter validation: everything here happens BEFORE any document is touched ----
                int[] faceIndices;
                var faceErr = ReadFaceIndexList(p, out faceIndices);
                if (faceErr != null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", faceErr);

                int neutralIndex = p?.Value<int?>("neutral_face_index") ?? -1;
                if (neutralIndex < 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "neutral_face_index (integer >= 0, from analyze_model(faces)) is required — the " +
                        "planar face or reference plane the drafted faces stay attached to. It is selected " +
                        "with MARK 1 (the Help's direction/neutral mark); the faces to draft take MARK 2.");

                if (faceIndices.Contains(neutralIndex))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"neutral_face_index {neutralIndex} also appears in face_indices. The neutral face " +
                        "is the datum a draft is measured from — drafting it as well is contradictory, and " +
                        "SolidWorks refuses it. Nothing was changed.");

                double angle = p?.Value<double?>("angle") ?? double.NaN;
                if (!IsFinite(angle) || angle < DraftAngleMinRad || angle > DraftAngleMaxRad)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"angle must be a finite number of RADIANS in [{DraftAngleMinRad}, " +
                        $"{Math.Round(DraftAngleMaxRad, 6)}] — 0.052360 = 3 deg, 0.139626 = 8 deg. This API " +
                        "takes radians (measured), so a degrees value would draft 57x too far.");

                int propagate = p?.Value<int?>("propagate") ?? 0;
                if (!DraftPropagationTypes.Contains(propagate))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"propagate must be a swDraftFacePropagationType_e member: 0=none, 1=next tangent " +
                        $"faces sharing the base neutral plane, 2=all neighbours of the neutral plane, " +
                        $"3=neighbours on all inner loops, 4=neighbours on the outer loop (the sides of a " +
                        $"box with the bottom face neutral) — got {propagate}.");

                bool flip = p?.Value<bool?>("flip_direction") ?? false;

                string volumeExpect = (p?.Value<string>("volume_expect") ?? "any").Trim().ToLowerInvariant();
                if (volumeExpect != "any" && volumeExpect != "increase" && volumeExpect != "decrease")
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"volume_expect must be 'any' (default), 'increase' or 'decrease' — got " +
                        $"'{volumeExpect}'. It states which way the draft must take the body volume; the " +
                        "tilt magnitudes are identical for flip_direction true and false, so this is the " +
                        "assertion that tells the two apart.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
                if (!(modelDoc is IPartDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "WRONG_DOCUMENT_TYPE",
                        "add_draft tapers faces of a solid BODY — the active document must be a part.");

                // ---- face resolution + the planarity gate (still no change to the document) ----
                var faces = EnumerateSolidBodyFaces(modelDoc);
                var picked = new List<Tuple<int, IFace2, double[]>>();
                foreach (int fi in faceIndices)
                {
                    if (fi >= faces.Count)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FACE_NOT_FOUND",
                            $"Face index {fi} is not in the live solid-body face enumeration (0..{faces.Count - 1}). " +
                            "Call analyze_model(faces) for valid indices — they shift whenever the body changes.");
                    var f = FetchFaceSurface(faces[fi]);
                    FetchFaceNormal(f);
                    if (!f.Planar || f.Normal == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DRAFT_FACE_NOT_PLANAR",
                            $"Face index {fi} is not a plane (it is {(f.Kind ?? "a non-planar surface")}). This " +
                            "tool is InsertMultiFaceDraft in FACE-draft mode, which tapers planar walls; a " +
                            "cylindrical or conical face is not tapered by it. Nothing was changed.");
                    picked.Add(Tuple.Create(fi, faces[fi], f.Normal));
                }
                if (neutralIndex >= faces.Count)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "FACE_NOT_FOUND",
                        $"neutral_face_index {neutralIndex} is not in the live solid-body face enumeration " +
                        $"(0..{faces.Count - 1}). Call analyze_model(faces) for valid indices.");
                var neutralFacts = FetchFaceSurface(faces[neutralIndex]);
                FetchFaceNormal(neutralFacts);
                if (!neutralFacts.Planar || neutralFacts.Normal == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_NEUTRAL_NOT_PLANAR",
                        $"neutral_face_index {neutralIndex} is not a plane. The neutral reference supplies both " +
                        "the datum line and the pull direction, so it must be planar. Nothing was changed.");
                double[] neutralBefore = neutralFacts.Normal;

                TopologyCounts(modelDoc, out int facesBefore, out int edgesBefore, out int vertsBefore);
                double? volumeBefore = RawVolume(modelDoc);

                // Before-state the verification is measured against, taken BEFORE the call: the tree
                // (for "the one feature that was not there before") and the name we want to own.
                var beforeTree = WalkFeatureTree(modelDoc);
                var beforeNames = new HashSet<string>(
                    beforeTree.Select(e => e.Name).Where(n => n != null));
                string desiredName = UniqueDraftFeatureName(beforeTree);

                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                // Neutral (mark 1) first, then the faces to draft (mark 2), on a cleared selection.
                // The marks, not the order, assign the roles — but the read-back below proves the
                // selection landed on the entities this call was pointed at, so an order- or
                // mark-swap cannot pass silently.
                // The handle the call returned is kept for the verification: its own Name and its own
                // GetTypeName2() — the INTERNAL type string, not the localized GetTypeText this install
                // (Chinese) would return — say what was added. So the tree check never has to guess the
                // type text, which is why this does not hard-code "Draft": asserting a guessed string
                // would fail a correct draft if this API's type string differed from the guess.
                IFeature createdFeat = null;
                string createdType = null;
                modelDoc.ClearSelection2(true);
                try
                {
                    if (!SelectFaceByIndexAppend(modelDoc, neutralIndex, 1))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FACE_NOT_FOUND",
                            $"Neutral face index {neutralIndex} was found but Select4(append=true, mark=1) " +
                            "refused it.");
                    foreach (var t in picked)
                        if (!SelectFaceByIndexAppend(modelDoc, t.Item1, 2))
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FACE_NOT_FOUND",
                                $"Face index {t.Item1} was found but Select4(append=true, mark=2) refused it.");

                    object created = modelDoc.FeatureManager.InsertMultiFaceDraft(
                        angle, flip, false, propagate, false, false);

                    if (created == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DRAFT_FAILED",
                            $"InsertMultiFaceDraft returned null for angle {R6(angle)} rad, " +
                            $"{picked.Count} face(s) [{string.Join(",", picked.Select(t => t.Item1))}] about " +
                            $"neutral face #{neutralIndex} (propagate {propagate}, flip {flip}): this " +
                            "install refuses to draft that set about that plane — commonly the neutral face " +
                            "is not adjacent to the drafted faces, the angle is too steep for the geometry, " +
                            "or propagation was asked for across a face that cannot take it. Nothing was added.");

                    createdFeat = created as IFeature;
                    try { createdType = createdFeat?.GetTypeName2(); } catch { }

                    // Our own handle for the read-back: the localized default name is not usable as a
                    // reference, and a rename that SolidWorks refuses is not a failure (verification
                    // falls back to "the one new entry of the created feature's own type").
                    try
                    {
                        if (createdFeat != null) createdFeat.Name = desiredName;
                    }
                    catch (Exception nameEx)
                    {
                        ExecLog.Write($"add_draft: rename to '{desiredName}' refused: {nameEx.Message}");
                    }
                }
                finally
                {
                    try { modelDoc.ClearSelection2(true); } catch { }
                }

                modelDoc.EditRebuild3();

                // ---- VERIFICATION ----
                var afterTree = WalkFeatureTree(modelDoc);
                string hitName = null;
                try { hitName = createdFeat?.Name; } catch { }
                var newOnes = afterTree.Where(e => e.Name != null && !beforeNames.Contains(e.Name)).ToList();
                var hit = newOnes.FirstOrDefault(e => hitName != null && e.Name == hitName);
                if (hit == null)
                {
                    // The name did not survive (rename refused, or the handle's name read threw): fall
                    // back to the created feature's OWN internal type string, so the assertion is still
                    // "the draft this call added", not a guessed literal.
                    var draftLike = newOnes.Where(e => createdType == null || e.Type == createdType).ToList();
                    if (newOnes.Count == 0)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DRAFT_NOT_PERSISTED",
                            "InsertMultiFaceDraft returned a Feature but the tree gained no entry of any " +
                            "type after the rebuild, so the call silently did nothing. A non-null return is " +
                            "not persistence, so nothing is reported as verified.");
                    if (draftLike.Count == 0)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DRAFT_NOT_PERSISTED",
                            $"InsertMultiFaceDraft returned a Feature (internal type '{createdType ?? "?"}') " +
                            $"named '{hitName ?? "?"}', but after the rebuild the tree gained only " +
                            string.Join(", ", newOnes.Select(
                                e => $"'{e.Name}' ({e.Type ?? "?"}{(e.Parent == null ? ", top level" : ", under " + e.Parent)})")) +
                            ". The returned feature is not in the tree, so the draft is not verified.");
                    if (draftLike.Count > 1)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DRAFT_AMBIGUOUS",
                            $"One call produced {draftLike.Count} new entries of type '{createdType}' " +
                            $"({string.Join(", ", draftLike.Select(e => e.Name))}) and the returned handle's " +
                            $"name '{hitName ?? "?"}' matches none of them — the state is not trustworthy, so " +
                            "no face-by-face verification is reported.");
                    hit = draftLike[0];
                }
                var draftFeat = hit.Feature;
                if (draftFeat == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_NOT_PERSISTED",
                        $"'{hit.Name}' is in the tree as a draft-type feature but its feature handle could " +
                        "not be read, so its definition cannot be confirmed.");

                // Read the feature's own definition: what SolidWorks stored is the second opinion on
                // angle / flip / propagation, independent of the numbers this call passed in.
                double? readAngle = null, readNeutralArea = null;
                bool? readFlip = null;
                int? readType = null, readProp = null, readFaceCount = null;
                try
                {
                    var data = draftFeat.GetDefinition() as IDraftFeatureData2;
                    if (data != null)
                    {
                        readAngle = SafeDouble(() => data.Angle);
                        readFlip = SafeBool(() => data.ReverseDirection);
                        readType = SafeInt(() => data.Type);
                        readProp = SafeInt(() => (int)data.FacePropagation);
                        try { readFaceCount = (int)data.GetFacesToDraftCount(); } catch { }
                        var np = data.NeutralPlane as IFace2;
                        if (np != null) readNeutralArea = SafeDouble(() => np.GetArea());
                    }
                }
                catch (Exception defEx)
                {
                    ExecLog.Write($"add_draft: GetDefinition(Draft) threw: {defEx.Message}");
                }

                if (readAngle == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_DATA_UNREADABLE",
                        $"'{hit.Name}' is in the tree as a Draft but IDraftFeatureData2.Angle could not be " +
                        "read, so the stored angle cannot be confirmed against the request.");
                if (Math.Abs(readAngle.Value - angle) > DraftTiltToleranceRad)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_ANGLE_MISMATCH",
                        $"IDraftFeatureData2.Angle reads back {R6(readAngle.Value)} rad but {R6(angle)} rad " +
                        "was requested — the feature stored a different taper than the call was given.");

                // The geometric truth: re-read the SAME face indices after the rebuild and measure how
                // far each plane normal moved. Face count is asserted first, because an index only
                // means the same thing on both sides of the rebuild if the enumeration did not change.
                TopologyCounts(modelDoc, out int facesAfter, out int edgesAfter, out int vertsAfter);
                var facesNow = EnumerateSolidBodyFaces(modelDoc);
                if (facesNow.Count != faces.Count || facesAfter != facesBefore)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_FACE_COUNT_CHANGED",
                        $"A draft tilts the existing planar walls and must not re-face the body, but the solid " +
                        $"body went from {faces.Count} enumerated faces ({facesBefore} by GetFaceCount) to " +
                        $"{facesNow.Count} ({facesAfter}). Something other than a wall taper was built " +
                        $"(propagate {propagate} can pull neighbours in); no face-by-face tilt " +
                        "verification is possible on a body whose face enumeration moved.");

                var draftedReports = new JArray();
                double maxTiltError = 0.0;
                foreach (var t in picked)
                {
                    var afterFacts = FetchFaceSurface(facesNow[t.Item1]);
                    FetchFaceNormal(afterFacts);
                    double tilt = afterFacts.Normal != null ? PlaneTiltRad(t.Item3, afterFacts.Normal) : double.NaN;
                    if (!IsFinite(tilt))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "DRAFT_ANGLE_MISMATCH",
                            $"Face index {t.Item1} is no longer readable as a tilted plane after the draft, so " +
                            "its angle against the requested taper cannot be measured.");
                    if (Math.Abs(tilt - angle) > maxTiltError) maxTiltError = Math.Abs(tilt - angle);
                    draftedReports.Add(new JObject
                    {
                        ["face_index"] = t.Item1,
                        ["normal_before"] = new JArray { R6(t.Item3[0]), R6(t.Item3[1]), R6(t.Item3[2]) },
                        ["normal_after"] = new JArray { R6(afterFacts.Normal[0]), R6(afterFacts.Normal[1]), R6(afterFacts.Normal[2]) },
                        ["tilt_rad"] = R6(tilt),
                        ["tilt_deg"] = Math.Round(tilt * 180.0 / Math.PI, 4)
                    });
                }

                if (maxTiltError > DraftTiltToleranceRad)
                {
                    // Hoisted out of the interpolated string: C# 7.3 (this project's language version)
                    // rejects a newline inside an interpolation hole, and the per-face summary reads
                    // better built in one place anyway.
                    string tiltSummary = string.Join(", ", draftedReports.Cast<JObject>()
                        .Select(o => "#" + o["face_index"] + "=" + o["tilt_rad"]));
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_ANGLE_MISMATCH",
                        $"The drafted faces did not tilt by the requested {R6(angle)} rad: worst error " +
                        $"{R6(maxTiltError)} rad over [{tiltSummary}]. The feature was created and " +
                        "is left in the (unsaved) document; discard the document rather than build on it.");
                }

                var neutralNow = FetchFaceSurface(facesNow[neutralIndex]);
                FetchFaceNormal(neutralNow);
                double neutralTilt = neutralNow.Normal != null ? PlaneTiltRad(neutralBefore, neutralNow.Normal) : 0.0;
                if (neutralTilt > DraftTiltToleranceRad)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_NEUTRAL_FACE_MOVED",
                        $"Neutral face #{neutralIndex}'s plane normal tilted by {R6(neutralTilt)} rad. The " +
                        "neutral face is the datum a draft is measured from and must not move — this is the " +
                        "signature of the two selection marks (1 = neutral/direction, 2 = faces to draft) " +
                        "having been swapped or of the neutral face also being drafted.");

                double? volumeAfter = RawVolume(modelDoc);
                if (volumeBefore == null || volumeAfter == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_VOLUME_UNREADABLE",
                        $"The draft was created and the faces tilted as requested, but the body volume could " +
                        "not be read before/after, so the direction of the taper cannot be confirmed.");
                double dVol = volumeAfter.Value - volumeBefore.Value;
                string volumeDirection = Math.Abs(dVol) < 1e-15 ? "unchanged" : (dVol > 0 ? "increase" : "decrease");
                if (Math.Abs(dVol) < 1e-15)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_VOLUME_UNCHANGED",
                        $"The faces tilted but the unrounded volume did not move ({volumeBefore.Value:E6} m3 " +
                        "-> " + volumeAfter.Value.ToString("E6") + " m3). A real wall taper always removes or " +
                        "adds material — a zero delta with a non-zero tilt means the drafted faces are not " +
                        "part of this body's envelope.");
                if (volumeExpect != "any" && volumeDirection != volumeExpect)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DRAFT_VOLUME_DIRECTION_UNEXPECTED",
                        $"The draft changed the volume in the wrong direction: expected '{volumeExpect}', got " +
                        $"'{volumeDirection}' (dV = {dVol:E6} m3). The taper is the right size but on the " +
                        $"wrong side of the neutral plane — flip_direction={(!flip)} is the call that tapers " +
                        "this way. Nothing was changed back; the feature is in the (unsaved) document.");

                var result = new JObject
                {
                    ["draft"] = hit.Name,
                    ["draft_type"] = hit.Type,
                    ["parent_feature"] = hit.Parent ?? "",
                    ["angle_rad"] = R6(angle),
                    ["angle_deg"] = Math.Round(angle * 180.0 / Math.PI, 4),
                    ["angle_applied_rad"] = R6(readAngle.Value),
                    ["flip_direction"] = flip,
                    ["flip_applied"] = readFlip ?? flip,
                    ["propagate"] = propagate,
                    ["propagate_applied"] = readProp ?? propagate,
                    ["neutral_face_index"] = neutralIndex,
                    ["neutral_normal_before"] = new JArray { R6(neutralBefore[0]), R6(neutralBefore[1]), R6(neutralBefore[2]) },
                    ["neutral_tilt_rad"] = R6(neutralTilt),
                    ["drafted_faces"] = draftedReports,
                    ["max_tilt_error_rad"] = R6(maxTiltError),
                    ["volume_before"] = volumeBefore.Value,
                    ["volume_after"] = volumeAfter.Value,
                    ["volume_delta"] = dVol,
                    ["volume_direction"] = volumeDirection,
                    ["faces"] = facesAfter,
                    ["edges"] = edgesAfter,
                    ["vertices"] = vertsAfter,
                    ["faces_before"] = facesBefore,
                    ["edges_before"] = edgesBefore,
                    ["vertices_before"] = vertsBefore,
                    ["face_count_unchanged"] = true,
                    ["volume_expect"] = volumeExpect,
                };
                // Nullable read-backs: omit rather than send a stand-in, so a missing diagnostic never
                // reads as a wrong value (same discipline as add_cosmetic_thread's end_condition).
                if (readType != null) result["draft_kind"] = readType.Value;          // swDraftType_e
                if (readFaceCount != null) result["faces_to_draft_count"] = readFaceCount.Value;
                if (readNeutralArea != null) result["neutral_face_area"] = R6(readNeutralArea.Value);

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
                        Features = new List<string> { hit.Name },
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = result,
                    Error = null
                };

                ExecLog.Write($"add_draft: {R6(angle)} rad on [{string.Join(",", picked.Select(t => t.Item1))}] " +
                              $"about #{neutralIndex} (prop {propagate}, flip {flip}) -> '{hit.Name}' " +
                              $"tilt<={R6(maxTiltError)} dV={dVol:E3} faces {facesNow.Count}/{edgesAfter}/{vertsAfter}");

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

        // ------------------------------------------------------------------------
        // Helpers
        // ------------------------------------------------------------------------

        // face_indices arrives as a JSON array STRING (create_shell's face_indices idiom) because that
        // is how every existing face-list parameter in this contract is carried. Validates the SHAPE
        // only — index bounds and planarity are checked against the live body afterwards.
        private static string ReadFaceIndexList(JObject p, out int[] indices)
        {
            indices = new int[0];
            // Read the raw token, not Value<string>: the adapter sends "[1,2]" (a string, like
            // create_shell's face_indices), but a caller that hands a real JSON array must not die in
            // the converter and report UNEXPECTED_ERROR for a well-formed request.
            var tok = p?["face_indices"];
            if (tok == null || tok.Type == JTokenType.Null)
                return "face_indices (JSON array of integer face indices from analyze_model(faces), e.g. " +
                       "\"[1,2,3,4]\") is required — the planar faces to taper. A draft needs at least one.";
            string raw = tok.Type == JTokenType.String ? tok.Value<string>() : tok.ToString(Formatting.None);
            if (string.IsNullOrWhiteSpace(raw))
                return "face_indices (JSON array of integer face indices from analyze_model(faces), e.g. " +
                       "\"[1,2,3,4]\") is required — the planar faces to taper. A draft needs at least one.";
            JArray arr;
            try { arr = JArray.Parse(raw); }
            catch { return "face_indices must be a JSON array of integers, e.g. \"[1,2,3,4]\"."; }
            if (arr.Count == 0) return "face_indices must name at least one face.";
            var list = new List<int>();
            foreach (var it in arr)
            {
                int? v = it.Value<int?>();
                if (v == null || v.Value < 0) return "face_indices must be non-negative integers.";
                list.Add(v.Value);
            }
            if (list.Count == 0) return "face_indices must name at least one face.";
            if (list.Count != list.Distinct().Count()) return "face_indices contains a duplicate face index.";
            indices = list.ToArray();
            return null;
        }

        // The i-th solid-body face list, in the EXACT order analyze_model(faces) and
        // SelectFaceByIndexAppend use (GetBodies2(swSolidBody,true) -> GetFaces()), so one index means
        // the same thing in the read, the selection and the before/after comparison.
        private static List<IFace2> EnumerateSolidBodyFaces(IModelDoc2 modelDoc)
        {
            var all = new List<IFace2>();
            var partDoc = modelDoc as IPartDoc;
            object[] bodies = null;
            try { bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[]; }
            catch { return all; }
            if (bodies == null) return all;
            foreach (var b in bodies)
            {
                var body = b as IBody2;
                if (body == null) continue;
                object[] fs = null;
                try { fs = body.GetFaces() as object[]; } catch { continue; }
                if (fs == null) continue;
                foreach (var f in fs) { var fa = f as IFace2; if (fa != null) all.Add(fa); }
            }
            return all;
        }

        // Angle between two plane normals, in radians (0 = the face did not turn). The normals are
        // unit vectors, and a draft never flips a face through 90 deg, so acos of the dot is the
        // whole measurement — no signed-axis decomposition, because the SIGN of a taper is judged
        // from the volume delta, which is what the body does about it.
        private static double PlaneTiltRad(double[] a, double[] b)
        {
            if (a == null || b == null || a.Length < 3 || b.Length < 3) return double.NaN;
            double dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
            double na = Math.Sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]);
            double nb = Math.Sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2]);
            if (na < 1e-12 || nb < 1e-12) return double.NaN;
            double c = dot / (na * nb);
            if (c > 1.0) c = 1.0;
            if (c < -1.0) c = -1.0;
            return Math.Acos(c);
        }

        // "Draft_1", de-duplicated against the tree: a drafted casting has several draft features and
        // each read-back handle has to name exactly one of them.
        private static string UniqueDraftFeatureName(List<FeatureWalkEntry> tree)
        {
            var taken = new HashSet<string>(tree.Select(e => e.Name).Where(n => n != null));
            for (int n = 1; n < 100; n++)
            {
                string candidate = "Draft_" + n;
                if (!taken.Contains(candidate)) return candidate;
            }
            return "Draft_" + Guid.NewGuid().ToString("N").Substring(0, 6);
        }

        private static bool? SafeBool(Func<bool> get)
        {
            try { return get(); } catch { return null; }
        }
    }
}
