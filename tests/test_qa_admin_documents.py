# written for task qa-portal-documents.
"""Unit tests for the /internal/qa-admin/documents* routes of
search/app/qa_admin.py (CONTRACT.md sec.14.2) — no real DB. Same fake-cursor
pattern as tests/test_qa_admin_delete.py: scripted results, recorded SQL,
commit/rollback tracking for the one-transaction rule.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import qa_admin


class FakeCursor:
    """Records every execute(); fetchall/fetchone/rowcount are scripted.
    commit on a clean `with` exit, rollback when the block raises."""

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
    app = FastAPI()
    app.include_router(qa_admin.internal)
    return TestClient(app)


ENTITY = "ZS_JEDOVNICE"
OTHER_ENTITY = "ZS_KAVCI_HORY"
FU_NEW = "aaaaaaaa-0000-0000-0000-000000000002"
FU_OLD = "aaaaaaaa-0000-0000-0000-000000000001"
KEY = "url:https://zs.example/rad"
BASE = "/internal/qa-admin/documents"
HASH = "a" * 64


def summary_row(file_uuid=FU_NEW, is_active=True, version_count=2, chunk_count=7,
                source_url="https://zs.example/rad", original_file_name=None,
                validity_state="current", file_format="web", content_sha256=HASH):
    return (file_uuid, source_url, original_file_name, "SCHOOL_RULES", "cs",
            None, ["G1", "G2"], ["PROVOZ"], ["STUDENT"], "entity",
            date(2026, 9, 1), None, "qa_portal__ZS_JEDOVNICE",
            datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc), is_active,
            version_count, chunk_count, validity_state, file_format, content_sha256)


def assert_scoped(cur, entity=ENTITY):
    """Every statement filters on legal_entity_code with this school's code."""
    for sql, params in cur.calls:
        assert "legal_entity_code = %(le)s" in sql, sql
        assert params["le"] == entity


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_default_status_active_and_scoped(monkeypatch):
    cur = FakeCursor([{"fetchall": [summary_row()]}])
    client = make_app(cur, monkeypatch)

    resp = client.get(BASE, params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    doc = body["documents"][0]
    assert doc == {
        "file_uuid": FU_NEW,
        "title": "https://zs.example/rad",
        "source_kind": "web",
        "source_url": "https://zs.example/rad",
        "original_file_name": None,
        "document_type": "SCHOOL_RULES",
        "document_language": "cs",
        "school_subject": None,
        "school_grade_codes": ["G1", "G2"],
        "domain_codes": ["PROVOZ"],
        "audience_roles": ["STUDENT"],
        "visibility": "entity",
        "valid_from": "2026-09-01",
        "valid_to": None,
        "user_name": "qa_portal__ZS_JEDOVNICE",
        "ingested_at": "2026-11-01T10:00:00+00:00",
        "is_active": True,
        "version_count": 2,
        "chunk_count": 7,
        "validity_state": "current",
        "file_format": "web",
        "content_sha256": HASH,
    }

    sql, params = cur.calls[0]
    assert "AND d.doc_active" in sql and "NOT d.doc_active" not in sql
    assert "shown_rank = 1" in sql
    # document tables are filtered by the school; code-id lookups are global
    assert "m.legal_entity_code = %(le)s" in sql
    assert "c.legal_entity_code = %(le)s" in sql
    assert "cl.legal_entity_code" not in sql and "tr.legal_entity_code" not in sql
    assert params["le"] == ENTITY
    assert params["lim"] == 51 and params["off"] == 0
    assert cur.committed is True


@pytest.mark.parametrize("status,frag", [
    ("inactive", "AND NOT d.doc_active"),
    ("all", None),
])
def test_list_status_param(monkeypatch, status, frag):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    resp = client.get(BASE, params={"legal_entity": ENTITY, "status": status})
    assert resp.status_code == 200
    sql = cur.calls[0][0]
    if frag:
        assert frag in sql
    else:
        assert "AND d.doc_active" not in sql and "NOT d.doc_active" not in sql


def test_list_invalid_status_and_limit_are_422_without_db(monkeypatch):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    for params in ({"status": "deleted"}, {"limit": 0}, {"limit": 201},
                   {"cursor": "abc"}, {"validity": "stale"}):
        resp = client.get(BASE, params={"legal_entity": ENTITY, **params})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert cur.calls == []


def test_list_q_is_escaped_substring_and_pagination(monkeypatch):
    rows = [summary_row(file_uuid=f"aaaaaaaa-0000-0000-0000-00000000000{i}")
            for i in range(3)]
    cur = FakeCursor([{"fetchall": rows}])
    client = make_app(cur, monkeypatch)
    resp = client.get(BASE, params={"legal_entity": ENTITY, "q": " 50%_x ",
                                    "limit": 2, "cursor": "4"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["documents"]) == 2
    assert body["next_cursor"] == "6"
    sql, params = cur.calls[0]
    assert "ILIKE %(q)s" in sql
    assert params["q"] == "%50\\%\\_x%"
    assert params["lim"] == 3 and params["off"] == 4


def test_list_file_document_title_falls_back(monkeypatch):
    cur = FakeCursor([{"fetchall": [
        summary_row(source_url=None, original_file_name="rad.pdf"),
        summary_row(source_url=None, original_file_name=None)]}])
    client = make_app(cur, monkeypatch)
    docs = client.get(BASE, params={"legal_entity": ENTITY}).json()["documents"]
    assert docs[0]["title"] == "rad.pdf" and docs[0]["source_kind"] == "file"
    assert docs[1]["title"] == FU_NEW


@pytest.mark.parametrize("state", ["current", "expired", "future"])
def test_list_returns_validity_state(monkeypatch, state):
    cur = FakeCursor([{"fetchall": [summary_row(validity_state=state)]}])
    client = make_app(cur, monkeypatch)
    doc = client.get(BASE, params={"legal_entity": ENTITY}).json()["documents"][0]
    assert doc["validity_state"] == state


def test_summary_sql_computes_validity_against_current_date(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    client.get(BASE, params={"legal_entity": ENTITY})
    sql = cur.calls[0][0]
    assert "d.valid_from > CURRENT_DATE THEN 'future'" in sql
    assert "d.valid_to < CURRENT_DATE THEN 'expired' ELSE 'current'" in sql


def test_list_without_new_filters_adds_no_clauses(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    client.get(BASE, params={"legal_entity": ENTITY})
    sql, params = cur.calls[0]
    assert "= ANY(d.audience_roles)" not in sql
    assert "%(type)s" not in sql and "%(source)s" not in sql
    assert "= %(validity)s" not in sql
    assert not {"type", "role", "validity", "source"} & set(params)


def test_list_filters_are_parameterized_and_scoped(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    evil = "x'; DROP TABLE document_metadata; --"
    resp = client.get(BASE, params={
        "legal_entity": ENTITY, "type": evil, "role": " TEACHER ",
        "validity": "expired", "source": "https://zs.example/rad"})
    assert resp.status_code == 200
    sql, params = cur.calls[0]
    assert "COALESCE(d.document_type_user, d.document_type_llm) = %(type)s" in sql
    assert "%(role)s = ANY(d.audience_roles)" in sql
    assert "= %(validity)s" in sql
    assert "d.source_url = %(source)s OR d.original_file_name = %(source)s" in sql
    assert "DROP TABLE" not in sql
    assert params["type"] == evil
    assert params["role"] == "TEACHER"
    assert params["validity"] == "expired"
    assert params["source"] == "https://zs.example/rad"
    # the shown version is filtered, and the school filter stays
    assert "d.shown_rank = 1" in sql
    assert "m.legal_entity_code = %(le)s" in sql
    assert params["le"] == ENTITY


def test_list_validity_all_and_blank_filters_are_ignored(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    resp = client.get(BASE, params={"legal_entity": ENTITY, "validity": "all",
                                    "type": "  ", "role": "", "source": ""})
    assert resp.status_code == 200
    sql, params = cur.calls[0]
    assert "= %(validity)s" not in sql and "%(type)s" not in sql
    assert "ANY(d.audience_roles)" not in sql and "%(source)s" not in sql


def test_list_filters_combine_with_status_and_q(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    client.get(BASE, params={"legal_entity": ENTITY, "status": "inactive",
                             "q": "rad", "type": "SCHOOL_RULES",
                             "validity": "future"})
    sql, params = cur.calls[0]
    for frag in ("AND NOT d.doc_active", "ILIKE %(q)s", "%(type)s", "%(validity)s"):
        assert frag in sql
    assert params["off"] == 0 and params["lim"] == 51


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------

def test_detail_returns_document_and_versions(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (KEY,)},
        {"fetchone": summary_row()},
        {"fetchall": [
            (FU_NEW, "bbbbbbbb-0000-0000-0000-000000000002",
             datetime(2026, 11, 1, tzinfo=timezone.utc), True, 7, f"{FU_NEW}_rad.pdf", "pdf", HASH),
            (FU_OLD, "bbbbbbbb-0000-0000-0000-000000000001",
             datetime(2026, 10, 1, tzinfo=timezone.utc), False, 5, f"{FU_OLD}_rad.pdf", "pdf", None)]},
    ])
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/{FU_OLD}", params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["file_uuid"] == FU_NEW
    assert [v["file_uuid"] for v in body["versions"]] == [FU_NEW, FU_OLD]
    assert body["versions"][1] == {
        "file_uuid": FU_OLD,
        "processing_uuid": "bbbbbbbb-0000-0000-0000-000000000001",
        "ingested_at": "2026-10-01T00:00:00+00:00",
        "is_active": False, "chunk_count": 5,
        "file_name": f"{FU_OLD}_rad.pdf", "file_format": "pdf",
        "content_sha256": None}
    assert body["versions"][0]["content_sha256"] == HASH
    assert cur.calls[0][1]["fu"] == FU_OLD
    assert cur.calls[2][1]["key"] == KEY
    assert_scoped(cur)


def test_detail_foreign_school_is_404(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/{FU_NEW}", params={"legal_entity": OTHER_ENTITY})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert len(cur.calls) == 1
    assert_scoped(cur, OTHER_ENTITY)


@pytest.mark.parametrize("method,path,params", [
    ("get", f"{BASE}/not-a-uuid", {"legal_entity": ENTITY}),
    ("post", f"{BASE}/not-a-uuid/deactivate", {"legal_entity": ENTITY}),
    ("post", f"{BASE}/not-a-uuid/activate", {"legal_entity": ENTITY}),
    ("delete", BASE, {"legal_entity": ENTITY, "file_uuid": "../../etc"}),
    ("get", f"{BASE}/not-a-uuid/chunks", {"legal_entity": ENTITY}),
    ("patch", f"{BASE}/not-a-uuid/content-hash", {"legal_entity": ENTITY}),
])
def test_invalid_uuid_is_400_without_db(monkeypatch, method, path, params):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    resp = getattr(client, method)(path, params=params)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
    assert cur.calls == []


def test_uuid_is_canonicalised(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)
    client.get(f"{BASE}/{FU_NEW.upper().replace('-', '')}",
               params={"legal_entity": ENTITY})
    assert cur.calls[0][1]["fu"] == FU_NEW


# ---------------------------------------------------------------------------
# deactivate / activate
# ---------------------------------------------------------------------------

def test_deactivate_updates_only_active_rows_of_the_document(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (KEY,)},
        {"rowcount": 1},
        {"fetchone": summary_row(is_active=False)},
    ])
    client = make_app(cur, monkeypatch)
    resp = client.post(f"{BASE}/{FU_NEW}/deactivate", params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json()["document"]["is_active"] is False
    sql, params = cur.calls[1]
    assert sql.startswith("UPDATE") and "SET is_active = false" in sql
    assert "AND m.is_active" in sql
    assert params["key"] == KEY
    assert_scoped(cur)
    assert cur.committed is True


def test_deactivate_is_idempotent(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (KEY,)},
        {"rowcount": 0},             # nothing was active
        {"fetchone": summary_row(is_active=False)},
    ])
    client = make_app(cur, monkeypatch)
    resp = client.post(f"{BASE}/{FU_NEW}/deactivate", params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json()["document"]["is_active"] is False


def test_deactivate_foreign_school_is_404_no_update(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)
    resp = client.post(f"{BASE}/{FU_NEW}/deactivate",
                       params={"legal_entity": OTHER_ENTITY})
    assert resp.status_code == 404
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.calls)


def test_activate_targets_newest_version(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (KEY,)},
        {"fetchone": (FU_NEW,)},     # newest version of the document
        {"rowcount": 2},
        {"fetchone": summary_row()},
    ])
    client = make_app(cur, monkeypatch)
    # addressed by the OLD version: still activates the newest
    resp = client.post(f"{BASE}/{FU_OLD}/activate", params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json()["document"]["file_uuid"] == FU_NEW

    newest_sql = cur.calls[1][0]
    assert "ORDER BY m.ldts_gmt DESC, m.file_uuid DESC LIMIT 1" in newest_sql
    upd_sql, upd_params = cur.calls[2]
    assert "SET is_active = (m.file_uuid = %(newest)s::uuid)" in upd_sql
    assert "IS DISTINCT FROM" in upd_sql
    assert upd_params["newest"] == FU_NEW
    assert upd_params["key"] == KEY
    assert_scoped(cur)


def test_activate_is_idempotent(monkeypatch):
    cur = FakeCursor([
        {"fetchone": (KEY,)},
        {"fetchone": (FU_NEW,)},
        {"rowcount": 0},             # already in the target state
        {"fetchone": summary_row()},
    ])
    client = make_app(cur, monkeypatch)
    resp = client.post(f"{BASE}/{FU_NEW}/activate", params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json()["document"]["is_active"] is True


# ---------------------------------------------------------------------------
# edit validity
# ---------------------------------------------------------------------------

VALIDITY_TABLES = ["document_chunks", "document_elements", "document_tables",
                   "document_metadata"]


def validity_script():
    return [
        {"fetchone": (KEY,)},
        {"fetchall": [(FU_NEW,), (FU_OLD,)]},
        {"rowcount": 12}, {"rowcount": 40}, {"rowcount": 3}, {"rowcount": 2},
        {"fetchone": summary_row()},
    ]


def test_validity_updates_every_version_in_all_four_tables(monkeypatch):
    cur = FakeCursor(validity_script())
    client = make_app(cur, monkeypatch)
    # addressed by the OLD version: the whole document is changed
    resp = client.patch(f"{BASE}/{FU_OLD}/validity", params={"legal_entity": ENTITY},
                        json={"valid_from": "2026-09-01", "valid_to": "2027-06-30"})
    assert resp.status_code == 200
    assert resp.json()["document"]["file_uuid"] == FU_NEW
    assert resp.json()["document"]["validity_state"] == "current"

    updates = [(sql, p) for sql, p in cur.calls if sql.startswith("UPDATE")]
    assert [sql.split()[1].split(".")[-1] for sql, _ in updates] == VALIDITY_TABLES
    for sql, params in updates:
        assert "SET valid_from = %(vf)s, valid_to = %(vt)s" in sql
        assert "file_uuid = ANY(%(fus)s::uuid[])" in sql
        assert sorted(params["fus"]) == sorted([FU_NEW, FU_OLD])
        assert params["vf"] == date(2026, 9, 1)
        assert params["vt"] == date(2027, 6, 30)
    assert cur.calls[0][1]["fu"] == FU_OLD
    assert_scoped(cur)
    assert cur.committed is True


def test_validity_null_clears_dates(monkeypatch):
    cur = FakeCursor(validity_script())
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_NEW}/validity", params={"legal_entity": ENTITY},
                        json={"valid_from": None, "valid_to": None})
    assert resp.status_code == 200
    updates = [p for sql, p in cur.calls if sql.startswith("UPDATE")]
    assert len(updates) == 4
    assert all(p["vf"] is None and p["vt"] is None for p in updates)


@pytest.mark.parametrize("body", [
    {"valid_from": "2026-09-01", "valid_to": "2026-09-01"},
    {"valid_from": "2026-09-01", "valid_to": None},
    {"valid_from": None, "valid_to": "2026-09-01"},
])
def test_validity_equal_dates_and_open_ended_are_fine(monkeypatch, body):
    cur = FakeCursor(validity_script())
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_NEW}/validity",
                        params={"legal_entity": ENTITY}, json=body)
    assert resp.status_code == 200


