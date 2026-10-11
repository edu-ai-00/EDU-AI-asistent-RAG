"""Live round-trip checks against the isolated dev QA portal API (task qa-tests-c5a).

Deliberately NOT mocked: every request in this file goes over real HTTP to the
running dev portal at BASE_URL. Every test skips cleanly if that portal does
not answer, so this file is safe to run anywhere (CONTRACT.md §5, §2.4a,
§2.4c, §3, §7).

Scope guardrails baked into this file:
  * SCHOOL is fixed to ZAKLADNI_SKOLA_BOROVA (the allocated resource).
  * Every pack this file creates is prefixed "t5-" (allocated pack namespace);
    it never writes to faq-provoz, skolni-druzina or any other existing pack.
  * Only the portal's published host port is used; no docker, no direct DB
    access. The port moved once already (Windows reserved the old range), so it
    comes from QA_PORTAL_BASE_URL with the original value as the default.

Run:
    cd qa_portal/tests
    "qa_portal/backend/.venv/Scripts/python.exe" \
        -m pytest -q test_live_roundtrip.py
"""
from __future__ import annotations

import csv as _csv
import io
import json
import os
import time
import uuid
import zipfile
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("QA_PORTAL_BASE_URL", "http://127.0.0.1:18082")
SCHOOL = "ZAKLADNI_SKOLA_BOROVA"
HEADERS = {"X-QA-Portal": "1", "Origin": BASE_URL}

DEFAULT_METADATA = {
    "visibility": "entity",
    "audience_roles": ["TEACHER"],
    "school_grade_codes": ["1"],
}

# CONTRACT.md §5.1 "Oddělovač" (v1.4.0): every writer now emits ',';
# every reader still accepts both ',' and ';' so old semicolon packs stay
# importable. The 9-column header (with category_code) is what today's
# writers (portal and search export) produce.
CSV_HEADER = ["entry_key", "canonical_q", "answer_md", "cat", "variants",
              "sources", "is_active", "domains", "category_code"]


def _pack_id(tag: str) -> str:
    # Every pack created by this file MUST start with "t5-" (allocated
    # namespace) so it can never collide with real content such as
    # faq-provoz or skolni-druzina.
    return f"t5-{tag}-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=20.0) as c:
        yield c


