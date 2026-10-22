"""Metric rollups behind the dashboard KPIs, the Reports screen and its trend.

Agents report counters; :func:`accumulate` folds them into today's row. Every
range the console offers is then an aggregate over those rows, so the tiles,
the trend line and the audit log can never disagree about what happened.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.money import compact as money_compact
from app.db.base import utcnow
from app.models.metrics import DailyMetric
from app.models.offpage import BacklinkStatus, BacklinkTarget
from app.models.social import MetricPoint

log = get_logger(__name__)

RANGE_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}

#: Shown on Reports when nothing has been recorded for the range yet.
REPORTS_CONNECT_HINT = (
    "Connect Google Analytics (or another analytics integration) to see your "
    "visitors here. Figures appear the day after you connect."
)
#: Shown instead of a number for a figure nothing in the workspace measures.
NOT_TRACKED = "Not tracked yet"
#: Shown instead of a cost per conversion when ads were measured but nothing
#: converted: there is nothing to divide by, and 0 would read as "free".
NO_CONVERSIONS = "No conversions yet"

#: Sources with READ_SESSIONS whose "sessions" are not website visits:
#: Business Profile reports local impressions there (contract 1.8, H1). The
#: daily rollup records it as ``LOCAL_IMPRESSIONS`` instead.
NOT_WEBSITE_VISITS: frozenset[str] = frozenset({"google_business_profile"})
#: ``metric_points.metric`` for Business Profile's local discovery impressions.
LOCAL_IMPRESSIONS = "local_impressions"
#: ``metric_points`` metrics that say a source measured the day.
_SESSIONS_METRIC = "sessions"
_SPEND_METRIC = "spend"

# Columns agents may accumulate into.
_COUNTERS: frozenset[str] = frozenset(
    {
        "organic_sessions",
        "aeo_citations",
        "pages_optimised",
        "backlinks_won",
        "pitches_sent",
        "ad_spend",
        "conversions",
        "fraud_blocked",
        "spend_saved",
        "referral_spam_blocked",
        "agent_runs",
        "actions_autonomous",
        "actions_approved",
        "actions_rejected",
    }
)

#: Reported by agents but deliberately not stored here: they have no column
#: on DailyMetric, and they are already the live state of the seo_issues
#: table, so a daily copy would only drift. Dropped without a warning.
_NOT_STORED: frozenset[str] = frozenset({"seo_issues_open", "seo_issues_fixed"})

#: Columns that hold a day's measured total from an outside source (analytics,
#: ad platforms) rather than a count of things agents did. They are *set* for
#: a day, never added to, so the daily rollup can run any number of times.
_SNAPSHOTS: frozenset[str] = frozenset({"organic_sessions", "conversions", "ad_spend"})


def record_snapshot(
    db: Session, *, tenant_id: str, day: date, values: dict[str, int | float]
) -> None:
    """Set one day's measured totals, replacing any earlier figure for that day."""
    clean = {k: v for k, v in values.items() if k in _SNAPSHOTS and v is not None}
    if not clean:
        return
    stmt = insert(DailyMetric).values(tenant_id=tenant_id, day=day, **clean)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_metric_tenant_day",
        set_=dict(clean) | {"updated_at": utcnow()},
    )
    db.execute(stmt)


def accumulate(
    db: Session, *, tenant_id: str, values: dict[str, int | float], day: date | None = None
) -> None:
    """Add counters to one day's row, creating it if needed.

    Uses an upsert with an ``ON CONFLICT`` increment, so several agents running
    concurrently cannot lose each other's counts to a read-modify-write race.
    """
    target_day = day or utcnow().date()
    snapshots_sent = sorted(k for k in values if k in _SNAPSHOTS)
    if snapshots_sent:
        log.warning(
            "Ignoring snapshot metric counters sent to accumulate: %s",
            ", ".join(snapshots_sent),
        )
    unknown = set(values) - _COUNTERS - _NOT_STORED
    if unknown:
        log.warning("Ignoring unknown metric counters: %s", ", ".join(sorted(unknown)))

    clean = {k: v for k, v in values.items() if k in (_COUNTERS - _SNAPSHOTS) and v}
    if not clean:
        return

    stmt = insert(DailyMetric).values(tenant_id=tenant_id, day=target_day, **clean)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_metric_tenant_day",
        set_={
            column: getattr(DailyMetric, column) + value for column, value in clean.items()
        }
        | {"updated_at": utcnow()},
    )
    db.execute(stmt)


