from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_search_client
from app.main import app
from tests.fake_search import FakeSearchClient


@pytest.fixture
def client():
    app.dependency_overrides[get_search_client] = lambda: FakeSearchClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    resp = client.get("/qa-api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_context(client):
    resp = client.get("/qa-api/v1/context")
    assert resp.status_code == 200
    assert resp.json()["contract_version"]


def test_list_schools(client):
    resp = client.get("/qa-api/v1/schools")
    assert resp.status_code == 200
    schools = resp.json()["schools"]
    assert schools == [{"code": "ZS_DEMO", "name": "ZS Demo", "kind": "school",
                         "level": "zs", "parent_code": None}]


def test_school_domains(client):
    resp = client.get("/qa-api/v1/schools/ZS_DEMO/domains")
    assert resp.status_code == 200
    assert resp.json()["domains"][0]["code"] == "PROVOZ"


def test_categories(client):
    resp = client.get("/qa-api/v1/categories")
    assert resp.status_code == 200
    assert resp.json()["categories"][0]["code"] == "GENERAL"


def test_list_packs(client):
    resp = client.get("/qa-api/v1/schools/ZS_DEMO/packs")
    assert resp.status_code == 200
    pack = resp.json()["packs"][0]
    assert pack["pack_id"] == "faq"
    assert pack["pack_etag"] == "etag-1"


def test_list_entries(client):
    resp = client.get("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pack_etag"] == "etag-1"
    assert body["entries"][0]["entry_key"] == "k1"


def test_get_entry(client):
    resp = client.get("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/k1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry"]["entry_key"] == "k1"
    assert body["pack_etag"] == "etag-1"
    assert body["pack_version"] == 3


def test_get_entry_missing_key_is_json_404(client):
    resp = client.get("/qa-api/v1/schools/ZS_DEMO/packs/faq/entries/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"


def test_unknown_school_is_404(client):
    resp = client.get("/qa-api/v1/schools/NOPE/packs")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_api_path_is_json_not_html(client):
    resp = client.get("/qa-api/v1/does-not-exist-at-all")
    assert resp.status_code == 404
    assert "application/json" in resp.headers["content-type"]
    body_text = resp.text.lower()
    assert "<!doctype" not in body_text
    assert "<html" not in body_text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_upstream_unavailable_returns_error_envelope(client):
    app.dependency_overrides[get_search_client] = lambda: FakeSearchClient(
        raise_unavailable_on="list_active_entities")
    resp = client.get("/qa-api/v1/schools")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "UNAVAILABLE"
    assert "<html" not in resp.text.lower()


def test_static_dir_absent_does_not_break_app(client):
    # STATIC_DIR does not exist in this dev checkout (Docker build creates it);
    # the app must still start and answer API requests, and /qa/ must not
    # 500 or leak a traceback.
    from app.main import STATIC_DIR
    assert not STATIC_DIR.is_dir()
    resp = client.get("/qa-api/v1/health")
    assert resp.status_code == 200
