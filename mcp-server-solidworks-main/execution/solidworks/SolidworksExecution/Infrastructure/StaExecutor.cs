using System;
using System.Collections.Concurrent;
using System.Threading;

namespace SolidworksExecution.Infrastructure
{
    /// <summary>
    /// Runs delegates on a single, long-lived, dedicated STA thread. (P0.1)
    ///
    /// SolidWorks COM objects must be called from an STA apartment. OWIN / Web API request
    /// threads are MTA, and while COM marshaling lets lightweight calls (e.g. FeatureExtrusion3)
    /// limp across the apartment boundary, heavier or more apartment-sensitive calls fail off-STA:
    /// InsertSheetMetalBaseFlange2 silently returned null and add_edge_feature (edge fillet)
    /// deadlocked (ContextSwitchDeadlock). Routing ALL COM-touching work through one STA thread —
    /// including the GetActiveObject attach — makes the RCW live on the STA, fixing both.
    ///
    /// A single worker thread also serializes COM access, which suits the single-writer,
    /// process-global state model the OperationGuard already assumes.
    /// </summary>
    public sealed class StaExecutor
    {
        public static readonly StaExecutor Instance = new StaExecutor();

        // Ceiling on how long any caller blocks waiting for the STA thread. SolidWorks can sit behind a
        // modal dialog, and an unbounded wait strands the OWIN request thread forever (the HTTP client's
        // own timeout only stops the caller, not the wedge). Deliberately generous — the slowest
        // legitimate operation measured in execution.log was analyze_model(feature_map) at ~108 s.
        public static readonly int StaTimeoutSeconds = 300;

        private readonly BlockingCollection<Action> _queue = new BlockingCollection<Action>();

        private StaExecutor()
        {
            var thread = new Thread(Worker)
            {
                IsBackground = true,
                Name = "SolidWorksStaThread"
            };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
        }

        private void Worker()
        {
            // Each job sets its own completion event and captures its own exception, so a throwing
            // job never tears down the worker loop.
            foreach (var work in _queue.GetConsumingEnumerable())
                work();
        }

        /// <summary>
        /// Marshal <paramref name="func"/> onto the STA thread and block until it returns.
        /// Throws TimeoutException (STA_TIMEOUT) if the STA thread does not report back within
        /// <see cref="StaTimeoutSeconds"/> — see the field for why an unbounded wait is the worse option.
        /// </summary>
        public T Run<T>(Func<T> func)
        {
            T result = default(T);
            Exception captured = null;
            // Not a `using` block around the wait: if the caller gives up and disposes while the job is
            // still queued/running, the job's completion signal must not throw ObjectDisposedException
            // INSIDE the worker loop — that would tear down the single STA thread every call depends on.
            var done = new ManualResetEventSlim(false);
            _queue.Add(() =>
            {
                try { result = func(); }
                catch (Exception ex) { captured = ex; }
                finally
                {
                    try { done.Set(); }
                    catch (ObjectDisposedException) { /* caller already timed out */ }
                }
            });
            try
            {
                if (!done.Wait(TimeSpan.FromSeconds(StaTimeoutSeconds)))
                    throw new TimeoutException(
                        $"STA_TIMEOUT after {StaTimeoutSeconds}s: the SolidWorks STA thread did not "
                        + "return. A modal SolidWorks dialog (or a hung COM call) blocks every operation "
                        + "until it is dismissed — check the SolidWorks window, then retry.");
            }
            finally
            {
                done.Dispose();
            }
            if (captured != null)
                throw new InvalidOperationException("STA execution failed: " + captured.Message, captured);
            return result;
        }
    }
}
