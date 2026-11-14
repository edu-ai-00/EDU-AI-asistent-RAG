"""CONTRACT.md §14.14 (v1.8.0): unchanged content is skipped, the content hash
is recorded once the chunks are stored, `skipped` counts as finished."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging

import pytest
from fastapi.testclient import TestClient

from app.adapters.search import SearchNotFound, SearchUnavailable
from app.api.deps import get_ingest_client, get_search_client, get_store
from app.config import settings
from app.main import app
from app.services import document_queue as q
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import DOC_UUID, DOCUMENT, FakeSearchClient
from tests.test_document_queue import PARAMS, PROC, _file_job, _job, _set, _url_job

CONTENT = b"%PDF-1.4 same bytes"
SHA = hashlib.sha256(CONTENT).hexdigest()
OTHER_SHA = hashlib.sha256(b"different bytes").hexdigest()
UPLOADER = "QA portal (ZS_DEMO)"           # display_name of the technical uploader
URL = "https://skola.example/a"
WEB_UUID = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state")


@pytest.fixture
def search():
    s = FakeSearchClient()
    doc = s._documents["ZS_DEMO"][DOC_UUID]
    doc.update(original_file_name="a.pdf", user_name=UPLOADER, content_sha256=SHA)
    return s


@pytest.fixture
def ingest(search):
    return FakeIngestClient(search)


def _add_web_doc(search, **changes):
    doc = {**DOCUMENT, "file_uuid": WEB_UUID, "title": URL, "source_kind": "web",
           "source_url": URL, "original_file_name": None, "user_name": UPLOADER,
           "content_sha256": SHA, **changes}
    search._documents["ZS_DEMO"][WEB_UUID] = doc
    return doc


def _dry_run(ingest, sha):
    ingest.preview_result = {"status": "dry_run", "files": [
        {"file_uuid": "x", "source_url": URL, "source_type": "web", "page_role": "parent",
         "content_sha256": sha}]}


def _crawl_job(store):
    return q.enqueue_job(store, job_id=q.new_job_id(), school="ZS_DEMO", batch_id="b",
                         source_kind="url", source=URL, user_id="qa_portal__ZS_DEMO",
                         params={**PARAMS, "save_level_1_child": True, "max_child_links": 5},
                         principal_subject="vpn-admin", payload_part=None)


# ── hash at enqueue ─────────────────────────────────────────────────────
def test_file_job_stores_the_sha256_of_the_payload(store):
    job = _file_job(store, CONTENT, "a.pdf")
    assert _job(store, job["job_id"])["_content_sha256"] == SHA
    assert "_content_sha256" not in q.public_job(job)


def test_url_job_has_no_hash_at_enqueue(store):
    assert _job(store, _url_job(store)["job_id"])["_content_sha256"] is None


# ── skipping a file ─────────────────────────────────────────────────────
def test_identical_file_is_skipped(store, ingest, search):
    job = _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    done = _job(store, job["job_id"])
    assert done["status"] == "skipped"
    assert done["result"] == {"reason": "unchanged", "file_uuid": DOC_UUID}
    assert done["error"] is None and done["processing_uuid"] is None
    assert done["finished_at"] and done["stored_chunks"] is None
    assert ingest.document_received == []
    assert not store.job_file_path(job["job_id"]).exists()
    assert search.document_filters[-1]["source"] == "a.pdf"
    assert search.document_calls[-1][2] == "all"
    assert q.public_job(done)["result"] == {"reason": "unchanged", "file_uuid": DOC_UUID}


def test_file_with_another_hash_is_ingested(store, ingest, search):
    job = _file_job(store, b"changed bytes", "a.pdf")
    q.process_next_job(store, ingest, search)
    assert _job(store, job["job_id"])["status"] == "completed"
    assert len(ingest.document_received) == 1


def test_file_of_another_uploader_is_not_the_same_document(store, ingest, search):
    search._documents["ZS_DEMO"][DOC_UUID]["user_name"] = "Some Teacher"
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_null_hash_never_matches(store, ingest, search):
    search._documents["ZS_DEMO"][DOC_UUID]["content_sha256"] = None
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_version_without_chunks_is_not_unchanged(store, ingest, search):
    search._documents["ZS_DEMO"][DOC_UUID]["chunk_count"] = 0
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_inactive_document_is_not_current(store, ingest, search):
    search._documents["ZS_DEMO"][DOC_UUID]["is_active"] = False
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_no_document_with_that_name_is_ingested(store, ingest, search):
    _file_job(store, CONTENT, "other.pdf")
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


@pytest.mark.parametrize("method", ["list_documents", "get_uploader"])
def test_lookup_failure_does_not_skip(store, ingest, search, method, caplog):
    search.raise_unavailable_on = method
    job = _file_job(store, CONTENT, "a.pdf")
    with caplog.at_level(logging.DEBUG):
        q.process_next_job(store, ingest, search)
    assert _job(store, job["job_id"])["status"] == "completed"
    assert len(ingest.document_received) == 1
    assert "a.pdf" not in caplog.text and SHA not in caplog.text
    assert job["job_id"] in caplog.text


def test_lookup_500_does_not_skip(store, ingest, search):
    search.raise_server_error_on = "list_documents"
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_without_a_search_client_nothing_is_skipped(store, ingest):
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest)
    assert len(ingest.document_received) == 1


def test_worker_thread_skips_with_its_search_client(store, ingest, search):
    import time
    worker = q.DocumentQueueWorker(store, ingest, idle_seconds=0.05, search=search)
    worker.start()
    try:
        job = _file_job(store, CONTENT, "a.pdf")
        deadline = time.monotonic() + 5
        while _job(store, job["job_id"])["status"] == "queued":
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        worker.stop()
    assert _job(store, job["job_id"])["status"] == "skipped"
    assert ingest.document_received == []


# ── skipping a web page (gateway dry run first) ─────────────────────────
def test_unchanged_page_is_skipped_via_the_dry_run(store, ingest, search):
    _add_web_doc(search)
    _dry_run(ingest, SHA)
    job = _url_job(store, URL)
    q.process_next_job(store, ingest, search)
    done = _job(store, job["job_id"])
    assert done["status"] == "skipped"
    assert done["result"] == {"reason": "unchanged", "file_uuid": WEB_UUID}
    assert ingest.document_received == []
    (preview,) = ingest.preview_received
    assert preview["url"] == URL and preview["save_level_1_child"] is False
    assert preview["legal_entity"] == "ZS_DEMO" and preview["visibility"] == "entity"
    assert search.document_filters[-1]["source"] == URL


def test_changed_page_is_ingested_after_the_dry_run(store, ingest, search):
    _add_web_doc(search)
    _dry_run(ingest, OTHER_SHA)
    job = _url_job(store, URL)
    q.process_next_job(store, ingest, search)
    assert _job(store, job["job_id"])["status"] == "completed"
    assert len(ingest.preview_received) == 1 and len(ingest.document_received) == 1


@pytest.mark.parametrize("fail", ["rejected", "unavailable"])
def test_dry_run_failure_does_not_skip(store, ingest, search, fail):
    _add_web_doc(search)
    ingest.preview_fail = fail
    job = _url_job(store, URL)
    q.process_next_job(store, ingest, search)
    assert _job(store, job["job_id"])["status"] == "completed"
    assert len(ingest.document_received) == 1


def test_dry_run_without_a_hash_does_not_skip(store, ingest, search):
    _add_web_doc(search)
    ingest.preview_result = {"status": "dry_run", "files": [{"file_uuid": "x"}]}
    _url_job(store, URL)
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_new_url_without_an_existing_document_is_ingested(store, ingest, search):
    _dry_run(ingest, SHA)
    _url_job(store, URL)
    q.process_next_job(store, ingest, search)
    assert len(ingest.document_received) == 1


def test_crawl_with_level_1_links_is_never_skipped(store, ingest, search):
    _add_web_doc(search)
    _dry_run(ingest, SHA)
    job = _crawl_job(store)
    q.process_next_job(store, ingest, search)
    assert _job(store, job["job_id"])["status"] == "completed"
    assert ingest.preview_received == []           # not even a dry run
    (call,) = ingest.document_received
    assert call["save_level_1_child"] is True


# ── recording the hash ──────────────────────────────────────────────────
def _ingested_job(store, ingest, search, files=None):
    ingest.document_result = {**ingest.document_result, "files": files if files is not None else [
        {"file_uuid": DOC_UUID, "file_name": "a.pdf", "status": "ok", "content_sha256": OTHER_SHA}]}
    job = _file_job(store, b"changed bytes", "a.pdf")
    q.process_next_job(store, ingest, search)
    return job["job_id"]


def _list(store, ingest, search):
    return q.list_jobs(store, ingest, "ZS_DEMO", None, search)


def test_hash_is_recorded_once_when_stored_chunks_become_positive(store, ingest, search):
    job_id = _ingested_job(store, ingest, search)
    assert _job(store, job_id)["_file_hashes"][0]["content_sha256"] == OTHER_SHA
    # nothing stored yet: no PATCH
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 0}]}
    _list(store, ingest, search)
    assert search.hash_calls == []
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 4}]}
    jobs = _list(store, ingest, search)
    assert jobs[0]["stored_chunks"] == 4
    assert search.hash_calls == [("ZS_DEMO", DOC_UUID, OTHER_SHA)]
    assert _job(store, job_id)["_hash_recorded"] is True
    _list(store, ingest, search)
    _list(store, ingest, search)
    assert len(search.hash_calls) == 1               # once
    assert "_file_hashes" not in jobs[0] and "_hash_recorded" not in jobs[0]


def test_each_file_of_a_crawl_gets_its_own_hash(store, ingest, search):
    second = "cccccccc-0000-0000-0000-000000000003"
    search._documents["ZS_DEMO"][second] = {**DOCUMENT, "file_uuid": second}
    files = [{"file_uuid": DOC_UUID, "status": "ok", "content_sha256": SHA},
             {"file_uuid": second, "status": "ok", "content_sha256": OTHER_SHA},
             {"file_uuid": "dddddddd-0000-0000-0000-000000000004", "status": "failed"},
             {"file_uuid": "eeeeeeee-0000-0000-0000-000000000005", "content_sha256": "XYZ"}]
    _ingested_job(store, ingest, search, files)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 2}]}
    _list(store, ingest, search)
    assert search.hash_calls == [("ZS_DEMO", DOC_UUID, SHA), ("ZS_DEMO", second, OTHER_SHA)]


def test_a_failed_patch_is_retried_on_a_later_listing(store, ingest, search):
    job_id = _ingested_job(store, ingest, search)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    search.hash_error = SearchUnavailable("down")
    _list(store, ingest, search)
    assert _job(store, job_id)["_hash_recorded"] is False
    assert _job(store, job_id)["stored_chunks"] == 3
    search.hash_error = None
    _list(store, ingest, search)
    assert _job(store, job_id)["_hash_recorded"] is True
    assert len(search.hash_calls) == 2
    _list(store, ingest, search)
    assert len(search.hash_calls) == 2


def test_only_the_failed_file_is_retried(store, ingest, search):
    second = "cccccccc-0000-0000-0000-000000000003"
    search._documents["ZS_DEMO"][second] = {**DOCUMENT, "file_uuid": second}
    files = [{"file_uuid": DOC_UUID, "content_sha256": SHA},
             {"file_uuid": second, "content_sha256": OTHER_SHA}]
    _ingested_job(store, ingest, search, files)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    real = search.update_document_content_hash
    failures = []

    def flaky(entity, file_uuid, sha):
        if file_uuid == second and not failures:
            failures.append(1)
            search.hash_calls.append((entity, file_uuid, sha))
            raise SearchUnavailable("down")
        return real(entity, file_uuid, sha)

    search.update_document_content_hash = flaky
    _list(store, ingest, search)
    _list(store, ingest, search)
    assert [c[1] for c in search.hash_calls] == [DOC_UUID, second, second]


def test_a_404_is_final_and_not_retried(store, ingest, search):
    job_id = _ingested_job(store, ingest, search)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    search.hash_error = SearchNotFound("no row")
    _list(store, ingest, search)
    assert _job(store, job_id)["_hash_recorded"] is True
    _list(store, ingest, search)
    assert len(search.hash_calls) == 1


def test_recording_stops_after_the_window(store, ingest, search):
    job_id = _ingested_job(store, ingest, search)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    search.hash_error = SearchUnavailable("down")
    _list(store, ingest, search)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)
    _set(store, job_id, finished_at=old.isoformat())
    search.hash_error = None
    _list(store, ingest, search)
    assert len(search.hash_calls) == 1 and _job(store, job_id)["_hash_recorded"] is False


def test_nothing_is_recorded_without_hashes_or_without_search(store, ingest, search):
    _ingested_job(store, ingest, search, files=[])
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    _list(store, ingest, search)
    assert search.hash_calls == []
    _ingested_job(store, ingest, search)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    q.list_jobs(store, ingest, "ZS_DEMO", None)       # no search client
    assert search.hash_calls == []


def test_a_job_that_did_not_complete_records_nothing(store, ingest, search):
    ingest.document_fail = "unavailable"
    _file_job(store, b"changed bytes", "a.pdf")
    q.process_next_job(store, ingest, search)
    _list(store, ingest, search)
    assert search.hash_calls == []


def test_the_recorded_hash_makes_the_next_identical_upload_skip(store, ingest, search):
    # end to end: ingest once, record the hash, upload the same file again
    sha = hashlib.sha256(b"changed bytes").hexdigest()
    _ingested_job(store, ingest, search, [{"file_uuid": DOC_UUID, "content_sha256": sha}])
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    _list(store, ingest, search)
    again = _file_job(store, b"changed bytes", "a.pdf")
    q.process_next_job(store, ingest, search)
    assert _job(store, again["job_id"])["status"] == "skipped"
    assert len(ingest.document_received) == 1


def test_the_hash_log_lines_carry_identifiers_only(store, ingest, search, caplog):
    _ingested_job(store, ingest, search)
    ingest.status_by_uuid[PROC] = {"files": [{"chunks": 3}]}
    search.hash_error = SearchUnavailable(f"down {OTHER_SHA}")
    with caplog.at_level(logging.DEBUG):
        _list(store, ingest, search)
    assert OTHER_SHA not in caplog.text and "a.pdf" not in caplog.text


# ── skipped is a finished status ────────────────────────────────────────
def test_skipped_does_not_count_against_the_school_cap(store, ingest, search, monkeypatch):
    monkeypatch.setattr(settings, "document_queue_school_cap", 1)
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    q.check_capacity(store, "ZS_DEMO", "22222222-0000-0000-0000-000000000009")
    _url_job(store)                                    # the only active job
    with pytest.raises(Exception) as exc:
        q.check_capacity(store, "ZS_DEMO", "22222222-0000-0000-0000-000000000009")
    assert exc.value.code == "INGEST_QUEUE_FULL"


def test_skipped_is_pruned_like_other_finished_jobs(store, ingest, search):
    job = _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert q.prune_jobs(store) == 0
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
    assert q.prune_jobs(store, later) == 1
    assert _job(store, job["job_id"]) is None


def test_skipped_job_is_not_looked_up_for_stored_chunks(store, ingest, search):
    _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    jobs = _list(store, ingest, search)
    assert jobs[0]["status"] == "skipped" and jobs[0]["stored_chunks"] is None
    assert ingest.status_calls == [] and search.hash_calls == []
    assert "skipped" in q.FINISHED


def test_restart_leaves_a_skipped_job_alone(store, ingest, search):
    job = _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert q.reconcile_jobs_on_startup(store) == 0
    assert _job(store, job["job_id"])["status"] == "skipped"


# ── through the API ─────────────────────────────────────────────────────
def test_job_listing_route_records_hashes_and_shows_skipped(store, ingest, search):
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    try:
        client = TestClient(app)
        _ingested_job(store, ingest, search)
        _file_job(store, CONTENT, "a.pdf")
        q.process_next_job(store, ingest, search)
        ingest.status_by_uuid[PROC] = {"files": [{"chunks": 5}]}
        jobs = client.get("/qa-api/v1/schools/ZS_DEMO/document-jobs").json()["jobs"]
    finally:
        app.dependency_overrides.clear()
    assert sorted(j["status"] for j in jobs) == ["completed", "skipped"]
    assert search.hash_calls == [("ZS_DEMO", DOC_UUID, OTHER_SHA)]
    skipped = next(j for j in jobs if j["status"] == "skipped")
    assert skipped["result"] == {"reason": "unchanged", "file_uuid": DOC_UUID}


def test_identical_file_with_new_metadata_is_ingested(store, ingest, search):
    # Same bytes, but the stored version has another validity than the upload
    # declares: skipping would silently drop the change.
    search._documents["ZS_DEMO"][DOC_UUID]["valid_to"] = "2030-06-30"
    job = _file_job(store, CONTENT, "a.pdf")
    q.process_next_job(store, ingest, search)
    assert _job(store, job["job_id"])["status"] == "completed"
    assert len(ingest.document_received) == 1