# ── Aggregates ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class KpiSnapshot:
    """The numbers both the dashboard and the Reports screen render."""

    range_key: str
    #: Website visits measured by analytics in the range (0 when none were).
    organic_sessions: int = 0
    aeo_citations: int = 0
    backlinks_won: int = 0
    #: ``None`` when no ad account was measured in the range, or nothing
    #: converted (see ``blended_cac_note``).
    blended_cac: float | None = None
    #: ``None`` when no ad account was measured in the range.
    ad_spend: float | None = None
    fraud_blocked: int = 0
    referral_spam_blocked: int = 0
    pages_optimised: int = 0
    conversions: int = 0
    spend_saved: float = 0.0
    # Six evenly spaced buckets for the organic-session sparkline. Days with
    # no row count as 0 here, so the numbers stay what the chart has always
    # drawn; ``trend_with_gaps`` says which buckets were never measured.
    trend: list[int] = field(default_factory=list)
    #: The same buckets, averaged over measured days only; ``None`` for a
    #: bucket with no measured day at all, which the chart shows as a gap.
    trend_with_gaps: list[int | None] = field(default_factory=list)
    #: Calendar days the range covers (7, 30 or 90), not the bucket count.
    days: int = 0
    #: False when no day in the range has measured website visits: "no data
    #: yet", not zero. Rows agents write (runs, approvals) do not count.
    available: bool = False
    #: Why ``blended_cac`` is ``None`` ("" when it is a number).
    blended_cac_note: str = ""
    #: Why ``ad_spend`` is ``None`` ("" when it is a number).
    ad_spend_note: str = ""

    def as_display(self) -> dict[str, str | int | float | list[int] | None]:
        return {
            "range": self.range_key,
            "organic_sessions": compact(self.organic_sessions),
            "organic_sessions_raw": self.organic_sessions,
            "aeo_citations": self.aeo_citations,
            "backlinks_won": self.backlinks_won,
            "blended_cac": self.blended_cac,
            "ad_spend": money_compact(self.ad_spend) if self.ad_spend is not None else None,
            "ad_spend_raw": round(self.ad_spend, 2) if self.ad_spend is not None else None,
            "fraud_blocked": self.fraud_blocked,
            "referral_spam_blocked": self.referral_spam_blocked,
            "pages_optimised": self.pages_optimised,
            "conversions": self.conversions,
            "spend_saved": round(self.spend_saved, 2),
            "trend": self.trend,
        }


def compact(value: float) -> str:
    """Format a *count* the way the KPI tiles do: 74.6K, 221.4K, 1.2M.

    Counts only. Amounts of money go through :func:`app.core.money.compact`,
    which carries the currency symbol and groups in lakhs and crores — a spend
    tile reading "1.2M" tells an Indian team nothing they can use.
    """
    number = float(value)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= threshold:
            return f"{number / threshold:.1f}{suffix}"
    return f"{number:,.0f}"


def rows_for_range(
    db: Session, *, tenant_id: str, range_key: str, today: date | None = None
) -> list[DailyMetric]:
    days = RANGE_DAYS.get(range_key, 30)
    start = (today or utcnow().date()) - timedelta(days=days - 1)
    return list(
        db.execute(
            select(DailyMetric)
            .where(DailyMetric.tenant_id == tenant_id, DailyMetric.day >= start)
            .order_by(DailyMetric.day)
        ).scalars()
    )


@dataclass(slots=True)
class Measured:
    """What outside sources actually measured in a range (``metric_points``)."""

    #: Website visits per day, from analytics sources only (never Business
    #: Profile). A day that is absent was not measured.
    sessions: dict[date, int] = field(default_factory=dict)
    #: True when an ad account reported spend for any day in the range.
    ads: bool = False


def _website_visit_sources() -> list[str]:
    from app.connectors.base.connector import Capability
    from app.connectors.base.registry import slugs_with_capability

    return [
        slug for slug in slugs_with_capability(Capability.READ_SESSIONS)
        if slug not in NOT_WEBSITE_VISITS
    ]


