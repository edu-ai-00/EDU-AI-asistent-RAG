"""CONTRACT.md §14.10: `format` list filter, stored-file download (stream
passthrough, headers, error mapping, policy) and file_name never leaking."""
from __future__ import annotations

import pytest
import requests
from fastapi.testclient import TestClient

from app.adapters import ingest as ingest_mod
from app.adapters.ingest import (HttpIngestClient, IngestNotFound, IngestRejected,
                                 IngestUnavailable)
from app.adapters.search import HttpSearchClient
from app.adapters import search as search_mod
from app.api.deps import (get_ingest_client, get_policy, get_principal, get_search_client,
                          get_store)
from app.config import settings
from app.main import app
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import DOC_UUID, DOCUMENT, FakeSearchClient
from tests.test_document_adapters import FakeResponse, Recorder
from tests.test_document_policy import OTHER, STAFF, RestrictedPolicy, TwoSchoolSearch

HEADERS = {"X-QA-Portal": "1"}
BASE = "/qa-api/v1/schools/ZS_DEMO"
VERSION_KEYS = {"file_uuid", "processing_uuid", "ingested_at", "is_active", "chunk_count",
                "file_format", "content_sha256"}


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


# ── format filter ───────────────────────────────────────────────────────
@pytest.mark.parametrize("fmt", ["pdf", "docx", "pptx", "xlsx", "web"])
def test_list_passes_each_format_to_search(client, search, fmt):
    assert client.get(f"{BASE}/documents", params={"format": fmt}).status_code == 200
    assert search.document_formats[-1] == fmt


def test_list_without_format_sends_none(client, search):
    client.get(f"{BASE}/documents")
    assert search.document_formats[-1] is None


def test_format_filter_narrows_the_result(client):
    assert client.get(f"{BASE}/documents", params={"format": "pdf"}).json()["documents"]
    assert client.get(f"{BASE}/documents", params={"format": "docx"}).json()["documents"] == []


@pytest.mark.parametrize("fmt", ["exe", "PDF", "txt", "pdf,docx"])
def test_unknown_format_is_422_and_never_reaches_search(client, search, fmt):
    resp = client.get(f"{BASE}/documents", params={"format": fmt})
    assert resp.status_code == 422
    assert resp.json()["error"]["fields"][0]["field"] == "format"
    assert search.document_calls == []


def test_summary_carries_file_format(client):
    assert client.get(f"{BASE}/documents").json()["documents"][0]["file_format"] == "pdf"


# ── file_name never leaks ───────────────────────────────────────────────
def test_detail_versions_are_whitelisted_and_file_name_is_stripped(client, search):
    resp = client.get(f"{BASE}/documents/{DOC_UUID}")
    assert resp.status_code == 200
    (version,) = resp.json()["versions"]
    assert set(version) == VERSION_KEYS
    assert version["file_format"] == "pdf"
    assert '"file_name"' not in resp.text and f"{DOC_UUID}_" not in resp.text


def test_file_name_on_the_document_object_is_stripped_too(client, search):
    search._documents["ZS_DEMO"][DOC_UUID]["file_name"] = f"{DOC_UUID}_x.pdf"
    resp = client.get(f"{BASE}/documents/{DOC_UUID}")
    assert "file_name" not in resp.json()["document"]


# ── download ────────────────────────────────────────────────────────────
def test_download_streams_the_bytes_unchanged_with_safe_headers(client, ingest):
    ingest.download_chunks = [b"abc", b"\x00\xff", b"def" * 100000]
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/download")
    assert resp.status_code == 200
    assert resp.content == b"abc" + b"\x00\xff" + b"def" * 100000
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"] == 'attachment; filename="skolni_rad.pdf"'
    assert ingest.download_calls == [("ZS_DEMO", DOC_UUID)]
    assert ingest.download_closed == 1


def test_download_never_serves_inline_even_for_html(client, ingest):
    ingest.download_chunks = [b"<script>alert(1)</script>"]
    ingest.download_disposition = 'attachment; filename="page.html"'
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/download")
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")


def test_download_without_a_gateway_disposition_is_still_an_attachment(client, ingest):
    ingest.download_disposition = None
    assert client.get(f"{BASE}/documents/{DOC_UUID}/download").headers["content-disposition"] == "attachment"


def test_download_needs_no_csrf_header(client):
    assert client.get(f"{BASE}/documents/{DOC_UUID}/download").status_code == 200


def test_download_canonicalises_the_uuid(client, ingest):
    client.get(f"{BASE}/documents/{DOC_UUID.upper()}/download")
    assert ingest.download_calls[0][1] == DOC_UUID


def test_download_of_an_unknown_or_unstored_file_is_404(client, ingest):
    ingest.download_fail = "not_found"
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/download")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_download_bad_uuid_is_400_without_calling_the_gateway(client, ingest):
    resp = client.get(f"{BASE}/documents/not-a-uuid/download")
    assert resp.status_code == 400
    assert ingest.download_calls == []


def test_download_gateway_400_is_400_with_its_message(client, ingest):
    ingest.download_fail = "rejected"
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/download")
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "file_uuid must be a UUID"


