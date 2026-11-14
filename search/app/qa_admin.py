# written for task qa-data-c1a; /pack-state and /entries added in qa-data-c1b;
# DELETE /entries and /packs added in qa-data-delete (CONTRACT.md sec.13);
# /documents* added in qa-portal-documents (CONTRACT.md sec.14.2);
# /entries/by-id, /documents/stats, /documents/{id}/chunks, content-hash: sec.14.11-14.14.
"""Internal QA admin router — global qa_category catalog, technical uploader,
pack state (version/etag/metadata), the paginated entry detail list, the
permanent-delete endpoints and ingested-document management.

NOT public (see contracts/qa-portal/internal-qa-admin.yaml): reachable only
from the app network, called exclusively by edu_ai_qa_admin.
"""

import hashlib
import json
import re
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from . import config

internal = APIRouter(prefix="/internal/qa-admin")

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,62}$")
_ALLOWED_POST_FIELDS = {"code", "labels", "sort_order", "notes", "created_by"}


def _cursor():
    """Lazy import: psycopg2 chybí v testovacím prostředí, testy tohle nahradí."""
    from .db import get_cursor
    return get_cursor()


def _error_body(code: str, message: str, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def _error(status: int, code: str, message: str) -> JSONResponse:
    # Built directly (not via HTTPException) so the envelope is exact even when
    # this router is mounted on a bare FastAPI() without server.py's shared
    # exception handler — as the unit tests do.
    return JSONResponse(status_code=status, content=_error_body(code, message))


def _default_language() -> str:
    """UI default language, falling back hard if the registry can't load.

    Lazy import for the same reason as `_cursor` — `languages.py` pulls in
    `db.py` (psycopg2) at module scope.
    """
    try:
        from .languages import ui_default_language
        return ui_default_language() or "cs"
    except Exception:
        return "cs"


def _is_unique_violation(exc: Exception) -> bool:
    # Recognized by pgcode/class name, never by importing psycopg2 (forbidden
    # at module scope here; qa_admin.py must import without it installed).
    if getattr(exc, "pgcode", None) == "23505":
        return True
    return type(exc).__name__ in {"UniqueViolation", "IntegrityError"}


def _resolve_label(labels: dict, lang: str | None, default_lang: str, code: str):
    """requested lang -> ui default lang -> code. See CONTRACT.md sec.4.1."""
    if lang and lang in labels:
        return labels[lang], False
    if default_lang in labels:
        return labels[default_lang], bool(lang)
    return code, True


def _category_row_to_dict(row, lang: str | None, default_lang: str) -> dict:
    cat_id, code, labels, is_active, sort_order, notes = row
    labels = labels or {}
    label, label_fallback = _resolve_label(labels, lang, default_lang, code)
    return {
        "id": cat_id,
        "code": code,
        "labels": labels,
        "label": label,
        "label_fallback": label_fallback,
        "is_active": is_active,
        "sort_order": sort_order,
        "notes": notes,
    }


@internal.get("/categories")
def list_categories(lang: str | None = None, include_inactive: bool = True):
    """Global qa_category catalog: legal_entity_code IS NULL, list_code fixed.

    include_inactive defaults to True — the admin portal must see disabled
    categories too (they may still be assigned to existing entries).
    """
    default_lang = _default_language()
    query = (
        f"SELECT id, code, labels, is_active, sort_order, notes "
        f"FROM {config.PG_SCHEMA}.code_list "
        "WHERE list_code = 'qa_category' AND legal_entity_code IS NULL")
    if not include_inactive:
        query += " AND is_active"
    query += " ORDER BY sort_order, code"
    with _cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    return {"categories": [_category_row_to_dict(r, lang, default_lang) for r in rows]}


@internal.post("/categories", status_code=201)
def create_category(body: dict = Body(...)):
    """Create a global category. legal_entity_code is always NULL (215 CHECK).

    Rejects unknown body fields, an invalid code, empty/non-dict labels or a
    label language that is not an active `language` code — all as 422,
    without touching the database. A code collision (active or inactive,
    per the `code_list_qa_category_code` unique index) is 409.
    """
    extra_keys = set(body.keys()) - _ALLOWED_POST_FIELDS
    if extra_keys:
        return _error(422, "VALIDATION_ERROR",
                      f"unexpected field(s): {sorted(extra_keys)}")

    code = body.get("code")
    labels = body.get("labels")
    sort_order = body.get("sort_order")
    notes = body.get("notes")
    created_by = body.get("created_by")

    if not isinstance(code, str) or not _CODE_RE.match(code):
        return _error(422, "VALIDATION_ERROR",
                      "code must match ^[A-Z][A-Z0-9_]{1,62}$")
    if not isinstance(labels, dict) or not labels:
        return _error(422, "VALIDATION_ERROR", "labels must be a non-empty object")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        return _error(422, "VALIDATION_ERROR",
                      "labels keys and values must all be strings")
    if sort_order is not None and not isinstance(sort_order, int):
        return _error(422, "VALIDATION_ERROR", "sort_order must be an integer")

    with _cursor() as cur:
        cur.execute(
            f"SELECT code FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'language' AND is_active")
        active_langs = {r[0] for r in cur.fetchall()}
        unknown = set(labels.keys()) - active_langs
        if unknown:
            return _error(422, "VALIDATION_ERROR",
                          f"unknown language code(s) in labels: {sorted(unknown)}")

        # sort_order has no DB default (NOT NULL); 0 is the documented
        # decision when the caller does not pick a position explicitly.
        effective_sort_order = sort_order if sort_order is not None else 0
        try:
            cur.execute(
                f"INSERT INTO {config.PG_SCHEMA}.code_list "
                "(list_code, legal_entity_code, code, labels, sort_order, "
                " notes, created_by) "
                "VALUES ('qa_category', NULL, %s, %s::jsonb, %s, %s, %s) "
                "RETURNING id, code, labels, is_active, sort_order, notes",
                (code, json.dumps(labels), effective_sort_order, notes, created_by))
            row = cur.fetchone()
        except Exception as exc:
            if _is_unique_violation(exc):
                return _error(409, "CATEGORY_CODE_EXISTS",
                              f"category code '{code}' already exists")
            raise

    default_lang = _default_language()
    return _category_row_to_dict(row, None, default_lang)


@internal.get("/uploader")
def get_uploader(legal_entity: str):
    """Get-or-create the technical uploader staff_user for a school.

    Fixed convention (CONTRACT.md sec.2.2): user_id = "qa_portal__{code}".
    404s (and inserts nothing) if the entity does not exist or is inactive —
    same entity register /v1/ingest-principal relies on.
    """
    user_id = f"qa_portal__{legal_entity}"
    display_name = "QA portál (technický účet)"
    notes = ("Technický vykonavatel administračních publikací QA portálu. "
             "Není fyzická osoba.")

    with _cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {config.PG_SCHEMA}.legal_entity "
            "WHERE legal_entity_code = %s AND is_active", (legal_entity,))
        if cur.fetchone() is None:
            return _error(404, "NOT_FOUND",
                          f"unknown or inactive legal entity '{legal_entity}'")

        cur.execute(
            f"INSERT INTO {config.PG_SCHEMA}.staff_user "
            "(user_id, legal_entity_code, display_name, is_active, notes) "
            "VALUES (%s, %s, %s, true, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id, legal_entity, display_name, notes))
        created = cur.rowcount == 1

    return {"user_id": user_id, "display_name": display_name, "created": created}


# ---------------------------------------------------------------------------
# /pack-state and /entries — shared pack loading, etag and metadata (c1b)
# ---------------------------------------------------------------------------

# Tracked per CONTRACT.md sec.2.4: writer applies these to every row, so a
# pack whose rows disagree cannot be republished without silently widening
# scope. audience_roles/school_grade_ids/school_subject_ids are id ARRAYs,
# compared as sets; the rest compared by value.
_UNIFORM_SET_FIELDS = ("audience_roles", "school_grade_ids", "school_subject_ids")
_UNIFORM_VALUE_FIELDS = ("visibility", "school_level", "valid_from", "valid_to")


def _sources_list(asset_refs) -> list:
    """qa_entries.asset_refs -> InternalEntry.sources, unchanged content.

    Same input shape as server.py's _sources_cell, but returns the structured
    {title, url} objects the portal round-trips instead of a CSV cell.
    """
    if isinstance(asset_refs, str):
        try:
            asset_refs = json.loads(asset_refs)
        except (TypeError, ValueError):
            return []
    if not isinstance(asset_refs, list):
        return []
    result = []
    for item in asset_refs:
        if isinstance(item, dict):
            result.append({"title": item.get("title") or "", "url": item.get("url") or ""})
    return result


def _cat_legacy(tags) -> str | None:
    """tags->>'category' — informational only, never decides assignment."""
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (TypeError, ValueError):
            return None
    if isinstance(tags, dict):
        return tags.get("category")
    return None


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _load_pack(cur, legal_entity: str, pack_id: str):
    """One (legal_entity, pack_id): manifest row + ALL active entries, in the
    deterministic order the etag and pagination both rely on. Returns None if
    the pack does not exist, is inactive, or belongs to another entity — the
    legal_entity_code predicate is not optional (CONTRACT.md / qa_export).
    """
    cur.execute(
        f"SELECT version, built_at, lang, visibility, audience_roles, "
        f"school_grade_ids, school_subject_ids, school_level, valid_from, "
        f"valid_to, domain_ids "
        f"FROM {config.PG_SCHEMA}.qa_pack_manifest "
        "WHERE pack_id = %s AND legal_entity_code = %s AND is_active",
        (pack_id, legal_entity))
    manifest_row = cur.fetchone()
    if manifest_row is None:
        return None

    cur.execute(
        f"""SELECT e.entry_key, e.entry_id, e.canonical_q, e.answer_md,
                   e.tags, e.asset_refs, e.domain_ids,
                   e.category_id, cl.code, cl.labels, cl.is_active,
                   e.owner_user_id, e.visibility, e.audience_roles,
                   e.school_grade_ids, e.school_subject_ids, e.school_level,
                   e.valid_from, e.valid_to, e.is_active,
                   ARRAY(SELECT DISTINCT t.type_code
                         FROM {config.PG_SCHEMA}.taxonomy_rules t
                         WHERE t.id = ANY(e.domain_ids)
                         ORDER BY t.type_code) AS domains,
                   COALESCE(ARRAY(
                       SELECT DISTINCT v.phrasing
                         FROM {config.PG_SCHEMA}.qa_variants v
                        WHERE v.entry_id = e.entry_id
                          AND v.is_active
                          AND lower(v.phrasing) <> lower(e.canonical_q)
                        ORDER BY v.phrasing), ARRAY[]::text[]) AS variants
              FROM {config.PG_SCHEMA}.qa_entries e
              LEFT JOIN {config.PG_SCHEMA}.code_list cl ON cl.id = e.category_id
             WHERE e.pack_id = %s AND e.legal_entity_code = %s AND e.is_active
             ORDER BY coalesce(e.entry_key, ''), e.entry_id""",
        (pack_id, legal_entity))
    entry_rows = cur.fetchall()
    # Re-sort in Python by the same key as the SQL ORDER BY: belt and braces
    # against a driver/test double that does not actually preserve the SQL
    # order, since the etag and pagination both depend on this being stable.
    entry_rows = sorted(entry_rows, key=lambda r: (r[0] or "", str(r[1])))
    return manifest_row, entry_rows


def _entry_row_to_dict(row, lang: str | None, default_lang: str) -> dict:
    (entry_key, entry_id, canonical_q, answer_md, tags, asset_refs, domain_ids,
     category_id, cat_code, cat_labels, cat_is_active,
     owner_user_id, visibility, audience_roles, school_grade_ids,
     school_subject_ids, school_level, valid_from, valid_to, is_active,
     domains, variants) = row

    category_label = None
    category_label_fallback = False
    if category_id is not None:
        category_label, category_label_fallback = _resolve_label(
            cat_labels or {}, lang, default_lang, cat_code or "")

    return {
        "entry_key": entry_key,
        "entry_id": str(entry_id),
        "canonical_q": canonical_q,
        "answer_md": answer_md,
        "variants": list(variants or []),
        "sources": _sources_list(asset_refs),
        "domain_ids": list(domain_ids) if domain_ids is not None else None,
        "domains": list(domains or []),
        "category_id": category_id,
        "category_code": cat_code,
        "category_label": category_label,
        "category_label_fallback": category_label_fallback,
        "category_is_active": cat_is_active,
        "cat_legacy": _cat_legacy(tags),
        "owner_user_id": owner_user_id,
        "visibility": visibility,
        "audience_roles": list(audience_roles) if audience_roles is not None else None,
        "school_grade_ids": list(school_grade_ids) if school_grade_ids is not None else None,
        "school_subject_ids": list(school_subject_ids) if school_subject_ids is not None else None,
        "school_level": school_level,
        "valid_from": _iso(valid_from),
        "valid_to": _iso(valid_to),
        "is_active": is_active,
    }


def _canonical_json(obj) -> bytes:
    """The one owner of the etag's serialization — CONTRACT.md sec.3."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")


def _pack_etag(version: int, built_at, entries: list[dict]) -> str:
    """sha256 over version + built_at + the deterministically ordered entries.

    Computed over ALL active entries, never a page — a paginated etag would
    change with `limit`, which is the one bug this recipe exists to prevent.
    """
    payload = {
        "version": version,
        "built_at": _iso(built_at),
        "entries": [
            {
                "entry_key": e["entry_key"],
                "entry_id": e["entry_id"],
                "canonical_q": e["canonical_q"],
                "answer_md": e["answer_md"],
                "category_code": e["category_code"],
                "domains": sorted(e["domains"]),
                "variants": sorted(e["variants"]),
                "sources": e["sources"],
                "is_active": e["is_active"],
            }
            for e in entries
        ],
    }
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return f"sha256:{digest}"


def _metadata_key(entry: dict, field: str):
    if field in _UNIFORM_SET_FIELDS:
        return frozenset(entry[field] or [])
    return entry[field]


def _compute_divergence(entries: list[dict]):
    """Reference = first entry in deterministic order (CONTRACT.md sec.2.4)."""
    ref = entries[0]
    divergent_fields = set()
    divergent_entries = set()
    for field in _UNIFORM_SET_FIELDS + _UNIFORM_VALUE_FIELDS:
        ref_val = _metadata_key(ref, field)
        for e in entries:
            if _metadata_key(e, field) != ref_val:
                divergent_fields.add(field)
                divergent_entries.add(e["entry_key"] or e["entry_id"])
    return ref, sorted(divergent_fields), sorted(divergent_entries), not divergent_fields


def _resolve_code_lists(cur, grade_ids, subject_ids, domain_ids):
    """code_list/taxonomy_rules ids -> codes, for the PackState.metadata block.
    Skips a query entirely for an empty/None id list (ANY(%s) on an empty
    Python list is a needless round trip and, for some drivers, ambiguous).
    """
    grade_codes = []
    if grade_ids:
        cur.execute(
            f"SELECT code FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'school_grade' AND id = ANY(%s) ORDER BY code",
            (list(grade_ids),))
        grade_codes = [r[0] for r in cur.fetchall()]
    subject_codes = []
    if subject_ids:
        cur.execute(
            f"SELECT code FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'school_subject' AND id = ANY(%s) ORDER BY code",
            (list(subject_ids),))
        subject_codes = [r[0] for r in cur.fetchall()]
    domain_codes = []
    if domain_ids:
        cur.execute(
            f"SELECT DISTINCT type_code FROM {config.PG_SCHEMA}.taxonomy_rules "
            "WHERE id = ANY(%s) ORDER BY type_code",
            (list(domain_ids),))
        domain_codes = [r[0] for r in cur.fetchall()]
    return grade_codes, subject_codes, domain_codes


@internal.get("/pack-state")
def pack_state(legal_entity: str, pack_id: str):
    """Version, etag and pack-level metadata of one pack (CONTRACT.md sec.3/2.4).

    Metadata comes from the ENTRIES, not the manifest — the writer applies it
    to every row and the portal sends it back unchanged. An empty pack (0
    active entries) falls back to the manifest's own metadata, is uniform by
    definition, and still gets a valid etag over an empty entry list.
    """
    default_lang = _default_language()
    with _cursor() as cur:
        loaded = _load_pack(cur, legal_entity, pack_id)
        if loaded is None:
            return _error(404, "NOT_FOUND",
                          f"no active QA pack '{pack_id}' for '{legal_entity}'")
        manifest_row, entry_rows = loaded
        (version, built_at, lang, m_visibility, m_audience_roles, m_grade_ids,
         m_subject_ids, m_level, m_valid_from, m_valid_to, m_domain_ids) = manifest_row

        entries = [_entry_row_to_dict(r, None, default_lang) for r in entry_rows]

        if entries:
            ref, divergent_fields, divergent_entries, metadata_uniform = \
                _compute_divergence(entries)
            visibility = ref["visibility"]
            audience_roles = ref["audience_roles"]
            grade_ids = ref["school_grade_ids"]
            subject_ids = ref["school_subject_ids"]
            school_level = ref["school_level"]
            valid_from = ref["valid_from"]
            valid_to = ref["valid_to"]
            domain_ids = ref["domain_ids"]
        else:
            divergent_fields, divergent_entries, metadata_uniform = [], [], True
            visibility = m_visibility
            audience_roles = list(m_audience_roles) if m_audience_roles is not None else None
            grade_ids = list(m_grade_ids) if m_grade_ids is not None else None
            subject_ids = list(m_subject_ids) if m_subject_ids is not None else None
            school_level = m_level
            valid_from = _iso(m_valid_from)
            valid_to = _iso(m_valid_to)
            domain_ids = list(m_domain_ids) if m_domain_ids is not None else None

        grade_codes, subject_codes, domain_codes = _resolve_code_lists(
            cur, grade_ids, subject_ids, domain_ids)
        pack_etag = _pack_etag(version, built_at, entries)

    return {
        "pack_id": pack_id,
        "legal_entity_code": legal_entity,
        "pack_version": version,
        "pack_etag": pack_etag,
        "built_at": _iso(built_at),
        "lang": lang,
        "entry_count": len(entries),
        "metadata": {
            "visibility": visibility,
            "audience_roles": audience_roles,
            "school_grade_ids": grade_ids,
            "school_grade_codes": grade_codes,
            "school_subject_ids": subject_ids,
            "school_subject_codes": subject_codes,
            "school_level": school_level,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "domain_codes": domain_codes,
        },
        "metadata_uniform": metadata_uniform,
        "divergent_fields": divergent_fields,
        "divergent_entries": divergent_entries,
    }


@internal.get("/entries")
def list_entries(legal_entity: str, pack_id: str, limit: int = 200,
                  cursor: str | None = None, lang: str | None = None):
    """Full per-entry detail, paginated in Python over the whole loaded pack
    (never a paginated SQL query) so pack_etag stays identical across pages.
    """
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return _error(422, "VALIDATION_ERROR", "limit must be between 1 and 500")

    default_lang = _default_language()
    with _cursor() as cur:
        loaded = _load_pack(cur, legal_entity, pack_id)
        if loaded is None:
            return _error(404, "NOT_FOUND",
                          f"no active QA pack '{pack_id}' for '{legal_entity}'")
        manifest_row, entry_rows = loaded
        version, built_at = manifest_row[0], manifest_row[1]
        entries = [_entry_row_to_dict(r, lang, default_lang) for r in entry_rows]
        pack_etag = _pack_etag(version, built_at, entries)
        metadata_uniform = _compute_divergence(entries)[3] if entries else True

    start = 0
    if cursor is not None:
        idx = next((i for i, e in enumerate(entries) if e["entry_key"] == cursor), None)
        if idx is None:
            return _error(422, "VALIDATION_ERROR", f"unknown cursor '{cursor}'")
        start = idx + 1

    page = entries[start:start + limit]
    has_more = (start + limit) < len(entries)
    next_cursor = page[-1]["entry_key"] if (has_more and page) else None

    return {
        "entries": page,
        "next_cursor": next_cursor,
        "pack_etag": pack_etag,
        "pack_version": version,
        "metadata_uniform": metadata_uniform,
    }


@internal.get("/entries/by-id")
def entry_by_id(legal_entity: str, entry_id: str):
    """Pack and key of one entry by id, for the portal's "try a question" panel
    (CONTRACT.md sec.14.11). Active or not; another school's entry is a 404."""
    eid = _parse_uuid(entry_id)
    if eid is None:
        return _error(400, "BAD_REQUEST", "entry_id must be a UUID")
    with _cursor() as cur:
        cur.execute(
            f"SELECT e.pack_id, e.entry_key, e.canonical_q, e.is_active "
            f"FROM {config.PG_SCHEMA}.qa_entries e "
            "WHERE e.entry_id = %(eid)s::uuid AND e.legal_entity_code = %(le)s",
            {"eid": eid, "le": legal_entity})
        row = cur.fetchone()
    if row is None:
        return _error(404, "NOT_FOUND",
                      f"no QA entry '{eid}' for '{legal_entity}'")
    return {"pack_id": row[0], "entry_key": row[1], "canonical_q": row[2],
            "is_active": bool(row[3])}


# ---------------------------------------------------------------------------
# DELETE /entries and /packs — permanent removal (CONTRACT.md sec.13)
# ---------------------------------------------------------------------------
#
# One `with _cursor() as cur:` block per request = one transaction: db.py's
# get_cursor() commits only after the block exits cleanly and rolls back on
# any exception, so a failure partway through leaves nothing deleted (sec.13.2).
# Every statement filters on legal_entity_code — a foreign pack/entry is 404,
# never touched. qa_variants cascades via FK; qa_embedding has none, so it is
# deleted explicitly. question_answer_log and embedding_registry are never
# named here (sec.13.1: they are not part of what "permanent" removes).

@internal.delete("/entries")
def delete_entry(legal_entity: str, pack_id: str, entry_key: str):
    """Permanently delete one entry (any pack, any legal entity's own scope).

    Variants count is read BEFORE the cascade fires, since the cascade itself
    never reports a row count for the child table.
    """
    with _cursor() as cur:
        cur.execute(
            f"SELECT entry_id FROM {config.PG_SCHEMA}.qa_entries "
            "WHERE legal_entity_code = %s AND pack_id = %s AND entry_key = %s",
            (legal_entity, pack_id, entry_key))
        row = cur.fetchone()
        if row is None:
            return _error(404, "NOT_FOUND",
                          f"no QA entry '{entry_key}' in pack '{pack_id}' "
                          f"for '{legal_entity}'")
        entry_id = row[0]

        cur.execute(
            f"SELECT COUNT(*) FROM {config.PG_SCHEMA}.qa_variants "
            "WHERE entry_id = %s AND legal_entity_code = %s",
            (entry_id, legal_entity))
        variant_count = cur.fetchone()[0]

        cur.execute(
            f"DELETE FROM {config.PG_SCHEMA}.qa_embedding "
            "WHERE entry_id = %s AND legal_entity_code = %s",
            (entry_id, legal_entity))
        embedding_count = cur.rowcount

        # legal_entity_code repeated on every statement, not just the lookup
        # (sec.13.2): entry_id is not reused across entities, but the WHERE
        # clause — not the earlier SELECT — is the one enforcement point.
        cur.execute(
            f"DELETE FROM {config.PG_SCHEMA}.qa_entries "
            "WHERE entry_id = %s AND legal_entity_code = %s",
            (entry_id, legal_entity))
        entry_count = cur.rowcount

    return {"deleted": {
        "entries": entry_count,
        "variants": variant_count,
        "embeddings": embedding_count,
        "manifests": 0,
    }}


@internal.delete("/packs")
def delete_pack(legal_entity: str, pack_id: str):
    """Permanently delete a whole pack: every entry (active or not), their
    variants (cascade) and embeddings, and every qa_pack_manifest row for the
    pair — not only the active manifest (sec.13.1).
    """
    with _cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {config.PG_SCHEMA}.qa_pack_manifest "
            "WHERE legal_entity_code = %s AND pack_id = %s LIMIT 1",
            (legal_entity, pack_id))
        if cur.fetchone() is None:
            return _error(404, "NOT_FOUND",
                          f"no QA pack '{pack_id}' for '{legal_entity}'")

        cur.execute(
            f"SELECT entry_id FROM {config.PG_SCHEMA}.qa_entries "
            "WHERE legal_entity_code = %s AND pack_id = %s",
            (legal_entity, pack_id))
        # entry_id is uuid; psycopg2 would bind a Python list as text[],
        # so pass strings and cast the array in SQL (operator uuid = text).
        entry_ids = [str(r[0]) for r in cur.fetchall()]

        variant_count = 0
        embedding_count = 0
        if entry_ids:
            cur.execute(
                f"SELECT COUNT(*) FROM {config.PG_SCHEMA}.qa_variants "
                "WHERE entry_id = ANY(%s::uuid[]) AND legal_entity_code = %s",
                (entry_ids, legal_entity))
            variant_count = cur.fetchone()[0]

            cur.execute(
                f"DELETE FROM {config.PG_SCHEMA}.qa_embedding "
                "WHERE entry_id = ANY(%s::uuid[]) AND legal_entity_code = %s",
                (entry_ids, legal_entity))
            embedding_count = cur.rowcount

        cur.execute(
            f"DELETE FROM {config.PG_SCHEMA}.qa_entries "
            "WHERE legal_entity_code = %s AND pack_id = %s",
            (legal_entity, pack_id))
        entry_count = cur.rowcount

        cur.execute(
            f"DELETE FROM {config.PG_SCHEMA}.qa_pack_manifest "
            "WHERE legal_entity_code = %s AND pack_id = %s",
            (legal_entity, pack_id))
        manifest_count = cur.rowcount

    return {"deleted": {
        "entries": entry_count,
        "variants": variant_count,
        "embeddings": embedding_count,
        "manifests": manifest_count,
    }}


