"""AI visibility tracker: target questions and per-engine observations.

Observations record whether answer engines (ChatGPT, Perplexity, Gemini,
Claude, …) cited the brand and which competitor hosts appeared. Question text
and optional answer excerpts are encrypted under the organisation key.
Cited domain lists are structured hostnames — safe to keep queryable.
"""
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class VisibilityQuery(Base, TimestampMixin, TenantScopedMixin):
    """A question the workspace wants answer engines asked about."""

    __tablename__ = "visibility_queries"
    __table_args__ = (
        Index("ix_visibility_queries_tenant_active", "tenant_id", "active"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    question_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    #: Short clear label for lists (never a substitute for the encrypted text).
    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tags: Mapped[list] = mapped_column(JSONB, default=list)


class VisibilitySettings(Base, TimestampMixin, TenantScopedMixin):
    """Per-workspace visibility config — one row per organisation."""

    __tablename__ = "visibility_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_visibility_settings_tenant"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: Competitor hostnames the tracker should flag when cited (no scheme).
    competitor_domains: Mapped[list] = mapped_column(JSONB, default=list)


class VisibilityObservation(Base, TenantScopedMixin):
    """One engine's answer to one question at one point in time."""

    __tablename__ = "visibility_observations"
    __table_args__ = (
        Index(
            "ix_visibility_obs_tenant_observed",
            "tenant_id",
            "observed_at",
        ),
        Index(
            "ix_visibility_obs_tenant_engine",
            "tenant_id",
            "engine",
            "observed_at",
        ),
        Index("ix_visibility_obs_query", "tenant_id", "query_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    query_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("visibility_queries.id", ondelete="SET NULL"), nullable=True
    )
    #: Denormalised encrypted question so deleting a query keeps history.
    question_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    brand_cited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    brand_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cited_domains: Mapped[list] = mapped_column(JSONB, default=list)
    competitors_cited: Mapped[list] = mapped_column(JSONB, default=list)
    answer_excerpt_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
