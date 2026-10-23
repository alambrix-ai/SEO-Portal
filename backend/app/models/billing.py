"""Billing accounts, usage meters, and plan entitlement snapshots."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class BillingAccount(Base, TimestampMixin, TenantScopedMixin):
    """Per-tenant billing record — provider secrets encrypted at rest."""

    __tablename__ = "billing_accounts"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_billing_account_tenant"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: fake | stripe | … — selected from config; never hard-coded in call sites.
    provider: Mapped[str] = mapped_column(String(32), default="fake", nullable=False)
    external_customer_id: Mapped[str] = mapped_column(String(120), default="")
    #: trial | active | past_due | cancelled
    status: Mapped[str] = mapped_column(String(24), default="trial", nullable=False)
    plan_slug: Mapped[str] = mapped_column(String(64), default="trial", nullable=False)
    seats_purchased: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Encrypted provider customer/secret bundle (Ctx.BILLING_SECRET).
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UsageMeter(Base, TimestampMixin, TenantScopedMixin):
    """Aggregated usage for a metric within a billing period."""

    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "metric", "period_start", name="uq_usage_meter_period"
        ),
        Index("ix_usage_meters_tenant_metric", "tenant_id", "metric"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: seats_used | agent_runs | llm_tokens | api_calls
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PlanEntitlement(Base, TimestampMixin, TenantScopedMixin):
    """Resolved entitlements for the tenant's current plan (cached from catalog)."""

    __tablename__ = "plan_entitlements"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_plan_entitlement_tenant"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    plan_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    #: {feature: {allowed, max_per_day, max_history}, seats, metrics: {...}}
    entitlements: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    #: Prices are never stored here — they live in config only.
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)
