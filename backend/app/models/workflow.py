"""Multi-step approvals, public API tokens, webhooks, and ROI snapshots."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ApprovalStep(Base, TimestampMixin, TenantScopedMixin):
    """One step in a multi-approver chain on an approval item."""

    __tablename__ = "approval_steps"
    __table_args__ = (
        UniqueConstraint("approval_item_id", "step_order", name="uq_approval_step_order"),
        Index("ix_approval_steps_tenant_item", "tenant_id", "approval_item_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    approval_item_id: Mapped[str] = mapped_column(
        FK, ForeignKey("approval_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Role slug required, or empty when any full-access approver may act.
    required_role: Mapped[str] = mapped_column(String(32), default="")
    #: Optional specific approver (blind index); empty means any matching role.
    approver_email_index: Mapped[str] = mapped_column(String(64), default="")
    #: pending | approved | rejected | skipped | escalated
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    sla_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    notes: Mapped[str] = mapped_column(String(500), default="")


class ApiToken(Base, TimestampMixin, TenantScopedMixin):
    """Hashed, scoped public API token — plaintext shown once at mint."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        UniqueConstraint("tenant_id", "token_hash", name="uq_api_token_hash"),
        Index("ix_api_tokens_prefix", "token_prefix"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Scope strings, e.g. ["approvals:read", "metrics:read"].
    scopes: Mapped[list] = mapped_column(JSONDoc, default=list)
    #: Optional encrypted metadata (created-by note); never the secret.
    meta_encrypted: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)


class OutboundWebhook(Base, TimestampMixin, TenantScopedMixin):
    """Tenant webhook endpoint — secret encrypted; delivery is egress-gated."""

    __tablename__ = "outbound_webhooks"
    __table_args__ = (Index("ix_outbound_webhooks_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: Event names this subscription receives.
    events: Mapped[list] = mapped_column(JSONDoc, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)


class WebhookDeliveryLog(Base, TimestampMixin, TenantScopedMixin):
    """Replay-protection ledger: nonce + timestamp per delivery attempt."""

    __tablename__ = "webhook_delivery_logs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "nonce", name="uq_webhook_delivery_nonce"),
        Index("ix_webhook_delivery_hook", "tenant_id", "webhook_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    webhook_id: Mapped[str] = mapped_column(
        FK, ForeignKey("outbound_webhooks.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RoiAttributionSnapshot(Base, TimestampMixin, TenantScopedMixin):
    """Channel ROI / attribution roll-up for reporting (no PII)."""

    __tablename__ = "roi_attribution_snapshots"
    __table_args__ = (
        Index("ix_roi_snapshots_tenant_period", "tenant_id", "period_start"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Structural metrics only: {channel: {spend, revenue, conversions}}.
    channels: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    blended_roas: Mapped[str] = mapped_column(String(32), default="0")
    notes: Mapped[str] = mapped_column(String(500), default="")
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)