# ---------------------------------------------------------------------------
# /documents* — ingested documents of one school (CONTRACT.md sec.14.2)
# ---------------------------------------------------------------------------
#
# A document = the versions (file_uuid rows of document_metadata) of one school
# sharing a source key, the same key the ingest flow uses to retire old versions.
# A row with neither source_url nor original_file_name would get a NULL key and
# merge with every other such row, so it falls back to its own file_uuid.
_DOC_KEY = (
    "COALESCE(CASE WHEN m.source_url IS NOT NULL THEN 'url:' || m.source_url "
    "ELSE 'file:' || m.original_file_name || '|' || COALESCE(m.user_name, '') END, "
    "'uuid:' || m.file_uuid::text)")

_DOC_STATUSES = {"active", "inactive", "all"}
_DOC_VALIDITIES = {"current", "expired", "future", "expiring", "all"}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Validity of the shown version against today (CONTRACT.md sec.14.8).
_VALIDITY_SQL = (
    "(CASE WHEN d.valid_from > CURRENT_DATE THEN 'future' "
    "WHEN d.valid_to < CURRENT_DATE THEN 'expired' ELSE 'current' END)")

# Active, current document whose valid_to falls within the next 30 days (sec.14.12).
_EXPIRING_SQL = (
    "(d.doc_active AND d.valid_to IS NOT NULL AND d.valid_to >= CURRENT_DATE "
    "AND d.valid_to <= CURRENT_DATE + 30 "
    "AND (d.valid_from IS NULL OR d.valid_from <= CURRENT_DATE))")

