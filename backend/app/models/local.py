"""Local business: where a business appears in Maps searches, and its reviews.

Two tables, one per local agent:

* ``local_rank_checks`` — one row per search phrase per check, written by the
  Local Search Tracker. The competitors seen above or around the business are
  stored with the check, because "you are fourth" means nothing without who
  the first three are.
* ``review_replies`` — one row per Google review the Review Responder has
  drafted a reply for. The review text and the reply are customer content, so
  both are encrypted under the organisation's key like page bodies are.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class LocalRankCheck(Base, TenantScopedMixin):
    __tablename__ = "local_rank_checks"
    __table_args__ = (
        Index("ix_local_rank_tenant_checked", "tenant_id", "checked_at"),
        Index("ix_local_rank_tenant_location", "tenant_id", "location_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: Optional multi-location pointer (nullable for legacy single-site checks).
    location_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    phrase: Mapped[str] = mapped_column(String(160), nullable=False)
    #: 1-based position among the results, or None when not in them.
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: How many results Google returned for the phrase (at most 20).
    results_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The business itself at the time of the check.
    place_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    business_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The top results: [{place_id, name, rating, review_count, position}].
    top_results: Mapped[list] = mapped_column(JSONB, default=list)


class ReviewReply(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "review_replies"
    __table_args__ = (UniqueConstraint("tenant_id", "review_name", name="uq_review_reply_review"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: Google's resource name: accounts/…/locations/…/reviews/….
    review_name: Mapped[str] = mapped_column(String(512), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: 1 to 5; 0 when Google did not report one.
    stars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_encrypted: Mapped[str] = mapped_column(Text, default="")
    reply_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: drafted | posted | rejected
    status: Mapped[str] = mapped_column(String(16), default="drafted", nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

