"""Fake SearchClient — same method names as the Protocol in adapters/search.py.
No network in this suite (CONTRACT.md §9); this fakes the internal qa-admin
router and the existing public /v1/* endpoints in one object.
"""
from __future__ import annotations

import copy
import uuid as _uuid

from app.adapters.search import (SearchBadRequest, SearchConflict, SearchNotFound,
                                 SearchServerError, SearchUnavailable)

ENTITIES = [
    {"legal_entity_code": "ZS_DEMO", "display_name": "ZS Demo", "kind": "school",
     "level": "zs", "parent_code": None},
]

DOMAINS = [{"code": "PROVOZ", "labels": {"cs": "Provoz"}, "sort_order": 1}]

CATEGORIES = [{"code": "GENERAL", "labels": {"cs": "Obecne"}, "is_active": True, "sort_order": 1}]

CODE_LISTS = {
    "role": [
        {"code": "TEACHER", "labels": {"cs": "Ucitel"}},
        {"code": "PARENT", "labels": {"cs": "Rodic"}},
        {"code": "STUDENT", "labels": {"cs": "Zak"}},
    ],
    "school_grade": [{"code": str(n), "labels": {"cs": f"{n}. rocnik"}} for n in range(1, 10)],
    "school_subject": [{"code": "MAT", "labels": {"cs": "Matematika"}},
                        {"code": "CJL", "labels": {"cs": "Cesky jazyk"}}],
    "language": [{"code": "cs", "labels": {"cs": "Cestina"}},
                 {"code": "en", "labels": {"cs": "Anglictina", "en": "English"}}],
}

DOCUMENT_TYPES = [
    {"type_code": "SCHOOL_RULES", "is_ui_default": False, "labels": {"cs": "Skolni rad"}},
    {"type_code": "OTHER", "is_ui_default": True, "labels": {"cs": "Jine"}},
]

VALIDITY_PERIODS = [
    {"id": 1, "legal_entity_code": "ZS_DEMO", "name": "Skolni rok 2026/27",
     "valid_from": "2026-09-01", "valid_to": "2027-08-31", "is_default": True},
]

DOC_UUID = "aaaaaaaa-0000-0000-0000-000000000001"

DOCUMENT = {
    "file_uuid": DOC_UUID, "title": "skolni_rad.pdf", "source_kind": "file",
    "source_url": None, "original_file_name": "skolni_rad.pdf",
    "document_type": "SCHOOL_RULES", "document_language": "cs", "school_subject": None,
    "school_grade_codes": [], "domain_codes": ["PROVOZ"], "audience_roles": ["PARENT"],
    "visibility": "entity", "valid_from": None, "valid_to": None,
    "user_name": "QA portal", "ingested_at": "2026-11-01T10:00:00+00:00",
    "is_active": True, "version_count": 1, "chunk_count": 12,
    "validity_state": "current", "file_format": "pdf", "content_sha256": None,
}

EXPLAIN_RESPONSE = {
    "query_norm_tokens": ["kdy"], "query_lemma_tokens": [], "query_stem_tokens": [],
    "query_language": "cs", "query_in_corpus": True, "query_absent_concepts": [],
    "pack_ids": ["faq"], "offer_pack_ids": [], "resolved": None, "trace_detail": {},
}

ENTRY_BY_ID = {"pack_id": "faq", "entry_key": "k1", "canonical_q": "Kdy zacina rok?",
               "is_active": True}
ENTRY_ID = "11111111-1111-1111-1111-111111111111"
STATS = {"active": 3, "inactive": 1, "expired": 2, "expiring_30": 1, "future": 0,
         "qa_entries_active": 17}
CHUNKS = [{"chunk_index": i, "heading_path": f"1.{i}", "section_heading": None,
           "text": f"text {i}", "char_count": 6} for i in range(5)]

ASK_RESPONSE = {
    "answer": "Skolni rok zacina **1. zari**.", "outcome": "answer",
    "answer_source": "qa_exact", "delivered_by": "online", "strategies": ["qa_exact"],
    "suggestions": [{"kind": "qa", "question": "Kdy konci rok?", "entry_id": None,
                     "score": 0.8}],
    "matched_entry_id": "11111111-1111-1111-1111-111111111111", "match_score": 1.0,
    "sources": [{"rank": 1, "title": "Skolni rad", "url": "https://zs.example/rad"}],
    "language": "cs", "duration_ms": 42, "trace": [],
}

PACKS = [{"pack_id": "faq", "version": 3, "lang": "cs", "built_at": None, "entry_count": 1},
         {"pack_id": "faq2", "version": 1, "lang": "cs", "built_at": None, "entry_count": 1}]