# Stored file format from file_name: a saved web page (*.html) is "web" (sec.14.10).
_DOC_FORMATS = {"pdf", "docx", "pptx", "xlsx", "web"}


def _format_sql(alias: str) -> str:
    return (f"(CASE WHEN {alias}.file_name ~* '[.]html?$' THEN 'web' "
            f"ELSE lower(substring({alias}.file_name from '[.]([A-Za-z0-9]+)$')) END)")


def _parse_uuid(value) -> str | None:
    """Canonical lowercase UUID string, or None if `value` is not a UUID."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _docs_cte() -> str:
    """One row per version in `d`; shown_rank = 1 marks the shown version."""
    s = config.PG_SCHEMA
    return f"""
WITH v AS (
    SELECT m.*, {_DOC_KEY} AS doc_key
    FROM {s}.document_metadata m
    WHERE m.legal_entity_code = %(le)s
), d AS (
    SELECT v.*,
           row_number() OVER (PARTITION BY doc_key
               ORDER BY is_active DESC, ldts_gmt DESC, file_uuid DESC) AS shown_rank,
           bool_or(is_active) OVER (PARTITION BY doc_key) AS doc_active,
           count(*) OVER (PARTITION BY doc_key) AS version_count
    FROM v
)"""


def _summary_sql(where_extra: str = "", paginate: bool = False) -> str:
    # One row per document = its shown version (active first, then newest).
    # Document tables are filtered by %(le)s; code ids resolve globally like
    # _resolve_code_lists (a unit's grades live on its organisation's rows).
    s = config.PG_SCHEMA
    sql = f"""{_docs_cte()}
