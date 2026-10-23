"""Workflow: approval chains, public API tokens, webhooks, ROI snapshots."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_db
from app.core.exceptions import AppError, NotFoundError
from app.core.rbac import Module
from app.models.approval import ApprovalItem
from app.models.workflow import ApiToken, ApprovalStep, OutboundWebhook, RoiAttributionSnapshot
from app.services import workflow as workflow_svc

router = APIRouter(prefix="/workflow", tags=["workflow"])


class ChainIn(BaseModel):
    steps: list[dict] = Field(default_factory=list)


class StepDecisionIn(BaseModel):
    decision: str
    notes: str = ""


class ApiTokenIn(BaseModel):
    label: str = "API token"
    scopes: list[str] = Field(default_factory=list)
    ttl_days: int | None = 90


class WebhookIn(BaseModel):
    name: str
    url: str
    events: list[str] = Field(default_factory=list)
    secret: str


class RoiIn(BaseModel):
    period_start: datetime
    period_end: datetime
    channels: dict = Field(default_factory=dict)
    notes: str = ""


@router.post("/approvals/{item_id}/chain")
def define_chain(
    item_id: str,
    body: ChainIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    item = db.get(ApprovalItem, item_id)
    if item is None or item.tenant_id != current.tenant_id:
        raise NotFoundError("Approval item not found")
    try:
        steps = workflow_svc.define_approval_chain(
            db,
            current.organization,
            item,
            steps=body.steps,
            actor_email=current.user.email,
        )
        db.commit()
    except ValueError as exc:
        raise AppError(str(exc), status_code=400) from exc
    return {"steps": [{"id": s.id, "order": s.step_order, "status": s.status} for s in steps]}


@router.post("/approvals/steps/{step_id}/decide")
def decide_step(
    step_id: str,
    body: StepDecisionIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    step = db.get(ApprovalStep, step_id)
    if step is None or step.tenant_id != current.tenant_id:
        raise NotFoundError("Approval step not found")
    try:
        row = workflow_svc.decide_step(
            db,
            current.organization,
            step,
            decision=body.decision,
            actor_user_id=current.user.id,
            actor_email=current.user.email,
            actor_role=current.user.role,
            notes=body.notes,
        )
        db.commit()
    except ValueError as exc:
        raise AppError(str(exc), status_code=400) from exc
    return {"id": row.id, "status": row.status, "decided_at": row.decided_at}


@router.post("/api-tokens")
def mint_token(
    body: ApiTokenIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        minted = workflow_svc.mint_api_token(
            db,
            current.organization,
            current.cipher,
            label=body.label,
            scopes=body.scopes,
            created_by_id=current.user.id,
            actor_email=current.user.email,
            ttl_days=body.ttl_days,
        )
        db.commit()
    except ValueError as exc:
        raise AppError(str(exc), status_code=400) from exc
    return {
        "id": minted.row.id,
        "prefix": minted.row.token_prefix,
        "scopes": minted.row.scopes,
        "token": minted.raw_token,
        "expires_at": minted.row.expires_at,
    }


@router.get("/api-tokens")
def list_tokens(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    rows = list(
        db.execute(
            select(ApiToken).where(ApiToken.tenant_id == current.tenant_id)
        ).scalars()
    )
    return {
        "tokens": [
            {
                "id": r.id,
                "label": r.label,
                "prefix": r.token_prefix,
                "scopes": r.scopes,
                "revoked": r.revoked_at is not None,
                "expires_at": r.expires_at,
            }
            for r in rows
        ]
    }


@router.delete("/api-tokens/{token_id}")
def revoke_token(
    token_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        row = workflow_svc.revoke_api_token(
            db, current.organization, token_id, actor_email=current.user.email
        )
        db.commit()
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    return {"id": row.id, "revoked_at": row.revoked_at}


@router.post("/webhooks")
def register_webhook(
    body: WebhookIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        row = workflow_svc.register_webhook(
            db,
            current.organization,
            current.cipher,
            name=body.name,
            url=body.url,
            events=body.events,
            secret=body.secret,
            created_by_id=current.user.id,
            actor_email=current.user.email,
        )
        db.commit()
    except ValueError as exc:
        raise AppError(str(exc), status_code=400) from exc
    return {"id": row.id, "name": row.name, "events": row.events, "enabled": row.enabled}


@router.get("/webhooks")
def list_webhooks(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    rows = list(
        db.execute(
            select(OutboundWebhook).where(OutboundWebhook.tenant_id == current.tenant_id)
        ).scalars()
    )
    return {
        "webhooks": [
            {"id": r.id, "name": r.name, "url": r.url, "events": r.events, "enabled": r.enabled}
            for r in rows
        ]
    }


@router.post("/roi/snapshots")
def create_roi_snapshot(
    body: RoiIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.REPORTS)
    try:
        row = workflow_svc.record_roi_snapshot(
            db,
            current.organization,
            period_start=body.period_start,
            period_end=body.period_end,
            channels=body.channels,
            created_by_id=current.user.id,
            actor_email=current.user.email,
            notes=body.notes,
        )
        db.commit()
    except ValueError as exc:
        raise AppError(str(exc), status_code=400) from exc
    return {
        "id": row.id,
        "blended_roas": row.blended_roas,
        "channels": row.channels,
    }


@router.get("/roi/snapshots")
def list_roi_snapshots(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.REPORTS)
    rows = list(
        db.execute(
            select(RoiAttributionSnapshot)
            .where(RoiAttributionSnapshot.tenant_id == current.tenant_id)
            .order_by(RoiAttributionSnapshot.period_start.desc())
            .limit(50)
        ).scalars()
    )
    return {
        "snapshots": [
            {
                "id": r.id,
                "period_start": r.period_start,
                "period_end": r.period_end,
                "blended_roas": r.blended_roas,
                "channels": r.channels,
            }
            for r in rows
        ]
    }