ENTRY = {
    "entry_key": "k1", "entry_id": "11111111-1111-1111-1111-111111111111",
    "canonical_q": "Kdy zacina rok?", "answer_md": "V zari.", "variants": [],
    "sources": [], "domains": ["PROVOZ"], "category_code": "GENERAL",
    "category_label": "Obecne", "category_is_active": True, "cat_legacy": None,
    "owner_user_id": "qa_portal__ZS_DEMO", "visibility": "public", "audience_roles": None,
    "school_grade_ids": None, "school_subject_ids": None, "valid_from": None,
    "valid_to": None, "is_active": True,
}

# A second pack, used by the import/export tests exercising a two-pack ZIP.
ENTRY2 = {
    "entry_key": "k2", "entry_id": "22222222-2222-2222-2222-222222222222",
    "canonical_q": "Kde je jidelna?", "answer_md": "V pravem kridle.", "variants": [],
    "sources": [], "domains": ["PROVOZ"], "category_code": "GENERAL",
    "category_label": "Obecne", "category_is_active": True, "cat_legacy": None,
    "owner_user_id": "qa_portal__ZS_DEMO", "visibility": "public", "audience_roles": None,
    "school_grade_ids": None, "school_subject_ids": None, "valid_from": None,
    "valid_to": None, "is_active": True,
}


