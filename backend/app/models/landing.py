"""Landing page drafts and A/B experiment metadata."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class LandingPage(Base, TimestampMixin, TenantScopedMixin):
    """A draft or live landing page managed through the approval gate."""

    __tablename__ = "landing_pages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "path", name="uq_landing_page_path"),
        Index("ix_landing_page_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Encrypted HTML body.
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: draft | proposed | live | archived
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    cms_id: Mapped[str] = mapped_column(String(128), default="")
    cms_slug: Mapped[str] = mapped_column(String(64), default="")
    cms_url: Mapped[str] = mapped_column(String(1000), default="")
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AbExperiment(Base, TimestampMixin, TenantScopedMixin):
    """A/B test metadata — traffic split starts only after HIGH approval."""

    __tablename__ = "ab_experiments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_ab_experiment_slug"),
        Index("ix_ab_experiment_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    #: Control landing page.
    control_page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("landing_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Variant landing page.
    variant_page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("landing_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Traffic weight for the variant (0–100); control gets the remainder.
    variant_weight: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    #: Metric key recorded into metric_points (e.g. sessions, conversions).
    metric_key: Mapped[str] = mapped_column(String(40), default="sessions", nullable=False)
    control_metric: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    variant_metric: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: proposed | running | stopped | rejected
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONDoc, default=dict)
