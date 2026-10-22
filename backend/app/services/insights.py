"""The Insights overview: one date range, compared with the period before it.

Sections and where their figures come from:

* **seo**      Website visits and conversions from the analytics source
               (``metric_points`` "sessions"/"conversions", Google Analytics or
               Adobe Analytics); Search Console / Bing clicks, impressions and
               average position (``metric_points``, never counted as
               sessions); pages optimised
               (``daily_metrics``); technical issues fixed (``seo_issues``);
               backlinks won (``backlink_targets`` that reached "Won").
* **ads**      spend, clicks, impressions and conversions per ad platform
               (``metric_points``), with blended cost per conversion.
* **social**   impressions, reach, engagement and clicks per platform and per
               post, for posts published in the period (``social_post_metrics``),
               plus the daily totals ``metric_points`` keeps for the trend.
* **content**  items made, by kind, and Creative Studio jobs finished.
* **agents**   approvals waiting now, agent runs and the actions they took.
* **llm**      tokens and estimated cost (:mod:`app.services.llm_usage`).

Rules every section follows:

* **Never invent a number.** A figure nothing measured is ``None`` with a
  note ("Not tracked yet", "Not reported by Instagram"); 0 means measured.
* **Say what is missing.** Each section has ``available`` and a plain
  ``connect_hint``.
* **Only what the role may see.** A section whose module the caller cannot
  view is left out. A Client Viewer (Reports view only) sees the results
  sections read-only: seo (with backlinks won), ads, social and content.
  They never see agents/approvals activity, and LLM cost needs the agents or
  admin module and is never shown to a Client Viewer.
* **Bounded and tenant-filtered.** The range is capped at
  ``insights_max_range_days``. Every query filters on the caller's tenant
  explicitly (RLS is the second line), aggregates in SQL over the
  ``(tenant_id, …)`` indexes, and the number of queries is fixed — it never
  grows with the number of rows.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.core.rbac import Module, Role
from app.models.agent import AgentRun, RunStatus
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.content import AssetKind, MarketingAsset
from app.models.creative import CreativeJob, CreativeJobStatus
from app.models.metrics import DailyMetric
from app.models.offpage import BacklinkStatus, BacklinkTarget
from app.models.seo import SeoIssue, SeoPage
from app.models.social import MetricPoint, SocialPostMetric
from app.schemas.insights import (
    AdsChannelOut,
    AdsSectionOut,
    BreakdownItemOut,
    ContentSectionOut,
    InsightsOverviewOut,
    JobUsageLineOut,
    LlmSectionOut,
    LlmUsageOut,
    MetricOut,
    PointOut,
    SectionOut,
    SectionsOut,
    SeoSectionOut,
    SeriesOut,
    SocialPlatformOut,
    SocialSectionOut,
    SocialTopPostOut,
    Unit,
    UsageLineOut,
    UsageTotalsOut,
)
from app.services import llm_usage
from app.services.metrics import NOT_WEBSITE_VISITS
from app.services.social_metrics import SOCIAL_SOURCE

#: Default range when none is given: the last 30 days, today included.
DEFAULT_DAYS = 30
#: Nothing before this is a real marketing date; also keeps the previous
#: period's arithmetic far from ``date.min``.
EARLIEST_DATE = date(2000, 1, 1)
#: Posts listed under "Top posts".
TOP_POSTS = 10
#: Longest post link passed on (the column holds 1000).
_URL_MAX = 1000

NOT_TRACKED = "Not tracked yet"
NO_POSTS = "No posts with figures in this period"
NOT_REPORTED = "Not reported"
COST_NOTE = "Costs are estimates from the prices set for each AI model."

HINT_SEARCH = "Connect Google Search Console to see search clicks."
HINT_ANALYTICS = "Connect Google Analytics or Adobe Analytics to see website visits."
HINT_ADS = "Connect an ad account, such as Google Ads or Meta Ads, to see spend, clicks and conversions."
HINT_SOCIAL = "Connect a social account and publish a post to see reach and engagement."
HINT_CONTENT = "Nothing has been made yet. Ask a content agent or use Creative Studio to create something."
HINT_AGENTS = "Start an agent to see its work here."
HINT_LLM = "AI usage appears here once an agent or Creative Studio has run."
HINT_QUIET = "Nothing was recorded in this period. Try a longer date range."

#: Analytics figures kept in ``metric_points`` (scheduler rollup, contract 4.1).
_ANALYTICS_METRICS: tuple[str, ...] = ("sessions", "conversions")
#: Sources with READ_SESSIONS whose "sessions" are not website visits:
#: Business Profile reports local impressions there (contract 1.8, H1).
#: Shared with Reports and the daily rollup, so the three agree.
_NOT_WEBSITE_VISITS: frozenset[str] = NOT_WEBSITE_VISITS
#: Search figures kept in ``metric_points``.
_SEARCH_METRICS: tuple[str, ...] = ("clicks", "impressions", "position")
#: Ad figures kept in ``metric_points`` (scheduler._AD_METRICS).
_AD_METRICS: tuple[str, ...] = ("spend", "clicks", "impressions", "conversions")
#: Social daily totals drawn as the trend.
_SOCIAL_SERIES: tuple[str, ...] = ("reach", "engagements")
#: Per-post counts shown in Insights: column name → response name.
_SOCIAL_FIELDS: dict[str, str] = {
    "impressions": "impressions",
    "reach": "reach",
    "engagements": "engagement",
    "clicks": "clicks",
}

_LABELS: dict[str, str] = {
    "sessions": "Website visits",
    "clicks": "Clicks",
    "impressions": "Impressions",
    "position": "Average position",
    "spend": "Spend",
    "conversions": "Conversions",
    "cac": "Cost per conversion",
    "reach": "Reach",
    "engagements": "Engagement",
    "engagement": "Engagement",
    "pages_optimised": "Pages optimised",
    "issues_fixed": "Technical issues fixed",
    "backlinks_won": "Backlinks won",
    "created": "Items created",
    "creatives": "Creatives produced",
    "pending_approvals": "Waiting for approval",
    "runs": "Agent runs",
    "succeeded": "Successful runs",
    "failed": "Failed runs",
    "actions": "Actions taken",
    "tokens_in": "Tokens sent",
    "tokens_out": "Tokens received",
    "cost": "Estimated cost",
}
_KIND_LABELS: dict[str, str] = {
    AssetKind.ARTICLE: "Articles",
    AssetKind.SEARCH_CAMPAIGN: "Search campaigns",
    AssetKind.SOCIAL_POST: "Social posts",
    AssetKind.EMAIL: "Emails",
    AssetKind.CREATIVE: "Creatives",
}


class Viewer(Protocol):
    """What the overview needs from ``app.api.deps.CurrentUser``."""

    @property
    def tenant_id(self) -> str: ...

    @property
    def role(self) -> Role: ...

    def can_view(self, module: Module | str) -> bool: ...


# ── Periods ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Window:
    """Inclusive UTC dates ``start``..``end``."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def since(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=UTC)

    @property
    def until(self) -> datetime:
        """Exclusive end: midnight after ``end``."""
        return datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=UTC)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(self.days)]


