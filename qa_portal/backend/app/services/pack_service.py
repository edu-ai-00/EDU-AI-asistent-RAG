"""Read-side application service: schools, domains, categories, packs, entries.

Every call goes through the search adapter (CONTRACT.md §9) — no SQL, no
PG_* here. This module only shapes adapter responses into the public API's
schemas (contracts/qa-portal/openapi.yaml) and applies the label fallback
rule; it never invents metadata (CONTRACT.md §2.4).
"""
from __future__ import annotations

from ..adapters.search import SearchClient, SearchNotFound, SearchUnavailable
from ..config import settings
from .. import errors
from .labels import resolve_label


def list_schools(search: SearchClient, allowed) -> list[dict]:
    try:
        entities = search.list_active_entities()
    except SearchUnavailable as e:
        raise errors.unavailable(f"entity register unavailable: {e}")
    return [
        {"code": e["legal_entity_code"], "name": e.get("display_name", e["legal_entity_code"]),
         "kind": e.get("kind"), "level": e.get("level"), "parent_code": e.get("parent_code")}
        for e in entities if allowed.allows(e["legal_entity_code"])
    ]


def is_school_active(search: SearchClient, school: str) -> bool:
    try:
        entities = search.list_active_entities()
    except SearchUnavailable as e:
        raise errors.unavailable(f"entity register unavailable: {e}")
    return any(e["legal_entity_code"] == school for e in entities)


def get_school_domains(search: SearchClient, school: str, lang: str | None) -> list[dict]:
    try:
        raw = search.get_domains(school)
    except SearchUnavailable as e:
        raise errors.unavailable(f"domain catalog unavailable: {e}")
    out = []
    for d in raw:
        label, fallback = resolve_label(d.get("labels"), lang, settings.ui_default_lang, d["code"])
        out.append({"code": d["code"], "label": label, "label_fallback": fallback,
                     "sort_order": d.get("sort_order", 0)})
    return out


def shape_category(c: dict, lang: str | None) -> dict:
    label, fallback = resolve_label(c.get("labels"), lang, settings.ui_default_lang, c["code"])
    return {"code": c["code"], "label": label, "label_fallback": fallback,
            "labels": c.get("labels") or {}, "is_active": c["is_active"],
            "sort_order": c.get("sort_order", 0), "notes": c.get("notes")}


def list_categories(search: SearchClient, lang: str | None, include_inactive: bool) -> list[dict]:
    try:
        raw = search.list_categories(lang, include_inactive)
    except SearchUnavailable as e:
        raise errors.unavailable(f"category catalog unavailable: {e}")
    return [shape_category(c, lang) for c in raw]


def category_catalog_map(search: SearchClient) -> dict[str, dict]:
    """{code: {"is_active": bool}} — for validating a NEWLY assigned code."""
    try:
        raw = search.list_categories(None, True)
    except SearchUnavailable as e:
        raise errors.unavailable(f"category catalog unavailable: {e}")
    return {c["code"]: {"is_active": c["is_active"]} for c in raw}


def domain_code_set(search: SearchClient, school: str) -> set[str]:
    try:
        raw = search.get_domains(school)
    except SearchUnavailable as e:
        raise errors.unavailable(f"domain catalog unavailable: {e}")
    return {d["code"] for d in raw}


def uploader_available(search: SearchClient, school: str) -> bool:
    try:
        search.get_uploader(school)
        return True
    except SearchNotFound:
        return False
    except SearchUnavailable:
        return False


def get_pack_state(search: SearchClient, school: str, pack: str) -> dict | None:
    try:
        return search.get_pack_state(school, pack)
    except SearchNotFound:
        return None
    except SearchUnavailable as e:
        raise errors.unavailable(f"pack state unavailable: {e}")


def list_packs(search: SearchClient, school: str) -> list[dict]:
    try:
        raw = search.list_qa_packs(school)
    except SearchUnavailable as e:
        raise errors.unavailable(f"pack list unavailable: {e}")
    has_uploader = uploader_available(search, school)
    out = []
    for p in raw:
        state = get_pack_state(search, school, p["pack_id"])
        metadata_uniform = state.get("metadata_uniform", True) if state else True
        out.append({
            "pack_id": p["pack_id"], "version": p["version"], "lang": p.get("lang"),
            "built_at": p.get("built_at"),
            "entry_count": p.get("entry_count", 0),
            "pack_etag": state["pack_etag"] if state else "",
            "metadata_uniform": metadata_uniform,
            "publishable": bool(metadata_uniform and has_uploader),
        })
    return out


