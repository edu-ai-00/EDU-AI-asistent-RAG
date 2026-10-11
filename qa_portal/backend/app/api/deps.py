"""FastAPI dependency wiring — construction of Principal/Policy/adapters/store.

CONTRACT.md §2.1: get_principal() is the ONLY place a Principal comes from,
and it never reads the request. Every route that takes a `school` path
parameter must depend on require_school_read/require_school_publish, never
just trust the path value.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from .. import errors
from ..adapters.ingest import HttpIngestClient, IngestClient
from ..adapters.search import HttpSearchClient, SearchClient
from ..config import settings
from ..policy import Policy, Principal, VpnAdminPolicy, build_vpn_admin_principal
from ..services.pack_service import is_school_active
from ..state.store import StateStore


def get_principal() -> Principal:
    return build_vpn_admin_principal()


def get_policy() -> Policy:
    return VpnAdminPolicy()


@lru_cache
def _default_search_client() -> HttpSearchClient:
    return HttpSearchClient()


@lru_cache
def _default_ingest_client() -> HttpIngestClient:
    return HttpIngestClient()


@lru_cache
def _default_store() -> StateStore:
    return StateStore(settings.state_dir)


def get_search_client() -> SearchClient:
    return _default_search_client()


def get_ingest_client() -> IngestClient:
    return _default_ingest_client()


def get_store() -> StateStore:
    return _default_store()


def require_school_read(school: str, principal: Principal = Depends(get_principal),
                         policy: Policy = Depends(get_policy),
                         search: SearchClient = Depends(get_search_client)) -> str:
    if not policy.may_read_pack(principal, school):
        raise errors.forbidden(f"policy does not allow reading school {school!r}")
    if not is_school_active(search, school):
        raise errors.not_found(f"no such active school {school!r}")
    return school


def require_school_publish(school: str, principal: Principal = Depends(get_principal),
                            policy: Policy = Depends(get_policy),
                            search: SearchClient = Depends(get_search_client)) -> str:
    if not policy.may_publish(principal, school):
        raise errors.forbidden(f"policy does not allow publishing for school {school!r}")
    if not is_school_active(search, school):
        raise errors.not_found(f"no such active school {school!r}")
    return school
