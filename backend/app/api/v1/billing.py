"""Commercial billing admin: plans, seats, usage meters, entitlements."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_db
from app.core.config import settings
from app.core.exceptions import FeatureNotConfiguredError, InvalidInputError
from app.core.rbac import Module
from app.models.billing import BillingAccount, PlanEntitlement, UsageMeter
from app.services import billing as billing_svc
from app.services.billing_provider import plan_catalog

router = APIRouter(prefix="/billing", tags=["billing"])


class EnsureIn(BaseModel):
    plan_slug: str = "trial"
    seats: int | None = None


class ChangePlanIn(BaseModel):
    plan_slug: str
    seats: int | None = None


class UsageIn(BaseModel):
    metric: str
    quantity: int = Field(default=1, ge=0)


@router.get("/catalog")
def get_catalog(
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    current.require_view(Module.ADMIN)
    # Prices come from config; never invent them here.
    # Always an array of `{slug, ...}` — a bare slug→meta map crashes the
    # console (``plans.map is not a function`` → blank screen).
    catalog = plan_catalog()
    plans = [
        {"slug": slug, **(meta if isinstance(meta, dict) else {"name": slug})}
        for slug, meta in catalog.items()
    ]
    return {
        "plans": plans,
        "provider": (settings.billing_provider or "fake").strip().lower() or "fake",
    }


@router.get("/account")
def get_account(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    row = db.execute(
        select(BillingAccount).where(BillingAccount.tenant_id == current.tenant_id)
    ).scalar_one_or_none()
    if row is None:
        return {"account": None}
    return {
        "account": {
            "id": row.id,
            "provider": row.provider,
            "status": row.status,
            "plan_slug": row.plan_slug,
            "seats_purchased": row.seats_purchased,
            "current_period_end": row.current_period_end,
            "external_customer_id": row.external_customer_id,
        }
    }


@router.post("/account")
def ensure_account(
    body: EnsureIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        row = billing_svc.ensure_billing_account(
            db,
            current.organization,
            current.cipher,
            plan_slug=body.plan_slug,
            seats=body.seats,
            actor_email=current.user.email,
        )
        db.commit()
    except FeatureNotConfiguredError:
        raise
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {
        "id": row.id,
        "plan_slug": row.plan_slug,
        "seats_purchased": row.seats_purchased,
        "status": row.status,
    }


@router.post("/plan")
def change_plan(
    body: ChangePlanIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        row = billing_svc.change_plan(
            db,
            current.organization,
            current.cipher,
            plan_slug=body.plan_slug,
            seats=body.seats,
            actor_email=current.user.email,
        )
        db.commit()
    except FeatureNotConfiguredError:
        raise
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {
        "id": row.id,
        "plan_slug": row.plan_slug,
        "seats_purchased": row.seats_purchased,
        "status": row.status,
    }


@router.post("/usage")
def record_usage(
    body: UsageIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        row = billing_svc.record_usage(
            db,
            current.organization,
            metric=body.metric,
            quantity=body.quantity,
            actor_email=current.user.email,
        )
        db.commit()
    except FeatureNotConfiguredError:
        raise
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {"metric": row.metric, "quantity": row.quantity, "period_start": row.period_start}


@router.get("/usage")
def list_usage(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    rows = list(
        db.execute(
            select(UsageMeter).where(UsageMeter.tenant_id == current.tenant_id)
        ).scalars()
    )
    return {
        "meters": [
            {
                "metric": r.metric,
                "quantity": r.quantity,
                "period_start": r.period_start,
                "period_end": r.period_end,
            }
            for r in rows
        ]
    }


@router.get("/entitlements")
def get_entitlements(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    row = db.execute(
        select(PlanEntitlement).where(PlanEntitlement.tenant_id == current.tenant_id)
    ).scalar_one_or_none()
    if row is None:
        return {"plan_slug": None, "entitlements": {}}
    return {
        "plan_slug": row.plan_slug,
        "entitlements": row.entitlements,
        "synced_at": row.synced_at,
    }
