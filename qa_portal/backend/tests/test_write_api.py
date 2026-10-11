"""Write-side API: categories, pack availability, preview, publish and
operation lookup (CONTRACT.md §3-§6). FakeSearchClient/FakeIngestClient are
in-memory test doubles — no network, no real ingest — labelled as such in
every assertion below; nothing here exercises the real search or ingest
service.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_ingest_client, get_search_client, get_store
from app.main import app
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import FakeSearchClient

HEADERS = {"X-QA-Portal": "1"}


@pytest.fixture
def search():
    return FakeSearchClient()


@pytest.fixture
def ingest(search):
    return FakeIngestClient(search)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "qa_admin_state")


@pytest.fixture
def client(search, ingest, store):
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _preview_body(base_etag: str = "etag-1") -> dict:
    return {
        "base_etag": base_etag,
        "entries": [{
            "entry_key": "k1", "canonical_q": "Kdy zacina rok?", "answer_md": "V zari, uz zase.",
            "variants": [], "sources": [], "domains": ["PROVOZ"], "category_code": "GENERAL",
            "is_active": True,
        }],
    }


# ── A: preview -> publish -> operation succeeds (mocked ingest + search) ───

def test_preview_then_publish_succeeds(client, ingest, store):
    prev = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
                        json=_preview_body(), headers=HEADERS)
    assert prev.status_code == 200
    preview_id = prev.json()["preview_id"]
    assert preview_id

    pub = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
                       json={"preview_id": preview_id, "expected_etag": "etag-1"},
                       headers=HEADERS)
    assert pub.status_code == 202
    op = pub.json()
    assert op["status"] == "accepted"
    operation_id = op["operation_id"]

    # TestClient waits for the BackgroundTask before returning the response,
    # so the fake ingest has already been called by the time we get here.
    assert len(ingest.received) == 1
    received = ingest.received[0]
    assert received["legal_entity"] == "ZS_DEMO"
    assert received["pack_id"] == "faq"
    # original entry_key preserved (never regenerated for an existing entry)
    csv_text = received["csv_bytes"].decode("utf-8-sig")
    assert "k1" in csv_text
    # original owner/uploader used for the ingest call, never client-supplied
    assert received["user_id"] == "qa_portal__ZS_DEMO"

    final = client.get(f"/qa-api/v1/operations/{operation_id}")
    assert final.status_code == 200
    body = final.json()
    assert body["status"] == "completed"
    assert body["result_etag"] and body["result_etag"] != "etag-1"


# ── B: stale expected_etag -> 409, no operation, lock not left held ────────

def test_publish_stale_etag_is_conflict_and_leaves_no_lock(client, ingest, store):
    prev = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
                        json=_preview_body(), headers=HEADERS)
    preview_id = prev.json()["preview_id"]

    pub = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
                       json={"preview_id": preview_id, "expected_etag": "stale-etag"},
                       headers=HEADERS)
    assert pub.status_code == 409
    assert pub.json()["error"]["code"] == "PACK_VERSION_CONFLICT"
    assert not ingest.received  # never reached the ingest call
    assert not store.list_operation_ids()  # no operation was minted

    # a second, correctly-addressed publish attempt must still be possible —
    # i.e. the rejected attempt above left no lock file held.
    pub2 = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
                        json={"preview_id": preview_id, "expected_etag": "etag-1"},
                        headers=HEADERS)
    assert pub2.status_code == 202
    assert len(ingest.received) == 1


# ── C: missing X-QA-Portal header -> 403, service never called ────────────

def test_write_without_csrf_header_is_forbidden(client, ingest, store):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview", json=_preview_body())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert not ingest.received
    assert not store.list_ids("previews")


def test_publish_without_csrf_header_is_forbidden(client, ingest, store):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
                        json={"preview_id": "11111111-1111-1111-1111-111111111111",
                               "expected_etag": "etag-1"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert not ingest.received
    assert not store.list_operation_ids()


def test_create_category_without_csrf_header_is_forbidden(client, search):
    resp = client.post("/qa-api/v1/categories",
                        json={"code": "AFTER_SCHOOL_CARE", "labels": {"cs": "Druzina"}})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert "AFTER_SCHOOL_CARE" not in search._categories


# ── D: categories — duplicate code 409, invalid code 422 (Error envelope) ──

def test_create_category_success(client):
    resp = client.post("/qa-api/v1/categories",
                        json={"code": "AFTER_SCHOOL_CARE", "labels": {"cs": "Druzina"}},
                        headers=HEADERS)
    assert resp.status_code == 201
    assert resp.json()["code"] == "AFTER_SCHOOL_CARE"


def test_create_category_duplicate_code_is_conflict(client):
    body = {"code": "GENERAL", "labels": {"cs": "Obecne"}}
    resp = client.post("/qa-api/v1/categories", json=body, headers=HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"]


def test_create_category_invalid_code_is_validation_error_envelope(client):
    resp = client.post("/qa-api/v1/categories",
                        json={"code": "not-a-valid-code", "labels": {"cs": "x"}},
                        headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"]


# ── E: unknown operation id -> 404 in the Error envelope ───────────────────

def test_get_unknown_operation_is_404(client):
    resp = client.get("/qa-api/v1/operations/99999999-9999-9999-9999-999999999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── pack availability check (POST /schools/{school}/packs) ────────────────

def test_create_pack_available_for_new_id(client):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs",
                        json={"pack_id": "brand-new-pack", "lang": "cs"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"pack_id": "brand-new-pack", "available": True}


def test_create_pack_taken_id_is_conflict(client):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs",
                        json={"pack_id": "faq", "lang": "cs"}, headers=HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"]
