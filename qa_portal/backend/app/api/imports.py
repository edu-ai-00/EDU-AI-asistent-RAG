"""Import routes for one school: CSV/ZIP preview and publish (CONTRACT.md
§5, §6). Both routes only ever act on content already checked and stored
under a preview_id — see services/preview_service.py and publish_service.py
docstrings; this module is HTTP transport only.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from .. import errors
from ..adapters.ingest import IngestClient
from ..adapters.search import SearchClient
from ..config import settings
from ..services import preview_service, publish_service
from ..state.store import StateStore
from .deps import (get_ingest_client, get_search_client, get_store,
                    require_school_publish)
from .models import PublishImportRequest
from .security import csrf_guard

router = APIRouter(tags=["import"])

# CSV/ZIP uploads only (CONTRACT.md §5); not a config knob — a hard transport
# limit so an oversized upload never reaches the parser at all.
MAX_IMPORT_BYTES = 10 * 1024 * 1024


@router.post("/schools/{school}/imports/preview", dependencies=[Depends(csrf_guard)])
async def preview_import(file: UploadFile = File(...),
                          pack_id: str | None = Form(default=None),
                          mode: str = Form(default="merge"),
                          pack_metadata_by_pack: str | None = Form(default=None),
                          school: str = Depends(require_school_publish),
                          search: SearchClient = Depends(get_search_client),
                          store: StateStore = Depends(get_store)) -> dict:
    if mode not in ("merge", "replace"):
        raise errors.validation_error(
            "mode must be one of merge, replace",
            [{"field": "mode", "message": f"{mode!r} is not merge or replace"}])

    parsed_metadata = None
    if pack_metadata_by_pack is not None:
        try:
            parsed_metadata = json.loads(pack_metadata_by_pack)
        except ValueError:
            parsed_metadata = None
        if not isinstance(parsed_metadata, dict):
            raise errors.validation_error(
                "pack_metadata_by_pack must be a JSON object",
                [{"field": "pack_metadata_by_pack",
                  "message": "must be a JSON object mapping pack_id to metadata"}])

    raw = await file.read()
    if len(raw) > MAX_IMPORT_BYTES:
        raise errors.payload_too_large(
            f"upload exceeds the {MAX_IMPORT_BYTES}-byte limit for an import file")

    result = preview_service.preview_import(
        search=search, store=store, school=school,
        filename=file.filename or "", content_type=file.content_type,
        raw=raw, pack_id_hint=pack_id, mode=mode,
        pack_metadata_by_pack=parsed_metadata)

    # A single-CSV import is exactly one pack: CONTRACT.md §5.3 says the WHOLE
    # input is validated before any write, so a bad row there means nothing in
    # this import can ever be published — report it as a top-level validation
    # error, not a 200 the client could read as a usable preview. A ZIP with
    # several packs stays a 200 with per-pack ok/errors (CONTRACT.md §5.2).
    if result["source_kind"] == "csv":
        (pack,) = result["packs"]
        if not pack["ok"]:
            raise errors.validation_error(
                f"the uploaded CSV for pack {pack['pack_id']!r} did not pass validation",
                pack["errors"])
    return result


@router.post("/schools/{school}/imports/{preview}/publish", dependencies=[Depends(csrf_guard)])
def publish_import(preview: str, body: PublishImportRequest,
                    school: str = Depends(require_school_publish),
                    search: SearchClient = Depends(get_search_client),
                    ingest: IngestClient = Depends(get_ingest_client),
                    store: StateStore = Depends(get_store)) -> JSONResponse:
    """Mirrors api/packs.py:publish_pack — synchronous validate+lock+mint
    (begin_import_publish), then the actual per-pack ingest calls run off the
    event loop as a Starlette BackgroundTask (run_import_publish).
    """
    op, locks, ctx = publish_service.begin_import_publish(
        search=search, store=store, school=school, preview_id=preview,
        expected_etags=body.expected_etags, confirm_replace=body.confirm_replace,
        lock_ttl_seconds=settings.lock_ttl_seconds)
    background = BackgroundTask(
        publish_service.run_import_publish,
        operation_id=op["operation_id"], school=school, store=store, search=search,
        ingest=ingest, locks=locks, ctx=ctx)
    return JSONResponse(status_code=202, content=publish_service.public_operation(op),
                         background=background)
