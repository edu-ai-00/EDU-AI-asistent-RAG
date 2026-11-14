"""HTTP adapters for documents (CONTRACT.md §14.2-§14.4): multipart field
mapping of document ingest, `run_ingest` still sending exactly what it did,
the gateway delete and the search document endpoints' error mapping."""
from __future__ import annotations

import io

import pytest
import requests

from app.adapters import ingest as ingest_mod
from app.adapters import search as search_mod
from app.adapters.ingest import (HttpIngestClient, IngestNotFound, IngestRejected,
                                 IngestUnavailable)
from app.adapters.search import (HttpSearchClient, SearchBadRequest, SearchNotFound,
                                 SearchUnavailable)
from app.config import settings

FU = "aaaaaaaa-0000-0000-0000-000000000001"


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class Recorder:
    def __init__(self, response=None, exc=None):
        self.calls: list[dict] = []
        self.response = response or FakeResponse(200, {"status": "completed"})
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        if self.exc:
            raise self.exc
        return self.response


@pytest.fixture
def post(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ingest_mod.requests, "post", rec)
    return rec


def test_run_ingest_sends_exactly_the_same_fields(post):
    HttpIngestClient("http://gw").run_ingest(
        csv_bytes=b"a,b", filename="faq.csv", legal_entity="ZS_DEMO",
        user_id="qa_portal__ZS_DEMO", visibility="entity", pack_id="faq", qa_mode="merge",
        domains=["PROVOZ", "GDPR"], audience_roles=["PARENT"],
        school_subject_grades=["1", "2"], valid_from="2026-09-01", valid_to="2027-08-31")
    call = post.calls[0]
    assert call["args"] == ("http://gw/v1/ingest",)
    assert call["data"] == [
        ("legal_entity", "ZS_DEMO"), ("user_id", "qa_portal__ZS_DEMO"),
        ("visibility", "entity"), ("user_defined_pack_id", "faq"), ("qa_mode", "merge"),
        ("domains", "PROVOZ"), ("domains", "GDPR"), ("audience_roles", "PARENT"),
        ("school_subject_grades", "1"), ("school_subject_grades", "2"),
        ("valid_from", "2026-09-01"), ("valid_to", "2027-08-31")]
    assert call["files"] == {"file": ("faq.csv", b"a,b", "text/csv")}
    assert call["timeout"] == max(settings.http_timeout_seconds, 300.0)


def test_run_ingest_minimal_keeps_required_fields_even_if_empty(post):
    HttpIngestClient("http://gw").run_ingest(
        csv_bytes=b"", filename="x.csv", legal_entity="ZS_DEMO", user_id="u",
        visibility="entity", pack_id="", qa_mode="replace")
    assert post.calls[0]["data"] == [
        ("legal_entity", "ZS_DEMO"), ("user_id", "u"), ("visibility", "entity"),
        ("user_defined_pack_id", ""), ("qa_mode", "replace")]


def test_document_ingest_file_field_mapping(post):
    fh = io.BytesIO(b"%PDF")
    HttpIngestClient("http://gw").run_document_ingest(
        legal_entity="ZS_DEMO", user_id="qa_portal__ZS_DEMO", visibility="organization",
        audience_roles=["PARENT", "TEACHER"], file=fh, filename="rad.pdf",
        document_type="SCHOOL_RULES", document_language="cs", school_subject="MAT",
        school_grade_codes=["1"], domain_codes=["PROVOZ"],
        valid_from="2026-09-01", valid_to=None)
    call = post.calls[0]
    assert call["data"] == [
        ("legal_entity", "ZS_DEMO"), ("user_id", "qa_portal__ZS_DEMO"),
        ("visibility", "organization"),
        ("audience_roles", "PARENT"), ("audience_roles", "TEACHER"),
        ("school_subject_grades", "1"), ("domains", "PROVOZ"),
        ("document_type", "SCHOOL_RULES"), ("document_language", "cs"),
        ("school_subject", "MAT"), ("valid_from", "2026-09-01")]
    assert call["files"] == {"file": ("rad.pdf", fh, "application/octet-stream")}
    assert call["timeout"] == settings.document_ingest_timeout_seconds == 1800


def test_document_ingest_url_field_mapping(post):
    HttpIngestClient("http://gw").run_document_ingest(
        legal_entity="ZS_DEMO", user_id="u", visibility="entity", audience_roles=["PARENT"],
        url="https://skola.example/", save_level_1_child=True, max_child_links=7)
    call = post.calls[0]
    assert call["data"] == [
        ("legal_entity", "ZS_DEMO"), ("user_id", "u"), ("visibility", "entity"),
        ("url", "https://skola.example/"), ("save_level_1_child", "true"),
        ("max_child_links", "7"), ("audience_roles", "PARENT")]
    assert call["files"] is None


@pytest.mark.parametrize("kw", [{}, {"file": b"x", "url": "https://a.example/"}])
def test_document_ingest_needs_exactly_one_source(post, kw):
    with pytest.raises(ValueError):
        HttpIngestClient("http://gw").run_document_ingest(
            legal_entity="Z", user_id="u", visibility="entity", audience_roles=["P"], **kw)
    assert post.calls == []