def default_range(today: date) -> tuple[date, date]:
    return today - timedelta(days=DEFAULT_DAYS - 1), today


def windows(start: date, end: date) -> tuple[Window, Window]:
    """The chosen period and the period of equal length just before it."""
    if start < EARLIEST_DATE:
        raise InvalidInputError("Choose a start date in 2000 or later.")
    if end < start:
        raise InvalidInputError("The end date must be on or after the start date.")
    current = Window(start, end)
    if current.days > settings.insights_max_range_days:
        raise InvalidInputError(
            f"Choose a range of {settings.insights_max_range_days} days or fewer."
        )
    previous_end = start - timedelta(days=1)
    previous = Window(previous_end - timedelta(days=current.days - 1), previous_end)
    return current, previous


def change_pct(current: float | None, previous: float | None) -> float | None:
    """Percentage change, or ``None`` when it would be meaningless."""
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _metric(
    key: str,
    current: float | None,
    previous: float | None,
    *,
    unit: Unit = "count",
    better: str = "up",
    note: str = "",
    is_estimate: bool = False,
) -> MetricOut:
    return MetricOut(
        key=key,
        label=_LABELS.get(key, key),
        value=_round(current),
        previous=_round(previous),
        change_pct=change_pct(current, previous),
        unit=unit,
        better="down" if better == "down" else "up",
        is_estimate=is_estimate,
        note=note,
    )


def _series(
    key: str, window: Window, values: dict[date, float], *, unit: Unit = "count",
    label: str | None = None,
) -> SeriesOut:
    """One point per calendar day; a day with no figure is ``None``."""
    return SeriesOut(
        key=key,
        label=label or _LABELS.get(key, key),
        unit=unit,
        points=[PointOut(date=day, value=_round(values.get(day))) for day in window.dates()],
    )


def _availability(current_has: bool, previous_has: bool, hint: str) -> tuple[bool, str]:
    """``available`` and the hint: what to connect, or that the period was quiet."""
    if current_has:
        return True, ""
    return False, HINT_QUIET if previous_has else hint


