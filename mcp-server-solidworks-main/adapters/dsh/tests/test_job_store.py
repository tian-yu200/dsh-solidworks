import json
import threading
import time

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


def test_concurrent_writers_do_not_collide_on_the_temp_file(tmp_path):
    # Two JobStore instances stand in for two server processes sharing one state root: the
    # per-instance lock does not serialize them, so a shared "*.tmp" name used to make saves
    # fail with WinError 32. Each save now uses its own temp name.
    first = JobStore(tmp_path)
    second = JobStore(tmp_path)
    first.create("job-1", "intent", {})
    payload = {"job_id": "job-1", "pad": "X" * 50_000}
    errors = []

    def writer(store, tag):
        for index in range(40):
            record = dict(payload)
            record.update(tag=tag, index=index)
            try:
                store.save(record)
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=writer, args=(first, "A")),
        threading.Thread(target=writer, args=(second, "B")),
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    assert errors == []
    assert elapsed < 2.0
    persisted = json.loads(
        (tmp_path / "job-1.json").read_text(encoding="utf-8")
    )
    assert persisted["job_id"] == "job-1"
    assert not list(tmp_path.glob("*.tmp"))
