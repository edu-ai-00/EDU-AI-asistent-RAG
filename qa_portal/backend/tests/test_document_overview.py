"""CONTRACT.md §14.12-§14.14 (v1.8.0) portal routes: stats, `expiring` validity,
chunks, refresh from the web, and the content-hash adapter."""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.adapters.search import (HttpSearchClient, SearchBadRequest, SearchNotFound,
                                 SearchServerError, SearchUnavailable,
                                 SearchValidationError)
from app.api.deps import (get_ingest_client, get_policy, get_principal, get_search_client,
                          get_store)
from app.config import settings
from app.main import app
from app.services import document_queue as q
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import CHUNKS, DOC_UUID, DOCUMENT, STATS, FakeSearchClient
from tests.test_document_adapters import FakeResponse, Recorder
from tests.test_document_policy import OTHER, STAFF, RestrictedPolicy, TwoSchoolSearch

HEADERS = {"X-QA-Portal": "1"}
BASE = "/qa-api/v1/schools/ZS_DEMO"
WEB_UUID = "bbbbbbbb-0000-0000-0000-000000000002"
URL_ = "https://skola.example/rad"
HASH = "a" * 64


def _web_doc(**changes) -> dict:
    doc = copy.deepcopy(DOCUMENT)
    doc.update(file_uuid=WEB_UUID, title=URL_, source_kind="web", source_url=URL_,
               original_file_name=None, file_format="web", document_type="OTHER",
               document_language="cs", school_subject="MAT", school_grade_codes=["3", "4"],
               domain_codes=["PROVOZ"], audience_roles=["PARENT", "TEACHER"],
               visibility="organization", valid_from="2026-09-01", valid_to="2027-08-31")
    doc.update(changes)
    return doc


@pytest.fixture
def search():
    s = FakeSearchClient()
    s._documents["ZS_DEMO"][WEB_UUID] = _web_doc()
    return s


@pytest.fixture
def ingest(search):
    return FakeIngestClient(search)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state")


@pytest.fixture
def client(search, ingest, store):
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── §14.12 stats and expiring ───────────────────────────────────────────
def test_stats_success_and_route_is_not_a_file_uuid(client, search):
    resp = client.get(f"{BASE}/documents/stats")
    assert resp.status_code == 200
    assert resp.json() == STATS
    assert search.stats_calls == ["ZS_DEMO"]


def test_stats_search_failures(client, search):
    search.raise_server_error_on = "get_document_stats"
    resp = client.get(f"{BASE}/documents/stats")
    assert resp.status_code == 502 and resp.json()["error"]["retryable"] is False
    search.raise_server_error_on = None
    search.raise_unavailable_on = "get_document_stats"
    assert client.get(f"{BASE}/documents/stats").status_code == 503


def test_stats_unknown_school_is_404(client):
    assert client.get("/qa-api/v1/schools/NOPE/documents/stats").status_code == 404


def test_validity_expiring_is_accepted_and_passed_to_search(client, search):
    resp = client.get(f"{BASE}/documents", params={"validity": "expiring"})
    assert resp.status_code == 200
    assert search.document_filters[-1]["validity"] == "expiring"


def test_validity_unknown_is_still_422(client, search):
    resp = client.get(f"{BASE}/documents", params={"validity": "soon"})
    assert resp.status_code == 422 and search.document_calls == []


# ── §14.14 content_sha256 passes through ────────────────────────────────
def test_content_sha256_passes_through_summaries_and_versions(client, search):
    search._documents["ZS_DEMO"][DOC_UUID]["content_sha256"] = HASH
    listed = client.get(f"{BASE}/documents").json()["documents"]
    assert next(d for d in listed if d["file_uuid"] == DOC_UUID)["content_sha256"] == HASH
    detail = client.get(f"{BASE}/documents/{DOC_UUID}").json()
    assert detail["document"]["content_sha256"] == HASH
    assert detail["versions"][0]["content_sha256"] == HASH
    assert "file_name" not in detail["document"]
    assert "file_name" not in detail["versions"][0]