def test_download_gateway_unavailable_is_503_retryable(client, ingest):
    ingest.download_fail = "unavailable"
    resp = client.get(f"{BASE}/documents/{DOC_UUID}/download")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE" and resp.json()["error"]["retryable"]
    assert "outage" not in resp.text


def test_download_unknown_school_is_404(client, ingest):
    assert client.get(f"/qa-api/v1/schools/NOPE/documents/{DOC_UUID}/download").status_code == 404
    assert ingest.download_calls == []


def _restricted(policy, tmp_path):
    two, ingest = TwoSchoolSearch(), FakeIngestClient()
    app.dependency_overrides[get_search_client] = lambda: two
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: StateStore(tmp_path / "s")
    app.dependency_overrides[get_policy] = lambda: policy
    app.dependency_overrides[get_principal] = lambda: STAFF
    return TestClient(app), ingest


def test_foreign_school_download_is_403_and_never_reaches_the_gateway(tmp_path):
    try:
        client, ingest = _restricted(RestrictedPolicy(), tmp_path)
        resp = client.get(f"/qa-api/v1/schools/{OTHER}/documents/{DOC_UUID}/download")
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"
        assert ingest.download_calls == []
    finally:
        app.dependency_overrides.clear()


def test_download_asks_the_read_permission(tmp_path):
    try:
        client, ingest = _restricted(RestrictedPolicy(frozenset({"read"})), tmp_path)
        assert client.get(f"{BASE}/documents/{DOC_UUID}/download").status_code == 403
        assert ingest.download_calls == []
        client, ingest = _restricted(RestrictedPolicy(frozenset({"delete", "ingest"})), tmp_path)
        assert client.get(f"{BASE}/documents/{DOC_UUID}/download").status_code == 200
    finally:
        app.dependency_overrides.clear()


# ── HTTP adapters ───────────────────────────────────────────────────────
class StreamResponse(FakeResponse):
    def __init__(self, status_code=200, body=None, text=None, chunks=(), headers=None,
                 fail_after=None):
        super().__init__(status_code, body, text)
        self._chunks, self.headers = list(chunks), headers or {}
        self._fail_after = fail_after
        self.closed = 0

    def iter_content(self, chunk_size=None):
        for i, c in enumerate(self._chunks):
            if self._fail_after is not None and i == self._fail_after:
                raise requests.ConnectionError("cut")
            yield c

    def close(self):
        self.closed += 1


def _open(monkeypatch, response=None, exc=None):
    rec = Recorder(response, exc)
    monkeypatch.setattr(ingest_mod.requests, "get", rec)
    return HttpIngestClient("http://gw"), rec


def test_adapter_streams_the_gateway_file(monkeypatch):
    resp = StreamResponse(chunks=[b"a", b"b"], headers={"Content-Disposition": "attachment; filename=x.pdf"})
    http, rec = _open(monkeypatch, resp)
    stored = http.open_document_file("ZS_DEMO", DOC_UUID.upper())
    call = rec.calls[0]
    assert call["args"] == (f"http://gw/v1/documents/{DOC_UUID}/file",)
    assert call["params"] == {"legal_entity": "ZS_DEMO"} and call["stream"] is True
    assert call["timeout"] == settings.document_download_timeout_seconds
    assert stored.content_disposition == "attachment; filename=x.pdf"
    assert b"".join(stored.chunks) == b"ab" and resp.closed == 1


@pytest.mark.parametrize("status,exc", [(404, IngestNotFound), (400, IngestRejected),
                                        (500, IngestUnavailable), (503, IngestUnavailable)])
def test_adapter_error_mapping_closes_the_connection(monkeypatch, status, exc):
    resp = StreamResponse(status, {"detail": "x"})
    http, _ = _open(monkeypatch, resp)
    with pytest.raises(exc):
        http.open_document_file("ZS_DEMO", DOC_UUID)
    assert resp.closed == 1


def test_adapter_network_error_is_unavailable(monkeypatch):
    http, _ = _open(monkeypatch, exc=requests.ConnectionError("down"))
    with pytest.raises(IngestUnavailable):
        http.open_document_file("ZS_DEMO", DOC_UUID)


def test_adapter_stream_cut_mid_way_is_unavailable(monkeypatch):
    resp = StreamResponse(chunks=[b"a", b"b"], fail_after=1)
    http, _ = _open(monkeypatch, resp)
    stored = http.open_document_file("ZS_DEMO", DOC_UUID)
    with pytest.raises(IngestUnavailable):
        list(stored.chunks)
    assert resp.closed == 1


def test_search_adapter_sends_format_only_when_set(monkeypatch):
    rec = Recorder(FakeResponse(200, {"documents": []}))
    monkeypatch.setattr(search_mod.requests, "request", rec)
    http = HttpSearchClient("http://s/v1", "http://s")
    http.list_documents("ZS_DEMO", "all", None, 5, None)
    http.list_documents("ZS_DEMO", "all", None, 5, None, file_format="docx")
    assert "format" not in rec.calls[0]["params"]
    assert rec.calls[1]["params"]["format"] == "docx"
