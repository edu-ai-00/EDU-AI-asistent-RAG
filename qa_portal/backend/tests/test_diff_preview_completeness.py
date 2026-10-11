"""compute_diff / preview_import completeness (F-C4a-1).

Every key in the pack's current entries must land in exactly one of
added/changed/unchanged/deactivated, and row_count must reflect the parsed
file regardless of validation outcome. FakeSearchClient/FakeIngestClient are
in-memory test doubles — no network, no real ingest or search service; every
assertion below is against mocked evidence only.
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


def _preview_csv(client, pack_id, rows, mode="merge", include_category_column=True):
    csv_bytes = csv_io.write_pack_csv(rows, include_category_column=include_category_column)
    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": pack_id, "mode": mode},
        files={"file": (f"{pack_id}.csv", csv_bytes, "text/csv")}, headers=HEADERS)
    return resp


def _row(entry_key, canonical_q="Kdy zacina rok?", answer_md="V zari.",
         domains=("PROVOZ",), category_code="GENERAL", is_active=True):
    return {"entry_key": entry_key, "canonical_q": canonical_q, "answer_md": answer_md,
            "cat": "", "variants": [], "sources": [], "is_active": is_active,
            "domains": list(domains), "category_code": category_code}


# ── A: re-importing an unmodified export is fully accounted for ───────────

def test_reimport_unmodified_export_is_all_unchanged(client):
    export = client.post("/qa-api/v1/schools/ZS_DEMO/exports",
                          json={"pack_ids": ["faq"], "format": "csv"}, headers=HEADERS)
    assert export.status_code == 201, export.text
    export_id = export.json()["export_id"]
    download = client.get(f"/qa-api/v1/exports/{export_id}/download")
    assert download.status_code == 200, download.text

    resp = client.post(
        "/qa-api/v1/schools/ZS_DEMO/imports/preview",
        data={"pack_id": "faq", "mode": "merge"},
        files={"file": ("faq.csv", download.content, "text/csv")}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    (pack,) = resp.json()["packs"]
    assert pack["ok"] is True
    diff = pack["diff"]
    assert diff["added"] == diff["changed"] == diff["deactivated"] == []
    assert diff["unchanged"] == ["k1"]  # the fixture's only faq entry
    assert pack["row_count"] == 1


# ── B: mixed CSV — completeness across every current key ──────────────────

def test_mixed_csv_every_current_key_in_exactly_one_bucket(client):
    # Seed three extra entries into `faq` via the online-editor preview/publish
    # path so we have edited/identical/omitted material besides the fixture's
    # own "k1".
    def _entry_write(entry_key, answer_md):
        return {"entry_key": entry_key, "canonical_q": "Kdy zacina rok?",
                 "answer_md": answer_md, "variants": [], "sources": [],
                 "domains": ["PROVOZ"], "category_code": "GENERAL", "is_active": True}

    setup = client.post(
        "/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
        json={"base_etag": "etag-1", "entries": [
            _entry_write("k1", "V zari."),  # keep the fixture entry as-is
            _entry_write("e_edit", "Original answer."),
            _entry_write("e_identical", "Stays exactly the same."),
            _entry_write("e_omit", "Will be omitted from the next CSV."),
        ]}, headers=HEADERS)
    assert setup.status_code == 200, setup.text
    preview_id = setup.json()["preview_id"]
    pub = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
                       json={"preview_id": preview_id, "expected_etag": "etag-1"},
                       headers=HEADERS)
    assert pub.status_code == 202, pub.text

    current_keys = {"k1", "e_edit", "e_identical", "e_omit"}

    # The fake ingest re-parses only entry_key/canonical_q/answer_md/category_code
    # from the CSV it "sends upstream" (tests/fake_ingest.py:_entries_from_csv),
    # so after the setup publish the seeded entries' domains are [] regardless
    # of what we asked for above — match that here so "identical" really is.
    resp = _preview_csv(client, "faq", [
        _row("e_edit", answer_md="Edited answer.", domains=()),          # changed
        _row("e_identical", answer_md="Stays exactly the same.", domains=()),  # unchanged
        _row("e_new", answer_md="Brand new row.", domains=()),            # added
        # e_omit and k1 are not mentioned -> unchanged (merge mode)
    ], mode="merge")
    assert resp.status_code == 200, resp.text
    (pack,) = resp.json()["packs"]
    assert pack["ok"] is True
    diff = pack["diff"]
    assert pack["row_count"] == 3

    changed_keys = {e["entry_key"] for e in diff["changed"]}
    deactivated_keys = {e["entry_key"] for e in diff["deactivated"]}
    unchanged_keys = set(diff["unchanged"])
    added_keys = {e["entry_key"] for e in diff["added"]}

    buckets = [changed_keys, deactivated_keys, unchanged_keys]
    for key in current_keys:
        memberships = [key in b for b in buckets]
        assert sum(memberships) == 1, f"{key} must appear in exactly one bucket, got {memberships}"
    assert "e_edit" in changed_keys
    assert "e_identical" in unchanged_keys
    assert "e_omit" in unchanged_keys
    assert "k1" in unchanged_keys
    assert deactivated_keys == set()

    assert added_keys == {"e_new"}
    assert "e_new" not in changed_keys | unchanged_keys | deactivated_keys


# ── C: header-only CSV — row_count distinguishes "empty" from "no-op" ─────

def test_empty_csv_reports_zero_rows_but_full_unchanged_set(client):
    resp = _preview_csv(client, "faq", [], mode="merge")
    assert resp.status_code == 200, resp.text
    (pack,) = resp.json()["packs"]
    assert pack["ok"] is True
    assert pack["row_count"] == 0
    assert pack["diff"]["unchanged"] == ["k1"]
    assert pack["diff"]["added"] == pack["diff"]["changed"] == pack["diff"]["deactivated"] == []


# ── D: mode=replace omission is still reported as deactivated ─────────────

def test_replace_mode_omission_is_deactivated_not_unchanged(client):
    resp = _preview_csv(client, "faq", [
        _row("k1", answer_md="V zari."),  # identical -> unchanged
    ], mode="replace")
    assert resp.status_code == 200, resp.text
    (pack,) = resp.json()["packs"]
    assert pack["ok"] is True
    assert pack["diff"]["unchanged"] == ["k1"]
    assert pack["diff"]["deactivated"] == []

    # Now omit k1 entirely under replace mode: it must be reported deactivated.
    resp2 = _preview_csv(client, "faq", [
        _row("other_key", answer_md="A different entry."),
    ], mode="replace")
    assert resp2.status_code == 200, resp2.text
    (pack2,) = resp2.json()["packs"]
    assert pack2["ok"] is True
    assert pack2["diff"]["unchanged"] == []
    assert [d["entry_key"] for d in pack2["diff"]["deactivated"]] == ["k1"]
    assert [a["entry_key"] for a in pack2["diff"]["added"]] == ["other_key"]
    assert pack2["row_count"] == 1
