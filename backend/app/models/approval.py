"""The human-in-the-loop queue.

Anything an agent produces while its guardrail requires review lands here as
one row. ``target_kind`` / ``target_id`` point back at the object the decision
applies to, and the encrypted payload carries exactly what the agent wants to
do — so approving an item applies it without re-running the agent.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ApprovalType(StrEnum):
    CONTENT_REWRITE = "Content Rewrite"
    AEO_INJECTION = "AEO Injection"
    SCHEMA_PATCH = "Schema Patch"
    PR_PITCH = "PR Pitch"
    AD_CREATIVE = "Ad Creative"
    BUDGET_SHIFT = "Budget Shift"
    AUDIENCE_ACTIVATION = "Audience Activation"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalItem(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "approval_items"
    __table_args__ = (
        Index("ix_approval_tenant_status", "tenant_id", "status"),
        Index("ix_approval_tenant_submitted", "tenant_id", "submitted_at"),
        # The lookup that stops an agent stacking the same proposal every run.
        Index(
            "ix_approval_open_target",
            "tenant_id",
            "status",
            "agent_slug",
            "target_kind",
            "target_id",
        ),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    agent_slug: Mapped[str] = mapped_column(String(64), default="", index=True)
    agent_name: Mapped[str] = mapped_column(String(160), default="")

    # What approving this actually changes.
    # seo_page | schema_patch | aeo_pair | outreach_pitch | ad_creative |
    # budget_plan | audience_cluster
    target_kind: Mapped[str] = mapped_column(String(40), default="")
    # Deliberately not a foreign key: one queue serves several tables.
    target_id: Mapped[str | None] = mapped_column(FK, nullable=True)

    # The pending change, encrypted under the organisation's data key.
    payload_encrypted: Mapped[str] = mapped_column(Text, default="")
    # SHA-256 prefix of the canonical payload, so two proposals can be
    # compared without decrypting either. The ciphertext cannot be compared —
    # AES-GCM uses a fresh nonce per write, so the same payload encrypts
    # differently every time. Non-reversible, and it holds no content.
    payload_digest: Mapped[str] = mapped_column(String(32), default="", index=True)
    # Non-sensitive one-liner for the card, e.g. "submitted 2h ago".
    meta: Mapped[str] = mapped_column(String(120), default="")

    status: Mapped[str] = mapped_column(
        String(16), default=ApprovalStatus.PENDING.value, nullable=False, index=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(160), default="")
    decided_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    # Set when applying an approved change failed downstream.
    apply_error: Mapped[str] = mapped_column(Text, default="")
