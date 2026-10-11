"""Fake IngestClient — same method names as the Protocol in adapters/ingest.py.
Records every call it received (for asserting on the CSV bytes actually sent)
and, on success, tells the paired FakeSearchClient to move the pack's etag —
mirroring what a real ingest run does upstream, so a test can assert on a
truthful post-publish GET rather than only on "we called ingest".
"""
from __future__ import annotations

import csv
import io

from app.adapters.ingest import IngestRejected, IngestUnavailable
from app.services.csv_io import HEADER_LEGACY, HEADER_NEW, _read_header


class FakeIngestClient:
    def __init__(self, search=None, *, fail: str | None = None):
        self.search = search
        self.received: list[dict] = []
        # fail: None (succeed), "rejected" (IngestRejected), "unavailable" (IngestUnavailable)
        self.fail = fail

    def run_ingest(self, *, csv_bytes: bytes, filename: str, legal_entity: str,
                    user_id: str, visibility: str, pack_id: str, qa_mode: str,
                    domains: list[str] | None = None,
                    audience_roles: list[str] | None = None,
                    school_subject_grades: list[str] | None = None,
                    valid_from: str | None = None,
                    valid_to: str | None = None) -> dict:
        self.received.append({
            "csv_bytes": csv_bytes, "filename": filename, "legal_entity": legal_entity,
            "user_id": user_id, "visibility": visibility, "pack_id": pack_id,
            "qa_mode": qa_mode, "domains": domains, "audience_roles": audience_roles,
            "school_subject_grades": school_subject_grades,
            "valid_from": valid_from, "valid_to": valid_to,
        })
        if self.fail == "rejected":
            raise IngestRejected(422, {"error": {"code": "VALIDATION_ERROR",
                                                  "message": "fake rejection", "retryable": False}})
        if self.fail == "unavailable":
            raise IngestUnavailable("fake ingest outage")

        entries = _entries_from_csv(csv_bytes)
        new_etag = None
        if self.search is not None:
            new_etag = self.search.bump_pack(legal_entity, pack_id, entries)
        return {"processing_uuid": "11111111-2222-3333-4444-555555555555",
                "status": "ok", "ok_count": len(entries), "error_count": 0,
                "result_etag": new_etag}

    def get_status(self, processing_uuid: str) -> dict | None:
        return {"status": "completed", "processing_uuid": processing_uuid}


def _entries_from_csv(csv_bytes: bytes) -> list[dict]:
    """Minimal re-parse of the pack CSV (csv_io.write_pack_csv) just enough to
    hand normalized-shaped rows back to bump_pack — this is a test double, not
    a second production CSV parser. Delimiter detection reuses csv_io's
    deterministic "," then ";" header check so both current (",") and legacy
    (";") fixtures round-trip here the same way the real parser does."""
    text = csv_bytes.decode("utf-8-sig")
    header_comma = _read_header(text, ",")
    delimiter = "," if header_comma in (HEADER_NEW, HEADER_LEGACY) else ";"
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    out = []
    for row in reader:
        out.append({
            "entry_key": row.get("entry_key") or "",
            "entry_id": None,
            "canonical_q": row.get("canonical_q") or row.get("question") or "",
            "answer_md": row.get("answer_md") or row.get("answer") or "",
            "variants": [], "sources": [], "domains": [],
            "category_code": row.get("category_code") or None,
            "category_label": None, "category_is_active": None, "cat_legacy": None,
            "owner_user_id": None, "visibility": "public", "audience_roles": None,
            "school_grade_ids": None, "school_subject_ids": None,
            "valid_from": None, "valid_to": None, "is_active": True,
        })
    return out