@pytest.mark.parametrize("body", [
    {"valid_from": "2026-13-01", "valid_to": None},        # no such month
    {"valid_from": "2026-02-30", "valid_to": None},        # no such day
    {"valid_from": "01.09.2026", "valid_to": None},        # not ISO
    {"valid_from": "20260901", "valid_to": None},          # compact form
    {"valid_from": "2026-09-01T00:00:00", "valid_to": None},
    {"valid_from": 20260901, "valid_to": None},            # not a string
    {"valid_from": "", "valid_to": None},
    {"valid_from": None, "valid_to": "garbage"},
    {"valid_from": "2027-01-01", "valid_to": "2026-01-01"},  # from > to
    {"valid_from": "2026-09-01"},                          # key missing
    {"valid_to": "2026-09-01"},
    {},
    {"valid_from": None, "valid_to": None, "is_active": True},  # extra key
    [],
    "2026-09-01",
])
def test_validity_bad_body_is_422_without_db(monkeypatch, body):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_NEW}/validity",
                        params={"legal_entity": ENTITY}, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert cur.calls == []


def test_validity_missing_body_is_422_without_db(monkeypatch):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_NEW}/validity", params={"legal_entity": ENTITY})
    assert resp.status_code == 422
    assert cur.calls == []


def test_validity_bad_uuid_is_400_without_db(monkeypatch):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/not-a-uuid/validity", params={"legal_entity": ENTITY},
                        json={"valid_from": None, "valid_to": None})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
    assert cur.calls == []


