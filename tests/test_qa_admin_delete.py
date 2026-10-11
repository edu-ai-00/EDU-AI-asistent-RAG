# written for task qa-data-delete.
"""Unit tests for search/app/qa_admin.py::delete_entry / delete_pack — no real
DB. Same fake-cursor pattern as tests/test_qa_admin_packs.py, extended with
commit/rollback tracking so the transaction rule (CONTRACT.md sec.13.2) is
actually checkable: a mid-way exception must roll back, never commit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import qa_admin


class FakeCursor:
    """Records every execute() call; fetchall/fetchone/rowcount are scripted.

    Also mimics db.py's get_cursor() contextmanager: commit() on a clean exit,
    rollback() when the `with` block raises — so tests can assert on it.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self._current = None
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
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


ENTITY = "ZS_JEDOVNICE"
OTHER_ENTITY = "ZS_KAVCI_HORY"
PACK = "faq_provoz"
ENTRY_KEY = "provoz-01"
ENTRY_ID = "11111111-1111-1111-1111-111111111111"


def _url(path, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/internal/qa-admin/{path}?{query}"


# ---------------------------------------------------------------------------
# 1. Single-entry delete: acceptance
# ---------------------------------------------------------------------------

def test_delete_entry_success_counts_and_scopes(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (ENTRY_ID,)},   # lookup
        {"fetchone": (3,)},          # variant count
        {"rowcount": 2},             # embedding delete
        {"rowcount": 1},             # entry delete
    ])
    client = make_app(cur, monkeypatch)

    resp = client.delete(_url("entries", legal_entity=ENTITY, pack_id=PACK,
                               entry_key=ENTRY_KEY))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": {
        "entries": 1, "variants": 3, "embeddings": 2, "manifests": 0}}

    lookup_sql, lookup_params = cur.calls[0]
    assert "legal_entity_code = %s" in lookup_sql
    assert lookup_params == (ENTITY, PACK, ENTRY_KEY)

    embedding_sql, embedding_params = cur.calls[2]
    assert "DELETE FROM" in embedding_sql and "qa_embedding" in embedding_sql
    assert embedding_params == (ENTRY_ID, ENTITY)

    entry_sql, entry_params = cur.calls[3]
    assert "DELETE FROM" in entry_sql and "qa_entries" in entry_sql
    assert "legal_entity_code = %s" in entry_sql
    assert entry_params == (ENTRY_ID, ENTITY)

    assert cur.committed is True
    assert cur.rolled_back is False


# ---------------------------------------------------------------------------
# 2. Foreign entity's entry_key -> 404, not a single delete issued
# ---------------------------------------------------------------------------

def test_delete_entry_wrong_entity_is_not_found(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)

    resp = client.delete(_url("entries", legal_entity=OTHER_ENTITY, pack_id=PACK,
                               entry_key=ENTRY_KEY))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert len(cur.calls) == 1
    assert not any("DELETE" in sql for sql, _ in cur.calls)


# ---------------------------------------------------------------------------
# 3. Whole-pack delete removes manifest rows too, statements ordered
#    child-before-parent so nothing is orphaned
# ---------------------------------------------------------------------------

def test_delete_pack_success_removes_manifest(monkeypatch):
    entry_ids = [ENTRY_ID, "22222222-2222-2222-2222-222222222222"]
    cur = FakeCursor([
        {"fetchone": (1,)},                          # manifest exists
        {"fetchall": [(e,) for e in entry_ids]},      # entries of the pack
        {"fetchone": (5,)},                           # variant count
        {"rowcount": 4},                              # embedding delete
        {"rowcount": 2},                              # entries delete
        {"rowcount": 3},                              # manifest delete (incl. inactive)
    ])
    client = make_app(cur, monkeypatch)

    resp = client.delete(_url("packs", legal_entity=ENTITY, pack_id=PACK))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": {
        "entries": 2, "variants": 5, "embeddings": 4, "manifests": 3}}

    sqls = [sql for sql, _ in cur.calls]
    embedding_idx = next(i for i, s in enumerate(sqls) if "qa_embedding" in s)
    entries_idx = next(i for i, s in enumerate(sqls) if "DELETE" in s and "qa_entries" in s)
    manifest_idx = next(i for i, s in enumerate(sqls) if "DELETE" in s and "qa_pack_manifest" in s)
    assert embedding_idx < entries_idx < manifest_idx

    for sql, params in cur.calls:
        if "legal_entity_code = %s" in sql:
            assert params is not None and ENTITY in params

    assert cur.committed is True


