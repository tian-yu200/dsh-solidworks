import sys
import threading
import uuid
from contextlib import contextmanager

from settings import (CLAUDE_ADAPTER_DIR, IDENTITY_ENDPOINT, IDENTITY_TIMEOUT_SECONDS,
                      JOB_ROOT, STATE_ROOT)


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
from execution_health import ExecutionCircuitOpen, ExecutionHealth  # noqa: E402
from adapter_log import write as _log  # noqa: E402


REFERENCE_GEOMETRY_HTTP_TIMEOUT = 310.0  # STA timeout is 300 s; leave room for the response.


class SolidWorksBackend:
    def __init__(self, execution_health=None):
        # The execution gate serializes requests without being needed to read the state lock.
        # Never hold _lock while a monitored operation waits on COM; otherwise the circuit's
        # local fast-fail path would itself wait behind the hung call.
        self._lock = threading.RLock()
        self._execution_gate = threading.RLock()
        self._state_version = None
        self._execution_health = execution_health or ExecutionHealth(
            JOB_ROOT / "execution-health.json",
            JOB_ROOT / "execution-operations.jsonl",
            STATE_ROOT / "benchmark" / "PAUSE",
            logger=_log,
        )

    def _unhealthy_response(self, tool):
        state = self._execution_health.unhealthy
        if not state:
            return None
        reason = state.get("reason", "manual recovery required")
        return {
            "status": "FAILED",
            "stateVersion": self._state_version,
            "cadState": None,
            "error": {"code": "EXECUTION_UNHEALTHY",
                      "message": "The SolidWorks execution backend is UNHEALTHY; %s was not sent to the STA. %s"
                                 % (tool, reason)},
        }

    @contextmanager
    def _execution(self, tool):
        """Serialize backend calls while polling the durable breaker instead of blocking blindly."""
        while True:
            blocked = self._unhealthy_response(tool)
            if blocked:
                yield blocked
                return
            if self._execution_gate.acquire(timeout=0.05):
                break
        try:
            yield self._unhealthy_response(tool)
        finally:
            self._execution_gate.release()

    def health(self):
        with self._execution("health") as blocked:
            if blocked:
                raise ExecutionCircuitOpen(blocked["error"]["message"])
            return get_health()

    def ensure_ready(self):
        with self._execution("ensure_ready") as blocked:
            if blocked:
                raise ExecutionCircuitOpen(blocked["error"]["message"])
            body = ensure_ready()
            state_version = body.get("stateVersion")
            if state_version is not None:
                with self._lock:
                    self._state_version = int(state_version)
            return body

    def _current_state_version(self):
        blocked = self._unhealthy_response("get_state")
        if blocked:
            raise ExecutionCircuitOpen(blocked["error"]["message"])
        if self._state_version is None:
            state_version = get_state()
            with self._lock:
                self._state_version = state_version
        return self._state_version

    @staticmethod
    def _state_mismatch(response):
        return (
            response.get("status") == "FAILED"
            and (response.get("error") or {}).get("code") == "INVALID_STATE_VERSION"
        )

    def call(self, tool, params):
        with self._execution(tool) as blocked:
            if blocked:
                return blocked
            with self._lock:
                state_version = self._current_state_version()
            return self._execute_tool(tool, params, state_version)

    def execute_ir_tool(self, tool, params, state_version):
        """ExecutionPort entry used by Feature Graph compilation with its threaded stateVersion."""
        with self._execution(tool) as blocked:
            if blocked:
                return blocked
            return self._execute_tool(tool, params, int(state_version))

    def _execute_tool(self, tool, params, state_version):
        operation_id = str(uuid.uuid4())
        timeout = REFERENCE_GEOMETRY_HTTP_TIMEOUT if tool == "add_reference_geometry" else None

        def invoke():
            def call_at_version(version, call_id):
                if timeout is not None:
                    return call_tool(tool, call_id, version, params, timeout_seconds=timeout)
                return call_tool(tool, call_id, version, params)

            response = call_at_version(state_version, operation_id)
            if self._state_mismatch(response):
                refreshed_version = get_state()
                response = call_at_version(refreshed_version, str(uuid.uuid4()))
                return response, refreshed_version
            return response, state_version

        if tool == "add_reference_geometry":
            try:
                response, state_version = self._execution_health.run_reference_geometry(
                    operation_id, state_version, params, invoke)
            except ExecutionCircuitOpen as exc:
                return {"status": "FAILED", "stateVersion": self._state_version,
                        "cadState": None, "error": {"code": "EXECUTION_UNHEALTHY", "message": str(exc)}}
        else:
            response, state_version = invoke()
        with self._lock:
            if response.get("stateVersion") is not None:
                self._state_version = int(response["stateVersion"])
            elif self._state_mismatch(response):
                self._state_version = int(state_version)
        return response

    def verify_state(self):
        return self.call("verify_state", {})

    def current_state_version(self):
        """GET /api/tool/state — the authoritative counter. No COM, no feature-tree walk.

        Measured against the live layer: ~2 ms, versus ~2.5 s (median) for verify_state,
        which walks FeatureManager.GetFeatures(true) on the STA thread
        (SolidWorksService.cs:793). Safe to call between steps.
        """
        blocked = self._unhealthy_response("get_state")
        if blocked:
            raise ExecutionCircuitOpen(blocked["error"]["message"])
        with self._execution("get_state") as blocked:
            blocked = self._unhealthy_response("get_state")
            if blocked:
                raise ExecutionCircuitOpen(blocked["error"]["message"])
            value = get_state()
            with self._lock:
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
        with self._execution("probe_identity") as blocked:
            if blocked:
                raise ExecutionCircuitOpen(blocked["error"]["message"])
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
        with self._execution("identity") as blocked:
            if blocked:
                return blocked
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
                with self._lock:
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

    def documents(self):
        """Read every open document without activation; missing/partial inventory fails closed."""
        with self._execution("documents") as blocked:
            if blocked:
                raise ExecutionCircuitOpen(blocked["error"]["message"])
            response = pooled_get(IDENTITY_ENDPOINT.rsplit("/", 1)[0] + "/documents", IDENTITY_TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise ExecutionLayerError("DOCUMENT_INVENTORY_UNAVAILABLE: update the execution service before building")
            body = response.json()
            docs = body.get("documents")
            fields = {"document_id", "title", "path", "type", "isDirty", "updateStamp"}
            if (not isinstance(docs, list) or "active_document" not in body
                    or not isinstance(body.get("undo_scope_active"), bool)
                    or any(not isinstance(d, dict) or not fields.issubset(d)
                           or not d["document_id"] or not isinstance(d["isDirty"], bool)
                           or not isinstance(d["updateStamp"], int) for d in docs)
                    or len({d["document_id"] for d in docs}) != len(docs)
                    or (body["active_document"] is not None and body["active_document"] not in docs)):
                raise ExecutionLayerError("DOCUMENT_INVENTORY_INVALID: incomplete document evidence")
            return body

    def commit_transaction(self, name="DSH SolidWorks operation"):
        return self.call("commit_undo_scope", {"name": name})

    def rollback_transaction(self):
        return self.call("rollback_undo_scope", {})

    def run_feature_graph(self, graph, fresh_document):
        with self._execution("run_feature_graph") as blocked:
            if blocked:
                return {"status": "FAILED", "error": blocked["error"], "cadState": None,
                        "stateVersion": self._state_version, "compiler": None}
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
            blocked = self._unhealthy_response("get_state")
            if blocked:
                return {
                    "status": result.status,
                    "error": result.error,
                    "compiler": result.to_dict(),
                    "cadState": None,
                    "stateVersion": self._state_version,
                }
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