def test_validity_foreign_school_is_404_no_update(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_NEW}/validity",
                        params={"legal_entity": OTHER_ENTITY},
                        json={"valid_from": None, "valid_to": "2027-06-30"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.calls)
    assert_scoped(cur, OTHER_ENTITY)


def test_validity_exception_midway_rolls_back(monkeypatch):
    class Boom(Exception):
        pass

    script = validity_script()
    script[3] = {"raise": Boom("permission denied")}   # document_elements update
    cur = FakeCursor(script)
    client = make_app(cur, monkeypatch)
    with pytest.raises(Boom):
        client.patch(f"{BASE}/{FU_NEW}/validity", params={"legal_entity": ENTITY},
                     json={"valid_from": "2026-09-01", "valid_to": None})
    assert cur.rolled_back is True
    assert cur.committed is False
    assert not any("document_metadata" in sql and sql.startswith("UPDATE")
                   for sql, _ in cur.calls)


# ---------------------------------------------------------------------------
# permanent delete
# ---------------------------------------------------------------------------

def delete_script():
    return [
        {"fetchone": (KEY,)},
        {"fetchall": [(FU_NEW,), (FU_OLD,)]},
        {"rowcount": 12},            # chunks
        {"rowcount": 3},             # tables
        {"rowcount": 40},            # elements
        {"rowcount": 2},             # metadata
    ]