@pytest.mark.parametrize("response,exc,expected", [
    (FakeResponse(400, {"detail": "Unknown document_type"}), None, IngestRejected),
    (FakeResponse(502, None, text="bad gateway"), None, IngestUnavailable),
    (None, requests.ConnectionError("down"), IngestUnavailable),
])
def test_document_ingest_errors(monkeypatch, response, exc, expected):
    monkeypatch.setattr(ingest_mod.requests, "post", Recorder(response, exc))
    with pytest.raises(expected) as e:
        HttpIngestClient("http://gw").run_document_ingest(
            legal_entity="Z", user_id="u", visibility="entity", audience_roles=["P"],
            url="https://a.example/")
    if expected is IngestRejected:
        assert e.value.status_code == 400
        assert e.value.body == {"detail": "Unknown document_type"}


def test_delete_document_calls_gateway(monkeypatch):
    body = {"deleted": {"versions": 1, "chunks": 2, "elements": 0, "tables": 0},
            "file_uuids": [FU], "files_removed": 1, "warnings": []}
    rec = Recorder(FakeResponse(200, body))
    monkeypatch.setattr(ingest_mod.requests, "delete", rec)
    assert HttpIngestClient("http://gw").delete_document("ZS_DEMO", FU.upper()) == body
    call = rec.calls[0]
    assert call["args"] == (f"http://gw/v1/documents/{FU}",)
    assert call["params"] == {"legal_entity": "ZS_DEMO"}


@pytest.mark.parametrize("response,exc,expected", [
    (FakeResponse(404, {"detail": "not found"}), None, IngestNotFound),
    (FakeResponse(400, {"detail": "file_uuid must be a UUID"}), None, IngestRejected),
    (FakeResponse(503, {"detail": "search unavailable"}), None, IngestUnavailable),
    (None, requests.Timeout("slow"), IngestUnavailable),
])
def test_delete_document_errors(monkeypatch, response, exc, expected):
    monkeypatch.setattr(ingest_mod.requests, "delete", Recorder(response, exc))
    with pytest.raises(expected):
        HttpIngestClient("http://gw").delete_document("ZS_DEMO", FU)


def test_delete_document_never_sends_a_raw_path(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ingest_mod.requests, "delete", rec)
    with pytest.raises(ValueError):
        HttpIngestClient("http://gw").delete_document("ZS_DEMO", "../../etc")
    assert rec.calls == []


# ── search adapter ──────────────────────────────────────────────────────
@pytest.fixture
def search_http():
    return HttpSearchClient("http://search/v1", "http://search")


def test_list_documents_params(monkeypatch, search_http):
    rec = Recorder(FakeResponse(200, {"documents": [], "next_cursor": None}))
    monkeypatch.setattr(search_mod.requests, "request", rec)
    search_http.list_documents("ZS_DEMO", "all", "rad", 20, "50")
    call = rec.calls[0]
    assert call["args"] == ("GET", "http://search/internal/qa-admin/documents")
    assert call["params"] == {"legal_entity": "ZS_DEMO", "status": "all", "limit": 20,
                              "q": "rad", "cursor": "50"}


@pytest.mark.parametrize("method,name,path", [
    ("GET", "get_document", f"/documents/{FU}"),
    ("POST", "deactivate_document", f"/documents/{FU}/deactivate"),
    ("POST", "activate_document", f"/documents/{FU}/activate"),
])
def test_document_paths(monkeypatch, search_http, method, name, path):
    rec = Recorder(FakeResponse(200, {"document": {}}))
    monkeypatch.setattr(search_mod.requests, "request", rec)
    getattr(search_http, name)("ZS_DEMO", FU.upper())
    assert rec.calls[0]["args"] == (method, f"http://search/internal/qa-admin{path}")
    assert rec.calls[0]["params"] == {"legal_entity": "ZS_DEMO"}


@pytest.mark.parametrize("response,exc,expected", [
    (FakeResponse(404, None, text="nf"), None, SearchNotFound),
    (FakeResponse(400, {"error": {"code": "BAD_REQUEST"}}), None, SearchBadRequest),
    (FakeResponse(500, None, text="boom"), None, SearchUnavailable),
    (None, requests.ConnectionError("down"), SearchUnavailable),
])
def test_document_error_mapping(monkeypatch, search_http, response, exc, expected):
    monkeypatch.setattr(search_mod.requests, "request", Recorder(response, exc))
    with pytest.raises(expected):
        search_http.deactivate_document("ZS_DEMO", FU)


def test_non_uuid_never_reaches_search(monkeypatch, search_http):
    rec = Recorder()
    monkeypatch.setattr(search_mod.requests, "request", rec)
    with pytest.raises(SearchBadRequest):
        search_http.get_document("ZS_DEMO", "../pack-state")
    assert rec.calls == []


def test_catalog_endpoints_unwrap(monkeypatch, search_http):
    rec = Recorder(FakeResponse(200, {"document_types": [{"type_code": "X"}],
                                      "validity_periods": [{"id": 1}]}))
    monkeypatch.setattr(search_mod.requests, "get", rec)
    assert search_http.get_document_types() == [{"type_code": "X"}]
    assert rec.calls[0]["args"] == ("http://search/v1/document-types",)
    assert search_http.get_validity_periods("ZS_DEMO") == [{"id": 1}]
    assert rec.calls[1]["args"] == ("http://search/v1/validity-periods",)
    assert rec.calls[1]["params"] == {"legal_entity": "ZS_DEMO"}
