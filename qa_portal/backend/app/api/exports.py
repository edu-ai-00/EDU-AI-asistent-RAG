"""Export routes: CSV (one pack) / ZIP (many packs) snapshots and their
download (CONTRACT.md §5.2, §5.3). HTTP transport only — the actual CSV/ZIP
composition lives in services/export_service.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .. import errors
from ..adapters.search import SearchClient
from ..policy import Policy, Principal
from ..services import export_service
from ..state.store import StateStore
from .deps import (get_policy, get_principal, get_search_client, get_store,
                   may_read_record_school, require_school_publish)
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
    # ext and school are internal bookkeeping, not part of the Export schema
    return {k: v for k, v in result.items() if k not in ("ext", "school")}


@router.get("/exports/{export}/download")
def download_export(export: str, store: StateStore = Depends(get_store),
                    principal: Principal = Depends(get_principal),
                    policy: Policy = Depends(get_policy)) -> Response:
    """No `school` in the path (openapi.yaml): the stored export metadata
    carries the owning school and the policy decides (CONTRACT.md §14.6).
    A record from before that field is visible only to an all-schools principal.
    """
    meta, blob = export_service.read_export(store, export)
    if meta is None or blob is None or not may_read_record_school(
            policy, principal, meta.get("school")):
        raise errors.not_found(f"no such export {export!r}")
    content_type = _CONTENT_TYPES.get(meta["ext"], "application/octet-stream")
    headers = {"Content-Disposition": f'attachment; filename="{meta["filename"]}"'}
    return Response(content=blob, media_type=content_type, headers=headers)
