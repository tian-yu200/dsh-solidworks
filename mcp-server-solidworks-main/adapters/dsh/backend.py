import sys
import threading
import uuid

from settings import CLAUDE_ADAPTER_DIR


# The existing adapter is intentionally reused as the single transport and compiler bridge.
_claude_path = str(CLAUDE_ADAPTER_DIR)
if _claude_path not in sys.path:
    sys.path.insert(0, _claude_path)

from execution_client import (  # noqa: E402
    ExecutionLayerError,
    call_tool,
    ensure_ready,
    get_health,
    get_state,
)
from ir_execution_port import run_feature_graph  # noqa: E402


class SolidWorksBackend:
    def __init__(self):
        self._lock = threading.RLock()
        self._state_version = None

    def health(self):
        return get_health()

    def ensure_ready(self):
        body = ensure_ready()
        state_version = body.get("stateVersion")
        if state_version is not None:
            self._state_version = int(state_version)
        return body

    def _current_state_version(self):
        if self._state_version is None:
            self._state_version = get_state()
        return self._state_version

    @staticmethod
    def _state_mismatch(response):
        return (
            response.get("status") == "FAILED"
            and (response.get("error") or {}).get("code") == "INVALID_STATE_VERSION"
        )

    def call(self, tool, params):
        with self._lock:
            state_version = self._current_state_version()
            response = call_tool(tool, str(uuid.uuid4()), state_version, params)
            if self._state_mismatch(response):
                state_version = get_state()
                response = call_tool(tool, str(uuid.uuid4()), state_version, params)
            if response.get("stateVersion") is not None:
                self._state_version = int(response["stateVersion"])
            return response

    def verify_state(self):
        return self.call("verify_state", {})

    def begin_transaction(self, name):
        return self.call("begin_undo_scope", {"name": name})

    def commit_transaction(self, name="DSH SolidWorks operation"):
        return self.call("commit_undo_scope", {"name": name})

    def rollback_transaction(self):
        return self.call("rollback_undo_scope", {})

    def run_feature_graph(self, graph, fresh_document):
        with self._lock:
            if fresh_document:
                nodes = graph.get("nodes") or []
                is_assembly = any(
                    isinstance(node, dict)
                    and node.get("type") in ("component", "mate")
                    for node in nodes
                )
                opened = self.call(
                    "open_new_assembly" if is_assembly else "open_new_part", {}
                )
                if opened.get("status") != "COMPLETED":
                    return {
                        "status": "FAILED",
                        "error": opened.get("error"),
                        "compiler": None,
                        "cadState": opened.get("cadState"),
                        "stateVersion": opened.get("stateVersion"),
                    }

            result = run_feature_graph(graph)
            self._state_version = get_state()
            return {
                "status": result.status,
                "error": result.error,
                "compiler": result.to_dict(),
                "cadState": None,
                "stateVersion": self._state_version,
            }


backend = SolidWorksBackend()

__all__ = ["ExecutionLayerError", "backend"]
