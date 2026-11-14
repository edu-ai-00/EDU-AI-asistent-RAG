"""Documents page API (CONTRACT.md §14.4): every route's success and failure,
CSRF on writes, upload validation (415/413/422), delete confirmation."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.adapters.search import SearchNotFound
from app.api.deps import get_ingest_client, get_search_client, get_store
from app.config import settings
from app.main import app
from app.services import document_queue
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import DOC_UUID, FakeSearchClient

HEADERS = {"X-QA-Portal": "1"}
BASE = "/qa-api/v1/schools/ZS_DEMO"
BATCH = "11111111-aaaa-bbbb-cccc-000000000001"
META = {"visibility": "entity", "audience_roles": ["PARENT"]}


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


def _upload(client, *, file=("rad.pdf", b"%PDF-1.4 data", "application/pdf"), url=None,
            meta=None, batch_id=BATCH, headers=HEADERS, extra=None):
    data = {}
    if url is not None:
        data["url"] = url
    if batch_id is not None:
        data["batch_id"] = batch_id
    if meta is not False:
        data["metadata"] = meta if isinstance(meta, str) else json.dumps(meta or META)
    data.update(extra or {})
    # (None, value) parts keep the body multipart even without a file
    files = {k: (None, v) for k, v in data.items()}
    if file is not None:
        files["file"] = file
    return client.post(f"{BASE}/documents", files=files, headers=headers)


def _fields(resp) -> set[str]:
    return {f["field"] for f in resp.json()["error"].get("fields") or []}


# ── reads ───────────────────────────────────────────────────────────────
def test_list_documents_passes_filters(client, search):
    resp = client.get(f"{BASE}/documents", params={"status": "all", "q": "rad", "limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["documents"][0]["file_uuid"] == DOC_UUID
    assert body["next_cursor"] is None
    assert search.document_calls[-1] == ("list", "ZS_DEMO", "all", "rad", 10, None)


def test_list_documents_defaults_to_active(client, search):
    assert client.get(f"{BASE}/documents").status_code == 200
    assert search.document_calls[-1] == ("list", "ZS_DEMO", "active", None, 50, None)


@pytest.mark.parametrize("params", [{"status": "deleted"}, {"limit": 0}, {"limit": 201}])
def test_list_documents_rejects_bad_params(client, params):
    resp = client.get(f"{BASE}/documents", params=params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_documents_upstream_outage_is_503(client, search):
    search.raise_unavailable_on = "list_documents"
    resp = client.get(f"{BASE}/documents")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE"


def test_get_document_with_versions(client):
    resp = client.get(f"{BASE}/documents/{DOC_UUID.upper()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["file_uuid"] == DOC_UUID
    assert body["versions"][0]["chunk_count"] == 12


def test_get_unknown_document_is_404(client):
    resp = client.get(f"{BASE}/documents/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_get_non_uuid_is_400_without_upstream_call(client, search):
    resp = client.get(f"{BASE}/documents/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
    assert search.document_calls == []


def test_unknown_school_is_404(client):
    resp = client.get("/qa-api/v1/schools/NOPE/documents")
    assert resp.status_code == 404


def test_document_options_shape(client):
    resp = client.get(f"{BASE}/document-options", params={"lang": "en"})
    assert resp.status_code == 200
    o = resp.json()
    assert o["visibility"] == ["entity", "organization"]
    assert {r["code"] for r in o["audience_roles"]} == {"TEACHER", "PARENT", "STUDENT"}
    assert len(o["school_grades"]) == 9
    assert [s["code"] for s in o["school_subjects"]] == ["MAT", "CJL"]
    en = next(x for x in o["languages"] if x["code"] == "en")
    assert en == {"code": "en", "label": "English"}           # requested language present
    cs = next(x for x in o["languages"] if x["code"] == "cs")
    assert cs["label_fallback"] is True                       # only Czech label exists
    assert o["document_types"][0] == {"code": "SCHOOL_RULES", "label": "Skolni rad",
                                      "label_fallback": True}
    assert o["domains"][0]["code"] == "PROVOZ"
    assert o["validity_periods"] == [{"id": 1, "name": "Skolni rok 2026/27",
                                      "valid_from": "2026-09-01", "valid_to": "2027-08-31",
                                      "is_default": True}]
    assert o["file_extensions"] == [".pdf", ".docx", ".pptx", ".xlsx"]
    assert o["max_file_bytes"] == 52428800
    assert o["max_batch_items"] == 100
    assert o["max_child_links"] == 10


def test_document_options_outage_is_503(client, search):
    search.raise_unavailable_on = "get_document_types"
    resp = client.get(f"{BASE}/document-options")
    assert resp.status_code == 503


# ── upload ──────────────────────────────────────────────────────────────
def test_upload_file_queues_job(client, store, ingest):
    meta = {**META, "document_type": "school_rules", "document_language": "cs",
            "school_subject": "MAT", "school_grade_codes": ["1", "2"],
            "domain_codes": ["PROVOZ"], "valid_from": "2026-09-01", "valid_to": "2027-08-31"}
    resp = _upload(client, meta=meta,
                   extra={"legal_entity": "EVIL", "user_id": "someone_else"})
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert set(job) == set(document_queue.PUBLIC_KEYS)
    assert job["status"] == "queued" and job["school"] == "ZS_DEMO"
    assert job["source_kind"] == "file" and job["source"] == "rad.pdf"
    assert job["batch_id"] == BATCH
    assert store.job_file_path(job["job_id"]).read_bytes() == b"%PDF-1.4 data"

    assert document_queue.process_next_job(store, ingest) == job["job_id"]
    sent = ingest.document_received[0]
    # identity comes from the path and the technical uploader, never the client
    assert sent["legal_entity"] == "ZS_DEMO"
    assert sent["user_id"] == "qa_portal__ZS_DEMO"
    assert sent["file"] == b"%PDF-1.4 data" and sent["filename"] == "rad.pdf"
    assert sent["document_type"] == "SCHOOL_RULES"            # canonical code
    assert sent["school_grade_codes"] == ["1", "2"]
    assert sent["domain_codes"] == ["PROVOZ"]
    assert sent["valid_to"] == "2027-08-31"
    assert not store.job_file_path(job["job_id"]).exists()


def test_upload_url_queues_job(client, store, ingest):
    resp = _upload(client, file=None, url="https://skola.example/rad",
                   extra={"save_level_1_child": "true", "max_child_links": "7"})
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["source_kind"] == "url" and job["source"] == "https://skola.example/rad"
    document_queue.process_next_job(store, ingest)
    sent = ingest.document_received[0]
    assert sent["url"] == "https://skola.example/rad" and sent["file"] is None
    assert sent["save_level_1_child"] is True and sent["max_child_links"] == 7


def test_upload_url_default_child_links(client, store, ingest):
    assert _upload(client, file=None, url="http://skola.example/").status_code == 202
    document_queue.process_next_job(store, ingest)
    assert ingest.document_received[0]["max_child_links"] == 5
    assert ingest.document_received[0]["save_level_1_child"] is False


def test_upload_strips_client_path_from_file_name(client):
    resp = _upload(client, file=("C:\\Users\\x\\rad.docx", b"x", "application/octet-stream"))
    assert resp.status_code == 202
    assert resp.json()["job"]["source"] == "rad.docx"


@pytest.mark.parametrize("name", ["pack.csv", "tool.exe", "noext"])
def test_upload_unsupported_extension_is_415(client, store, name):
    resp = _upload(client, file=(name, b"a;b", "text/csv"))
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert store.list_ids("jobs") == []


def test_upload_too_big_is_413_and_leaves_nothing(client, store, monkeypatch):
    monkeypatch.setattr(settings, "max_document_file_bytes", 10)
    resp = _upload(client, file=("rad.pdf", b"x" * 11, "application/pdf"))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert store.list_ids("jobs") == []
    assert list(store.job_file_path("x").parent.iterdir()) == []


def test_upload_exactly_at_limit_is_accepted(client, monkeypatch):
    monkeypatch.setattr(settings, "max_document_file_bytes", 10)
    assert _upload(client, file=("rad.pdf", b"x" * 10, "application/pdf")).status_code == 202


def test_upload_declared_length_over_limit_is_413_before_parsing(client, store, monkeypatch):
    monkeypatch.setattr(settings, "max_document_file_bytes", 10)
    resp = _upload(client, file=("rad.pdf", b"x" * (2 * 1024 * 1024), "application/pdf"))
    assert resp.status_code == 413
    assert store.list_ids("jobs") == []


def test_upload_empty_file_is_422(client):
    resp = _upload(client, file=("rad.pdf", b"", "application/pdf"))
    assert resp.status_code == 422
    assert _fields(resp) == {"file"}


def test_upload_unknown_codes_are_422_with_fields(client, store):
    meta = {"visibility": "entity", "audience_roles": ["PARENT", "ALIEN"],
            "document_type": "NOPE", "document_language": "xx", "school_subject": "ZZZ",
            "school_grade_codes": ["42"], "domain_codes": ["UNKNOWN"]}
    resp = _upload(client, meta=meta)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _fields(resp) == {"metadata.audience_roles[1]", "metadata.document_type",
                             "metadata.document_language", "metadata.school_subject",
                             "metadata.school_grade_codes[0]", "metadata.domain_codes[0]"}
    assert store.list_ids("jobs") == []


def test_upload_empty_roles_is_422(client):
    resp = _upload(client, meta={"visibility": "entity", "audience_roles": []})
    assert resp.status_code == 422
    assert "metadata.audience_roles" in _fields(resp)


@pytest.mark.parametrize("meta,field", [
    ({**META, "visibility": "private"}, "metadata.visibility"),
    ({**META, "valid_from": "2027-01-01", "valid_to": "2026-01-01"}, "metadata.valid_from"),
    ({**META, "valid_to": "31.12.2026"}, "metadata.valid_to"),
    ({**META, "owner": "x"}, "metadata.owner"),
    ({"audience_roles": ["PARENT"]}, "metadata.visibility"),
])
def test_upload_bad_metadata_is_422(client, meta, field):
    resp = _upload(client, meta=meta)
    assert resp.status_code == 422
    assert field in _fields(resp)


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", ""])
def test_upload_unparsable_metadata_is_422(client, raw):
    resp = _upload(client, meta=raw)
    assert resp.status_code == 422
    assert "metadata" in _fields(resp)


def test_upload_missing_metadata_is_422(client):
    resp = _upload(client, meta=False)
    assert resp.status_code == 422
    assert "metadata" in _fields(resp)


def test_upload_file_and_url_is_422(client):
    resp = _upload(client, url="https://skola.example/")
    assert resp.status_code == 422
    assert _fields(resp) == {"file", "url"}


def test_upload_neither_file_nor_url_is_422(client):
    resp = _upload(client, file=None)
    assert resp.status_code == 422


@pytest.mark.parametrize("url", ["ftp://skola.example/x", "skola.example/x", "https://",
                                 "https://a b.example/"])
def test_upload_bad_url_is_422(client, url):
    resp = _upload(client, file=None, url=url)
    assert resp.status_code == 422
    assert "url" in _fields(resp)


@pytest.mark.parametrize("batch_id", [None, "batch-1"])
def test_upload_bad_batch_id_is_422(client, batch_id):
    resp = _upload(client, batch_id=batch_id)
    assert resp.status_code == 422
    assert "batch_id" in _fields(resp)


@pytest.mark.parametrize("extra,field", [
    ({"max_child_links": "11"}, "max_child_links"),
    ({"max_child_links": "0"}, "max_child_links"),
    ({"max_child_links": "many"}, "max_child_links"),
    ({"save_level_1_child": "yes"}, "save_level_1_child"),
])
def test_upload_bad_url_options_are_422(client, extra, field):
    resp = _upload(client, file=None, url="https://skola.example/", extra=extra)
    assert resp.status_code == 422
    assert field in _fields(resp)


def test_upload_children_with_file_is_422(client):
    resp = _upload(client, extra={"save_level_1_child": "true"})
    assert resp.status_code == 422
    assert "save_level_1_child" in _fields(resp)


def test_upload_without_uploader_is_503(client, search, store):
    def missing(_school):
        raise SearchNotFound("no uploader")
    search.get_uploader = missing
    resp = _upload(client)
    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "PUBLISH_UPLOADER_UNAVAILABLE" and body["retryable"] is False
    assert store.list_ids("jobs") == []


def test_upload_code_lists_outage_is_503(client, search):
    search.raise_unavailable_on = "get_code_lists"
    assert _upload(client).status_code == 503


def test_upload_batch_cap_is_422(client, monkeypatch):
    monkeypatch.setattr(settings, "max_document_batch_items", 2)
    assert _upload(client).status_code == 202
    assert _upload(client).status_code == 202
    resp = _upload(client)
    assert resp.status_code == 422
    assert _fields(resp) == {"batch_id"}
    other = _upload(client, batch_id="11111111-aaaa-bbbb-cccc-000000000002")
    assert other.status_code == 202


def test_upload_school_queue_cap_is_429(client, store, monkeypatch):
    monkeypatch.setattr(settings, "document_queue_school_cap", 2)
    for i in range(2):
        assert _upload(client, batch_id=str(uuid.uuid4())).status_code == 202
    resp = _upload(client, batch_id=str(uuid.uuid4()))
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["code"] == "INGEST_QUEUE_FULL" and err["retryable"] is True
    assert len(store.list_ids("jobs")) == 2
    assert list(store.job_file_path("x").parent.glob("*.part")) == []


def test_upload_without_csrf_is_403(client, store):
    resp = _upload(client, headers={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert store.list_ids("jobs") == []


# ── jobs listing ────────────────────────────────────────────────────────
def test_document_jobs_order_and_batch_filter(client):
    other = "11111111-aaaa-bbbb-cccc-000000000009"
    ids = [_upload(client).json()["job"]["job_id"] for _ in range(3)]
    other_id = _upload(client, batch_id=other).json()["job"]["job_id"]

    all_jobs = client.get(f"{BASE}/document-jobs").json()["jobs"]
    assert [j["job_id"] for j in all_jobs] == [other_id, *reversed(ids)]   # newest first

    batch = client.get(f"{BASE}/document-jobs", params={"batch_id": BATCH.upper()}).json()
    assert [j["job_id"] for j in batch["jobs"]] == ids                     # oldest first

    none = client.get(f"{BASE}/document-jobs", params={"batch_id": "nope"}).json()
    assert none == {"jobs": []}


def test_document_jobs_hide_other_schools(client, store):
    document_queue.enqueue_job(store, job_id=str(uuid.uuid4()), school="ZS_OTHER",
                               batch_id=BATCH, source_kind="url", source="https://x.example/",
                               params={"visibility": "entity", "audience_roles": ["PARENT"]},
                               user_id="u", principal_subject="t", payload_part=None)
    assert client.get(f"{BASE}/document-jobs").json() == {"jobs": []}


# ── deactivate / activate ───────────────────────────────────────────────
def test_deactivate_and_activate(client, search):
    resp = client.post(f"{BASE}/documents/{DOC_UUID}/deactivate", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["document"]["is_active"] is False
    resp = client.post(f"{BASE}/documents/{DOC_UUID}/activate", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["document"]["is_active"] is True
    assert [c[0] for c in search.document_calls] == ["deactivate", "activate"]


@pytest.mark.parametrize("action", ["deactivate", "activate"])
def test_deactivate_unknown_is_404(client, action):
    resp = client.post(f"{BASE}/documents/{uuid.uuid4()}/{action}", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.parametrize("action", ["deactivate", "activate"])
def test_deactivate_outage_is_503(client, search, action):
    search.raise_unavailable_on = f"{action}_document"
    resp = client.post(f"{BASE}/documents/{DOC_UUID}/{action}", headers=HEADERS)
    assert resp.status_code == 503


@pytest.mark.parametrize("action", ["deactivate", "activate"])
def test_deactivate_without_csrf_is_403(client, search, action):
    resp = client.post(f"{BASE}/documents/{DOC_UUID}/{action}")
    assert resp.status_code == 403
    assert search.document_calls == []


# ── delete ──────────────────────────────────────────────────────────────
def _delete(client, file_uuid=DOC_UUID, confirm=DOC_UUID, headers=HEADERS):
    body = None if confirm is None else {"confirm_file_uuid": confirm}
    return client.request("DELETE", f"{BASE}/documents/{file_uuid}", json=body, headers=headers)


def test_delete_document(client, ingest):
    resp = _delete(client, confirm=DOC_UUID.upper())
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"]["versions"] == 2 and body["files_removed"] == 2
    assert ingest.delete_calls == [("ZS_DEMO", DOC_UUID)]


def test_delete_confirmation_mismatch_is_409_without_upstream(client, ingest):
    resp = _delete(client, confirm=str(uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DELETE_CONFIRMATION_MISMATCH"
    resp = _delete(client, confirm="not-a-uuid")
    assert resp.status_code == 409
    assert ingest.delete_calls == []


def test_delete_missing_body_is_422(client, ingest):
    resp = _delete(client, confirm=None)
    assert resp.status_code == 422
    assert ingest.delete_calls == []


def test_delete_non_uuid_path_is_400(client, ingest):
    resp = _delete(client, file_uuid="abc", confirm="abc")
    assert resp.status_code == 400
    assert ingest.delete_calls == []


@pytest.mark.parametrize("fail,status", [("not_found", 404), ("rejected", 400),
                                         ("unavailable", 503)])
def test_delete_gateway_errors(client, ingest, fail, status):
    ingest.delete_fail = fail
    resp = _delete(client)
    assert resp.status_code == status


def test_delete_without_csrf_is_403(client, ingest):
    resp = _delete(client, headers={})
    assert resp.status_code == 403
    assert ingest.delete_calls == []
