"""New-pack metadata confirmation (CONTRACT.md §2.4/§2.4a, contract 1.1.0).

FakeSearchClient/FakeIngestClient are in-memory test doubles — no network, no
real ingest or search service is exercised anywhere in this file; every
assertion below is labelled MOCKED evidence.
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


def _new_pack_body(pack_metadata: dict | None) -> dict:
    body = {
        "base_etag": "",
        "entries": [{
            "entry_key": None, "canonical_q": "Kde je sekretariat?", "answer_md": "V prizemi.",
            "variants": [], "sources": [], "domains": ["PROVOZ"], "category_code": None,
            "is_active": True,
        }],
    }
    if pack_metadata is not None:
        body["pack_metadata"] = pack_metadata
    return body


def _existing_pack_body(pack_metadata: dict | None = None) -> dict:
    body = {
        "base_etag": "etag-1",
        "entries": [{
            "entry_key": "k1", "canonical_q": "Kdy zacina rok?", "answer_md": "V zari, uz zase.",
            "variants": [], "sources": [], "domains": ["PROVOZ"], "category_code": "GENERAL",
            "is_active": True,
        }],
    }
    if pack_metadata is not None:
        body["pack_metadata"] = pack_metadata
    return body


VALID_METADATA = {
    "visibility": "entity",
    "audience_roles": ["TEACHER"],
    "school_grade_codes": ["1"],
}


# ── ACCEPTANCE: new pack — preview requires + carries pack_metadata, ───────
# ── publish sends it (unchanged) to ingest ─────────────────────────────────

def test_new_pack_publish_sends_confirmed_metadata_to_ingest(client, ingest):
    prev = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/preview",
                        json=_new_pack_body(VALID_METADATA), headers=HEADERS)
    assert prev.status_code == 200
    preview_id = prev.json()["preview_id"]

    pub = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/publish",
                       json={"preview_id": preview_id, "expected_etag": ""},
                       headers=HEADERS)
    assert pub.status_code == 202

    # MOCKED evidence: assert on what the fake ingest actually received, not
    # just the HTTP status.
    assert len(ingest.received) == 1
    received = ingest.received[0]
    assert received["visibility"] == "entity"
    assert received["audience_roles"] == ["TEACHER"]
    assert received["school_subject_grades"] == ["1"]


# ── new pack WITHOUT pack_metadata -> 422 field "pack_metadata" ────────────

def test_new_pack_preview_without_metadata_is_422(client, ingest):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/preview",
                        json=_new_pack_body(None), headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata" for f in body["error"]["fields"])
    assert not ingest.received


# ── PRESERVATION: existing pack, preview WITHOUT pack_metadata -> publish ──
# ── still sends the pack-state metadata unchanged (guards §2.4) ───────────

def test_existing_pack_publish_keeps_pack_state_metadata(client, ingest, search):
    search._packs["ZS_DEMO"]["faq"]["metadata"] = {
        "visibility": "organization", "audience_roles": ["PARENT"],
        "school_grade_codes": ["3"], "valid_from": None, "valid_to": None,
    }

    prev = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
                        json=_existing_pack_body(None), headers=HEADERS)
    assert prev.status_code == 200
    preview_id = prev.json()["preview_id"]

    pub = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
                       json={"preview_id": preview_id, "expected_etag": "etag-1"},
                       headers=HEADERS)
    assert pub.status_code == 202

    assert len(ingest.received) == 1
    received = ingest.received[0]
    assert received["visibility"] == "organization"
    assert received["audience_roles"] == ["PARENT"]
    assert received["school_subject_grades"] == ["3"]


# ── existing pack WITH pack_metadata -> 422 field "pack_metadata" ──────────

def test_existing_pack_preview_with_metadata_is_422(client, ingest):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
                        json=_existing_pack_body(VALID_METADATA), headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata" for f in body["error"]["fields"])
    assert not ingest.received


# ── FAILURE: unknown role code -> 422 fields[].field pack_metadata.audience_roles

def test_new_pack_preview_unknown_role_code_is_422(client):
    bad = dict(VALID_METADATA, audience_roles=["NOT_A_ROLE"])
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/preview",
                        json=_new_pack_body(bad), headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata.audience_roles[0]" for f in body["error"]["fields"])


def test_new_pack_preview_malformed_valid_from_is_422(client):
    """A date the gateway would reject must fail here, not as UPSTREAM_ERROR."""
    bad = dict(VALID_METADATA, valid_from="2026-13-99")
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/preview",
                        json=_new_pack_body(bad), headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata.valid_from" for f in body["error"]["fields"])


def test_new_pack_preview_reversed_validity_is_422(client):
    bad = dict(VALID_METADATA, valid_from="2026-09-01", valid_to="2026-08-01")
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/preview",
                        json=_new_pack_body(bad), headers=HEADERS)
    assert resp.status_code == 422
    assert any(f["field"] == "pack_metadata.valid_from"
               for f in resp.json()["error"]["fields"])


# ── code lists unreachable -> 503 UNAVAILABLE, retryable true ──────────────

def test_new_pack_preview_code_lists_unavailable_is_503(client, search):
    search.raise_unavailable_on = "get_code_lists"
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs/newpack/preview",
                        json=_new_pack_body(VALID_METADATA), headers=HEADERS)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "UNAVAILABLE"
    assert body["error"]["retryable"] is True


# ── GET /schools/{school}/metadata-options -> 200 with the three keys ──────

def test_get_metadata_options(client):
    resp = client.get("/qa-api/v1/schools/ZS_DEMO/metadata-options", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["visibility"] == ["entity", "organization"]
    codes = {o["code"] for o in body["audience_roles"]}
    assert codes == {"TEACHER", "PARENT", "STUDENT"}
    grade_codes = {o["code"] for o in body["school_grades"]}
    assert grade_codes == {str(n) for n in range(1, 10)}


# ── createPack: optional metadata is checked, never stored ─────────────────

def test_create_pack_with_valid_metadata_is_not_persisted(client, search):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs",
                        json={"pack_id": "brand-new", "lang": "cs", "metadata": VALID_METADATA},
                        headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"pack_id": "brand-new", "available": True}
    # NOTHING materialises before the first publish (openapi.yaml description).
    assert "brand-new" not in search._packs.get("ZS_DEMO", {})


def test_create_pack_with_invalid_metadata_is_422(client):
    bad = dict(VALID_METADATA, audience_roles=[])
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/packs",
                        json={"pack_id": "brand-new-2", "lang": "cs", "metadata": bad},
                        headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
