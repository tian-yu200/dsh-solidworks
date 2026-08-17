import hmac
import json
import os
import re
import threading
from pathlib import Path

from models import utc_now
from policy import new_secret, secret_hash
from settings import JOB_ROOT


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
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(job, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp, path)

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
