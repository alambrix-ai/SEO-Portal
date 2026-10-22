"""Performance figures read back from the platforms: per post, and per day.

Two tables, because the console asks two different questions:

* ``social_post_metrics``  "how is this post doing?" One row per published
                           post (``marketing_assets`` row), overwritten with
                           the platform's latest lifetime totals each time
                           they are collected.
* ``metric_points``        "how did this go over time?" One number per
                           organisation per day per source per metric (and
                           optional dimension, e.g. a network or campaign):
                           Search Console clicks, ad spend per channel, social
                           reach per network and so on.

Every social count is nullable on purpose. Platforms report different things
(Reddit has no impressions; Instagram no longer reports them at all), and a
figure the platform did not give is unknown, not zero. Showing 0 would tell a
marketer a post reached nobody when we simply do not know.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id

#: The counts ``read_post_metrics`` returns, in display order. Each is a
#: nullable column on ``SocialPostMetric``.
POST_METRIC_FIELDS: tuple[str, ...] = (
    "impressions",
    "reach",
    "engagements",
    "clicks",
    "likes",
    "comments",
    "shares",
    "saves",
)


def _non_negative(column: str, table: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"ck_{table}_{column}")


class SocialPostMetric(Base, TimestampMixin, TenantScopedMixin):
    """The latest totals the platform reported for one published post."""

    __tablename__ = "social_post_metrics"
    __table_args__ = (
        UniqueConstraint("asset_id", name="uq_social_post_metrics_asset"),
        Index("ix_social_post_metrics_tenant_network_fetched", "tenant_id", "network", "fetched_at"),
        *(_non_negative(field, "social_post_metrics") for field in POST_METRIC_FIELDS),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: The post. The foreign key does not check that it is the same
    #: organisation's: load the asset through a tenant-filtered query first.
    asset_id: Mapped[str] = mapped_column(
        FK, ForeignKey("marketing_assets.id", ondelete="CASCADE"), nullable=False
    )
    #: Network key from ``agents/social_media_manager/platforms.NETWORKS``
    #: (facebook, instagram …).
    network: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The platform's id for the post, as published (``MarketingAsset.remote_id``).
    remote_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: When these figures were read from the platform.
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # None = the platform does not report it (or did not this time); never 0.
    impressions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reach: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagements: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    clicks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    likes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comments: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    saves: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class MetricPoint(Base, TimestampMixin, TenantScopedMixin):
    """One daily figure from one source: e.g. Search Console clicks on 3 Oct.

    A day with no row is a day with no data, which the reports must show as
    such rather than as 0.
    """

    __tablename__ = "metric_points"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "day", "source", "metric", "dimension", name="uq_metric_points_key"
        ),
        Index("ix_metric_points_tenant_metric_day", "tenant_id", "metric", "day"),
        CheckConstraint("source <> ''", name="ck_metric_points_source"),
        CheckConstraint("metric <> ''", name="ck_metric_points_metric"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    #: Connector slug the figure came from (google_search_console, meta_ads …).
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    #: What was measured: clicks, impressions, position, spend, reach …
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Optional breakdown (a network, channel or campaign id); "" for none.
    dimension: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
