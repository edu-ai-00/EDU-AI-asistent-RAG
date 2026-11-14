"""CONTRACT.md §14.8 (v1.6.0): list filters, PATCH validity, check-sources,
preview-url, search 500 -> 502 mapping and the /knowledge SPA address."""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters import ingest as ingest_mod
from app.adapters import search as search_mod
from app.adapters.ingest import HttpIngestClient, IngestRejected
from app.adapters.search import HttpSearchClient, SearchServerError, SearchUnavailable
from app.api.deps import (get_ingest_client, get_policy, get_principal, get_search_client,
                          get_store)
from app.config import settings
from app.main import app, mount_spa
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import DOC_UUID, DOCUMENT, FakeSearchClient
from tests.test_document_adapters import FakeResponse, Recorder
from tests.test_document_policy import (OTHER, STAFF, RestrictedPolicy, TwoSchoolSearch)

HEADERS = {"X-QA-Portal": "1"}
BASE = "/qa-api/v1/schools/ZS_DEMO"
META = {"visibility": "entity", "audience_roles": ["PARENT"]}
UPLOADER = "QA portal (ZS_DEMO)"  # FakeSearchClient.get_uploader display_name


@pytest.fixture
def search():
    return FakeSearchClient()


@pytest.fixture
def ingest(search):
    return FakeIngestClient(search)


@pytest.fixture
def client(search, ingest, tmp_path):
    store = StateStore(tmp_path / "state")
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _add_doc(search, uid, **kw):
    doc = {**copy.deepcopy(DOCUMENT), "file_uuid": uid, **kw}
    search._documents["ZS_DEMO"][uid] = doc
    return doc


def _fields(resp) -> set[str]:
    return {f["field"] for f in resp.json()["error"].get("fields") or []}


# ── list filters ────────────────────────────────────────────────────────
def test_list_passes_new_filters_to_search(client, search):
    resp = client.get(f"{BASE}/documents", params={
        "type": "SCHOOL_RULES", "role": "PARENT", "validity": "current",
        "source": "skolni_rad.pdf", "status": "all"})
    assert resp.status_code == 200
    assert resp.json()["documents"][0]["file_uuid"] == DOC_UUID
    assert search.document_filters[-1] == {"type_code": "SCHOOL_RULES", "role": "PARENT",
                                           "validity": "current", "source": "skolni_rad.pdf"}


def test_list_without_filters_sends_none(client, search):
    client.get(f"{BASE}/documents")
    assert search.document_filters[-1] == {"type_code": None, "role": None,
                                           "validity": None, "source": None}


def test_list_filter_narrows_result(client):
    resp = client.get(f"{BASE}/documents", params={"type": "OTHER"})
    assert resp.json()["documents"] == []


def test_list_rejects_unknown_validity_without_upstream_call(client, search):
    resp = client.get(f"{BASE}/documents", params={"validity": "forever"})
    assert resp.status_code == 422
    assert _fields(resp) == {"validity"}
    assert search.document_calls == []


def test_http_adapter_sends_filters_only_when_set(monkeypatch):
    rec = Recorder(FakeResponse(200, {"documents": [], "next_cursor": None}))
    monkeypatch.setattr(search_mod.requests, "request", rec)
    http = HttpSearchClient("http://search/v1", "http://search")
    http.list_documents("ZS_DEMO", "all", None, 5, None, type_code="X", role="PARENT",
                        validity="expired", source="a.pdf")
    assert rec.calls[0]["params"] == {"legal_entity": "ZS_DEMO", "status": "all", "limit": 5,
                                      "type": "X", "role": "PARENT", "validity": "expired",
                                      "source": "a.pdf"}
    http.list_documents("ZS_DEMO", "all", None, 5, None)
    assert set(rec.calls[1]["params"]) == {"legal_entity", "status", "limit"}


# ── PATCH validity ──────────────────────────────────────────────────────
def test_patch_validity_success(client, search):
    resp = client.patch(f"{BASE}/documents/{DOC_UUID}/validity", headers=HEADERS,
                        json={"valid_from": "2026-09-01", "valid_to": None})
    assert resp.status_code == 200
    assert resp.json()["document"]["valid_from"] == "2026-09-01"
    assert search.document_calls[-1] == ("validity", "ZS_DEMO", DOC_UUID, "2026-09-01", None)


def test_patch_validity_requires_csrf(client, search):
    resp = client.patch(f"{BASE}/documents/{DOC_UUID}/validity",
                        json={"valid_from": None, "valid_to": None})
    assert resp.status_code == 403
    assert search.document_calls == []