SELECT d.file_uuid, d.source_url, d.original_file_name,
       COALESCE(d.document_type_user, d.document_type_llm),
       COALESCE(d.document_language_user, d.document_language_llm),
       d.school_subject_user,
       ARRAY(SELECT cl.code FROM {s}.code_list cl
             WHERE cl.list_code = 'school_grade' AND cl.id = ANY(d.school_grade_user)
             ORDER BY cl.sort_order, cl.code),
       ARRAY(SELECT DISTINCT tr.type_code FROM {s}.taxonomy_rules tr
             WHERE tr.id = ANY(CASE WHEN cardinality(d.document_domain_user) > 0
                                    THEN d.document_domain_user
                                    ELSE d.document_domain_llm END)
             ORDER BY tr.type_code),
       d.audience_roles, d.visibility, d.valid_from, d.valid_to, d.user_name,
       d.ldts_gmt, d.doc_active, d.version_count,
       (SELECT count(*) FROM {s}.document_chunks c
         WHERE c.file_uuid = d.file_uuid AND c.legal_entity_code = %(le)s),
       {_VALIDITY_SQL},
       {_format_sql("d")},
       d.content_sha256
FROM d
WHERE d.shown_rank = 1{where_extra}
ORDER BY d.ldts_gmt DESC, d.file_uuid DESC"""
    if paginate:
        sql += "\nLIMIT %(lim)s OFFSET %(off)s"
    return sql


def _summary_row_to_dict(row) -> dict:
    (file_uuid, source_url, original_file_name, document_type, document_language,
     school_subject, grade_codes, domain_codes, audience_roles, visibility,
     valid_from, valid_to, user_name, ingested_at, is_active, version_count,
     chunk_count, validity_state, file_format, content_sha256) = row
    return {
        "file_uuid": str(file_uuid),
        "title": source_url or original_file_name or str(file_uuid),
        "source_kind": "web" if source_url is not None else "file",
        "source_url": source_url,
        "original_file_name": original_file_name,
        "document_type": document_type,
        "document_language": document_language,
        "school_subject": school_subject,
        "school_grade_codes": list(grade_codes or []),
        "domain_codes": list(domain_codes or []),
        "audience_roles": list(audience_roles or []),
        "visibility": visibility,
        "valid_from": _iso(valid_from),
        "valid_to": _iso(valid_to),
        "user_name": user_name,
        "ingested_at": _iso(ingested_at),
        "is_active": bool(is_active),
        "version_count": int(version_count),
        "chunk_count": int(chunk_count),
        "validity_state": validity_state,
        "file_format": file_format,
        "content_sha256": content_sha256,
    }


def _resolve_doc_key(cur, legal_entity: str, file_uuid: str) -> str | None:
    """Source key of the document owning `file_uuid` within this school only."""
    cur.execute(
        f"SELECT {_DOC_KEY} FROM {config.PG_SCHEMA}.document_metadata m "
        "WHERE m.legal_entity_code = %(le)s AND m.file_uuid = %(fu)s::uuid LIMIT 1",
        {"le": legal_entity, "fu": file_uuid})
    row = cur.fetchone()
    return row[0] if row else None


def _load_summary(cur, legal_entity: str, key: str) -> dict | None:
    cur.execute(_summary_sql(" AND d.doc_key = %(key)s"),
                {"le": legal_entity, "key": key})
    row = cur.fetchone()
    return _summary_row_to_dict(row) if row else None


def _doc_not_found(legal_entity: str, file_uuid: str) -> JSONResponse:
    return _error(404, "NOT_FOUND",
                  f"no document '{file_uuid}' for '{legal_entity}'")


def _bad_uuid() -> JSONResponse:
    return _error(400, "BAD_REQUEST", "file_uuid must be a UUID")


@internal.get("/documents")
def list_documents(legal_entity: str, status: str = "active", q: str | None = None,
                   limit: int = 50, cursor: str | None = None,
                   type: str | None = None, role: str | None = None,
                   validity: str = "all", source: str | None = None,
                   format: str | None = None):
    """One row per document (its shown version); the cursor is an offset.
    type/role/validity/source filter the shown version (CONTRACT.md sec.14.8)."""
    if status not in _DOC_STATUSES:
        return _error(422, "VALIDATION_ERROR",
                      "status must be one of active, inactive, all")
    if validity not in _DOC_VALIDITIES:
        return _error(422, "VALIDATION_ERROR",
                      "validity must be one of current, expired, future, expiring, all")
    if limit < 1 or limit > 200:
        return _error(422, "VALIDATION_ERROR", "limit must be between 1 and 200")
    if format is not None and format.strip() and format.strip().lower() not in _DOC_FORMATS:
        return _error(422, "VALIDATION_ERROR",
                      "format must be one of " + ", ".join(sorted(_DOC_FORMATS)))
    offset = 0
    if cursor is not None:
        if not cursor.isdigit():
            return _error(422, "VALIDATION_ERROR", f"unknown cursor '{cursor}'")
        offset = int(cursor)

    where = ""
    params = {"le": legal_entity, "lim": limit + 1, "off": offset}
    if status == "active":
        where += " AND d.doc_active"
    elif status == "inactive":
        where += " AND NOT d.doc_active"
    if q and q.strip():
        # LIKE wildcards in q are literal; backslash is PostgreSQL's default ESCAPE.
        escaped = (q.strip().replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
        params["q"] = f"%{escaped}%"
        where += " AND (d.source_url ILIKE %(q)s OR d.original_file_name ILIKE %(q)s)"
    if type and type.strip():
        params["type"] = type.strip()
        where += " AND COALESCE(d.document_type_user, d.document_type_llm) = %(type)s"
    if role and role.strip():
        params["role"] = role.strip()
        where += " AND %(role)s = ANY(d.audience_roles)"
    if validity == "expiring":
        where += f" AND {_EXPIRING_SQL}"
    elif validity != "all":
        params["validity"] = validity
        where += f" AND {_VALIDITY_SQL} = %(validity)s"
    if source:
        params["source"] = source
        where += " AND (d.source_url = %(source)s OR d.original_file_name = %(source)s)"
    if format and format.strip():
        params["format"] = format.strip().lower()
        where += f" AND {_format_sql('d')} = %(format)s"

    with _cursor() as cur:
        cur.execute(_summary_sql(where, paginate=True), params)
        rows = cur.fetchall()

    has_more = len(rows) > limit
    return {
        "documents": [_summary_row_to_dict(r) for r in rows[:limit]],
        "next_cursor": str(offset + limit) if has_more else None,
    }


@internal.get("/documents/stats")
def documents_stats(legal_entity: str):
    """Per-document counts of the shown version plus active QA entries
    (CONTRACT.md sec.14.12). Registered before /documents/{file_uuid}.
    expired/future/expiring_30 count active documents only (what search hides)."""
    with _cursor() as cur:
        cur.execute(
            f"""{_docs_cte()}
