"""Read-only catalog routes: schools, school domains, global categories.

CONTRACT.md §2.1/§9: no SQL here, only pack_service calls over the search
adapter. School access always goes through the policy via deps, never a bare
path parameter (see require_school_read in deps.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import errors
from ..adapters.search import SearchClient, SearchConflict, SearchValidationError
from ..config import settings
from ..policy import Policy, Principal
from ..services import pack_service
from ..services.labels import resolve_label
from ..services.validation import fetch_code_lists
from .deps import get_policy, get_principal, get_search_client, require_school_read
from .models import CreateCategoryRequest
from .security import csrf_guard

router = APIRouter(tags=["catalog"])


@router.get("/schools")
def list_schools(principal: Principal = Depends(get_principal),
                  policy: Policy = Depends(get_policy),
                  search: SearchClient = Depends(get_search_client)) -> dict:
    allowed = policy.allowed_schools(principal)
    return {"schools": pack_service.list_schools(search, allowed)}


@router.get("/schools/{school}/domains")
def list_school_domains(lang: str | None = None,
                         school: str = Depends(require_school_read),
                         search: SearchClient = Depends(get_search_client)) -> dict:
    return {"domains": pack_service.get_school_domains(search, school, lang)}


@router.get("/schools/{school}/metadata-options")
def get_pack_metadata_options(lang: str | None = None,
                               school: str = Depends(require_school_read),
                               search: SearchClient = Depends(get_search_client)) -> dict:
    """Číselníky pro dialog nového packu (CONTRACT.md §2.4a). visibility je
    frontendový token (§10), ne databázový číselník — proto natvrdo zde."""
    code_lists = fetch_code_lists(search, school)

    def _options(list_code: str) -> list[dict]:
        out = []
        for c in code_lists.get(list_code, []):
            label, fallback = resolve_label(c.get("labels"), lang, settings.ui_default_lang, c["code"])
            item = {"code": c["code"], "label": label}
            if fallback:
                item["label_fallback"] = True
            out.append(item)
        return out

    return {
        "visibility": ["entity", "organization"],
        "audience_roles": _options("role"),
        "school_grades": _options("school_grade"),
    }


@router.get("/categories")
def list_categories(lang: str | None = None, include_inactive: bool = False,
                     search: SearchClient = Depends(get_search_client)) -> dict:
    return {"categories": pack_service.list_categories(search, lang, include_inactive)}


@router.post("/categories", status_code=201, dependencies=[Depends(csrf_guard)])
def create_category(body: CreateCategoryRequest,
                     principal: Principal = Depends(get_principal),
                     policy: Policy = Depends(get_policy),
                     search: SearchClient = Depends(get_search_client)) -> dict:
    """Global category, immutable code/list_code/id (CONTRACT.md §4.3). The
    search adapter owns the actual uniqueness check and the 409/422 wording
    (contracts/qa-portal/internal-qa-admin.yaml `Error.code` is an open
    string, not a fixed enum) — this route relays that upstream envelope
    rather than inventing its own duplicate-code detection.
    """
    if not policy.may_manage_categories(principal):
        raise errors.forbidden("policy does not allow managing categories")
    payload = body.model_dump(exclude_none=True)
    try:
        created = search.create_category(payload)
    except SearchConflict as e:
        err = (e.body or {}).get("error", {})
        raise errors.AppError(
            409, err.get("code") or "CATEGORY_CODE_TAKEN",
            err.get("message") or f"category code {body.code!r} already exists",
            retryable=bool(err.get("retryable", False)))
    except SearchValidationError as e:
        err = (e.body or {}).get("error", {})
        raise errors.validation_error(
            err.get("message") or "invalid category", err.get("fields") or [])
    return pack_service.shape_category(created, None)
