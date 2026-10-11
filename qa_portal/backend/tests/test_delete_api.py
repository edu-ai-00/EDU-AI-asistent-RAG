"""Permanent delete API (CONTRACT.md sec.13). DeleteFakeSearchClient extends
the shared FakeSearchClient (tests/fake_search.py, not edited here) with the
two internal/qa-admin DELETE calls, mocked in-memory only — no network, no
database, matching the rest of this suite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.adapters.search import SearchNotFound
from app.api.deps import get_ingest_client, get_search_client, get_store
from app.main import app
from app.state.lock import PackLock
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import FakeSearchClient

HEADERS = {"X-QA-Portal": "1"}


class DeleteFakeSearchClient(FakeSearchClient):
    """Adds the two internal/qa-admin delete endpoints this task introduces.
    Records calls so tests can assert the upstream was (or was not) reached.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.delete_entry_calls: list[tuple[str, str, str]] = []
        self.delete_pack_calls: list[tuple[str, str]] = []

    def delete_entry(self, legal_entity: str, pack_id: str, entry_key: str) -> dict:
        self.delete_entry_calls.append((legal_entity, pack_id, entry_key))
        school_packs = self._entries.get(legal_entity, {})
        entries = school_packs.get(pack_id)
        if entries is None:
            raise SearchNotFound(f"no such pack {pack_id!r} for {legal_entity!r}")
        remaining = [e for e in entries if e["entry_key"] != entry_key]
        if len(remaining) == len(entries):
            raise SearchNotFound(f"no such entry {entry_key!r} in pack {pack_id!r}")
        school_packs[pack_id] = remaining
        return {"deleted": {"entries": 1, "variants": 1, "embeddings": 1, "manifests": 0}}

    def delete_pack(self, legal_entity: str, pack_id: str) -> dict:
        self.delete_pack_calls.append((legal_entity, pack_id))
        packs = self._packs.get(legal_entity, {})
        if pack_id not in packs:
            raise SearchNotFound(f"no such pack {pack_id!r} for {legal_entity!r}")
        entry_count = len(self._entries.get(legal_entity, {}).get(pack_id, []))
        del packs[pack_id]
        self._entries.get(legal_entity, {}).pop(pack_id, None)
        return {"deleted": {"entries": entry_count, "variants": entry_count,
                             "embeddings": entry_count, "manifests": 1}}


@pytest.fixture
def search():
    return DeleteFakeSearchClient()


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


def _take_preview(client, school="ZS_DEMO", pack="faq", base_etag="etag-1") -> str:
    resp = client.post(f"/qa-api/v1/schools/{school}/packs/{pack}/preview",
                        json=_preview_body(base_etag), headers=HEADERS)
    assert resp.status_code == 200
    return resp.json()["preview_id"]


# ── acceptance: one entry deleted, counts returned, its preview dropped ────

def test_delete_entry_success_drops_pack_preview(client, search, store):
    preview_id = _take_preview(client)
    assert store.get_preview(preview_id) is not None

    resp = client.delete("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/k1", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": {"entries": 1, "variants": 1, "embeddings": 1, "manifests": 0}}
    assert search.delete_entry_calls == [("ZS_DEMO", "faq", "k1")]
    assert store.get_preview(preview_id) is None


# ── whole-pack delete: correct confirmation works, wrong one is 409 ────────

def test_delete_pack_correct_confirmation_succeeds(client, search, store):
    preview_id = _take_preview(client)

    resp = client.request(
        "DELETE", "/qa-api/v1/schools/ZS_DEMO/packs/faq",
        json={"confirm_pack_id": "faq"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"]["manifests"] == 1
    assert search.delete_pack_calls == [("ZS_DEMO", "faq")]
    assert store.get_preview(preview_id) is None


def test_delete_pack_wrong_confirmation_is_conflict_and_never_calls_search(client, search, store):
    preview_id = _take_preview(client)

    resp = client.request(
        "DELETE", "/qa-api/v1/schools/ZS_DEMO/packs/faq",
        json={"confirm_pack_id": "not-faq"}, headers=HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DELETE_CONFIRMATION_MISMATCH"
    assert search.delete_pack_calls == []
    # a rejected confirmation must not touch stored previews either
    assert store.get_preview(preview_id) is not None


# ── lock contention with publish: 409 PUBLISH_IN_PROGRESS both directions ──

def test_delete_entry_while_pack_locked_is_publish_in_progress(client, store):
    lock = PackLock(store.base_dir, "ZS_DEMO", "faq", 120)
    lock.acquire()
    try:
        resp = client.delete("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/k1", headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PUBLISH_IN_PROGRESS"
    finally:
        lock.release()


def test_delete_pack_while_pack_locked_is_publish_in_progress(client, store):
    lock = PackLock(store.base_dir, "ZS_DEMO", "faq", 120)
    lock.acquire()
    try:
        resp = client.request(
            "DELETE", "/qa-api/v1/schools/ZS_DEMO/packs/faq",
            json={"confirm_pack_id": "faq"}, headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PUBLISH_IN_PROGRESS"
    finally:
        lock.release()


def test_publish_while_delete_holds_lock_is_publish_in_progress(client, ingest, store):
    preview_id = _take_preview(client)
    lock = PackLock(store.base_dir, "ZS_DEMO", "faq", 120)
    lock.acquire()
    try:
        resp = client.post(
            "/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
            json={"preview_id": preview_id, "expected_etag": "etag-1"}, headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PUBLISH_IN_PROGRESS"
        assert not ingest.received
    finally:
        lock.release()


# ── foreign/unknown school or pack -> 404, no preview dropped ──────────────

def test_delete_entry_unknown_pack_is_404_and_drops_no_preview(client, search, store):
    other_preview = _take_preview(client, pack="faq2", base_etag="etag2-1")

    resp = client.delete("/qa-api/v1/schools/ZS_DEMO/packs/does-not-exist/entries/k1",
                          headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert store.get_preview(other_preview) is not None


def test_delete_entry_unknown_entry_key_is_404(client, search, store):
    resp = client.delete("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/no-such-key",
                          headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_delete_pack_unknown_pack_is_404(client, search, store):
    resp = client.request(
        "DELETE", "/qa-api/v1/schools/ZS_DEMO/packs/does-not-exist",
        json={"confirm_pack_id": "does-not-exist"}, headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_delete_entry_unknown_school_is_404(client):
    resp = client.delete("/qa-api/v1/schools/NOT_A_SCHOOL/packs/faq/entries/k1", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── missing X-QA-Portal header -> 403 before anything else ─────────────────

def test_delete_entry_without_csrf_header_is_forbidden(client, search):
    resp = client.delete("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/k1")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert search.delete_entry_calls == []


def test_delete_pack_without_csrf_header_is_forbidden(client, search):
    resp = client.request(
        "DELETE", "/qa-api/v1/schools/ZS_DEMO/packs/faq", json={"confirm_pack_id": "faq"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert search.delete_pack_calls == []