SELECT count(*) FILTER (WHERE d.doc_active),
       count(*) FILTER (WHERE NOT d.doc_active),
       count(*) FILTER (WHERE d.doc_active AND d.valid_to < CURRENT_DATE),
       count(*) FILTER (WHERE {_EXPIRING_SQL}),
       count(*) FILTER (WHERE d.doc_active AND d.valid_from > CURRENT_DATE)
FROM d
WHERE d.shown_rank = 1""", {"le": legal_entity})
        row = cur.fetchone() or (0, 0, 0, 0, 0)
        cur.execute(
            f"SELECT count(*) FROM {config.PG_SCHEMA}.qa_entries e "
            "WHERE e.legal_entity_code = %(le)s AND e.is_active",
            {"le": legal_entity})
        qa_row = cur.fetchone()
    return {
        "active": int(row[0] or 0),
        "inactive": int(row[1] or 0),
        "expired": int(row[2] or 0),
        "expiring_30": int(row[3] or 0),
        "future": int(row[4] or 0),
        "qa_entries_active": int(qa_row[0] or 0) if qa_row else 0,
    }


@internal.get("/documents/{file_uuid}")
def get_document(file_uuid: str, legal_entity: str):
    """One document with all its versions, newest first."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    with _cursor() as cur:
        key = _resolve_doc_key(cur, legal_entity, fu)
        if key is None:
            return _doc_not_found(legal_entity, fu)
        document = _load_summary(cur, legal_entity, key)
        cur.execute(
            f"SELECT m.file_uuid, m.processing_uuid, m.ldts_gmt, m.is_active, "
            f"(SELECT count(*) FROM {config.PG_SCHEMA}.document_chunks c "
            "  WHERE c.file_uuid = m.file_uuid AND c.legal_entity_code = %(le)s), "
            f"m.file_name, {_format_sql('m')}, m.content_sha256 "
            f"FROM {config.PG_SCHEMA}.document_metadata m "
            f"WHERE m.legal_entity_code = %(le)s AND {_DOC_KEY} = %(key)s "
            "ORDER BY m.ldts_gmt DESC, m.file_uuid DESC",
            {"le": legal_entity, "key": key})
        versions = [{
            "file_uuid": str(r[0]),
            "processing_uuid": str(r[1]) if r[1] is not None else None,
            "ingested_at": _iso(r[2]),
            "is_active": bool(r[3]),
            "chunk_count": int(r[4]),
            "file_name": r[5],
            "file_format": r[6],
            "content_sha256": r[7],
        } for r in cur.fetchall()]
    if document is None:
        return _doc_not_found(legal_entity, fu)
    return {"document": document, "versions": versions}


