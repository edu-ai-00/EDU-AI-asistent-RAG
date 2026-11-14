"""Fake IngestClient — same method names as the Protocol in adapters/ingest.py.
Records every call it received (for asserting on the CSV bytes actually sent)
and, on success, tells the paired FakeSearchClient to move the pack's etag —
mirroring what a real ingest run does upstream, so a test can assert on a
truthful post-publish GET rather than only on "we called ingest".
"""
from __future__ import annotations

import csv
import io

from app.adapters.ingest import IngestNotFound, IngestRejected, IngestUnavailable, StoredFile
from app.services.csv_io import HEADER_LEGACY, HEADER_NEW, _read_header


class FakeIngestClient:
    def __init__(self, search=None, *, fail: str | None = None):
        self.search = search
        self.received: list[dict] = []
        # fail: None (succeed), "rejected" (IngestRejected), "unavailable" (IngestUnavailable)
        self.fail = fail
        # Documents (CONTRACT.md §14.3-§14.5).
        self.document_received: list[dict] = []
        self.document_result: dict = {"processing_uuid": "99999999-0000-0000-0000-000000000001",
                                      "status": "completed", "total_files": 1,
                                      "ok_count": 1, "error_count": 0, "files": []}
        self.document_fail: str | None = None   # None, "rejected", "unavailable", "crash"
        # URL preview (CONTRACT.md §14.8): calls and the dry-run answer.
        self.preview_received: list[dict] = []
        self.preview_fail: str | None = None    # None, "rejected", "unavailable"
        self.preview_result: dict = {"status": "dry_run", "files": [
            {"file_uuid": "x", "file_name": "p.html", "file_path": "/data/uploads/x_p.html",
             "source_url": "https://skola.example/", "source_type": "web",
             "page_role": "parent", "status": "dry_run"},
            {"file_uuid": "y", "file_name": "c.pdf", "file_path": "/data/uploads/y_c.pdf",
             "source_url": "https://skola.example/a.pdf", "source_type": "pdf",
             "page_role": "child", "status": "dry_run"}]}
        self.delete_calls: list[tuple[str, str]] = []
        self.delete_fail: str | None = None     # None, "not_found", "rejected", "unavailable"
        # Stored-file download (§14.10): calls, failure mode, the stream served.
        self.download_calls: list[tuple[str, str]] = []
        self.download_fail: str | None = None   # None, "not_found", "rejected", "unavailable"
        self.download_chunks: list[bytes] = [b"%PDF-1.7 ", b"fake bytes"]
        self.download_disposition: str | None = 'attachment; filename="skolni_rad.pdf"'
        self.download_closed = 0
        self.status_by_uuid: dict[str, dict | None] = {}
        self.status_calls: list[str] = []
        self.status_fail = False

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
        self.status_calls.append(processing_uuid)
        if self.status_fail:
            raise IngestUnavailable("fake status outage")
        if processing_uuid in self.status_by_uuid:
            return self.status_by_uuid[processing_uuid]
        return {"status": "completed", "processing_uuid": processing_uuid}

    def run_document_ingest(self, *, legal_entity: str, user_id: str, visibility: str,
                            audience_roles: list[str], file=None, filename=None, url=None,
                            save_level_1_child: bool = False, max_child_links=None,
                            document_type=None, document_language=None, school_subject=None,
                            school_grade_codes=None, domain_codes=None,
                            valid_from=None, valid_to=None) -> dict:
        content = file.read() if hasattr(file, "read") else file
        self.document_received.append({
            "legal_entity": legal_entity, "user_id": user_id, "visibility": visibility,
            "audience_roles": audience_roles, "file": content, "filename": filename,
            "url": url, "save_level_1_child": save_level_1_child,
            "max_child_links": max_child_links, "document_type": document_type,
            "document_language": document_language, "school_subject": school_subject,
            "school_grade_codes": school_grade_codes, "domain_codes": domain_codes,
            "valid_from": valid_from, "valid_to": valid_to,
        })
        if self.document_fail == "rejected":
            raise IngestRejected(400, {"detail": "Unknown document_type"})
        if self.document_fail == "unavailable":
            raise IngestUnavailable("fake ingest outage")
        if self.document_fail == "crash":
            raise RuntimeError("boom")
        return dict(self.document_result)

    def preview_document_url(self, *, legal_entity: str, user_id: str, visibility: str,
                             audience_roles: list[str], url: str,
                             save_level_1_child: bool = False, max_child_links=None,
                             document_type=None, document_language=None, school_subject=None,
                             school_grade_codes=None, domain_codes=None,
                             valid_from=None, valid_to=None) -> dict:
        self.preview_received.append({
            "dry_run": True, "legal_entity": legal_entity, "user_id": user_id,
            "visibility": visibility, "audience_roles": audience_roles, "url": url,
            "save_level_1_child": save_level_1_child, "max_child_links": max_child_links,
            "document_type": document_type, "document_language": document_language,
            "school_subject": school_subject, "school_grade_codes": school_grade_codes,
            "domain_codes": domain_codes, "valid_from": valid_from, "valid_to": valid_to})
        if self.preview_fail == "rejected":
            raise IngestRejected(400, {"detail": "URL could not be fetched"})
        if self.preview_fail == "unavailable":
            raise IngestUnavailable("fake gateway outage")
        return dict(self.preview_result)

    def delete_document(self, legal_entity: str, file_uuid: str) -> dict:
        self.delete_calls.append((legal_entity, file_uuid))
        if self.delete_fail == "not_found":
            raise IngestNotFound({"detail": "document not found"})
        if self.delete_fail == "rejected":
            raise IngestRejected(400, {"detail": "file_uuid must be a UUID"})
        if self.delete_fail == "unavailable":
            raise IngestUnavailable("fake gateway outage")
        return {"deleted": {"versions": 2, "chunks": 20, "elements": 5, "tables": 1},
                "file_uuids": [file_uuid], "files_removed": 2, "warnings": []}

    def open_document_file(self, legal_entity: str, file_uuid: str) -> StoredFile:
        self.download_calls.append((legal_entity, file_uuid))
        if self.download_fail == "not_found":
            raise IngestNotFound({"detail": "file is not stored"})
        if self.download_fail == "rejected":
            raise IngestRejected(400, {"detail": "file_uuid must be a UUID"})
        if self.download_fail == "unavailable":
            raise IngestUnavailable("fake gateway outage")

        def close() -> None:
            self.download_closed += 1

        return StoredFile(chunks=iter(list(self.download_chunks)),
                          content_disposition=self.download_disposition, close=close)


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
