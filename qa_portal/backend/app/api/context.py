from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..adapters.search import SearchClient, SearchUnavailable
from ..config import settings
from ..policy import Policy, Principal
from .deps import get_policy, get_principal, get_search_client

logger = logging.getLogger("qa_portal.context")

router = APIRouter(tags=["context"])


@router.get("/health")
def health() -> dict:
    """Container HEALTHCHECK target. Answers 200 without touching search or
    ingest — a dependency outage must not also flip this service's own health.
    """
    return {"status": "ok"}


def _candidate_schools(principal: Principal, policy: Policy, search: SearchClient) -> list[str]:
    selector = policy.allowed_schools(principal)
    if not selector.all_active:
        return sorted(selector.codes)
    try:
        return [e["legal_entity_code"] for e in search.list_active_entities()]
    except SearchUnavailable as e:
        # Fail closed: without the register no school can be confirmed.
        logger.warning("context: entity register unavailable (%s)", type(e).__name__)
        return []


@router.get("/context")
def get_context(principal: Principal = Depends(get_principal),
                 policy: Policy = Depends(get_policy),
                 search: SearchClient = Depends(get_search_client)) -> dict:
    # CONTRACT.md §14.6: true when the policy allows it for at least one school.
    schools = _candidate_schools(principal, policy, search)
    return {
        "mode": "vpn_admin",
        "contract_version": settings.contract_version,
        "portal_version": settings.portal_version,
        "capabilities": {
            "may_publish": any(policy.may_publish(principal, s) for s in schools),
            "may_replace": any(policy.may_replace(principal, s) for s in schools),
            "may_manage_categories": policy.may_manage_categories(principal),
        },
        "ui_default_lang": settings.ui_default_lang,
    }
