"""Read published social posts' figures back from the platforms.

``collect(db, org)`` is the whole job for one workspace. It finds the social
posts published in the last ``social_metrics_lookback_days`` (tenant-filtered,
ids and network only — never the post text), asks each network's connector
for their latest totals in small batches, and stores:

* one ``social_post_metrics`` row per post, overwritten with the newest
  totals. A figure the platform did not report is stored as ``None`` — not
  reported is not the same as zero;
* one ``metric_points`` row per network per metric for today
  (``source="social"``, ``dimension=<network>``): the sum of the posts read in
  this run, so the Insights trend has one number per day. Only for a network
  read *completely*: when a batch failed, answered with something that is
  not a mapping, left a post unanswered (a non-dict figure), or a cap left
  posts unread, that network gets no point today, because a partial sum
  would overwrite the day's total with a smaller number. Its per-post rows
  are still stored.

Connectors opt in by having ``read_post_metrics`` (see :class:`PostMetricsReader`);
one without it is skipped, never failed. Reads are bounded: at most
``MAX_POSTS_PER_RUN`` posts per workspace per run, ``BATCH_SIZE`` ids per call,
and X — which bills every read — at most ``x_metrics_max_reads_per_run``.

``record_points`` is the one writer of ``metric_points``; the daily rollup
uses it too.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.social import POST_METRIC_FIELDS, MetricPoint, SocialPostMetric
from app.models.workspace import Organization

log = get_logger(__name__)

#: ``metric_points.source`` for social figures; the network is the dimension.
SOCIAL_SOURCE = "social"
#: Post ids per ``read_post_metrics`` call.
BATCH_SIZE = 50
#: Posts read per workspace per run, newest first, across every network.
MAX_POSTS_PER_RUN = 500
#: The X connector's slug. X charges per read, so it has its own cap.
X_CONNECTOR = "x_twitter"
#: Largest value a BIGINT column holds.
_BIGINT_MAX = 2**63 - 1
#: Rows per ``metric_points`` insert statement.
_POINTS_PER_STATEMENT = 500

# Column widths on ``metric_points`` (app/models/social.py).
_SOURCE_MAX = 64
_METRIC_MAX = 32
_DIMENSION_MAX = 128
_NETWORK_MAX = 32
_REMOTE_ID_MAX = 255


class PostMetricsReader(Protocol):
    """What a connector offers to have its posts' figures collected.

    ``remote_ids`` are the platform's post ids exactly as stored on the asset
    (``MarketingAsset.remote_id``). The reply maps each id the platform
    answered for to its figures, keyed by the names in ``POST_METRIC_FIELDS``
    (impressions, reach, engagements, clicks, likes, comments, shares, saves).
    A figure the platform does not report is ``None``; extra keys (Reddit's
    score, upvote_ratio) are allowed and ignored here. Ids it did not answer
    for are simply absent. Raise on a failed call; it is logged and the
    connector is skipped for this run.
    """

    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, int | float | None]]: ...


@dataclass(frozen=True, slots=True)
class Point:
    """One daily figure for ``metric_points``."""

    day: date
    source: str
    metric: str
    value: float
    dimension: str = ""


@dataclass(slots=True)
class CollectResult:
    """Counts from one workspace's collection run. Ids and counts only."""

    #: Post ids sent to platforms (what X bills for).
    posts_requested: int = 0
    #: ``social_post_metrics`` rows written.
    posts_updated: int = 0
    #: Networks whose connector is missing or has no ``read_post_metrics``.
    connectors_skipped: int = 0
    #: Networks whose read failed this run.
    connectors_failed: int = 0
    networks: set[str] = field(default_factory=set)
    #: Networks read only in part (a failed batch, a refused post, or a cap): no daily point.
    incomplete_networks: set[str] = field(default_factory=set)