def test_delete_counts_and_scope(monkeypatch):
    cur = FakeCursor(delete_script())
    client = make_app(cur, monkeypatch)
    resp = client.delete(BASE, params={"legal_entity": ENTITY, "file_uuid": FU_NEW})
    assert resp.status_code == 200
    assert resp.json() == {
        "deleted": {"versions": 2, "chunks": 12, "elements": 40, "tables": 3},
        "file_uuids": sorted([FU_NEW, FU_OLD]),
    }
    deletes = [(sql, p) for sql, p in cur.calls if sql.startswith("DELETE")]
    tables = [sql.split()[2].split(".")[-1] for sql, _ in deletes]
    assert tables == ["document_chunks", "document_tables",
                      "document_elements", "document_metadata"]
    for sql, params in deletes:
        assert "ANY(%(fus)s::uuid[])" in sql
        assert sorted(params["fus"]) == sorted([FU_NEW, FU_OLD])
    assert_scoped(cur)
    assert cur.committed is True


def test_delete_never_touches_logs_or_code_lists(monkeypatch):
    cur = FakeCursor(delete_script())
    client = make_app(cur, monkeypatch)
    client.delete(BASE, params={"legal_entity": ENTITY, "file_uuid": FU_NEW})
    for sql, _ in cur.calls:
        assert "ingestion_log" not in sql
        assert "question_answer_log" not in sql
        assert "code_list" not in sql


