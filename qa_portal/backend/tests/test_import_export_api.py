"""Import/export API — CSV/ZIP round trip, per-pack ZIP reporting, merge
non-destructiveness, replace confirmation, validation-before-write, media
type/format rejection, and export-download 404/CSRF (CONTRACT.md §5, §6, §8).

FakeSearchClient/FakeIngestClient are in-memory test doubles — no network, no
real ingest or search service — every assertion below is against mocked
evidence only.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_ingest_client, get_search_client, get_store
from app.main import app
from app.services import csv_io
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


def _create_export(client, pack_ids, fmt="zip"):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/exports",
                        json={"pack_ids": pack_ids, "format": fmt}, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _download(client, export_id):
    resp = client.get(f"/qa-api/v1/exports/{export_id}/download")
    assert resp.status_code == 200, resp.text
    return resp.content, resp.headers["content-type"]


# ── A: CSV export -> download -> feed back as a preview (round trip) ──────

def test_csv_export_download_reimport_is_non_destructive(client, ingest):
    export = _create_export(client, ["faq"], fmt="csv")
    assert export["format"] == "csv"
    content, content_type = _download(client, export["export_id"])
    assert content_type.startswith("text/csv")

    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("faq.csv", content, "text/csv")}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_kind"] == "csv"
    (pack,) = body["packs"]
    assert pack["ok"] is True
    # the existing entry_key survives the round trip — never dropped, never
    # (re)marked as removed or as a brand-new entry by re-importing an
    # unmodified export; an identical re-import reports it as `unchanged`,
    # not silently omitted from every bucket (F-C4a-1).
    assert pack["diff"]["added"] == []
    assert pack["diff"]["changed"] == []
    assert pack["diff"]["deactivated"] == []
    assert pack["diff"]["unchanged"] == ["k1"]
    assert pack["row_count"] == 1
    assert not ingest.received  # preview alone never calls ingest


# ── B: ZIP export of two packs -> preview reports results PER PACK ────────

def test_zip_export_reimport_reports_per_pack(client):
    export = _create_export(client, ["faq", "faq2"], fmt="zip")
    assert export["format"] == "zip"
    content, content_type = _download(client, export["export_id"])
    assert content_type == "application/zip"

    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"mode": "merge"},
        files={"file": ("export.zip", content, "application/zip")}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_kind"] == "zip"
    assert len(body["packs"]) == 2  # per-pack, not one merged blob
    by_id = {p["pack_id"]: p for p in body["packs"]}
    assert set(by_id) == {"faq", "faq2"}
    for pack in by_id.values():
        assert pack["ok"] is True
        assert pack["diff"]["deactivated"] == []


# ── C: merge mode with rows omitted from the CSV must not delete them ─────

def test_merge_mode_omitting_existing_rows_does_not_remove_them(client):
    empty_csv = csv_io.write_pack_csv([], include_category_column=True)
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("faq.csv", empty_csv, "text/csv")}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    (pack,) = resp.json()["packs"]
    assert pack["ok"] is True
    assert pack["diff"]["deactivated"] == []       # nothing was removed/deactivated
    assert pack["diff"]["unchanged"] == ["k1"]      # the untouched entry survives as-is


# ── D: publishing a replace preview without confirm_replace -> 422 ────────

def test_publish_replace_without_confirm_is_rejected(client, ingest):
    csv_bytes = csv_io.write_pack_csv(
        [{"entry_key": "k1", "canonical_q": "Kdy zacina rok?", "answer_md": "V zari.",
          "cat": "", "variants": [], "sources": [], "is_active": True,
          "domains": ["PROVOZ"], "category_code": "GENERAL"}],
        include_category_column=True)
    prev = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "replace"},
        files={"file": ("faq.csv", csv_bytes, "text/csv")}, headers=HEADERS)
    assert prev.status_code == 200, prev.text
    preview_id = prev.json()["preview_id"]

    pub = client.post(
        f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
        json={"expected_etags": {"faq": "etag-1"}, "confirm_replace": False},
        headers=HEADERS)
    assert pub.status_code == 422
    assert pub.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not ingest.received


# ── E: an invalid row (unknown domain/category) -> 422, never a partial write ──

def test_csv_with_unknown_domain_is_rejected_before_any_write(client, ingest):
    bad_csv = csv_io.write_pack_csv(
        [{"entry_key": "k1", "canonical_q": "Kdy zacina rok?", "answer_md": "V zari.",
          "cat": "", "variants": [], "sources": [], "is_active": True,
          "domains": ["NOPE_NOT_A_DOMAIN"], "category_code": "GENERAL"}],
        include_category_column=True)
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("faq.csv", bad_csv, "text/csv")}, headers=HEADERS)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"]
    assert "preview_id" not in body  # no usable preview handed back for publish
    assert not ingest.received


def test_csv_with_unknown_category_is_rejected(client, ingest):
    bad_csv = csv_io.write_pack_csv(
        [{"entry_key": "k1", "canonical_q": "Kdy zacina rok?", "answer_md": "V zari.",
          "cat": "", "variants": [], "sources": [], "is_active": True,
          "domains": ["PROVOZ"], "category_code": "NOT_A_REAL_CATEGORY"}],
        include_category_column=True)
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("faq.csv", bad_csv, "text/csv")}, headers=HEADERS)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["fields"]
    assert not ingest.received


# ── F: unsupported media type, and csv format with more than one pack ─────

def test_non_csv_non_zip_upload_is_415(client):
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("notes.txt", b"just some text", "text/plain")}, headers=HEADERS)
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_csv_format_with_two_packs_is_422(client):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/exports",
                        json={"pack_ids": ["faq", "faq2"], "format": "csv"}, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ── G: unknown export id -> 404 envelope; any write without CSRF -> 403 ───

def test_download_unknown_export_is_404_envelope(client):
    resp = client.get("/qa-api/v1/exports/99999999-9999-9999-9999-999999999999/download")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_import_preview_without_csrf_header_is_forbidden(client, ingest):
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("faq.csv", b"entry_key;canonical_q\n", "text/csv")})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert not ingest.received


def test_create_export_without_csrf_header_is_forbidden(client, store):
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/exports", json={"pack_ids": ["faq"]})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert not store.list_ids("exports")


def test_import_publish_without_csrf_header_is_forbidden(client, ingest):
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/11111111-1111-1111-1111-111111111111/publish",
        json={"expected_etags": {"faq": "etag-1"}})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert not ingest.received