# ── Points ─────────────────────────────────────────────────────────────────
def _clean_point(point: Point) -> Point | None:
    """``point`` if it can be stored as-is, else ``None`` (never truncated)."""
    if not isinstance(point.day, date) or isinstance(point.day, datetime):
        return None
    if not (0 < len(point.source) <= _SOURCE_MAX and 0 < len(point.metric) <= _METRIC_MAX):
        return None
    if len(point.dimension) > _DIMENSION_MAX:
        return None
    if isinstance(point.value, bool) or not isinstance(point.value, (int, float)):
        return None
    if not math.isfinite(point.value):
        return None
    return point


def record_points(db: Session, *, tenant_id: str, points: Iterable[Point]) -> int:
    """Set each day's figure, replacing an earlier one for the same key.

    Idempotent, like ``metrics.record_snapshot``: a late correction from the
    source overwrites the old number. Points that cannot be stored (empty or
    over-long names, NaN, infinity) are dropped. Returns how many were sent.
    """
    keyed: dict[tuple[date, str, str, str], float] = {}
    dropped = 0
    for raw in points:
        point = _clean_point(raw)
        if point is None:
            dropped += 1
            continue
        # One statement may not touch the same row twice; the last value wins.
        keyed[(point.day, point.source, point.metric, point.dimension)] = float(point.value)
    if dropped:
        log.warning("Dropped %d metric point(s) that could not be stored", dropped)
    if not keyed:
        return 0

    rows = [
        {
            "tenant_id": tenant_id,
            "day": day,
            "source": source,
            "metric": metric,
            "dimension": dimension,
            "value": value,
        }
        for (day, source, metric, dimension), value in keyed.items()
    ]
    for start in range(0, len(rows), _POINTS_PER_STATEMENT):
        stmt = insert(MetricPoint).values(rows[start : start + _POINTS_PER_STATEMENT])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_metric_points_key",
            set_={"value": stmt.excluded.value, "updated_at": utcnow()},
        )
        db.execute(stmt)
    return len(rows)


# ── Collection ─────────────────────────────────────────────────────────────
def network_for(channel: str) -> str:
    """The network key (facebook, x …) for a connector slug, else the slug."""
    from app.agents.social_media_manager.platforms import NETWORKS

    for key, network in NETWORKS.items():
        if network.connector == channel:
            return key
    return channel[:_NETWORK_MAX]


