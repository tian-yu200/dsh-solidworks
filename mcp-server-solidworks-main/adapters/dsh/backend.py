import sys
import threading
import uuid

from settings import CLAUDE_ADAPTER_DIR, IDENTITY_ENDPOINT, IDENTITY_TIMEOUT_SECONDS


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
    pooled_get,
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

    def current_state_version(self):
        """GET /api/tool/state — the authoritative counter. No COM, no feature-tree walk.

        Measured against the live layer: ~2 ms, versus ~2.5 s (median) for verify_state,
        which walks FeatureManager.GetFeatures(true) on the STA thread
        (SolidWorksService.cs:793). Safe to call between steps.
        """
        value = get_state()
        if value is not None:
            self._state_version = int(value)
        return self._state_version

    def probe_identity(self):
        """GET /health — COM attach state + the ACTIVE DOCUMENT TITLE + stateVersion.

        Measured ~0.2-0.35 s. DELIBERATELY NARROW: GetHealthInfo reports only
        `activeDocument` (SolidWorksService.cs:382) — no document PATH and no document
        TYPE. So this can prove a TITLE-BOUND identity and nothing more; callers must not
        treat it as a substitute for verify_state's full cadState.
        """
        return get_health()

    def identity(self):
        """GET /api/tool/identity — the identity subset of verify_state, no feature-tree walk.

        The C# service (ToolController.Identity -> SolidWorksService.GetIdentityInfo) reports
        exactly the document-identity fields verify_state derives — stateVersion,
        activeDocument, activeDocumentPath, documentType, isDirty — read the SAME way under
        the SAME key names, in ~16 ms instead of the seconds verify_state pays for
        FeatureManager.GetFeatures(true). Callers use it wherever ONLY the document identity
        is compared (per-step / per-edit guards, save assessment); anything that reads CAD
        content (features, sketches, dimensions) still calls verify_state.

        FALLBACK: when the endpoint is missing (older service build), unreachable, or answers
        anything but 200 with all identity fields present, this returns the FULL verify_state
        response instead — the strictly stronger check — so a caller's safety can never be
        weakened by this shortcut. Only a transport/shape failure triggers the fallback; a
        503 from the endpoint itself (COM attach failed / no active document) also falls
        through to verify_state, which reports the same condition authoritatively.
        """
        try:
            response = pooled_get(IDENTITY_ENDPOINT, IDENTITY_TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise ExecutionLayerError(
                    f"Unexpected HTTP {response.status_code} from /api/tool/identity: "
                    f"{response.text}"
                )
            body = response.json()
            if body.get("stateVersion") is None:
                raise ExecutionLayerError(
                    "identity endpoint response is missing stateVersion"
                )
            state_version = int(body["stateVersion"])
            self._state_version = state_version
            return {
                "status": "COMPLETED",
                "stateVersion": state_version,
                "cadState": {
                    "stateVersion": state_version,
                    "activeDocument": body.get("activeDocument"),
                    "activeDocumentPath": body.get("activeDocumentPath"),
                    "documentType": body.get("documentType"),
                    "isDirty": body.get("isDirty"),
                },
                "error": None,
            }
        except Exception:  # noqa: BLE001 - ANY failure degrades to the full verification
            return self.verify_state()

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
