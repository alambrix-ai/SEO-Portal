"""Anomaly alert rules and fired events."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class AlertRule(Base, TimestampMixin, TenantScopedMixin):
    """One threshold rule for a workspace metric."""

    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_alert_rule_name"),
        Index("ix_alert_rule_tenant_enabled", "tenant_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: organic_sessions_drop | ad_spend_spike | conversions_drop
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    #: relative drop/spike fraction, e.g. 0.3 = 30%
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    #: email | slack | webhook (webhook URL must pass egress allowlist at fire time)
    channel: Mapped[str] = mapped_column(String(32), default="email", nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[str] = mapped_column(String(8), default="on", nullable=False)


class AlertEvent(Base, TimestampMixin, TenantScopedMixin):
    """One fired alert, deduped by rule + day fingerprint."""

    __tablename__ = "alert_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_alert_event_dedupe"),
        Index("ix_alert_event_tenant_at", "tenant_id", "fired_at"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: open | acknowledged | resolved
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
