"""Programmatic ads models: channels, budgets, creatives, audiences, fraud."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, TimestampMixin, new_id


class AdChannel(StrEnum):
    GOOGLE = "google"
    META = "meta"
    LINKEDIN = "linkedin"
    TIKTOK = "tiktok"
    DSP = "dsp"


CHANNEL_LABELS: dict[str, str] = {
    AdChannel.GOOGLE.value: "Google Ads",
    AdChannel.META.value: "Meta Ads",
    AdChannel.LINKEDIN.value: "LinkedIn Ads",
    AdChannel.TIKTOK.value: "TikTok Ads",
    AdChannel.DSP.value: "DSP / Exchange",
}

# The connector each channel spends through — a channel with no connected
# connector is reported but never bid on.
CHANNEL_CONNECTOR: dict[str, str] = {
    AdChannel.GOOGLE.value: "google_ads",
    AdChannel.META.value: "meta_ads",
    AdChannel.LINKEDIN.value: "linkedin_ads",
    AdChannel.TIKTOK.value: "tiktok_ads",
    AdChannel.DSP.value: "dsp_exchange",
}


class BudgetAllocation(Base, TimestampMixin, TenantScopedMixin):
    """Current spend split and measured CAC for one channel.

    The predictive budget engine rewrites ``percent`` on its schedule; ``cac``
    is refreshed from each channel's own reporting API via its connector.
    """

    __tablename__ = "budget_allocations"
    __table_args__ = (UniqueConstraint("tenant_id", "channel", name="uq_budget_tenant_channel"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cac: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    spend: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Percentage points this channel moved on the engine's last pass.
    last_shift: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # A locked channel keeps its share regardless of CAC — the manual override.
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AdCreative(Base, TimestampMixin, TenantScopedMixin):
    """A DCO-generated variant: banner dimensions plus the copy strings.

    Copy is the customer's brand voice and is encrypted under their key; the
    platform, dimensions and performance counters stay queryable.
    """

    __tablename__ = "ad_creatives"
    __table_args__ = (Index("ix_creative_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    dimensions: Mapped[str] = mapped_column(String(24), nullable=False)

    headline_encrypted: Mapped[str] = mapped_column(Text, default="")
    body_copy_encrypted: Mapped[str] = mapped_column(Text, default="")
    call_to_action: Mapped[str] = mapped_column(String(80), default="")

    # Product metadata the generator pulled straight from the source page.
    source_page_url: Mapped[str] = mapped_column(String(512), default="")
    source_product: Mapped[str] = mapped_column(String(255), default="")
    audience_segment: Mapped[str] = mapped_column(String(120), default="")

    # generated | queued | live | rejected
    status: Mapped[str] = mapped_column(String(16), default="generated", nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ctr: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class AudienceCluster(Base, TimestampMixin, TenantScopedMixin):
    """A first-party lookalike segment built without third-party cookies.

    Interaction markers are embedded and clustered inside the tenant's own
    boundary, so no identifiers are shipped to an ad platform — only the
    resulting segment size and signal labels.
    """

    __tablename__ = "audience_clusters"

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    centroid_dims: Mapped[int] = mapped_column(Integer, default=64, nullable=False)
    # Mean intra-cluster cohesion; a low value means the segment is too
    # diffuse to activate.
    cohesion: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    top_signals: Mapped[list] = mapped_column(JSONB, default=list)
    activated_channels: Mapped[list] = mapped_column(JSONB, default=list)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FraudEvent(Base, TenantScopedMixin):
    """One blocked placement from the click-fraud / ad-waste controller."""

    __tablename__ = "fraud_events"
    __table_args__ = (Index("ix_fraud_tenant_at", "tenant_id", "at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(32), default="Blocked", nullable=False)
    channel: Mapped[str] = mapped_column(String(24), default="")
    impressions_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    spend_saved: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Detection features only — never a raw IP or device id.
    signal: Mapped[dict] = mapped_column(JSONB, default=dict)