# ── §14.13 chunks ───────────────────────────────────────────────────────
def test_chunks_success_defaults(client, search):
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/chunks")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"chunks": CHUNKS, "total": 5, "next_cursor": None}
    assert search.chunk_calls == [("ZS_DEMO", DOC_UUID, 20, None)]


def test_chunks_paging_passes_limit_and_cursor(client, search):
    first = client.get(f"{BASE}/documents/{DOC_UUID}/chunks", params={"limit": 2}).json()
    assert [c["chunk_index"] for c in first["chunks"]] == [0, 1] and first["next_cursor"] == "2"
    second = client.get(f"{BASE}/documents/{DOC_UUID}/chunks",
                        params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [c["chunk_index"] for c in second["chunks"]] == [2, 3]
    assert search.chunk_calls[-1] == ("ZS_DEMO", DOC_UUID, 2, "2")


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": "x"},
                                    {"cursor": "c" * 201}])
def test_chunks_validation_is_422_before_search(client, search, params):
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/chunks", params=params)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert search.chunk_calls == []


def test_chunks_limit_bounds_are_accepted(client):
    for limit in (1, 100):
        assert client.get(f"{BASE}/documents/{DOC_UUID}/chunks",
                          params={"limit": limit}).status_code == 200


def test_chunks_bad_uuid_is_400_unknown_is_404(client, search):
    assert client.get(f"{BASE}/documents/not-a-uuid/chunks").status_code == 400
    assert search.chunk_calls == []
    resp = client.get(f"{BASE}/documents/cccccccc-0000-0000-0000-000000000009/chunks")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"


def test_chunks_search_400_and_422_mapping(client, search, monkeypatch):
    def bad(*_a):
        raise SearchBadRequest({"error": {"code": "BAD_REQUEST", "message": "bad cursor"}})
    monkeypatch.setattr(search, "list_document_chunks", bad)
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/chunks", params={"cursor": "zz"})
    assert resp.status_code == 400 and resp.json()["error"]["message"] == "bad cursor"

    def invalid(*_a):
        raise SearchValidationError({"error": {"fields": [{"field": "limit", "message": "x"}]}})
    monkeypatch.setattr(search, "list_document_chunks", invalid)
    assert client.get(f"{BASE}/documents/{DOC_UUID}/chunks").status_code == 422


def test_chunks_search_500_is_502_and_outage_503(client, search):
    search.raise_server_error_on = "list_document_chunks"
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/chunks")
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "UPSTREAM_ERROR"
    search.raise_server_error_on = None
    search.raise_unavailable_on = "list_document_chunks"
    assert client.get(f"{BASE}/documents/{DOC_UUID}/chunks").status_code == 503


# ── §14.14 refresh ──────────────────────────────────────────────────────
def _refresh(client, file_uuid=WEB_UUID, school="ZS_DEMO", headers=HEADERS):
    return client.post(f"/qa-api/v1/schools/{school}/documents/{file_uuid}/refresh",
                       headers=headers)


def test_refresh_enqueues_a_url_job_with_the_shown_versions_metadata(client, store):
    resp = _refresh(client)
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["source_kind"] == "url" and job["source"] == URL_
    assert job["status"] == "queued" and job["school"] == "ZS_DEMO"
    stored = store.get("jobs", job["job_id"])
    assert stored["_params"] == {
        "visibility": "organization", "audience_roles": ["PARENT", "TEACHER"],
        "document_type": "OTHER", "document_language": "cs", "school_subject": "MAT",
        "school_grade_codes": ["3", "4"], "domain_codes": ["PROVOZ"],
        "valid_from": "2026-09-01", "valid_to": "2027-08-31", "file_name": None,
        "save_level_1_child": False, "max_child_links": None}
    assert stored["_user_id"] == "qa_portal__ZS_DEMO"
    assert set(job) == set(q.PUBLIC_KEYS)


def test_refresh_uses_a_fresh_batch_id_each_time(client):
    first = _refresh(client).json()["job"]["batch_id"]
    second = _refresh(client).json()["job"]["batch_id"]
    assert first != second


