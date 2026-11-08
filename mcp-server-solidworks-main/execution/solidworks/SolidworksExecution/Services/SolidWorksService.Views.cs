using System;
using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;


namespace SolidworksExecution.Services
{
    // SolidWorksService partial: render_views, PNG images of the active part in standard orthographic
    // views with hidden lines shown, so the planner can SEE its build next to the drawing.
    public partial class SolidWorksService
    {
        // The planner could only compare its build with the drawing through numbers, and numbers that
        // happen to agree hide a missing feature: on 2026-11-09 a 缺口块 build without its ramp and
        // front step still measured 42×30×30, and passed (findings M5). These images let it compare
        // view by view. Read-only for the model: only the view orientation and display mode change,
        // and the display mode is restored.
        private static readonly Dictionary<string, int> StandardViews = new Dictionary<string, int>
        {
            { "front", (int)swStandardViews_e.swFrontView },
            { "back", (int)swStandardViews_e.swBackView },
            { "left", (int)swStandardViews_e.swLeftView },
            { "right", (int)swStandardViews_e.swRightView },
            { "top", (int)swStandardViews_e.swTopView },
            { "bottom", (int)swStandardViews_e.swBottomView },
            { "isometric", (int)swStandardViews_e.swIsometricView },
        };

        public ExecutionResponse RenderViews(ToolRequest request)
        {
            if (_guard.IsDuplicate(request.OperationId))
                return _guard.GetDuplicate(request.OperationId);

            if (!_guard.IsStateVersionValid(request.StateVersion))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "INVALID_STATE_VERSION", "Incoming state_version does not match current state.");

            if (!EnsureConnected())
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "COM_ATTACH_FAILED", "SolidWorks process not found or COM not registered.");

            var p = request.Params as JObject;
            string outDir = p?.Value<string>("output_dir");
            if (string.IsNullOrEmpty(outDir) || !Path.IsPathRooted(outDir))
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "MISSING_PARAMETER", "output_dir must be an absolute directory path.");

            var names = new List<string>();
            var raw = p?["views"];
            if (raw is JArray arr)
                foreach (var v in arr) names.Add(((string)v ?? "").Trim().ToLowerInvariant());
            else
                foreach (var v in ((string)raw ?? "front,top,left,isometric").Split(','))
                    names.Add(v.Trim().ToLowerInvariant());
            foreach (var n in names)
                if (!StandardViews.ContainsKey(n))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", $"Unknown view '{n}'. Use: {string.Join(", ", StandardViews.Keys)}.");

            var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
            if (modelDoc == null)
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
            if (modelDoc.GetType() == (int)swDocumentTypes_e.swDocDRAWING)
                return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                    "WRONG_DOCUMENT_TYPE", "render_views needs a part or assembly window.");

            // A screen capture of a window SolidWorks is told not to repaint would be stale, so the
            // request-wide CommandInProgress is lifted while capturing and put back afterwards.
            bool commandInProgress = false;
            try { commandInProgress = _solidWorks.CommandInProgress; _solidWorks.CommandInProgress = false; }
            catch { }
            var view = modelDoc.ActiveView as IModelView;
            int previousMode = -1;
            var rendered = new JArray();
            try
            {
                Directory.CreateDirectory(outDir);
                if (view != null)
                {
                    try
                    {
                        previousMode = view.DisplayMode;
                        view.DisplayMode = (int)swViewDisplayMode_e.swViewDisplayMode_HiddenLinesGrayed;
                    }
                    catch (Exception ex) { ExecLog.Write($"render_views: display mode not set: {ex.Message}"); }
                }
                foreach (var name in names)
                {
                    var item = new JObject { ["view"] = name };
                    string path = Path.Combine(outDir, name + ".png");
                    try
                    {
                        if (File.Exists(path)) File.Delete(path);
                        modelDoc.ShowNamedView2("", StandardViews[name]);
                        modelDoc.ViewZoomtofit2();
                        modelDoc.GraphicsRedraw2();
                        int errors = 0, warnings = 0;
                        bool saved = modelDoc.Extension.SaveAs(path, (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                            (int)(swSaveAsOptions_e.swSaveAsOptions_Silent | swSaveAsOptions_e.swSaveAsOptions_Copy),
                            null, ref errors, ref warnings);
                        item["saved"] = saved && File.Exists(path);
                        if (saved && File.Exists(path)) item["path"] = path;
                        else item["error"] = $"SaveAs returned {saved}, errors={errors}, warnings={warnings}";
                    }
                    catch (Exception ex)
                    {
                        item["saved"] = false;
                        item["error"] = ex.GetType().Name + ": " + ex.Message;
                    }
                    rendered.Add(item);
                }
            }
            finally
            {
                if (view != null && previousMode > 0)
                {
                    try { view.DisplayMode = previousMode; } catch { }
                }
                if (commandInProgress)
                {
                    try { _solidWorks.CommandInProgress = true; } catch { }
                }
            }
            ExecLog.Write($"render_views: {rendered.Count} view(s) -> {outDir}");
            return new ExecutionResponse
            {
                OperationId = request.OperationId,
                Status = "COMPLETED",
                Verified = true,
                StateVersion = _guard.GetCurrentStateVersion(),
                CadState = new CadState
                {
                    StateVersion = _guard.GetCurrentStateVersion(),
                    ActiveDocument = modelDoc.GetTitle(),
                    DocumentType = DocumentTypeName(modelDoc),
                    ActiveSketch = null,
                    Features = new List<string> { new JObject { ["views"] = rendered }.ToString(Newtonsoft.Json.Formatting.None) },
                    Dimensions = new List<string>()
                },
                Error = null
            };
        }
    }
}
