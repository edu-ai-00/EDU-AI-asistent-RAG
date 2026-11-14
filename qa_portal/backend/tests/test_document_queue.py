"""Document ingest queue (CONTRACT.md §14.5): FIFO, result mapping, caps,
restart, pruning, stored_chunks lookup, the worker thread and lifespan."""
from __future__ import annotations

import datetime as dt
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.errors import AppError
from app.services import document_queue as q
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient

PARAMS = {"visibility": "entity", "audience_roles": ["PARENT"], "document_type": None,
          "document_language": None, "school_subject": None, "school_grade_codes": [],
          "domain_codes": [], "valid_from": None, "valid_to": None, "file_name": None,
          "save_level_1_child": False, "max_child_links": None}
BATCH = "22222222-0000-0000-0000-000000000001"


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state")


@pytest.fixture
def ingest():
    return FakeIngestClient()


def _url_job(store, url="https://skola.example/a", school="ZS_DEMO", batch=BATCH):
    return q.enqueue_job(store, job_id=q.new_job_id(), school=school, batch_id=batch,
                         source_kind="url", source=url,
                         params={**PARAMS, "max_child_links": 5}, user_id="qa_portal__ZS_DEMO",
                         principal_subject="vpn-admin", payload_part=None)


def _file_job(store, content=b"data", name="a.pdf"):
    job_id = q.new_job_id()
    part = store.job_file_path(f"{job_id}.part")
    part.write_bytes(content)
    return q.enqueue_job(store, job_id=job_id, school="ZS_DEMO", batch_id=BATCH,
                         source_kind="file", source=name, params={**PARAMS, "file_name": name},
                         user_id="qa_portal__ZS_DEMO", principal_subject="vpn-admin",
                         payload_part=part)


def _job(store, job_id):
    return store.get("jobs", job_id)


def _set(store, job_id, **fields):
    job = _job(store, job_id)
    job.update(fields)
    store.save("jobs", job_id, job)


def test_fifo_by_created_at(store, ingest):
    urls = [f"https://skola.example/{i}" for i in range(3)]
    jobs = [_url_job(store, u) for u in urls]
    assert [q.process_next_job(store, ingest) for _ in range(3)] == [j["job_id"] for j in jobs]
    assert [r["url"] for r in ingest.document_received] == urls
    assert q.process_next_job(store, ingest) is None


def test_processing_job_is_not_picked_twice(store, ingest):
    first = _url_job(store)
    second = _url_job(store)
    claimed = q._claim_next(store)
    assert claimed["job_id"] == first["job_id"]
    assert _job(store, first["job_id"])["status"] == "processing"
    assert q._claim_next(store)["job_id"] == second["job_id"]
    assert q._claim_next(store) is None


def test_file_job_completed_and_payload_removed(store, ingest):
    job = _file_job(store, b"%PDF")
    assert store.job_file_path(job["job_id"]).read_bytes() == b"%PDF"
    q.process_next_job(store, ingest)
    done = _job(store, job["job_id"])
    assert done["status"] == "completed"
    assert done["processing_uuid"] == "99999999-0000-0000-0000-000000000001"
    assert done["result"] == {"status": "completed", "total_files": 1, "ok_count": 1,
                              "error_count": 0}
    assert done["started_at"] and done["finished_at"] and done["error"] is None
    assert ingest.document_received[0]["file"] == b"%PDF"
    assert ingest.document_received[0]["filename"] == "a.pdf"
    assert not store.job_file_path(job["job_id"]).exists()


def test_partial_counts_as_completed(store, ingest):
    ingest.document_result = {**ingest.document_result, "status": "partial", "error_count": 1}
    job = _url_job(store)
    q.process_next_job(store, ingest)
    assert _job(store, job["job_id"])["status"] == "completed"


def test_gateway_status_failed_is_failed(store, ingest):
    ingest.document_result = {**ingest.document_result, "status": "failed", "ok_count": 0}
    job = _file_job(store)
    q.process_next_job(store, ingest)
    done = _job(store, job["job_id"])
    assert done["status"] == "failed"
    assert done["error"]["code"] == "UPSTREAM_ERROR"
    assert done["result"]["status"] == "failed"
    assert not store.job_file_path(job["job_id"]).exists()


@pytest.mark.parametrize("fail,code", [("rejected", "BAD_REQUEST"),
                                       ("unavailable", "UPSTREAM_ERROR"),
                                       ("crash", "INTERNAL_ERROR")])
