import json

import pytest

from job_store import JobStore


def test_job_token_is_hashed_and_wrong_token_is_rejected(tmp_path):
    store = JobStore(tmp_path)
    job, token = store.create("job-token-test", "intent", {})

    persisted = json.loads(
        (tmp_path / "job-token-test.json").read_text(encoding="utf-8")
    )
    assert token not in (tmp_path / "job-token-test.json").read_text(
        encoding="utf-8"
    )
    assert persisted["token_hash"] == job["token_hash"]
    assert persisted["execution_status"] == "not_started"
    assert persisted["verification_status"] == "not_run"
    assert persisted["recovery_status"] == "not_needed"
    assert persisted["trust_level"] == "unverified"
    assert persisted["failure_history"] == []
    assert persisted["recovery_history"] == []
    assert store.load("job-token-test", token)["job_id"] == "job-token-test"
    with pytest.raises(PermissionError, match="Invalid job token"):
        store.load("job-token-test", "wrong-token")


def test_job_id_cannot_escape_store_root(tmp_path):
    store = JobStore(tmp_path)
    with pytest.raises(ValueError, match="Invalid CAD job ID"):
        store.load("../outside", "irrelevant")
