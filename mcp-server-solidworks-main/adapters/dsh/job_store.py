import hmac
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from models import utc_now
from policy import new_secret, secret_hash
from settings import JOB_ROOT

# On Windows a concurrent reader/replacer can hold the target open for a moment, so a
# cross-process replace may fail with a sharing violation even though it is retryable.
_REPLACE_ATTEMPTS = 20
_REPLACE_BACKOFF_SECONDS = 0.01


class JobStore:
    def __init__(self, root=JOB_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id
        ):
            raise ValueError("Invalid CAD job ID")
        return self.root / f"{job_id}.json"

    def create(self, job_id, intent, expected_document):
        token = new_secret()
        now = utc_now()
        job = {
            "job_id": job_id,
            "token_hash": secret_hash(token),
            "intent": intent,
            "status": "active",
            "execution_status": "not_started",
            "verification_status": "not_run",
            "recovery_status": "not_needed",
            "trust_level": "unverified",
            "created_at": now,
            "updated_at": now,
            "expected_document": expected_document or {},
            "backups": {},
            "completed_steps": [],
            "pending_actions": {},
            "failure_history": [],
            "recovery_history": [],
            "last_state": {},
            "error": None,
        }
        self.save(job)
        return job, token

    def save(self, job):
        with self._lock:
            job["updated_at"] = utc_now()
            path = self._path(job["job_id"])
            # Per-writer temp name: the lock only serializes THIS instance, so two server
            # processes saving the same job would otherwise collide on a shared "*.tmp".
            # os.replace stays atomic, so readers never observe a partial file.
            temp = path.with_name(
                f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temp.write_text(
                    json.dumps(job, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._replace_with_retry(temp, path)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass

    @staticmethod
    def _replace_with_retry(temp, path):
        """Atomically swap temp -> path, retrying the Windows sharing violation.

        A concurrent cross-process reader or writer can hold the destination open just long
        enough for `os.replace` to raise PermissionError (WinError 5/32). That is transient,
        so back off and retry instead of failing the whole save.
        """
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_SECONDS)

    def load(self, job_id, token):
        with self._lock:
            path = self._path(job_id)
            if not path.is_file():
                raise ValueError(f"Unknown CAD job: {job_id}")
            job = json.loads(path.read_text(encoding="utf-8"))
            if not hmac.compare_digest(
                job.get("token_hash", ""), secret_hash(token or "")
            ):
                raise PermissionError("Invalid job token")
            return job


job_store = JobStore()