def test_delete_foreign_school_is_404_nothing_deleted(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)
    resp = client.delete(BASE, params={"legal_entity": OTHER_ENTITY,
                                       "file_uuid": FU_NEW})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert not any(sql.startswith("DELETE") for sql, _ in cur.calls)


def test_delete_exception_midway_rolls_back(monkeypatch):
    class Boom(Exception):
        pass

    script = delete_script()
    script[3] = {"raise": Boom("db exploded")}   # tables delete fails
    cur = FakeCursor(script)
    client = make_app(cur, monkeypatch)
    with pytest.raises(Boom):
        client.delete(BASE, params={"legal_entity": ENTITY, "file_uuid": FU_NEW})
    assert cur.rolled_back is True
    assert cur.committed is False
    assert not any("document_metadata" in sql and sql.startswith("DELETE")
                   for sql, _ in cur.calls)


# ---------------------------------------------------------------------------
# file format (sec.14.10)
# ---------------------------------------------------------------------------

def test_list_format_filter_is_parameterized(monkeypatch):
    cur = FakeCursor([{"fetchall": [summary_row(file_format="pdf")]}])
    client = make_app(cur, monkeypatch)
    resp = client.get(BASE, params={"legal_entity": ENTITY, "format": " PDF "})
    assert resp.status_code == 200
    assert resp.json()["documents"][0]["file_format"] == "pdf"
    sql, params = cur.calls[0]
    assert params["format"] == "pdf" and "= %(format)s" in sql
    assert "~* '[.]html?$' THEN 'web'" in sql
    assert_scoped(cur)