@pytest.fixture(autouse=True)
def _require_live(client):
    """Every test in this file depends on this fixture: if the dev portal is
    down, skip cleanly instead of failing/erroring (task instructions)."""
    try:
        resp = client.get("/qa-api/v1/context", timeout=3.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"qa-portal dev API at {BASE_URL} is unreachable: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"qa-portal dev API at {BASE_URL} returned {resp.status_code}")


# ── small helpers ───────────────────────────────────────────────────────────

def _wait_operation(client: httpx.Client, operation_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/qa-api/v1/operations/{operation_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.4)
    raise AssertionError(f"operation {operation_id} did not finish within {timeout}s: {last}")


def _csv_bytes(rows: list[dict], *, delimiter: str = ",") -> bytes:
    """Build CSV bytes per CONTRACT.md §5.1: UTF-8 BOM, CRLF, RFC 4180
    quoting, trailing category_code column. Built locally (not by importing
    backend app code) so this file only depends on the live HTTP surface,
    matching the byte layout app/services/csv_io.write_pack_csv produces.

    delimiter defaults to ',' (what every writer emits since v1.4.0);
    callers proving the legacy path pass delimiter=';' explicitly — the
    reader must still accept both."""
    buf = io.StringIO(newline="")
    writer = _csv.writer(buf, delimiter=delimiter, quoting=_csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for r in rows:
        sources_cell_lines = []
        for s in (r.get("sources") or []):
            title = (s.get("title") or "").strip()
            url = (s.get("url") or "").strip()
            if url and title and title != url:
                sources_cell_lines.append(f"{title} | {url}")
            elif url or title:
                sources_cell_lines.append(url or title)
        writer.writerow([
            r.get("entry_key") or "",
            r.get("canonical_q") or "",
            r.get("answer_md") or "",
            r.get("cat") or "",
            "\n".join(r.get("variants") or []),
            "\n".join(sources_cell_lines),
            "TRUE" if r.get("is_active", True) else "FALSE",
            json.dumps(r.get("domains") or [], ensure_ascii=False),
            r.get("category_code") or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


def _entries(client: httpx.Client, pack_id: str) -> dict:
    resp = client.get(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/entries", params={"limit": 200})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_pack_direct(client: httpx.Client, pack_id: str, entries: list[dict],
                         metadata: dict) -> dict:
    """Create a brand-new t5- pack through preview+publish (CONTRACT.md §2.4a),
    waiting for the publish operation to complete. Returns the finished
    operation record."""
    body: dict[str, Any] = {"base_etag": "", "entries": entries, "pack_metadata": metadata}
    resp = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/preview", json=body)
    assert resp.status_code == 200, resp.text
    preview_id = resp.json()["preview_id"]
    pub = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/publish",
                       json={"preview_id": preview_id, "expected_etag": ""})
    assert pub.status_code == 202, pub.text
    op = _wait_operation(client, pub.json()["operation_id"])
    assert op["status"] == "completed", op
    return op


def _export(client: httpx.Client, pack_ids: list[str], fmt: str) -> tuple[bytes, str]:
    resp = client.post(f"/qa-api/v1/schools/{SCHOOL}/exports",
                        json={"pack_ids": pack_ids, "format": fmt})
    assert resp.status_code == 201, resp.text
    export_id = resp.json()["export_id"]
    download = client.get(f"/qa-api/v1/exports/{export_id}/download")
    assert download.status_code == 200, download.text
    return download.content, download.headers["content-type"]


def _zip_manifest(zip_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return json.loads(zf.read("manifest.json").decode("utf-8"))


# ── 1. CSV round trip: diacritics, markdown, quoting, variants, sources, ──
# ── domains — an identical re-import must be a true no-op ─────────────────

def test_csv_roundtrip_is_noop_and_preserves_text_fields(client):
    pack_id = _pack_id("csv")
    entry = {
        "entry_key": None,
        "canonical_q": 'Kdy začíná školní rok se "zvláštním"; polem?',
        "answer_md": '**Školní rok** začíná 1. září; nezapomeňte si vzít "přezůvky".',
        "variants": ["Kdy začne škola?", "Od kdy je škola?"],
        "sources": [{"title": "Vyhláška; MŠMT", "url": "https://example.cz/vyhlaska?a=1&b=2"}],
        "domains": ["SCHOOL_OPERATIONS", "ATTENDANCE"],
        "category_code": None,
        "is_active": True,
    }
    _create_pack_direct(client, pack_id, [entry], DEFAULT_METADATA)

    before = _entries(client, pack_id)
    assert before["total"] == 1
    stored = before["entries"][0]
    entry_key = stored["entry_key"]
    pack_etag = before["pack_etag"]

    assert stored["canonical_q"] == entry["canonical_q"]
    assert stored["answer_md"] == entry["answer_md"]
    assert stored["variants"] == entry["variants"]
    assert stored["sources"] == entry["sources"]
    assert set(stored["domains"]) == set(entry["domains"])

    csv_bytes, content_type = _export(client, [pack_id], "csv")
    assert content_type.startswith("text/csv")
    text = csv_bytes.decode("utf-8-sig")
    # diacritics and quoting must survive byte-for-byte on the wire
    assert "začíná" in text
    assert "zvláštním" in text
    assert "přezůvky" in text
    assert "MŠMT" in text

    # CONTRACT.md §5.1 (v1.4.0): the portal now writes ',' exclusively. The
    # header line must split on ',' into the known column set, and on ';'
    # into a single element (there is no ';' in the header at all).
    header_line = text.split("\r\n", 1)[0]
    assert header_line.split(",") == CSV_HEADER
    assert header_line.split(";") == [header_line]

    preview = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
        data={"pack_id": pack_id, "mode": "merge"},
        files={"file": (f"{pack_id}.csv", csv_bytes, "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    (pinfo,) = preview.json()["packs"]
    assert pinfo["ok"] is True
    assert pinfo["diff"]["added"] == []
    assert pinfo["diff"]["changed"] == []
    assert pinfo["diff"]["deactivated"] == []
    assert pinfo["diff"]["unchanged"] == [entry_key]

    pub = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/{preview.json()['preview_id']}/publish",
        json={"expected_etags": {pack_id: pack_etag}})
    assert pub.status_code == 202, pub.text
    op = _wait_operation(client, pub.json()["operation_id"])
    assert op["status"] == "completed", op
    (pack_status,) = op["packs"]
    assert pack_status["pack_id"] == pack_id
    assert pack_status["status"] == "ok"

    after = _entries(client, pack_id)
    restored = after["entries"][0]
    assert restored["entry_key"] == entry_key
    assert restored["canonical_q"] == entry["canonical_q"]
    assert restored["answer_md"] == entry["answer_md"]
    assert restored["variants"] == entry["variants"]
    assert restored["sources"] == entry["sources"]
    assert set(restored["domains"]) == set(entry["domains"])

    # The etag is defined over version + built_at + entries (CONTRACT.md §3),
    # and the writer bumps the manifest version on every ingest — so a no-op
    # re-import MOVES the etag by design and the old one stops being valid.
    # What must not move is the CONTENT, asserted above. Pinned here so nobody
    # later "fixes" the etag to be content-only without noticing that publish
    # uses exactly this movement as its proof that the write happened.
    assert after["pack_etag"] != pack_etag
    assert after["pack_version"] > before["pack_version"]
    assert after["total"] == before["total"]


# ── 2. ZIP round trip of two packs: per-pack results, no entries lost, ────
# ── existing packs are never asked for metadata ────────────────────────────

def test_zip_roundtrip_reports_per_pack_and_loses_nothing(client):
    pack_a = _pack_id("zip-a")
    pack_b = _pack_id("zip-b")
    entry_a = {
        "entry_key": None, "canonical_q": "Kde je jídelna?", "answer_md": "V přízemí budovy.",
        "variants": [], "sources": [], "domains": ["SCHOOL_OPERATIONS"],
        "category_code": None, "is_active": True,
    }
    entry_b = {
        "entry_key": None, "canonical_q": "Co dělat při šikaně?", "answer_md": "Kontaktujte třídního učitele.",
        "variants": ["Koho kontaktovat při šikaně?"], "sources": [], "domains": ["BULLYING"],
        "category_code": None, "is_active": True,
    }
    _create_pack_direct(client, pack_a, [entry_a], DEFAULT_METADATA)
    _create_pack_direct(client, pack_b, [entry_b],
                         {"visibility": "organization", "audience_roles": ["PARENT"],
                          "school_grade_codes": []})

    before_a = _entries(client, pack_a)
    before_b = _entries(client, pack_b)

    zip_bytes, content_type = _export(client, [pack_a, pack_b], "zip")
    assert content_type == "application/zip"

    preview = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
        data={"mode": "merge"},
        files={"file": ("export.zip", zip_bytes, "application/zip")},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["source_kind"] == "zip"
    by_id = {p["pack_id"]: p for p in body["packs"]}
    assert set(by_id) == {pack_a, pack_b}
    for pid, pinfo in by_id.items():
        assert pinfo["ok"] is True
        assert pinfo["diff"]["deactivated"] == []
        # existing packs must NOT be asked for metadata
        assert pinfo["is_new_pack"] is False
        assert pinfo["metadata_required"] is False
        assert pinfo["publishable"] is True

    pub = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/{body['preview_id']}/publish",
        json={"expected_etags": {pack_a: before_a["pack_etag"], pack_b: before_b["pack_etag"]}})
    assert pub.status_code == 202, pub.text
    op = _wait_operation(client, pub.json()["operation_id"])
    assert op["status"] == "completed", op
    statuses = {p["pack_id"]: p["status"] for p in op["packs"]}
    assert statuses == {pack_a: "ok", pack_b: "ok"}

    after_a = _entries(client, pack_a)
    after_b = _entries(client, pack_b)
    assert after_a["total"] == before_a["total"] == 1
    assert after_b["total"] == before_b["total"] == 1
    assert {e["entry_key"] for e in after_a["entries"]} == \
           {e["entry_key"] for e in before_a["entries"]}
    assert {e["entry_key"] for e in after_b["entries"]} == \
           {e["entry_key"] for e in before_b["entries"]}


# ── 3. import into a not-yet-existing pack (§2.4c) ─────────────────────────

def test_import_into_new_pack_requires_confirmed_metadata(client):
    pack_id = _pack_id("new")
    row = {
        "entry_key": None, "canonical_q": "Kdy je zápis do první třídy?",
        "answer_md": "Zápis probíhá v dubnu, termín zveřejníme na webu.",
        "variants": [], "sources": [], "domains": ["SCHOOL_OPERATIONS"],
        "category_code": None, "is_active": True,
    }
    csv_bytes = _csv_bytes([row])

    # (a) without pack_metadata_by_pack: preview is 200 but not publishable
    preview1 = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
        data={"pack_id": pack_id, "mode": "merge"},
        files={"file": (f"{pack_id}.csv", csv_bytes, "text/csv")},
    )
    assert preview1.status_code == 200, preview1.text
    (pinfo1,) = preview1.json()["packs"]
    assert pinfo1["is_new_pack"] is True
    assert pinfo1["metadata_required"] is True
    assert pinfo1["publishable"] is False

    pub1 = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/{preview1.json()['preview_id']}/publish",
        json={"expected_etags": {pack_id: ""}})
    assert pub1.status_code == 202, pub1.text
    op1 = _wait_operation(client, pub1.json()["operation_id"])
    (pack_status1,) = op1["packs"]
    assert pack_status1["pack_id"] == pack_id
    assert pack_status1["status"] == "failed"
    assert pack_status1["message"] == "PACK_METADATA_REQUIRED"

    # the pack must not exist afterwards
    still_missing = client.get(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/entries")
    assert still_missing.status_code == 404, still_missing.text

    # (b) repeat WITH pack_metadata_by_pack -> pack gets created with exactly
    # the confirmed visibility/roles/grades
    confirmed_metadata = {
        "visibility": "organization",
        "audience_roles": ["STUDENT", "PARENT"],
        "school_grade_codes": ["3", "4"],
    }
    preview2 = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
        data={"pack_id": pack_id, "mode": "merge",
              "pack_metadata_by_pack": json.dumps({pack_id: confirmed_metadata})},
        files={"file": (f"{pack_id}.csv", csv_bytes, "text/csv")},
    )
    assert preview2.status_code == 200, preview2.text
    (pinfo2,) = preview2.json()["packs"]
    assert pinfo2["is_new_pack"] is True
    assert pinfo2["metadata_required"] is False
    assert pinfo2["publishable"] is True

    pub2 = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/{preview2.json()['preview_id']}/publish",
        json={"expected_etags": {pack_id: ""}})
    assert pub2.status_code == 202, pub2.text
    op2 = _wait_operation(client, pub2.json()["operation_id"])
    (pack_status2,) = op2["packs"]
    assert pack_status2["status"] == "ok"

    # read back via the API: the entries endpoint for role/visibility (codes),
    # and a ZIP export manifest for school_grade_codes (the entries endpoint
    # only exposes internal school_grade_ids, not the codes we submitted).
    entries_after = _entries(client, pack_id)
    assert entries_after["total"] == 1
    stored_entry = entries_after["entries"][0]
    assert stored_entry["visibility"] == confirmed_metadata["visibility"]
    # role order isn't part of the contract; compare as sets
    assert sorted(stored_entry["audience_roles"]) == sorted(confirmed_metadata["audience_roles"])

    zip_bytes, _ = _export(client, [pack_id], "zip")
    manifest = _zip_manifest(zip_bytes)
    (pack_meta,) = manifest["packs"]
    assert pack_meta["pack_id"] == pack_id
    assert pack_meta["metadata"]["visibility"] == confirmed_metadata["visibility"]
    assert sorted(pack_meta["metadata"]["audience_roles"]) == sorted(confirmed_metadata["audience_roles"])
    assert sorted(pack_meta["metadata"]["school_grade_codes"]) == \
           sorted(confirmed_metadata["school_grade_codes"])


# ── 3b. a legacy ';'-delimited CSV still imports and yields the same ──────
# ── entries as the modern ','-delimited version of the same rows ──────────

def test_legacy_semicolon_csv_imports_same_entries_as_comma_csv(client):
    """CONTRACT.md §5.1 (v1.4.0): readers accept both delimiters so old
    exports stay importable without conversion. Same logical rows, two
    packs, one comma- and one semicolon-delimited CSV; the resulting
    entries must agree on everything but the generated entry_key/pack_id."""
    row = {
        "entry_key": None, "canonical_q": "Kdy je konzultační den?",
        "answer_md": "Každé první úterý v měsíci od 16 do 18 hodin.",
        "variants": ["Kdy jsou konzultace?"], "sources": [],
        "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True,
    }
    comma_pack = _pack_id("delim-comma")
    semi_pack = _pack_id("delim-semi")
    comma_csv = _csv_bytes([row], delimiter=",")
    semi_csv = _csv_bytes([row], delimiter=";")

    # sanity on the fixtures themselves before touching the API
    comma_header = comma_csv.decode("utf-8-sig").split("\r\n", 1)[0]
    semi_header = semi_csv.decode("utf-8-sig").split("\r\n", 1)[0]
    assert comma_header.split(",") == CSV_HEADER
    assert semi_header.split(";") == CSV_HEADER

    results = {}
    for pack_id, csv_bytes in ((comma_pack, comma_csv), (semi_pack, semi_csv)):
        preview = client.post(
            f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
            data={"pack_id": pack_id, "mode": "merge",
                  "pack_metadata_by_pack": json.dumps({pack_id: DEFAULT_METADATA})},
            files={"file": (f"{pack_id}.csv", csv_bytes, "text/csv")},
        )
        assert preview.status_code == 200, preview.text
        (pinfo,) = preview.json()["packs"]
        assert pinfo["ok"] is True, pinfo
        assert pinfo["publishable"] is True

        pub = client.post(
            f"/qa-api/v1/schools/{SCHOOL}/imports/{preview.json()['preview_id']}/publish",
            json={"expected_etags": {pack_id: ""}})
        assert pub.status_code == 202, pub.text
        op = _wait_operation(client, pub.json()["operation_id"])
        (pack_status,) = op["packs"]
        assert pack_status["status"] == "ok", pack_status

        entries = _entries(client, pack_id)
        assert entries["total"] == 1
        results[pack_id] = entries["entries"][0]

    comma_entry = results[comma_pack]
    semi_entry = results[semi_pack]
    for field in ("canonical_q", "answer_md", "variants", "sources",
                  "is_active", "visibility"):
        assert comma_entry[field] == semi_entry[field], field
    assert sorted(comma_entry["domains"]) == sorted(semi_entry["domains"])
    assert sorted(comma_entry["audience_roles"]) == sorted(semi_entry["audience_roles"])


# ── 4. no partial write: one bad domain code in an import -> 422, pack ────
# ── etag and every entry unchanged ─────────────────────────────────────────

def test_import_with_one_unknown_domain_code_writes_nothing(client):
    pack_id = _pack_id("badrow")
    good_entry = {
        "entry_key": None, "canonical_q": "Kdy je den otevřených dveří?",
        "answer_md": "Vždy v listopadu.", "variants": [], "sources": [],
        "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True,
    }
    _create_pack_direct(client, pack_id, [good_entry], DEFAULT_METADATA)
    before = _entries(client, pack_id)
    existing_key = before["entries"][0]["entry_key"]

    bad_csv = _csv_bytes([{
        "entry_key": existing_key, "canonical_q": "Upravena otazka?",
        "answer_md": "Upravena odpoved.", "variants": [], "sources": [],
        "domains": ["NOPE_NOT_A_DOMAIN"], "category_code": None, "is_active": True,
    }])
    resp = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
        data={"pack_id": pack_id, "mode": "merge"},
        files={"file": (f"{pack_id}.csv", bad_csv, "text/csv")},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"]
    assert "preview_id" not in body

    after = _entries(client, pack_id)
    assert after["pack_etag"] == before["pack_etag"]
    assert after["entries"] == before["entries"]


# ── 5. merge mode never deactivates an entry it simply omits ──────────────

def test_merge_import_omitting_an_entry_keeps_it_active(client):
    pack_id = _pack_id("merge")
    entry_1 = {
        "entry_key": None, "canonical_q": "Kdy jsou třídní schůzky?",
        "answer_md": "Vždy třetí čtvrtek v měsíci.", "variants": [], "sources": [],
        "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True,
    }
    _create_pack_direct(client, pack_id, [entry_1], DEFAULT_METADATA)
    after_first = _entries(client, pack_id)
    key_1 = after_first["entries"][0]["entry_key"]

    entry_2 = {
        "entry_key": None, "canonical_q": "Kde najdu rozvrh hodin?",
        "answer_md": "Na webu školy v sekci Rozvrhy.", "variants": [], "sources": [],
        "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True,
    }
    body = {"base_etag": after_first["pack_etag"], "entries": [entry_2]}
    resp = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/preview", json=body)
    assert resp.status_code == 200, resp.text
    preview_id = resp.json()["preview_id"]
    pub = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/publish",
                       json={"preview_id": preview_id, "expected_etag": after_first["pack_etag"]})
    assert pub.status_code == 202, pub.text
    op = _wait_operation(client, pub.json()["operation_id"])
    assert op["status"] == "completed", op

    two_entries = _entries(client, pack_id)
    assert two_entries["total"] == 2
    key_2 = next(e["entry_key"] for e in two_entries["entries"] if e["entry_key"] != key_1)

    # merge-import a CSV containing ONLY entry_1
    only_first_csv = _csv_bytes([{
        "entry_key": key_1, "canonical_q": entry_1["canonical_q"],
        "answer_md": entry_1["answer_md"], "variants": [], "sources": [],
        "domains": entry_1["domains"], "category_code": None, "is_active": True,
    }])
    preview = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/preview",
        data={"pack_id": pack_id, "mode": "merge"},
        files={"file": (f"{pack_id}.csv", only_first_csv, "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    (pinfo,) = preview.json()["packs"]
    assert pinfo["ok"] is True
    assert pinfo["diff"]["deactivated"] == []
    assert key_2 not in pinfo["diff"].get("changed", [])

    pub2 = client.post(
        f"/qa-api/v1/schools/{SCHOOL}/imports/{preview.json()['preview_id']}/publish",
        json={"expected_etags": {pack_id: two_entries["pack_etag"]}})
    assert pub2.status_code == 202, pub2.text
    op2 = _wait_operation(client, pub2.json()["operation_id"])
    assert op2["status"] == "completed", op2

    final = _entries(client, pack_id)
    by_key = {e["entry_key"]: e for e in final["entries"]}
    assert by_key[key_1]["is_active"] is True
    assert key_2 in by_key
    assert by_key[key_2]["is_active"] is True  # the omitted entry stayed active


# ── 6. stale etag: publishing a second preview built on the old etag ──────
# ── is rejected and changes nothing ────────────────────────────────────────

def test_stale_etag_publish_is_rejected(client):
    pack_id = _pack_id("stale")
    entry = {
        "entry_key": None, "canonical_q": "Kde je šatna?", "answer_md": "V přízemí vlevo.",
        "variants": [], "sources": [], "domains": ["SCHOOL_OPERATIONS"],
        "category_code": None, "is_active": True,
    }
    _create_pack_direct(client, pack_id, [entry], DEFAULT_METADATA)
    v1 = _entries(client, pack_id)
    key = v1["entries"][0]["entry_key"]
    old_etag = v1["pack_etag"]

    # preview A, built on the current (old) etag, but NOT published yet
    edit_a = {"entry_key": key, "canonical_q": "Kde je šatna? (upraveno A)",
              "answer_md": "V přízemí vlevo, otevřeno od 7:30.", "variants": [], "sources": [],
              "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True}
    preview_a = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/preview",
                             json={"base_etag": old_etag, "entries": [edit_a]})
    assert preview_a.status_code == 200, preview_a.text

    # someone else publishes preview B first, moving the pack to a new etag
    edit_b = {"entry_key": key, "canonical_q": "Kde je šatna? (upraveno B)",
              "answer_md": "V přízemí vlevo, otevřeno od 7:00.", "variants": [], "sources": [],
              "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True}
    preview_b = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/preview",
                             json={"base_etag": old_etag, "entries": [edit_b]})
    assert preview_b.status_code == 200, preview_b.text
    pub_b = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/publish",
                         json={"preview_id": preview_b.json()["preview_id"], "expected_etag": old_etag})
    assert pub_b.status_code == 202, pub_b.text
    op_b = _wait_operation(client, pub_b.json()["operation_id"])
    assert op_b["status"] == "completed", op_b

    after_b = _entries(client, pack_id)
    assert after_b["pack_etag"] != old_etag

    # now try to publish preview A, still carrying the OLD etag
    pub_a = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/publish",
                         json={"preview_id": preview_a.json()["preview_id"], "expected_etag": old_etag})
    assert pub_a.status_code == 409, pub_a.text
    assert pub_a.json()["error"]["code"] == "PACK_VERSION_CONFLICT"

    after_a_attempt = _entries(client, pack_id)
    assert after_a_attempt == after_b  # nothing changed by the rejected publish
    assert after_a_attempt["entries"][0]["answer_md"] == edit_b["answer_md"]


# ── 7. owner and metadata are preserved across a re-publish ────────────────

def test_republish_preserves_owner_and_metadata(client):
    pack_id = _pack_id("owner")
    metadata = {"visibility": "organization", "audience_roles": ["TEACHER", "STUDENT"],
                "school_grade_codes": ["5"]}
    entry = {
        "entry_key": None, "canonical_q": "Jak nahlásit ztrátu žákovské knížky?",
        "answer_md": "Nahlaste to třídnímu učiteli.", "variants": [], "sources": [],
        "domains": ["SCHOOL_OPERATIONS"], "category_code": None, "is_active": True,
    }
    _create_pack_direct(client, pack_id, [entry], metadata)
    before = _entries(client, pack_id)
    stored = before["entries"][0]
    key = stored["entry_key"]
    owner_before = stored["owner_user_id"]
    assert owner_before  # the technical uploader account, but must not change below
    visibility_before = stored["visibility"]
    roles_before = stored["audience_roles"]

    zip_before, _ = _export(client, [pack_id], "zip")
    grades_before = _zip_manifest(zip_before)["packs"][0]["metadata"]["school_grade_codes"]

    # re-publish an edit through the ordinary editor path (not metadata!)
    edited = dict(entry, entry_key=key, answer_md="Nahlaste to třídnímu učiteli nebo v kanceláři školy.")
    preview = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/preview",
                           json={"base_etag": before["pack_etag"], "entries": [edited]})
    assert preview.status_code == 200, preview.text
    pub = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack_id}/publish",
                       json={"preview_id": preview.json()["preview_id"],
                             "expected_etag": before["pack_etag"]})
    assert pub.status_code == 202, pub.text
    op = _wait_operation(client, pub.json()["operation_id"])
    assert op["status"] == "completed", op

    after = _entries(client, pack_id)
    stored_after = after["entries"][0]
    assert stored_after["answer_md"] == edited["answer_md"]  # the edit actually applied
    assert stored_after["owner_user_id"] == owner_before
    assert stored_after["visibility"] == visibility_before
    assert sorted(stored_after["audience_roles"]) == sorted(roles_before)

    zip_after, _ = _export(client, [pack_id], "zip")
    grades_after = _zip_manifest(zip_after)["packs"][0]["metadata"]["school_grade_codes"]
    assert sorted(grades_after) == sorted(grades_before)


