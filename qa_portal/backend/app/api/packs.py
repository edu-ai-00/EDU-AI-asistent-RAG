"""Pack/entry routes for one school: read-only listing plus the write half —
availability check, preview and publish (CONTRACT.md §3, §5, §6). Import/
export are a later slice — do not add stubs for them here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from .. import errors
from ..adapters.ingest import IngestClient
from ..adapters.search import SearchClient, SearchNotFound, SearchUnavailable
from ..config import settings
from ..services import pack_service, preview_service, publish_service
from ..services.validation import fetch_code_lists, validate_pack_metadata
from ..state.lock import PackLock, PublishInProgress
from ..state.store import StateStore
from .deps import (get_ingest_client, get_search_client, get_store,
                    require_school_publish, require_school_read)
from .models import (CreatePackRequest, DeletePackRequest, PreviewPackRequest,
                      PublishPackRequest)
from .security import csrf_guard

router = APIRouter(tags=["entries"])


@router.get("/schools/{school}/packs")
def list_packs(school: str = Depends(require_school_read),
                search: SearchClient = Depends(get_search_client)) -> dict:
    return {"packs": pack_service.list_packs(search, school)}


@router.post("/schools/{school}/packs", dependencies=[Depends(csrf_guard)])
def create_pack(body: CreatePackRequest,
                 school: str = Depends(require_school_publish),
                 search: SearchClient = Depends(get_search_client)) -> dict:
    """Ověří, že pack_id je volný; NEZAKLÁDÁ řádek v qa_pack_manifest — ten
    vznikne až první publikací (openapi.yaml /schools/{school}/packs POST).

    `metadata`, if sent, is only checked against the code lists here — for an
    immediate dialog error — and stored nowhere; the binding copy is the one
    later sent to /preview (CONTRACT.md §2.4a).
    """
    if pack_service.get_pack_state(search, school, body.pack_id) is not None:
        raise errors.AppError(
            409, "PACK_ID_TAKEN",
            f"pack_id {body.pack_id!r} already exists for school {school!r}",
            retryable=False)
    if body.metadata is not None:
        code_lists = fetch_code_lists(search, school)
        field_errors = validate_pack_metadata(body.metadata.model_dump(), code_lists)
        if field_errors:
            raise errors.validation_error(
                "pack_metadata did not pass validation",
                [e.to_error_field() for e in field_errors])
    return {"pack_id": body.pack_id, "available": True}


@router.get("/schools/{school}/packs/{pack}/entries")
def list_entries(pack: str,
                  limit: int = Query(100, ge=1, le=200),
                  cursor: str | None = None,
                  q: str | None = None,
                  lang: str | None = None,
                  school: str = Depends(require_school_read),
                  search: SearchClient = Depends(get_search_client)) -> dict:
    return pack_service.list_entries_page(search, school, pack, limit, cursor, q, lang)


@router.get("/schools/{school}/packs/{pack}/entries/{entry_key}")
def get_entry(pack: str, entry_key: str, lang: str | None = None,
              school: str = Depends(require_school_read),
              search: SearchClient = Depends(get_search_client)) -> dict:
    entry, meta = pack_service.get_entry(search, school, pack, entry_key, lang)
    if entry is None:
        raise errors.not_found(f"no such entry {entry_key!r} in pack {pack!r}")
    return {"entry": entry, "pack_etag": meta["pack_etag"], "pack_version": meta["pack_version"]}


@router.post("/schools/{school}/packs/{pack}/preview", dependencies=[Depends(csrf_guard)])
def preview_pack(pack: str, body: PreviewPackRequest,
                  school: str = Depends(require_school_publish),
                  search: SearchClient = Depends(get_search_client),
                  store: StateStore = Depends(get_store)) -> dict:
    pack_metadata = body.pack_metadata.model_dump() if body.pack_metadata is not None else None
    return preview_service.preview_pack_changes(
        search=search, store=store, school=school, pack=pack,
        base_etag=body.base_etag, entry_writes=[e.to_dict() for e in body.entries],
        pack_metadata=pack_metadata)


@router.post("/schools/{school}/packs/{pack}/publish", dependencies=[Depends(csrf_guard)])
def publish_pack(pack: str, body: PublishPackRequest,
                  school: str = Depends(require_school_publish),
                  search: SearchClient = Depends(get_search_client),
                  ingest: IngestClient = Depends(get_ingest_client),
                  store: StateStore = Depends(get_store)) -> JSONResponse:
    """Synchronous validate+lock+mint (begin_pack_publish), then the actual
    ingest call runs off the event loop as a Starlette BackgroundTask
    (publish_service.run_pack_publish) — see that module's docstring for why
    this is "off the event loop" without hand-rolling a thread.
    """
    op, lock, ctx = publish_service.begin_pack_publish(
        search=search, store=store, school=school, pack_id=pack,
        preview_id=body.preview_id, expected_etag=body.expected_etag,
        lock_ttl_seconds=settings.lock_ttl_seconds)
    background = BackgroundTask(
        publish_service.run_pack_publish,
        operation_id=op["operation_id"], school=school, pack_id=pack,
        store=store, search=search, ingest=ingest, lock=lock, ctx=ctx)
    return JSONResponse(status_code=202, content=publish_service.public_operation(op),
                         background=background)


@router.delete("/schools/{school}/packs/{pack}/entries/{entry_key}",
                dependencies=[Depends(csrf_guard)])
def delete_entry(pack: str, entry_key: str,
                  school: str = Depends(require_school_publish),
                  search: SearchClient = Depends(get_search_client),
                  store: StateStore = Depends(get_store)) -> dict:
    """Permanent delete of one entry (CONTRACT.md §13). Holds the SAME
    PackLock as publish for the whole operation, so a concurrent publish and
    a concurrent delete of the same pack can never interleave.
    """
    lock = PackLock(store.base_dir, school, pack, settings.lock_ttl_seconds)
    try:
        lock.acquire()
    except PublishInProgress:
        raise errors.publish_in_progress()
    try:
        try:
            result = search.delete_entry(school, pack, entry_key)
        except SearchNotFound:
            raise errors.not_found(f"no such entry {entry_key!r} in pack {pack!r}")
        except SearchUnavailable as e:
            raise errors.unavailable(f"delete failed: {e}")
        # A single entry's removal moves the pack's etag too — any preview
        # taken before this could otherwise "restore" it on a later publish.
        pack_service.invalidate_previews_for_pack(store, school, pack)
        return result
    finally:
        lock.release()


@router.delete("/schools/{school}/packs/{pack}", dependencies=[Depends(csrf_guard)])
def delete_pack(pack: str, body: DeletePackRequest,
                 school: str = Depends(require_school_publish),
                 search: SearchClient = Depends(get_search_client),
                 store: StateStore = Depends(get_store)) -> dict:
    """Permanent delete of a whole pack (CONTRACT.md §13). The retyped
    confirmation is checked BEFORE the lock and BEFORE calling search — a
    mismatch must never touch data or contend for the lock.
    """
    if body.confirm_pack_id != pack:
        raise errors.delete_confirmation_mismatch()
    lock = PackLock(store.base_dir, school, pack, settings.lock_ttl_seconds)
    try:
        lock.acquire()
    except PublishInProgress:
        raise errors.publish_in_progress()
    try:
        try:
            result = search.delete_pack(school, pack)
        except SearchNotFound:
            raise errors.not_found(f"no such pack {pack!r} for school {school!r}")
        except SearchUnavailable as e:
            raise errors.unavailable(f"delete failed: {e}")
        pack_service.invalidate_previews_for_pack(store, school, pack)
        return result
    finally:
        lock.release()
