"""preview_pack_changes / preview_import (CONTRACT.md §3, §5).

A preview stores EXACTLY the checked content under preview_id; publish later
reads only that stored content, never a body sent at publish time. Both
online-editor and CSV/ZIP import previews are validated in full before any
entry is written anywhere (CONTRACT.md §5.3).
"""
from __future__ import annotations

import datetime as dt
import uuid

from .. import errors
from ..adapters.search import SearchClient
from ..config import settings
from ..state.store import StateStore
from . import csv_io, zip_io
from .diff import compute_diff
from .pack_service import (category_catalog_map, domain_code_set,
                            fetch_all_entries, get_pack_state)
from .validation import (FieldError, fetch_code_lists, find_duplicate_entry_keys,
                          validate_category, validate_domains,
                          validate_pack_metadata, validate_required_text)


def _new_entry_key(used: set[str]) -> str:
    for _ in range(5):
        key = f"e_{uuid.uuid4().hex[:16]}"
        if key not in used:
            used.add(key)
            return key
    raise RuntimeError("could not mint a unique entry_key")  # pragma: no cover


def _expires_at(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def _raise_for_errors(all_errors: list[FieldError]) -> None:
    if not all_errors:
        return
    codes = {e.code for e in all_errors}
    fields = [e.to_error_field() for e in all_errors]
    if "UNKNOWN_DOMAIN" in codes:
        raise errors.unknown_domain("One or more domain codes are not in this school's catalog", fields)
    if "UNKNOWN_CATEGORY" in codes:
        raise errors.unknown_category("One or more category codes are unknown", fields)
    if "INACTIVE_CATEGORY" in codes:
        raise errors.inactive_category("One or more category codes are inactive", fields)
    raise errors.validation_error("The submitted entries did not pass validation", fields)


def _resolve_write_entry(write: dict, current_by_key: dict[str, dict],
                          used_keys: set[str]) -> dict:
    key = (write.get("entry_key") or "").strip() or None
    if key is None:
        key = _new_entry_key(used_keys)
    else:
        used_keys.add(key)
    return {
        "entry_key": key,
        "canonical_q": write.get("canonical_q", ""),
        "answer_md": write.get("answer_md", ""),
        "variants": list(write.get("variants") or []),
        "sources": list(write.get("sources") or []),
        "domains": [d.strip().upper() for d in (write.get("domains") or []) if d.strip()],
        "category_code": write.get("category_code"),
        "is_active": bool(write.get("is_active", True)),
    }


def preview_pack_changes(*, search: SearchClient, store: StateStore, school: str,
                          pack: str, base_etag: str, entry_writes: list[dict],
                          pack_metadata: dict | None = None) -> dict:
    state = get_pack_state(search, school, pack)
    if state is None:
        if base_etag not in ("", None):
            raise errors.version_conflict(
                f"pack {pack!r} does not exist yet for {school!r}", current_etag=None)
        current_by_key: dict[str, dict] = {}
        # A new pack has no state to read metadata from — the administrator
        # must confirm it now (CONTRACT.md §2.4a); no form default is ever used.
        if pack_metadata is None:
            raise errors.validation_error(
                "pack_metadata is required for a pack that does not exist yet",
                [{"field": "pack_metadata", "message": "required for a new pack"}])
        code_lists = fetch_code_lists(search, school)
        meta_errors = validate_pack_metadata(pack_metadata, code_lists)
        if meta_errors:
            raise errors.validation_error(
                "pack_metadata did not pass validation",
                [e.to_error_field() for e in meta_errors])
    else:
        if base_etag != state["pack_etag"]:
            raise errors.version_conflict(
                "the pack changed since you loaded it", current_etag=state["pack_etag"])
        current_by_key = {e["entry_key"]: e
                           for e in fetch_all_entries(search, school, pack)[0]
                           if e["entry_key"]}
        # §2.4: an EXISTING pack keeps its own metadata; accepting a client
        # copy here would silently rewrite that pack's visibility on publish.
        if pack_metadata is not None:
            raise errors.validation_error(
                "pack_metadata is not allowed for a pack that already exists",
                [{"field": "pack_metadata",
                  "message": "an existing pack keeps its own metadata (CONTRACT.md §2.4)"}])

    domain_catalog = domain_code_set(search, school)
    category_catalog = category_catalog_map(search)

    used_keys = set(current_by_key.keys())
    proposed: list[dict] = []
    all_errors: list[FieldError] = []
    for write in entry_writes:
        resolved = _resolve_write_entry(write, current_by_key, used_keys)
        proposed.append(resolved)
        all_errors += validate_required_text(resolved)
        all_errors += validate_domains(resolved["domains"], domain_catalog)
        previous = current_by_key.get(resolved["entry_key"])
        all_errors += validate_category(
            resolved["category_code"], category_catalog,
            previous_code=(previous or {}).get("category_code"))
    all_errors += find_duplicate_entry_keys([p["entry_key"] for p in proposed])
    _raise_for_errors(all_errors)

    diff = compute_diff(current_by_key, proposed, mode="merge")

    preview_id = str(uuid.uuid4())
    warnings = []
    if state is not None and not state.get("metadata_uniform", True):
        warnings.append({
            "code": "PACK_METADATA_DIVERGENT",
            "message": "This pack's entries have divergent metadata; publish will be refused "
                       "until that is resolved.",
        })
    expires_at = _expires_at(settings.preview_ttl_seconds)
    store.save_preview(preview_id, {
        "kind": "pack", "school": school, "pack_id": pack, "base_etag": base_etag,
        "entries": proposed, "expires_at": expires_at,
        "pack_metadata": pack_metadata,
    })
    return {"preview_id": preview_id, "base_etag": base_etag, "diff": diff,
            "warnings": warnings, "expires_at": expires_at}


def _resolve_csv_row(row, current_by_key: dict[str, dict], used_keys: set[str],
                      legacy_unresolved: list[dict]) -> dict:
    key = row.entry_key
    current = current_by_key.get(key) if key else None
    if key is None:
        key = _new_entry_key(used_keys)
    else:
        used_keys.add(key)

    if row.domains is csv_io.INHERIT_DOMAINS:
        domains = list(current["domains"]) if current else []
    else:
        domains = row.domains

    if row.category_code is csv_io.UNSPECIFIED_CATEGORY:
        category_code = current.get("category_code") if current else None
        if row.cat_legacy.strip():
            legacy_unresolved.append({"entry_key": key, "cat": row.cat_legacy.strip()})
    else:
        category_code = row.category_code

    return {
        "entry_key": key, "canonical_q": row.canonical_q, "answer_md": row.answer_md,
        "variants": row.variants, "sources": row.sources, "domains": domains,
        "category_code": category_code, "is_active": row.is_active,
    }, current


def _preview_one_pack(*, search: SearchClient, school: str, pack_id: str, file_label: str,
                       csv_bytes: bytes, mode: str, domain_catalog: set[str],
                       category_catalog: dict[str, dict]) -> dict:
    parsed = csv_io.parse_pack_csv(csv_bytes, filename=file_label)
    pack_errors: list[FieldError] = list(parsed.errors)

    state = get_pack_state(search, school, pack_id)
    is_new_pack = state is None
    base_etag = "" if is_new_pack else state["pack_etag"]
    current_by_key = ({} if is_new_pack
                       else {e["entry_key"]: e
                             for e in fetch_all_entries(search, school, pack_id)[0]
                             if e["entry_key"]})

    used_keys = set(current_by_key.keys())
    legacy_unresolved: list[dict] = []
    proposed: list[dict] = []
    for r in parsed.rows:
        resolved, current = _resolve_csv_row(r, current_by_key, used_keys, legacy_unresolved)
        proposed.append(resolved)
        pack_errors += validate_required_text(resolved, file=file_label, row=r.row)
        pack_errors += validate_domains(resolved["domains"], domain_catalog,
                                         file=file_label, row=r.row)
        pack_errors += validate_category(
            resolved["category_code"], category_catalog,
            previous_code=(current or {}).get("category_code"),
            file=file_label, row=r.row)
    pack_errors += find_duplicate_entry_keys(
        [p["entry_key"] for p in proposed], file=file_label,
        rows=[r.row for r in parsed.rows])

    ok = not pack_errors
    diff = compute_diff(current_by_key, proposed, mode=mode) if ok else \
        {"added": [], "changed": [], "unchanged": [], "deactivated": []}

    return {
        "pack_id": pack_id, "file": file_label, "base_etag": base_etag,
        "is_new_pack": is_new_pack, "diff": diff, "ok": ok,
        "row_count": len(parsed.rows),
        "errors": [e.to_import_error() for e in pack_errors],
        "legacy_cat_unresolved": legacy_unresolved,
        "_entries": proposed,  # internal only, stripped before the HTTP response
    }


def _manifest_metadata_by_pack(manifest: dict) -> dict[str, dict]:
    """Extract {pack_id: metadata} from an export manifest's packs[] list
    (CONTRACT.md §2.4c: echoed as suggested_metadata only, never confirmation).
    """
    out: dict[str, dict] = {}
    for entry in manifest.get("packs") or []:
        pid = entry.get("pack_id")
        meta = entry.get("metadata")
        if pid and meta:
            out[pid] = meta
    return out


def _resolve_pack_metadata(*, search: SearchClient, school: str, packs: list[dict],
                            pack_metadata_by_pack: dict | None) -> list[FieldError]:
    """CONTRACT.md §2.4c: resolve/validate confirmed metadata per pack in an
    import. Mutates each pack dict in place (adds metadata_required,
    publishable, suggested_metadata, and — for new packs with confirmed
    metadata — pack_metadata). Returns top-level field errors (raised by the
    caller as 422), never partial errors mixed with a 200.
    """
    requested = dict(pack_metadata_by_pack or {})
    by_id = {p["pack_id"]: p for p in packs}
    code_lists: dict | None = None
    top_errors: list[FieldError] = []

    for pack_id, meta in requested.items():
        p = by_id.get(pack_id)
        if p is None:
            top_errors.append(FieldError(
                f"pack_metadata_by_pack.{pack_id}", "no such pack in this import"))
            continue
        if not p["is_new_pack"]:
            top_errors.append(FieldError(
                f"pack_metadata_by_pack.{pack_id}",
                "an existing pack keeps its own metadata (CONTRACT.md §2.4)"))
            continue
        if code_lists is None:
            code_lists = fetch_code_lists(search, school)
        meta_errors = validate_pack_metadata(meta, code_lists)
        if meta_errors:
            for e in meta_errors:
                e.file = p["file"]
            top_errors += meta_errors
            continue
        p["_confirmed_metadata"] = meta

    if top_errors:
        return top_errors

    for p in packs:
        if p["is_new_pack"]:
            p["metadata_required"] = "_confirmed_metadata" not in p
            suggested = p.pop("_suggested_metadata", None)
            if suggested is not None:
                p["suggested_metadata"] = suggested
        else:
            p["metadata_required"] = False
        p["publishable"] = bool(p["ok"]) and not p["metadata_required"]
    return []


def preview_import(*, search: SearchClient, store: StateStore, school: str,
                    filename: str, content_type: str | None, raw: bytes,
                    pack_id_hint: str | None, mode: str,
                    pack_metadata_by_pack: dict | None = None) -> dict:
    is_zip = filename.lower().endswith(".zip") or content_type == "application/zip"
    is_csv = filename.lower().endswith(".csv") or (content_type or "").startswith("text/csv")
    if not is_zip and not is_csv:
        raise errors.unsupported_media()

    domain_catalog = domain_code_set(search, school)
    category_catalog = category_catalog_map(search)

    if is_csv:
        if not pack_id_hint:
            raise errors.validation_error(
                "pack_id is required to confirm the target pack for a CSV upload",
                [{"field": "pack_id", "message": "required for a single-CSV import"}])
        packs = [_preview_one_pack(
            search=search, school=school, pack_id=pack_id_hint, file_label=filename,
            csv_bytes=raw, mode=mode, domain_catalog=domain_catalog,
            category_catalog=category_catalog)]
        source_kind = "csv"
    else:
        try:
            manifest, files = zip_io.parse_import_zip(raw)
        except zip_io.BadZip as e:
            raise errors.validation_error(str(e), [{"field": "file", "message": str(e)}])
        manifest_entity = manifest.get("legal_entity_code")
        if manifest_entity != school:
            raise errors.import_entity_mismatch(
                f"manifest targets {manifest_entity!r}, not {school!r}")
        manifest_metadata = _manifest_metadata_by_pack(manifest)
        packs = []
        for path, content in sorted(files.items()):
            pack_id = path.removeprefix("packs/").removesuffix(".csv")
            p = _preview_one_pack(
                search=search, school=school, pack_id=pack_id, file_label=path,
                csv_bytes=content, mode=mode, domain_catalog=domain_catalog,
                category_catalog=category_catalog)
            suggested = manifest_metadata.get(pack_id)
            if p["is_new_pack"] and suggested is not None:
                p["_suggested_metadata"] = suggested
            packs.append(p)
        source_kind = "zip"

    meta_errors = _resolve_pack_metadata(
        search=search, school=school, packs=packs,
        pack_metadata_by_pack=pack_metadata_by_pack)
    if meta_errors:
        raise errors.validation_error(
            "pack_metadata_by_pack did not pass validation",
            [e.to_error_field() for e in meta_errors])

    preview_id = str(uuid.uuid4())
    expires_at = _expires_at(settings.preview_ttl_seconds)
    stored_packs, public_packs = [], []
    for p in packs:
        public = {k: v for k, v in p.items() if k != "_entries" and k != "_confirmed_metadata"}
        public_packs.append(public)
        stored_packs.append({**public, "entries": p["_entries"],
                              "pack_metadata": p.get("_confirmed_metadata")})
    store.save_preview(preview_id, {
        "kind": "import", "school": school, "source_kind": source_kind, "mode": mode,
        "expires_at": expires_at, "packs": stored_packs,
    })
    return {"preview_id": preview_id, "source_kind": source_kind, "mode": mode,
            "packs": public_packs, "expires_at": expires_at}
