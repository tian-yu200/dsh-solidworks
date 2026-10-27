using System;
using System.IO;
using System.Text;

namespace SolidworksExecution.Infrastructure
{
    // Minimal file logger (P0.6-mini). Writes next to the exe as execution.log.
    // Must NEVER throw — logging failures are swallowed.
    public static class ExecLog
    {
        private static readonly object _lock = new object();

        public static readonly string FilePath =
            Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "execution.log");

        // One long-lived writer instead of File.AppendAllText per line: the old form did an
        // open/write/close (plus a flush of the handle) for every line, 2+ times per tool call,
        // on the STA thread while the HTTP request blocked. AutoFlush keeps each line visible
        // immediately, so crash-tail behaviour is unchanged.
        private static StreamWriter _writer;

        public static void Write(string message)
        {
            try
            {
                lock (_lock)
                {
                    if (_writer == null)
                        _writer = new StreamWriter(FilePath, true, new UTF8Encoding(false))
                        {
                            AutoFlush = true
                        };
                    _writer.Write(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + "  " + message);
                    _writer.Write(Environment.NewLine);
                }
            }
            catch
            {
                // logging must never break execution — drop the writer so the next call reopens it
                try { if (_writer != null) _writer.Dispose(); } catch { }
                _writer = null;
            }
        }
    }
}