def test_refresh_job_is_ingested_without_level_1_links(client, store, ingest):
    _refresh(client)
    q.process_next_job(store, ingest)
    (call,) = ingest.document_received
    assert call["url"] == URL_ and call["save_level_1_child"] is False
    assert call["visibility"] == "organization" and call["audience_roles"] == ["PARENT", "TEACHER"]
    assert call["school_grade_codes"] == ["3", "4"] and call["valid_to"] == "2027-08-31"


def test_refresh_of_a_file_document_is_422(client, store):
    resp = _refresh(client, DOC_UUID)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR" and err["fields"][0]["field"] == "file_uuid"
    assert store.list_ids("jobs") == []


def test_refresh_of_a_private_document_is_422(client, search, store):
    search._documents["ZS_DEMO"][WEB_UUID]["visibility"] = "private"
    resp = _refresh(client)
    assert resp.status_code == 422 and store.list_ids("jobs") == []


def test_refresh_bad_uuid_400_unknown_404(client, store):
    assert _refresh(client, "nope").status_code == 400
    assert _refresh(client, "dddddddd-0000-0000-0000-000000000001").status_code == 404
    assert store.list_ids("jobs") == []


def test_refresh_needs_csrf(client, store):
    resp = _refresh(client, headers={})
    assert resp.status_code == 403 and store.list_ids("jobs") == []


def test_refresh_without_uploader_is_503(client, search, store):
    search.raise_unavailable_on = "get_uploader"
    resp = _refresh(client)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "PUBLISH_UPLOADER_UNAVAILABLE"
    assert store.list_ids("jobs") == []


def test_refresh_search_500_is_502(client, search):
    search.raise_server_error_on = "get_document"
    assert _refresh(client).status_code == 502


def test_refresh_respects_the_school_queue_cap(client, store, monkeypatch):
    monkeypatch.setattr(settings, "document_queue_school_cap", 1)
    assert _refresh(client).status_code == 202
    resp = _refresh(client)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "INGEST_QUEUE_FULL"


def test_refresh_then_unchanged_page_is_skipped(client, search, store, ingest):
    search._documents["ZS_DEMO"][WEB_UUID]["content_sha256"] = HASH
    ingest.preview_result = {"status": "dry_run", "files": [
        {"file_uuid": "x", "source_url": URL_, "content_sha256": HASH}]}
    job_id = _refresh(client).json()["job"]["job_id"]
    q.process_next_job(store, ingest, search)
    done = store.get("jobs", job_id)
    assert done["status"] == "skipped"
    assert done["result"] == {"reason": "unchanged", "file_uuid": WEB_UUID}
    assert ingest.document_received == []


# ── policy ──────────────────────────────────────────────────────────────
def _routes(school: str):
    base = f"/qa-api/v1/schools/{school}"
    return [("GET", f"{base}/documents/stats"),
            ("GET", f"{base}/documents/{DOC_UUID}/chunks"),
            ("POST", f"{base}/documents/{DOC_UUID}/refresh")]


@pytest.fixture
def restricted(tmp_path):
    two = TwoSchoolSearch()
    two._documents["ZS_DEMO"][WEB_UUID] = _web_doc()
    ing = FakeIngestClient()
    store = StateStore(tmp_path / "state")

    def make(policy):
        app.dependency_overrides[get_search_client] = lambda: two
        app.dependency_overrides[get_ingest_client] = lambda: ing
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_policy] = lambda: policy
        app.dependency_overrides[get_principal] = lambda: STAFF
        return TestClient(app)

    yield two, store, make
    app.dependency_overrides.clear()


@pytest.mark.parametrize("method,path", _routes(OTHER))
def test_foreign_school_is_403_on_every_new_route(restricted, method, path):
    two, store, make = restricted
    resp = make(RestrictedPolicy()).request(method, path, headers=HEADERS)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"
    assert two.stats_calls == [] and two.chunk_calls == [] and store.list_ids("jobs") == []


