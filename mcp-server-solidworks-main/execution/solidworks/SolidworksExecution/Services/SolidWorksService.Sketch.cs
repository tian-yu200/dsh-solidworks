using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;


namespace SolidworksExecution.Services
{
    // SolidWorksService partial: sketch lifecycle and 2D geometry (create/edit sketch, entities, dimensions, constraints).
    public partial class SolidWorksService
    {

        public ExecutionResponse CreateSketch(ToolRequest request)
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
                string planeName = p != null ? p.Value<string>("plane") : null;
                bool onFace = p?.Value<bool?>("on_face") ?? false;

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                // Auto-exit any active sketch so multi-sketch workflows (sweep needs a path + a profile,
                // loft needs multiple profiles) work without a dedicated exit-sketch tool. Previously this
                // returned SKETCH_ALREADY_ACTIVE; finishing the open sketch before starting a new one is the
                // sensible behaviour and all existing flows call create_sketch with no sketch active anyway.
                if (modelDoc.SketchManager.ActiveSketch != null)
                {
                    AutoConstrainActiveSketch(modelDoc);
                    modelDoc.SketchManager.InsertSketch(true);
                }

                modelDoc.ClearSelection2(true);
                if (onFace)
                {
                    // Sketch on a model FACE. Two ways to pick the face:
                    //   (a) face_index — the i-th face from analyze_model(faces). ROBUST: reuses the same
                    //       GetBodies2(swSolidBody,true) → GetFaces() enumeration and selects the IFace2
                    //       directly, bypassing the coordinate pick that is ambiguous on a revolve end-cap
                    //       (the flat circular cap's centroid sits on the revolve axis/origin → FACE_NOT_FOUND).
                    //   (b) face_x/y/z — a 3D point lying ON the planar face (interior, not on an edge).
                    // Plane-name selection can't reach model faces, so this is what enables features on
                    // existing geometry (e.g. a recess/hub/keyway on a gear face, or a bore on a shaft end).
                    int? faceIndex = p?.Value<int?>("face_index");
                    if (faceIndex != null)
                    {
                        var partDoc = modelDoc as IPartDoc;
                        var allFaces = new List<IFace2>();
                        object[] bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
                        if (bodies != null)
                            foreach (var b in bodies)
                            {
                                var body = b as IBody2;
                                if (body == null) continue;
                                object[] fs = body.GetFaces() as object[];
                                if (fs == null) continue;
                                foreach (var f in fs) { var fa = f as IFace2; if (fa != null) allFaces.Add(fa); }
                            }
                        int fi = faceIndex.Value;
                        if (fi < 0 || fi >= allFaces.Count)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FACE_NOT_FOUND",
                                $"Face index {fi} out of range (part has {allFaces.Count} faces). Call analyze_model(faces) for valid indices.");
                        var ent = allFaces[fi] as IEntity;
                        bool faceSel = ent != null && ent.Select4(false, null);
                        if (!faceSel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FACE_NOT_FOUND", $"Failed to select face index {fi}.");
                    }
                    else
                    {
                        double fx = p?.Value<double?>("face_x") ?? 0.0;
                        double fy = p?.Value<double?>("face_y") ?? 0.0;
                        double fz = p?.Value<double?>("face_z") ?? 0.0;
                        bool faceSel = modelDoc.Extension.SelectByID2("", "FACE", fx, fy, fz, false, 0, null, 0);
                        if (!faceSel)
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "FACE_NOT_FOUND",
                                $"No face found at ({fx}, {fy}, {fz}). Provide a point lying ON the target planar face (interior, not on an edge), or use face_index from analyze_model(faces).");
                    }
                }
                else
                {
                    if (string.IsNullOrEmpty(planeName))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER", "plane is required (or set on_face=true with face_x/face_y/face_z).");
                    bool selected = SelectPlaneFlexible(modelDoc, planeName, false, 0);
                    if (!selected)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "PLANE_NOT_FOUND", $"Plane '{planeName}' not found");
                }

                if (onFace)
                {
                    try
                    {
                        var selMgr = modelDoc.SelectionManager as ISelectionMgr;
                        int selCount = selMgr?.GetSelectedObjectCount2(-1) ?? -1;
                        ExecLog.Write($"create_sketch on_face: selectedCount={selCount} before InsertSketch");
                    }
                    catch { }
                }

                modelDoc.SketchManager.InsertSketch(true);

                var activeSketch = modelDoc.SketchManager.ActiveSketch;
                if (onFace)
                    ExecLog.Write($"create_sketch on_face: activeSketch={(activeSketch != null)} after InsertSketch");
                if (activeSketch == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_CREATION_FAILED", "Sketch was not created or is not active after InsertSketch. The face may be non-planar, or the selection was lost — try analyze_model(faces) and a different face_index.");

                // The NEW sketch's REAL runtime name ("Sketch1", "Çizim3", ...). The compiler
                // records cadState.activeSketch as this sketch's runtime name and later features
                // (sweep path, loft profiles) reference sketches by exactly that string, so the
                // placeholder this line used to fall back to made name-referenced features
                // unbuildable. See ActiveSketchName for how the real name is resolved.
                string activeSketchName = ActiveSketchName(modelDoc);

                // Echo the NEW sketch's MEASURED plane + frame (same reader analyze uses). The
                // caller (IR compiler) compares this against the original sketch's recorded frame
                // and transforms 2D coordinates — the deterministic fix for the support-normal /
                // in-plane-axis mismatch class (2-1 Sketch5). Best-effort: never fails the create.
                object sketchPlaneEcho = null;
                try { sketchPlaneEcho = ReadSketchPlane(activeSketch); } catch { }

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
                        ActiveSketch = activeSketchName,
                        Features = new List<string>(),
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = sketchPlaneEcho,
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

        // The single-entity CREATE core shared by add_sketch_entity and add_sketch_entities (Batch-3):
        // one switch, never forked. Creates ONE entity record (entity_type + its params, incl. the
        // per-record construction flag) in the ACTIVE sketch. Returns null on success (createdSeg /
        // createdSegs populated) or "CODE|message" on failure — the caller maps it into its own
        // response shape. The caller owns the AddToDB bracket (ADR-042) and the guard preamble.
        private string CreateSketchEntityCore(IModelDoc2 modelDoc, JObject p, string entityType,
            string sketchName, out ISketchSegment createdSeg, out object[] createdSegs)
        {
            createdSeg = null;
            createdSegs = null;
            bool created = false;
            switch (entityType.ToLowerInvariant())
            {
                case "rectangle":
                {
                    double? x1 = p?.Value<double?>("x1");
                    double? y1 = p?.Value<double?>("y1");
                    double? x2 = p?.Value<double?>("x2");
                    double? y2 = p?.Value<double?>("y2");
                    if (x1 == null || y1 == null || x2 == null || y2 == null)
                        return "MISSING_PARAMETER|rectangle requires x1, y1, x2, y2.";
                    var segs = modelDoc.SketchManager.CreateCornerRectangle(
                        x1.Value, y1.Value, 0, x2.Value, y2.Value, 0) as object[];
                    createdSegs = segs;
                    created = segs != null && segs.Length > 0;
                    break;
                }
                case "circle":
                {
                    double? cx = p?.Value<double?>("cx");
                    double? cy = p?.Value<double?>("cy");
                    double? radius = p?.Value<double?>("radius");
                    if (cx == null || cy == null || radius == null)
                        return "MISSING_PARAMETER|circle requires cx, cy, radius.";
                    var arc = modelDoc.SketchManager.CreateCircleByRadius(cx.Value, cy.Value, 0, radius.Value);
                    createdSeg = arc as ISketchSegment;
                    created = arc != null;
                    break;
                }
                case "line":
                {
                    double? x1 = p?.Value<double?>("x1");
                    double? y1 = p?.Value<double?>("y1");
                    double? x2 = p?.Value<double?>("x2");
                    double? y2 = p?.Value<double?>("y2");
                    if (x1 == null || y1 == null || x2 == null || y2 == null)
                        return "MISSING_PARAMETER|line requires x1, y1, x2, y2.";
                    var seg = modelDoc.SketchManager.CreateLine(x1.Value, y1.Value, 0, x2.Value, y2.Value, 0);
                    createdSeg = seg as ISketchSegment;
                    created = seg != null;
                    break;
                }
                case "arc":
                {
                    // 3-point arc: start (x1,y1), end (x2,y2), mid-arc point (xm,ym)
                    double? x1 = p?.Value<double?>("x1");
                    double? y1 = p?.Value<double?>("y1");
                    double? x2 = p?.Value<double?>("x2");
                    double? y2 = p?.Value<double?>("y2");
                    double? xm = p?.Value<double?>("xm");
                    double? ym = p?.Value<double?>("ym");
                    if (x1 == null || y1 == null || x2 == null || y2 == null || xm == null || ym == null)
                        return "MISSING_PARAMETER|arc requires x1, y1 (start), x2, y2 (end), xm, ym (mid-arc point).";
                    var arc = modelDoc.SketchManager.Create3PointArc(
                        x1.Value, y1.Value, 0,
                        x2.Value, y2.Value, 0,
                        xm.Value, ym.Value, 0);
                    createdSeg = arc as ISketchSegment;
                    created = arc != null;
                    break;
                }
                case "arc_center":
                {
                    // Center-based arc: exact center (cx,cy) + start (x1,y1), end (x2,y2), direction
                    // (+1 CCW, -1 CW). Guarantees the radius; numerically stable for shallow arcs.
                    double? cx = p?.Value<double?>("cx");
                    double? cy = p?.Value<double?>("cy");
                    double? x1 = p?.Value<double?>("x1");
                    double? y1 = p?.Value<double?>("y1");
                    double? x2 = p?.Value<double?>("x2");
                    double? y2 = p?.Value<double?>("y2");
                    if (cx == null || cy == null || x1 == null || y1 == null || x2 == null || y2 == null)
                        return "MISSING_PARAMETER|arc_center requires cx, cy (center), x1, y1 (start), x2, y2 (end).";
                    // direction: +1 CCW / -1 CW, applied VERBATIM when given. When OMITTED (or 0),
                    // pick the MINOR (<=180°) arc deterministically — a corner round / fillet arc is
                    // virtually always the minor one, and a wrong sign silently draws the 270°
                    // complement (live benchmark failure: R2 90° corner came out as the other side).
                    // Callers that really want the major arc pass the explicit sign (the analyze/IR
                    // round-trip always does — its 'dir' is read from the original geometry).
                    double dirRaw = p?.Value<double?>("direction") ?? 0.0;
                    short direction;
                    if (Math.Abs(dirRaw) >= 0.5)
                        direction = (short)(dirRaw >= 0 ? 1 : -1);
                    else
                    {
                        double a1 = Math.Atan2(y1.Value - cy.Value, x1.Value - cx.Value);
                        double a2 = Math.Atan2(y2.Value - cy.Value, x2.Value - cx.Value);
                        double ccwSweep = a2 - a1;
                        while (ccwSweep < 0) ccwSweep += 2 * Math.PI;
                        while (ccwSweep >= 2 * Math.PI) ccwSweep -= 2 * Math.PI;
                        direction = (short)(ccwSweep <= Math.PI ? 1 : -1);
                    }
                    // Endpoints passed VERBATIM (shared exactly with neighbours — the closed contour is
                    // preserved). The caller's AddToDB bracket keeps SW inference off the exact radius.
                    var arc = modelDoc.SketchManager.CreateArc(
                        cx.Value, cy.Value, 0,
                        x1.Value, y1.Value, 0,
                        x2.Value, y2.Value, 0,
                        direction);
                    createdSeg = arc;
                    created = arc != null;
                    break;
                }
                case "ellipse":
                {
                    // Center-based ellipse: cx,cy (center), x1,y1 (major-axis point), x2,y2 (minor-axis point).
                    double? cx = p?.Value<double?>("cx");
                    double? cy = p?.Value<double?>("cy");
                    double? x1 = p?.Value<double?>("x1");
                    double? y1 = p?.Value<double?>("y1");
                    double? x2 = p?.Value<double?>("x2");
                    double? y2 = p?.Value<double?>("y2");
                    if (cx == null || cy == null || x1 == null || y1 == null || x2 == null || y2 == null)
                        return "MISSING_PARAMETER|ellipse requires cx, cy (center), x1, y1 (major-axis point), x2, y2 (minor-axis point).";
                    var ell = modelDoc.SketchManager.CreateEllipse(
                        cx.Value, cy.Value, 0,
                        x1.Value, y1.Value, 0,
                        x2.Value, y2.Value, 0);
                    createdSeg = ell as ISketchSegment;
                    created = ell != null;
                    break;
                }
                case "spline":
                {
                    // Through-point spline: 'points' = flat [x1,y1,x2,y2,...] (>= 2 points), z=0.
                    var ptsTok = p?["points"] as JArray;
                    if (ptsTok == null || ptsTok.Count < 4 || ptsTok.Count % 2 != 0)
                        return "MISSING_PARAMETER|spline requires 'points': a flat list [x1,y1,x2,y2,...] of >= 2 (x,y) pairs.";
                    int nPts = ptsTok.Count / 2;
                    var pointData = new double[nPts * 3];
                    for (int i = 0; i < nPts; i++)
                    {
                        pointData[i * 3] = ptsTok[i * 2].Value<double>();
                        pointData[i * 3 + 1] = ptsTok[i * 2 + 1].Value<double>();
                        pointData[i * 3 + 2] = 0.0;
                    }
                    var spl = modelDoc.SketchManager.CreateSpline(pointData);
                    createdSeg = spl as ISketchSegment;
                    created = spl != null;
                    break;
                }
                case "fillet":
                {
                    // Sketch fillet: rounds the corner between two segments meeting at vertex (vx, vy).
                    double? vx = p?.Value<double?>("vx");
                    double? vy = p?.Value<double?>("vy");
                    double? radius = p?.Value<double?>("radius");
                    if (vx == null || vy == null || radius == null)
                        return "MISSING_PARAMETER|fillet requires vx, vy (vertex coordinates) and radius.";
                    modelDoc.ClearSelection2(true);
                    bool vertexSelected = modelDoc.Extension.SelectByID2(
                        "", "SKETCHPOINT", vx.Value, vy.Value, 0, false, 0, null, 0);
                    if (!vertexSelected)
                        return $"VERTEX_NOT_FOUND|No sketch vertex found at or near ({vx.Value}, {vy.Value}).";
                    var fillet = modelDoc.SketchManager.CreateFillet(radius.Value,
                        (int)swConstrainedCornerAction_e.swConstrainedCornerDeleteGeometry);
                    createdSeg = fillet as ISketchSegment;
                    created = fillet != null;
                    if (created) RegenerateSketchInPlace(modelDoc, sketchName);
                    break;
                }
                case "chamfer":
                {
                    // Sketch chamfer: cuts the corner between two adjacent segments at vertex (vx, vy).
                    double? vx = p?.Value<double?>("vx");
                    double? vy = p?.Value<double?>("vy");
                    double? distance = p?.Value<double?>("distance");
                    if (vx == null || vy == null || distance == null)
                        return "MISSING_PARAMETER|chamfer requires vx, vy (vertex coordinates) and distance.";
                    modelDoc.ClearSelection2(true);
                    bool vertexSelected = modelDoc.Extension.SelectByID2(
                        "", "SKETCHPOINT", vx.Value, vy.Value, 0, false, 0, null, 0);
                    if (!vertexSelected)
                        return $"VERTEX_NOT_FOUND|No sketch vertex found at or near ({vx.Value}, {vy.Value}).";
                    var chamfer = modelDoc.SketchManager.CreateChamfer(
                        (int)swSketchChamferType_e.swSketchChamfer_DistanceEqual,
                        distance.Value, distance.Value);
                    createdSeg = chamfer as ISketchSegment;
                    created = chamfer != null;
                    if (created) RegenerateSketchInPlace(modelDoc, sketchName);
                    break;
                }
                default:
                    return $"UNSUPPORTED_ENTITY_TYPE|entity_type '{entityType}' is not supported. Supported: rectangle, circle, line, arc, arc_center, ellipse, spline, fillet, chamfer.";
            }

            if (!created)
                return $"ENTITY_CREATION_FAILED|Failed to create sketch entity of type '{entityType}'.";

            // construction=true converts the created segment(s) to construction/reference geometry
            // (centerlines, symmetry axes, hole-position scaffolding). Applied post-create because
            // SketchManager has no construction-aware create calls.
            bool asConstruction = p?.Value<bool?>("construction") ?? false;
            if (asConstruction)
            {
                try
                {
                    if (createdSeg != null) createdSeg.ConstructionGeometry = true;
                    if (createdSegs != null)
                        foreach (var o in createdSegs)
                        {
                            var s = o as ISketchSegment;
                            if (s != null) s.ConstructionGeometry = true;
                        }
                }
                catch (Exception ex)
                {
                    return $"CONSTRUCTION_FLAG_FAILED|Entity created but could not be converted to construction geometry: {ex.Message}";
                }
            }
            return null;
        }

        // Batch-3: add_sketch_entities — N entity records in ONE call. The single-entity tool's calls
        // were the largest call-count item in both level-2-2 benchmark runs (62 and 16 calls); a batch
        // cuts both the round-trips and the per-call context echo. Same JSON-string idiom as ADR-022
        // (the adapter parses `segments` into a JArray of records, each = add_sketch_entity's params).
        // NOT transactional (IR-ADR-001 failure isolation): on the first failing record the run STOPS
        // and reports FAILED with the record index + how many records were already created (they remain
        // in the sketch — the caller decides whether to continue, fix, or discard). state_version bumps
        // only on COMPLETED. The echo is a compact per-type count, NOT per-segment result_geometry —
        // read the sketch back with analyze_model(sketch) when exact readback is needed.
        public ExecutionResponse AddSketchEntities(ToolRequest request)
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
                var segsTok = p?["segments"] as JArray;
                if (segsTok == null || segsTok.Count == 0)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "segments is required: a JSON array of entity records, each with entity_type + its add_sketch_entity params.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var activeSketch = modelDoc.SketchManager.ActiveSketch;
                if (activeSketch == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_NOT_ACTIVE", "No active sketch. Call create_sketch first.");
                string sketchName = ActiveSketchName(modelDoc);

                int done = 0;
                var counts = new Dictionary<string, int>();
                // ONE AddToDB bracket around the whole batch (ADR-042 — frozen-exact coordinates, no
                // pixel-tolerance snapping) and ONE redraw at the end, instead of per segment.
                bool prevAddToDb = modelDoc.SketchManager.AddToDB;
                modelDoc.SketchManager.AddToDB = true;
                try
                {
                    for (int i = 0; i < segsTok.Count; i++)
                    {
                        var sp = segsTok[i] as JObject;
                        string et = sp?.Value<string>("entity_type");
                        if (sp == null || string.IsNullOrEmpty(et))
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                                "MISSING_PARAMETER",
                                $"segments[{i}] must be an object with entity_type. {done} segment(s) before it were created and remain in the sketch.");

                        ISketchSegment seg; object[] segs;
                        string err = CreateSketchEntityCore(modelDoc, sp, et, sketchName, out seg, out segs);
                        if (err != null)
                        {
                            var parts = err.Split(new[] { '|' }, 2);
                            string code = parts[0];
                            string msg = parts.Length > 1 ? parts[1] : err;
                            ExecLog.Write($"add_sketch_entities: FAILED at segments[{i}] ({et}) after {done} created — {code}");
                            return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(), code,
                                $"segments[{i}] ({et}): {msg} {done} segment(s) before it were created and remain in the sketch.");
                        }
                        done++;
                        string key = et.ToLowerInvariant();
                        counts[key] = counts.TryGetValue(key, out var c) ? c + 1 : 1;
                    }
                }
                finally
                {
                    modelDoc.SketchManager.AddToDB = prevAddToDb;
                }
                modelDoc.GraphicsRedraw2();

                ExecLog.Write($"add_sketch_entities: created {done} segment(s) in {sketchName}");

                var countsJson = new JObject();
                foreach (var kv in counts) countsJson[kv.Key] = kv.Value;

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
                        ActiveSketch = sketchName,
                        Features = new List<string>(),
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = new JObject
                    {
                        ["segment_count"] = done,
                        ["counts"] = countsJson
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

        public ExecutionResponse AddSketchEntity(ToolRequest request)
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
                string entityType = p?.Value<string>("entity_type");
                if (string.IsNullOrEmpty(entityType))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "entity_type is required (rectangle, circle, line).");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var activeSketch = modelDoc.SketchManager.ActiveSketch;
                if (activeSketch == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_NOT_ACTIVE", "No active sketch. Call create_sketch first.");

                // The entity switch lives in CreateSketchEntityCore — ONE switch shared with
                // add_sketch_entities (Batch-3), never forked. The core also applies the per-record
                // construction flag. Captured segment(s) feed the result_geometry echo below
                // (read back from SW, never the input, so snapping/inference drift is visible).
                //
                // AddToDB for the WHOLE create (generalizes ADR-022's arc_center-only toggle): all
                // coordinates arrive frozen-exact (analyze round-trip / IR lowering), so SW's input
                // inference/snapping must never touch them. Found live on the 1-2 flange rebuild: a
                // 1.59mm line (raised-face step) built fine in one document, then CreateLine returned
                // NULL for the same coords in the next — the pixel-based snap tolerance depends on the
                // window's zoom, so short segments nondeterministically collapse without AddToDB.
                ISketchSegment createdSeg;
                object[] createdSegs;
                string coreErr;
                // Resolved ONCE, before the create: the core needs the sketch's runtime name to
                // re-enter it after a sketch fillet/chamfer (RegenerateSketchInPlace) and the
                // response echoes it as this sketch's name (see ActiveSketchName).
                string activeSketchName = ActiveSketchName(modelDoc);
                bool prevAddToDbAll = modelDoc.SketchManager.AddToDB;
                modelDoc.SketchManager.AddToDB = true;
                try
                {
                    coreErr = CreateSketchEntityCore(modelDoc, p, entityType,
                        activeSketchName, out createdSeg, out createdSegs);
                }
                finally
                {
                    modelDoc.SketchManager.AddToDB = prevAddToDbAll;
                }
                if (coreErr != null)
                {
                    var coreParts = coreErr.Split(new[] { '|' }, 2);
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        coreParts[0], coreParts.Length > 1 ? coreParts[1] : coreErr);
                }

                // In-band echo of the REAL created geometry (Task 2). Read back from SW (never the
                // input), so AddToDB snapping / inference drift is visible to the caller without a
                // separate analyze pass. Best-effort; a read failure must not fail the create.
                object resultGeom = null;
                try
                {
                    if (createdSeg != null)
                    {
                        resultGeom = ReadSegment(createdSeg);
                    }
                    else if (createdSegs != null)
                    {
                        var arr = new JArray();
                        foreach (var o in createdSegs)
                        {
                            var rs = ReadSegment(o as ISketchSegment);
                            if (rs != null) arr.Add(rs);
                        }
                        resultGeom = new JObject { ["kind"] = "rectangle", ["segments"] = arr };
                    }
                }
                catch { resultGeom = null; }

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
                        ActiveSketch = activeSketchName,
                        Features = new List<string>(),
                        Dimensions = new List<string>()
                    },
                    ResultGeometry = resultGeom,
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

        public ExecutionResponse AddDimension(ToolRequest request)
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
                double? px = p?.Value<double?>("px");
                double? py = p?.Value<double?>("py");
                double? value = p?.Value<double?>("value");
                double labelOffsetX = p?.Value<double?>("label_offset_x") ?? 0.0;
                double labelOffsetY = p?.Value<double?>("label_offset_y") ?? -0.015;

                if (px == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "px is required (X coordinate of a point on the target segment).");
                if (py == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "py is required (Y coordinate of a point on the target segment).");
                if (value == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "value is required.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var activeSketch = modelDoc.SketchManager.ActiveSketch;
                if (activeSketch == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_NOT_ACTIVE", "No active sketch. Call create_sketch first.");

                string activeSketchName = ActiveSketchName(modelDoc);

                // Force SolidWorks to commit and refresh sketch geometry before attempting selection.
                // Without this, segments created by add_rectangle may not yet be queryable via SelectByID2.
                modelDoc.ClearSelection2(true);
                modelDoc.GraphicsRedraw2();

                // Suppress the "input dimension value" dialog — otherwise SolidWorks blocks waiting for user input
                bool prevInputDimVal = _solidWorks.GetUserPreferenceToggle(
                    (int)swUserPreferenceToggle_e.swInputDimValOnCreate);
                _solidWorks.SetUserPreferenceToggle(
                    (int)swUserPreferenceToggle_e.swInputDimValOnCreate, false);

                // SelectByID2 with coordinates selects the nearest segment — robust for any sketch geometry
                bool selected = modelDoc.Extension.SelectByID2(
                    "", "SKETCHSEGMENT", px.Value, py.Value, 0, false, 0, null, 0);

                if (!selected)
                {
                    _solidWorks.SetUserPreferenceToggle(
                        (int)swUserPreferenceToggle_e.swInputDimValOnCreate, prevInputDimVal);
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SEGMENT_NOT_FOUND", $"No sketch segment found at or near ({px.Value}, {py.Value}).");
                }

                var dim = modelDoc.AddDimension2(px.Value + labelOffsetX, py.Value + labelOffsetY, 0) as IDisplayDimension;

                // Restore preference regardless of outcome
                _solidWorks.SetUserPreferenceToggle(
                    (int)swUserPreferenceToggle_e.swInputDimValOnCreate, prevInputDimVal);

                if (dim == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DIMENSION_CREATION_FAILED", "AddDimension2 returned null.");

                // Drive the dimension to the requested value
                var swDim = dim.GetDimension2(0) as SolidWorks.Interop.sldworks.IDimension;
                if (swDim != null)
                    swDim.SystemValue = value.Value;

                var existingDims = new List<string>();
                existingDims.Add($"px={px.Value},py={py.Value},value={value.Value}");

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
                        ActiveSketch = activeSketchName,
                        Features = new List<string>(),
                        Dimensions = existingDims
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

        public ExecutionResponse AddSketchConstraint(ToolRequest request)
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
                string constraintType = p?.Value<string>("constraint_type");
                if (string.IsNullOrEmpty(constraintType))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "constraint_type is required (horizontal, vertical, coincident, parallel, perpendicular, tangent, equal, midpoint).");

                double? px1 = p?.Value<double?>("px1");
                double? py1 = p?.Value<double?>("py1");
                if (px1 == null || py1 == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "px1 and py1 are required (point on the first entity).");

                double? px2 = p?.Value<double?>("px2");
                double? py2 = p?.Value<double?>("py2");
                string entityType1 = p?.Value<string>("entity_type1") ?? "SKETCHSEGMENT";
                string entityType2 = p?.Value<string>("entity_type2") ?? "SKETCHSEGMENT";

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (modelDoc.SketchManager.ActiveSketch == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_NOT_ACTIVE", "No active sketch. Call create_sketch first.");

                string swConstraintId;
                bool needsTwoEntities = false;
                switch (constraintType.ToLowerInvariant())
                {
                    case "horizontal":    swConstraintId = "sgHORIZONTAL2D";  break;
                    case "vertical":      swConstraintId = "sgVERTICAL2D";    break;
                    case "coincident":    swConstraintId = "sgCOINCIDENT";    needsTwoEntities = true; break;
                    case "parallel":      swConstraintId = "sgPARALLEL";      needsTwoEntities = true; break;
                    case "perpendicular": swConstraintId = "sgPERPENDICULAR"; needsTwoEntities = true; break;
                    case "tangent":       swConstraintId = "sgTANGENT";       needsTwoEntities = true; break;
                    case "equal":         swConstraintId = "sgSAMELENGTH";    needsTwoEntities = true; break;
                    case "midpoint":      swConstraintId = "sgATMIDDLE";      needsTwoEntities = true; break;
                    default:
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "UNSUPPORTED_CONSTRAINT_TYPE",
                            $"constraint_type '{constraintType}' is not supported. Supported: horizontal, vertical, coincident, parallel, perpendicular, tangent, equal, midpoint.");
                }

                if (needsTwoEntities && (px2 == null || py2 == null))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", $"constraint_type '{constraintType}' requires px2 and py2 (point on the second entity).");

                modelDoc.ClearSelection2(true);
                bool sel1 = modelDoc.Extension.SelectByID2("", entityType1, px1.Value, py1.Value, 0, false, 0, null, 0);
                if (!sel1)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "ENTITY_NOT_FOUND", $"No {entityType1} found at or near ({px1.Value}, {py1.Value}).");

                if (needsTwoEntities)
                {
                    bool sel2 = modelDoc.Extension.SelectByID2("", entityType2, px2.Value, py2.Value, 0, true, 0, null, 0);
                    if (!sel2)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "ENTITY_NOT_FOUND", $"No {entityType2} found at or near ({px2.Value}, {py2.Value}).");
                }

                modelDoc.SketchAddConstraints(swConstraintId);

                string activeSketchName = ActiveSketchName(modelDoc);

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
                        ActiveSketch = activeSketchName,
                        Features = new List<string>(),
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
        // M18 — edit_sketch
        // -----------------------------------------------------------------------
        public ExecutionResponse EditSketch(ToolRequest request)
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
                string sketchName = p?.Value<string>("sketch_name");
                if (string.IsNullOrEmpty(sketchName))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER",
                        "sketch_name is required (name of existing sketch feature, e.g. 'Sketch1').");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                if (modelDoc.SketchManager.ActiveSketch != null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_ALREADY_ACTIVE",
                        "A sketch is already active. Exit the current sketch first (call extrude_feature or close the sketch).");

                bool selected = modelDoc.Extension.SelectByID2(sketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                if (!selected)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_NOT_FOUND",
                        $"Sketch '{sketchName}' not found. Ensure the name matches exactly (e.g. 'Sketch1').");

                modelDoc.EditSketch();

                var activeSketch = modelDoc.SketchManager.ActiveSketch;
                if (activeSketch == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SKETCH_EDIT_FAILED",
                        $"EditSketch() was called but no sketch became active. Ensure '{sketchName}' is a valid sketch feature.");

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
                        ActiveSketch = sketchName,
                        Features = new List<string>(),
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

        /// <summary>
        /// CreateFillet/CreateChamfer apply the corner to the model, but the new sketch segment
        /// isn't regenerated/painted until the user clicks in the view. EditRebuild3 (Ctrl+B)
        /// regenerates it, but exits the active sketch — so we re-enter edit mode on the same
        /// sketch by name. Net effect: the fillet/chamfer appears immediately and the user stays
        /// in the sketch.
        /// </summary>
        private void RegenerateSketchInPlace(IModelDoc2 modelDoc, string sketchName)
        {
            modelDoc.EditRebuild3();
            if (!string.IsNullOrEmpty(sketchName) && modelDoc.SketchManager.ActiveSketch == null)
            {
                modelDoc.ClearSelection2(true);
                if (modelDoc.Extension.SelectByID2(sketchName, "SKETCH", 0, 0, 0, false, 0, null, 0))
                    modelDoc.EditSketch();
                modelDoc.ClearSelection2(true);
            }
            modelDoc.GraphicsRedraw2();
        }

        // -----------------------------------------------------------------------
        // The active sketch's feature NAME.
        //
        // SketchManager.ActiveSketch is typed Sketch and ISketch carries neither a name nor a
        // back-pointer to its feature (reflection over SolidWorks.Interop.sldworks v33), so the name
        // has to come from the sketch's FEATURE. The cast that looks wrong on paper is the one that
        // works: SolidWorks' live sketch object answers IFeature through COM QueryInterface, so
        // `ActiveSketch as IFeature` returns the owning feature — the same mechanism verify_state has
        // always used for its activeSketch field, and the one proven against a live session by this
        // batch. An IUnknown-identity scan matching each Sketch feature's GetSpecificFeature2() was
        // tried first and did NOT resolve, so it is deliberately not kept as a fallback.
        //
        // The NAME is what every by-name consumer needs: create_sketch's activeSketch echo (the
        // compiler reads exactly that field as a sketch node's runtime name, so while the echo was a
        // "Sketch" placeholder a sweep or loft could never name its own sketches), the sweep profile
        // selection and its PROFILE_NOT_FOUND guard, and the by-name re-selects that boss, cut and
        // revolve do after leaving a sketch.
        private static string ActiveSketchName(IModelDoc2 modelDoc)
        {
            object active;
            try { active = modelDoc.SketchManager.ActiveSketch; }
            catch (COMException) { return null; }
            var feat = active as IFeature;
            if (feat == null) return null;
            try { return feat.Name; }
            catch (COMException) { return null; }
        }

        // -----------------------------------------------------------------------
        // Two-tier parametric sketch constraint & dimension engine (pre-exit).
        //
        // Why this exists: entities are drawn under `SketchManager.AddToDB = true` so pixel-level
        // snapping never collapses short segments, which leaves raw geometry under-defined (`(-)`
        // blue lines that deform when dragged in SolidWorks). Right before exiting an active 2D
        // model sketch, we:
        //   Tier 1 — recognize canonical primitives (circles/bores, axis-aligned rectangles) and
        //            apply clean engineering relations + driving dimensions anchored to the origin;
        //   Tier 2 — reconstruct intrinsic geometric relations (endpoint coincidence, horizontal,
        //            vertical, line-arc / arc-arc tangency, equal-radius fillets/slots, origin/axis
        //            anchoring) and invoke `ISketchManager.FullyDefineSketch` from the origin datum;
        //   Guard  — verify `GetConstrainedStatus()` never stays over-defined/unsolved and segment
        //            coordinates never drift (> 0.01 mm), rolling back added relations/dimensions
        //            on any solver anomaly so geometry creation never regresses.
        // -----------------------------------------------------------------------

        // Compile-time constants for SolidWorks enum values (ADR-018: no runtime swconst load).
        private const int SW_DOC_PART = 1;
        private const int SW_DOC_ASSEMBLY = 2;
        private const int SW_SKETCH_LINE = 0;
        private const int SW_SKETCH_ARC = 1;
        private const int SW_SKETCH_ELLIPSE = 2;
        private const int SW_SKETCH_SPLINE = 3;
        private const int SW_POINT_ORIGIN = 8;
        private const int SW_CONSTRAINED_UNDER = 2;
        private const int SW_CONSTRAINED_FULLY = 3;
        private const int SW_CONSTRAINED_OVER = 4;
        private const int SW_CONSTRAINED_NO_SOLUTION = 5;
        private const int SW_CONSTRAINED_INVALID = 6;
        private const int SW_REL_FILTER_OVERDEFINING = 2;
        private const int SW_FD_REL_ALL_GEOMETRIC =
            1   /* Equal */ |
            2   /* Horizontal */ |
            4   /* Vertical */ |
            8   /* Tangent */ |
            16  /* Perpendicular */ |
            32  /* Colinear */ |
            64  /* Concentric */ |
            128 /* Parallel */ |
            512 /* Coincident */;

        private const double COORD_TOL = 1e-7;
        private const double CONNECT_TOL = 1e-6;
        private const double TANGENT_SIN_TOL = 1e-4;
        private const double DRIFT_TOL = 1e-5; // 0.01 mm max allowed solver drift

        private sealed class SegmentSnapshot
        {
            public ISketchSegment Segment;
            public int SegType;
            public bool IsCircle;
            public double X1, Y1, X2, Y2, Cx, Cy, Radius;
        }

        internal void AutoConstrainActiveSketch(IModelDoc2 modelDoc)
        {
            if (modelDoc == null) return;
            try
            {
                int docType = modelDoc.GetType();
                if (docType != SW_DOC_PART && docType != SW_DOC_ASSEMBLY) return;

                var sk = modelDoc.SketchManager?.ActiveSketch as ISketch;
                if (sk == null) return;

                int statusBefore = SafeConstrainedStatus(sk);
                if (statusBefore == SW_CONSTRAINED_FULLY) return;

                var rawSegs = sk.GetSketchSegments() as object[];
                if (rawSegs == null || rawSegs.Length == 0) return;

                string sketchName = ActiveSketchName(modelDoc) ?? "Sketch";
                var snapshot = CaptureSketchSnapshot(rawSegs);
                if (snapshot.Count == 0) return;

                var realLines = new List<ISketchLine>();
                var allLines = new List<ISketchLine>();
                var realCircles = new List<ISketchArc>();
                var realArcs = new List<ISketchArc>();
                var freeformSegs = new List<ISketchSegment>();
                int realCount = 0;

                foreach (var o in rawSegs)
                {
                    var seg = o as ISketchSegment;
                    if (seg == null) continue;
                    bool isCon = false;
                    try { isCon = seg.ConstructionGeometry; } catch { }
                    int st = -1;
                    try { st = seg.GetType(); } catch { }

                    if (st == SW_SKETCH_LINE)
                    {
                        var ln = seg as ISketchLine;
                        if (ln != null)
                        {
                            allLines.Add(ln);
                            if (!isCon) { realLines.Add(ln); realCount++; }
                        }
                    }
                    else if (st == SW_SKETCH_ARC)
                    {
                        var arc = seg as ISketchArc;
                        if (arc != null && !isCon)
                        {
                            realCount++;
                            if (arc.IsCircle() == 1) realCircles.Add(arc);
                            else realArcs.Add(arc);
                        }
                    }
                    else if (!isCon)
                    {
                        realCount++;
                        if (st == SW_SKETCH_SPLINE || st == SW_SKETCH_ELLIPSE)
                            freeformSegs.Add(seg);
                    }
                }

                if (realCount == 0 && allLines.Count == 0) return;

                double[] s2m = GetSketchToModelArray(sk);
                string originExtName = FindOriginExtSketchPointName(modelDoc);
                ISketchPoint originSketchPt = FindBuiltInSketchOriginPoint(sk);
                var addedDims = new List<IDisplayDimension>();

                bool prevAddToDb = modelDoc.SketchManager.AddToDB;
                bool prevInputDimVal = false;
                bool toggledInputDim = false;
                try
                {
                    prevInputDimVal = _solidWorks.GetUserPreferenceToggle(
                        (int)swUserPreferenceToggle_e.swInputDimValOnCreate);
                    _solidWorks.SetUserPreferenceToggle(
                        (int)swUserPreferenceToggle_e.swInputDimValOnCreate, false);
                    toggledInputDim = true;
                }
                catch { }

                modelDoc.SketchManager.AddToDB = false;
                try
                {
                    bool handledByCanonical = false;

                    // ---- Tier 1A: Pure circle sketch(es) (holes, bores, bosses, concentric rings) ----
                    if (realCount > 0 && realCircles.Count == realCount && allLines.Count == realLines.Count)
                    {
                        ConstrainCanonicalCircles(
                            modelDoc, sk, snapshot, s2m, realCircles,
                            originExtName, originSketchPt, addedDims);
                        handledByCanonical = SafeConstrainedStatus(sk) == SW_CONSTRAINED_FULLY;
                    }
                    // ---- Tier 1B: Canonical axis-aligned rectangle (box base, rectangular pocket/boss) ----
                    else if (realCount == 4 && realLines.Count == 4 && allLines.Count == 4)
                    {
                        if (TryConstrainCanonicalRectangle(
                            modelDoc, sk, snapshot, s2m, realLines,
                            originExtName, originSketchPt, addedDims))
                        {
                            handledByCanonical = SafeConstrainedStatus(sk) == SW_CONSTRAINED_FULLY;
                        }
                    }

                    // ---- Tier 2: General multi-segment contours (slots, rounded rects, arbitrary line/arc profiles, revolve sections) ----
                    if (!handledByCanonical && SafeConstrainedStatus(sk) != SW_CONSTRAINED_FULLY)
                    {
                        if (rawSegs.Length <= 80 && freeformSegs.Count == 0)
                        {
                            ApplyGeometricRelations(
                                modelDoc, sk, snapshot, allLines, realLines, realArcs, realCircles,
                                originExtName, originSketchPt);
                        }

                        if (SafeConstrainedStatus(sk) == SW_CONSTRAINED_UNDER)
                        {
                            TryRunFullyDefineSketch(modelDoc, sk, snapshot, originExtName, originSketchPt);
                        }

                        // Splines / ellipses have control-polygon handles that FullyDefineSketch does not dimension;
                        // lock remaining under-defined freeform curves so the profile cannot be dragged out of shape.
                        if (freeformSegs.Count > 0 && SafeConstrainedStatus(sk) == SW_CONSTRAINED_UNDER)
                        {
                            foreach (var fseg in freeformSegs)
                            {
                                TryApplyConstraint(modelDoc, sk, snapshot, "sgFIXED", () => fseg.Select4(false, null));
                            }
                        }
                    }

                    // ---- Final health & zero-drift safety guard ----
                    int statusAfter = SafeConstrainedStatus(sk);
                    if (IsBadConstrainedStatus(statusAfter))
                    {
                        CleanupOverDefiningRelations(sk);
                        statusAfter = SafeConstrainedStatus(sk);
                    }

                    if (IsBadConstrainedStatus(statusAfter) || HasGeometryDrifted(snapshot))
                    {
                        ExecLog.Write($"auto_constrain '{sketchName}': rollback triggered (status={statusAfter}, drifted={HasGeometryDrifted(snapshot)})");
                        RollbackAddedConstraintsAndDimensions(modelDoc, sk, addedDims);
                        RestoreSnapshotGeometryIfNeeded(snapshot);
                    }
                    else
                    {
                        ExecLog.Write($"auto_constrain '{sketchName}': status {statusBefore} -> {statusAfter} (segs={rawSegs.Length}, dims={addedDims.Count})");
                    }
                }
                finally
                {
                    try { modelDoc.ClearSelection2(true); } catch { }
                    try { modelDoc.SketchManager.AddToDB = prevAddToDb; } catch { }
                    if (toggledInputDim)
                    {
                        try
                        {
                            _solidWorks.SetUserPreferenceToggle(
                                (int)swUserPreferenceToggle_e.swInputDimValOnCreate, prevInputDimVal);
                        }
                        catch { }
                    }
                }
            }
            catch (Exception ex)
            {
                try { modelDoc.ClearSelection2(true); } catch { }
                ExecLog.Write($"auto_constrain skipped on exception: {ex.Message}");
            }
        }

        private void ConstrainCanonicalCircles(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            double[] s2m,
            List<ISketchArc> circles,
            string originExtName,
            ISketchPoint originSketchPt,
            List<IDisplayDimension> addedDims)
        {
            var placedCenters = new List<ISketchArc>();
            foreach (var arc in circles)
            {
                var cp = arc.IGetCenterPoint2();
                if (cp == null) continue;
                double cx = cp.X, cy = cp.Y;
                double r = arc.GetRadius();

                bool centerLocked = false;
                if (Math.Abs(cx) <= COORD_TOL && Math.Abs(cy) <= COORD_TOL)
                {
                    centerLocked = TryApplyPointToOriginConstraint(
                        modelDoc, sk, snapshot, cp, originExtName, originSketchPt, "sgCOINCIDENT");
                }
                else
                {
                    foreach (var prev in placedCenters)
                    {
                        var pcp = prev.IGetCenterPoint2();
                        if (pcp != null && Hypot2(cx - pcp.X, cy - pcp.Y) <= COORD_TOL)
                        {
                            var segA = arc as ISketchSegment;
                            var segB = prev as ISketchSegment;
                            centerLocked = TryApplyConstraint(modelDoc, sk, snapshot, "sgCONCENTRIC", () =>
                                segA != null && segB != null && segA.Select4(false, null) && segB.Select4(true, null));
                            if (centerLocked) break;
                        }
                    }

                    if (!centerLocked)
                    {
                        if (Math.Abs(cy) <= COORD_TOL)
                        {
                            TryApplyPointToOriginConstraint(
                                modelDoc, sk, snapshot, cp, originExtName, originSketchPt, "sgHORIZONTALPOINTS2D");
                            TryApplyPointToOriginDimension(
                                modelDoc, sk, snapshot, s2m, cp, originExtName, originSketchPt,
                                true, cx * 0.5, -Math.Max(r * 1.2, 0.005), addedDims);
                        }
                        else if (Math.Abs(cx) <= COORD_TOL)
                        {
                            TryApplyPointToOriginConstraint(
                                modelDoc, sk, snapshot, cp, originExtName, originSketchPt, "sgVERTICALPOINTS2D");
                            TryApplyPointToOriginDimension(
                                modelDoc, sk, snapshot, s2m, cp, originExtName, originSketchPt,
                                false, -Math.Max(r * 1.2, 0.005), cy * 0.5, addedDims);
                        }
                        else
                        {
                            TryApplyPointToOriginDimension(
                                modelDoc, sk, snapshot, s2m, cp, originExtName, originSketchPt,
                                true, cx * 0.5, Math.Min(0.0, cy) - Math.Max(r * 1.2, 0.005), addedDims);
                            TryApplyPointToOriginDimension(
                                modelDoc, sk, snapshot, s2m, cp, originExtName, originSketchPt,
                                false, Math.Min(0.0, cx) - Math.Max(r * 1.2, 0.005), cy * 0.5, addedDims);
                        }
                    }
                }

                var arcSeg = arc as ISketchSegment;
                if (arcSeg != null)
                {
                    TryApplySegmentDimension(
                        modelDoc, sk, snapshot, s2m, arcSeg,
                        cx + r * 0.85, cy + r * 0.85, addedDims);
                }
                placedCenters.Add(arc);
            }
        }

        private bool TryConstrainCanonicalRectangle(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            double[] s2m,
            List<ISketchLine> lines,
            string originExtName,
            ISketchPoint originSketchPt,
            List<IDisplayDimension> addedDims)
        {
            var horiz = new List<ISketchLine>();
            var vert = new List<ISketchLine>();
            foreach (var ln in lines)
            {
                var p1 = ln.IGetStartPoint2();
                var p2 = ln.IGetEndPoint2();
                if (p1 == null || p2 == null) return false;
                double dx = Math.Abs(p2.X - p1.X), dy = Math.Abs(p2.Y - p1.Y);
                if (dy <= COORD_TOL && dx > CONNECT_TOL) horiz.Add(ln);
                else if (dx <= COORD_TOL && dy > CONNECT_TOL) vert.Add(ln);
                else return false;
            }
            if (horiz.Count != 2 || vert.Count != 2) return false;

            // Sort bottom/top and left/right.
            if (LineMidY(horiz[0]) > LineMidY(horiz[1]))
            {
                var tmp = horiz[0]; horiz[0] = horiz[1]; horiz[1] = tmp;
            }
            if (LineMidX(vert[0]) > LineMidX(vert[1]))
            {
                var tmp = vert[0]; vert[0] = vert[1]; vert[1] = tmp;
            }

            ISketchLine bot = horiz[0], top = horiz[1], left = vert[0], right = vert[1];
            double minX = LineMidX(left), maxX = LineMidX(right);
            double minY = LineMidY(bot), maxY = LineMidY(top);
            double w = maxX - minX, h = maxY - minY;
            if (w <= CONNECT_TOL || h <= CONNECT_TOL) return false;
            double cx = 0.5 * (minX + maxX), cy = 0.5 * (minY + maxY);

            // Ensure corner coincidences and H/V relations.
            CoincideNearbyEndpoints(modelDoc, sk, snapshot, lines, null);
            foreach (var hl in horiz)
            {
                var seg = hl as ISketchSegment;
                if (seg != null) TryApplyConstraint(modelDoc, sk, snapshot, "sgHORIZONTAL2D", () => seg.Select4(false, null));
            }
            foreach (var vl in vert)
            {
                var seg = vl as ISketchSegment;
                if (seg != null) TryApplyConstraint(modelDoc, sk, snapshot, "sgVERTICAL2D", () => seg.Select4(false, null));
            }

            // Overall width & height driving dimensions.
            double offX = Math.Max(w * 0.18, 0.005);
            double offY = Math.Max(h * 0.18, 0.005);
            TryApplySegmentDimension(modelDoc, sk, snapshot, s2m, bot as ISketchSegment, cx, minY - offY, addedDims);
            TryApplySegmentDimension(modelDoc, sk, snapshot, s2m, left as ISketchSegment, minX - offX, cy, addedDims);

            // Anchor rectangle position to the sketch origin without adding stray construction lines
            // (which could confuse downstream revolve-axis auto-detection).
            var corners = CollectUniquePoints(lines, null);
            ISketchPoint cornerOnOrigin = null;
            foreach (var pt in corners)
            {
                if (Math.Abs(pt.X) <= COORD_TOL && Math.Abs(pt.Y) <= COORD_TOL)
                {
                    cornerOnOrigin = pt;
                    break;
                }
            }

            if (cornerOnOrigin != null)
            {
                TryApplyPointToOriginConstraint(
                    modelDoc, sk, snapshot, cornerOnOrigin, originExtName, originSketchPt, "sgCOINCIDENT");
            }
            else if (Math.Abs(cx) <= COORD_TOL && Math.Abs(minY) <= COORD_TOL)
            {
                TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, bot as ISketchSegment, originExtName, originSketchPt, "sgATMIDDLE");
            }
            else if (Math.Abs(cx) <= COORD_TOL && Math.Abs(maxY) <= COORD_TOL)
            {
                TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, top as ISketchSegment, originExtName, originSketchPt, "sgATMIDDLE");
            }
            else if (Math.Abs(cy) <= COORD_TOL && Math.Abs(minX) <= COORD_TOL)
            {
                TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, left as ISketchSegment, originExtName, originSketchPt, "sgATMIDDLE");
            }
            else if (Math.Abs(cy) <= COORD_TOL && Math.Abs(maxX) <= COORD_TOL)
            {
                TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, right as ISketchSegment, originExtName, originSketchPt, "sgATMIDDLE");
            }
            else
            {
                // Pick bottom-left corner to anchor X and Y position to origin.
                ISketchPoint bl = FindCornerNear(corners, minX, minY);
                if (bl != null)
                {
                    if (Math.Abs(minX) <= COORD_TOL)
                        TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, left as ISketchSegment, originExtName, originSketchPt, "sgCOINCIDENT");
                    else
                        TryApplyPointToOriginDimension(
                            modelDoc, sk, snapshot, s2m, bl, originExtName, originSketchPt,
                            true, 0.5 * minX, minY - offY * 1.8, addedDims);

                    if (Math.Abs(minY) <= COORD_TOL)
                        TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, bot as ISketchSegment, originExtName, originSketchPt, "sgCOINCIDENT");
                    else
                        TryApplyPointToOriginDimension(
                            modelDoc, sk, snapshot, s2m, bl, originExtName, originSketchPt,
                            false, minX - offX * 1.8, 0.5 * minY, addedDims);
                }
            }
            return true;
        }

        private void ApplyGeometricRelations(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            List<ISketchLine> allLines,
            List<ISketchLine> realLines,
            List<ISketchArc> realArcs,
            List<ISketchArc> realCircles,
            string originExtName,
            ISketchPoint originSketchPt)
        {
            // 1) Weld coincident endpoints created under AddToDB.
            CoincideNearbyEndpoints(modelDoc, sk, snapshot, allLines, realArcs);

            // 2) Horizontal & Vertical relations on all lines (including revolve construction axes).
            foreach (var ln in allLines)
            {
                var p1 = ln.IGetStartPoint2();
                var p2 = ln.IGetEndPoint2();
                var seg = ln as ISketchSegment;
                if (p1 == null || p2 == null || seg == null) continue;
                double dx = Math.Abs(p2.X - p1.X), dy = Math.Abs(p2.Y - p1.Y);
                if (dy <= COORD_TOL && dx > CONNECT_TOL)
                {
                    TryApplyConstraint(modelDoc, sk, snapshot, "sgHORIZONTAL2D", () => seg.Select4(false, null));
                    if (Math.Abs(p1.Y) <= COORD_TOL)
                        TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, seg, originExtName, originSketchPt, "sgCOINCIDENT");
                }
                else if (dx <= COORD_TOL && dy > CONNECT_TOL)
                {
                    TryApplyConstraint(modelDoc, sk, snapshot, "sgVERTICAL2D", () => seg.Select4(false, null));
                    if (Math.Abs(p1.X) <= COORD_TOL)
                        TryApplySegmentToOriginConstraint(modelDoc, sk, snapshot, seg, originExtName, originSketchPt, "sgCOINCIDENT");
                }
            }

            // 3) Line-Arc tangency at shared endpoints (slots, rounded rects, filleted/tangent contours).
            foreach (var arc in realArcs)
            {
                var arcSeg = arc as ISketchSegment;
                var cp = arc.IGetCenterPoint2();
                var aStart = arc.IGetStartPoint2();
                var aEnd = arc.IGetEndPoint2();
                if (arcSeg == null || cp == null || aStart == null || aEnd == null) continue;
                double r = arc.GetRadius();
                if (r <= CONNECT_TOL) continue;

                foreach (var ln in realLines)
                {
                    var lnSeg = ln as ISketchSegment;
                    var l1 = ln.IGetStartPoint2();
                    var l2 = ln.IGetEndPoint2();
                    if (lnSeg == null || l1 == null || l2 == null) continue;
                    double ldx = l2.X - l1.X, ldy = l2.Y - l1.Y;
                    double llen = Hypot2(ldx, ldy);
                    if (llen <= CONNECT_TOL) continue;
                    double ux = ldx / llen, uy = ldy / llen;

                    ISketchPoint joint = null;
                    if (Hypot2(aStart.X - l1.X, aStart.Y - l1.Y) <= CONNECT_TOL ||
                        Hypot2(aStart.X - l2.X, aStart.Y - l2.Y) <= CONNECT_TOL)
                        joint = aStart;
                    else if (Hypot2(aEnd.X - l1.X, aEnd.Y - l1.Y) <= CONNECT_TOL ||
                             Hypot2(aEnd.X - l2.X, aEnd.Y - l2.Y) <= CONNECT_TOL)
                        joint = aEnd;

                    if (joint != null)
                    {
                        double rx = (joint.X - cp.X) / r, ry = (joint.Y - cp.Y) / r;
                        // Line tangent to circle <=> line unit vector is perpendicular to radius vector at joint.
                        if (Math.Abs(ux * rx + uy * ry) <= TANGENT_SIN_TOL)
                        {
                            TryApplyConstraint(modelDoc, sk, snapshot, "sgTANGENT", () =>
                                lnSeg.Select4(false, null) && arcSeg.Select4(true, null));
                        }
                    }
                }
            }

            // 4) Arc-Arc tangency at shared endpoints & equal-radius grouping (slots / rounded rectangles).
            for (int i = 0; i < realArcs.Count; i++)
            {
                var a1 = realArcs[i];
                var s1 = a1 as ISketchSegment;
                var c1 = a1.IGetCenterPoint2();
                var p1a = a1.IGetStartPoint2();
                var p1b = a1.IGetEndPoint2();
                if (s1 == null || c1 == null || p1a == null || p1b == null) continue;
                double r1 = a1.GetRadius();
                if (r1 <= CONNECT_TOL) continue;

                for (int j = i + 1; j < realArcs.Count; j++)
                {
                    var a2 = realArcs[j];
                    var s2 = a2 as ISketchSegment;
                    var c2 = a2.IGetCenterPoint2();
                    var p2a = a2.IGetStartPoint2();
                    var p2b = a2.IGetEndPoint2();
                    if (s2 == null || c2 == null || p2a == null || p2b == null) continue;
                    double r2 = a2.GetRadius();
                    if (r2 <= CONNECT_TOL) continue;

                    ISketchPoint joint = null;
                    if (Hypot2(p1a.X - p2a.X, p1a.Y - p2a.Y) <= CONNECT_TOL ||
                        Hypot2(p1a.X - p2b.X, p1a.Y - p2b.Y) <= CONNECT_TOL)
                        joint = p1a;
                    else if (Hypot2(p1b.X - p2a.X, p1b.Y - p2a.Y) <= CONNECT_TOL ||
                             Hypot2(p1b.X - p2b.X, p1b.Y - p2b.Y) <= CONNECT_TOL)
                        joint = p1b;

                    if (joint != null)
                    {
                        double r1x = (joint.X - c1.X) / r1, r1y = (joint.Y - c1.Y) / r1;
                        double r2x = (joint.X - c2.X) / r2, r2y = (joint.Y - c2.Y) / r2;
                        if (Math.Abs(r1x * r2y - r1y * r2x) <= TANGENT_SIN_TOL)
                        {
                            TryApplyConstraint(modelDoc, sk, snapshot, "sgTANGENT", () =>
                                s1.Select4(false, null) && s2.Select4(true, null));
                        }
                    }
                }
            }

            // Equal radius on matching-radius arcs in slots (2 arcs) and rounded rectangles (4 arcs).
            if (realArcs.Count >= 2 && realArcs.Count <= 8)
            {
                var baseArc = realArcs[0];
                var baseSeg = baseArc as ISketchSegment;
                double baseR = baseArc.GetRadius();
                if (baseSeg != null && baseR > CONNECT_TOL)
                {
                    for (int i = 1; i < realArcs.Count; i++)
                    {
                        if (Math.Abs(realArcs[i].GetRadius() - baseR) <= COORD_TOL)
                        {
                            var otherSeg = realArcs[i] as ISketchSegment;
                            if (otherSeg != null)
                            {
                                TryApplyConstraint(modelDoc, sk, snapshot, "sgSAMELENGTH", () =>
                                    baseSeg.Select4(false, null) && otherSeg.Select4(true, null));
                            }
                        }
                    }
                }
            }

            // Angled slot parallel sides (2 non-axis-aligned parallel lines + 2 arcs).
            if (realLines.Count == 2 && realArcs.Count == 2)
            {
                var lA = realLines[0]; var lB = realLines[1];
                var pA1 = lA.IGetStartPoint2(); var pA2 = lA.IGetEndPoint2();
                var pB1 = lB.IGetStartPoint2(); var pB2 = lB.IGetEndPoint2();
                if (pA1 != null && pA2 != null && pB1 != null && pB2 != null)
                {
                    double dxA = pA2.X - pA1.X, dyA = pA2.Y - pA1.Y;
                    double dxB = pB2.X - pB1.X, dyB = pB2.Y - pB1.Y;
                    double lenA = Hypot2(dxA, dyA), lenB = Hypot2(dxB, dyB);
                    bool axisAligned = Math.Abs(dxA) <= COORD_TOL || Math.Abs(dyA) <= COORD_TOL;
                    if (!axisAligned && lenA > CONNECT_TOL && lenB > CONNECT_TOL)
                    {
                        double cross = Math.Abs((dxA * dyB - dyA * dxB) / (lenA * lenB));
                        if (cross <= TANGENT_SIN_TOL)
                        {
                            var sA = lA as ISketchSegment; var sB = lB as ISketchSegment;
                            if (sA != null && sB != null)
                            {
                                TryApplyConstraint(modelDoc, sk, snapshot, "sgPARALLEL", () =>
                                    sA.Select4(false, null) && sB.Select4(true, null));
                            }
                        }
                    }
                }
            }

            // 5) Anchor any vertex or arc/circle center sitting directly at (0, 0) to the sketch origin.
            foreach (var pt in CollectUniquePoints(allLines, realArcs))
            {
                if (Math.Abs(pt.X) <= COORD_TOL && Math.Abs(pt.Y) <= COORD_TOL)
                {
                    TryApplyPointToOriginConstraint(modelDoc, sk, snapshot, pt, originExtName, originSketchPt, "sgCOINCIDENT");
                    break;
                }
            }
            foreach (var arc in realArcs)
            {
                var cp = arc.IGetCenterPoint2();
                if (cp != null && Math.Abs(cp.X) <= COORD_TOL && Math.Abs(cp.Y) <= COORD_TOL)
                {
                    TryApplyPointToOriginConstraint(modelDoc, sk, snapshot, cp, originExtName, originSketchPt, "sgCOINCIDENT");
                }
            }
            foreach (var cir in realCircles)
            {
                var cp = cir.IGetCenterPoint2();
                if (cp != null && Math.Abs(cp.X) <= COORD_TOL && Math.Abs(cp.Y) <= COORD_TOL)
                {
                    TryApplyPointToOriginConstraint(modelDoc, sk, snapshot, cp, originExtName, originSketchPt, "sgCOINCIDENT");
                }
            }
        }

        private void TryRunFullyDefineSketch(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            string originExtName,
            ISketchPoint originSketchPt)
        {
            modelDoc.ClearSelection2(true);
            // Official SolidWorks API convention (Fully_Define_Underdefined_Sketch_Example_CSharp):
            // Mark 2 = Horizontal datum, Mark 4 = Vertical datum -> OR'ed Mark 6 selects the single origin point for both.
            bool datumSelected = SelectOriginWithMark(modelDoc, originExtName, originSketchPt, false, 6);

            // HorizontalDimScheme / VerticalDimScheme: 1 = Baseline (0 = Chain, 2 = Ordinate).
            // HorizontalDimPlacement / VerticalDimPlacement: 0 = Above / Right.
            modelDoc.SketchManager.FullyDefineSketch(
                true,
                true,
                SW_FD_REL_ALL_GEOMETRIC,
                true,
                1,
                null,
                1,
                null,
                0,
                0);

            int st = SafeConstrainedStatus(sk);
            if (IsBadConstrainedStatus(st))
            {
                CleanupOverDefiningRelations(sk);
                st = SafeConstrainedStatus(sk);
            }

            // Fallback when an oblique face sketch could not bind the global Origin as Mark-6 datum:
            // pick the first sketch point, lock it if needed, and re-run FullyDefineSketch from that local datum.
            if (st == SW_CONSTRAINED_UNDER && !datumSelected)
            {
                var pts = sk.GetSketchPoints2() as object[];
                ISketchPoint localAnchor = null;
                if (pts != null)
                {
                    foreach (var o in pts)
                    {
                        var p = o as ISketchPoint;
                        if (p != null) { localAnchor = p; break; }
                    }
                }
                if (localAnchor != null)
                {
                    TryApplyConstraint(modelDoc, sk, snapshot, "sgFIXED", () => localAnchor.Select4(false, null));
                    modelDoc.ClearSelection2(true);
                    if (SelectPointWithMark(modelDoc, localAnchor, false, 6))
                    {
                        modelDoc.SketchManager.FullyDefineSketch(
                            true, true, SW_FD_REL_ALL_GEOMETRIC, true,
                            1, null, 1, null, 0, 0);
                        if (IsBadConstrainedStatus(SafeConstrainedStatus(sk)))
                            CleanupOverDefiningRelations(sk);
                    }
                }
            }
        }

        private void CoincideNearbyEndpoints(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            List<ISketchLine> lines,
            List<ISketchArc> arcs)
        {
            var pts = new List<ISketchPoint>();
            if (lines != null)
            {
                foreach (var ln in lines)
                {
                    var p1 = ln.IGetStartPoint2();
                    var p2 = ln.IGetEndPoint2();
                    if (p1 != null) pts.Add(p1);
                    if (p2 != null) pts.Add(p2);
                }
            }
            if (arcs != null)
            {
                foreach (var a in arcs)
                {
                    var p1 = a.IGetStartPoint2();
                    var p2 = a.IGetEndPoint2();
                    if (p1 != null) pts.Add(p1);
                    if (p2 != null) pts.Add(p2);
                }
            }

            for (int i = 0; i < pts.Count; i++)
            {
                var pa = pts[i];
                for (int j = i + 1; j < pts.Count; j++)
                {
                    var pb = pts[j];
                    if (ReferenceEquals(pa, pb)) continue;
                    if (Hypot2(pa.X - pb.X, pa.Y - pb.Y) <= COORD_TOL)
                    {
                        TryApplyConstraint(modelDoc, sk, snapshot, "sgCOINCIDENT", () =>
                            pa.Select4(false, null) && pb.Select4(true, null));
                    }
                }
            }
        }

        private static List<ISketchPoint> CollectUniquePoints(List<ISketchLine> lines, List<ISketchArc> arcs)
        {
            var result = new List<ISketchPoint>();
            void AddIfDistinct(ISketchPoint p)
            {
                if (p == null) return;
                foreach (var ex in result)
                {
                    if (ReferenceEquals(ex, p) || Hypot2(ex.X - p.X, ex.Y - p.Y) <= COORD_TOL)
                        return;
                }
                result.Add(p);
            }
            if (lines != null)
            {
                foreach (var ln in lines)
                {
                    AddIfDistinct(ln.IGetStartPoint2());
                    AddIfDistinct(ln.IGetEndPoint2());
                }
            }
            if (arcs != null)
            {
                foreach (var a in arcs)
                {
                    AddIfDistinct(a.IGetStartPoint2());
                    AddIfDistinct(a.IGetEndPoint2());
                }
            }
            return result;
        }

        private static ISketchPoint FindCornerNear(List<ISketchPoint> pts, double x, double y)
        {
            ISketchPoint best = null;
            double bestDist = double.MaxValue;
            foreach (var p in pts)
            {
                double d = Hypot2(p.X - x, p.Y - y);
                if (d < bestDist) { bestDist = d; best = p; }
            }
            return bestDist <= 1e-4 ? best : null;
        }

        private bool TryApplyPointToOriginConstraint(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            ISketchPoint pt,
            string originExtName,
            ISketchPoint originSketchPt,
            string sgConstraintId)
        {
            if (pt == null) return false;
            return TryApplyConstraint(modelDoc, sk, snapshot, sgConstraintId, () =>
                pt.Select4(false, null) && SelectOriginWithMark(modelDoc, originExtName, originSketchPt, true, 0));
        }

        private bool TryApplySegmentToOriginConstraint(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            ISketchSegment seg,
            string originExtName,
            ISketchPoint originSketchPt,
            string sgConstraintId)
        {
            if (seg == null) return false;
            return TryApplyConstraint(modelDoc, sk, snapshot, sgConstraintId, () =>
                seg.Select4(false, null) && SelectOriginWithMark(modelDoc, originExtName, originSketchPt, true, 0));
        }

        private bool TryApplyConstraint(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            string sgConstraintId,
            Func<bool> selectEntities)
        {
            try
            {
                modelDoc.ClearSelection2(true);
                if (!selectEntities()) return false;
                modelDoc.SketchAddConstraints(sgConstraintId);
                int st = SafeConstrainedStatus(sk);
                if (IsBadConstrainedStatus(st) || HasGeometryDrifted(snapshot))
                {
                    CleanupOverDefiningRelations(sk);
                    return !IsBadConstrainedStatus(SafeConstrainedStatus(sk)) && !HasGeometryDrifted(snapshot);
                }
                return true;
            }
            catch
            {
                return false;
            }
        }

        private bool TryApplySegmentDimension(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            double[] s2m,
            ISketchSegment seg,
            double labelU,
            double labelV,
            List<IDisplayDimension> addedDims)
        {
            if (seg == null || SafeConstrainedStatus(sk) == SW_CONSTRAINED_FULLY) return false;
            try
            {
                modelDoc.ClearSelection2(true);
                if (!seg.Select4(false, null)) return false;
                SketchPointToModel(s2m, labelU, labelV, out double mx, out double my, out double mz);
                var dispDim = modelDoc.AddDimension2(mx, my, mz) as IDisplayDimension;
                if (dispDim == null) return false;

                int st = SafeConstrainedStatus(sk);
                if (IsBadConstrainedStatus(st) || HasGeometryDrifted(snapshot))
                {
                    DeleteDisplayDimension(modelDoc, dispDim);
                    return false;
                }
                addedDims?.Add(dispDim);
                return true;
            }
            catch
            {
                return false;
            }
        }

        private bool TryApplyPointToOriginDimension(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<SegmentSnapshot> snapshot,
            double[] s2m,
            ISketchPoint pt,
            string originExtName,
            ISketchPoint originSketchPt,
            bool alongX,
            double labelU,
            double labelV,
            List<IDisplayDimension> addedDims)
        {
            if (pt == null || SafeConstrainedStatus(sk) == SW_CONSTRAINED_FULLY) return false;
            try
            {
                modelDoc.ClearSelection2(true);
                if (!pt.Select4(false, null)) return false;
                if (!SelectOriginWithMark(modelDoc, originExtName, originSketchPt, true, 0)) return false;
                SketchPointToModel(s2m, labelU, labelV, out double mx, out double my, out double mz);
                var dispDim = (alongX
                    ? modelDoc.SketchManager.AddAlongXDimension(mx, my, mz)
                    : modelDoc.SketchManager.AddAlongYDimension(mx, my, mz)) as IDisplayDimension;
                if (dispDim == null) return false;

                int st = SafeConstrainedStatus(sk);
                if (IsBadConstrainedStatus(st) || HasGeometryDrifted(snapshot))
                {
                    DeleteDisplayDimension(modelDoc, dispDim);
                    return false;
                }
                addedDims?.Add(dispDim);
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static bool SelectOriginWithMark(
            IModelDoc2 modelDoc,
            string originExtName,
            ISketchPoint originSketchPt,
            bool append,
            int mark)
        {
            if (!string.IsNullOrEmpty(originExtName))
            {
                try
                {
                    if (modelDoc.Extension.SelectByID2(originExtName, "EXTSKETCHPOINT", 0, 0, 0, append, mark, null, 0))
                        return true;
                }
                catch { }
            }
            if (originSketchPt != null)
                return SelectPointWithMark(modelDoc, originSketchPt, append, mark);
            return false;
        }

        private static bool SelectPointWithMark(IModelDoc2 modelDoc, ISketchPoint pt, bool append, int mark)
        {
            if (pt == null) return false;
            try
            {
                var selMgr = modelDoc.SelectionManager as ISelectionMgr;
                var selData = selMgr?.CreateSelectData();
                if (selData != null)
                {
                    selData.Mark = mark;
                    return pt.Select4(append, selData);
                }
                return pt.SelectByMark(append, mark);
            }
            catch
            {
                return false;
            }
        }

        private static string FindOriginExtSketchPointName(IModelDoc2 modelDoc)
        {
            try
            {
                var feat = modelDoc.FirstFeature() as IFeature;
                while (feat != null)
                {
                    string tn = null;
                    try { tn = feat.GetTypeName2(); } catch { }
                    if (tn == "OriginProfileFeature")
                    {
                        string nm = feat.Name;
                        if (!string.IsNullOrEmpty(nm))
                            return $"Point1@{nm}";
                    }
                    feat = feat.GetNextFeature() as IFeature;
                }
            }
            catch { }
            return null;
        }

        private static ISketchPoint FindBuiltInSketchOriginPoint(ISketch sk)
        {
            try
            {
                var pts = sk.GetSketchPoints2() as object[];
                if (pts == null) return null;
                foreach (var o in pts)
                {
                    var pt = o as ISketchPoint;
                    if (pt == null) continue;
                    int tp = -1;
                    try { tp = pt.Type; } catch { }
                    if (tp == SW_POINT_ORIGIN) return pt;
                }
            }
            catch { }
            return null;
        }

        private static double[] GetSketchToModelArray(ISketch sk)
        {
            try
            {
                var s2m = sk?.ModelToSketchTransform?.IInverse();
                var d = s2m?.ArrayData as double[];
                return (d != null && d.Length >= 12) ? d : null;
            }
            catch
            {
                return null;
            }
        }

        private static void SketchPointToModel(double[] s2m, double u, double v, out double mx, out double my, out double mz)
        {
            if (s2m != null && s2m.Length >= 12)
            {
                mx = s2m[9] + u * s2m[0] + v * s2m[3];
                my = s2m[10] + u * s2m[1] + v * s2m[4];
                mz = s2m[11] + u * s2m[2] + v * s2m[5];
            }
            else
            {
                mx = u;
                my = v;
                mz = 0.0;
            }
        }

        private static int SafeConstrainedStatus(ISketch sk)
        {
            try { return sk.GetConstrainedStatus(); }
            catch { return SW_CONSTRAINED_UNDER; }
        }

        private static bool IsBadConstrainedStatus(int st)
        {
            return st == SW_CONSTRAINED_OVER || st == SW_CONSTRAINED_NO_SOLUTION || st == SW_CONSTRAINED_INVALID;
        }

        private static void CleanupOverDefiningRelations(ISketch sk)
        {
            try
            {
                var relMgr = sk?.RelationManager;
                if (relMgr == null) return;
                var over = relMgr.GetRelations(SW_REL_FILTER_OVERDEFINING) as object[];
                if (over == null) return;
                foreach (var o in over)
                {
                    var rel = o as SketchRelation;
                    if (rel != null)
                    {
                        try { relMgr.DeleteRelation(rel); } catch { }
                    }
                }
            }
            catch { }
        }

        private static void DeleteDisplayDimension(IModelDoc2 modelDoc, IDisplayDimension dispDim)
        {
            if (modelDoc == null || dispDim == null) return;
            try
            {
                modelDoc.ClearSelection2(true);
                var ann = dispDim.IGetAnnotation();
                if (ann != null && ann.Select3(false, null))
                {
                    modelDoc.EditDelete();
                    return;
                }
                string selName = dispDim.GetNameForSelection();
                if (!string.IsNullOrEmpty(selName) &&
                    modelDoc.Extension.SelectByID2(selName, "DIMENSION", 0, 0, 0, false, 0, null, 0))
                {
                    modelDoc.EditDelete();
                }
            }
            catch { }
            finally
            {
                try { modelDoc.ClearSelection2(true); } catch { }
            }
        }

        private static void RollbackAddedConstraintsAndDimensions(
            IModelDoc2 modelDoc,
            ISketch sk,
            List<IDisplayDimension> addedDims)
        {
            if (addedDims != null)
            {
                for (int i = addedDims.Count - 1; i >= 0; i--)
                    DeleteDisplayDimension(modelDoc, addedDims[i]);
            }
            try { sk?.RelationManager?.DeleteAllRelations(); } catch { }
        }

        private static List<SegmentSnapshot> CaptureSketchSnapshot(object[] rawSegs)
        {
            var list = new List<SegmentSnapshot>();
            if (rawSegs == null) return list;
            foreach (var o in rawSegs)
            {
                var seg = o as ISketchSegment;
                if (seg == null) continue;
                int st = -1;
                try { st = seg.GetType(); } catch { }
                if (st == SW_SKETCH_LINE)
                {
                    var ln = seg as ISketchLine;
                    var p1 = ln?.IGetStartPoint2();
                    var p2 = ln?.IGetEndPoint2();
                    if (p1 == null || p2 == null) continue;
                    list.Add(new SegmentSnapshot
                    {
                        Segment = seg, SegType = st,
                        X1 = p1.X, Y1 = p1.Y, X2 = p2.X, Y2 = p2.Y
                    });
                }
                else if (st == SW_SKETCH_ARC)
                {
                    var arc = seg as ISketchArc;
                    var cp = arc?.IGetCenterPoint2();
                    if (cp == null) continue;
                    bool isCir = arc.IsCircle() == 1;
                    var snap = new SegmentSnapshot
                    {
                        Segment = seg, SegType = st, IsCircle = isCir,
                        Cx = cp.X, Cy = cp.Y, Radius = arc.GetRadius()
                    };
                    if (!isCir)
                    {
                        var p1 = arc.IGetStartPoint2();
                        var p2 = arc.IGetEndPoint2();
                        if (p1 != null) { snap.X1 = p1.X; snap.Y1 = p1.Y; }
                        if (p2 != null) { snap.X2 = p2.X; snap.Y2 = p2.Y; }
                    }
                    list.Add(snap);
                }
            }
            return list;
        }

        private static bool HasGeometryDrifted(List<SegmentSnapshot> snapshot)
        {
            if (snapshot == null) return false;
            foreach (var s in snapshot)
            {
                if (s.SegType == SW_SKETCH_LINE)
                {
                    var ln = s.Segment as ISketchLine;
                    var p1 = ln?.IGetStartPoint2();
                    var p2 = ln?.IGetEndPoint2();
                    if (p1 == null || p2 == null) continue;
                    if (Hypot2(p1.X - s.X1, p1.Y - s.Y1) > DRIFT_TOL ||
                        Hypot2(p2.X - s.X2, p2.Y - s.Y2) > DRIFT_TOL)
                        return true;
                }
                else if (s.SegType == SW_SKETCH_ARC)
                {
                    var arc = s.Segment as ISketchArc;
                    var cp = arc?.IGetCenterPoint2();
                    if (cp == null) continue;
                    if (Hypot2(cp.X - s.Cx, cp.Y - s.Cy) > DRIFT_TOL ||
                        Math.Abs(arc.GetRadius() - s.Radius) > DRIFT_TOL)
                        return true;
                    if (!s.IsCircle)
                    {
                        var p1 = arc.IGetStartPoint2();
                        var p2 = arc.IGetEndPoint2();
                        if (p1 != null && Hypot2(p1.X - s.X1, p1.Y - s.Y1) > DRIFT_TOL) return true;
                        if (p2 != null && Hypot2(p2.X - s.X2, p2.Y - s.Y2) > DRIFT_TOL) return true;
                    }
                }
            }
            return false;
        }

        private static void RestoreSnapshotGeometryIfNeeded(List<SegmentSnapshot> snapshot)
        {
            if (snapshot == null || !HasGeometryDrifted(snapshot)) return;
            foreach (var s in snapshot)
            {
                try
                {
                    if (s.SegType == SW_SKETCH_LINE)
                    {
                        var ln = s.Segment as ISketchLine;
                        var p1 = ln?.IGetStartPoint2();
                        var p2 = ln?.IGetEndPoint2();
                        if (p1 != null) { p1.X = s.X1; p1.Y = s.Y1; }
                        if (p2 != null) { p2.X = s.X2; p2.Y = s.Y2; }
                    }
                    else if (s.SegType == SW_SKETCH_ARC)
                    {
                        var arc = s.Segment as ISketchArc;
                        var cp = arc?.IGetCenterPoint2();
                        if (cp != null) { cp.X = s.Cx; cp.Y = s.Cy; }
                        arc?.SetRadius(s.Radius);
                    }
                }
                catch { }
            }
        }

        private static double Hypot2(double dx, double dy)
        {
            return Math.Sqrt(dx * dx + dy * dy);
        }

        private static double LineMidX(ISketchLine ln)
        {
            var p1 = ln.IGetStartPoint2();
            var p2 = ln.IGetEndPoint2();
            return 0.5 * ((p1?.X ?? 0.0) + (p2?.X ?? 0.0));
        }

        private static double LineMidY(ISketchLine ln)
        {
            var p1 = ln.IGetStartPoint2();
            var p2 = ln.IGetEndPoint2();
            return 0.5 * ((p1?.Y ?? 0.0) + (p2?.Y ?? 0.0));
        }
    }
}
