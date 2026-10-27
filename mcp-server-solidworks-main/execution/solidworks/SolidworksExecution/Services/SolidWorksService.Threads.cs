using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;

namespace SolidworksExecution.Services
{
    // ================================================================================
    // Thread features — the COSMETIC path (annotation only, no topology change).
    //
    // MIT ATTRIBUTION: the IFeatureManager.InsertCosmeticThread3 call shape — seven
    // POSITIONAL arguments (Standard, StandardType, Size, Diameter, EndType, Depth, Note),
    // ISO = 8, "Tapped Hole", blind = 0 / through = 2 — and the edge-selection rule that
    // precedes it (clear the selection, then pre-select the CIRCULAR bore-mouth EDGE with
    // mark 0 and append=False) are taken from
    //   https://github.com/wzyn20051216/solidworks-automation-skill
    // (MIT License, "Copyright (c) 2026 SolidWorks Automation Skill Contributors"),
    // specifically subskills/solidworks-threaded-holes/scripts/create_threaded_hole_template.py
    // lines 409-431 and references/threaded-hole-lessons.md. That repo's README/LICENSE were read
    // verbatim; the facts about the API itself are Dassault Systèmes' (SOLIDWORKS API Help:
    // InsertCosmeticThread3 is available from SOLIDWORKS 2015 FCS / Rev 23.0, and
    // ICosmeticThreadFeatureData from SOLIDWORKS 2003 SP5). Both signatures and every enum value
    // below were additionally RE-VERIFIED by reflecting the local SolidWorks Interop assemblies
    // (api\redist\SolidWorks.Interop.sldworks.dll / SolidWorks.Interop.swconst.dll):
    //   InsertCosmeticThread3(Int32 Standard, String StandardType, String Size, Double Diameter,
    //                         Int32 EndType, Double Depth, String Note) -> Feature
    //   swCosmeticStandardType_e  ...ISO=8, GB=13, DIN=4, JIS=9 ... (0..16, plus None=-2)
    //   swCosmeticEndConditions_e Blind=0, BlindUptoNext=1, Through=2, Blind2Dia=3
    //   swCosmeticThreadDiameterType_e ConicalOffset=1, MajorDiameter=2, MinorDiameter=3
    //   ICosmeticThreadFeatureData {ApplyThread, BlindDepth, Diameter, DiameterType, EndCondition,
    //                               Size, Standard, StandardType, StartFromFacePlane, ThreadCallout}
    //
    // WHY THE REAL (cut) THREAD PATH IS NOT HERE: it is a different API with a different
    // selection mark (IThreadFeatureData wants mark 1, cosmetic wants mark 0 — an asymmetry that
    // must never be hidden behind one shared helper default), it changes topology, and it needs
    // a Metric Tap.SLDLFP profile library that an install without Toolbox/thread profiles does
    // not have. Cosmetic is therefore the default representation, not a fallback.
    //
    // VERIFICATION, and why it looks nothing like create_shell's. A cosmetic thread adds ZERO
    // faces and ZERO edges: it is an annotation the drawing reads, not geometry. So
    //   * a topology diff must be EXACTLY zero — a nonzero diff means a real thread or an
    //     accidental cut happened, and
    //   * the thread CANNOT be confirmed from the tree tail: in a part or assembly a cosmetic
    //     thread is a SUBFEATURE of the hole/cut extrusion, so the last TOP-LEVEL feature is
    //     unchanged and LastFeatureName() (built on GetFeatures(true), which also skips
    //     folder-grouped features) can never see it. Persistence is proven by a recursive walk
    //     for GetTypeName2() == "CosmeticThread". A non-null return from InsertCosmeticThread3 is
    //     NOT persistence, which is why every branch below runs after EditRebuild3().
    //   * a third assertion the reference repo does not have: the created feature's
    //     Standard/Size/StandardType are written back explicitly, because the arguments
    //     InsertCosmeticThread3 takes for them are ignored by the feature it returns (measured on
    //     33.0 — see SPEC WRITE-BACK in AddCosmeticThread).
    // ================================================================================
    public partial class SolidWorksService
    {
        // swCosmeticStandardType_e members the contract is allowed to receive. Read off the local
        // swconst DLL (see header); an out-of-range integer here would otherwise cost a null
        // return and an unexplainable COSMETIC_THREAD_FAILED.
        private static readonly HashSet<int> CosmeticStandardTypes = new HashSet<int>
        {
            -2, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16
        };

