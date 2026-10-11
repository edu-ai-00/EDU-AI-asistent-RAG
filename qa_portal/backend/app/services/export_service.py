"""CSV (one pack) / ZIP (many packs) export — CONTRACT.md §5.2, §5.3.

CSV is explicitly NOT a full backup (no pack-level metadata); the ZIP
manifest carries that. Neither format ever includes passwords, tokens or a
user_id — the importer's uploader is resolved server-side, per school, at
publish time.
"""
from __future__ import annotations

import datetime as dt
import uuid

from .. import errors
from ..adapters.search import SearchClient
from ..config import settings
from ..state.store import StateStore
from . import csv_io, zip_io
from .pack_service import fetch_all_entries, get_pack_state


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _expires_at(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def _pack_rows(entries: list[dict]) -> list[dict]:
    return [{
        "entry_key": e["entry_key"], "canonical_q": e["canonical_q"], "answer_md": e["answer_md"],
        "cat": e.get("cat_legacy") or "", "variants": e["variants"], "sources": e["sources"],
        "is_active": True, "domains": e["domains"], "category_code": e.get("category_code"),
    } for e in entries if e.get("is_active", True)]


def create_export(*, search: SearchClient, store: StateStore, school: str,
                   pack_ids: list[str], fmt: str, lang: str | None) -> dict:
    if fmt == "csv" and len(pack_ids) != 1:
        raise errors.validation_error(
            "csv export supports exactly one pack; use zip for several",
            [{"field": "pack_ids", "message": "csv format allows exactly one pack_id"}])

    csvs: dict[str, bytes] = {}
    manifest_packs = []
    for pack_id in pack_ids:
        state = get_pack_state(search, school, pack_id)
        if state is None:
            raise errors.not_found(f"no such pack {pack_id!r} for {school!r}")
        entries, _ = fetch_all_entries(search, school, pack_id, lang)
        rows = _pack_rows(entries)
        csvs[pack_id] = csv_io.write_pack_csv(rows, include_category_column=True)
        manifest_packs.append({
            "pack_id": pack_id, "lang": state.get("lang"), "version": state["pack_version"],
            "pack_etag": state["pack_etag"], "entry_count": len(rows),
            "metadata": state.get("metadata", {}),
        })

    export_id = str(uuid.uuid4())
    expires_at = _expires_at(settings.export_ttl_seconds)
    note = None
    if fmt == "csv":
        content = next(iter(csvs.values()))
        ext, filename = "csv", f"{pack_ids[0]}.csv"
        note = ("CSV export is not a full backup of the pack — it does not carry pack-level "
                "metadata (visibility, grades, subjects, validity). Use the ZIP export for that.")
    else:
        manifest = {
            "legal_entity_code": school, "exported_at": _now(),
            "portal_version": settings.portal_version, "contract_version": settings.contract_version,
            "packs": manifest_packs,
        }
        content = zip_io.build_export_zip(manifest, csvs)
        ext, filename = "zip", f"{school}_qa_export.zip"

    store.save_export_blob(export_id, content, ext)
    meta = {"export_id": export_id, "format": fmt, "filename": filename, "bytes": len(content),
            "expires_at": expires_at, "ext": ext, "note": note}
    store.save_export_meta(export_id, meta)
    return {**meta, "download_url": f"/qa-api/v1/exports/{export_id}/download"}


def read_export(store: StateStore, export_id: str) -> tuple[dict | None, bytes | None]:
    meta = store.get_export_meta(export_id)
    if meta is None:
        return None, None
    blob = store.read_export_blob(export_id, meta["ext"])
    return meta, blob