class FakeSearchClient:
    """Happy-path fake. raise_unavailable_on toggles a SearchUnavailable for
    one named method, to exercise the upstream-error path.

    Mutable per-instance state (packs/entries/categories) so the write-side
    tests can preview/publish and then observe the resulting pack_etag move,
    same as the real internal-qa-admin router would report it.
    """

    def __init__(self, raise_unavailable_on: str | None = None,
                 raise_server_error_on: str | None = None):
        self.raise_unavailable_on = raise_unavailable_on
        # An upstream HTTP 500 for one named method (CONTRACT.md §14.8: 502).
        self.raise_server_error_on = raise_server_error_on
        self._packs: dict[str, dict[str, dict]] = {
            # A pack with entries ALWAYS has metadata in the real internal API —
            # it is derived from the entries themselves. The empty dict this used
            # to carry was a shape the server never produces, and it hid the fact
            # that publish was silently defaulting visibility/audience_roles.
            "ZS_DEMO": {"faq": {"pack_etag": "etag-1", "pack_version": 3,
                                 "metadata_uniform": True,
                                 "metadata": {"visibility": "entity",
                                              "audience_roles": ["TEACHER"],
                                              "school_grade_codes": ["1"]}},
                        "faq2": {"pack_etag": "etag2-1", "pack_version": 1,
                                 "metadata_uniform": True,
                                 "metadata": {"visibility": "entity",
                                              "audience_roles": ["PARENT"],
                                              "school_grade_codes": []}}},
        }
        self._entries: dict[str, dict[str, list[dict]]] = {
            "ZS_DEMO": {"faq": [dict(ENTRY)], "faq2": [dict(ENTRY2)]},
        }
        self._categories: dict[str, dict] = {c["code"]: dict(c) for c in CATEGORIES}
        self._etag_seq = 1
        # Documents per school (CONTRACT.md §14.2); calls recorded for asserts.
        self._documents: dict[str, dict[str, dict]] = {
            "ZS_DEMO": {DOC_UUID: copy.deepcopy(DOCUMENT)}}
        self.document_calls: list[tuple] = []
        # §14.8 filters of each list call: {type_code, role, validity, source}
        self.document_filters: list[dict] = []
        self.document_formats: list[str | None] = []   # §14.10 `format` of each list call
        # Try a question (§14.9): payloads received, the canned answer and an
        # optional exception (e.g. SearchBadRequest) for the next call.
        self.ask_calls: list[dict] = []
        self.ask_response: dict = copy.deepcopy(ASK_RESPONSE)
        self.ask_error: Exception | None = None
        # §14.11-§14.14: explain, entry lookup, stats, chunks, content hashes.
        self.explain_calls: list[dict] = []
        self.explain_response: dict = copy.deepcopy(EXPLAIN_RESPONSE)
        self.explain_error: Exception | None = None
        self.entry_calls: list[tuple] = []
        self.stats_calls: list[str] = []
        self.chunk_calls: list[tuple] = []
        self.hash_calls: list[tuple] = []
        self.hash_error: Exception | None = None

    def _maybe_raise(self, name: str) -> None:
        if self.raise_server_error_on == name:
            raise SearchServerError("500: fake permission denied")
        if self.raise_unavailable_on == name:
            raise SearchUnavailable("fake upstream outage")

    def list_active_entities(self) -> list[dict]:
        self._maybe_raise("list_active_entities")
        return ENTITIES

    def get_domains(self, legal_entity: str) -> list[dict]:
        self._maybe_raise("get_domains")
        return DOMAINS

    def get_code_lists(self, legal_entity: str) -> dict[str, list[dict]]:
        self._maybe_raise("get_code_lists")
        return CODE_LISTS

    def list_qa_packs(self, legal_entity: str) -> list[dict]:
        self._maybe_raise("list_qa_packs")
        return PACKS

    def get_pack_state(self, legal_entity: str, pack_id: str) -> dict:
        self._maybe_raise("get_pack_state")
        from app.adapters.search import SearchNotFound
        state = self._packs.get(legal_entity, {}).get(pack_id)
        if state is None:
            raise SearchNotFound(f"no such pack {pack_id!r} for {legal_entity!r}")
        return state

    def list_entries(self, legal_entity: str, pack_id: str, limit: int,
                      cursor: str | None, lang: str | None) -> dict:
        self._maybe_raise("list_entries")
        from app.adapters.search import SearchNotFound
        state = self._packs.get(legal_entity, {}).get(pack_id)
        if state is None:
            raise SearchNotFound(f"no such pack {pack_id!r} for {legal_entity!r}")
        entries = self._entries.get(legal_entity, {}).get(pack_id, [])
        return {"entries": entries, "next_cursor": None, "total": len(entries),
                "pack_etag": state["pack_etag"], "pack_version": state["pack_version"],
                "metadata_uniform": state.get("metadata_uniform", True)}

    def get_uploader(self, legal_entity: str) -> dict:
        self._maybe_raise("get_uploader")
        return {"user_id": f"qa_portal__{legal_entity}",
                "display_name": f"QA portal ({legal_entity})", "created": False}

    def list_categories(self, lang: str | None, include_inactive: bool) -> list[dict]:
        self._maybe_raise("list_categories")
        cats = self._categories.values()
        return [c for c in cats if include_inactive or c["is_active"]]

    def create_category(self, payload: dict) -> dict:
        code = payload["code"]
        if code in self._categories:
            raise SearchConflict({"error": {
                "code": "CATEGORY_CODE_TAKEN",
                "message": f"category code {code!r} already exists", "retryable": False}})
        rec = {"id": len(self._categories) + 1, "code": code,
               "labels": payload.get("labels") or {}, "is_active": True,
               "sort_order": payload.get("sort_order") or 0, "notes": payload.get("notes")}
        self._categories[code] = rec
        return rec

    # ── documents (CONTRACT.md §14.2) ──────────────────────────────────────
    def _doc(self, legal_entity: str, file_uuid: str) -> dict:
        try:
            fu = str(_uuid.UUID(file_uuid))
        except ValueError:
            raise SearchBadRequest({"error": {"code": "BAD_REQUEST", "message": "not a UUID"}})
        doc = self._documents.get(legal_entity, {}).get(fu)
        if doc is None:
            raise SearchNotFound("no such document")
        return doc

    def list_documents(self, legal_entity: str, status: str, q: str | None,
                       limit: int, cursor: str | None, *, type_code: str | None = None,
                       role: str | None = None, validity: str | None = None,
                       source: str | None = None, file_format: str | None = None) -> dict:
        self._maybe_raise("list_documents")
        self.document_formats.append(file_format)
        self.document_calls.append(("list", legal_entity, status, q, limit, cursor))
        self.document_filters.append({"type_code": type_code, "role": role,
                                      "validity": validity, "source": source})
        docs = list(self._documents.get(legal_entity, {}).values())
        if status == "active":
            docs = [d for d in docs if d["is_active"]]
        elif status == "inactive":
            docs = [d for d in docs if not d["is_active"]]
        if q:
            docs = [d for d in docs if q.lower() in (d["title"] or "").lower()]
        if type_code:
            docs = [d for d in docs if d["document_type"] == type_code]
        if role:
            docs = [d for d in docs if role in d["audience_roles"]]
        if validity and validity != "all":
            docs = [d for d in docs if d.get("validity_state", "current") == validity]
        if file_format:
            docs = [d for d in docs if d.get("file_format") == file_format]
        if source:
            docs = [d for d in docs
                    if source in (d["source_url"], d["original_file_name"])]
        return {"documents": copy.deepcopy(docs[:limit]), "next_cursor": None}

    def get_document(self, legal_entity: str, file_uuid: str) -> dict:
        self._maybe_raise("get_document")
        self.document_calls.append(("get", legal_entity, file_uuid))
        doc = self._doc(legal_entity, file_uuid)
        return {"document": copy.deepcopy(doc), "versions": [
            {"file_uuid": doc["file_uuid"], "processing_uuid": None,
             "ingested_at": doc["ingested_at"], "is_active": doc["is_active"],
             "chunk_count": doc["chunk_count"], "file_format": doc.get("file_format"),
             "content_sha256": doc.get("content_sha256"),
             # search-internal: the portal must strip it (§14.10)
             "file_name": f"{doc['file_uuid']}_{doc['title']}"}]}

    def update_document_validity(self, legal_entity: str, file_uuid: str,
                                 valid_from: str | None, valid_to: str | None) -> dict:
        self._maybe_raise("update_document_validity")
        self.document_calls.append(("validity", legal_entity, file_uuid, valid_from, valid_to))
        doc = self._doc(legal_entity, file_uuid)
        doc["valid_from"], doc["valid_to"] = valid_from, valid_to
        return {"document": copy.deepcopy(doc)}

    def deactivate_document(self, legal_entity: str, file_uuid: str) -> dict:
        self._maybe_raise("deactivate_document")
        self.document_calls.append(("deactivate", legal_entity, file_uuid))
        doc = self._doc(legal_entity, file_uuid)
        doc["is_active"] = False
        return {"document": copy.deepcopy(doc)}

    def activate_document(self, legal_entity: str, file_uuid: str) -> dict:
        self._maybe_raise("activate_document")
        self.document_calls.append(("activate", legal_entity, file_uuid))
        doc = self._doc(legal_entity, file_uuid)
        doc["is_active"] = True
        return {"document": copy.deepcopy(doc)}

    def get_document_types(self) -> list[dict]:
        self._maybe_raise("get_document_types")
        return copy.deepcopy(DOCUMENT_TYPES)

    def get_validity_periods(self, legal_entity: str) -> list[dict]:
        self._maybe_raise("get_validity_periods")
        return copy.deepcopy(VALIDITY_PERIODS)

    def ask(self, payload: dict) -> dict:
        self._maybe_raise("ask")
        self.ask_calls.append(copy.deepcopy(payload))
        if self.ask_error is not None:
            raise self.ask_error
        return copy.deepcopy(self.ask_response)

    def explain(self, payload: dict) -> dict:
        self._maybe_raise("explain")
        self.explain_calls.append(copy.deepcopy(payload))
        if self.explain_error is not None:
            raise self.explain_error
        return copy.deepcopy(self.explain_response)

    def get_entry_by_id(self, legal_entity: str, entry_id: str) -> dict:
        self._maybe_raise("get_entry_by_id")
        self.entry_calls.append((legal_entity, entry_id))
        if entry_id != ENTRY_ID:
            raise SearchNotFound("no such entry")
        return dict(ENTRY_BY_ID)

    def get_document_stats(self, legal_entity: str) -> dict:
        self._maybe_raise("get_document_stats")
        self.stats_calls.append(legal_entity)
        return dict(STATS)

    def list_document_chunks(self, legal_entity: str, file_uuid: str, limit: int,
                             cursor: str | None) -> dict:
        self._maybe_raise("list_document_chunks")
        self.chunk_calls.append((legal_entity, file_uuid, limit, cursor))
        self._doc(legal_entity, file_uuid)
        start = int(cursor or 0)
        page = CHUNKS[start:start + limit]
        nxt = str(start + limit) if start + limit < len(CHUNKS) else None
        return {"chunks": copy.deepcopy(page), "total": len(CHUNKS), "next_cursor": nxt}

    def update_document_content_hash(self, legal_entity: str, file_uuid: str,
                                     content_sha256: str) -> dict:
        self._maybe_raise("update_document_content_hash")
        self.hash_calls.append((legal_entity, file_uuid, content_sha256))
        if self.hash_error is not None:
            raise self.hash_error
        doc = self._doc(legal_entity, file_uuid)
        doc["content_sha256"] = content_sha256
        return {"document": copy.deepcopy(doc)}

    # ── test-only helpers, mimicking what a real ingest run changes ────────
    def bump_pack(self, legal_entity: str, pack_id: str, entries: list[dict]) -> str:
        """Simulates the search-side effect of a successful ingest: a new
        pack_etag and the entries as ingest would have stored them."""
        self._etag_seq += 1
        new_etag = f"etag-{self._etag_seq}"
        school_packs = self._packs.setdefault(legal_entity, {})
        prior = school_packs.get(pack_id, {})
        school_packs[pack_id] = {**prior, "pack_etag": new_etag,
                                  "pack_version": prior.get("pack_version", 0) + 1,
                                  "metadata_uniform": True, "metadata": prior.get("metadata", {})}
        self._entries.setdefault(legal_entity, {})[pack_id] = [dict(e) for e in entries]
        return new_etag