        // One entry of the recursive feature-tree walk (see WalkFeatureTree).
        private sealed class FeatureWalkEntry
        {
            public IFeature Feature;
            public string Name;
            public string Type;      // GetTypeName2(), null when the call threw
            public string Parent;    // parent feature name; null = top level
        }

        public ExecutionResponse AddCosmeticThread(ToolRequest request)
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

                int edgeIndex = p?.Value<int?>("edge_index") ?? -1;
                if (edgeIndex < 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "edge_index (integer >= 0, from analyze_model(edges)) is required. A cosmetic thread " +
                        "attaches to the CIRCULAR EDGE at the mouth of the bore, not to the cylindrical face.");

                string size = p?.Value<string>("size");
                if (string.IsNullOrWhiteSpace(size))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "size (thread size string for the Standard's database, e.g. \"M5x0.8\") is required. " +
                        "It is matched as TEXT against the thread-database row, so it must be the form this " +
                        "install's database uses — see the tool description for the probed forms.");

                double? diameter = p?.Value<double?>("diameter");
                if (diameter == null || !IsFinite(diameter.Value) || diameter.Value <= 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "diameter (finite number > 0, meters) is required and MUST be the NOMINAL MAJOR " +
                        "diameter (0.005 for an M5), not the tap-drill diameter: for an internal thread " +
                        "ICosmeticThreadFeatureData.DiameterType is swCosmeticThread_MajorDiameter, so the " +
                        "pilot would place the thread on the wrong circle.");

