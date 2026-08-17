from datetime import datetime, timezone


EXECUTION_STATUSES = {
    "not_started",
    "completed",
    "failed",
    "partial",
}
VERIFICATION_STATUSES = {
    "not_run",
    "passed",
    "failed",
    "unavailable",
}
RECOVERY_STATUSES = {
    "not_needed",
    "rolled_back",
    "rollback_failed",
    "backup_only",
}
TRUST_LEVELS = {
    "unverified",
    "built_unverified",
    "drawing_consistent",
    "verified",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def cad_state(response):
    return (response or {}).get("cadState") or {}


def error_from(response):
    error = (response or {}).get("error")
    if not error:
        return None
    if isinstance(error, dict):
        return {
            "code": error.get("code", "UNKNOWN"),
            "message": error.get("message", str(error)),
        }
    return {"code": "UNKNOWN", "message": str(error)}


def envelope(
    *,
    status,
    risk="read",
    job_id=None,
    action_id=None,
    target_document=None,
    backup_path=None,
    completed_steps=None,
    partial_state=None,
    verification=None,
    error=None,
    **extra,
):
    result = {
        "job_id": job_id,
        "action_id": action_id,
        "status": status,
        "risk": risk,
        "target_document": target_document,
        "backup_path": backup_path,
        "completed_steps": completed_steps or [],
        "partial_state": partial_state or {},
        "verification": verification or {},
        "error": error,
    }
    result.update(extra)
    return result


def verification_status(verification):
    verification = verification or {}
    if verification.get("all_completed") is True:
        return "passed"
    if verification.get("available") is False:
        return "unavailable"
    return "failed"
