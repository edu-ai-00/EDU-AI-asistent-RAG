"""GET /operations/{operation} (CONTRACT.md §6): reports EXCLUSIVELY the
portal's own stored correlation for one operation_id. Never derives status
from a global "recent ingest runs" search — see adapters/ingest.py and
publish_service.reconcile_operations_on_startup for why since_seconds cannot
stand in for a per-operation lookup.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import errors
from ..policy import Policy, Principal
from ..services import publish_service
from ..state.store import StateStore
from .deps import get_policy, get_principal, get_store, may_read_record_school

router = APIRouter(tags=["operations"])


@router.get("/operations/{operation}")
def get_operation(operation: str, store: StateStore = Depends(get_store),
                  principal: Principal = Depends(get_principal),
                  policy: Policy = Depends(get_policy)) -> dict:
    op = store.get_operation(operation)
    # §14.6: a record of a school the policy does not let us read is a 404,
    # indistinguishable from an unknown id.
    if op is None or not may_read_record_school(policy, principal, op.get("school")):
        raise errors.not_found(f"no such operation {operation!r}")
    return publish_service.public_operation(op)