@internal.post("/documents/{file_uuid}/deactivate")
def deactivate_document(file_uuid: str, legal_entity: str):
    """is_active = false on the document's active version(s). Idempotent."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    with _cursor() as cur:
        key = _resolve_doc_key(cur, legal_entity, fu)
        if key is None:
            return _doc_not_found(legal_entity, fu)
        cur.execute(
            f"UPDATE {config.PG_SCHEMA}.document_metadata m SET is_active = false "
            f"WHERE m.legal_entity_code = %(le)s AND {_DOC_KEY} = %(key)s "
            "AND m.is_active",
            {"le": legal_entity, "key": key})
        document = _load_summary(cur, legal_entity, key)
    if document is None:
        return _doc_not_found(legal_entity, fu)
    return {"document": document}


@internal.post("/documents/{file_uuid}/activate")
def activate_document(file_uuid: str, legal_entity: str):
    """Activate the NEWEST version and deactivate all others. Idempotent;
    an older (superseded) version is never re-activated."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    with _cursor() as cur:
        key = _resolve_doc_key(cur, legal_entity, fu)
        if key is None:
            return _doc_not_found(legal_entity, fu)
        cur.execute(
            f"SELECT m.file_uuid FROM {config.PG_SCHEMA}.document_metadata m "
            f"WHERE m.legal_entity_code = %(le)s AND {_DOC_KEY} = %(key)s "
            "ORDER BY m.ldts_gmt DESC, m.file_uuid DESC LIMIT 1",
            {"le": legal_entity, "key": key})
        newest = str(cur.fetchone()[0])
        # Only rows whose flag actually changes are written.
        cur.execute(
            f"UPDATE {config.PG_SCHEMA}.document_metadata m "
            "SET is_active = (m.file_uuid = %(newest)s::uuid) "
            f"WHERE m.legal_entity_code = %(le)s AND {_DOC_KEY} = %(key)s "
            "AND m.is_active IS DISTINCT FROM (m.file_uuid = %(newest)s::uuid)",
            {"le": legal_entity, "key": key, "newest": newest})
        document = _load_summary(cur, legal_entity, key)
    if document is None:
        return _doc_not_found(legal_entity, fu)
    return {"document": document}


