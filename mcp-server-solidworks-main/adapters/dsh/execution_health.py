"""Fail closed after a reference-geometry COM call stops responding."""
import json
import os
import threading
import time
import uuid
from pathlib import Path


WARNING_SECONDS = 30.0
UNHEALTHY_SECONDS = 300.0


class ExecutionCircuitOpen(RuntimeError):
    """The backend has an unresolved operation whose outcome is unknown."""


class ExecutionHealth:
    def __init__(self, state_path, journal_path, pause_path, *, warning_seconds=WARNING_SECONDS,
                 unhealthy_seconds=UNHEALTHY_SECONDS, clock=time.monotonic, logger=None):
        self.state_path = Path(state_path)
        self.journal_path = Path(journal_path)
        self.pause_path = Path(pause_path)
        self.warning_seconds = float(warning_seconds)
        self.unhealthy_seconds = float(unhealthy_seconds)
        self._clock = clock
        self._logger = logger or (lambda _message: None)
        self._lock = threading.RLock()
        self._unhealthy = self._load_state()
        if self._unhealthy:
            self._pause_queue(self._unhealthy)

    def _load_state(self):
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("status") == "UNHEALTHY":
                return state
        except (OSError, ValueError, AttributeError):
            pass
        return {
            "status": "UNHEALTHY",
            "reason": "execution health journal is unreadable; manual recovery required",
            "opened_at": None,
        }

    @property
    def unhealthy(self):
        with self._lock:
            if self._unhealthy is not None:
                return dict(self._unhealthy)
            # The DSH MCP server and bench_queue can be separate Python processes.
            self._unhealthy = self._load_state()
            return dict(self._unhealthy) if self._unhealthy else None

    def _append(self, event):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            with self.journal_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()

    def _write_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_name("%s.%s.tmp" % (self.state_path.name, uuid.uuid4().hex))
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, self.state_path)

    def _pause_queue(self, state):
        if self.pause_path.exists():
            return
        self.pause_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.pause_path.with_name("%s.%s.tmp" % (self.pause_path.name, uuid.uuid4().hex))
        temp_path.write_text(
            "Execution backend UNHEALTHY; operation %s outcome is unknown. Manual recovery required.\n"
            % state.get("operation_id", "unknown"),
            encoding="utf-8",
        )
        try:
            # PAUSE is an existence flag. Create it exclusively so an operator's existing
            # marker is never replaced, including a race with a manual pause.
            descriptor = os.open(self.pause_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(temp_path.read_text(encoding="utf-8"))
        except OSError:
            if not self.pause_path.exists():
                raise
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _sta_timeout(outcome):
        value = outcome.get("value")
        if isinstance(value, tuple) and value:
            value = value[0]
        if isinstance(value, dict):
            error = value.get("error") or {}
            detail = " ".join(str(error.get(k, "")) for k in ("code", "message"))
        else:
            detail = str(outcome.get("error", ""))
        return "STA_TIMEOUT" in detail or "StaTimeout" in detail

    def _mark_unhealthy(self, *, operation_id, tool, state_version, params, elapsed, reason):
        state = {
            "status": "UNHEALTHY",
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "operation_id": operation_id,
            "tool": tool,
            "state_version": state_version,
            "params": params,
            "elapsed_seconds": round(elapsed, 3),
            "reason": reason,
        }
        with self._lock:
            if self._unhealthy is not None:
                return self._unhealthy
            self._unhealthy = state
        persistence_errors = []
        for label, action in (("state", lambda: self._write_state(state)),
                              ("queue pause", lambda: self._pause_queue(state))):
            try:
                action()
            except OSError as exc:
                persistence_errors.append("%s: %s" % (label, exc))
        if persistence_errors:
            state["persistence_errors"] = persistence_errors
            self._logger("Could not fully persist/pause after execution backend failure: %s" %
                         "; ".join(persistence_errors))
        try:
            self._append({"event": "unhealthy", **state})
        except OSError as exc:
            self._logger("Could not append execution unhealthy event: %s" % exc)
        self._logger("Execution backend UNHEALTHY after %.1fs (%s, op=%s); queue pause attempted" %
                     (elapsed, tool, operation_id))
        return state

    def run_reference_geometry(self, operation_id, state_version, params, invoke):
        existing = self.unhealthy
        if existing:
            raise ExecutionCircuitOpen("EXECUTION_UNHEALTHY: %s" % existing.get("reason", "manual recovery required"))

        start = self._clock()
        self._append({"event": "started", "operation_id": operation_id, "tool": "add_reference_geometry",
                      "state_version": state_version, "params": params,
                      "started_monotonic": start})
        done = threading.Event()
        outcome = {}

        def worker():
            try:
                outcome["value"] = invoke()
            except Exception as exc:  # the call outcome is journaled, never retried here
                outcome["error"] = exc
            finally:
                outcome["finished_monotonic"] = self._clock()
                done.set()

        threading.Thread(target=worker, name="reference-geometry-%s" % operation_id[:8], daemon=True).start()
        if not done.wait(self.warning_seconds):
            elapsed = self._clock() - start
            self._append({"event": "warning", "operation_id": operation_id, "tool": "add_reference_geometry",
                          "elapsed_seconds": round(elapsed, 3), "threshold_seconds": self.warning_seconds})
            self._logger("Reference geometry operation exceeded %.0fs (op=%s)" %
                         (self.warning_seconds, operation_id))
            remaining = max(0.0, self.unhealthy_seconds - (self._clock() - start))
            if not done.wait(remaining):
                elapsed = self._clock() - start
                self._mark_unhealthy(operation_id=operation_id, tool="add_reference_geometry",
                                     state_version=state_version, params=params, elapsed=elapsed,
                                     reason="reference geometry call exceeded the STA timeout; outcome unknown")
                raise ExecutionCircuitOpen("EXECUTION_UNHEALTHY: add_reference_geometry outcome is unknown")

        elapsed = outcome.get("finished_monotonic", self._clock()) - start
        if elapsed >= self.unhealthy_seconds or self._sta_timeout(outcome):
            self._mark_unhealthy(operation_id=operation_id, tool="add_reference_geometry",
                                 state_version=state_version, params=params, elapsed=elapsed,
                                 reason="execution layer reported STA_TIMEOUT; outcome unknown")
            raise ExecutionCircuitOpen("EXECUTION_UNHEALTHY: add_reference_geometry timed out in the STA")

        event = {"event": "completed" if "value" in outcome else "failed",
                 "operation_id": operation_id, "tool": "add_reference_geometry",
                 "elapsed_seconds": round(elapsed, 3)}
        if "error" in outcome:
            event.update(error_type=type(outcome["error"]).__name__, error=str(outcome["error"]))
        else:
            result = outcome["value"]
            if isinstance(result, tuple) and result:
                result = result[0]
            event.update(status=result.get("status") if isinstance(result, dict) else None,
                         error_code=((result.get("error") or {}).get("code")
                                     if isinstance(result, dict) else None))
        self._append(event)
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]
