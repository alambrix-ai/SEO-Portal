"""Proposals that turn a technical audit finding into an approval item."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class SeoIssueFixProposal(Base, TimestampMixin, TenantScopedMixin):
    """Links one ``SeoIssue`` to the approval item that would fix it.

    Kept separate from ``seo_issues`` so the auditor can stay read-only and so
    a rejected proposal does not mutate the finding's own status.
    """

    __tablename__ = "seo_issue_fix_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "issue_id", name="uq_fix_proposal_issue"),
        Index("ix_fix_proposal_tenant_status", "tenant_id", "status"),
        Index("ix_fix_proposal_approval", "tenant_id", "approval_item_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    issue_id: Mapped[str] = mapped_column(
        FK,
        ForeignKey("seo_issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_id: Mapped[str] = mapped_column(
        FK,
        ForeignKey("seo_pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    fix_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    #: proposed | applied | rejected | superseded
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