def _parse_validity_date(value, field: str):
    """date | None for an ISO YYYY-MM-DD string or null; ValueError otherwise."""
    if value is None:
        return None
    if isinstance(value, str) and _ISO_DATE_RE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD) or null")


@internal.patch("/documents/{file_uuid}/validity")
def update_document_validity(file_uuid: str, legal_entity: str,
                             body: Any = Body(None)):
    """Set valid_from/valid_to on every version of the document in the four
    document tables, in one transaction (CONTRACT.md sec.14.8)."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    if not isinstance(body, dict) or set(body) != {"valid_from", "valid_to"}:
        return _error(422, "VALIDATION_ERROR",
                      "body must be an object with exactly valid_from and valid_to")
    try:
        valid_from = _parse_validity_date(body["valid_from"], "valid_from")
        valid_to = _parse_validity_date(body["valid_to"], "valid_to")
    except ValueError as e:
        return _error(422, "VALIDATION_ERROR", str(e))
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        return _error(422, "VALIDATION_ERROR", "valid_from must not be after valid_to")
    s = config.PG_SCHEMA
    with _cursor() as cur:
        key = _resolve_doc_key(cur, legal_entity, fu)
        if key is None:
            return _doc_not_found(legal_entity, fu)
        cur.execute(
            f"SELECT DISTINCT m.file_uuid FROM {s}.document_metadata m "
            f"WHERE m.legal_entity_code = %(le)s AND {_DOC_KEY} = %(key)s",
            {"le": legal_entity, "key": key})
        file_uuids = sorted(str(r[0]) for r in cur.fetchall())
        params = {"le": legal_entity, "fus": file_uuids,
                  "vf": valid_from, "vt": valid_to}
        for table in ("document_chunks", "document_elements",
                      "document_tables", "document_metadata"):
            cur.execute(
                f"UPDATE {s}.{table} SET valid_from = %(vf)s, valid_to = %(vt)s "
                "WHERE file_uuid = ANY(%(fus)s::uuid[]) AND legal_entity_code = %(le)s",
                params)
        document = _load_summary(cur, legal_entity, key)
    if document is None:
        return _doc_not_found(legal_entity, fu)
    return {"document": document}


@internal.get("/documents/{file_uuid}/chunks")
def document_chunks(file_uuid: str, legal_entity: str, limit: int = 20,
                    cursor: str | None = None):
    """Chunks of ONE version (file_uuid is the version), by chunk_index
    (CONTRACT.md sec.14.13). The cursor is an offset."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    if limit < 1 or limit > 100:
        return _error(422, "VALIDATION_ERROR", "limit must be between 1 and 100")
    offset = 0
    if cursor is not None:
        if not cursor.isdigit():
            return _error(422, "VALIDATION_ERROR", f"unknown cursor '{cursor}'")
        offset = int(cursor)
    s = config.PG_SCHEMA
    with _cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {s}.document_metadata m "
            "WHERE m.legal_entity_code = %(le)s AND m.file_uuid = %(fu)s::uuid",
            {"le": legal_entity, "fu": fu})
        if cur.fetchone() is None:
            return _doc_not_found(legal_entity, fu)
        cur.execute(
            f"SELECT count(*) FROM {s}.document_chunks c "
            "WHERE c.legal_entity_code = %(le)s AND c.file_uuid = %(fu)s::uuid",
            {"le": legal_entity, "fu": fu})
        total = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT c.chunk_index, c.heading_path, c.section_heading, c.chunk_text "
            f"FROM {s}.document_chunks c "
            "WHERE c.legal_entity_code = %(le)s AND c.file_uuid = %(fu)s::uuid "
            "ORDER BY c.chunk_index LIMIT %(lim)s OFFSET %(off)s",
            {"le": legal_entity, "fu": fu, "lim": limit, "off": offset})
        rows = cur.fetchall()
    return {
        "chunks": [{
            "chunk_index": int(r[0]),
            "heading_path": r[1],
            "section_heading": r[2],
            "text": r[3] or "",
            "char_count": len(r[3] or ""),
        } for r in rows],
        "total": total,
        "next_cursor": str(offset + limit) if offset + limit < total else None,
    }


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@internal.patch("/documents/{file_uuid}/content-hash")
def update_content_hash(file_uuid: str, legal_entity: str,
                        body: Any = Body(None)):
    """Set content_sha256 on that ONE version (CONTRACT.md sec.14.14)."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    value = body.get("content_sha256") if isinstance(body, dict) else None
    if (not isinstance(body, dict) or set(body) != {"content_sha256"}
            or not isinstance(value, str) or not _SHA256_RE.match(value)):
        return _error(422, "VALIDATION_ERROR",
                      "body must be an object with exactly content_sha256 "
                      "(64 lowercase hex characters)")
    with _cursor() as cur:
        cur.execute(
            f"UPDATE {config.PG_SCHEMA}.document_metadata m "
            "SET content_sha256 = %(h)s "
            "WHERE m.file_uuid = %(fu)s::uuid AND m.legal_entity_code = %(le)s",
            {"h": value, "fu": fu, "le": legal_entity})
        updated = cur.rowcount
    if not updated:
        return _doc_not_found(legal_entity, fu)
    return {"file_uuid": fu, "content_sha256": value}


@internal.delete("/documents")
def delete_document(legal_entity: str, file_uuid: str):
    """Permanently delete every version of the document from the four
    document tables, in one transaction. ingestion_log, question_answer_log
    and code lists are never named here (sec.14.2)."""
    fu = _parse_uuid(file_uuid)
    if fu is None:
        return _bad_uuid()
    s = config.PG_SCHEMA
    with _cursor() as cur:
        key = _resolve_doc_key(cur, legal_entity, fu)
        if key is None:
            return _doc_not_found(legal_entity, fu)
        cur.execute(
            f"SELECT DISTINCT m.file_uuid FROM {s}.document_metadata m "
            f"WHERE m.legal_entity_code = %(le)s AND {_DOC_KEY} = %(key)s",
            {"le": legal_entity, "key": key})
        # Strings + ::uuid[] cast, as in delete_pack.
        file_uuids = sorted(str(r[0]) for r in cur.fetchall())
        params = {"le": legal_entity, "fus": file_uuids}
        counts = {}
        for name, table in (("chunks", "document_chunks"),
                            ("tables", "document_tables"),
                            ("elements", "document_elements"),
                            ("metadata", "document_metadata")):
            cur.execute(
                f"DELETE FROM {s}.{table} "
                "WHERE file_uuid = ANY(%(fus)s::uuid[]) AND legal_entity_code = %(le)s",
                params)
            counts[name] = cur.rowcount

    return {
        "deleted": {
            "versions": len(file_uuids),
            "chunks": counts["chunks"],
            "elements": counts["elements"],
            "tables": counts["tables"],
        },
        "file_uuids": file_uuids,
    }