# ── Sources ────────────────────────────────────────────────────────────────
def _slugs(capability_name: str) -> list[str]:
    from app.connectors.base.connector import Capability
    from app.connectors.base.registry import slugs_with_capability

    return slugs_with_capability(Capability[capability_name])


def _source_label(slug: str) -> str:
    from app.connectors.base.registry import get_spec

    spec = get_spec(slug)
    return spec.name if spec else slug.replace("_", " ").title()


def _network_label(network: str) -> str:
    from app.agents.social_media_manager.platforms import rules_for

    rules = rules_for(network)
    return rules.label if rules else network.replace("_", " ").title()


def safe_post_url(raw: object) -> str | None:
    """The platform's link to a post, only if it is a plain https URL."""
    if not isinstance(raw, str) or not raw or len(raw) > _URL_MAX:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return None
    return raw


# ── Loaders (one query each, tenant-filtered, aggregated in SQL) ───────────
@dataclass(frozen=True, slots=True)
class PointRow:
    source: str
    metric: str
    day: date
    value: float


def load_points(
    db: Session, *, tenant_id: str, since: date, until: date, sources: Sequence[str],
    metrics: Sequence[str],
) -> list[PointRow]:
    """Daily ``metric_points`` totals per source and metric, dimensions summed."""
    if not sources or not metrics:
        return []
    rows = db.execute(
        select(
            MetricPoint.source,
            MetricPoint.metric,
            MetricPoint.day,
            func.sum(MetricPoint.value),
        )
        .where(
            MetricPoint.tenant_id == tenant_id,
            MetricPoint.metric.in_(list(metrics)),
            MetricPoint.day >= since,
            MetricPoint.day <= until,
            MetricPoint.source.in_(list(sources)),
        )
        .group_by(MetricPoint.source, MetricPoint.metric, MetricPoint.day)
    ).all()
    return [PointRow(str(r[0]), str(r[1]), r[2], float(r[3] or 0.0)) for r in rows]


@dataclass(slots=True)
class SocialRow:
    network: str
    current: bool
    posts: int
    #: column → (sum of reported values, number of posts that reported it)
    figures: dict[str, tuple[int, int]] = field(default_factory=dict)


def load_social(db: Session, *, tenant_id: str, current: Window, previous: Window) -> list[SocialRow]:
    """Per network and period: posts published, and each figure's sum and count."""
    is_current = (MarketingAsset.published_at >= current.since).label("is_current")
    columns: list[Any] = [SocialPostMetric.network, is_current, func.count(SocialPostMetric.id)]
    for name in _SOCIAL_FIELDS:
        column = getattr(SocialPostMetric, name)
        columns += [func.sum(column), func.count(column)]
    rows = db.execute(
        select(*columns)
        .join(
            MarketingAsset,
            (MarketingAsset.id == SocialPostMetric.asset_id)
            & (MarketingAsset.tenant_id == tenant_id),
        )
        .where(
            SocialPostMetric.tenant_id == tenant_id,
            MarketingAsset.published_at >= previous.since,
            MarketingAsset.published_at < current.until,
        )
        .group_by(SocialPostMetric.network, is_current)
    ).all()
    out: list[SocialRow] = []
    for row in rows:
        social = SocialRow(network=str(row[0]), current=bool(row[1]), posts=int(row[2] or 0))
        for index, name in enumerate(_SOCIAL_FIELDS):
            total, reported = row[3 + index * 2], row[4 + index * 2]
            social.figures[name] = (int(total or 0), int(reported or 0))
        out.append(social)
    return out


def load_top_posts(db: Session, *, tenant_id: str, window: Window) -> list[Any]:
    """The period's best posts by engagement, then reach. No post text."""
    return list(
        db.execute(
            select(
                MarketingAsset.id,
                SocialPostMetric.network,
                MarketingAsset.title,
                MarketingAsset.remote_url,
                MarketingAsset.published_at,
                SocialPostMetric.impressions,
                SocialPostMetric.engagements,
            )
            .join(
                MarketingAsset,
                (MarketingAsset.id == SocialPostMetric.asset_id)
                & (MarketingAsset.tenant_id == tenant_id),
            )
            .where(
                SocialPostMetric.tenant_id == tenant_id,
                MarketingAsset.published_at >= window.since,
                MarketingAsset.published_at < window.until,
            )
            .order_by(
                SocialPostMetric.engagements.desc().nulls_last(),
                SocialPostMetric.reach.desc().nulls_last(),
                MarketingAsset.id,
            )
            .limit(TOP_POSTS)
        ).all()
    )


@dataclass(slots=True)
class SeoCounts:
    #: ``*_tracked`` False = nothing in the workspace measures it.
    pages_tracked: bool = False
    pages_current: int = 0
    pages_previous: int = 0
    issues_tracked: bool = False
    issues_current: int = 0
    issues_previous: int = 0
    backlinks_tracked: bool = False
    backlinks_current: int = 0
    backlinks_previous: int = 0


