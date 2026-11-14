"""Documents page routes (CONTRACT.md §14.4). HTTP transport only — the logic
lives in services/document_service.py and services/document_queue.py.

Every route resolves the school through the policy (deps.require_school_*)
before anything else; every write also passes csrf_guard (§8).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from starlette.background import BackgroundTask

from .. import errors
from ..adapters.ingest import IngestClient
from ..adapters.search import SearchClient
from ..policy import Principal
from ..services import document_queue, document_service
from ..state.store import StateStore
from .deps import (get_ingest_client, get_principal, get_search_client, get_store,
                   require_school_deactivate, require_school_delete,
                   require_school_documents_read, require_school_ingest)
from .models import (CheckSourcesIn, DeleteDocumentRequest, DocumentValidityIn,
                     PreviewUrlIn)
from .security import csrf_guard

router = APIRouter(tags=["documents"])


@router.get("/schools/{school}/documents")
def list_documents(status: str = Query("active", pattern="^(active|inactive|all)$"),
                   q: str | None = Query(None, max_length=500),
                   type: str | None = Query(None, max_length=100),
                   role: str | None = Query(None, max_length=100),
                   validity: str | None = Query(None, max_length=20),
                   source: str | None = Query(None, max_length=2048),
                   file_format: str | None = Query(None, alias="format", max_length=20),
                   limit: int = Query(50, ge=1, le=200),
                   cursor: str | None = Query(None, max_length=200),
                   school: str = Depends(require_school_documents_read),
                   search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.list_documents(search, school, status, q, limit, cursor,
                                           type_code=type, role=role, validity=validity,
                                           source=source, file_format=file_format)


@router.post("/schools/{school}/documents/check-sources", dependencies=[Depends(csrf_guard)])
def check_sources(body: CheckSourcesIn,
                  school: str = Depends(require_school_ingest),
                  search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.check_sources(search, school, body)


@router.post("/schools/{school}/documents/preview-url", dependencies=[Depends(csrf_guard)])
def preview_url(body: PreviewUrlIn,
                school: str = Depends(require_school_ingest),
                search: SearchClient = Depends(get_search_client),
                ingest: IngestClient = Depends(get_ingest_client)) -> dict:
    return document_service.preview_url(search, ingest, school, body)


@router.get("/schools/{school}/documents/stats")
def documents_stats(school: str = Depends(require_school_documents_read),
                    search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.get_stats(search, school)


@router.get("/schools/{school}/documents/{file_uuid}")
def get_document(file_uuid: str,
                 school: str = Depends(require_school_documents_read),
                 search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.get_document(search, school, file_uuid)


@router.get("/schools/{school}/documents/{file_uuid}/chunks")
def document_chunks(file_uuid: str,
                    limit: int = Query(20, ge=1, le=100),
                    cursor: str | None = Query(None, max_length=200),
                    school: str = Depends(require_school_documents_read),
                    search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.list_chunks(search, school, file_uuid, limit, cursor)


@router.post("/schools/{school}/documents/{file_uuid}/refresh",
             dependencies=[Depends(csrf_guard)])
def refresh_document(file_uuid: str,
                     school: str = Depends(require_school_ingest),
                     principal: Principal = Depends(get_principal),
                     search: SearchClient = Depends(get_search_client),
                     store: StateStore = Depends(get_store)) -> JSONResponse:
    body = document_service.refresh_document(search, store, school, file_uuid,
                                             principal.subject)
    return JSONResponse(status_code=202, content=body)


@router.get("/schools/{school}/documents/{file_uuid}/download")
def download_document(file_uuid: str,
                      school: str = Depends(require_school_documents_read),
                      ingest: IngestClient = Depends(get_ingest_client)) -> StreamingResponse:
    """§14.10: one version's stored file, streamed unchanged. Never inline:
    octet-stream + nosniff, so a saved page cannot run in the portal's origin."""
    stored = document_service.open_download(ingest, school, file_uuid)
    return StreamingResponse(
        stored.chunks, media_type="application/octet-stream",
        headers={"Content-Disposition": stored.content_disposition or "attachment",
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        background=BackgroundTask(stored.close))


@router.get("/schools/{school}/document-options")
def get_document_options(lang: str | None = None,
                         school: str = Depends(require_school_documents_read),
                         search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.build_options(search, school, lang)


@router.post("/schools/{school}/documents", dependencies=[Depends(csrf_guard)])
async def upload_document(request: Request,
                          school: str = Depends(require_school_ingest),
                          principal: Principal = Depends(get_principal),
                          search: SearchClient = Depends(get_search_client),
                          store: StateStore = Depends(get_store)) -> JSONResponse:
    """One item (file xor url) -> 202 {job}. No File/Form parameters on
    purpose: the body is read only after the dependencies passed, and then
    size-bounded (document_service.accept_upload)."""
    body = await document_service.accept_upload(
        request=request, school=school, search=search, store=store,
        principal_subject=principal.subject)
    return JSONResponse(status_code=202, content=body)


@router.get("/schools/{school}/document-jobs")
def list_document_jobs(batch_id: str | None = Query(None, max_length=64),
                       school: str = Depends(require_school_documents_read),
                       store: StateStore = Depends(get_store),
                       ingest: IngestClient = Depends(get_ingest_client),
                       search: SearchClient = Depends(get_search_client)) -> dict:
    if batch_id is not None:
        try:
            batch_id = document_service.canonical_uuid(batch_id, "batch_id")
        except errors.AppError:
            return {"jobs": []}  # not a UUID: no job can carry it
    return {"jobs": document_queue.list_jobs(store, ingest, school, batch_id, search)}


@router.post("/schools/{school}/documents/{file_uuid}/deactivate",
             dependencies=[Depends(csrf_guard)])
def deactivate_document(file_uuid: str,
                        school: str = Depends(require_school_deactivate),
                        search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.set_document_active(search, school, file_uuid, active=False)


@router.post("/schools/{school}/documents/{file_uuid}/activate",
             dependencies=[Depends(csrf_guard)])
def activate_document(file_uuid: str,
                      school: str = Depends(require_school_deactivate),
                      search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.set_document_active(search, school, file_uuid, active=True)


@router.patch("/schools/{school}/documents/{file_uuid}/validity",
              dependencies=[Depends(csrf_guard)])
def update_document_validity(file_uuid: str, body: DocumentValidityIn,
                             school: str = Depends(require_school_deactivate),
                             search: SearchClient = Depends(get_search_client)) -> dict:
    return document_service.set_document_validity(search, school, file_uuid, body)


@router.delete("/schools/{school}/documents/{file_uuid}", dependencies=[Depends(csrf_guard)])
def delete_document(file_uuid: str, body: DeleteDocumentRequest,
                    school: str = Depends(require_school_delete),
                    ingest: IngestClient = Depends(get_ingest_client)) -> dict:
    return document_service.delete_document(ingest, school, file_uuid, body.confirm_file_uuid)
