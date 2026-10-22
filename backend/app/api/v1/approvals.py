"""Approvals queue — reviewing what agents produced under a guardrail."""
from __future__ import annotations

from app.core.exceptions import ConflictError
from fastapi import APIRouter, Query
from sqlalchemy.orm import Session

from app.api.deps import ClientIp, CurrentUser, CurrentUserDep, DbSession
from app.core.rbac import Module
from app.models.approval import ApprovalItem
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import (
    ApprovalDetailOut,
    ApprovalHistoryItemOut,
    ApprovalHistoryOut,
    ApprovalOut,
    DecisionRequest,
    EditTextRequest,
    HistoryStatus,
    RequestChangesRequest,
    RevisionOut,
)
from app.services import approvals as approval_service, views
from app.services.creative.pipeline import REVISION_JOB_KEY

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _may_write(current: CurrentUser, module: Module) -> bool:
    return current.module_enabled(module) and current.can_write(module)


def _cards(current: CurrentUser, db: Session, items: list[ApprovalItem]) -> list[ApprovalOut]:
    """Approval cards with the extra actions this user may take on each.

    "Ask for changes" needs approvals write; "Edit text" also needs content
    write, because it changes the content itself (an approver may only
    decide). The routes enforce the same rules; these flags only shape the UI.
    """
    org = current.organization
    entries = [(item, approval_service.read_payload(org, item)) for item in items]
    support = approval_service.revision_support(db, org=org, entries=entries)
    may_revise = _may_write(current, Module.APPROVALS)
    may_edit = may_revise and _may_write(current, Module.CONTENT)
    cards: list[ApprovalOut] = []
    for item, payload in entries:
        extra = support.get(item.id)
        cards.append(
            views.approval_out(
                item,
                payload,
                can_request_changes=bool(extra and extra.revise and may_revise),
                can_edit_text=bool(extra and extra.edit and may_edit),
                preview_file_id=extra.preview_file_id if extra else None,
            )
        )
    return cards


@router.get("", response_model=list[ApprovalOut])
def list_pending(
    current: CurrentUserDep,
    db: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ApprovalOut]:
    current.require_view(Module.APPROVALS)
    items = approval_service.list_pending(db, tenant_id=current.tenant_id, limit=limit)
    # Decrypted for the "why it waits" line a spending agent's hold adds.
    return _cards(current, db, items)


@router.get("/practice", response_model=list[ApprovalOut])
def list_practised(
    current: CurrentUserDep,
    db: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
    agent: str = Query(default="", max_length=64),
) -> list[ApprovalOut]:
    """What spending agents in practice mode would have done (last 30 days).

    Each one is recorded with its full plan and was never sent.
    """
    current.require_view(Module.APPROVALS)
    items = approval_service.list_practised(
        db, tenant_id=current.tenant_id, limit=limit, agent_slug=agent
    )
    return [
        views.approval_out(item, approval_service.read_payload(current.organization, item))
        for item in items
    ]


@router.get("/count")
def pending_count(current: CurrentUserDep, db: DbSession) -> dict[str, int]:
    """Backs the sidebar badge, so it can refresh without the full list."""
    current.require_view(Module.APPROVALS)
    return {"pending": approval_service.pending_count(db, tenant_id=current.tenant_id)}


#: How each decided state reads in the history.
HISTORY_STATUS_LABEL: dict[str, str] = {
    "approved": "Approved",
    "rejected": "Rejected",
    "superseded": "Replaced by a revised version",
    "expired": "Expired",
}