def _between(column: Any, window: Window) -> Any:  # noqa: ANN401 - SQL expression
    return (column >= window.since) & (column < window.until)


def load_seo_counts(
    db: Session, *, tenant_id: str, current: Window, previous: Window, backlinks: bool
) -> SeoCounts:
    counts = SeoCounts()
    pages = db.execute(
        select(func.count(SeoPage.id)).where(SeoPage.tenant_id == tenant_id)
    ).all()
    counts.pages_tracked = bool(pages and pages[0][0])
    optimised = db.execute(
        select(
            func.coalesce(
                func.sum(DailyMetric.pages_optimised).filter(DailyMetric.day >= current.start), 0
            ),
            func.coalesce(
                func.sum(DailyMetric.pages_optimised).filter(DailyMetric.day < current.start), 0
            ),
        ).where(
            DailyMetric.tenant_id == tenant_id,
            DailyMetric.day >= previous.start,
            DailyMetric.day <= current.end,
        )
    ).all()
    if optimised:
        counts.pages_current, counts.pages_previous = int(optimised[0][0]), int(optimised[0][1])

    fixed = SeoIssue.status == "fixed"
    issues = db.execute(
        select(
            func.count(SeoIssue.id),
            func.count(SeoIssue.id).filter(fixed, _between(SeoIssue.resolved_at, current)),
            func.count(SeoIssue.id).filter(fixed, _between(SeoIssue.resolved_at, previous)),
        ).where(SeoIssue.tenant_id == tenant_id)
    ).all()
    if issues:
        counts.issues_tracked = bool(issues[0][0])
        counts.issues_current, counts.issues_previous = int(issues[0][1]), int(issues[0][2])

    if backlinks:
        won = BacklinkTarget.status == BacklinkStatus.WON.value
        links = db.execute(
            select(
                func.count(BacklinkTarget.id),
                func.count(BacklinkTarget.id).filter(won, _between(BacklinkTarget.won_at, current)),
                func.count(BacklinkTarget.id).filter(won, _between(BacklinkTarget.won_at, previous)),
            ).where(BacklinkTarget.tenant_id == tenant_id)
        ).all()
        if links:
            counts.backlinks_tracked = bool(links[0][0])
            counts.backlinks_current, counts.backlinks_previous = int(links[0][1]), int(links[0][2])
    return counts


@dataclass(slots=True)
class ContentCounts:
    #: kind → (created in the period, created in the previous period)
    by_kind: dict[str, tuple[int, int]] = field(default_factory=dict)
    creatives_current: int = 0
    creatives_previous: int = 0


def load_content(db: Session, *, tenant_id: str, current: Window, previous: Window) -> ContentCounts:
    counts = ContentCounts()
    in_current = MarketingAsset.created_at >= current.since
    rows = db.execute(
        select(
            MarketingAsset.kind,
            func.count(MarketingAsset.id).filter(in_current),
            func.count(MarketingAsset.id).filter(~in_current),
        )
        .where(
            MarketingAsset.tenant_id == tenant_id,
            MarketingAsset.created_at >= previous.since,
            MarketingAsset.created_at < current.until,
        )
        .group_by(MarketingAsset.kind)
    ).all()
    for row in rows:
        counts.by_kind[str(row[0])] = (int(row[1] or 0), int(row[2] or 0))
    job_current = CreativeJob.created_at >= current.since
    jobs = db.execute(
        select(
            func.count(CreativeJob.id).filter(job_current),
            func.count(CreativeJob.id).filter(~job_current),
        ).where(
            CreativeJob.tenant_id == tenant_id,
            CreativeJob.status == CreativeJobStatus.SUCCEEDED,
            CreativeJob.created_at >= previous.since,
            CreativeJob.created_at < current.until,
        )
    ).all()
    if jobs:
        counts.creatives_current, counts.creatives_previous = int(jobs[0][0] or 0), int(jobs[0][1] or 0)
    return counts


@dataclass(slots=True)
class ActivityCounts:
    #: ``None`` when the caller cannot see approvals.
    pending_approvals: int | None = None
    #: status → (runs in the period, runs in the previous period)
    runs: dict[str, tuple[int, int]] = field(default_factory=dict)
    actions_current: int = 0
    actions_previous: int = 0