def test_gateway_errors_fail_the_job(store, ingest, fail, code):
    ingest.document_fail = fail
    job = _file_job(store)
    q.process_next_job(store, ingest)
    done = _job(store, job["job_id"])
    assert done["status"] == "failed" and done["error"]["code"] == code
    assert done["finished_at"]
    assert not store.job_file_path(job["job_id"]).exists()
    if fail == "rejected":
        assert done["error"]["message"] == "Unknown document_type"   # gateway detail
    # no automatic retry
    assert q.process_next_job(store, ingest) is None


def test_missing_payload_fails_the_job(store, ingest):
    job = _file_job(store)
    store.job_file_path(job["job_id"]).unlink()
    q.process_next_job(store, ingest)
    assert _job(store, job["job_id"])["error"]["code"] == "INTERNAL_ERROR"
    assert ingest.document_received == []


def test_url_job_sends_url_options(store, ingest):
    _url_job(store, "https://skola.example/x")
    q.process_next_job(store, ingest)
    sent = ingest.document_received[0]
    assert sent["url"] == "https://skola.example/x"
    assert sent["max_child_links"] == 5 and sent["save_level_1_child"] is False
    assert sent["user_id"] == "qa_portal__ZS_DEMO" and sent["legal_entity"] == "ZS_DEMO"


def test_batch_cap(store, monkeypatch):
    monkeypatch.setattr(settings, "max_document_batch_items", 2)
    _url_job(store)
    _url_job(store)
    with pytest.raises(AppError) as e:
        _url_job(store)
    assert e.value.status_code == 422 and e.value.fields[0]["field"] == "batch_id"
    _url_job(store, batch="22222222-0000-0000-0000-000000000002")


def test_school_cap_counts_only_queued_and_processing(store, ingest, monkeypatch):
    monkeypatch.setattr(settings, "document_queue_school_cap", 2)
    _url_job(store, batch=str(uuid.uuid4()))
    _url_job(store, batch=str(uuid.uuid4()))
    with pytest.raises(AppError) as e:
        _url_job(store, batch=str(uuid.uuid4()))
    assert e.value.status_code == 429 and e.value.code == "INGEST_QUEUE_FULL"
    assert e.value.retryable is True
    _url_job(store, school="ZS_OTHER")           # another school has its own cap
    q.process_next_job(store, ingest)             # one finishes -> room again
    _url_job(store, batch=str(uuid.uuid4()))


def test_restart_reconcile(store):
    queued = _url_job(store)
    processing = _file_job(store)
    _set(store, processing["job_id"], status="processing", started_at=q._now())
    assert q.reconcile_jobs_on_startup(store) == 1
    assert _job(store, queued["job_id"])["status"] == "queued"
    unknown = _job(store, processing["job_id"])
    assert unknown["status"] == "unknown" and unknown["finished_at"]
    assert not store.job_file_path(processing["job_id"]).exists()


def test_prune_old_finished_jobs_and_orphans(store, ingest):
    old = _url_job(store)
    recent = _url_job(store)
    queued_old = _url_job(store)
    q.process_next_job(store, ingest)
    q.process_next_job(store, ingest)
    now = dt.datetime.now(dt.timezone.utc)
    _set(store, old["job_id"], finished_at=(now - dt.timedelta(days=8)).isoformat())
    _set(store, queued_old["job_id"], created_at=(now - dt.timedelta(days=30)).isoformat())
    orphan = store.job_file_path("orphan.part")
    orphan.write_bytes(b"x")
    stale = time.time() - 2 * 3600
    os.utime(orphan, (stale, stale))
    fresh_orphan = store.job_file_path("fresh.part")
    fresh_orphan.write_bytes(b"x")

    assert q.prune_jobs(store) == 1
    assert _job(store, old["job_id"]) is None
    assert _job(store, recent["job_id"]) is not None
    assert _job(store, queued_old["job_id"])["status"] == "queued"
    assert not orphan.exists()
    assert fresh_orphan.exists()          # may still be an upload in progress


def test_prune_keeps_payload_of_queued_job(store):
    job = _file_job(store)
    payload = store.job_file_path(job["job_id"])
    stale = time.time() - 2 * 3600
    os.utime(payload, (stale, stale))
    q.prune_jobs(store)
    assert payload.exists()