def test_list_bad_format_is_422(monkeypatch):
    cur = FakeCursor([])
    client = make_app(cur, monkeypatch)
    resp = client.get(BASE, params={"legal_entity": ENTITY, "format": "exe"})
    assert resp.status_code == 422 and not cur.calls


# ---------------------------------------------------------------------------
# validity=expiring (sec.14.12)
# ---------------------------------------------------------------------------

def test_list_validity_expiring_filters_active_current_next_30_days(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    resp = client.get(BASE, params={"legal_entity": ENTITY, "validity": "expiring"})
    assert resp.status_code == 200
    sql, params = cur.calls[0]
    assert "d.doc_active AND d.valid_to IS NOT NULL" in sql
    assert "d.valid_to >= CURRENT_DATE AND d.valid_to <= CURRENT_DATE + 30" in sql
    assert "d.valid_from <= CURRENT_DATE" in sql
    assert "validity" not in params
    assert_scoped(cur)


# ---------------------------------------------------------------------------
# stats (sec.14.12)
# ---------------------------------------------------------------------------

def test_stats_counts_and_scope(monkeypatch):
    cur = FakeCursor([{"fetchone": (5, 2, 1, 3, 4)}, {"fetchone": (11,)}])
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/stats", params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json() == {"active": 5, "inactive": 2, "expired": 1,
                           "expiring_30": 3, "future": 4, "qa_entries_active": 11}
    doc_sql, qa_sql = cur.calls[0][0], cur.calls[1][0]
    assert "d.shown_rank = 1" in doc_sql and "FROM d" in doc_sql
    assert "m.legal_entity_code = %(le)s" in doc_sql
    assert "qa_entries" in qa_sql and "e.is_active" in qa_sql
    assert len(cur.calls) == 2 and cur.committed is True
    assert_scoped(cur)


def test_stats_is_not_swallowed_by_the_uuid_route(monkeypatch):
    cur = FakeCursor([{"fetchone": (0, 0, 0, 0, 0)}, {"fetchone": (0,)}])
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/stats", params={"legal_entity": OTHER_ENTITY})
    assert resp.status_code == 200
    assert resp.json()["qa_entries_active"] == 0
    assert_scoped(cur, OTHER_ENTITY)


# ---------------------------------------------------------------------------
# chunks (sec.14.13)
# ---------------------------------------------------------------------------

def chunk_row(i, text="Text"):
    return (i, "Rad > Cl. 1", "Cl. 1", text)


