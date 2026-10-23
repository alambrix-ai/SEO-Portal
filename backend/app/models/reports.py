"""Scheduled autopilot reports and their encrypted artifacts."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ScheduledReport(Base, TimestampMixin, TenantScopedMixin):
    """One workspace's weekly digest configuration."""

    __tablename__ = "scheduled_reports"
    __table_args__ = (Index("ix_scheduled_report_tenant_kind", "tenant_id", "kind"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: weekly_autopilot
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="weekly_autopilot")
    #: Enabled flag as string for simple CHECK-free toggles: on | off
    status: Mapped[str] = mapped_column(String(16), default="on", nullable=False)
    #: Workspace member user ids allowed to receive the digest (never free-form emails).
    recipient_user_ids: Mapped[list] = mapped_column(JSONDoc, default=list)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportArtifact(Base, TimestampMixin, TenantScopedMixin):
    """One generated report body (encrypted) plus optional storage ref."""

    __tablename__ = "report_artifacts"
    __table_args__ = (Index("ix_report_artifact_tenant_at", "tenant_id", "generated_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    schedule_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("scheduled_reports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="weekly_autopilot")
    #: Non-sensitive subject line (no PII beyond workspace name necessity).
    subject: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    storage_ref: Mapped[str] = mapped_column(String(512), default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