def _count(value: Any) -> int | None:  # noqa: ANN401 - platform data is untyped
    """A platform figure as a storable count, or ``None`` if it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    count = int(value)
    if count < 0 or count > _BIGINT_MAX:
        return None
    return count


@dataclass(frozen=True, slots=True)
class _Post:
    asset_id: str
    channel: str
    remote_id: str


def _posted(db: Session, *, tenant_id: str, since: datetime) -> tuple[list[_Post], bool]:
    """Published social posts with a platform id, newest first. Ids only.

    The run cap is judged on the query's row count *before* unusable remote
    ids are dropped, so a long or empty id cannot hide that posts were unread.
    """
    rows = db.execute(
        select(MarketingAsset.id, MarketingAsset.channel, MarketingAsset.remote_id)
        .where(
            MarketingAsset.tenant_id == tenant_id,
            MarketingAsset.kind == AssetKind.SOCIAL_POST,
            MarketingAsset.status == AssetStatus.PUBLISHED,
            MarketingAsset.remote_id != "",
            MarketingAsset.published_at.is_not(None),
            MarketingAsset.published_at >= since,
        )
        .order_by(MarketingAsset.published_at.desc())
        # One more than is read, so the caller can tell the cap cut posts off.
        .limit(MAX_POSTS_PER_RUN + 1)
    ).all()
    capped = len(rows) > MAX_POSTS_PER_RUN
    posts = [
        _Post(asset_id=str(row[0]), channel=str(row[1] or ""), remote_id=str(row[2] or ""))
        for row in rows
        if row[1] and row[2] and len(str(row[2])) <= _REMOTE_ID_MAX
    ]
    return posts[:MAX_POSTS_PER_RUN], capped


def _store_post(
    db: Session,
    *,
    tenant_id: str,
    post: _Post,
    network: str,
    figures: dict[str, int | None],
    fetched_at: datetime,
) -> None:
    values: dict[str, Any] = {
        "network": network,
        "remote_id": post.remote_id,
        "fetched_at": fetched_at,
        **figures,
    }
    stmt = insert(SocialPostMetric).values(tenant_id=tenant_id, asset_id=post.asset_id, **values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_social_post_metrics_asset",
        # None overwrites: "not reported this time" must not keep a stale number.
        set_=values | {"updated_at": utcnow()},
        # Belt and braces with RLS: never touch another workspace's row.
        where=SocialPostMetric.tenant_id == tenant_id,
    )
    db.execute(stmt)


def collect(
    db: Session,
    org: Organization,
    *,
    bundle: Any = None,  # noqa: ANN401 - a ConnectorBundle, or a test double
    now: datetime | None = None,
) -> CollectResult:
    """Collect the latest figures for one workspace's recent posts.

    The caller owns the transaction (and the tenant pin). ``bundle`` is built
    and closed here when not given.
    """
    result = CollectResult()
    moment = now or utcnow()
    since = moment - timedelta(days=settings.social_metrics_lookback_days)
    posts, capped_overall = _posted(db, tenant_id=org.id, since=since)
    if not posts:
        return result

    by_channel: dict[str, list[_Post]] = {}
    for post in posts:
        by_channel.setdefault(post.channel, []).append(post)

    owned_bundle = bundle is None
    if owned_bundle:
        from app.services.connectors import build_connector_bundle

        bundle = build_connector_bundle(db, org)
    totals: dict[str, dict[str, int]] = {}
    try:
        for channel, channel_posts in by_channel.items():
            connector = bundle.instances.get(channel)
            if connector is None or not callable(getattr(connector, "read_post_metrics", None)):
                result.connectors_skipped += 1
                continue
            reader: PostMetricsReader = connector
            network = network_for(channel)
            network_totals = totals.setdefault(network, {})
            if capped_overall:
                result.incomplete_networks.add(network)
            if channel == X_CONNECTOR and len(channel_posts) > settings.x_metrics_max_reads_per_run:
                channel_posts = channel_posts[: settings.x_metrics_max_reads_per_run]
                result.incomplete_networks.add(network)
            for start in range(0, len(channel_posts), BATCH_SIZE):
                batch = channel_posts[start : start + BATCH_SIZE]
                result.posts_requested += len(batch)
                try:
                    reply = reader.read_post_metrics([p.remote_id for p in batch])
                except Exception as exc:  # noqa: BLE001 - one network must not stop the rest
                    log_failure(
                        log, "Social metrics read failed", exc,
                        tenant=org.id, connector=channel, batch=len(batch),
                    )
                    result.connectors_failed += 1
                    result.incomplete_networks.add(network)
                    break
                if not isinstance(reply, dict):
                    result.incomplete_networks.add(network)
                    continue
                answered = 0
                for post in batch:
                    raw = reply.get(post.remote_id)
                    if not isinstance(raw, dict):
                        continue
                    answered += 1
                    figures = {name: _count(raw.get(name)) for name in POST_METRIC_FIELDS}
                    _store_post(
                        db, tenant_id=org.id, post=post, network=network,
                        figures=figures, fetched_at=moment,
                    )
                    result.posts_updated += 1
                    result.networks.add(network)
                    for name, value in figures.items():
                        if value is not None:
                            network_totals[name] = network_totals.get(name, 0) + value
                if answered < len(batch):
                    result.incomplete_networks.add(network)
    finally:
        if owned_bundle:
            bundle.close()

    if result.incomplete_networks:
        log.info(
            "Social metrics: no daily totals for %d network(s) read in part (tenant %s)",
            len(result.incomplete_networks), org.id,
        )
    today = moment.date()
    record_points(
        db,
        tenant_id=org.id,
        points=(
            Point(day=today, source=SOCIAL_SOURCE, metric=name, value=float(value), dimension=network)
            for network, figures in totals.items()
            # A partial sum would replace the day's total with a smaller one.
            if network not in result.incomplete_networks
            # A metric no post reported gets no row: no data, not zero.
            for name, value in figures.items()
        ),
    )
    return result
