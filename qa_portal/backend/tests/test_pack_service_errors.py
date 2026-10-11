"""Upstream entry-read failures must surface as 404/503, not a bare 500
(CONTRACT.md, openapi.yaml /schools/{school}/packs/{pack}/entries)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.adapters.search import SearchNotFound, SearchUnavailable
from app.api.deps import get_search_client
from app.main import app
from tests.fake_search import FakeSearchClient


class RaisingSearchClient(FakeSearchClient):
    """FakeSearchClient with list_entries forced to raise a given exception."""

    def __init__(self, exc: Exception):
        super().__init__()
        self._exc = exc

    def list_entries(self, legal_entity, pack_id, limit, cursor, lang):
        raise self._exc


def _client(exc: Exception):
    app.dependency_overrides[get_search_client] = lambda: RaisingSearchClient(exc)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_list_entries_not_found_is_404():
    resp = _client(SearchNotFound("no such pack")).get(
        "/qa-api/v1/schools/ZS_DEMO/packs/faq/entries")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert "application/json" in resp.headers["content-type"]


def test_get_entry_not_found_is_404():
    resp = _client(SearchNotFound("no such pack")).get(
        "/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/k1")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert "application/json" in resp.headers["content-type"]


def test_list_entries_unavailable_is_503():
    resp = _client(SearchUnavailable("upstream down")).get(
        "/qa-api/v1/schools/ZS_DEMO/packs/faq/entries")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE"
    assert "application/json" in resp.headers["content-type"]


def test_get_entry_unavailable_is_503():
    resp = _client(SearchUnavailable("upstream down")).get(
        "/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/k1")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE"
    assert "application/json" in resp.headers["content-type"]
