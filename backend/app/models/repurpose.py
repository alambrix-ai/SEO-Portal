"""Content repurposing jobs linking a source article to channel drafts."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ContentRepurposeJob(Base, TimestampMixin, TenantScopedMixin):
    """One source article/page turned into multi-channel drafts."""

    __tablename__ = "content_repurpose_jobs"
    __table_args__ = (
        Index("ix_repurpose_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: MarketingAsset id (article) or SeoPage id.
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(FK, nullable=False, index=True)
    source_title: Mapped[str] = mapped_column(String(300), default="")
    #: Encrypted derived pack (social / email / script drafts).
    pack_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: proposed | approved | rejected
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    asset_ids: Mapped[list] = mapped_column(JSONDoc, default=list)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
