"""Export routes: CSV (one pack) / ZIP (many packs) snapshots and their
download (CONTRACT.md §5.2, §5.3). HTTP transport only — the actual CSV/ZIP
composition lives in services/export_service.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .. import errors
from ..adapters.search import SearchClient
from ..services import export_service
from ..state.store import StateStore
from .deps import get_search_client, get_store, require_school_publish
from .models import CreateExportRequest
from .security import csrf_guard

router = APIRouter(tags=["export"])

_CONTENT_TYPES = {"csv": "text/csv", "zip": "application/zip"}


@router.post("/schools/{school}/exports", status_code=201, dependencies=[Depends(csrf_guard)])
def create_export(body: CreateExportRequest,
                   school: str = Depends(require_school_publish),
                   search: SearchClient = Depends(get_search_client),
                   store: StateStore = Depends(get_store)) -> dict:
    result = export_service.create_export(
        search=search, store=store, school=school,
        pack_ids=body.pack_ids, fmt=body.format, lang=body.lang)
    return {k: v for k, v in result.items() if k != "ext"}  # ext is internal bookkeeping


@router.get("/exports/{export}/download")
def download_export(export: str, store: StateStore = Depends(get_store)) -> Response:
    """No `school` in the path (openapi.yaml). The stored export metadata
    carries no owning school, so under the current VpnAdminPolicy — one admin
    for every active school — the only distinguishable outcome is "known
    export id" or not. A future per-school policy must first make
    export_service record the owner (CONTRACT.md §5.2).
    """
    meta, blob = export_service.read_export(store, export)
    if meta is None or blob is None:
        raise errors.not_found(f"no such export {export!r}")
    content_type = _CONTENT_TYPES.get(meta["ext"], "application/octet-stream")
    headers = {"Content-Disposition": f'attachment; filename="{meta["filename"]}"'}
    return Response(content=blob, media_type=content_type, headers=headers)
