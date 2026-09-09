"""Metric rollups behind the dashboard KPIs, the Reports screen and its trend.

Agents report counters; :func:`accumulate` folds them into today's row. Every
range the console offers is then an aggregate over those rows, so the tiles,
the trend line and the audit log can never disagree about what happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.money import compact as money_compact
from app.db.base import utcnow
from app.models.metrics import DailyMetric

log = get_logger(__name__)

RANGE_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}

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


def accumulate(
    db: Session, *, tenant_id: str, values: dict[str, int | float], day: date | None = None
) -> None:
    """Add counters to one day's row, creating it if needed.

    Uses an upsert with an ``ON CONFLICT`` increment, so several agents running
    concurrently cannot lose each other's counts to a read-modify-write race.
    """
    target_day = day or utcnow().date()
    clean = {k: v for k, v in values.items() if k in _COUNTERS and v}
    if not clean:
        return

    unknown = set(values) - _COUNTERS
    if unknown:
        log.warning("Ignoring unknown metric counters: %s", ", ".join(sorted(unknown)))

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
    organic_sessions: int = 0
    aeo_citations: int = 0
    backlinks_won: int = 0
    blended_cac: float = 0.0
    ad_spend: float = 0.0
    fraud_blocked: int = 0
    referral_spam_blocked: int = 0
    pages_optimised: int = 0
    conversions: int = 0
    spend_saved: float = 0.0
    # Six evenly spaced buckets for the organic-session sparkline.
    trend: list[int] = field(default_factory=list)

    def as_display(self) -> dict[str, str | int | float | list[int]]:
        return {
            "range": self.range_key,
            "organic_sessions": compact(self.organic_sessions),
            "organic_sessions_raw": self.organic_sessions,
            "aeo_citations": self.aeo_citations,
            "backlinks_won": self.backlinks_won,
            "blended_cac": self.blended_cac,
            "ad_spend": money_compact(self.ad_spend),
            "ad_spend_raw": round(self.ad_spend, 2),
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


def rows_for_range(db: Session, *, tenant_id: str, range_key: str) -> list[DailyMetric]:
    days = RANGE_DAYS.get(range_key, 30)
    start = utcnow().date() - timedelta(days=days - 1)
    return list(
        db.execute(
            select(DailyMetric)
            .where(DailyMetric.tenant_id == tenant_id, DailyMetric.day >= start)
            .order_by(DailyMetric.day)
        ).scalars()
    )


def snapshot(db: Session, *, tenant_id: str, range_key: str = "30d") -> KpiSnapshot:
    rows = rows_for_range(db, tenant_id=tenant_id, range_key=range_key)
    snap = KpiSnapshot(range_key=range_key if range_key in RANGE_DAYS else "30d")

    for row in rows:
        snap.organic_sessions += row.organic_sessions
        snap.aeo_citations += row.aeo_citations
        snap.backlinks_won += row.backlinks_won
        snap.ad_spend += row.ad_spend
        snap.fraud_blocked += row.fraud_blocked
        snap.referral_spam_blocked += row.referral_spam_blocked
        snap.pages_optimised += row.pages_optimised
        snap.conversions += row.conversions
        snap.spend_saved += row.spend_saved

    snap.blended_cac = (
        round(snap.ad_spend / snap.conversions, 2) if snap.conversions else 0.0
    )
    snap.trend = _bucket_trend([row.organic_sessions for row in rows], buckets=6)
    return snap


def _bucket_trend(series: list[int], *, buckets: int = 6) -> list[int]:
    """Average a daily series into a fixed number of buckets.

    A fixed count keeps the sparkline's shape comparable across ranges — 7d
    and 90d draw the same number of points.
    """
    if not series:
        return [0] * buckets
    if len(series) <= buckets:
        return series + [series[-1]] * (buckets - len(series))

    size = len(series) / buckets
    out: list[int] = []
    for i in range(buckets):
        chunk = series[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)]
        out.append(round(sum(chunk) / len(chunk)) if chunk else 0)
    return out


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