@router.get("/history", response_model=ApprovalHistoryOut)
def list_history(
    current: CurrentUserDep,
    db: DbSession,
    status: HistoryStatus | None = Query(default=None),
    type_: str = Query(default="", alias="type", max_length=40, pattern=r"^[A-Za-z0-9 _.\-]*$"),
    cursor: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=approval_service.HISTORY_MAX_PAGE),
) -> ApprovalHistoryOut:
    """Items that have left the queue, newest decision first.

    Who decided, when, their note, and, for an item sent back for changes,
    the revised version that replaced it. Payloads are not included.
    """
    current.require_view(Module.APPROVALS)
    page = approval_service.list_history(
        db,
        org=current.organization,
        status=status,
        type_=type_ or None,
        cursor=cursor or None,
        limit=limit,
    )
    return ApprovalHistoryOut(
        items=[
            ApprovalHistoryItemOut(
                id=entry.item.id,
                type=entry.item.type,
                title=entry.item.title,
                agent=entry.item.agent_name,
                agent_slug=entry.item.agent_slug,
                target_kind=entry.item.target_kind,
                status=entry.item.status,
                status_label=HISTORY_STATUS_LABEL.get(entry.item.status, entry.item.status),
                submitted_at=entry.item.submitted_at,
                decided_at=entry.item.decided_at,
                decided_by=entry.item.decided_by or "",
                note=entry.item.decision_note or "",
                revision=entry.revision,
                revises_id=entry.revises_id,
                replaced_by_id=entry.replaced_by_id,
                replaced_by_status=entry.replaced_by_status,
            )
            for entry in page.entries
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/{item_id}", response_model=ApprovalDetailOut)
def get_item(item_id: str, current: CurrentUserDep, db: DbSession) -> ApprovalDetailOut:
    """The pending change in full, decrypted for a reviewer."""
    current.require_view(Module.APPROVALS)
    item = approval_service.get_item(db, tenant_id=current.tenant_id, item_id=item_id)
    payload = approval_service.read_payload(current.organization, item)
    base = _cards(current, db, [item])[0]
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
    try:
        approval_service.approve(
            db,
            org=current.organization,
            actor=current.user,
            item_id=item_id,
            note=payload.note,
            ip_address=ip,
        )
    except ConflictError:
        # The item stays pending, but what the attempt recorded is kept: its
        # apply_error (shown on the card) and any part of the change that did
        # reach a platform, so a retry does not repeat it. The same rule as a
        # scheduled run, which keeps partial work rather than rolling it back.
        db.commit()
        raise
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


@router.post("/{item_id}/request-changes", response_model=RevisionOut)
def request_changes(
    item_id: str,
    payload: RequestChangesRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> RevisionOut:
    """Close the item and ask for a revised version, within the revision limit.

    The feedback is stored encrypted and is never logged or audited as text.
    """
    current.require_write(Module.APPROVALS)
    result = approval_service.request_changes(
        db,
        org=current.organization,
        actor=current.user,
        item_id=item_id,
        feedback=payload.feedback,
        ip_address=ip,
    )
    # A creative's reviser starts a background job for the revised version
    # and leaves its id on the session for this response.
    job_id = db.info.pop(REVISION_JOB_KEY, None)
    db.commit()
    return RevisionOut(
        toast=Toast(message=result.message, kind="success"),
        approval_id=result.approval_id,
        revision=result.revision,
        max_revisions=result.max_revisions,
        job_id=job_id if isinstance(job_id, str) else None,
    )


@router.post("/{item_id}/edit-text", response_model=RevisionOut)
def edit_text(
    item_id: str,
    payload: EditTextRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> RevisionOut:
    """Correct the item's text in place; re-checked, redrawn, still pending.

    Changing content needs content write as well as approvals write: an
    approver decides on work but does not rewrite it.
    """
    current.require_write(Module.APPROVALS)
    current.require_write(Module.CONTENT)
    result = approval_service.edit_text(
        db,
        org=current.organization,
        actor=current.user,
        item_id=item_id,
        fields=dict(payload.fields),
        ip_address=ip,
    )
    db.commit()
    return RevisionOut(
        toast=Toast(message=result.message, kind="success"),
        approval_id=result.approval_id,
        revision=result.revision,
        max_revisions=result.max_revisions,
    )
