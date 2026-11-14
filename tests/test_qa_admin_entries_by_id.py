# written for task qa-portal-documents.
"""Unit tests for GET /internal/qa-admin/entries/by-id (CONTRACT.md sec.14.11);
no real DB, fake cursor as in tests/test_qa_admin_documents.py.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import qa_admin


class FakeCursor:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self._current = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._current = self.script.pop(0) if self.script else {}

    def fetchone(self):
        return (self._current or {}).get("fetchone")


def make_app(cur, monkeypatch):
    monkeypatch.setattr(qa_admin, "_cursor", lambda: cur)
    app = FastAPI()
    app.include_router(qa_admin.internal)
    return TestClient(app)


ENTITY = "ZS_JEDOVNICE"
OTHER = "ZS_KAVCI_HORY"
EID = "cccccccc-0000-0000-0000-000000000001"
URL = "/internal/qa-admin/entries/by-id"


def test_by_id_returns_pack_key_question_and_flag(monkeypatch):
    cur = FakeCursor([{"fetchone": ("jidelna", "obed-cena", "Kolik stoji obed?", True)}])
    resp = make_app(cur, monkeypatch).get(
        URL, params={"legal_entity": ENTITY, "entry_id": EID})
    assert resp.status_code == 200
    assert resp.json() == {"pack_id": "jidelna", "entry_key": "obed-cena",
                           "canonical_q": "Kolik stoji obed?", "is_active": True}
    sql, params = cur.calls[0]
    assert "qa_entries" in sql
    assert "e.legal_entity_code = %(le)s" in sql and "e.entry_id = %(eid)s::uuid" in sql
    assert params == {"eid": EID, "le": ENTITY}
    assert len(cur.calls) == 1


def test_by_id_inactive_entry_is_returned_with_flag(monkeypatch):
    cur = FakeCursor([{"fetchone": ("jidelna", None, "Q?", False)}])
    body = make_app(cur, monkeypatch).get(
        URL, params={"legal_entity": ENTITY, "entry_id": EID}).json()
    assert body["is_active"] is False and body["entry_key"] is None


def test_by_id_foreign_or_unknown_is_404_and_scoped(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    resp = make_app(cur, monkeypatch).get(
        URL, params={"legal_entity": OTHER, "entry_id": EID})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert cur.calls[0][1]["le"] == OTHER


@pytest.mark.parametrize("bad", ["nope", "../../x", "123"])
def test_by_id_bad_uuid_is_400_without_db(monkeypatch, bad):
    cur = FakeCursor()
    resp = make_app(cur, monkeypatch).get(
        URL, params={"legal_entity": ENTITY, "entry_id": bad})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
    assert cur.calls == []


def test_by_id_uuid_is_canonicalised(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    make_app(cur, monkeypatch).get(
        URL, params={"legal_entity": ENTITY, "entry_id": EID.upper().replace("-", "")})
    assert cur.calls[0][1]["eid"] == EID


def test_list_entries_route_is_not_shadowed(monkeypatch):
    # /entries stays its own route (pack_id required -> 422 without it)
    cur = FakeCursor()
    resp = make_app(cur, monkeypatch).get(
        "/internal/qa-admin/entries", params={"legal_entity": ENTITY})
    assert resp.status_code == 422 and cur.calls == []
