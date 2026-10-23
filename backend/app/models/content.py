"""Marketing assets the content agents produce, and the images made for them.

One table for every kind of asset — an article, a search campaign, a social
post, an email — because the console treats them alike: a queue to review,
preview, publish or export. What differs per kind is the *content*, kept as a
JSON document (encrypted under the organisation's key, like page bodies) whose
shape the producing agent owns.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, TimestampMixin, new_id


class AssetKind:
    ARTICLE = "article"
    SEARCH_CAMPAIGN = "search_campaign"
    SOCIAL_POST = "social_post"
    EMAIL = "email"
    #: A Creative Studio output: a banner, social image, flyer, brochure or
    #: email header, rendered from a creative job.
    CREATIVE = "creative"
    #: Short-form video script + captions (text asset; thumbnails are CREATIVE).
    VIDEO_SCRIPT = "video_script"


class AssetStatus:
    #: Written, waiting for a person (or for the policy to publish it).
    DRAFT = "draft"
    #: Approved; the channel has no publishing API, so it is ready to download.
    APPROVED = "approved"
    #: Approved, publishing on its scheduled date.
    SCHEDULED = "scheduled"
    #: Claimed by the publisher and being sent to the platform right now.
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    #: Approved and downloaded for use elsewhere (no API for this channel).
    EXPORTED = "exported"
    REJECTED = "rejected"
    #: The platform refused it; ``error`` says why.
    FAILED = "failed"


class MarketingAsset(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "marketing_assets"
    __table_args__ = (
        Index("ix_assets_tenant_kind_status", "tenant_id", "kind", "status"),
        Index("ix_assets_tenant_scheduled", "tenant_id", "scheduled_for"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Where it is meant to go: wordpress, google_ads, instagram, mailchimp …
    channel: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    #: JSON document, encrypted (Ctx.ASSET_CONTENT). Shape depends on ``kind``.
    content_encrypted: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=AssetStatus.DRAFT, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    remote_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="")
    #: GeneratedImage ids, in display order.
    image_ids: Mapped[list] = mapped_column(JSONB, default=list)
    #: Structural facts only (keyword, word count, score) — no customer copy.
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class GeneratedImage(Base, TenantScopedMixin):
    __tablename__ = "generated_images"

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="")
    #: Which AI integration made it (openai, google_gemini).
    provider: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    mime: Mapped[str] = mapped_column(String(40), default="image/png", nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
