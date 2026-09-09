"""Off-page authority models: backlink targets, pitches, competitor alerts."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
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
from app.db.types import EncryptedString


class BacklinkStatus(StrEnum):
    DISCOVERED = "Discovered"
    PITCHED = "Pitched"
    REPLIED = "Replied"
    WON = "Won"
    REJECTED = "Rejected"


class BacklinkTarget(Base, TimestampMixin, TenantScopedMixin):
    """A placement opportunity the discovery crawler surfaced."""

    __tablename__ = "backlink_targets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", name="uq_backlink_tenant_domain"),
        Index("ix_backlink_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    authority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Guest Post | Directory | Resource Page
    placement_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=BacklinkStatus.DISCOVERED.value, nullable=False
    )

    # Cosine similarity between the target page's embedding and the client's
    # topic embedding — how the crawler decides a domain is contextually right.
    relevance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # A webmaster's address is personal data even when publicly listed.
    contact_email: Mapped[str | None] = mapped_column(
        EncryptedString("backlink_targets.contact_email"), nullable=True
    )
    discovered_via: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutreachPitch(Base, TimestampMixin, TenantScopedMixin):
    """A generated digital-PR pitch, with the sentiment tuning applied.

    Subject and body are the customer's outbound messaging, so both are
    encrypted under the organisation's own key.
    """

    __tablename__ = "outreach_pitches"
    __table_args__ = (Index("ix_pitch_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    target_id: Mapped[str] = mapped_column(
        FK, ForeignKey("backlink_targets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_domain: Mapped[str] = mapped_column(String(255), nullable=False)

    subject_encrypted: Mapped[str] = mapped_column(Text, default="")
    body_encrypted: Mapped[str] = mapped_column(Text, default="")

    # Which messaging variation sentiment analysis selected.
    tone: Mapped[str] = mapped_column(String(40), default="professional", nullable=False)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    variant: Mapped[str] = mapped_column(String(8), default="A", nullable=False)
    # drafted | queued | sent | replied | bounced
    status: Mapped[str] = mapped_column(String(16), default="drafted", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_counter_pitch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CompetitorAlert(Base, TimestampMixin, TenantScopedMixin):
    """A competitor just earned a link — the trigger for a counter-pitch."""

    __tablename__ = "competitor_alerts"
    __table_args__ = (Index("ix_alert_tenant_detected", "tenant_id", "detected_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    competitor: Mapped[str] = mapped_column(String(160), nullable=False)
    source_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    authority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    counter_launched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    counter_pitch_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