def normalize_internal_entry(ie: dict) -> dict:
    """InternalEntry (adapter) -> the shape diff/csv/etag services share."""
    return {
        "entry_key": ie.get("entry_key"),
        "entry_id": ie.get("entry_id"),
        "canonical_q": ie.get("canonical_q", ""),
        "answer_md": ie.get("answer_md", ""),
        "variants": list(ie.get("variants") or []),
        "sources": list(ie.get("sources") or []),
        "domains": list(ie.get("domains") or []),
        "category_code": ie.get("category_code"),
        "category_label": ie.get("category_label"),
        "category_is_active": ie.get("category_is_active"),
        "cat_legacy": ie.get("cat_legacy"),
        "owner_user_id": ie.get("owner_user_id"),
        "visibility": ie.get("visibility"),
        "audience_roles": ie.get("audience_roles"),
        "school_grade_ids": ie.get("school_grade_ids"),
        "school_subject_ids": ie.get("school_subject_ids"),
        "valid_from": ie.get("valid_from"),
        "valid_to": ie.get("valid_to"),
        "is_active": bool(ie.get("is_active", True)),
    }


def fetch_all_entries(search: SearchClient, school: str, pack: str,
                       lang: str | None = None) -> tuple[list[dict], dict]:
    """Page through /internal/qa-admin/entries until exhausted. Returns
    (normalized entries, last page's pack_etag/pack_version/metadata_uniform)."""
    entries: list[dict] = []
    cursor = None
    page_meta: dict = {}
    try:
        while True:
            page = search.list_entries(school, pack, 200, cursor, lang)
            page_meta = {"pack_etag": page["pack_etag"], "pack_version": page["pack_version"],
                         "metadata_uniform": page.get("metadata_uniform", True)}
            entries.extend(normalize_internal_entry(e) for e in page["entries"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
    except SearchNotFound:
        raise errors.not_found(f"pack {pack!r} not found for school {school!r}")
    except SearchUnavailable as e:
        raise errors.unavailable(f"pack entries unavailable: {e}")
    return entries, page_meta


def list_entries_page(search: SearchClient, school: str, pack: str, limit: int,
                       cursor: str | None, q: str | None, lang: str | None) -> dict:
    try:
        page = search.list_entries(school, pack, limit, cursor, lang)
    except SearchNotFound:
        raise errors.not_found(f"pack {pack!r} not found for school {school!r}")
    except SearchUnavailable as e:
        raise errors.unavailable(f"pack entries unavailable: {e}")
    entries = [normalize_internal_entry(e) for e in page["entries"]]
    if q:
        needle = q.strip().lower()
        entries = [e for e in entries
                   if needle in e["canonical_q"].lower() or needle in e["answer_md"].lower()]
    return {
        "entries": entries, "next_cursor": page.get("next_cursor"),
        "total": page.get("total", len(entries)),
        "pack_etag": page["pack_etag"], "pack_version": page["pack_version"],
        "metadata_uniform": page.get("metadata_uniform", True),
    }


def invalidate_previews_for_pack(store, school: str, pack: str) -> None:
    """CONTRACT.md §13.2: drop every stored preview covering this (school,
    pack) so an older preview can never republish content just deleted. A
    "pack" preview matches by pack_id; an "import" preview may cover several
    packs, so any of its packs[] matching is enough to drop the whole preview.
    """
    for preview_id in store.list_ids("previews"):
        rec = store.get_preview(preview_id)
        if rec is None or rec.get("school") != school:
            continue
        if rec.get("kind") == "pack" and rec.get("pack_id") == pack:
            store.delete("previews", preview_id)
        elif rec.get("kind") == "import" and any(
                p.get("pack_id") == pack for p in rec.get("packs", [])):
            store.delete("previews", preview_id)


def get_entry(search: SearchClient, school: str, pack: str, entry_key: str,
              lang: str | None) -> tuple[dict | None, dict]:
    entries, meta = fetch_all_entries(search, school, pack, lang)
    for e in entries:
        if e["entry_key"] == entry_key:
            return e, meta
    return None, meta