# ── stored_chunks ───────────────────────────────────────────────────────
PROC = "99999999-0000-0000-0000-000000000001"


def test_stored_chunks_looked_up_by_known_processing_uuid(store, ingest):
    job = _url_job(store)
    q.process_next_job(store, ingest)
    ingest.status_by_uuid[PROC] = {"processing_uuid": PROC,
                                   "files": [{"chunks": 4}, {"chunks": 3}, {"chunks": None}]}
    jobs = q.list_jobs(store, ingest, "ZS_DEMO", None)
    assert jobs[0]["stored_chunks"] == 7
    assert ingest.status_calls == [PROC]
    assert _job(store, job["job_id"])["stored_chunks"] == 7
    q.list_jobs(store, ingest, "ZS_DEMO", None)
    assert ingest.status_calls == [PROC]         # a positive value is not looked up again


def test_stored_chunks_zero_stays_unset_and_is_retried(store, ingest):
    _url_job(store)
    q.process_next_job(store, ingest)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 0}]}
    assert q.list_jobs(store, ingest, "ZS_DEMO", None)[0]["stored_chunks"] is None
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 2}]}
    assert q.list_jobs(store, ingest, "ZS_DEMO", None)[0]["stored_chunks"] == 2


def test_stored_chunks_lookup_failure_leaves_it_unchanged(store, ingest):
    _url_job(store)
    q.process_next_job(store, ingest)
    ingest.status_fail = True
    assert q.list_jobs(store, ingest, "ZS_DEMO", None)[0]["stored_chunks"] is None


def test_stored_chunks_not_looked_up_after_window_or_when_failed(store, ingest):
    old = _url_job(store)
    q.process_next_job(store, ingest)
    finished = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)
    _set(store, old["job_id"], finished_at=finished.isoformat())
    ingest.document_fail = "unavailable"
    _url_job(store)
    q.process_next_job(store, ingest)
    _url_job(store)                                  # still queued
    q.list_jobs(store, ingest, "ZS_DEMO", None)
    assert ingest.status_calls == []


# ── worker thread and lifespan ──────────────────────────────────────────
def test_worker_thread_processes_queue(store, ingest):
    worker = q.DocumentQueueWorker(store, ingest, idle_seconds=0.05)
    worker.start()
    try:
        job = _url_job(store)
        deadline = time.monotonic() + 5
        while _job(store, job["job_id"])["status"] != "completed":
            assert time.monotonic() < deadline, "worker did not process the job"
            time.sleep(0.02)
    finally:
        worker.stop()
    assert not worker._thread.is_alive()


def test_lifespan_starts_and_stops_worker(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    ingest = FakeIngestClient()
    started = []
    real_start = q.start_worker

    def spy(s, i):
        w = real_start(s, i)
        started.append(w)
        return w
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "get_ingest_client", lambda: ingest)
    monkeypatch.setattr(main.document_queue, "start_worker", spy)
    monkeypatch.setattr(settings, "document_worker_enabled", True)
    stuck = _file_job(store)
    _set(store, stuck["job_id"], status="processing")
    with TestClient(main.app) as client:
        assert client.get("/qa-api/v1/health").status_code == 200
        assert _job(store, stuck["job_id"])["status"] == "unknown"   # reconciled at startup
    assert len(started) == 1 and not started[0]._thread.is_alive()


def test_lifespan_survives_broken_state_and_disabled_worker(monkeypatch):
    def broken():
        raise OSError("volume not mounted")
    monkeypatch.setattr(main, "get_store", broken)
    monkeypatch.setattr(settings, "document_worker_enabled", False)
    called = []
    monkeypatch.setattr(main.document_queue, "start_worker", lambda *a: called.append(a))
    with TestClient(main.app) as client:
        assert client.get("/qa-api/v1/health").status_code == 200
    assert called == []


def test_lifespan_survives_worker_start_failure(tmp_path, monkeypatch):
    def broken(*_a):
        raise RuntimeError("no threads")
    monkeypatch.setattr(main, "get_store", lambda: StateStore(tmp_path / "s"))
    monkeypatch.setattr(main, "get_ingest_client", lambda: FakeIngestClient())
    monkeypatch.setattr(main.document_queue, "start_worker", broken)
    monkeypatch.setattr(settings, "document_worker_enabled", True)
    with TestClient(main.app) as client:
        assert client.get("/qa-api/v1/health").status_code == 200
