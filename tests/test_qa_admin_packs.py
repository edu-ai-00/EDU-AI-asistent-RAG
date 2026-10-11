# written for task qa-data-c1b.
"""Unit tests for search/app/qa_admin.py::pack-state and entries — no real DB.

Same fake-cursor pattern as tests/test_qa_admin_catalog.py: a single FakeCursor
per app records executed SQL/params and returns scripted results in call
order, monkeypatched over qa_admin._cursor.
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import qa_admin


class FakeCursor:
    """Records every execute() call; fetchall/fetchone are scripted in order."""

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
PACK = "faq_provoz"

MANIFEST_ROW = (
    3,                                  # version
    datetime.datetime(2026, 8, 1, 12, 0, 0),  # built_at
    "cs",                               # lang
    "entity",                           # visibility
    ["TEACHER"],                        # audience_roles
    [10, 11],                           # school_grade_ids
    [20],                               # school_subject_ids
    "ZS",                               # school_level
    None,                               # valid_from
    None,                               # valid_to
    [30],                               # domain_ids
)

ENTRY_1 = (
    "provoz-01", "11111111-1111-1111-1111-111111111111",
    "Kdy zacina skolni rok?", "1. zari.",
    {"category": "provoz"}, [{"title": "Web", "url": "https://example.org"}],
    [30],
    100, "SKOLNI_ROK", {"cs": "Skolni rok"}, True,
    "u1", "entity", ["TEACHER"], [10, 11], [20], "ZS", None, None, True,
    ["DOMAIN_PROVOZ"],
    # The SQL excludes a phrasing equal to canonical_q (qa_export's rule); a
    # fixture standing in for that SQL result therefore never includes it.
    ["Kdy je zacatek roku?"],
)

ENTRY_2 = (
    "provoz-02", "22222222-2222-2222-2222-222222222222",
    "Kdy konci skolni rok?", "30. cervna.",
    {"category": "provoz"}, [],
    [30],
    None, None, None, None,
    "u1", "entity", ["TEACHER"], [10, 11], [20], "ZS", None, None, True,
    ["DOMAIN_PROVOZ"],
    [],
)


def _pack_script(entries, grade_codes=None, subject_codes=None, domain_codes=None):
    """manifest fetchone + entries fetchall, then whichever code-list lookups
    /pack-state fires (only when the corresponding id list is non-empty)."""
    script = [
        {"fetchone": MANIFEST_ROW},
        {"fetchall": list(entries)},
    ]
    if grade_codes is not None:
        script.append({"fetchall": grade_codes})
    if subject_codes is not None:
        script.append({"fetchall": subject_codes})
    if domain_codes is not None:
        script.append({"fetchall": domain_codes})
    return script


# ---------------------------------------------------------------------------
# 1. Foreign entity cannot reach another entity's pack
# ---------------------------------------------------------------------------

def test_pack_state_wrong_entity_is_not_found(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)

    resp = client.get(f"/internal/qa-admin/pack-state?legal_entity=OTHER_SCHOOL&pack_id={PACK}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    sql, params = cur.calls[0]
    assert "legal_entity_code = %s" in sql
    assert params == (PACK, "OTHER_SCHOOL")


def test_entries_wrong_entity_is_not_found(monkeypatch):
    cur = FakeCursor([{"fetchone": None}])
    client = make_app(cur, monkeypatch)

    resp = client.get(f"/internal/qa-admin/entries?legal_entity=OTHER_SCHOOL&pack_id={PACK}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 2/3. etag is over the WHOLE pack, not a page; changes with content, not order
# ---------------------------------------------------------------------------

def test_etag_identical_across_page_sizes(monkeypatch):
    cur_small = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client_small = make_app(cur_small, monkeypatch)
    resp_small = client_small.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}&limit=1")

    cur_big = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client_big = make_app(cur_big, monkeypatch)
    resp_big = client_big.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}&limit=100")

    assert resp_small.status_code == 200 and resp_big.status_code == 200
    assert resp_small.json()["pack_etag"] == resp_big.json()["pack_etag"]
    assert len(resp_small.json()["entries"]) == 1
    assert len(resp_big.json()["entries"]) == 2


def test_etag_changes_with_content_not_with_row_order(monkeypatch):
    cur = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client = make_app(cur, monkeypatch)
    base_etag = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}").json()["pack_etag"]

    # reordered rows (as if the DB returned them differently) -> same etag
    cur_reordered = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_2, ENTRY_1]}])
    client_reordered = make_app(cur_reordered, monkeypatch)
    reordered_etag = client_reordered.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}").json()["pack_etag"]
    assert reordered_etag == base_etag

    # changed answer_md -> different etag
    entry_1_changed = ENTRY_1[:3] + ("Zacatek je jinak.",) + ENTRY_1[4:]
    cur_changed = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [entry_1_changed, ENTRY_2]}])
    client_changed = make_app(cur_changed, monkeypatch)
    changed_etag = client_changed.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}").json()["pack_etag"]
    assert changed_etag != base_etag


# ---------------------------------------------------------------------------
# 4. Pagination: union of pages == all entries, no dup/loss
# ---------------------------------------------------------------------------

def test_pagination_round_trip(monkeypatch):
    cur1 = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client1 = make_app(cur1, monkeypatch)
    page1 = client1.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}&limit=1").json()
    assert len(page1["entries"]) == 1
    assert page1["next_cursor"] == "provoz-01"

    cur2 = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client2 = make_app(cur2, monkeypatch)
    page2 = client2.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}"
        f"&limit=1&cursor={page1['next_cursor']}").json()
    assert len(page2["entries"]) == 1
    assert page2["next_cursor"] is None

    keys = {page1["entries"][0]["entry_key"], page2["entries"][0]["entry_key"]}
    assert keys == {"provoz-01", "provoz-02"}


# ---------------------------------------------------------------------------
# 5. Unknown cursor -> 422, never a silent restart from the beginning
# ---------------------------------------------------------------------------

def test_unknown_cursor_is_validation_error(monkeypatch):
    cur = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client = make_app(cur, monkeypatch)
    resp = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}&cursor=nope")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 6. metadata_uniform: true when identical, false + divergence detail otherwise
# ---------------------------------------------------------------------------

def test_metadata_uniform_true_when_identical(monkeypatch):
    cur = FakeCursor(_pack_script(
        [ENTRY_1, ENTRY_2],
        grade_codes=[("R5",), ("R6",)], subject_codes=[("MAT",)],
        domain_codes=[("DOMAIN_PROVOZ",)]))
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/pack-state?legal_entity={ENTITY}&pack_id={PACK}").json()
    assert body["metadata_uniform"] is True
    assert body["divergent_fields"] == []
    assert body["divergent_entries"] == []


def test_metadata_divergent_grade_ids(monkeypatch):
    entry_2_diff_grade = ENTRY_2[:14] + ([99],) + ENTRY_2[15:]
    cur = FakeCursor(_pack_script(
        [ENTRY_1, entry_2_diff_grade],
        grade_codes=[("R5",), ("R6",)], subject_codes=[("MAT",)],
        domain_codes=[("DOMAIN_PROVOZ",)]))
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/pack-state?legal_entity={ENTITY}&pack_id={PACK}").json()
    assert body["metadata_uniform"] is False
    assert body["divergent_fields"] == ["school_grade_ids"]
    assert body["divergent_entries"] == ["provoz-02"]


# ---------------------------------------------------------------------------
# 7. Metadata and owner are preserved verbatim, including null
# ---------------------------------------------------------------------------

def test_owner_and_validity_pass_through_unchanged(monkeypatch):
    cur = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1, ENTRY_2]}])
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}").json()
    e1 = body["entries"][0]
    assert e1["owner_user_id"] == "u1"
    assert e1["visibility"] == "entity"
    assert e1["valid_from"] is None
    assert e1["valid_to"] is None


# ---------------------------------------------------------------------------
# 8. A variant equal to canonical_q never shows up (the round-trip guard)
# ---------------------------------------------------------------------------

def test_variants_exclude_canonical_phrase(monkeypatch):
    cur = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [ENTRY_1]}])
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}").json()
    variants = body["entries"][0]["variants"]
    assert "Kdy zacina skolni rok?" not in variants
    assert "Kdy je zacatek roku?" in variants
    # the exclusion is a DB-side predicate (qa_export's rule); assert it is
    # actually issued, since the fake cursor cannot exercise it end-to-end
    entries_sql = cur.calls[1][0]
    assert "lower(v.phrasing) <> lower(e.canonical_q)" in entries_sql


# ---------------------------------------------------------------------------
# 9. Category label fallback and inactive categories stay visible
# ---------------------------------------------------------------------------

def test_category_label_fallback_and_inactive_visible(monkeypatch):
    entry_inactive_cat = ENTRY_1[:10] + (False,) + ENTRY_1[11:]
    cur = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": [entry_inactive_cat]}])
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}&lang=en").json()
    e1 = body["entries"][0]
    assert e1["category_code"] == "SKOLNI_ROK"
    assert e1["category_label"] == "Skolni rok"  # only `cs` in labels
    assert e1["category_label_fallback"] is True
    assert e1["category_is_active"] is False  # inactive category is not hidden


# ---------------------------------------------------------------------------
# 10. Empty pack: entry_count 0, uniform metadata, valid etag, no entries
# ---------------------------------------------------------------------------

def test_empty_pack_pack_state(monkeypatch):
    cur = FakeCursor(_pack_script(
        [], grade_codes=[("R5",), ("R6",)], subject_codes=[("MAT",)],
        domain_codes=[("DOMAIN_PROVOZ",)]))
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/pack-state?legal_entity={ENTITY}&pack_id={PACK}").json()
    assert body["entry_count"] == 0
    assert body["metadata_uniform"] is True
    assert body["divergent_fields"] == []
    assert body["pack_etag"].startswith("sha256:")


def test_empty_pack_entries(monkeypatch):
    cur = FakeCursor([{"fetchone": MANIFEST_ROW}, {"fetchall": []}])
    client = make_app(cur, monkeypatch)
    body = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}").json()
    assert body["entries"] == []
    assert body["next_cursor"] is None
    assert body["metadata_uniform"] is True


# ---------------------------------------------------------------------------
# limit bounds -> 422 without touching the DB
# ---------------------------------------------------------------------------

def test_limit_out_of_range_is_validation_error(monkeypatch):
    cur = FakeCursor([])
    client = make_app(cur, monkeypatch)
    resp = client.get(
        f"/internal/qa-admin/entries?legal_entity={ENTITY}&pack_id={PACK}&limit=0")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert cur.calls == []
