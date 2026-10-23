"""Suggested internal links waiting for approval or already applied."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class InternalLinkSuggestion(Base, TimestampMixin, TenantScopedMixin):
    """One proposed internal link from a source page to a target page."""

    __tablename__ = "internal_link_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_page_id",
            "target_page_id",
            "anchor",
            name="uq_internal_link_identity",
        ),
        Index("ix_internal_link_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    source_page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    target_url: Mapped[str] = mapped_column(String(512), nullable=False)
    anchor: Mapped[str] = mapped_column(String(80), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: proposed | applied | rejected | superseded
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
