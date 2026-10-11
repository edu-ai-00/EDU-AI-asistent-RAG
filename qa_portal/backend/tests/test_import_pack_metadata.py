"""Import into a not-yet-existing pack — confirmed metadata per pack
(CONTRACT.md §2.4c, contract 1.2.0).

FakeSearchClient/FakeIngestClient are in-memory test doubles — no network, no
real ingest or search service is exercised anywhere in this file; every
assertion below is labelled MOCKED evidence.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

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


def _one_row_csv(entry_key="k1", question="Kde je sekretariat?"):
    return csv_io.write_pack_csv(
        [{"entry_key": entry_key, "canonical_q": question, "answer_md": "V prizemi.",
          "cat": "", "variants": [], "sources": [], "is_active": True,
          "domains": ["PROVOZ"], "category_code": "GENERAL"}],
        include_category_column=True)


def _preview_csv(client, pack_id, *, metadata_by_pack=None, mode="merge"):
    data = {"pack_id": pack_id, "mode": mode}
    if metadata_by_pack is not None:
        data["pack_metadata_by_pack"] = json.dumps(metadata_by_pack)
    return client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview", data=data,
        files={"file": (f"{pack_id}.csv", _one_row_csv(), "text/csv")}, headers=HEADERS)


def _build_zip(pack_rows: dict[str, bytes], manifest_packs: list[dict] | None = None) -> bytes:
    manifest = {"legal_entity_code": "ZS_DEMO", "exported_at": "2026-01-01T00:00:00+00:00",
                "portal_version": "test", "contract_version": "test",
                "packs": manifest_packs or []}
    return zip_io.build_export_zip(manifest, pack_rows)


# ── ACCEPTANCE (CSV): new pack, metadata confirmed at preview -> ingest gets it ──

def test_csv_new_pack_publish_sends_confirmed_metadata_to_ingest(client, ingest):
    prev = _preview_csv(client, "newpack", metadata_by_pack={"newpack": VALID_METADATA_A})
    assert prev.status_code == 200, prev.text
    body = prev.json()
    (pack,) = body["packs"]
    assert pack["is_new_pack"] is True
    assert pack["metadata_required"] is False
    assert pack["publishable"] is True
    preview_id = body["preview_id"]

    pub = client.post(
        f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
        json={"expected_etags": {"newpack": ""}}, headers=HEADERS)
    assert pub.status_code == 202, pub.text

    # MOCKED evidence: assert on what the fake ingest actually received.
    assert len(ingest.received) == 1
    received = ingest.received[0]
    assert received["pack_id"] == "newpack"
    assert received["visibility"] == "entity"
    assert received["audience_roles"] == ["TEACHER"]
    assert received["school_subject_grades"] == ["1"]


# ── ACCEPTANCE (ZIP, per-pack keying): two new packs, DIFFERENT metadata ────
# ── each — this is the point of the whole slice ─────────────────────────────

def test_zip_two_new_packs_each_get_their_own_metadata(client, ingest):
    zip_bytes = _build_zip({
        "packa": _one_row_csv(entry_key="ka", question="Otazka A?"),
        "packb": _one_row_csv(entry_key="kb", question="Otazka B?"),
    })
    data = {"mode": "merge",
            "pack_metadata_by_pack": json.dumps(
                {"packa": VALID_METADATA_A, "packb": VALID_METADATA_B})}
    prev = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview", data=data,
        files={"file": ("export.zip", zip_bytes, "application/zip")}, headers=HEADERS)
    assert prev.status_code == 200, prev.text
    body = prev.json()
    by_id = {p["pack_id"]: p for p in body["packs"]}
    assert set(by_id) == {"packa", "packb"}
    for p in by_id.values():
        assert p["is_new_pack"] is True
        assert p["publishable"] is True
    preview_id = body["preview_id"]

    pub = client.post(
        f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
        json={"expected_etags": {"packa": "", "packb": ""}}, headers=HEADERS)
    assert pub.status_code == 202, pub.text

    assert len(ingest.received) == 2
    by_pack = {r["pack_id"]: r for r in ingest.received}
    assert by_pack["packa"]["visibility"] == "entity"
    assert by_pack["packa"]["audience_roles"] == ["TEACHER"]
    assert by_pack["packa"]["school_subject_grades"] == ["1"]
    assert by_pack["packb"]["visibility"] == "organization"
    assert by_pack["packb"]["audience_roles"] == ["PARENT"]
    assert by_pack["packb"]["school_subject_grades"] == ["3"]


# ── FAILURE: new pack, no metadata -> 200 metadata_required, publish fails ──
# ── truthfully and ingest is NEVER called for it ────────────────────────────

def test_csv_new_pack_without_metadata_fails_publish_without_calling_ingest(client, ingest):
    prev = _preview_csv(client, "newpack")
    assert prev.status_code == 200, prev.text
    body = prev.json()
    (pack,) = body["packs"]
    assert pack["is_new_pack"] is True
    assert pack["metadata_required"] is True
    assert pack["publishable"] is False
    preview_id = body["preview_id"]

    pub = client.post(
        f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
        json={"expected_etags": {"newpack": ""}}, headers=HEADERS)
    assert pub.status_code == 202, pub.text
    op = pub.json()
    (entry,) = op["packs"]
    assert entry["pack_id"] == "newpack"
    assert entry["status"] == "failed"
    assert entry["message"] == "PACK_METADATA_REQUIRED"
    assert not ingest.received


# ── metadata for an already-existing pack -> 422 ────────────────────────────

def test_metadata_for_existing_pack_is_422(client, ingest):
    resp = _preview_csv(client, "faq", metadata_by_pack={"faq": VALID_METADATA_A})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata_by_pack.faq" for f in body["error"]["fields"])
    assert not ingest.received


# ── a key for a pack not present in this import -> 422 ──────────────────────

def test_metadata_key_for_pack_not_in_import_is_422(client, ingest):
    resp = _preview_csv(client, "newpack",
                         metadata_by_pack={"someotherpack": VALID_METADATA_A})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata_by_pack.someotherpack"
               for f in body["error"]["fields"])
    assert not ingest.received


# ── unknown role code -> 422, indexed field name, file set ─────────────────

def test_unknown_role_code_is_422_with_file(client):
    bad = dict(VALID_METADATA_A, audience_roles=["NOT_A_ROLE"])
    resp = _preview_csv(client, "newpack", metadata_by_pack={"newpack": bad})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    field_entry = next(f for f in body["error"]["fields"]
                        if f["field"] == "pack_metadata.audience_roles[0]")
    assert field_entry["file"] == "newpack.csv"


# ── malformed JSON body -> 422 VALIDATION_ERROR, field pack_metadata_by_pack ─

def test_malformed_json_pack_metadata_by_pack_is_422(client):
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "newpack", "mode": "merge",
              "pack_metadata_by_pack": "{not valid json"},
        files={"file": ("newpack.csv", _one_row_csv(), "text/csv")}, headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(f["field"] == "pack_metadata_by_pack" for f in body["error"]["fields"])


# ── ZIP manifest metadata is echoed as suggested_metadata but does NOT make ──
# ── the pack publishable on its own ─────────────────────────────────────────

def test_zip_manifest_metadata_is_suggestion_only(client, ingest):
    zip_bytes = _build_zip(
        {"packc": _one_row_csv(entry_key="kc", question="Otazka C?")},
        manifest_packs=[{"pack_id": "packc", "metadata": VALID_METADATA_A}])
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"mode": "merge"},
        files={"file": ("export.zip", zip_bytes, "application/zip")}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (pack,) = body["packs"]
    assert pack["pack_id"] == "packc"
    assert pack["suggested_metadata"] == VALID_METADATA_A
    assert pack["metadata_required"] is True
    assert pack["publishable"] is False
    preview_id = body["preview_id"]

    pub = client.post(
        f"/qa-api/v1/schools/ZS_DEMO/imports/{preview_id}/publish",
        json={"expected_etags": {"packc": ""}}, headers=HEADERS)
    assert pub.status_code == 202, pub.text
    op = pub.json()
    (entry,) = op["packs"]
    assert entry["status"] == "failed"
    assert entry["message"] == "PACK_METADATA_REQUIRED"
    assert not ingest.received
