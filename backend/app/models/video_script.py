"""Short-form video script jobs for Creative Studio expansion."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class VideoScriptJob(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "video_script_jobs"
    __table_args__ = (Index("ix_video_script_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Encrypted script + caption lines.
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    thumbnail_formats: Mapped[list] = mapped_column(JSONDoc, default=list)
    asset_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    #: proposed | approved | rejected
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    product_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
