"""Compliance: retention policies, subject requests, audit exports, AI governance."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id

ALLOWED_DATA_CLASSES = frozenset(
    {
        "login_codes",
        "refresh_tokens",
        "marketing_assets",
        "agent_runs",
        "approval_items",
        "audit_log",
        "creative_jobs",
    }
)


class RetentionPolicy(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "data_class", name="uq_retention_data_class"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: login_codes | refresh_tokens | marketing_assets | agent_runs | ...
    data_class: Mapped[str] = mapped_column(String(64), nullable=False)
    ttl_days: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SubjectRequest(Base, TimestampMixin, TenantScopedMixin):
    """GDPR / DPDP subject access or erasure request."""

    __tablename__ = "subject_requests"
    __table_args__ = ()

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: export | erasure
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Blind index of subject email — never store raw email here.
    subject_email_index: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="received", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    #: Encrypted export artifact reference / notes (no raw PII in evidence).
    result_encrypted: Mapped[str] = mapped_column(Text, default="")
    requested_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)


class AuditExportJob(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "audit_export_jobs"

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    format: Mapped[str] = mapped_column(String(16), default="json", nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    #: HMAC signature over export body for integrity.
    signature: Mapped[str] = mapped_column(String(128), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Encrypted export payload or storage pointer.
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    siem_url: Mapped[str] = mapped_column(String(1000), default="")
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)


class AiGovernancePolicy(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "ai_governance_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_ai_governance_tenant"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: Allow-listed model connector slugs.
    model_allowlist: Mapped[list] = mapped_column(JSONDoc, default=list)
    #: Per-agent daily token/cost caps {agent_slug: max_tokens}.
    agent_cost_caps: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    log_prompts: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    log_actions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
