import hmac
import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend import ExecutionLayerError, backend
from job_store import job_store
from models import (
    cad_state,
    envelope,
    error_from,
    utc_now,
    verification_status,
)
from policy import (
    action_expired,
    edits_risk,
    expected_output_paths,
    graph_risk,
    new_secret,
    normalize_path,
    output_exists,
    output_file_status,
    payload_hash,
    same_document,
    secret_hash,
    validate_drawing_workflow,
    validate_edits,
    validate_save_or_export_payload,
)
from references import read_reference
from settings import BACKUP_ROOT


class CadOrchestrator:
    def __init__(self):
        self._write_lock = threading.RLock()

    @staticmethod
    def _target(state):
        return state.get("activeDocumentPath") or state.get("activeDocument")

    @staticmethod
    def _identity(state):
        if not state:
            return {}
        return {
            "title": state.get("activeDocument"),
            "path": state.get("activeDocumentPath") or "",
            "type": state.get("documentType"),
        }

    @staticmethod
    def _response_error(response):
        return error_from(response) or {
            "code": "OPERATION_FAILED",
            "message": "The SolidWorks operation did not complete.",
        }

    @staticmethod
    def _close_document(expected_identity=None, *, save=False, close_all=False):
        params = {
            "save": save,
            "close_all": close_all,
        }
        if not close_all:
            expected_identity = expected_identity or {}
            params.update(
                expected_title=expected_identity.get("title") or "",
                expected_path=expected_identity.get("path") or "",
                expected_type=expected_identity.get("type") or "",
            )
        return backend.call("close_document", params)

    def _close_created_document(
        self,
        job,
        *,
        kind,
        created_identity,
        previous_identity,
    ):
        if created_identity:
            close_response = self._close_document(created_identity)
        else:
            close_response = {
                "status": "SKIPPED",
                "error": {
                    "code": "CREATED_DOCUMENT_IDENTITY_UNAVAILABLE",
                    "message": (
                        "The newly created document was left open because its "
                        "identity could not be verified safely."
                    ),
                },
            }

        close_verify = backend.verify_state()
        state = (
            cad_state(close_verify)
            if close_verify.get("status") == "COMPLETED"
            else {}
        )
        verify_error = error_from(close_verify)
        recovered = (
            close_response.get("status") == "COMPLETED"
            and (
                (
                    previous_identity
                    and close_verify.get("status") == "COMPLETED"
                    and same_document(previous_identity, state)
                )
                or (
                    not previous_identity
                    and verify_error
                    and verify_error.get("code") == "NO_ACTIVE_DOCUMENT"
                )
            )
        )
        recovery_status = "rolled_back" if recovered else "rollback_failed"
        event = {
            "at": utc_now(),
            "kind": kind,
            "close_new_document": close_response,
            "verification": close_verify,
            "recovery_status": recovery_status,
        }
        if recovered:
            job["expected_document"] = previous_identity or {}
            job["last_state"] = state if previous_identity else {}
        else:
            job["expected_document"] = created_identity or {}
            job["last_state"] = state
        return recovery_status, state, event

    @staticmethod
    def _job_metadata(job):
        return {
            "execution_status": job.get("execution_status", "not_started"),
            "verification_status": job.get(
                "verification_status", "not_run"
            ),
            "recovery_status": job.get("recovery_status", "not_needed"),
            "trust_level": job.get("trust_level", "unverified"),
        }

    def _record_success(self, job, kind, trust_level):
        job["execution_status"] = "completed"
        job["verification_status"] = "passed"
        job["recovery_status"] = "not_needed"
        if job.get("trust_level") != "verified":
            job["trust_level"] = trust_level
        job["error"] = None
        job_store.save(job)

    def _record_failure(
        self,
        job,
        *,
        kind,
        phase,
        error,
        execution_status,
        verification_status_value,
        recovery_status,
        recovery=None,
    ):
        job["execution_status"] = execution_status
        job["verification_status"] = verification_status_value
        job["recovery_status"] = recovery_status
        job["trust_level"] = "unverified"
        job["error"] = error
        job.setdefault("failure_history", []).append(
            {
                "at": utc_now(),
                "kind": kind,
                "phase": phase,
                "error": error,
                "execution_status": execution_status,
                "verification_status": verification_status_value,
                "recovery_status": recovery_status,
            }
        )
        if recovery:
            job.setdefault("recovery_history", []).append(recovery)
        job_store.save(job)

    @staticmethod
    def _graph_is_assembly(graph):
        return any(
            isinstance(node, dict)
            and node.get("type") in ("component", "mate")
            for node in (graph.get("nodes") or [])
        )

    def _begin_transaction(self, job, name):
        response = backend.begin_transaction(name)
        if response.get("status") == "COMPLETED":
            return None
        return self._response_error(response)

    def _recover_transaction(
        self,
        job,
        *,
        kind,
        previous_identity,
        newly_created,
    ):
        rollback = backend.rollback_transaction()
        event = {
            "at": utc_now(),
            "kind": kind,
            "rollback": rollback,
            "newly_created_document": newly_created,
        }
        recovery_status = (
            "rolled_back"
            if rollback.get("status") == "COMPLETED"
            else "rollback_failed"
        )

        rollback_verify = backend.verify_state()
        event["rollback_verification"] = rollback_verify
        rollback_state = (
            cad_state(rollback_verify)
            if rollback_verify.get("status") == "COMPLETED"
            else {}
        )
        if rollback_verify.get("status") != "COMPLETED":
            recovery_status = "rollback_failed"

        if newly_created:
            current_identity = job.get("expected_document") or {}
            target_still_active = (
                rollback_verify.get("status") == "COMPLETED"
                and current_identity
                and same_document(current_identity, rollback_state)
            )
            if not target_still_active:
                recovery_status = "rollback_failed"
                event["close_new_document"] = {
                    "status": "SKIPPED",
                    "error": {
                        "code": "ACTIVE_DOCUMENT_CHANGED",
                        "message": (
                            "The newly created document was not closed because "
                            "it is no longer the verified active document."
                        ),
                    },
                }
                event["verification"] = rollback_verify
                state = rollback_state
                job["last_state"] = state
            else:
                close_status, state, close_event = (
                    self._close_created_document(
                        job,
                        kind=kind,
                        created_identity=current_identity,
                        previous_identity=previous_identity,
                    )
                )
                event["close_new_document"] = close_event[
                    "close_new_document"
                ]
                event["verification"] = close_event["verification"]
                if (
                    rollback.get("status") != "COMPLETED"
                    or close_status != "rolled_back"
                ):
                    recovery_status = "rollback_failed"
        else:
            verify = rollback_verify
            event["verification"] = verify
            state = rollback_state
            if (
                verify.get("status") != "COMPLETED"
                or not same_document(previous_identity, state)
            ):
                recovery_status = "rollback_failed"
            else:
                job["last_state"] = state

        event["recovery_status"] = recovery_status
        return recovery_status, state, event

    def _transaction_failure_response(
        self,
        job,
        *,
        kind,
        phase,
        risk,
        error,
        verification,
        recovery_status,
        recovery_event,
        completed_steps,
        partial_state,
        target_state,
        backup_path=None,
    ):
        if phase == "verification":
            verification_value = verification_status(verification)
            response_status = (
                "partial"
                if recovery_status == "rollback_failed"
                else "verification_failed"
            )
            execution_value = (
                "partial"
                if recovery_status == "rollback_failed"
                else "completed"
            )
        else:
            verification_value = (
                "passed"
                if phase == "commit"
                and verification.get("all_completed") is True
                else "not_run"
            )
            response_status = (
                "failed"
                if recovery_status == "rolled_back"
                else "partial"
            )
            execution_value = (
                "failed"
                if recovery_status == "rolled_back"
                else "partial"
            )

        self._record_failure(
            job,
            kind=kind,
            phase=phase,
            error=error,
            execution_status=execution_value,
            verification_status_value=verification_value,
            recovery_status=recovery_status,
            recovery=recovery_event,
        )
        return envelope(
            status=response_status,
            risk=risk,
            job_id=job["job_id"],
            target_document=self._target(target_state),
            backup_path=backup_path,
            completed_steps=completed_steps,
            partial_state=partial_state,
            verification=verification,
            error=error,
            **self._job_metadata(job),
        )

    def status(self):
        try:
            health = backend.health()
        except ExecutionLayerError as exc:
            return envelope(
                status="failed",
                error={"code": "EXECUTION_UNAVAILABLE", "message": str(exc)},
            )

        state = {}
        state_error = None
        if health.get("comAttached"):
            response = backend.verify_state()
            if response.get("status") == "COMPLETED":
                state = cad_state(response)
            else:
                state_error = error_from(response)
        return envelope(
            status="completed",
            target_document=self._target(state),
            partial_state=state,
            verification={"health": health, "state_error": state_error},
        )

    def reference(self, topic):
        value = read_reference(topic)
        return envelope(
            status="completed",
            verification={"topic": topic, "content": value},
        )

    def inspect_state(
        self,
        job_id="",
        job_token="",
        scope="state",
        component="",
        reference_topic="contract",
    ):
        if scope == "reference":
            return self.reference(reference_topic)
        if bool(job_id) != bool(job_token):
            raise ValueError(
                "job_id and job_token must be supplied together"
            )
        if not job_id:
            if scope != "state":
                raise ValueError(
                    "A CAD job is required for document analysis"
                )
            return self.status()

        with self._write_lock:
            job = self._load_job(job_id, job_token)
            state, state_response = self._verify_job_document(job)
            if scope == "state":
                return envelope(
                    status="completed",
                    job_id=job_id,
                    target_document=self._target(state),
                    partial_state=state,
                    verification={"state": state_response},
                    **self._job_metadata(job),
                )

            document_type = state.get("documentType")
            params = {}
            if scope == "selection":
                tool = "get_selection"
            elif document_type == "PART" and scope in {
                "features",
                "geometry",
                "mass_properties",
                "bodies",
                "faces",
                "edges",
            }:
                tool = "analyze_model"
                params = {"analysis_type": scope}
            elif document_type == "ASSEMBLY" and scope in {
                "components",
                "components_flat",
                "mates",
                "faces",
                "edges",
            }:
                tool = "analyze_assembly"
                params = {
                    "analysis_type": scope,
                    "component": component,
                }
                if scope in {"faces", "edges"} and not component.strip():
                    raise ValueError(
                        f"component is required for assembly {scope} analysis"
                    )
            elif document_type == "DRAWING" and scope == "drawing":
                tool = "analyze_slddrw_test"
                params = {
                    "include_geometry": False,
                    "include_relations": False,
                }
            else:
                raise ValueError(
                    f"scope '{scope}' is not valid for {document_type or 'no'} document"
                )

            response = backend.call(tool, params)
            if response.get("status") != "COMPLETED":
                return envelope(
                    status="failed",
                    risk="read",
                    job_id=job_id,
                    target_document=self._target(state),
                    partial_state=state,
                    verification={"state": state_response, "analysis": response},
                    error=self._response_error(response),
                    **self._job_metadata(job),
                )
            return envelope(
                status="completed",
                risk="read",
                job_id=job_id,
                target_document=self._target(state),
                partial_state=cad_state(response) or state,
                verification={"state": state_response, "analysis": response},
                **self._job_metadata(job),
            )

    def begin_job(self, intent, target_document="", as_assembly=False):
        with self._write_lock:
            ready = backend.ensure_ready()
            if not ready.get("comAttached"):
                return envelope(
                    status="failed",
                    error={
                        "code": "SOLIDWORKS_NOT_READY",
                        "message": ready.get("launchError")
                        or ready.get("ensureError")
                        or "SolidWorks COM attach failed.",
                    },
                    verification={"readiness": ready},
                )

            completed = ["ensure_ready"]
            if target_document:
                opened = backend.call(
                    "open_document",
                    {
                        "file_path": str(Path(target_document).resolve()),
                        "as_assembly": as_assembly,
                    },
                )
                if opened.get("status") != "COMPLETED":
                    return envelope(
                        status="failed",
                        target_document=target_document,
                        completed_steps=completed,
                        partial_state=cad_state(opened),
                        error=self._response_error(opened),
                    )
                completed.append("open_document")

            verified = backend.verify_state()
            state = (
                cad_state(verified)
                if verified.get("status") == "COMPLETED"
                else {}
            )
            if target_document and not state:
                return envelope(
                    status="failed",
                    target_document=target_document,
                    completed_steps=completed,
                    error=self._response_error(verified),
                )

            job_id = str(uuid.uuid4())
            job, token = job_store.create(
                job_id, intent, self._identity(state)
            )
            job["last_state"] = state
            job_store.save(job)
            return envelope(
                status="completed",
                job_id=job_id,
                target_document=self._target(state),
                completed_steps=completed,
                partial_state=state,
                verification={"readiness": ready},
                job_token=token,
                instruction=(
                    "Keep job_token private and pass it to every later call for this CAD job."
                ),
                **self._job_metadata(job),
            )

    def _load_job(self, job_id, job_token, allow_closed_target=False):
        job = job_store.load(job_id, job_token)
        if job.get("status") != "active":
            raise ValueError(
                f"CAD job {job_id} is {job.get('status')}, not active"
            )
        if job.get("target_closed") and not allow_closed_target:
            raise ValueError(
                f"CAD job {job_id} target document is already closed"
            )
        return job

    def _verify_job_document(self, job, allow_no_document=False):
        response = backend.verify_state()
        if response.get("status") != "COMPLETED":
            error = self._response_error(response)
            if allow_no_document and error.get("code") == "NO_ACTIVE_DOCUMENT":
                return {}, response
            raise RuntimeError(f"{error['code']}: {error['message']}")
        state = cad_state(response)
        expected = job.get("expected_document") or {}
        if expected and not same_document(expected, state):
            raise RuntimeError(
                "ACTIVE_DOCUMENT_CHANGED: expected "
                f"'{expected.get('path') or expected.get('title')}', but SolidWorks has "
                f"'{self._target(state)}' active"
            )
        job["last_state"] = state
        job_store.save(job)
        return state, response

    @staticmethod
    def _safe_name(value):
        text = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in (value or "unsaved")
        )
        return text[:120] or "document"

    def _backup_once(self, job, state):
        source = state.get("activeDocumentPath") or ""
        if not source:
            return None
        source_path = Path(source)
        if not source_path.is_file():
            raise RuntimeError(
                f"BACKUP_SOURCE_MISSING: active document path does not exist: {source}"
            )
        key = normalize_path(source)
        existing = (job.get("backups") or {}).get(key)
        if existing:
            return existing["backup_path"]

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        directory = (
            BACKUP_ROOT
            / self._safe_name(source_path.stem)
            / stamp
        )
        directory.mkdir(parents=True, exist_ok=False)
        destination = directory / source_path.name
        try:
            shutil.copy2(source_path, destination)
            metadata = {
                "job_id": job["job_id"],
                "source_path": str(source_path),
                "backup_path": str(destination),
                "created_at": utc_now(),
                "state_version": state.get("stateVersion"),
                "document_type": state.get("documentType"),
                "was_dirty": state.get("isDirty"),
            }
            (directory / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        job.setdefault("backups", {})[key] = metadata
        job_store.save(job)
        return str(destination)

    def _queue_confirmation(self, job, kind, payload, risk, summary):
        action_id = str(uuid.uuid4())
        confirmation_text = f"CONFIRM {action_id}"
        job.setdefault("pending_actions", {})[action_id] = {
            "action_id": action_id,
            "kind": kind,
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "risk": risk,
            "summary": summary,
            "confirmation_token_hash": "",
            "confirmation_text": confirmation_text,
            "created_at": utc_now(),
            "consumed": False,
        }
        job_store.save(job)
        return envelope(
            status="pending_confirmation",
            risk=risk,
            job_id=job["job_id"],
            action_id=action_id,
            target_document=self._target(job.get("last_state") or {}),
            confirmation_text=confirmation_text,
            action_summary=summary,
            instruction=(
                "Call request_confirmation for this action, show the exact summary "
                "to the user, and call confirm_action only after explicit approval."
            ),
        )

    def _assess_save_or_export(self, job, payload):
        allow_no_document = (
            payload.get("operation") == "close"
            and payload.get("close_all", False)
        )
        state, _ = self._verify_job_document(
            job, allow_no_document=allow_no_document
        )
        normalized = validate_save_or_export_payload(payload, state)
        operation = normalized["operation"]
        risk = "write"
        if operation == "save":
            target = normalized["file_path"] or state.get(
                "activeDocumentPath", ""
            )
            risk = "overwrite" if output_exists(target) else "write"
            summary = f"Save the active document to '{target}'."
        elif operation == "export":
            target = expected_output_paths(normalized, state)[0]
            risk = "overwrite" if output_exists(target) else "write"
            summary = (
                f"Export the active document as {normalized['format']} "
                f"to '{target}'."
            )
        elif operation == "batch_export":
            risk = "batch"
            summary = (
                f"Batch export the active document from base path "
                f"'{normalized['file_path']}' using formats "
                f"{normalized['formats_json']}."
            )
        else:
            dirty_discard = (
                bool(state.get("isDirty"))
                and not normalized["save_before_close"]
            )
            if normalized["close_all"] or dirty_discard:
                risk = "destructive"
            elif (
                normalized["save_before_close"]
                and state.get("activeDocumentPath")
            ):
                risk = "overwrite"
            summary = (
                f"Close {'all documents' if normalized['close_all'] else 'the active document'}; "
                f"save_before_close={normalized['save_before_close']}."
            )
        return normalized, state, risk, summary

    def _reassess_risk(self, kind, payload, job):
        if kind == "feature_graph":
            graph = payload.get("graph")
            if not isinstance(graph, dict):
                raise ValueError("Pending Feature Graph is invalid")
            normalized = {
                "graph": graph,
                "fresh_document": bool(payload.get("fresh_document", True)),
            }
            risk = graph_risk(graph)
            summary = (
                f"Build {len(graph.get('nodes') or [])} Feature Graph nodes "
                f"({'new document' if normalized['fresh_document'] else 'active document'})."
            )
            return normalized, risk, summary
        if kind == "document_edits":
            edits = validate_edits(payload.get("operations"))
            risk = edits_risk(edits)
            summary = "Apply edits: " + ", ".join(
                f"{item['tool']}({json.dumps(item['params'], ensure_ascii=False)})"
                for item in edits
            )
            return {"operations": edits}, risk, summary
        if kind == "drawing_workflow":
            normalized = validate_drawing_workflow(payload)
            count = self._drawing_step_count(normalized)
            risk = "batch" if count > 7 else "write"
            summary = f"Create a drawing and execute {count} drawing steps."
            return normalized, risk, summary
        if kind == "save_or_export":
            normalized, _, risk, summary = self._assess_save_or_export(
                job, payload
            )
            return normalized, risk, summary
        raise ValueError(f"Unsupported pending action kind: {kind}")

    def request_confirmation(self, job_id, job_token, action_id):
        with self._write_lock:
            job = self._load_job(job_id, job_token)
            action = (job.get("pending_actions") or {}).get(action_id)
            if not action:
                raise ValueError(f"Unknown pending action: {action_id}")
            if action.get("consumed"):
                raise ValueError("Pending action has already been consumed")
            if action_expired(action):
                raise ValueError("Pending action has expired")
            if payload_hash(action.get("payload")) != action.get(
                "payload_hash"
            ):
                raise PermissionError(
                    "Pending action parameters changed before confirmation"
                )

            token = new_secret()
            action["confirmation_token_hash"] = secret_hash(token)
            action["confirmation_requested_at"] = utc_now()
            job_store.save(job)
            return envelope(
                status="pending_confirmation",
                risk=action["risk"],
                job_id=job_id,
                action_id=action_id,
                target_document=self._target(job.get("last_state") or {}),
                confirmation_token=token,
                confirmation_text=action["confirmation_text"],
                action_summary=action["summary"],
                instruction=(
                    "Show action_summary to the user. After explicit approval, "
                    "call confirm_action with these exact values."
                ),
                **self._job_metadata(job),
            )

    def confirm_action(
        self,
        job_id,
        job_token,
        action_id,
        confirmation_token,
        user_confirmation,
        action_summary,
    ):
        with self._write_lock:
            job = self._load_job(job_id, job_token)
            action = (job.get("pending_actions") or {}).get(action_id)
            if not action:
                raise ValueError(f"Unknown pending action: {action_id}")
            if action.get("consumed"):
                raise ValueError("Confirmation token has already been consumed")
            if action_expired(action):
                raise ValueError("Confirmation token has expired")
            if not action.get("confirmation_requested_at"):
                raise PermissionError(
                    "Confirmation was not requested for this pending action"
                )
            if not hmac.compare_digest(
                action.get("confirmation_token_hash", ""),
                secret_hash(confirmation_token or ""),
            ):
                raise PermissionError("Invalid confirmation token")
            if user_confirmation.strip() != action.get("confirmation_text"):
                raise PermissionError(
                    f"user_confirmation must exactly equal: {action.get('confirmation_text')}"
                )
            if not isinstance(action_summary, str) or not hmac.compare_digest(
                action_summary, action.get("summary", "")
            ):
                raise PermissionError(
                    "action_summary must exactly equal the pending action summary"
                )
            if payload_hash(action.get("payload")) != action.get("payload_hash"):
                raise PermissionError("Pending action parameters changed after approval")

            kind = action["kind"]
            payload, current_risk, current_summary = self._reassess_risk(
                kind, action["payload"], job
            )
            if (
                current_risk != action["risk"]
                or current_summary != action.get("summary")
            ):
                action["consumed"] = True
                action["consumed_at"] = utc_now()
                changes = []
                if current_risk != action["risk"]:
                    changes.append(
                        f"risk changed from {action['risk']} to {current_risk}"
                    )
                if current_summary != action.get("summary"):
                    changes.append("action summary changed")
                action["stale_reason"] = "; ".join(changes)
                job_store.save(job)
                return self._queue_confirmation(
                    job,
                    kind,
                    payload,
                    current_risk,
                    current_summary,
                )

            action["consumed"] = True
            action["consumed_at"] = utc_now()
            job_store.save(job)
            if kind == "feature_graph":
                return self._execute_feature_graph(job, payload, current_risk)
            if kind == "document_edits":
                return self._execute_document_edits(job, payload, current_risk)
            if kind == "drawing_workflow":
                return self._execute_drawing_workflow(job, payload, current_risk)
            if kind == "save_or_export":
                return self._execute_save_or_export(job, payload, current_risk)
            raise ValueError(f"Unsupported pending action kind: {kind}")

    def feature_graph(self, job_id, job_token, graph_json, fresh_document=True):
        graph = json.loads(graph_json)
        if not isinstance(graph, dict):
            raise ValueError("graph_json must decode to a JSON object")
        payload = {"graph": graph, "fresh_document": bool(fresh_document)}
        risk = graph_risk(graph)
        with self._write_lock:
            job = self._load_job(job_id, job_token)
            if risk == "batch":
                return self._queue_confirmation(
                    job,
                    "feature_graph",
                    payload,
                    risk,
                    f"Build {len(graph.get('nodes') or [])} Feature Graph nodes "
                    f"({'new document' if fresh_document else 'active document'}).",
                )
            return self._execute_feature_graph(job, payload, risk)

    def _execute_feature_graph(self, job, payload, risk):
        fresh_document = payload["fresh_document"]
        backup_path = None
        previous_identity = dict(job.get("expected_document") or {})
        before = {}

        if fresh_document:
            opened = backend.call(
                "open_new_assembly"
                if self._graph_is_assembly(payload["graph"])
                else "open_new_part",
                {},
            )
            if opened.get("status") != "COMPLETED":
                error = self._response_error(opened)
                self._record_failure(
                    job,
                    kind="feature_graph",
                    phase="create_document",
                    error=error,
                    execution_status="failed",
                    verification_status_value="not_run",
                    recovery_status="not_needed",
                )
                return envelope(
                    status="failed",
                    risk=risk,
                    job_id=job["job_id"],
                    target_document=self._target(job.get("last_state") or {}),
                    error=error,
                    **self._job_metadata(job),
                )
            before = cad_state(opened)
            verified = backend.verify_state()
            current = (
                cad_state(verified)
                if verified.get("status") == "COMPLETED"
                else {}
            )
            created_identity = self._identity(before)
            if (
                verified.get("status") != "COMPLETED"
                or not same_document(created_identity, current)
            ):
                recovery_status, recovered_state, recovery = (
                    self._close_created_document(
                        job,
                        kind="feature_graph",
                        created_identity=created_identity,
                        previous_identity=previous_identity,
                    )
                )
                error = {
                    "code": "NEW_DOCUMENT_VERIFICATION_FAILED",
                    "message": (
                        "SolidWorks created a new document but it could not be "
                        "verified as the active transaction target."
                    ),
                }
                self._record_failure(
                    job,
                    kind="feature_graph",
                    phase="create_document",
                    error=error,
                    execution_status=(
                        "failed"
                        if recovery_status == "rolled_back"
                        else "partial"
                    ),
                    verification_status_value="failed",
                    recovery_status=recovery_status,
                    recovery=recovery,
                )
                return envelope(
                    status=(
                        "failed"
                        if recovery_status == "rolled_back"
                        else "partial"
                    ),
                    risk=risk,
                    job_id=job["job_id"],
                    target_document=self._target(recovered_state),
                    partial_state=recovered_state,
                    verification={
                        "state": verified,
                        "all_completed": False,
                        "available": verified.get("status") == "COMPLETED",
                    },
                    error=error,
                    **self._job_metadata(job),
                )
            before = current
            job["expected_document"] = self._identity(current)
            job["last_state"] = current
            job_store.save(job)
        else:
            before, _ = self._verify_job_document(job)
            backup_path = self._backup_once(job, before)

        transaction_name = "DSH Feature Graph"
        begin_error = self._begin_transaction(job, transaction_name)
        if begin_error:
            recovery_status = "backup_only" if backup_path else "not_needed"
            recovery = None
            state = before
            if fresh_document:
                recovery_status, state, recovery = (
                    self._close_created_document(
                        job,
                        kind="feature_graph",
                        created_identity=self._identity(before),
                        previous_identity=previous_identity,
                    )
                )
            self._record_failure(
                job,
                kind="feature_graph",
                phase="begin_transaction",
                error=begin_error,
                execution_status=(
                    "partial"
                    if recovery_status == "rollback_failed"
                    else "failed"
                ),
                verification_status_value="not_run",
                recovery_status=recovery_status,
                recovery=recovery,
            )
            return envelope(
                status="failed"
                if recovery_status != "rollback_failed"
                else "partial",
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(state),
                backup_path=backup_path,
                partial_state=state,
                error=begin_error,
                **self._job_metadata(job),
            )

        response = backend.run_feature_graph(
            payload["graph"], fresh_document=False
        )
        compiler = response.get("compiler") or {}
        completed = [
            item.get("id")
            for item in compiler.get("node_log") or []
            if item.get("status") == "COMPLETED"
        ]
        if response.get("status") != "COMPLETED":
            recovery_status, state, recovery = self._recover_transaction(
                job,
                kind="feature_graph",
                previous_identity=previous_identity
                if fresh_document
                else self._identity(before),
                newly_created=fresh_document,
            )
            verification = {
                "state": recovery.get("verification"),
                "deep_checks": [],
                "all_completed": recovery_status == "rolled_back",
                "available": (
                    (recovery.get("verification") or {}).get("status")
                    == "COMPLETED"
                    or (
                        fresh_document
                        and not previous_identity
                        and (error_from(recovery.get("verification")) or {}).get(
                            "code"
                        )
                        == "NO_ACTIVE_DOCUMENT"
                    )
                ),
            }
            return self._transaction_failure_response(
                job,
                kind="feature_graph",
                phase="execution",
                risk=risk,
                error=response.get("error")
                or {"code": "FEATURE_GRAPH_FAILED", "message": "Compiler failed."},
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={"cad": state, "compiler": compiler},
                target_state=state,
                backup_path=backup_path,
            )

        try:
            state, verification = self._post_verify(job)
        except Exception as exc:  # noqa: BLE001
            state = job.get("last_state") or before
            verification = {
                "state_error": str(exc),
                "deep_checks": [],
                "all_completed": False,
                "available": False,
            }
        if not verification.get("all_completed"):
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="feature_graph",
                    previous_identity=previous_identity
                    if fresh_document
                    else self._identity(before),
                    newly_created=fresh_document,
                )
            )
            return self._transaction_failure_response(
                job,
                kind="feature_graph",
                phase="verification",
                risk=risk,
                error={
                    "code": "POST_VERIFICATION_FAILED",
                    "message": (
                        "The Feature Graph executed, but objective "
                        "post-verification did not pass."
                    ),
                },
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={"cad": recovered_state, "compiler": compiler},
                target_state=recovered_state,
                backup_path=backup_path,
            )

        committed = backend.commit_transaction(transaction_name)
        if committed.get("status") != "COMPLETED":
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="feature_graph",
                    previous_identity=previous_identity
                    if fresh_document
                    else self._identity(before),
                    newly_created=fresh_document,
                )
            )
            return self._transaction_failure_response(
                job,
                kind="feature_graph",
                phase="commit",
                risk=risk,
                error=self._response_error(committed),
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={"cad": recovered_state, "compiler": compiler},
                target_state=recovered_state,
                backup_path=backup_path,
            )

        job["completed_steps"].append(
            {
                "kind": "feature_graph",
                "at": utc_now(),
                "nodes": compiler.get("nodes_completed"),
            }
        )
        self._record_success(job, "feature_graph", "built_unverified")
        return envelope(
            status="completed",
            risk=risk,
            job_id=job["job_id"],
            target_document=self._target(state),
            backup_path=backup_path,
            completed_steps=completed,
            partial_state={"cad": state, "compiler": compiler},
            verification=verification,
            **self._job_metadata(job),
        )

    def document_edits(self, job_id, job_token, operations_json):
        edits = validate_edits(json.loads(operations_json))
        payload = {"operations": edits}
        risk = edits_risk(edits)
        with self._write_lock:
            job = self._load_job(job_id, job_token)
            if risk in ("destructive", "batch"):
                return self._queue_confirmation(
                    job,
                    "document_edits",
                    payload,
                    risk,
                    "Apply edits: "
                    + ", ".join(
                        f"{item['tool']}({json.dumps(item['params'], ensure_ascii=False)})"
                        for item in edits
                    ),
                )
            return self._execute_document_edits(job, payload, risk)

    def _execute_document_edits(self, job, payload, risk):
        before, _ = self._verify_job_document(job)
        backup_path = self._backup_once(job, before)
        completed = []
        step_states = []
        transaction_name = "DSH Document Edits"
        begin_error = self._begin_transaction(job, transaction_name)
        if begin_error:
            self._record_failure(
                job,
                kind="document_edits",
                phase="begin_transaction",
                error=begin_error,
                execution_status="failed",
                verification_status_value="not_run",
                recovery_status=(
                    "backup_only" if backup_path else "not_needed"
                ),
            )
            return envelope(
                status="failed",
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(before),
                backup_path=backup_path,
                partial_state=before,
                error=begin_error,
                **self._job_metadata(job),
            )

        response = None
        failed_operation = None
        for edit in payload["operations"]:
            response = backend.call(edit["tool"], edit["params"])
            if response.get("status") != "COMPLETED":
                failed_operation = edit
                break
            completed.append(edit["tool"])
            verify_response = backend.verify_state()
            verified_state = (
                cad_state(verify_response)
                if verify_response.get("status") == "COMPLETED"
                else {}
            )
            if (
                verify_response.get("status") != "COMPLETED"
                or not same_document(self._identity(before), verified_state)
            ):
                response = {
                    "status": "FAILED",
                    "error": {
                        "code": "POST_STEP_VERIFICATION_FAILED",
                        "message": (
                            f"{edit['tool']} completed, but the active "
                            "document could not be verified."
                        ),
                    },
                }
                failed_operation = edit
                break
            job["last_state"] = verified_state
            job_store.save(job)
            step_states.append(verified_state)

        if failed_operation is not None:
            recovery_status, state, recovery = self._recover_transaction(
                job,
                kind="document_edits",
                previous_identity=self._identity(before),
                newly_created=False,
            )
            recovery_verify = recovery.get("verification") or {}
            verification = {
                "state": recovery_verify,
                "deep_checks": [],
                "all_completed": recovery_status == "rolled_back",
                "available": recovery_verify.get("status") == "COMPLETED",
            }
            return self._transaction_failure_response(
                job,
                kind="document_edits",
                phase="execution",
                risk=risk,
                error=self._response_error(response),
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={
                    "cad": state,
                    "failed_operation": failed_operation,
                    "step_states": step_states,
                },
                target_state=state,
                backup_path=backup_path,
            )

        state, verification = self._post_verify(job)
        if not verification.get("all_completed"):
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="document_edits",
                    previous_identity=self._identity(before),
                    newly_created=False,
                )
            )
            return self._transaction_failure_response(
                job,
                kind="document_edits",
                phase="verification",
                risk=risk,
                error={
                    "code": "POST_VERIFICATION_FAILED",
                    "message": (
                        "The document edits executed, but objective "
                        "post-verification did not pass."
                    ),
                },
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={
                    "cad": recovered_state,
                    "step_states": step_states,
                },
                target_state=recovered_state,
                backup_path=backup_path,
            )

        committed = backend.commit_transaction(transaction_name)
        if committed.get("status") != "COMPLETED":
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="document_edits",
                    previous_identity=self._identity(before),
                    newly_created=False,
                )
            )
            return self._transaction_failure_response(
                job,
                kind="document_edits",
                phase="commit",
                risk=risk,
                error=self._response_error(committed),
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={
                    "cad": recovered_state,
                    "step_states": step_states,
                },
                target_state=recovered_state,
                backup_path=backup_path,
            )

        job["completed_steps"].append(
            {"kind": "document_edits", "at": utc_now(), "operations": completed}
        )
        self._record_success(job, "document_edits", "built_unverified")
        return envelope(
            status="completed",
            risk=risk,
            job_id=job["job_id"],
            target_document=self._target(state),
            backup_path=backup_path,
            completed_steps=completed,
            partial_state={"cad": state, "step_states": step_states},
            verification=verification,
            **self._job_metadata(job),
        )

    @staticmethod
    def _drawing_step_count(payload):
        def enabled(value):
            return value is not None and value is not False

        return (
            1
            + len(payload.get("views") or [])
            + (1 if enabled(payload.get("flat_pattern")) else 0)
            + len(payload.get("section_views") or [])
            + len(payload.get("detail_views") or [])
            + (1 if enabled(payload.get("model_annotations")) else 0)
            + (1 if enabled(payload.get("auto_dimensions")) else 0)
            + (1 if enabled(payload.get("center_marks")) else 0)
            + (1 if enabled(payload.get("bom")) else 0)
            + (1 if enabled(payload.get("auto_balloons")) else 0)
            + (1 if enabled(payload.get("hole_table")) else 0)
            + (1 if enabled(payload.get("standards_check")) else 0)
        )

    def drawing_workflow(self, job_id, job_token, workflow_json):
        payload = validate_drawing_workflow(json.loads(workflow_json))
        risk = "batch" if self._drawing_step_count(payload) > 7 else "write"
        with self._write_lock:
            job = self._load_job(job_id, job_token)
            if risk == "batch":
                return self._queue_confirmation(
                    job,
                    "drawing_workflow",
                    payload,
                    risk,
                    f"Create a drawing and execute {self._drawing_step_count(payload)} "
                    "drawing steps.",
                )
            return self._execute_drawing_workflow(job, payload, risk)

    def _execute_drawing_workflow(self, job, payload, risk):
        previous_identity = dict(job.get("expected_document") or {})
        if previous_identity:
            self._verify_job_document(job)

        completed = []
        step_states = []
        response = backend.call(
            "create_drawing",
            {"model_path": payload.get("model_path", "")},
        )
        if response.get("status") != "COMPLETED":
            error = self._response_error(response)
            self._record_failure(
                job,
                kind="drawing_workflow",
                phase="create_document",
                error=error,
                execution_status="failed",
                verification_status_value="not_run",
                recovery_status="not_needed",
            )
            return envelope(
                status="failed",
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(job.get("last_state") or {}),
                error=error,
                **self._job_metadata(job),
        )

        created_state = cad_state(response)
        created_identity = self._identity(created_state)
        created_verify = backend.verify_state()
        state = (
            cad_state(created_verify)
            if created_verify.get("status") == "COMPLETED"
            else {}
        )
        active_document_changed = (
            created_verify.get("status") == "COMPLETED"
            and created_state
            and not same_document(created_identity, state)
        )
        if active_document_changed:
            error = {
                "code": "ACTIVE_DOCUMENT_CHANGED_DURING_OPERATION",
                "message": (
                    "The active SolidWorks document changed after the drawing "
                    "was created. No further workflow steps or recovery actions "
                    "were attempted."
                ),
            }
            job["expected_document"] = created_identity
            job["last_state"] = state
            self._record_failure(
                job,
                kind="drawing_workflow",
                phase="create_document",
                error=error,
                execution_status="partial",
                verification_status_value="failed",
                recovery_status="rollback_failed",
            )
            return envelope(
                status="partial",
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(state),
                partial_state=state,
                verification={
                    "state": created_verify,
                    "deep_checks": [],
                    "all_completed": False,
                    "available": True,
                },
                error=error,
                **self._job_metadata(job),
            )
        if (
            created_verify.get("status") != "COMPLETED"
            or state.get("documentType") != "DRAWING"
        ):
            recovery_status, recovered_state, recovery = (
                self._close_created_document(
                    job,
                    kind="drawing_workflow",
                    created_identity=created_identity,
                    previous_identity=previous_identity,
                )
            )
            error = {
                "code": "NEW_DRAWING_VERIFICATION_FAILED",
                "message": (
                    "SolidWorks created a drawing, but it could not be "
                    "verified as the active workflow target."
                ),
            }
            self._record_failure(
                job,
                kind="drawing_workflow",
                phase="create_document",
                error=error,
                execution_status=(
                    "failed"
                    if recovery_status == "rolled_back"
                    else "partial"
                ),
                verification_status_value="failed",
                recovery_status=recovery_status,
                recovery=recovery,
            )
            return envelope(
                status=(
                    "failed"
                    if recovery_status == "rolled_back"
                    else "partial"
                ),
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(recovered_state),
                partial_state=recovered_state,
                verification={
                    "state": created_verify,
                    "deep_checks": [],
                    "all_completed": False,
                    "available": (
                        created_verify.get("status") == "COMPLETED"
                    ),
                },
                error=error,
                **self._job_metadata(job),
            )

        completed.append("create_drawing")
        step_states.append(state)
        job["expected_document"] = self._identity(state)
        job["last_state"] = state
        job_store.save(job)

        transaction_name = "DSH Drawing Workflow"
        begin_error = self._begin_transaction(job, transaction_name)
        if begin_error:
            recovery_status, recovered_state, recovery = (
                self._close_created_document(
                    job,
                    kind="drawing_workflow",
                    created_identity=self._identity(state),
                    previous_identity=previous_identity,
                )
            )
            self._record_failure(
                job,
                kind="drawing_workflow",
                phase="begin_transaction",
                error=begin_error,
                execution_status=(
                    "failed"
                    if recovery_status == "rolled_back"
                    else "partial"
                ),
                verification_status_value="not_run",
                recovery_status=recovery_status,
                recovery=recovery,
            )
            return envelope(
                status=(
                    "failed"
                    if recovery_status == "rolled_back"
                    else "partial"
                ),
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(recovered_state),
                completed_steps=completed,
                partial_state=recovered_state,
                error=begin_error,
                **self._job_metadata(job),
            )

        def failed_response(code, message):
            return {
                "status": "FAILED",
                "error": {"code": code, "message": message},
            }

        def run(tool, params):
            verify_before = backend.verify_state()
            state_before = (
                cad_state(verify_before)
                if verify_before.get("status") == "COMPLETED"
                else {}
            )
            if (
                verify_before.get("status") != "COMPLETED"
                or not same_document(job["expected_document"], state_before)
            ):
                return failed_response(
                    "ACTIVE_DOCUMENT_CHANGED",
                    f"The active SolidWorks document changed before {tool}.",
                )

            tool_response = backend.call(tool, params)
            if tool_response.get("status") != "COMPLETED":
                return tool_response
            completed.append(tool)

            verify_after = backend.verify_state()
            state_after = (
                cad_state(verify_after)
                if verify_after.get("status") == "COMPLETED"
                else {}
            )
            if (
                verify_after.get("status") != "COMPLETED"
                or not same_document(job["expected_document"], state_after)
            ):
                return failed_response(
                    "POST_STEP_VERIFICATION_FAILED",
                    f"{tool} completed, but the drawing identity changed.",
                )

            job["last_state"] = state_after
            job_store.save(job)
            step_states.append(state_after)
            return tool_response

        for view in payload.get("views") or []:
            response = run(
                "add_drawing_view",
                {
                    "view_type": view["view_type"],
                    "pos_x": view.get("pos_x", 0.1),
                    "pos_y": view.get("pos_y", 0.1),
                    "scale": view.get("scale", 1.0),
                    "model_path": view.get(
                        "model_path", payload.get("model_path", "")
                    ),
                    "display_mode": view.get("display_mode", ""),
                },
            )
            if response.get("status") != "COMPLETED":
                break

        if (
            response.get("status") == "COMPLETED"
            and payload.get("flat_pattern") not in (None, False)
        ):
            flat = payload["flat_pattern"]
            response = run(
                "add_flat_pattern_view",
                {
                    "pos_x": flat.get("pos_x", 0.1),
                    "pos_y": flat.get("pos_y", 0.1),
                    "scale": flat.get("scale", 1.0),
                    "model_path": flat.get(
                        "model_path", payload.get("model_path", "")
                    ),
                    "config_name": flat.get("config_name", ""),
                    "hide_bend_lines": flat.get("hide_bend_lines", False),
                    "flip_view": flat.get("flip_view", False),
                },
            )
        if response.get("status") == "COMPLETED":
            for section in payload.get("section_views") or []:
                response = run("add_section_view", section)
                if response.get("status") != "COMPLETED":
                    break

        if response.get("status") == "COMPLETED":
            for detail in payload.get("detail_views") or []:
                response = run("create_detail_view", detail)
                if response.get("status") != "COMPLETED":
                    break

        if (
            response.get("status") == "COMPLETED"
            and payload.get("model_annotations") not in (None, False)
        ):
            options = (
                payload["model_annotations"]
                if isinstance(payload["model_annotations"], dict)
                else {}
            )
            response = run(
                "import_model_annotations",
                {
                    "all_views": options.get("all_views", True),
                    "eliminate_duplicates": options.get(
                        "eliminate_duplicates", True
                    ),
                },
            )

        if (
            response.get("status") == "COMPLETED"
            and payload.get("auto_dimensions") not in (None, False)
        ):
            options = (
                payload["auto_dimensions"]
                if isinstance(payload["auto_dimensions"], dict)
                else {}
            )
            response = run(
                "auto_dimension_drawing",
                {
                    "all_views": options.get("all_views", True),
                    "include_unmarked": options.get("include_unmarked", False),
                    "eliminate_duplicates": options.get(
                        "eliminate_duplicates", True
                    ),
                },
            )
        if (
            response.get("status") == "COMPLETED"
            and payload.get("center_marks") not in (None, False)
        ):
            options = (
                payload["center_marks"]
                if isinstance(payload["center_marks"], dict)
                else {}
            )
            response = run(
                "auto_center_marks",
                {
                    "include_slots": options.get("include_slots", True),
                    "extended_lines": options.get("extended_lines", True),
                },
            )

        if (
            response.get("status") == "COMPLETED"
            and payload.get("bom") not in (None, False)
        ):
            options = payload["bom"] if isinstance(payload["bom"], dict) else {}
            response = run(
                "create_bom",
                {
                    "bom_type": options.get("bom_type", "top_level"),
                    "template_path": options.get("template_path", ""),
                },
            )

        if (
            response.get("status") == "COMPLETED"
            and payload.get("auto_balloons") not in (None, False)
        ):
            options = (
                payload["auto_balloons"]
                if isinstance(payload["auto_balloons"], dict)
                else {}
            )
            response = run(
                "add_balloons",
                {
                    "view_name": options.get("view_name", ""),
                    "style": options.get("style", "circular"),
                },
            )

        if (
            response.get("status") == "COMPLETED"
            and payload.get("hole_table") not in (None, False)
        ):
            options = (
                payload["hole_table"]
                if isinstance(payload["hole_table"], dict)
                else {}
            )
            response = run(
                "create_hole_table",
                {
                    "view_name": options.get("view_name", ""),
                    "tag_style": options.get("tag_style", "numeric"),
                },
            )

        if (
            response.get("status") == "COMPLETED"
            and payload.get("standards_check") not in (None, False)
        ):
            options = (
                payload["standards_check"]
                if isinstance(payload["standards_check"], dict)
                else {}
            )
            response = run(
                "check_drawing_standards",
                {
                    "expected_view_count": options.get(
                        "expected_view_count", 0
                    ),
                    "require_dimensions": options.get(
                        "require_dimensions", True
                    ),
                    "require_resolved_references": options.get(
                        "require_resolved_references", True
                    ),
                    "max_independent_scales": options.get(
                        "max_independent_scales", 0
                    ),
                },
            )

        if response.get("status") != "COMPLETED":
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="drawing_workflow",
                    previous_identity=previous_identity,
                    newly_created=True,
                )
            )
            recovery_verify = recovery.get("verification") or {}
            verification = {
                "state": recovery_verify,
                "deep_checks": [],
                "all_completed": recovery_status == "rolled_back",
                "available": (
                    recovery_verify.get("status") == "COMPLETED"
                    or (
                        not previous_identity
                        and (error_from(recovery_verify) or {}).get("code")
                        == "NO_ACTIVE_DOCUMENT"
                    )
                ),
            }
            return self._transaction_failure_response(
                job,
                kind="drawing_workflow",
                phase="execution",
                risk=risk,
                error=self._response_error(response),
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={
                    "cad": recovered_state,
                    "step_states": step_states,
                },
                target_state=recovered_state,
            )

        state, verification = self._post_verify(job)
        if not verification.get("all_completed"):
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="drawing_workflow",
                    previous_identity=previous_identity,
                    newly_created=True,
                )
            )
            return self._transaction_failure_response(
                job,
                kind="drawing_workflow",
                phase="verification",
                risk=risk,
                error={
                    "code": "POST_VERIFICATION_FAILED",
                    "message": (
                        "The drawing workflow executed, but objective "
                        "post-verification did not pass."
                    ),
                },
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={
                    "cad": recovered_state,
                    "step_states": step_states,
                },
                target_state=recovered_state,
            )

        committed = backend.commit_transaction(transaction_name)
        if committed.get("status") != "COMPLETED":
            recovery_status, recovered_state, recovery = (
                self._recover_transaction(
                    job,
                    kind="drawing_workflow",
                    previous_identity=previous_identity,
                    newly_created=True,
                )
            )
            return self._transaction_failure_response(
                job,
                kind="drawing_workflow",
                phase="commit",
                risk=risk,
                error=self._response_error(committed),
                verification=verification,
                recovery_status=recovery_status,
                recovery_event=recovery,
                completed_steps=completed,
                partial_state={
                    "cad": recovered_state,
                    "step_states": step_states,
                },
                target_state=recovered_state,
            )

        job["completed_steps"].append(
            {"kind": "drawing_workflow", "at": utc_now(), "operations": completed}
        )
        self._record_success(job, "drawing_workflow", "drawing_consistent")
        return envelope(
            status="completed",
            risk=risk,
            job_id=job["job_id"],
            target_document=self._target(state),
            completed_steps=completed,
            partial_state={"cad": state, "step_states": step_states},
            verification=verification,
            **self._job_metadata(job),
        )

    def save_or_export(
        self,
        job_id,
        job_token,
        operation,
        file_path="",
        format="",
        formats_json="[]",
        save_before_close=False,
        close_all=False,
    ):
        payload = {
            "operation": operation,
            "file_path": file_path,
            "format": format,
            "formats_json": formats_json,
            "save_before_close": save_before_close,
            "close_all": close_all,
        }
        with self._write_lock:
            job = self._load_job(job_id, job_token)
            payload, _, risk, summary = self._assess_save_or_export(
                job, payload
            )

            if risk in ("destructive", "overwrite", "batch"):
                return self._queue_confirmation(
                    job, "save_or_export", payload, risk, summary
                )
            return self._execute_save_or_export(job, payload, risk)

    def _execute_save_or_export(self, job, payload, risk):
        state, _ = self._verify_job_document(
            job, allow_no_document=payload.get("close_all", False)
        )
        operation = payload["operation"]
        backup_path = None
        if operation == "save" or (
            operation == "close" and payload.get("save_before_close")
        ):
            backup_path = self._backup_once(job, state)

        if operation == "save":
            response = backend.call(
                "save_document", {"file_path": payload.get("file_path", "")}
            )
        elif operation == "export":
            response = backend.call(
                "export_document",
                {
                    "format": payload.get("format", ""),
                    "file_path": payload.get("file_path", ""),
                },
            )
        elif operation == "batch_export":
            response = backend.call(
                "batch_export",
                {
                    "file_path_base": payload.get("file_path", ""),
                    "formats_json": payload.get("formats_json", "[]"),
                },
            )
        else:
            response = self._close_document(
                self._identity(state),
                save=payload.get("save_before_close", False),
                close_all=payload.get("close_all", False),
            )

        if response.get("status") != "COMPLETED":
            error = self._response_error(response)
            self._record_failure(
                job,
                kind="save_or_export",
                phase="execution",
                error=error,
                execution_status="failed",
                verification_status_value="not_run",
                recovery_status=(
                    "backup_only" if backup_path else "not_needed"
                ),
            )
            return envelope(
                status="failed",
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(state),
                backup_path=backup_path,
                partial_state=state,
                error=error,
                **self._job_metadata(job),
            )

        output_paths = expected_output_paths(payload, state)
        if (
            operation == "close"
            and payload.get("save_before_close")
            and state.get("activeDocumentPath")
        ):
            output_paths = [state["activeDocumentPath"]]
        output_checks = {
            path: output_file_status(path)
            for path in output_paths if path
        }

        if operation == "close":
            close_verify = backend.verify_state()
            close_error = error_from(close_verify)
            after = (
                cad_state(close_verify)
                if close_verify.get("status") == "COMPLETED"
                else {}
            )
            target_identity = self._identity(state)
            no_active_document = (
                close_error or {}
            ).get("code") == "NO_ACTIVE_DOCUMENT"
            target_closed = (
                no_active_document
                or (
                    close_verify.get("status") == "COMPLETED"
                    and (
                        not target_identity
                        or not same_document(target_identity, after)
                    )
                )
            )
            verification = {
                "state": close_verify,
                "deep_checks": [],
                "all_completed": target_closed,
                "available": (
                    close_verify.get("status") == "COMPLETED"
                    or no_active_document
                ),
                "output_files": output_checks,
                "target_closed": target_closed,
                "closed_document": self._identity(state),
            }
        else:
            after, verification = self._post_verify(
                job, update_identity=(operation == "save")
            )
            verification["output_files"] = output_checks

        invalid_outputs = [
            path
            for path, check in output_checks.items()
            if not check["valid"]
        ]
        verification["all_outputs_valid"] = not invalid_outputs
        verification["all_completed"] = bool(
            verification.get("all_completed")
            and not invalid_outputs
        )
        if not verification["all_completed"]:
            if invalid_outputs:
                error = {
                    "code": "OUTPUT_VERIFICATION_FAILED",
                    "message": (
                        "Expected output files are missing or empty: "
                        + ", ".join(invalid_outputs)
                    ),
                }
            else:
                error = {
                    "code": "POST_VERIFICATION_FAILED",
                    "message": (
                        f"{operation} executed, but objective "
                        "post-verification did not pass."
                    ),
                }
            self._record_failure(
                job,
                kind="save_or_export",
                phase="verification",
                error=error,
                execution_status="completed",
                verification_status_value=verification_status(verification),
                recovery_status=(
                    "backup_only" if backup_path else "not_needed"
                ),
            )
            return envelope(
                status="verification_failed",
                risk=risk,
                job_id=job["job_id"],
                target_document=self._target(after),
                backup_path=backup_path,
                completed_steps=[operation],
                partial_state=after,
                verification=verification,
                error=error,
                **self._job_metadata(job),
            )

        if operation == "close":
            job["closed_document"] = self._identity(state)
            job["target_closed"] = True
            job["expected_document"] = {}
            job["last_state"] = {}
        job["completed_steps"].append(
            {"kind": "save_or_export", "at": utc_now(), "operation": operation}
        )
        self._record_success(job, "save_or_export", "verified")
        return envelope(
            status="completed",
            risk=risk,
            job_id=job["job_id"],
            target_document=self._target(after),
            backup_path=backup_path,
            completed_steps=[operation],
            partial_state=after,
            verification=verification,
            **self._job_metadata(job),
        )

    def _post_verify(self, job, update_identity=False):
        response = backend.verify_state()
        if response.get("status") != "COMPLETED":
            return {}, {
                "state": response,
                "deep_checks": [],
                "all_completed": False,
                "available": False,
            }
        state = cad_state(response)
        identity_match = True
        identity_error = None
        if update_identity or not job.get("expected_document"):
            job["expected_document"] = self._identity(state)
        elif not same_document(job["expected_document"], state):
            identity_match = False
            identity_error = (
                "ACTIVE_DOCUMENT_CHANGED_DURING_OPERATION: expected "
                f"'{job['expected_document'].get('path') or job['expected_document'].get('title')}', "
                f"got '{self._target(state)}'"
            )
        job["last_state"] = state
        job_store.save(job)

        doc_type = state.get("documentType")
        checks = []
        if doc_type == "PART":
            checks = [
                backend.call("analyze_model", {"analysis_type": "geometry"}),
                backend.call(
                    "analyze_model", {"analysis_type": "mass_properties"}
                ),
            ]
        elif doc_type == "ASSEMBLY":
            checks = [
                backend.call(
                    "analyze_assembly",
                    {"analysis_type": "components", "component": ""},
                ),
                backend.call(
                    "analyze_assembly",
                    {"analysis_type": "mates", "component": ""},
                ),
            ]
        elif doc_type == "DRAWING":
            checks = [
                backend.call(
                    "analyze_slddrw_test",
                    {"include_geometry": False, "include_relations": False},
                )
            ]
        result = {
            "state": response,
            "deep_checks": checks,
            "all_completed": bool(
                identity_match
                and checks
                and all(
                    check.get("status") == "COMPLETED"
                    for check in checks
                )
            ),
            "available": True,
            "identity_match": identity_match,
        }
        if identity_error:
            result["identity_error"] = identity_error
        return state, result

    def finish_job(self, job_id, job_token):
        with self._write_lock:
            job = self._load_job(
                job_id, job_token, allow_closed_target=True
            )
            if job.get("target_closed"):
                state = {}
                verification = {
                    "state": {},
                    "deep_checks": [],
                    "all_completed": True,
                    "available": True,
                    "target_closed": True,
                    "closed_document": job.get("closed_document") or {},
                }
            else:
                state, verification = self._post_verify(job)
            if not verification.get("all_completed"):
                error = {
                    "code": "FINAL_VERIFICATION_FAILED",
                    "message": (
                        "The CAD job remains active because final objective "
                        "verification did not pass."
                    ),
                }
                self._record_failure(
                    job,
                    kind="finish_job",
                    phase="verification",
                    error=error,
                    execution_status=job.get(
                        "execution_status", "not_started"
                    ),
                    verification_status_value=verification_status(
                        verification
                    ),
                    recovery_status=job.get(
                        "recovery_status", "not_needed"
                    ),
                )
                return envelope(
                    status="verification_failed",
                    job_id=job_id,
                    target_document=self._target(state),
                    completed_steps=job.get("completed_steps") or [],
                    partial_state=state,
                    verification=verification,
                    error=error,
                    **self._job_metadata(job),
                )
            job["status"] = "completed"
            job["finished_at"] = utc_now()
            job["execution_status"] = "completed"
            job["verification_status"] = "passed"
            job["recovery_status"] = "not_needed"
            job["trust_level"] = "verified"
            job["error"] = None
            job_store.save(job)
            return envelope(
                status="completed",
                job_id=job_id,
                target_document=self._target(state),
                completed_steps=job.get("completed_steps") or [],
                partial_state=state,
                verification=verification,
                **self._job_metadata(job),
            )


orchestrator = CadOrchestrator()