def load_activity(
    db: Session, *, tenant_id: str, current: Window, previous: Window,
    approvals: bool, agents: bool,
) -> ActivityCounts:
    counts = ActivityCounts()
    if approvals:
        pending = db.execute(
            select(func.count(ApprovalItem.id)).where(
                ApprovalItem.tenant_id == tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
            )
        ).all()
        counts.pending_approvals = int(pending[0][0] or 0) if pending else 0
    if agents:
        in_current = AgentRun.started_at >= current.since
        rows = db.execute(
            select(
                AgentRun.status,
                func.count(AgentRun.id).filter(in_current),
                func.count(AgentRun.id).filter(~in_current),
                func.coalesce(func.sum(AgentRun.actions_taken).filter(in_current), 0),
                func.coalesce(func.sum(AgentRun.actions_taken).filter(~in_current), 0),
            )
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.started_at >= previous.since,
                AgentRun.started_at < current.until,
            )
            .group_by(AgentRun.status)
        ).all()
        for row in rows:
            counts.runs[str(row[0])] = (int(row[1] or 0), int(row[2] or 0))
            counts.actions_current += int(row[3] or 0)
            counts.actions_previous += int(row[4] or 0)
    return counts


# ── Builders (pure) ────────────────────────────────────────────────────────
def _by_period(
    rows: Iterable[PointRow], current: Window
) -> tuple[dict[str, dict[date, float]], dict[str, dict[date, float]]]:
    """metric → day → value, split into the current and the previous period."""
    now: dict[str, dict[date, float]] = defaultdict(dict)
    before: dict[str, dict[date, float]] = defaultdict(dict)
    for row in rows:
        target = now if row.day >= current.start else before
        target[row.metric][row.day] = target[row.metric].get(row.day, 0.0) + row.value
    return now, before


def _total(values: dict[date, float]) -> float | None:
    return sum(values.values()) if values else None


def _weighted_position(rows: Iterable[PointRow]) -> tuple[dict[date, float], float | None]:
    """Average position per day and overall, weighted by impressions.

    Positions from several search sources cannot be added; each source-day
    counts in proportion to its impressions (or equally when a source gave
    none).
    """
    positions: dict[tuple[str, date], float] = {}
    impressions: dict[tuple[str, date], float] = {}
    for row in rows:
        if row.metric == "position":
            positions[(row.source, row.day)] = row.value
        elif row.metric == "impressions":
            impressions[(row.source, row.day)] = row.value
    by_day: dict[date, list[tuple[float, float]]] = defaultdict(list)
    for key, position in positions.items():
        weight = impressions.get(key) or 1.0
        by_day[key[1]].append((position, weight))
    daily = {
        day: sum(p * w for p, w in pairs) / sum(w for _, w in pairs)
        for day, pairs in by_day.items()
    }
    pairs = [pair for day_pairs in by_day.values() for pair in day_pairs]
    overall = sum(p * w for p, w in pairs) / sum(w for _, w in pairs) if pairs else None
    return daily, overall


def _tracked(key: str, tracked: bool, current: int, previous: int) -> MetricOut:
    if not tracked:
        return _metric(key, None, None, note=NOT_TRACKED)
    return _metric(key, current, previous)


def build_seo(
    rows: Sequence[PointRow], counts: SeoCounts, *, current: Window, backlinks: bool,
    analytics: Sequence[PointRow] = (),
) -> SeoSectionOut:
    now_rows = [r for r in rows if r.day >= current.start]
    before_rows = [r for r in rows if r.day < current.start]
    now, before = _by_period(rows, current)
    position_daily, position_now = _weighted_position(now_rows)
    _, position_before = _weighted_position(before_rows)

    search_note = "" if now_rows else NOT_REPORTED
    # Website visits: the analytics source, compared with the period before.
    visits_now, visits_before = _by_period(analytics, current)
    has_visits_now = any(r.day >= current.start for r in analytics)
    _, visits_hint = _availability(
        has_visits_now, any(r.day < current.start for r in analytics), HINT_ANALYTICS
    )
    metrics = [
        _metric(
            "sessions", _total(visits_now["sessions"]), _total(visits_before["sessions"]),
            note=visits_hint,
        ),
        _metric(
            "conversions", _total(visits_now["conversions"]), _total(visits_before["conversions"]),
            note=visits_hint,
        ),
        _metric("clicks", _total(now["clicks"]), _total(before["clicks"]), note=search_note),
        _metric(
            "impressions", _total(now["impressions"]), _total(before["impressions"]),
            note=search_note,
        ),
        _metric(
            "position", position_now, position_before, unit="position", better="down",
            note=search_note,
        ),
        _tracked("pages_optimised", counts.pages_tracked, counts.pages_current, counts.pages_previous),
        _tracked("issues_fixed", counts.issues_tracked, counts.issues_current, counts.issues_previous),
    ]
    if backlinks:
        metrics.append(
            _tracked(
                "backlinks_won", counts.backlinks_tracked,
                counts.backlinks_current, counts.backlinks_previous,
            )
        )
    available, hint = _availability(bool(now_rows), bool(before_rows), HINT_SEARCH)
    # Pages, issues and backlinks count too: a crawled site with no Search
    # Console still has something real to show, but the hint stays.
    available = available or any(m.value is not None for m in metrics)
    return SeoSectionOut(
        available=available,
        connect_hint=hint,
        metrics=metrics,
        series=[
            _series("sessions", current, visits_now["sessions"]),
            _series("clicks", current, now["clicks"]),
            _series("impressions", current, now["impressions"]),
            _series("position", current, position_daily, unit="position"),
        ],
        top_queries=[],
    )


