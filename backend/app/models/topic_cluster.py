"""Topic clusters and encrypted content briefs for the planner agent."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class TopicCluster(Base, TimestampMixin, TenantScopedMixin):
    """One topical group derived from Search Console queries and page keywords."""

    __tablename__ = "topic_clusters"
    __table_args__ = (
        Index("ix_topic_cluster_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Seed phrases / queries that define the cluster (non-sensitive labels).
    seed_terms: Mapped[list] = mapped_column(JSONDoc, default=list)
    #: Gap score 0–100: how much of the cluster is uncovered by synced pages.
    gap_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: open | planned | archived
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopicClusterPage(Base, TenantScopedMixin):
    """Membership of a synced page in a topic cluster."""

    __tablename__ = "topic_cluster_pages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cluster_id", "page_id", name="uq_topic_cluster_page"),
        Index("ix_topic_cluster_pages_cluster", "tenant_id", "cluster_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    cluster_id: Mapped[str] = mapped_column(
        FK, ForeignKey("topic_clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relevance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class ContentBrief(Base, TimestampMixin, TenantScopedMixin):
    """An encrypted brief for the article writer, produced from a cluster gap."""

    __tablename__ = "content_briefs"
    __table_args__ = (
        Index("ix_content_brief_tenant_status", "tenant_id", "status"),
        Index("ix_content_brief_cluster", "tenant_id", "cluster_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    cluster_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("topic_clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    target_keywords: Mapped[list] = mapped_column(JSONDoc, default=list)
    #: Encrypted free-form brief body (outline, angle, must-cover points).
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: draft | proposed | approved | rejected | written
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
