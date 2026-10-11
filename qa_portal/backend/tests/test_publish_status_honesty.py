"""Regression coverage for the three C5 review findings fixed in
publish_service.py (CONTRACT.md §6 — the two new bullets — and §7's
IMPORT_PACKS_FAILED):

1. `run_import_publish` must derive the top-level operation status from
   `packs[]` instead of always claiming "completed".
2. A per-pack lock acquired in `begin_import_publish` must be released if an
   unexpected exception happens before it is added to `locks{}`.
3. An existing pack's metadata must never be defaulted at publish time; a
   pack state with no `visibility`/`audience_roles` is refused, not guessed.

FakeSearchClient/FakeIngestClient are in-memory test doubles — no network, no
real ingest or search service is exercised anywhere in this file; every
assertion below is labelled MOCKED evidence.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.adapters.ingest import IngestRejected
from app.api.deps import get_ingest_client, get_search_client, get_store
from app.main import app
from app.services import csv_io, zip_io
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import FakeSearchClient

HEADERS = {"X-QA-Portal": "1"}

VALID_METADATA_A = {
    "visibility": "entity",
    "audience_roles": ["TEACHER"],
    "school_grade_codes": ["1"],
}
VALID_METADATA_B = {
    "visibility": "organization",
    "audience_roles": ["PARENT"],
    "school_grade_codes": ["3"],
}


class PackFailingIngestClient(FakeIngestClient):
    """Same as FakeIngestClient, but rejects ingest for a chosen subset of
    pack_ids — lets a single test exercise a MIXED (one ok, one failed)
    import without a second real ingest backend.
    """

    def __init__(self, search, fail_pack_ids):
        super().__init__(search)
        self.fail_pack_ids = set(fail_pack_ids)

    def run_ingest(self, *, pack_id, **kwargs):
        if pack_id in self.fail_pack_ids:
            self.received.append({"pack_id": pack_id, **kwargs})
            raise IngestRejected(422, {"error": {
                "code": "VALIDATION_ERROR", "message": "fake rejection for this pack",
                "retryable": False}})
        return super().run_ingest(pack_id=pack_id, **kwargs)


@pytest.fixture
def search():
    return FakeSearchClient()


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "qa_admin_state")


def _make_client(search, ingest, store):
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


def _one_row_csv(entry_key="k1", question="Kde je sekretariat?"):
    return csv_io.write_pack_csv(
        [{"entry_key": entry_key, "canonical_q": question, "answer_md": "V prizemi.",
          "cat": "", "variants": [], "sources": [], "is_active": True,
          "domains": ["PROVOZ"], "category_code": "GENERAL"}],
        include_category_column=True)


def _build_zip(pack_rows: dict[str, bytes]) -> bytes:
    manifest = {"legal_entity_code": "ZS_DEMO", "exported_at": "2026-01-01T00:00:00+00:00",
                "portal_version": "test", "contract_version": "test", "packs": []}
    return zip_io.build_export_zip(manifest, pack_rows)


def _preview_two_new_packs(client, metadata_by_pack):
    zip_bytes = _build_zip({
        "packa": _one_row_csv(entry_key="ka", question="Otazka A?"),
        "packb": _one_row_csv(entry_key="kb", question="Otazka B?"),
    })
    data = {"mode": "merge", "pack_metadata_by_pack": json.dumps(metadata_by_pack)}
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview", data=data,
        files={"file": ("export.zip", zip_bytes, "application/zip")}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── FINDING 1, ACCEPTANCE: both packs publish fine -> completed, no error ──

def test_import_both_packs_ok_operation_is_completed(search, store):
    ingest = FakeIngestClient(search)
    client = _make_client(search, ingest, store)
    try:
        body = _preview_two_new_packs(
            client, {"packa": VALID_METADATA_A, "packb": VALID_METADATA_B})
        preview_id = body["preview_id"]

        pub = client.post(
            f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
            json={"expected_etags": {"packa": "", "packb": ""}}, headers=HEADERS)
        assert pub.status_code == 202, pub.text
        operation_id = pub.json()["operation_id"]

        # TestClient waits for the BackgroundTask before returning the 202,
        # but JSONResponse renders its body from the pre-background snapshot
        # ("accepted") — the final status is only visible via a fresh GET.
        final = client.get(f"/qa-api/v1/operations/{operation_id}")
        assert final.status_code == 200, final.text
        op = final.json()
        assert op["status"] == "completed"
        assert op["error"] is None
        statuses = {p["pack_id"]: p["status"] for p in op["packs"]}
        assert statuses == {"packa": "ok", "packb": "ok"}
    finally:
        app.dependency_overrides.clear()


# ── FINDING 1, FAILURE: both packs fail ingest -> failed/IMPORT_PACKS_FAILED ─

def test_import_both_packs_failed_operation_is_failed(search, store):
    ingest = FakeIngestClient(search, fail="rejected")
    client = _make_client(search, ingest, store)
    try:
        body = _preview_two_new_packs(
            client, {"packa": VALID_METADATA_A, "packb": VALID_METADATA_B})
        preview_id = body["preview_id"]

        pub = client.post(
            f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
            json={"expected_etags": {"packa": "", "packb": ""}}, headers=HEADERS)
        assert pub.status_code == 202, pub.text
        operation_id = pub.json()["operation_id"]

        final = client.get(f"/qa-api/v1/operations/{operation_id}")
        assert final.status_code == 200, final.text
        op = final.json()
        assert op["status"] == "failed"
        assert op["error"]["code"] == "IMPORT_PACKS_FAILED"
        statuses = {p["pack_id"]: p["status"] for p in op["packs"]}
        assert statuses == {"packa": "failed", "packb": "failed"}
    finally:
        app.dependency_overrides.clear()


# ── FINDING 1, MIXED: one pack ok, one pack fails -> still failed overall ──

def test_import_mixed_result_operation_is_failed(search, store):
    ingest = PackFailingIngestClient(search, fail_pack_ids={"packb"})
    client = _make_client(search, ingest, store)
    try:
        body = _preview_two_new_packs(
            client, {"packa": VALID_METADATA_A, "packb": VALID_METADATA_B})
        preview_id = body["preview_id"]

        pub = client.post(
            f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
            json={"expected_etags": {"packa": "", "packb": ""}}, headers=HEADERS)
        assert pub.status_code == 202, pub.text
        operation_id = pub.json()["operation_id"]

        final = client.get(f"/qa-api/v1/operations/{operation_id}")
        assert final.status_code == 200, final.text
        op = final.json()
        assert op["status"] == "failed"
        assert op["error"]["code"] == "IMPORT_PACKS_FAILED"
        assert "1 of 2" in op["error"]["message"]
        statuses = {p["pack_id"]: p["status"] for p in op["packs"]}
        assert statuses == {"packa": "ok", "packb": "failed"}
    finally:
        app.dependency_overrides.clear()


# ── FINDING 3: existing pack with no confirmed visibility/audience_roles ───
# ── -> single-pack publish is refused, ingest never called ────────────────

def test_existing_pack_without_confirmed_metadata_refuses_single_publish(search, store):
    ingest = FakeIngestClient(search)
    client = _make_client(search, ingest, store)
    try:
        search._packs["ZS_DEMO"]["faq"]["metadata"] = {
            "visibility": None, "audience_roles": [],
        }
        prev = client.post(
            "/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
            json={"base_etag": "etag-1", "entries": [{
                "entry_key": "k1", "canonical_q": "Kdy zacina rok?",
                "answer_md": "V zari, uz zase.", "variants": [], "sources": [],
                "domains": ["PROVOZ"], "category_code": "GENERAL", "is_active": True,
            }]}, headers=HEADERS)
        assert prev.status_code == 200, prev.text
        preview_id = prev.json()["preview_id"]

        pub = client.post(
            "/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
            json={"preview_id": preview_id, "expected_etag": "etag-1"}, headers=HEADERS)
        assert pub.status_code == 422, pub.text
        body = pub.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert any(f["field"] == "pack_metadata" for f in body["error"]["fields"])
        assert not ingest.received

        # ── FINDING 2/regression: the lock from the refused attempt above ──
        # must already be released — a second, correctly-confirmed attempt
        # must not answer PUBLISH_IN_PROGRESS.
        pub2 = client.post(
            "/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
            json={"preview_id": preview_id, "expected_etag": "etag-1"}, headers=HEADERS)
        assert pub2.status_code != 409
        assert pub2.json().get("error", {}).get("code") != "PUBLISH_IN_PROGRESS"
    finally:
        app.dependency_overrides.clear()


# ── FINDING 3 inside an import: same pack -> failed/PACK_METADATA_REQUIRED ──

def test_existing_pack_without_confirmed_metadata_fails_inside_import(search, store):
    ingest = FakeIngestClient(search)
    client = _make_client(search, ingest, store)
    try:
        search._packs["ZS_DEMO"]["faq"]["metadata"] = {
            "visibility": "entity", "audience_roles": [],
        }
        resp = client.post(
            "/qa-api/v1/schools/ZS_DEMO/imports/preview",
            data={"pack_id": "faq", "mode": "merge"},
            files={"file": ("faq.csv", _one_row_csv(), "text/csv")}, headers=HEADERS)
        assert resp.status_code == 200, resp.text
        preview_id = resp.json()["preview_id"]

        pub = client.post(
            f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
            json={"expected_etags": {"faq": "etag-1"}}, headers=HEADERS)
        assert pub.status_code == 202, pub.text
        operation_id = pub.json()["operation_id"]

        final = client.get(f"/qa-api/v1/operations/{operation_id}")
        assert final.status_code == 200, final.text
        op = final.json()
        (entry,) = op["packs"]
        assert entry["status"] == "failed"
        assert entry["message"] == "PACK_METADATA_REQUIRED"
        assert op["status"] == "failed"
        assert op["error"]["code"] == "IMPORT_PACKS_FAILED"
        assert not ingest.received
    finally:
        app.dependency_overrides.clear()
