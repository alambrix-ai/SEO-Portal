"""Public ad-library snapshots for competitor creative watch."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class CompetitorAd(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "competitor_ads"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "platform", "external_id", name="uq_competitor_ad_external"
        ),
        Index("ix_competitor_ad_tenant_platform", "tenant_id", "platform"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(24), nullable=False)  # meta | google
    advertiser: Mapped[str] = mapped_column(String(200), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Encrypted headline/body/CTA — always treated as untrusted third-party copy.
    copy_encrypted: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Structural facts only (no customer PII).
    detail: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