                int standard = p?.Value<int?>("standard") ?? 8;   // swStandardType_StandardISO
                if (!CosmeticStandardTypes.Contains(standard))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"standard must be a swCosmeticStandardType_e member: ISO=8 (default), GB=13, DIN=4, " +
                        $"JIS=9, AnsiInch=0, AnsiMetric=1, HelicoilMetric=7, None=-2 — got {standard}.");

                string standardType = p?.Value<string>("standard_type") ?? "Tapped Hole";
                if (string.IsNullOrWhiteSpace(standardType))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        "standard_type must be a non-empty thread-type string for this Standard " +
                        "(\"Tapped Hole\" for ISO/GB metric internal threads).");

                int endType = p?.Value<int?>("end_type") ?? 0;    // swEndConditionBlind
                if (endType < 0 || endType > 3)
                    // swCosmeticEndConditions_e ONLY: 0 Blind, 1 BlindUptoNext, 2 Through, 3 Blind2Dia.
                    // 2 does NOT mean "through all" as it does in swEndConditions_e for an extrude, and
                    // the real-Thread family has no 2 at all — never pass a raw integer across families.
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER",
                        $"end_type must be a swCosmeticEndConditions_e member: 0=blind, 1=blind up to next, " +
                        $"2=through, 3=blind 2xDia — got {endType}.");

                double depth = p?.Value<double?>("depth") ?? 0.0;
                bool through = endType == 2;
                if (!through && !(IsFinite(depth) && depth > 0))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        $"depth (finite number > 0, meters) is required for end_type {endType} — the blind end " +
                        "conditions honour it, and 2 (through) is the only one that does not.");

                string note = p?.Value<string>("note");
                if (string.IsNullOrWhiteSpace(note)) note = size;

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
                if (!(modelDoc is IPartDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "WRONG_DOCUMENT_TYPE",
                        "add_cosmetic_thread threads a hole in a solid BODY — the active document must be a part.");

                // A thread is a body feature: an open sketch would swallow the edge selection
                // (same exit-first guard create_shell/add_edge_feature run).
                if (modelDoc.SketchManager.ActiveSketch != null)
                    modelDoc.SketchManager.InsertSketch(true);

                // The edge handle must mean the same thing it does in the read: index into the SAME
                // GetBodies2(swSolidBody,true) -> GetEdges() enumeration analyze_model(edges) reports.
                var edge = FindEdgeByIndex(modelDoc, edgeIndex);
                if (edge == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "EDGE_NOT_FOUND",
                        $"Edge index {edgeIndex} is not in the live solid-body edge enumeration. Call " +
                        "analyze_model(edges) for valid indices (a cosmetic thread needs the CIRCULAR edge " +
                        "at the bore mouth; those edges report kind/center/normal/radius).");

                // Diagnostics for both the failure message and the success echo: is the selected edge
                // actually a full circle, and where is it?
                var edgeFacts = FetchEdge(edge);
                var edgeCircle = edgeFacts.IsCircle && edgeFacts.Circle != null && edgeFacts.Circle.Length >= 7
                    ? edgeFacts.Circle : null;

                modelDoc.ClearSelection2(true);

                // MARK 0 and append=False, on the pre-cleared selection — the reference repo's rule
                // (edge.Select2(False, 0)) and the API Help's own example. The real-Thread data object
                // wants MARK 1 on the same edge; do not unify the two behind a default.
                var selEntity = edge as IEntity;
                var selData = (modelDoc.SelectionManager as ISelectionMgr)?.CreateSelectData();
                if (selEntity == null || selData == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "EDGE_NOT_FOUND",
                        $"Edge index {edgeIndex} could not be turned into a selectable entity.");
                selData.Mark = 0;
                if (!selEntity.Select4(false, selData))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "EDGE_NOT_FOUND",
                        $"Edge index {edgeIndex} was found but Select4(append=false, mark=0) refused it.");

                // Before-state the verification is measured against: the tree (for the new-thread diff
                // and a unique name), the tree tail (a cosmetic thread must NOT append a top-level
                // feature), and the topology (it must not change the topology at all).
                var lastBefore = LastFeatureName(modelDoc);
                var beforeTree = WalkFeatureTree(modelDoc);
                var beforeThreads = new HashSet<string>(
                    beforeTree.Where(e => e.Type == "CosmeticThread").Select(e => e.Name));
                var beforeNames = new HashSet<string>(
                    beforeTree.Select(e => e.Name).Where(n => n != null));
                TopologyCounts(modelDoc, out int facesBefore, out int edgesBefore, out int vertsBefore);

                string desiredName = UniqueThreadFeatureName(beforeTree, size);

                object created = null;
                try
                {
                    created = modelDoc.FeatureManager.InsertCosmeticThread3(
                        standard, standardType, size, diameter.Value, endType, through ? 0.0 : depth, note);
                }
                finally
                {
                    // The selection was consumed by the call either way; leave the tree clean.
                    try { modelDoc.ClearSelection2(true); } catch { }
                }

                if (created == null)
                {
                    // Null (not a throw) is what this call does with a bad argument — see the failure
                    // codes below. The edge's own circle report is the single most useful line here.
                    string edgeDesc = edgeCircle != null
                        ? $"it IS a circular edge (center [{R6(edgeCircle[0])}, {R6(edgeCircle[1])}, {R6(edgeCircle[2])}] " +
                          $"m, normal [{R6(edgeCircle[3])}, {R6(edgeCircle[4])}, {R6(edgeCircle[5])}], radius " +
                          $"{R6(edgeCircle[6])} m)"
                        : "it is NOT a full circular edge — a cosmetic thread needs the circle at the bore mouth";
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_FAILED",
                        $"InsertCosmeticThread3 returned null for size '{size}' (standard {standard}, " +
                        $"standard_type '{standardType}', end_type {endType}) on edge #{edgeIndex}: {edgeDesc}. " +
                        "Usual causes: the edge is not a bore-mouth circle; 'size' is not a row of this " +
                        "Standard/StandardType's thread database on THIS locale (try the other documented " +
                        "forms, e.g. \"M5x0.80\" / \"M5\"); 'diameter' is not the nominal major; or the thread " +
                        "profile library is not installed. Nothing was changed.");
                }

                // Our own deterministic handle: the localized default name is useless for read-back and
                // for a later rename/delete. Best-effort — if SolidWorks refuses, verification below
                // falls back to "the one CosmeticThread that was not there before" instead of by-name.
                try
                {
                    var createdFeat = created as IFeature;
                    if (createdFeat != null) createdFeat.Name = desiredName;
                }
                catch (Exception nameEx)
                {
                    ExecLog.Write($"add_cosmetic_thread: rename to '{desiredName}' refused: {nameEx.Message}");
                }

                modelDoc.EditRebuild3();

                // ---- VERIFICATION (three assertions, all measured AFTER the rebuild) ----
                var afterTree = WalkFeatureTree(modelDoc);
                var newThreads = afterTree
                    .Where(e => e.Type == "CosmeticThread" && !beforeThreads.Contains(e.Name))
                    .ToList();
                string lastAfter = LastFeatureName(modelDoc);
                TopologyCounts(modelDoc, out int facesAfter, out int edgesAfter, out int vertsAfter);

                if (newThreads.Count == 0)
                {
                    // Say WHAT the call actually created, if anything — "not a CosmeticThread" is a very
                    // different bug from "the tree is byte-identical, the call silently did nothing".
                    var added = afterTree.Where(e => e.Name != null && !beforeNames.Contains(e.Name)).ToList();
                    string addedDesc = added.Count == 0
                        ? "and the tree gained no feature of any type, so the call silently did nothing"
                        : "but the tree did gain " + string.Join(", ",
                            added.Skip(Math.Max(0, added.Count - 8)).Select(
                                e => $"'{e.Name}' ({e.Type ?? "?"}{(e.Parent == null ? ", top level" : ", under " + e.Parent)})"));
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_NOT_PERSISTED",
                        $"'{desiredName}' is not in the feature tree as a CosmeticThread after the rebuild " +
                        $"(tree tail {lastBefore ?? "<none>"} -> {lastAfter ?? "<none>"}) {addedDesc}. A " +
                        "non-null return from InsertCosmeticThread3 is not persistence.");
                }

                var hit = newThreads.FirstOrDefault(e => e.Name == desiredName) ?? newThreads[0];
                if (newThreads.Count > 1)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_AMBIGUOUS",
                        $"The rebuild produced {newThreads.Count} new CosmeticThread entries " +
                        $"({string.Join(", ", newThreads.Select(e => e.Name))}) from one call — the state is " +
                        "not trustworthy, so nothing is reported as verified.");

                if (hit.Parent == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_NOT_SUBFEATURE",
                        $"'{hit.Name}' is a TOP-LEVEL feature. A cosmetic thread is a subfeature of the hole " +
                        "or cut extrusion it belongs to; a top-level entry means something other than a " +
                        "cosmetic thread was created.");

                if (lastAfter != lastBefore)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_ADDED_TOP_LEVEL_FEATURE",
                        $"The tree tail moved ({lastBefore ?? "<none>"} -> {lastAfter ?? "<none>"}). A cosmetic " +
                        "thread hangs under its cut and never becomes the last top-level feature, so a real " +
                        "thread or an extra cut was created instead.");

                // Read back what the DRAWING will show. ThreadCallout is the documented read-back of the
                // Note argument — the only verification that proves the drawing-visible semantics.
                string readCallout = null, readSize = null, readStandardType = null;
                double? readDiameter = null, readBlindDepth = null;
                int? readStandard = null, readEndCondition = null, readDiameterType = null;
                try
                {
                    var data = hit.Feature?.GetDefinition() as ICosmeticThreadFeatureData;
                    if (data != null)
                    {
                        readCallout = SafeString(() => data.ThreadCallout);
                        readSize = SafeString(() => data.Size);
                        readStandardType = SafeString(() => data.StandardType);
                        readStandard = SafeInt(() => data.Standard);
                        readEndCondition = SafeInt(() => data.EndCondition);
                        readDiameterType = SafeInt(() => data.DiameterType);
                        readDiameter = SafeDouble(() => data.Diameter);
                        readBlindDepth = SafeDouble(() => data.BlindDepth);
                    }
                }
                catch (Exception defEx)
                {
                    ExecLog.Write($"add_cosmetic_thread: GetDefinition(CosmeticThread) threw: {defEx.Message}");
                }

                // ---- SPEC WRITE-BACK. This exists because of what was MEASURED, not as a retry. ----
                // InsertCosmeticThread3's Standard/StandardType/Size arguments are IGNORED by the feature
                // it creates: on 33.0 (Chinese locale) 12 triples — ISO/GB/DIN/JIS/ANSI-inch/ANSI-metric x
                // "Tapped Hole"/"M"/"Metric"/"ISO 261" x M5x0.8 handed as 0.005 m AND as 5.0 document-mm —
                // every one read back Standard=-2 (swStandardType_StandardNone), Size="", StandardType="",
                // while Diameter, EndCondition, BlindDepth, DiameterType and ThreadCallout all came back
                // exactly as sent. So the call yields a real CosmeticThread feature carrying NO spec, and
                // the drawing-visible thread is the callout text at the given major diameter. Setting the
                // three properties on ICosmeticThreadFeatureData and calling ModifyDefinition fixes that:
                // all 12 trials then read back the requested Standard/StandardType/Size (spec_writeback
                // "applied"), and the topology stays unchanged through the extra rebuild. Be clear about
                // what this proves — the strings are stored, NOT validated ("ZZZ-no-such-size" stores and
                // reads back the same), so spec_stored means "the feature records what you sent" and never
                // "the thread table knows this size". The path is a diagnostic, never a failure: an install
                // that refused the write-back would still have delivered the annotation that was asked for.
                string specWriteback = "not_needed";
                if (readStandard != standard || readSize != size ||
                    !string.Equals(readStandardType ?? "", standardType ?? "", StringComparison.Ordinal))
                {
                    specWriteback = "ignored";
                    try
                    {
                        var wData = hit.Feature?.GetDefinition() as ICosmeticThreadFeatureData;
                        if (wData == null)
                            specWriteback = "data_unavailable";
                        else
                        {
                            wData.Standard = standard;
                            wData.StandardType = standardType;
                            wData.Size = size;
                            if (!hit.Feature.ModifyDefinition(wData, modelDoc, null))
                                specWriteback = "modify_rejected";
                            else
                            {
                                modelDoc.EditRebuild3();
                                var aData = hit.Feature.GetDefinition() as ICosmeticThreadFeatureData;
                                if (aData != null)
                                {
                                    readStandard = SafeInt(() => aData.Standard);
                                    readSize = SafeString(() => aData.Size);
                                    readStandardType = SafeString(() => aData.StandardType);
                                    readCallout = SafeString(() => aData.ThreadCallout);
                                    readDiameter = SafeDouble(() => aData.Diameter);
                                    readBlindDepth = SafeDouble(() => aData.BlindDepth);
                                    specWriteback = (readStandard == standard && readSize == size)
                                        ? "applied" : "ignored";
                                }
                                else specWriteback = "unreadable_after_modify";
                            }
                        }
                    }
                    catch (Exception wEx)
                    {
                        // The write-back is optional; the thread already exists and is already verified
                        // below. A COM refusal here must not turn a good annotation into a FAILED job.
                        ExecLog.Write($"add_cosmetic_thread: spec write-back refused: {wEx.Message}");
                        specWriteback = "exception";
                    }
                    // The definition was edited and the document rebuilt, so the topology is
                    // re-measured before the zero-diff assertion below judges it.
                    TopologyCounts(modelDoc, out facesAfter, out edgesAfter, out vertsAfter);
                }

                if (readCallout == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_NOT_READABLE",
                        $"'{hit.Name}' is in the tree as a CosmeticThread but its definition could not be read, " +
                        "so the callout the drawing would show cannot be confirmed.");

                if (readCallout != note)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_CALLOUT_MISMATCH",
                        $"ICosmeticThreadFeatureData.ThreadCallout read back '{readCallout}' but the Note sent " +
                        $"was '{note}'. The drawing annotation is not what was asked for.");

                if (facesAfter != facesBefore || edgesAfter != edgesBefore || vertsAfter != vertsBefore)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "COSMETIC_THREAD_TOPOLOGY_CHANGED",
                        $"A cosmetic thread is an annotation and must add ZERO faces/edges/vertices, but the " +
                        $"counts went faces {facesBefore}->{facesAfter}, edges {edgesBefore}->{edgesAfter}, " +
                        $"vertices {vertsBefore}->{vertsAfter}. That is the signature of a real (cut) thread " +
                        "or an accidental cut, not a cosmetic thread.");

                var result = new JObject
                {
                    ["cosmetic_thread"] = hit.Name,
                    ["parent_feature"] = hit.Parent,
                    ["size"] = readSize ?? size,
                    ["callout"] = readCallout,
                    ["standard"] = readStandard ?? standard,
                    ["standard_type"] = readStandardType ?? standardType,
                    ["diameter"] = readDiameter ?? diameter.Value,
                    ["edge_index"] = edgeIndex,
                    ["edge_is_circle"] = edgeCircle != null,
                    ["faces"] = facesAfter,
                    ["edges"] = edgesAfter,
                    ["vertices"] = vertsAfter,
                    ["topology_unchanged"] = true,
                    // spec_stored is "the feature's own Standard/Size read back equal to what was sent".
                    // It is NOT "this install's thread table has a row for that size" — MEASURED: the size
                    // text is never validated ("ZZZ-no-such-size" stores and reads back identically), so
                    // a typo in `size` is invisible here. The drawing-visible spec is `callout`.
                    ["spec_stored"] = readStandard == standard && readSize == size,
                    ["spec_writeback"] = specWriteback,
                    // A cosmetic thread is ADDITIVE per edge, not idempotent (MEASURED: calling again on
                    // a mouth that already has one creates a second subfeature under the same hole). These
                    // are document-wide CosmeticThread counts before/after this call, so a pile-up from
                    // retried jobs is visible in the response itself.
                    ["threads_in_document_before"] = beforeThreads.Count,
                    ["threads_in_document_after"] = afterTree.Count(e => e.Type == "CosmeticThread"),
                };
                // Nullable read-backs: omit rather than send null/JValue-empty when this build does not
                // implement the property, so a missing diagnostic never reads as a wrong value.
                if (readEndCondition != null) result["end_condition"] = readEndCondition.Value;
                if (readDiameterType != null) result["diameter_type"] = readDiameterType.Value;
                if (readBlindDepth != null) result["blind_depth"] = readBlindDepth.Value;
                if (edgeCircle != null)
                {
                    result["edge_center"] = new JArray { R6(edgeCircle[0]), R6(edgeCircle[1]), R6(edgeCircle[2]) };
                    result["edge_normal"] = new JArray { R6(edgeCircle[3]), R6(edgeCircle[4]), R6(edgeCircle[5]) };
                    result["edge_radius"] = R6(edgeCircle[6]);
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
                        Features = new List<string> { hit.Name },
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = result,
                    Error = null
                };

                ExecLog.Write($"add_cosmetic_thread: {size} d={diameter.Value} end={endType} " +
                              $"depth={depth} edge#{edgeIndex} -> '{hit.Name}' under '{hit.Parent}' " +
                              $"callout='{readCallout}' spec={readStandard}/{readSize ?? ""} " +
                              $"({specWriteback}) topology={facesAfter}/{edgesAfter}/{vertsAfter}");

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

        // The i-th solid-body edge, using the EXACT enumeration analyze_model(edges) reports
        // (GetBodies2(swSolidBody,true) -> GetEdges(), bodies in enumeration order, edges in theirs),
        // so an index from the read means the same thing here. Returns null when out of range.
        // Mirrors SelectFaceByIndexAppend's walk for faces.
        private IEdge FindEdgeByIndex(IModelDoc2 modelDoc, int edgeIndex)
        {
            if (edgeIndex < 0) return null;
            var partDoc = modelDoc as IPartDoc;
            object[] bodies = null;
            try { bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[]; }
            catch { return null; }
            if (bodies == null) return null;
            int i = 0;
            foreach (var b in bodies)
            {
                var body = b as IBody2;
                if (body == null) continue;
                object[] es = null;
                try { es = body.GetEdges() as object[]; } catch { continue; }
                if (es == null) continue;
                foreach (var e in es)
                {
                    if (i == edgeIndex) return e as IEdge;
                    i++;
                }
            }
            return null;
        }

        // Recursive feature-tree walk: top-level features AND sub-features at every depth, each entry
        // carrying the parent's name. This is the walk the verification is built on, and the reason it
        // exists: a cosmetic thread is a SUBFEATURE of its hole/cut, so GetFeatures(true) (what
        // LastFeatureName uses) and a top-level-only walk both report a perfectly good thread as
        // missing. Budgeted like FindFeatureByName's loops, and every COM call is individually guarded
        // so one hostile feature cannot truncate the walk.
        private static List<FeatureWalkEntry> WalkFeatureTree(IModelDoc2 modelDoc)
        {
            var entries = new List<FeatureWalkEntry>();
            if (modelDoc == null) return entries;
            int budget = 20000;
            IFeature first = null;
            try { first = modelDoc.FirstFeature() as IFeature; } catch { }
            WalkInto(first, null, true, entries, ref budget);
            return entries;
        }

        private static void WalkInto(IFeature feat, string parentName, bool isTop,
                                     List<FeatureWalkEntry> entries, ref int budget)
        {
            while (feat != null && budget-- > 0)
            {
                string name = null, type = null;
                try { name = feat.Name; } catch { }
                try { type = feat.GetTypeName2(); } catch { }
                entries.Add(new FeatureWalkEntry { Feature = feat, Name = name, Type = type, Parent = parentName });
                try
                {
                    WalkInto(feat.GetFirstSubFeature() as IFeature, name, false, entries, ref budget);
                }
                catch { }
                try
                {
                    feat = isTop ? feat.GetNextFeature() as IFeature : feat.GetNextSubFeature() as IFeature;
                }
                catch { feat = null; }
            }
        }

        // Faces/edges/vertices of the SOLID bodies — the negative topology assertion. Summed over
        // GetBodies2(swSolidBody,true) exactly like BuildBodySummary, plus the vertex count.
        private static void TopologyCounts(IModelDoc2 modelDoc, out int faces, out int edges, out int vertices)
        {
            faces = 0; edges = 0; vertices = 0;
            var partDoc = modelDoc as IPartDoc;
            object[] bodies = null;
            try { bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[]; }
            catch { }
            if (bodies == null) return;
            foreach (var b in bodies)
            {
                var body = b as IBody2;
                if (body == null) continue;
                try { faces += body.GetFaceCount(); } catch { }
                try { edges += body.GetEdgeCount(); } catch { }
                try { vertices += body.GetVertexCount(); } catch { }
            }
        }

        // "CosmeticThread_M5x0.8", de-duplicated against the tree already in the document (a second M5
        // in the same part gets _2). Feature names must be unique to be a usable read-back handle.
        private static string UniqueThreadFeatureName(List<FeatureWalkEntry> tree, string size)
        {
            var taken = new HashSet<string>(tree.Select(e => e.Name).Where(n => n != null));
            var safe = new string((size ?? "").Where(ch => char.IsLetterOrDigit(ch) || ch == '.' || ch == '-')
                                  .ToArray());
            if (safe.Length == 0) safe = "Thread";
            string baseName = "CosmeticThread_" + safe;
            if (!taken.Contains(baseName)) return baseName;
            for (int n = 2; n < 100; n++)
            {
                string candidate = baseName + n;
                if (!taken.Contains(candidate)) return candidate;
            }
            return baseName + Guid.NewGuid().ToString("N").Substring(0, 6);
        }

        private static bool IsFinite(double v)
        {
            return !double.IsNaN(v) && !double.IsInfinity(v);
        }

        // Read-back discipline: a property this API family does not implement on this build throws
        // rather than returning a default, and a diagnostic read must never turn a verified feature
        // into a failure. Each read is therefore separately guarded and yields null when unavailable.
        private static string SafeString(Func<string> get)
        {
            try { return get(); } catch { return null; }
        }

        private static int? SafeInt(Func<int> get)
        {
            try { return get(); } catch { return null; }
        }

        private static double? SafeDouble(Func<double> get)
        {
            try { return get(); } catch { return null; }
        }
    }
}
