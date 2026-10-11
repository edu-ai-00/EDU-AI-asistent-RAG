"""Fake SearchClient — same method names as the Protocol in adapters/search.py.
No network in this suite (CONTRACT.md §9); this fakes the internal qa-admin
router and the existing public /v1/* endpoints in one object.
"""
from __future__ import annotations

from app.adapters.search import SearchConflict, SearchUnavailable

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

    def __init__(self, raise_unavailable_on: str | None = None):
        self.raise_unavailable_on = raise_unavailable_on
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

    def _maybe_raise(self, name: str) -> None:
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
