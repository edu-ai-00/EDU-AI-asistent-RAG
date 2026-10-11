from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import settings
from ..policy import Policy, Principal
from .deps import get_policy, get_principal

router = APIRouter(tags=["context"])


@router.get("/health")
def health() -> dict:
    """Container HEALTHCHECK target. Answers 200 without touching search or
    ingest — a dependency outage must not also flip this service's own health.
    """
    return {"status": "ok"}


@router.get("/context")
def get_context(principal: Principal = Depends(get_principal),
                 policy: Policy = Depends(get_policy)) -> dict:
    return {
        "mode": "vpn_admin",
        "contract_version": settings.contract_version,
        "portal_version": settings.portal_version,
        "capabilities": {
            "may_publish": True,
            "may_replace": True,
            "may_manage_categories": policy.may_manage_categories(principal),
        },
        "ui_default_lang": settings.ui_default_lang,
    }