def _ad_sources() -> list[str]:
    from app.connectors.base.connector import Capability
    from app.connectors.base.registry import slugs_with_capability

    return slugs_with_capability(Capability.READ_AD_PERFORMANCE)


def measured_for_range(
    db: Session, *, tenant_id: str, range_key: str, today: date | None = None
) -> Measured:
    """Measured website visits per day, and whether ads were measured.

    One tenant-filtered query over ``metric_points``. ``daily_metrics`` is not
    used for this: agents create its rows (runs, approvals) with
    ``organic_sessions`` defaulting to 0, so a row there does not mean the day
    was measured. When several analytics sources measured the same day (the
    workspace switched source), the larger figure is taken, never the sum.
    """
    days = RANGE_DAYS.get(range_key, 30)
    start = (today or utcnow().date()) - timedelta(days=days - 1)
    visit_sources = _website_visit_sources()
    ad_sources = _ad_sources()
    out = Measured()
    if not visit_sources and not ad_sources:
        return out
    rows = db.execute(
        select(MetricPoint.source, MetricPoint.metric, MetricPoint.day, func.sum(MetricPoint.value))
        .where(
            MetricPoint.tenant_id == tenant_id,
            MetricPoint.day >= start,
            MetricPoint.metric.in_([_SESSIONS_METRIC, _SPEND_METRIC]),
            MetricPoint.source.in_([*visit_sources, *ad_sources]),
        )
        .group_by(MetricPoint.source, MetricPoint.metric, MetricPoint.day)
    ).all()
    visit_set, ad_set = set(visit_sources), set(ad_sources)
    for source, metric, day, value in rows:
        if metric == _SESSIONS_METRIC and source in visit_set:
            sessions = max(int(round(float(value or 0.0))), 0)
            out.sessions[day] = max(out.sessions.get(day, 0), sessions)
        elif metric == _SPEND_METRIC and source in ad_set:
            out.ads = True
    return out