def _cac(spend: float | None, conversions: float | None) -> float | None:
    return spend / conversions if spend is not None and conversions else None


def build_ads(rows: Sequence[PointRow], *, current: Window) -> AdsSectionOut:
    now, before = _by_period(rows, current)
    by_source: dict[str, list[PointRow]] = defaultdict(list)
    for row in rows:
        if row.day >= current.start:
            by_source[row.source].append(row)
    channels = []
    for slug in sorted(by_source):
        source_now, _ = _by_period(by_source[slug], current)
        spend, conversions = _total(source_now["spend"]), _total(source_now["conversions"])
        channels.append(
            AdsChannelOut(
                channel=slug,
                label=_source_label(slug),
                spend=_round(spend),
                clicks=_round(_total(source_now["clicks"])),
                conversions=_round(conversions),
                cac=_round(_cac(spend, conversions)),
                note="" if conversions else "Cost per conversion shows once there are conversions",
            )
        )
    spend_now, spend_before = _total(now["spend"]), _total(before["spend"])
    conv_now, conv_before = _total(now["conversions"]), _total(before["conversions"])
    has_now = any(r.day >= current.start for r in rows)
    has_before = any(r.day < current.start for r in rows)
    available, hint = _availability(has_now, has_before, HINT_ADS)
    note = "" if has_now else NOT_REPORTED
    return AdsSectionOut(
        available=available,
        connect_hint=hint,
        metrics=[
            _metric("spend", spend_now, spend_before, unit="currency", better="down", note=note),
            _metric("clicks", _total(now["clicks"]), _total(before["clicks"]), note=note),
            _metric("impressions", _total(now["impressions"]), _total(before["impressions"]), note=note),
            _metric("conversions", conv_now, conv_before, note=note),
            _metric(
                "cac", _cac(spend_now, conv_now), _cac(spend_before, conv_before),
                unit="currency", better="down",
                note="" if conv_now else "Shows once there are conversions",
            ),
        ],
        series=[
            _series("spend", current, now["spend"], unit="currency"),
            _series("clicks", current, now["clicks"]),
        ],
        channels=channels,
    )


def _social_value(row: SocialRow | None, name: str) -> float | None:
    if row is None:
        return None
    total, reported = row.figures.get(name, (0, 0))
    return float(total) if reported else None


def _sum_reported(rows: Iterable[SocialRow], name: str) -> float | None:
    values = [v for v in (_social_value(row, name) for row in rows) if v is not None]
    return sum(values) if values else None


def build_social(
    social_rows: Sequence[SocialRow],
    top: Sequence[Any],
    series_rows: Sequence[PointRow],
    *,
    current: Window,
) -> SocialSectionOut:
    now = {row.network: row for row in social_rows if row.current}
    before = {row.network: row for row in social_rows if not row.current}
    platforms = []
    for network in sorted(now):
        label = _network_label(network)
        row = now[network]
        values = {_SOCIAL_FIELDS[name]: _social_value(row, name) for name in _SOCIAL_FIELDS}
        missing = [_LABELS[out].lower() for out, value in values.items() if value is None]
        platforms.append(
            SocialPlatformOut(
                platform=network,
                label=label,
                note=f"Not reported by {label}: {', '.join(missing)}" if missing else "",
                **values,
            )
        )
    posts = [
        SocialTopPostOut(
            id=str(row[0]),
            platform=str(row[1]),
            title=str(row[2] or ""),
            url=safe_post_url(row[3]),
            posted_at=row[4],
            impressions=None if row[5] is None else int(row[5]),
            engagement=None if row[6] is None else int(row[6]),
        )
        for row in top
    ]
    daily, _ = _by_period(series_rows, current)
    available, hint = _availability(bool(now), bool(before), HINT_SOCIAL)
    metrics = []
    for name, out_name in _SOCIAL_FIELDS.items():
        value = _sum_reported(now.values(), name)
        if value is not None:
            note = ""
        elif not now:
            note = NO_POSTS
        else:
            note = "Not reported by these platforms"
        metrics.append(_metric(out_name, value, _sum_reported(before.values(), name), note=note))
    return SocialSectionOut(
        available=available,
        connect_hint=hint,
        metrics=metrics,
        series=[
            _series(name, current, daily[name], label=f"{_LABELS[name]} of recent posts, as read each day")
            for name in _SOCIAL_SERIES
        ],
        platforms=platforms,
        top_posts=posts,
    )