def test_chunks_page_total_and_next_cursor(monkeypatch):
    cur = FakeCursor([{"fetchone": (1,)}, {"fetchone": (45,)},
                      {"fetchall": [chunk_row(20, "abcd"), chunk_row(21, None)]}])
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/{FU_NEW}/chunks",
                      params={"legal_entity": ENTITY, "limit": 2, "cursor": "20"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 45 and body["next_cursor"] == "22"
    assert body["chunks"][0] == {"chunk_index": 20, "heading_path": "Rad > Cl. 1",
                                 "section_heading": "Cl. 1", "text": "abcd",
                                 "char_count": 4}
    assert body["chunks"][1]["text"] == "" and body["chunks"][1]["char_count"] == 0
    sql, params = cur.calls[2]
    assert "ORDER BY c.chunk_index" in sql
    assert params["lim"] == 2 and params["off"] == 20 and params["fu"] == FU_NEW
    assert "document_metadata" in cur.calls[0][0]
    assert "document_chunks" in cur.calls[1][0]
    assert_scoped(cur)


def test_chunks_default_limit_and_last_page(monkeypatch):
    cur = FakeCursor([{"fetchone": (1,)}, {"fetchone": (3,)},
                      {"fetchall": [chunk_row(0), chunk_row(1), chunk_row(2)]}])
    client = make_app(cur, monkeypatch)
    body = client.get(f"{BASE}/{FU_NEW}/chunks", params={"legal_entity": ENTITY}).json()
    assert cur.calls[2][1]["lim"] == 20 and cur.calls[2][1]["off"] == 0
    assert body["next_cursor"] is None and body["total"] == 3


def test_chunks_foreign_school_is_404_no_chunk_query(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/{FU_NEW}/chunks", params={"legal_entity": OTHER_ENTITY})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert len(cur.calls) == 1
    assert_scoped(cur, OTHER_ENTITY)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "x"},
                                    {"cursor": "-1"}])
def test_chunks_bad_limit_or_cursor_is_422_without_db(monkeypatch, params):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    resp = client.get(f"{BASE}/{FU_NEW}/chunks",
                      params={"legal_entity": ENTITY, **params})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert cur.calls == []


# ---------------------------------------------------------------------------
# content hash (sec.14.14)
# ---------------------------------------------------------------------------

def test_summary_and_version_sql_read_content_sha256_explicitly(monkeypatch):
    cur = FakeCursor([{"fetchall": []}])
    client = make_app(cur, monkeypatch)
    client.get(BASE, params={"legal_entity": ENTITY})
    assert "d.content_sha256" in cur.calls[0][0]

    cur2 = FakeCursor([{"fetchone": (KEY,)}, {"fetchone": summary_row()},
                       {"fetchall": []}])
    make_app(cur2, monkeypatch).get(f"{BASE}/{FU_NEW}", params={"legal_entity": ENTITY})
    assert "m.content_sha256" in cur2.calls[2][0]


def test_content_hash_sets_one_version_scoped(monkeypatch):
    cur = FakeCursor([{"rowcount": 1}])
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_OLD}/content-hash",
                        params={"legal_entity": ENTITY},
                        json={"content_sha256": HASH})
    assert resp.status_code == 200
    assert resp.json() == {"file_uuid": FU_OLD, "content_sha256": HASH}
    sql, params = cur.calls[0]
    assert sql.startswith("UPDATE") and "document_metadata" in sql
    assert "m.file_uuid = %(fu)s::uuid" in sql and "m.source_url" not in sql
    assert params == {"h": HASH, "fu": FU_OLD, "le": ENTITY}
    assert len(cur.calls) == 1 and cur.committed is True
    assert_scoped(cur)


def test_content_hash_foreign_school_is_404(monkeypatch):
    cur = FakeCursor([{"rowcount": 0}])
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_OLD}/content-hash",
                        params={"legal_entity": OTHER_ENTITY},
                        json={"content_sha256": HASH})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert_scoped(cur, OTHER_ENTITY)


@pytest.mark.parametrize("body", [
    None, [], {}, {"content_sha256": None}, {"content_sha256": "a" * 63},
    {"content_sha256": "a" * 65}, {"content_sha256": "A" * 64},
    {"content_sha256": "g" * 64}, {"content_sha256": 5},
    {"content_sha256": "a" * 64, "extra": 1}, {"sha256": "a" * 64},
])
def test_content_hash_bad_body_is_422_without_db(monkeypatch, body):
    cur = FakeCursor()
    client = make_app(cur, monkeypatch)
    resp = client.patch(f"{BASE}/{FU_OLD}/content-hash",
                        params={"legal_entity": ENTITY}, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert cur.calls == []