# ---------------------------------------------------------------------------
# 4. Foreign entity's pack -> 404, nothing deleted
# ---------------------------------------------------------------------------

def test_delete_pack_wrong_entity_is_not_found(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)

    resp = client.delete(_url("packs", legal_entity=OTHER_ENTITY, pack_id=PACK))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert len(cur.calls) == 1
    assert not any("DELETE" in sql for sql, _ in cur.calls)


# ---------------------------------------------------------------------------
# 5. Mid-transaction failure rolls back: nothing committed
# ---------------------------------------------------------------------------

def test_delete_entry_exception_midway_rolls_back(monkeypatch):
    class Boom(Exception):
        pass

    cur = FakeCursor([
        {"fetchone": (ENTRY_ID,)},   # lookup ok
        {"fetchone": (3,)},          # variant count ok
        {"raise": Boom("db exploded")},  # embedding delete fails
    ])
    client = make_app(cur, monkeypatch)

    with __import__("pytest").raises(Boom):
        client.delete(_url("entries", legal_entity=ENTITY, pack_id=PACK,
                            entry_key=ENTRY_KEY))

    assert cur.rolled_back is True
    assert cur.committed is False
    # the entry delete (4th statement) never ran
    assert len(cur.calls) == 3


def test_delete_pack_exception_midway_rolls_back(monkeypatch):
    class Boom(Exception):
        pass

    cur = FakeCursor([
        {"fetchone": (1,)},                       # manifest exists
        {"fetchall": [(ENTRY_ID,)]},               # entries
        {"fetchone": (1,)},                        # variant count
        {"rowcount": 1},                           # embedding delete ok
        {"raise": Boom("db exploded")},             # entries delete fails
    ])
    client = make_app(cur, monkeypatch)

    with __import__("pytest").raises(Boom):
        client.delete(_url("packs", legal_entity=ENTITY, pack_id=PACK))

    assert cur.rolled_back is True
    assert cur.committed is False
    # the manifest delete never ran
    assert not any("qa_pack_manifest" in sql and "DELETE" in sql for sql, _ in cur.calls)


# ---------------------------------------------------------------------------
# 6. question_answer_log and embedding_registry are never touched
# ---------------------------------------------------------------------------

def test_delete_entry_never_touches_log_or_registry(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (ENTRY_ID,)},
        {"fetchone": (0,)},
        {"rowcount": 0},
        {"rowcount": 1},
    ])
    client = make_app(cur, monkeypatch)
    client.delete(_url("entries", legal_entity=ENTITY, pack_id=PACK,
                        entry_key=ENTRY_KEY))
    for sql, _ in cur.calls:
        assert "question_answer_log" not in sql
        assert "embedding_registry" not in sql


def test_delete_pack_never_touches_log_or_registry(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (1,)},
        {"fetchall": [(ENTRY_ID,)]},
        {"fetchone": (0,)},
        {"rowcount": 0},
        {"rowcount": 1},
        {"rowcount": 1},
    ])
    client = make_app(cur, monkeypatch)
    client.delete(_url("packs", legal_entity=ENTITY, pack_id=PACK))
    for sql, _ in cur.calls:
        assert "question_answer_log" not in sql
        assert "embedding_registry" not in sql


# ---------------------------------------------------------------------------
# 7. Empty pack (manifest exists, no entries left): manifest still removed,
#    no variant/embedding queries fired for an empty id list
# ---------------------------------------------------------------------------

def test_delete_pack_with_no_entries_still_removes_manifest(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (1,)},        # manifest exists
        {"fetchall": []},          # no entries at all
        {"rowcount": 0},           # entries delete (no-op)
        {"rowcount": 2},           # manifest delete
    ])
    client = make_app(cur, monkeypatch)
    resp = client.delete(_url("packs", legal_entity=ENTITY, pack_id=PACK))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": {
        "entries": 0, "variants": 0, "embeddings": 0, "manifests": 2}}
    assert len(cur.calls) == 4  # no variant-count / embedding-delete round trip