@pytest.mark.parametrize("body,fields", [
    ({"valid_from": "2026-13-40", "valid_to": None}, {"valid_from"}),
    ({"valid_from": None, "valid_to": "soon"}, {"valid_to"}),
    ({"valid_from": "2027-01-01", "valid_to": "2026-01-01"}, {"valid_from"}),
    ({"valid_from": "2026-09-01"}, {"valid_to"}),            # both keys are required
    ({"valid_from": None, "valid_to": None, "x": 1}, {"x"}),  # no extra keys
])
def test_patch_validity_422_before_upstream(client, search, body, fields):
    resp = client.patch(f"{BASE}/documents/{DOC_UUID}/validity", headers=HEADERS, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _fields(resp) == fields
    assert search.document_calls == []


def test_patch_validity_bad_uuid_404_and_upstream_errors(client, search):
    ok = {"valid_from": None, "valid_to": None}
    assert client.patch(f"{BASE}/documents/nope/validity", headers=HEADERS,
                        json=ok).status_code == 400
    unknown = "bbbbbbbb-0000-0000-0000-000000000009"
    resp = client.patch(f"{BASE}/documents/{unknown}/validity", headers=HEADERS, json=ok)
    assert resp.status_code == 404
    search.raise_unavailable_on = "update_document_validity"
    assert client.patch(f"{BASE}/documents/{DOC_UUID}/validity", headers=HEADERS,
                        json=ok).status_code == 503


def test_http_adapter_patch_sends_both_keys(monkeypatch):
    rec = Recorder(FakeResponse(200, {"document": {}}))
    monkeypatch.setattr(search_mod.requests, "request", rec)
    HttpSearchClient("http://search/v1", "http://search").update_document_validity(
        "ZS_DEMO", DOC_UUID.upper(), None, "2027-01-01")
    call = rec.calls[0]
    assert call["args"] == ("PATCH", f"http://search/internal/qa-admin/documents/{DOC_UUID}/validity")
    assert call["params"] == {"legal_entity": "ZS_DEMO"}
    assert call["json"] == {"valid_from": None, "valid_to": "2027-01-01"}


def test_http_adapter_maps_search_422(monkeypatch):
    body = {"error": {"code": "VALIDATION_ERROR", "fields": [{"field": "valid_to",
                                                              "message": "bad"}]}}
    monkeypatch.setattr(search_mod.requests, "request", Recorder(FakeResponse(422, body)))
    with pytest.raises(search_mod.SearchValidationError):
        HttpSearchClient("http://s/v1", "http://s").update_document_validity(
            "ZS_DEMO", DOC_UUID, None, None)


# ── check-sources ───────────────────────────────────────────────────────
def _check(client, sources):
    return client.post(f"{BASE}/documents/check-sources", headers=HEADERS,
                       json={"sources": sources})


def test_check_sources_url_always_supersedes(client, search):
    _add_doc(search, "aaaaaaaa-0000-0000-0000-000000000002", title="https://skola.example/",
             source_kind="web", source_url="https://skola.example/",
             original_file_name=None, user_name="Someone Else")
    resp = _check(client, [{"kind": "url", "value": "  https://skola.example/  "},
                           {"kind": "url", "value": "https://none.example/"}])
    assert resp.status_code == 200
    first, second = resp.json()["results"]
    assert first["existing"]["source_url"] == "https://skola.example/"
    assert first["supersedes"] is True
    assert first["value"] == "  https://skola.example/  "
    assert second == {"kind": "url", "value": "https://none.example/",
                      "existing": None, "supersedes": False}
    # trimmed before search is called, status=all
    assert search.document_filters[0]["source"] == "https://skola.example/"
    assert search.document_calls[0][2] == "all"


def test_check_sources_file_supersedes_only_for_the_technical_uploader(client, search):
    # DOCUMENT is uploaded by "QA portal" (not the technical uploader's name)
    other = _check(client, [{"kind": "file", "value": "skolni_rad.pdf"}]).json()["results"][0]
    assert other["existing"]["file_uuid"] == DOC_UUID
    assert other["supersedes"] is False

    own = _add_doc(search, "aaaaaaaa-0000-0000-0000-000000000003",
                   user_name=UPLOADER, original_file_name="skolni_rad.pdf")
    mine = _check(client, [{"kind": "file", "value": "skolni_rad.pdf"}]).json()["results"][0]
    assert mine["existing"]["file_uuid"] == own["file_uuid"]
    assert mine["supersedes"] is True


def test_check_sources_a_url_value_does_not_match_a_file_name(client, search):
    _add_doc(search, "aaaaaaaa-0000-0000-0000-000000000004", original_file_name="x",
             source_url=None, user_name=UPLOADER)
    res = _check(client, [{"kind": "url", "value": "x"}]).json()["results"][0]
    assert res["existing"] is None and res["supersedes"] is False


def test_check_sources_batch_bound_and_validation(client, search):
    many = [{"kind": "url", "value": f"https://a.example/{i}"}
            for i in range(settings.max_document_batch_items + 1)]
    resp = _check(client, many)
    assert resp.status_code == 422 and _fields(resp) == {"sources"}
    assert search.document_calls == []
    exact = _check(client, many[:settings.max_document_batch_items])
    assert exact.status_code == 200
    blank = _check(client, [{"kind": "url", "value": "   "}])
    assert blank.status_code == 422 and _fields(blank) == {"sources[0].value"}
    assert _check(client, [{"kind": "dir", "value": "x"}]).status_code == 422


def test_check_sources_csrf_and_errors(client, search):
    body = {"sources": [{"kind": "url", "value": "https://a.example/"}]}
    assert client.post(f"{BASE}/documents/check-sources", json=body).status_code == 403
    search.raise_unavailable_on = "list_documents"
    assert _check(client, body["sources"]).status_code == 503
    search.raise_unavailable_on = None
    search.raise_server_error_on = "list_documents"
    resp = _check(client, body["sources"])
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_check_sources_missing_uploader_is_503(client, search):
    search.raise_unavailable_on = "get_uploader"
    resp = _check(client, [{"kind": "file", "value": "a.pdf"}])
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "PUBLISH_UPLOADER_UNAVAILABLE"


# ── preview-url ─────────────────────────────────────────────────────────
def _preview(client, **over):
    body = {"url": "https://skola.example/", "save_level_1_child": True,
            "max_child_links": 3, "metadata": META, **over}
    return client.post(f"{BASE}/documents/preview-url", headers=HEADERS, json=body)


def test_preview_url_dry_run_as_technical_uploader(client, ingest):
    resp = _preview(client)
    assert resp.status_code == 200
    assert resp.json() == {"files": [
        {"source_url": "https://skola.example/", "source_type": "web", "page_role": "parent"},
        {"source_url": "https://skola.example/a.pdf", "source_type": "pdf",
         "page_role": "child"}]}
    call = ingest.preview_received[0]
    assert call["dry_run"] is True
    assert call["user_id"] == "qa_portal__ZS_DEMO" and call["legal_entity"] == "ZS_DEMO"
    assert call["url"] == "https://skola.example/"
    assert call["save_level_1_child"] is True and call["max_child_links"] == 3
    assert call["audience_roles"] == ["PARENT"]
    assert ingest.document_received == []  # a preview never ingests


def test_preview_url_defaults_child_links(client, ingest):
    assert _preview(client, max_child_links=None).status_code == 200
    assert ingest.preview_received[0]["max_child_links"] == settings.default_document_child_links


@pytest.mark.parametrize("over,fields", [
    ({"url": "ftp://x.example/"}, {"url"}),
    ({"url": "not a url"}, {"url"}),
    ({"max_child_links": 11}, {"max_child_links"}),
    ({"max_child_links": 0}, {"max_child_links"}),
    ({"metadata": {**META, "audience_roles": ["NOPE"]}}, {"metadata.audience_roles[0]"}),
    ({"metadata": {**META, "valid_from": "2027-01-01", "valid_to": "2026-01-01"}},
     {"metadata.valid_from"}),
])
def test_preview_url_422_without_gateway_call(client, ingest, over, fields):
    resp = _preview(client, **over)
    assert resp.status_code == 422
    assert _fields(resp) == fields
    assert ingest.preview_received == []


def test_preview_url_csrf_and_unknown_body_field(client, ingest):
    body = {"url": "https://skola.example/", "metadata": META}
    assert client.post(f"{BASE}/documents/preview-url", json=body).status_code == 403
    resp = client.post(f"{BASE}/documents/preview-url", headers=HEADERS,
                       json={**body, "user_id": "evil"})
    assert resp.status_code == 422 and ingest.preview_received == []


def test_preview_url_gateway_4xx_is_400_with_its_message(client, ingest):
    ingest.preview_fail = "rejected"
    resp = _preview(client)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
    assert "URL could not be fetched" in resp.json()["error"]["message"]


def test_preview_url_gateway_down_is_503(client, ingest):
    ingest.preview_fail = "unavailable"
    resp = _preview(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE"


def test_preview_url_missing_uploader_is_503(client, search):
    search.raise_unavailable_on = "get_uploader"
    resp = _preview(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "PUBLISH_UPLOADER_UNAVAILABLE"


def test_http_ingest_preview_sends_dry_run_and_short_timeout(monkeypatch):
    rec = Recorder(FakeResponse(200, {"files": []}))
    monkeypatch.setattr(ingest_mod.requests, "post", rec)
    HttpIngestClient("http://gw").preview_document_url(
        legal_entity="ZS_DEMO", user_id="u", visibility="entity", audience_roles=["PARENT"],
        url="https://skola.example/", save_level_1_child=True, max_child_links=2)
    call = rec.calls[0]
    assert call["args"] == ("http://gw/v1/ingest",)
    assert call["data"] == [
        ("legal_entity", "ZS_DEMO"), ("user_id", "u"), ("visibility", "entity"),
        ("url", "https://skola.example/"), ("save_level_1_child", "true"),
        ("max_child_links", "2"), ("audience_roles", "PARENT"), ("dry_run", "true")]
    assert call["files"] is None
    assert call["timeout"] == settings.document_preview_timeout_seconds == 120


def test_http_ingest_preview_gateway_4xx_is_rejected(monkeypatch):
    monkeypatch.setattr(ingest_mod.requests, "post",
                        Recorder(FakeResponse(400, {"detail": "bad"})))
    with pytest.raises(IngestRejected):
        HttpIngestClient("http://gw").preview_document_url(
            legal_entity="Z", user_id="u", visibility="entity", audience_roles=["P"],
            url="https://a.example/")


# ── search 500 -> 502 UPSTREAM_ERROR (retryable false) ──────────────────
@pytest.mark.parametrize("status,exc,expected", [
    (500, SearchServerError, 502),
    (502, SearchUnavailable, 503),
    (503, SearchUnavailable, 503),
    (504, SearchUnavailable, 503),
])
def test_http_adapter_5xx_classes(monkeypatch, status, exc, expected):
    monkeypatch.setattr(search_mod.requests, "request",
                        Recorder(FakeResponse(status, None, text="boom")))
    monkeypatch.setattr(search_mod.requests, "get",
                        Recorder(FakeResponse(status, None, text="boom")))
    http = HttpSearchClient("http://s/v1", "http://s")
    for call in (lambda: http.list_documents("ZS_DEMO", "all", None, 1, None),
                 lambda: http.get_document_types()):
        with pytest.raises(exc) as e:
            call()
        assert (type(e.value) is SearchServerError) == (status == 500)


@pytest.mark.parametrize("method,path,kw", [
    ("GET", "documents", {}),
    ("GET", f"documents/{DOC_UUID}", {}),
    ("GET", "document-options", {}),
    ("POST", f"documents/{DOC_UUID}/deactivate", {}),
    ("POST", f"documents/{DOC_UUID}/activate", {}),
    ("PATCH", f"documents/{DOC_UUID}/validity",
     {"json": {"valid_from": None, "valid_to": None}}),
    ("POST", "documents/check-sources",
     {"json": {"sources": [{"kind": "url", "value": "https://a.example/"}]}}),
    ("POST", "documents/preview-url",
     {"json": {"url": "https://a.example/", "metadata": META}}),
])
@pytest.mark.parametrize("failing", ["list_documents", "get_document", "get_code_lists",
                                     "deactivate_document", "activate_document",
                                     "update_document_validity", "get_document_types",
                                     "list_active_entities", "get_domains"])
def test_search_500_is_502_upstream_error_everywhere(client, search, method, path, kw, failing):
    search.raise_server_error_on = failing
    resp = client.request(method, f"{BASE}/{path}", headers=HEADERS, **kw)
    if resp.status_code in (200, 404, 422, 400):
        return  # this route does not call the failing method
    assert resp.status_code == 502, (path, failing, resp.text)
    err = resp.json()["error"]
    assert err["code"] == "UPSTREAM_ERROR" and err["retryable"] is False
    assert "permission denied" not in resp.text


def test_search_500_on_upload_validation_and_uploader_is_502(client, search):
    search.raise_server_error_on = "get_uploader"
    resp = client.post(f"{BASE}/documents/check-sources", headers=HEADERS,
                       json={"sources": [{"kind": "file", "value": "a.pdf"}]})
    assert resp.status_code == 502


def test_search_unavailable_stays_503_retryable(client, search):
    search.raise_unavailable_on = "list_documents"
    err = client.get(f"{BASE}/documents").json()["error"]
    assert err["code"] == "UNAVAILABLE" and err["retryable"] is True


def test_qa_routes_keep_503_on_search_500(client, search):
    search.raise_server_error_on = "list_qa_packs"
    resp = client.get(f"{BASE}/packs")
    assert resp.status_code == 503


# ── policy: a foreign school with a restricted policy ───────────────────
@pytest.fixture
def restricted(tmp_path):
    search = TwoSchoolSearch()
    ingest = FakeIngestClient()
    store = StateStore(tmp_path / "state")
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_policy] = lambda: RestrictedPolicy()
    app.dependency_overrides[get_principal] = lambda: STAFF
    yield TestClient(app), search, ingest
    app.dependency_overrides.clear()


NEW_ROUTES = [
    ("PATCH", "documents/{u}/validity", {"json": {"valid_from": None, "valid_to": None}}),
    ("POST", "documents/check-sources",
     {"json": {"sources": [{"kind": "url", "value": "https://a.example/"}]}}),
    ("POST", "documents/preview-url", {"json": {"url": "https://a.example/", "metadata": META}}),
]


@pytest.mark.parametrize("method,path,kw", NEW_ROUTES)
def test_new_routes_foreign_school_is_403(restricted, method, path, kw):
    client, search, ingest = restricted
    resp = client.request(method, f"/qa-api/v1/schools/{OTHER}/" + path.format(u=DOC_UUID),
                          headers=HEADERS, **kw)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert search.document_calls == [] and ingest.preview_received == []


@pytest.mark.parametrize("method,path,kw", NEW_ROUTES)
def test_new_routes_own_school_passes(restricted, method, path, kw):
    client, _, _ = restricted
    resp = client.request(method, "/qa-api/v1/schools/ZS_DEMO/" + path.format(u=DOC_UUID),
                          headers=HEADERS, **kw)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("deny,method,path,kw", [
    ("deactivate", *NEW_ROUTES[0]), ("ingest", *NEW_ROUTES[1]), ("ingest", *NEW_ROUTES[2])])
def test_new_routes_ask_their_own_permission(search, ingest, tmp_path, deny, method, path, kw):
    store = StateStore(tmp_path / "state")
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_policy] = lambda: RestrictedPolicy(frozenset({deny}))
    app.dependency_overrides[get_principal] = lambda: STAFF
    try:
        resp = TestClient(app).request(
            method, "/qa-api/v1/schools/ZS_DEMO/" + path.format(u=DOC_UUID),
            headers=HEADERS, **kw)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


# ── SPA at /knowledge, API 404 stays JSON ───────────────────────────────
@pytest.fixture
def spa_dir(tmp_path):
    d = tmp_path / "spa"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    (d / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    return d


def test_spa_served_under_knowledge(spa_dir):
    spa = FastAPI()
    mount_spa(spa, spa_dir)
    c = TestClient(spa)
    for path in ("/knowledge", "/knowledge/", "/knowledge/qa", "/knowledge/documents"):
        resp = c.get(path)
        assert resp.status_code == 200 and "<title>spa</title>" in resp.text, path
    asset = c.get("/knowledge/assets/app.js")
    assert asset.status_code == 200 and asset.text == "console.log(1)"
    # a deploy must reach the browser: index revalidated, hashed assets immutable
    assert c.get("/knowledge/documents").headers["cache-control"] == "no-cache"
    assert "immutable" in asset.headers["cache-control"]
    # the old /qa address is gone (nginx answers it 404 as well)
    assert c.get("/qa").status_code == 404 and c.get("/qa/documents").status_code == 404


def test_spa_does_not_escape_the_static_dir(spa_dir):
    spa = FastAPI()
    mount_spa(spa, spa_dir)
    resp = TestClient(spa).get("/knowledge/%2e%2e/secret.txt")
    assert "secret" not in resp.text


def test_unknown_api_path_stays_json_404_with_spa_mounted(spa_dir):
    before = list(app.router.routes)
    mount_spa(app, spa_dir)
    try:
        c = TestClient(app)
        for path in ("/qa-api/v1/nope", "/qa-api/v1/knowledge/x"):
            resp = c.get(path)
            assert resp.status_code == 404
            assert "application/json" in resp.headers["content-type"]
            assert "<html" not in resp.text.lower()
            assert resp.json()["error"]["code"] == "NOT_FOUND"
        assert c.get("/knowledge/documents").status_code == 200
    finally:
        app.router.routes[:] = before
