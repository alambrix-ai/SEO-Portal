"""Commercial billing: accounts, metering, entitlement sync."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.db.base import utcnow
from app.models.billing import BillingAccount, PlanEntitlement, UsageMeter
from app.models.workspace import Organization
from app.services import audit
from app.services.billing_provider import get_provider, plan_catalog
from app.services.encryption import Ctx, OrgCipher

ALLOWED_METRICS = frozenset({"seats_used", "agent_runs", "llm_tokens", "api_calls"})


def ensure_billing_account(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    plan_slug: str = "trial",
    seats: int | None = None,
    actor_email: str,
    provider_name: str | None = None,
) -> BillingAccount:
    catalog = plan_catalog()
    if plan_slug not in catalog:
        raise ValueError(f"unknown plan_slug: {plan_slug}")
    plan = catalog[plan_slug]
    seats = int(seats if seats is not None else plan.get("seats") or 1)
    if seats < 1 or seats > 10_000:
        raise ValueError("seats out of range")
    provider = get_provider(provider_name)
    row = db.execute(
        select(BillingAccount).where(BillingAccount.tenant_id == org.id)
    ).scalar_one_or_none()
    customer = provider.ensure_customer(
        tenant_id=org.id,
        plan_slug=plan_slug,
        seats=seats,
        email_fingerprint=crypto.fingerprint(actor_email),
    )
    if row is None:
        row = BillingAccount(tenant_id=org.id)
        db.add(row)
    row.provider = provider.name
    row.external_customer_id = customer.external_id
    row.status = customer.status
    row.plan_slug = customer.plan_slug
    row.seats_purchased = customer.seats
    row.current_period_end = customer.current_period_end
    row.secret_encrypted = cipher.encrypt_json(
        {"external_customer_id": customer.external_id},
        context=Ctx.BILLING_SECRET,
    )
    db.flush()
    sync_entitlements(db, org, plan_slug=row.plan_slug, actor_email=actor_email)
    # Mirror seats onto org for existing Admin UI fields.
    org.plan_tier = row.plan_slug if row.plan_slug in {"trial", "growth", "enterprise"} else org.plan_tier
    org.plan_name = str(plan.get("name") or row.plan_slug)
    org.seats_total = row.seats_purchased
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="billing.account.ensure",
        module="admin",
        context={"plan_slug": row.plan_slug, "provider": row.provider, "seats": row.seats_purchased},
    )
    return row


def change_plan(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    plan_slug: str,
    seats: int | None = None,
    actor_email: str,
) -> BillingAccount:
    row = db.execute(
        select(BillingAccount).where(BillingAccount.tenant_id == org.id)
    ).scalar_one_or_none()
    if row is None:
        return ensure_billing_account(
            db, org, cipher, plan_slug=plan_slug, seats=seats, actor_email=actor_email
        )
    catalog = plan_catalog()
    if plan_slug not in catalog:
        raise ValueError(f"unknown plan_slug: {plan_slug}")
    seats = int(seats if seats is not None else catalog[plan_slug].get("seats") or row.seats_purchased)
    provider = get_provider(row.provider)
    customer = provider.change_plan(
        external_customer_id=row.external_customer_id,
        plan_slug=plan_slug,
        seats=seats,
    )
    row.plan_slug = customer.plan_slug
    row.seats_purchased = customer.seats
    row.status = customer.status
    row.current_period_end = customer.current_period_end
    row.secret_encrypted = cipher.encrypt_json(
        {"external_customer_id": customer.external_id},
        context=Ctx.BILLING_SECRET,
    )
    db.flush()
    sync_entitlements(db, org, plan_slug=row.plan_slug, actor_email=actor_email)
    org.plan_name = str(catalog[plan_slug].get("name") or plan_slug)
    org.seats_total = row.seats_purchased
    if plan_slug in {"trial", "growth", "enterprise"}:
        org.plan_tier = plan_slug
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="billing.plan.change",
        module="admin",
        context={"plan_slug": plan_slug, "seats": seats},
    )
    return row


def sync_entitlements(
    db: Session,
    org: Organization,
    *,
    plan_slug: str,
    actor_email: str,
) -> PlanEntitlement:
    catalog = plan_catalog()
    plan = catalog.get(plan_slug)
    if plan is None:
        raise ValueError(f"unknown plan_slug: {plan_slug}")
    payload = {
        "features": plan.get("features") or {},
        "metrics": plan.get("metrics") or {},
        "seats": plan.get("seats"),
        # Prices stay in config; we only record that a price exists for the plan.
        "has_price": "price_monthly" in plan,
    }
    row = db.execute(
        select(PlanEntitlement).where(PlanEntitlement.tenant_id == org.id)
    ).scalar_one_or_none()
    if row is None:
        row = PlanEntitlement(tenant_id=org.id, plan_slug=plan_slug)
        db.add(row)
    row.plan_slug = plan_slug
    row.entitlements = payload
    row.synced_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="billing.entitlements.sync",
        module="admin",
        context={"plan_slug": plan_slug},
    )
    return row


def _period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def record_usage(
    db: Session,
    org: Organization,
    *,
    metric: str,
    quantity: int,
    actor_email: str,
    report_to_provider: bool = True,
) -> UsageMeter:
    metric = (metric or "").strip()
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"unsupported metric: {metric}")
    quantity = int(quantity)
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    start, end = _period_bounds()
    row = db.execute(
        select(UsageMeter).where(
            UsageMeter.tenant_id == org.id,
            UsageMeter.metric == metric,
            UsageMeter.period_start == start,
        )
    ).scalar_one_or_none()
    if row is None:
        row = UsageMeter(
            tenant_id=org.id,
            metric=metric,
            period_start=start,
            period_end=end,
            quantity=0,
        )
        db.add(row)
    row.quantity = int(row.quantity) + quantity
    db.flush()
    if report_to_provider:
        account = db.execute(
            select(BillingAccount).where(BillingAccount.tenant_id == org.id)
        ).scalar_one_or_none()
        if account and account.external_customer_id:
            try:
                get_provider(account.provider).report_usage(
                    external_customer_id=account.external_customer_id,
                    metric=metric,
                    quantity=quantity,
                )
            except LookupError:
                pass
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="billing.usage.record",
        module="admin",
        context={"metric": metric, "delta": quantity, "total": row.quantity},
    )
    return row


def usage_within_limit(db: Session, org: Organization, metric: str) -> bool:
    """True when current-period usage is under the plan cap (None = unlimited)."""
    ent = db.execute(
        select(PlanEntitlement).where(PlanEntitlement.tenant_id == org.id)
    ).scalar_one_or_none()
    if ent is None:
        catalog = plan_catalog()
        plan = catalog.get("trial") or next(iter(catalog.values()), {})
        caps = (plan.get("metrics") or {})
    else:
        caps = (ent.entitlements or {}).get("metrics") or {}
    cap = caps.get(metric)
    if cap is None:
        return True
    start, _ = _period_bounds()
    row = db.execute(
        select(UsageMeter).where(
            UsageMeter.tenant_id == org.id,
            UsageMeter.metric == metric,
            UsageMeter.period_start == start,
        )
    ).scalar_one_or_none()
    used = int(row.quantity) if row else 0
    return used < int(cap)


def resolved_feature(db: Session, org: Organization, feature: str) -> dict:
    ent = db.execute(
        select(PlanEntitlement).where(PlanEntitlement.tenant_id == org.id)
    ).scalar_one_or_none()
    if ent is None:
        features = (plan_catalog().get("trial") or {}).get("features") or {}
    else:
        features = (ent.entitlements or {}).get("features") or {}
    return dict(features.get(feature) or {})
