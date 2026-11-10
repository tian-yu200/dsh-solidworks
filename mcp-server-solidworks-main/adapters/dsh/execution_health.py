"""Fail closed after a reference-geometry COM call stops responding."""
import json
import hashlib
import os
import threading
import time
import uuid
from pathlib import Path


WARNING_SECONDS = 30.0
UNHEALTHY_SECONDS = 300.0
RECOVERY_MESSAGE_EN = (
    "SolidWorks may be stuck. Save other work, then close and restart SolidWorks. "
    "The system will recover automatically after it detects a new healthy instance "
    "and a short documents probe succeeds."
)
RECOVERY_MESSAGE_ZH = (
    "SolidWorks 可能卡住：请先保存其他工作，再关闭并重启 SolidWorks。系统检测到新的健康实例，"
    "且轻量文档探测成功后会自动恢复。"
)


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
        self._recovery_lock = threading.Lock()
        self._state_signature = None
        self._unhealthy = self._load_state()
        if self._unhealthy:
            created = self._pause_queue(self._unhealthy)
            if created:
                self._unhealthy["pause_created_by_health"] = True
                try:
                    self._write_state(self._unhealthy)
                except OSError as exc:
                    self._logger("Could not persist owned PAUSE metadata: %s" % exc)

    def _read_pause_marker(self):
        try:
            marker = json.loads(self.pause_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(marker, dict) and marker.get("made_by") == "health" and marker.get("owner_id"):
            return marker
        return None

    @staticmethod
    def _state_from_pause(marker, reason, *, signature=None):
        return {
            "status": "UNHEALTHY",
            "reason": reason,
            "opened_at": marker.get("opened_at"),
            "operation_id": marker.get("operation_id"),
            "pause_owner_id": marker.get("owner_id"),
            "pause_created_by_health": True,
            "baseline_instance": marker.get("baseline_instance") or {},
            "state_corrupt": True,
            "_loaded_signature": signature,
        }

    def _load_state(self):
        if not self.state_path.exists():
            marker = self._read_pause_marker()
            if marker:
                return self._state_from_pause(
                    marker, "execution health state file is missing; recovery evidence loaded from health-owned PAUSE"
                )
            return None
        try:
            raw = self.state_path.read_bytes()
            signature = hashlib.sha256(raw).hexdigest()
            state = json.loads(raw.decode("utf-8"))
            if isinstance(state, dict) and state.get("status") == "UNHEALTHY":
                self._state_signature = signature
                marker = self._read_pause_marker()
                if marker and marker.get("owner_id") == state.get("pause_owner_id"):
                    state.setdefault("baseline_instance", marker.get("baseline_instance") or {})
                    state.setdefault("pause_created_by_health", True)
                if "baseline_instance" not in state:
                    state["baseline_instance"] = {"solidworks_pids": [],
                                                   "state_version": state.get("state_version")}
                return state
        except (OSError, ValueError, UnicodeError, AttributeError):
            raw = None
            signature = None
            try:
                raw = self.state_path.read_bytes()
                signature = hashlib.sha256(raw).hexdigest()
            except OSError:
                pass
        marker = self._read_pause_marker()
        if marker:
            self._state_signature = signature
            return self._state_from_pause(
                marker, "execution health state file is unreadable; recovery evidence loaded from health-owned PAUSE",
                signature=signature,
            )
        self._state_signature = signature
        return {
            "status": "UNHEALTHY",
            "reason": "execution health state is unreadable; restart SolidWorks and wait for a healthy probe",
            "opened_at": None,
            "state_corrupt": True,
            "_loaded_signature": signature,
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
        self._state_signature = hashlib.sha256(self.state_path.read_bytes()).hexdigest()

    def _pause_queue(self, state):
        marker = self._read_pause_marker()
        owner_id = state.get("pause_owner_id")
        if marker:
            return bool(owner_id and marker.get("owner_id") == owner_id)
        if self.pause_path.exists():
            return False
        self.pause_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.pause_path.with_name("%s.%s.tmp" % (self.pause_path.name, uuid.uuid4().hex))
        owner_id = owner_id or uuid.uuid4().hex
        state["pause_owner_id"] = owner_id
        payload = {
            "made_by": "health",
            "owner_id": owner_id,
            "operation_id": state.get("operation_id"),
            "opened_at": state.get("opened_at"),
            "baseline_instance": state.get("baseline_instance") or {},
            "reason": state.get("reason"),
        }
        temp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            # PAUSE is an existence flag. Create it exclusively so an operator's existing
            # marker is never replaced, including a race with a manual pause.
            descriptor = os.open(self.pause_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(temp_path.read_text(encoding="utf-8"))
            return True
        except OSError:
            if not self.pause_path.exists():
                raise
            marker = self._read_pause_marker()
            return bool(marker and marker.get("owner_id") == owner_id)
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

    def _mark_unhealthy(self, *, operation_id, tool, state_version, params, elapsed, reason,
                        baseline_instance=None):
        state = {
            "status": "UNHEALTHY",
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "operation_id": operation_id,
            "tool": tool,
            "state_version": state_version,
            "params": params,
            "elapsed_seconds": round(elapsed, 3),
            "reason": reason,
            "baseline_instance": baseline_instance or {"state_version": state_version},
            "pause_owner_id": uuid.uuid4().hex,
        }
        with self._lock:
            if self._unhealthy is not None:
                return self._unhealthy
            self._unhealthy = state
        persistence_errors = []
        try:
            state["pause_created_by_health"] = self._pause_queue(state)
        except OSError as exc:
            persistence_errors.append("queue pause: %s" % exc)
        try:
            self._write_state(state)
        except OSError as exc:
            persistence_errors.append("state: %s" % exc)
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

    @staticmethod
    def _is_new_instance(baseline, current):
        baseline_pids = {int(pid) for pid in baseline.get("solidworks_pids", []) if str(pid).isdigit()}
        current_pids = {int(pid) for pid in current.get("solidworks_pids", []) if str(pid).isdigit()}
        if baseline_pids and current_pids and not baseline_pids.intersection(current_pids):
            return True
        baseline_services = {int(pid) for pid in baseline.get("execution_service_pids", []) if str(pid).isdigit()}
        current_services = {int(pid) for pid in current.get("execution_service_pids", []) if str(pid).isdigit()}
        if baseline_services and current_services and not baseline_services.intersection(current_services):
            return True
        old_version = baseline.get("state_version")
        new_version = current.get("state_version")
        # OperationGuard is process-local and starts at zero after the execution service restarts.
        return (isinstance(old_version, int) and isinstance(new_version, int)
                and old_version > 0 and new_version < old_version)

    def has_new_instance(self, current_instance):
        state = self.unhealthy
        return bool(state and self._is_new_instance(state.get("baseline_instance") or {}, current_instance or {}))

    def recovery_message(self, reason=None):
        detail = (" Reason: %s." % reason) if reason else ""
        return ("EXECUTION_UNHEALTHY: %s Recovery: %s %s%s" %
                (detail, RECOVERY_MESSAGE_EN, RECOVERY_MESSAGE_ZH, ""))

    def try_recover(self, current_instance, probe):
        if not self._recovery_lock.acquire(blocking=False):
            return False
        try:
            state = self.unhealthy
            if not state or not self._is_new_instance(state.get("baseline_instance") or {}, current_instance or {}):
                return False
            if not probe():
                return False
        except Exception:
            return False
        try:
            with self._lock:
                current = self._unhealthy
                if not current or current.get("pause_owner_id") != state.get("pause_owner_id"):
                    return False
                if not self._state_matches_disk(current):
                    return False
                if not self._remove_owned_pause(current):
                    return False
                try:
                    if self.state_path.exists():
                        self.state_path.unlink()
                except OSError as exc:
                    self._pause_queue(current)
                    self._logger("Could not clear execution health state after probe: %s" % exc)
                    return False
                self._unhealthy = None
                self._state_signature = None

            event = {"event": "recovered", "operation_id": state.get("operation_id"),
                     "recovered_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                     "baseline_instance": state.get("baseline_instance"), "current_instance": current_instance}
            try:
                self._append(event)
            except OSError as exc:
                self._logger("Could not journal execution health recovery: %s" % exc)
            self._logger("Execution backend recovered on a new healthy instance (op=%s)" % state.get("operation_id"))
            return True
        finally:
            self._recovery_lock.release()

    def _state_matches_disk(self, state):
        if not self.state_path.exists():
            return True
        try:
            raw = self.state_path.read_bytes()
        except OSError:
            return False
        signature = hashlib.sha256(raw).hexdigest()
        expected = state.get("_loaded_signature") or self._state_signature
        if expected and signature == expected:
            return True
        try:
            disk_state = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return bool(state.get("state_corrupt") and expected and signature == expected)
        return (isinstance(disk_state, dict) and disk_state.get("status") == "UNHEALTHY"
                and disk_state.get("pause_owner_id") == state.get("pause_owner_id"))

    def _remove_owned_pause(self, state):
        if not self.pause_path.exists():
            return True
        marker = self._read_pause_marker()
        if not marker or marker.get("owner_id") != state.get("pause_owner_id"):
            return True
        try:
            self.pause_path.unlink()
            return True
        except OSError as exc:
            self._logger("Could not remove health-owned PAUSE after recovery: %s" % exc)
            return False

    def run_reference_geometry(self, operation_id, state_version, params, invoke, *, baseline_instance=None):
        existing = self.unhealthy
        if existing:
            raise ExecutionCircuitOpen(self.recovery_message(existing.get("reason")))

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
                                     reason="reference geometry call exceeded the STA timeout; outcome unknown",
                                     baseline_instance=baseline_instance)
                raise ExecutionCircuitOpen(self.recovery_message("add_reference_geometry outcome is unknown"))

        elapsed = outcome.get("finished_monotonic", self._clock()) - start
        if elapsed >= self.unhealthy_seconds or self._sta_timeout(outcome):
            self._mark_unhealthy(operation_id=operation_id, tool="add_reference_geometry",
                                 state_version=state_version, params=params, elapsed=elapsed,
                                 reason="execution layer reported STA_TIMEOUT; outcome unknown",
                                 baseline_instance=baseline_instance)
            raise ExecutionCircuitOpen(self.recovery_message("add_reference_geometry timed out in the STA"))

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
