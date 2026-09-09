"""Approvals queue — reviewing what agents produced under a guardrail."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.rbac import Module
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import ApprovalDetailOut, ApprovalOut, DecisionRequest
from app.services import approvals as approval_service, views

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalOut])
def list_pending(
    current: CurrentUserDep,
    db: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ApprovalOut]:
    current.require_view(Module.APPROVALS)
    items = approval_service.list_pending(db, tenant_id=current.tenant_id, limit=limit)
    return [views.approval_out(item) for item in items]


@router.get("/count")
def pending_count(current: CurrentUserDep, db: DbSession) -> dict[str, int]:
    """Backs the sidebar badge, so it can refresh without the full list."""
    current.require_view(Module.APPROVALS)
    return {"pending": approval_service.pending_count(db, tenant_id=current.tenant_id)}


@router.get("/{item_id}", response_model=ApprovalDetailOut)
def get_item(item_id: str, current: CurrentUserDep, db: DbSession) -> ApprovalDetailOut:
    """The pending change in full, decrypted for a reviewer."""
    current.require_view(Module.APPROVALS)
    item = approval_service.get_item(db, tenant_id=current.tenant_id, item_id=item_id)
    payload = approval_service.read_payload(current.organization, item)
    base = views.approval_out(item)
    return ApprovalDetailOut(**base.model_dump(), payload=payload)


@router.post("/{item_id}/approve", response_model=ActionResult)
def approve(
    item_id: str,
    payload: DecisionRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    """Apply the change and close the item.

    The stored payload is replayed through the same code path the agent would
    have used, so approving days later publishes exactly what was proposed.
    """
    current.require_write(Module.APPROVALS)
    item = approval_service.approve(
        db,
        org=current.organization,
        actor=current.user,
        item_id=item_id,
        note=payload.note,
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Approved", kind="success"))


@router.post("/{item_id}/reject", response_model=ActionResult)
def reject(
    item_id: str,
    payload: DecisionRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    """Decline the change and return its target to a settled state."""
    current.require_write(Module.APPROVALS)
    approval_service.reject(
        db,
        org=current.organization,
        actor=current.user,
        item_id=item_id,
        note=payload.note,
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Rejected"))
