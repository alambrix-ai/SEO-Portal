"""Inbound social mentions routed toward review-style replies."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class SocialMention(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "social_mentions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "network", "external_id", name="uq_social_mention_external"
        ),
        Index("ix_social_mention_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    network: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author: Mapped[str] = mapped_column(String(200), default="")
    #: Encrypted mention body — never logged.
    text_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: Encrypted proposed reply.
    reply_encrypted: Mapped[str] = mapped_column(Text, default="")
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral", nullable=False)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: new | routed | replied | ignored
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONDoc, default=dict)