def _today_arg(fn: object, today: date) -> dict[str, date]:
    """``today`` when ``fn`` takes it, so older replacements keep working."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if "today" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {"today": today}
    return {}


def snapshot(db: Session, *, tenant_id: str, range_key: str = "30d") -> KpiSnapshot:
    today = utcnow().date()
    rows = rows_for_range(
        db, tenant_id=tenant_id, range_key=range_key, **_today_arg(rows_for_range, today)
    )
    measured = measured_for_range(
        db, tenant_id=tenant_id, range_key=range_key, **_today_arg(measured_for_range, today)
    )
    snap = KpiSnapshot(range_key=range_key if range_key in RANGE_DAYS else "30d")
    snap.days = RANGE_DAYS[snap.range_key]
    # Measured visits only: a row an agent run created is not a measurement.
    snap.available = bool(measured.sessions)
    snap.organic_sessions = sum(measured.sessions.values())

    ad_spend = 0.0
    for row in rows:
        snap.aeo_citations += row.aeo_citations
        snap.backlinks_won += row.backlinks_won
        ad_spend += row.ad_spend
        snap.fraud_blocked += row.fraud_blocked
        snap.referral_spam_blocked += row.referral_spam_blocked
        snap.pages_optimised += row.pages_optimised
        snap.conversions += row.conversions
        snap.spend_saved += row.spend_saved

    if not measured.ads:
        # No ad account measured anything: "Not tracked yet", not a zero spend.
        snap.ad_spend_note = snap.blended_cac_note = NOT_TRACKED
    else:
        snap.ad_spend = ad_spend
        if snap.conversions:
            snap.blended_cac = round(ad_spend / snap.conversions, 2)
        else:
            snap.blended_cac_note = NO_CONVERSIONS
    daily = calendar_series(
        measured.sessions,
        start=today - timedelta(days=snap.days - 1),
        days=snap.days,
    )
    snap.trend = _bucket_trend(daily, buckets=6)
    snap.trend_with_gaps = _bucket_gaps(daily, buckets=6)
    return snap


def calendar_series(values: dict[date, int], *, start: date, days: int) -> list[int | None]:
    """One entry per calendar day from ``start``: the day's value, or ``None``.

    ``None`` is a day nothing was recorded for. The trend used to be built
    from the stored rows alone, so three days of data were stretched across
    the whole chart.
    """
    return [values.get(start + timedelta(days=offset)) for offset in range(days)]


def _chunks(series: Sequence[int | None], buckets: int) -> list[Sequence[int | None]]:
    """Split a calendar series into ``buckets`` consecutive runs of days.

    A series shorter than ``buckets`` is padded at the *old* end with missing
    days, so the most recent day is always the last bucket and nothing is
    repeated to fill the chart.
    """
    if len(series) < buckets:
        series = [None] * (buckets - len(series)) + list(series)
    size = len(series) / buckets
    return [series[int(i * size) : int((i + 1) * size)] for i in range(buckets)]


def _bucket_trend(series: Sequence[int | None], *, buckets: int = 6) -> list[int]:
    """Average a calendar-day series into a fixed number of buckets.

    A fixed count keeps the sparkline's shape comparable across ranges — 7d
    and 90d draw the same number of points. A day with no data counts as 0
    here (the numbers the chart has always drawn); see :func:`_bucket_gaps`
    for the version that keeps them apart.
    """
    out: list[int] = []
    for chunk in _chunks(series, buckets):
        values = [value or 0 for value in chunk]
        out.append(round(sum(values) / len(values)) if values else 0)
    return out


def _bucket_gaps(series: Sequence[int | None], *, buckets: int = 6) -> list[int | None]:
    """Like :func:`_bucket_trend`, averaging measured days only.

    A bucket with no measured day is ``None``: a gap on the chart, not a dip
    to zero.
    """
    out: list[int | None] = []
    for chunk in _chunks(series, buckets):
        measured = [value for value in chunk if value is not None]
        out.append(round(sum(measured) / len(measured)) if measured else None)
    return out


def backlinks_won(db: Session, *, tenant_id: str, range_key: str) -> int | None:
    """Backlinks that reached "Won" in the range, from the backlink tracker.

    ``None`` when the workspace tracks no backlink targets at all: nothing
    measures backlinks there, so the screen says "Not tracked yet" instead
    of a 0 that reads as a result. (``daily_metrics.backlinks_won`` is never
    written, so it is not used.)
    """
    days = RANGE_DAYS.get(range_key, 30)
    since = datetime.combine(utcnow().date() - timedelta(days=days - 1), time.min, tzinfo=UTC)
    row = db.execute(
        select(
            func.count(BacklinkTarget.id),
            func.count(BacklinkTarget.id).filter(
                BacklinkTarget.status == BacklinkStatus.WON.value,
                BacklinkTarget.won_at >= since,
            ),
        ).where(BacklinkTarget.tenant_id == tenant_id)
    ).one()
    tracked, won = int(row[0] or 0), int(row[1] or 0)
    return won if tracked else None


def trend_polyline(trend: list[int], *, width: int = 320, height: int = 120) -> str:
    """Render a trend as SVG ``points``, matching the Reports chart viewBox."""
    if not trend:
        return ""
    top, bottom = 8, height - 8
    peak = max(trend) or 1
    step = width / max(len(trend) - 1, 1)
    points = [
        f"{round(i * step, 1)},{round(bottom - (value / peak) * (bottom - top), 1)}"
        for i, value in enumerate(trend)
    ]
    return " ".join(points)


def today_row(db: Session, *, tenant_id: str) -> DailyMetric | None:
    return db.execute(
        select(DailyMetric).where(
            DailyMetric.tenant_id == tenant_id, DailyMetric.day == utcnow().date()
        )
    ).scalar_one_or_none()


def total_since(db: Session, *, tenant_id: str, column: str, days: int) -> float:
    """Sum one counter over a window. Used by agents that report cumulative work."""
    if column not in _COUNTERS:
        raise ValueError(f"Unknown metric column {column!r}")
    start = utcnow().date() - timedelta(days=days - 1)
    total = db.execute(
        select(func.coalesce(func.sum(getattr(DailyMetric, column)), 0)).where(
            DailyMetric.tenant_id == tenant_id, DailyMetric.day >= start
        )
    ).scalar_one()
    return float(total or 0)