@pytest.mark.parametrize("deny,method,path", [
    ("read", "GET", f"{BASE}/documents/stats"),
    ("read", "GET", f"{BASE}/documents/{DOC_UUID}/chunks"),
    ("ingest", "POST", f"{BASE}/documents/{WEB_UUID}/refresh"),
])
def test_each_new_route_asks_its_own_permission(restricted, deny, method, path):
    two, store, make = restricted
    resp = make(RestrictedPolicy(frozenset({deny}))).request(method, path, headers=HEADERS)
    assert resp.status_code == 403
    assert two.stats_calls == [] and two.chunk_calls == [] and store.list_ids("jobs") == []


def test_own_school_passes_the_policy_on_new_routes(restricted):
    two, store, make = restricted
    client = make(RestrictedPolicy())
    assert client.get(f"{BASE}/documents/stats").status_code == 200
    assert client.get(f"{BASE}/documents/{DOC_UUID}/chunks").status_code == 200
    assert client.post(f"{BASE}/documents/{WEB_UUID}/refresh", headers=HEADERS).status_code == 202


def test_reading_does_not_need_ingest(restricted):
    two, store, make = restricted
    client = make(RestrictedPolicy(frozenset({"ingest", "deactivate"})))
    assert client.get(f"{BASE}/documents/stats").status_code == 200


# ── the HTTP adapter ────────────────────────────────────────────────────
def _http(monkeypatch, response):
    rec = Recorder(response)
    monkeypatch.setattr("app.adapters.search.requests.request", rec)
    return rec, HttpSearchClient("http://s/v1", "http://s")


def test_adapter_content_hash_patch(monkeypatch):
    rec, client = _http(monkeypatch, FakeResponse(200, {"document": {}}))
    client.update_document_content_hash("ZS_DEMO", DOC_UUID.upper(), HASH)
    call = rec.calls[0]
    assert call["args"] == ("PATCH",
                            f"http://s/internal/qa-admin/documents/{DOC_UUID}/content-hash")
    assert call["params"] == {"legal_entity": "ZS_DEMO"}
    assert call["json"] == {"content_sha256": HASH}


def test_adapter_content_hash_rejects_a_bad_uuid_without_a_request(monkeypatch):
    rec, client = _http(monkeypatch, FakeResponse(200, {}))
    with pytest.raises(SearchBadRequest):
        client.update_document_content_hash("ZS_DEMO", "../x", HASH)
    assert rec.calls == []


@pytest.mark.parametrize("status,exc", [(404, SearchNotFound), (400, SearchBadRequest),
                                        (422, SearchValidationError),
                                        (500, SearchServerError), (502, SearchUnavailable)])
def test_adapter_new_routes_error_mapping(monkeypatch, status, exc):
    _rec, client = _http(monkeypatch, FakeResponse(status, {"error": {"message": "x"}}))
    for call in (lambda: client.update_document_content_hash("ZS_DEMO", DOC_UUID, HASH),
                 lambda: client.get_document_stats("ZS_DEMO"),
                 lambda: client.list_document_chunks("ZS_DEMO", DOC_UUID, 20, None)):
        with pytest.raises(exc):
            call()


def test_adapter_stats_and_chunks_params(monkeypatch):
    rec, client = _http(monkeypatch, FakeResponse(200, {}))
    client.get_document_stats("ZS_DEMO")
    client.list_document_chunks("ZS_DEMO", DOC_UUID, 50, "40")
    client.list_document_chunks("ZS_DEMO", DOC_UUID, 20, None)
    assert rec.calls[0]["args"] == ("GET", "http://s/internal/qa-admin/documents/stats")
    assert rec.calls[0]["params"] == {"legal_entity": "ZS_DEMO"}
    assert rec.calls[1]["args"] == ("GET",
                                    f"http://s/internal/qa-admin/documents/{DOC_UUID}/chunks")
    assert rec.calls[1]["params"] == {"legal_entity": "ZS_DEMO", "limit": 50, "cursor": "40"}
    assert rec.calls[2]["params"] == {"legal_entity": "ZS_DEMO", "limit": 20}