# ── 8. cross-school isolation: publishing into one school must not touch ──
#      another school's pack, and a pack may not be read through the wrong
#      school's path (CONTRACT.md §2.1, §9).

OTHER_SCHOOL = "ZAKLADNI_SKOLA_LIPOVA"


def test_publish_does_not_touch_another_school(client):
    """The highest-risk failure for a multi-school admin tool: one school's
    publish silently reaching into another school's data."""
    other_packs = client.get(f"/qa-api/v1/schools/{OTHER_SCHOOL}/packs")
    assert other_packs.status_code == 200, other_packs.text
    before = {p["pack_id"]: p["pack_etag"] for p in other_packs.json()["packs"]}

    pack_id = _pack_id("cross")
    _create_pack_direct(client, pack_id, [{
        "entry_key": None, "canonical_q": "Kdo vydává potvrzení o studiu?",
        "answer_md": "Kancelář školy, obvykle do tří dnů.",
        "variants": [], "sources": [], "domains": ["SCHOOL_OPERATIONS"],
        "category_code": None, "is_active": True}],
        {"visibility": "entity", "audience_roles": ["PARENT"],
         "school_grade_codes": []})

    after = client.get(f"/qa-api/v1/schools/{OTHER_SCHOOL}/packs")
    assert after.status_code == 200, after.text
    assert {p["pack_id"]: p["pack_etag"] for p in after.json()["packs"]} == before, (
        "publishing into one school changed another school's packs")
    assert pack_id not in {p["pack_id"] for p in after.json()["packs"]}


def test_pack_is_not_readable_through_another_schools_path(client):
    """The new pack belongs to SCHOOL; asking for it under OTHER_SCHOOL must
    not return its entries, whatever the policy says about admin breadth."""
    pack_id = _pack_id("scope")
    _create_pack_direct(client, pack_id, [{
        "entry_key": None, "canonical_q": "Kde najdu jídelníček?",
        "answer_md": "Na webu školy v sekci Jídelna.",
        "variants": [], "sources": [], "domains": ["SCHOOL_OPERATIONS"],
        "category_code": None, "is_active": True}],
        {"visibility": "entity", "audience_roles": ["PARENT"],
         "school_grade_codes": []})

    mine = _entries(client, pack_id)
    assert mine["entries"], "precondition: the pack exists for its own school"

    foreign = client.get(
        f"/qa-api/v1/schools/{OTHER_SCHOOL}/packs/{pack_id}/entries")
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
