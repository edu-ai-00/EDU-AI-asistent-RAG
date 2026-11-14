"""Try a question (CONTRACT.md §14.9). HTTP transport only; logic lives in
services/ask_service.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..adapters.search import SearchClient
from ..services import ask_service
from .deps import get_search_client, require_school_documents_read, require_school_publish
from .models import AskPreviewIn
from .security import csrf_guard

router = APIRouter(tags=["ask"])


@router.post("/schools/{school}/ask-preview", dependencies=[Depends(csrf_guard)])
def ask_preview(body: AskPreviewIn,
                school: str = Depends(require_school_publish),
                search: SearchClient = Depends(get_search_client)) -> dict:
    return ask_service.ask_preview(search, school, body)


@router.get("/schools/{school}/entries/by-id")
def entry_by_id(entry_id: str = Query(..., max_length=64),
                school: str = Depends(require_school_documents_read),
                search: SearchClient = Depends(get_search_client)) -> dict:
    return ask_service.get_entry_by_id(search, school, entry_id)
