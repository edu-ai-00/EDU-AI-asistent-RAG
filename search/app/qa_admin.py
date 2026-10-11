# written for task qa-data-c1a; /pack-state and /entries added in qa-data-c1b;
# DELETE /entries and /packs added in qa-data-delete (CONTRACT.md sec.13).
"""Internal QA admin router — global qa_category catalog, technical uploader,
pack state (version/etag/metadata), the paginated entry detail list and the
permanent-delete endpoints.

NOT public (see contracts/qa-portal/internal-qa-admin.yaml): reachable only
from the app network, called exclusively by edu_ai_qa_admin.
"""

import hashlib
import json
import re

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
