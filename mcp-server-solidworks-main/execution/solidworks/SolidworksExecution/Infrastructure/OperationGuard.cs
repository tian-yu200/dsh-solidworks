using System.Collections.Concurrent;
using System.Threading;
using SolidworksExecution.Models;

namespace SolidworksExecution.Infrastructure
{
    public class OperationGuard : IOperationGuard
    {
        private static readonly OperationGuard _instance = new OperationGuard();
        public static OperationGuard Instance => _instance;

        // Only the ids are kept: GetDuplicate answers a fresh DUPLICATE response and never read the
        // stored one, which carried a whole cadState per call for the life of the process.
        private readonly ConcurrentDictionary<string, byte> _completed
            = new ConcurrentDictionary<string, byte>();

        private volatile int _currentStateVersion = 0;

        private OperationGuard() { }

        public bool IsDuplicate(string operationId)
        {
            return _completed.ContainsKey(operationId);
        }

        public ExecutionResponse GetDuplicate(string operationId)
        {
            if (_completed.ContainsKey(operationId))
            {
                return new ExecutionResponse
                {
                    OperationId = operationId,
                    Status = "DUPLICATE",
                    LastKnownStateVersion = _currentStateVersion,
                    CadState = null,
                    Error = null
                };
            }
            return null;
        }

        public bool IsStateVersionValid(int incomingStateVersion)
        {
            return incomingStateVersion == _currentStateVersion;
        }

        public void RegisterCompleted(string operationId, ExecutionResponse response)
        {
            _completed[operationId] = 0;
            Interlocked.Increment(ref _currentStateVersion);
        }

        public int GetCurrentStateVersion()
        {
            return _currentStateVersion;
        }
    }
}