def build_content(counts: ContentCounts) -> ContentSectionOut:
    made_now = sum(made for made, _ in counts.by_kind.values())
    made_before = sum(before for _, before in counts.by_kind.values())
    available, hint = _availability(
        bool(made_now or counts.creatives_current),
        bool(made_before or counts.creatives_previous),
        HINT_CONTENT,
    )
    return ContentSectionOut(
        available=available,
        connect_hint=hint,
        metrics=[
            _metric("created", made_now, made_before),
            _metric("creatives", counts.creatives_current, counts.creatives_previous),
        ],
        by_kind=[
            BreakdownItemOut(
                key=kind,
                label=_KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
                value=made,
            )
            for kind, (made, _) in sorted(counts.by_kind.items())
            if made
        ],
    )


def build_agents(counts: ActivityCounts, *, agents: bool) -> SectionOut:
    metrics: list[MetricOut] = []
    if counts.pending_approvals is not None:
        # A live count: there is no "previous period" for what waits now.
        metrics.append(_metric("pending_approvals", counts.pending_approvals, None, better="down"))
    available, hint = False, ""
    if agents:
        runs_now = sum(now for now, _ in counts.runs.values())
        runs_before = sum(before for _, before in counts.runs.values())
        ok = counts.runs.get(RunStatus.SUCCEEDED.value, (0, 0))
        failed = counts.runs.get(RunStatus.FAILED.value, (0, 0))
        metrics += [
            _metric("runs", runs_now, runs_before),
            _metric("succeeded", ok[0], ok[1]),
            _metric("failed", failed[0], failed[1], better="down"),
            _metric("actions", counts.actions_current, counts.actions_previous),
        ]
        available, hint = _availability(bool(runs_now), bool(runs_before), HINT_AGENTS)
    # A pending count is measured even when it is 0.
    available = available or counts.pending_approvals is not None
    return SectionOut(available=available, connect_hint=hint, metrics=metrics)


def build_llm(now: llm_usage.UsageSummary, before: llm_usage.UsageSummary) -> LlmSectionOut:
    totals, prior = now.totals, before.totals
    available, hint = _availability(totals.runs > 0, prior.runs > 0, HINT_LLM)
    return LlmSectionOut(
        available=available,
        connect_hint=hint,
        metrics=[
            _metric(
                "tokens_in", totals.tokens_in, prior.tokens_in, unit="tokens",
                note="" if totals.tokens_in is not None else NOT_REPORTED,
            ),
            _metric(
                "tokens_out", totals.tokens_out, prior.tokens_out, unit="tokens",
                note="" if totals.tokens_out is not None else NOT_REPORTED,
            ),
            # None when no price is set: "No price set", never free.
            _metric(
                "cost", totals.cost, prior.cost, unit="currency", better="down",
                is_estimate=True, note=totals.cost_label,
            ),
        ],
        currency_symbol=now.currency_symbol,
        note=COST_NOTE,
    )


def usage_out(summary: llm_usage.UsageSummary) -> LlmUsageOut:
    """``llm_usage.UsageSummary`` as its response model."""
    lines: list[UsageLineOut] | list[JobUsageLineOut]
    if summary.group is llm_usage.UsageGroup.JOB:
        lines = [JobUsageLineOut.model_validate(line, from_attributes=True) for line in summary.lines]
    else:
        lines = [UsageLineOut.model_validate(line, from_attributes=True) for line in summary.lines]
    return LlmUsageOut(
        group=summary.group.value,
        start=summary.start,
        end=summary.end,
        lines=lines,
        totals=UsageTotalsOut.model_validate(summary.totals, from_attributes=True),
        next_cursor=summary.next_cursor,
        currency_symbol=summary.currency_symbol,
        note=summary.note,
    )


# ── Permissions ────────────────────────────────────────────────────────────
#: The outcome sections a Client Viewer may read without the module itself.
_CLIENT_RESULT_MODULES: frozenset[Module] = frozenset(
    {Module.SEO, Module.OFFPAGE, Module.ADS, Module.CONTENT}
)


