"""Candidates for the content-refresh agent (pages losing clicks or rank)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ContentRefreshCandidate(Base, TimestampMixin, TenantScopedMixin):
    """One URL the refresh agent thinks needs attention."""

    __tablename__ = "content_refresh_candidates"
    __table_args__ = (
        Index("ix_refresh_candidates_tenant_status", "tenant_id", "status"),
        Index("ix_refresh_candidates_tenant_url", "tenant_id", "url"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    page_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="SET NULL"), nullable=True
    )
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    reason_codes: Mapped[list] = mapped_column(JSONB, default=list)
    prior_clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recent_clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prior_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: open | proposed | skipped
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    rationale_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
