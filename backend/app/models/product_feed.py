"""Merchant-oriented product feed findings awaiting approved fixes."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ProductFeedFinding(Base, TimestampMixin, TenantScopedMixin):
    """One feed/schema issue on a Creative Studio ``Product`` row."""

    __tablename__ = "product_feed_findings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "product_id", "kind", name="uq_product_feed_finding"
        ),
        Index("ix_product_feed_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(
        FK, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    #: open | proposed | fixed | ignored
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    proposed_patch: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
