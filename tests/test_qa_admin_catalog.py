# written for task qa-data-c1a.
"""Unit tests for search/app/qa_admin.py — no real DB, no psycopg2.

A fake cursor (context manager) records executed SQL/params and returns
scripted results; it is monkeypatched over qa_admin._cursor so the router
never touches a real connection or import psycopg2.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import qa_admin


class FakeCursor:
    """Records every execute() call; fetchall/fetchone/rowcount are scripted."""

    def __init__(self, script=None):
        # script: list of dicts, one per expected execute() call, each with
        # optional "fetchall", "fetchone", "rowcount", or "raise".
        self.script = list(script or [])
        self.calls = []  # (sql, params)
        self._current = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._current = self.script.pop(0) if self.script else {}
        if "raise" in self._current:
            raise self._current["raise"]

    def fetchall(self):
        return (self._current or {}).get("fetchall", [])

    def fetchone(self):
        return (self._current or {}).get("fetchone")

    @property
    def rowcount(self):
        return (self._current or {}).get("rowcount", 0)


def make_app(fake_cursor, monkeypatch):
    monkeypatch.setattr(qa_admin, "_cursor", lambda: fake_cursor)
    monkeypatch.setattr(qa_admin, "_default_language", lambda: "cs")
    app = FastAPI()
    app.include_router(qa_admin.internal)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. GET /categories includes inactive by default
# ---------------------------------------------------------------------------

def test_get_categories_includes_inactive_by_default(monkeypatch):
    rows = [
        (1, "DRUZINA", {"cs": "Družina"}, True, 10, None),
        (2, "STARE", {"cs": "Staré"}, False, 20, "vyřazeno"),
    ]
    cur = FakeCursor([{"fetchall": rows}])
    client = make_app(cur, monkeypatch)

    resp = client.get("/internal/qa-admin/categories")
    assert resp.status_code == 200
    codes = [c["code"] for c in resp.json()["categories"]]
    assert "STARE" in codes
    sql = cur.calls[0][0]
    assert "is_active" not in sql.split("ORDER BY")[0].split("legal_entity_code IS NULL")[1]


# ---------------------------------------------------------------------------
# 2. label_fallback semantics
# ---------------------------------------------------------------------------

def test_label_fallback_missing_language(monkeypatch):
    rows = [(1, "DRUZINA", {"cs": "Družina"}, True, 10, None)]
    cur = FakeCursor([{"fetchall": rows}])
    client = make_app(cur, monkeypatch)

    resp = client.get("/internal/qa-admin/categories?lang=en")
    body = resp.json()["categories"][0]
    assert body["label"] == "Družina"
    assert body["label_fallback"] is True


def test_label_fallback_present_language(monkeypatch):
    rows = [(1, "DRUZINA", {"cs": "Družina", "en": "After-school care"}, True, 10, None)]
    cur = FakeCursor([{"fetchall": rows}])
    client = make_app(cur, monkeypatch)

    resp = client.get("/internal/qa-admin/categories?lang=en")
    body = resp.json()["categories"][0]
    assert body["label"] == "After-school care"
    assert body["label_fallback"] is False


# ---------------------------------------------------------------------------
# 3. POST success writes global row
# ---------------------------------------------------------------------------

def test_post_category_success(monkeypatch):
    row = (5, "NOVA", {"cs": "Nová"}, True, 0, None)
    cur = FakeCursor([
        {"fetchall": [("cs",), ("en",)]},  # active language codes
        {"fetchone": row},                 # INSERT ... RETURNING
    ])
    client = make_app(cur, monkeypatch)

    resp = client.post("/internal/qa-admin/categories",
                       json={"code": "NOVA", "labels": {"cs": "Nová"}})
    assert resp.status_code == 201
    assert resp.json()["code"] == "NOVA"

    insert_sql, insert_params = cur.calls[1]
    assert "legal_entity_code" in insert_sql
    assert "'qa_category'" in insert_sql
    assert "NULL" in insert_sql  # legal_entity_code is always NULL (215 CHECK)
    assert "NOVA" in insert_params


# ---------------------------------------------------------------------------
# 4. POST duplicate code -> 409
# ---------------------------------------------------------------------------

class FakeUniqueViolation(Exception):
    pgcode = "23505"


def test_post_category_duplicate_code(monkeypatch):
    cur = FakeCursor([
        {"fetchall": [("cs",)]},
        {"raise": FakeUniqueViolation("duplicate key")},
    ])
    client = make_app(cur, monkeypatch)

    resp = client.post("/internal/qa-admin/categories",
                       json={"code": "DUP", "labels": {"cs": "Duplicitní"}})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CATEGORY_CODE_EXISTS"


# ---------------------------------------------------------------------------
# 5. POST invalid code / unknown language -> 422, no INSERT
# ---------------------------------------------------------------------------

def test_post_category_invalid_code(monkeypatch):
    cur = FakeCursor([])
    client = make_app(cur, monkeypatch)

    resp = client.post("/internal/qa-admin/categories",
                       json={"code": "not-valid", "labels": {"cs": "x"}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert cur.calls == []  # never reached the DB at all


def test_post_category_unknown_language(monkeypatch):
    cur = FakeCursor([{"fetchall": [("cs",)]}])
    client = make_app(cur, monkeypatch)

    resp = client.post("/internal/qa-admin/categories",
                       json={"code": "NOVA2", "labels": {"xx": "???"}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # only the language lookup ran, never the INSERT
    assert len(cur.calls) == 1


# ---------------------------------------------------------------------------
# 6. GET /uploader — entity gate, get-or-create, idempotency
# ---------------------------------------------------------------------------

def test_uploader_unknown_entity(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)

    resp = client.get("/internal/qa-admin/uploader?legal_entity=GHOST")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert len(cur.calls) == 1  # only the entity check, no INSERT


def test_uploader_creates_mapping(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (1,)},   # entity exists and active
        {"rowcount": 1},      # INSERT actually inserted a row
    ])
    client = make_app(cur, monkeypatch)

    resp = client.get("/internal/qa-admin/uploader?legal_entity=ZS_JEDOVNICE")
    body = resp.json()
    assert resp.status_code == 200
    assert body["created"] is True
    assert body["user_id"] == "qa_portal__ZS_JEDOVNICE"


def test_uploader_existing_mapping(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (1,)},   # entity exists and active
        {"rowcount": 0},      # ON CONFLICT DO NOTHING skipped the row
    ])
    client = make_app(cur, monkeypatch)

    resp = client.get("/internal/qa-admin/uploader?legal_entity=ZS_JEDOVNICE")
    body = resp.json()
    assert resp.status_code == 200
    assert body["created"] is False