def can_see_results(viewer: Viewer, module: Module) -> bool:
    """May the caller see the results section fed by ``module``?

    Anyone who can view the module may. A Client Viewer with Reports view
    also sees the outcome sections (SEO, backlinks, ads, social, content)
    read-only. Agents, approvals and LLM cost are never among them.
    """
    if viewer.can_view(module):
        return True
    return (
        viewer.role == Role.CLIENT
        and module in _CLIENT_RESULT_MODULES
        and viewer.can_view(Module.REPORTS)
    )


def can_see_llm_cost(viewer: Viewer) -> bool:
    """Agents or admin view, and never a Client Viewer."""
    if viewer.role == Role.CLIENT:
        return False
    return viewer.can_view(Module.AGENTS) or viewer.can_view(Module.ADMIN)


# ── Entry points ───────────────────────────────────────────────────────────
def overview(db: Session, viewer: Viewer, *, start: date, end: date) -> InsightsOverviewOut:
    """Every section the caller may see, for ``start``..``end`` and the period before."""
    tenant_id = viewer.tenant_id
    if not tenant_id:
        raise ValueError("tenant_id is required")
    current, previous = windows(start, end)

    see_seo = can_see_results(viewer, Module.SEO)
    see_ads = can_see_results(viewer, Module.ADS)
    see_content = can_see_results(viewer, Module.CONTENT)
    # Activity, not results: never widened for a Client Viewer.
    see_approvals = viewer.can_view(Module.APPROVALS)
    see_agents = viewer.can_view(Module.AGENTS)

    search_slugs = _slugs("READ_SEARCH_PERFORMANCE") if see_seo else []
    analytics_slugs = (
        [s for s in _slugs("READ_SESSIONS") if s not in _NOT_WEBSITE_VISITS] if see_seo else []
    )
    ad_slugs = _slugs("READ_AD_PERFORMANCE") if see_ads else []
    metrics_wanted = sorted(
        set(_SEARCH_METRICS if see_seo else ())
        | set(_ANALYTICS_METRICS if see_seo else ())
        | set(_AD_METRICS if see_ads else ())
        | set(_SOCIAL_SERIES if see_content else ())
    )
    # One query for every daily figure the visible sections draw.
    points = load_points(
        db, tenant_id=tenant_id, since=previous.start, until=current.end,
        sources=[
            *search_slugs, *analytics_slugs, *ad_slugs, *([SOCIAL_SOURCE] if see_content else []),
        ],
        metrics=metrics_wanted,
    )
    search_set, ad_set = set(search_slugs), set(ad_slugs)
    analytics_set = set(analytics_slugs)
    sections = SectionsOut()

    if see_seo:
        see_backlinks = can_see_results(viewer, Module.OFFPAGE)
        sections.seo = build_seo(
            [p for p in points if p.source in search_set and p.metric in _SEARCH_METRICS],
            load_seo_counts(
                db, tenant_id=tenant_id, current=current, previous=previous,
                backlinks=see_backlinks,
            ),
            current=current,
            backlinks=see_backlinks,
            analytics=[
                p for p in points if p.source in analytics_set and p.metric in _ANALYTICS_METRICS
            ],
        )
    if see_ads:
        sections.ads = build_ads(
            [p for p in points if p.source in ad_set and p.metric in _AD_METRICS], current=current
        )
    if see_content:
        sections.social = build_social(
            load_social(db, tenant_id=tenant_id, current=current, previous=previous),
            load_top_posts(db, tenant_id=tenant_id, window=current),
            [p for p in points if p.source == SOCIAL_SOURCE and p.day >= current.start],
            current=current,
        )
        sections.content = build_content(
            load_content(db, tenant_id=tenant_id, current=current, previous=previous)
        )
    if see_approvals or see_agents:
        sections.agents = build_agents(
            load_activity(
                db, tenant_id=tenant_id, current=current, previous=previous,
                approvals=see_approvals, agents=see_agents,
            ),
            agents=see_agents,
        )
    if can_see_llm_cost(viewer):
        group = llm_usage.UsageGroup.PROVIDER
        sections.llm = build_llm(
            llm_usage.summary(db, tenant_id, current.start, current.end, group, limit=1),
            llm_usage.summary(db, tenant_id, previous.start, previous.end, group, limit=1),
        )

    return InsightsOverviewOut(
        from_=current.start,
        to=current.end,
        previous_from=previous.start,
        previous_to=previous.end,
        days=current.days,
        sections=sections,
    )


def usage(
    db: Session, viewer: Viewer, *, start: date, end: date, group: str,
    cursor: str | None, limit: int | None,
) -> LlmUsageOut:
    """One page of LLM usage. The route has already checked :func:`can_see_llm_cost`."""
    current, _ = windows(start, end)
    return usage_out(
        llm_usage.summary(db, viewer.tenant_id, current.start, current.end, group, cursor, limit)
    )
