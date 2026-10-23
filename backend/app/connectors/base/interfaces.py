"""Capability interfaces agents program against.

An agent asks for *a CMS* or *an ad platform*, never for WordPress or Google
Ads by name. These mixins define those shapes, and the payload dataclasses are
the common vocabulary each vendor's connector normalises into — that
normalisation is the connector's whole job.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable

from app.connectors.base.connector import BaseConnector
from app.core.exceptions import ConnectorConfigError


# ── Payloads ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class RemotePage:
    """A content node as the CMS describes it."""

    remote_id: str
    url: str
    title: str
    body: str = ""
    word_count: int = 0
    schema_types: list[str] = field(default_factory=list)
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChannelPerformance:
    """One channel's numbers for a period, normalised across platforms."""

    channel: str
    spend: float = 0.0
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    # Cost per acquisition, derived where the platform does not report it.
    cac: float = 0.0

    @property
    def derived_cac(self) -> float:
        if self.cac:
            return self.cac
        return round(self.spend / self.conversions, 2) if self.conversions else 0.0


@dataclass(slots=True)
class TrafficSample:
    """One traffic record used for fraud and referral-spam assessment."""

    source: str
    referrer: str = ""
    sessions: int = 1
    bounce_rate: float = 0.0
    avg_session_seconds: float = 0.0
    channel: str = ""
    signals: dict[str, Any] = field(default_factory=dict)
    #: False when the source reports no bounce / time-on-site figures, so
    #: detectors must not read the zero defaults as "bounced instantly".
    engagement_known: bool = True


@dataclass(slots=True)
class SessionMetrics:
    day: date
    sessions: int = 0
    conversions: int = 0
    revenue: float = 0.0


@dataclass(slots=True)
class SearchDay:
    """One day of a site's search figures (Search Console, ``date`` dimension).

    ``position`` is the average position that day, or None when the source
    reported none (no impressions). Days with no row are simply absent.
    """

    day: date
    clicks: int = 0
    impressions: int = 0
    position: float | None = None


@dataclass(slots=True)
class CitationHit:
    """An answer engine citing one of the organisation's pages."""

    engine: str
    query: str
    cited_url: str
    position: int = 0


# ── Interfaces ─────────────────────────────────────────────────────────────
class CmsConnector(BaseConnector):
    """A content system the platform can crawl and write back to."""

    @abstractmethod
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        ...

    @abstractmethod
    def read_page(self, remote_id: str) -> RemotePage:
        ...

    @abstractmethod
    def write_page(
        self,
        remote_id: str,
        *,
        body: str,
        title: str | None = None,
        meta_description: str | None = None,
    ) -> bool:
        """Publish new copy. Returns True when the CMS accepted it.

        ``meta_description`` is optional: connectors that expose an excerpt /
        SEO description field should persist it; others may ignore it when the
        body already carries a meta tag or frontmatter key.
        """

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        """Attach or patch a JSON-LD graph. Not every CMS supports this."""
        raise NotImplementedError(f"{self.name} cannot inject structured data")

    def register_webhook(self, *, callback_url: str, secret: str) -> bool:
        """Subscribe to content changes so the sync stays live."""
        return False


@dataclass(slots=True)
class PageVitals:
    """Core Web Vitals for one URL, as real devices recorded them.

    Every field is optional and ``None`` means *unmeasured*, not good. Field
    data only exists for URLs with enough traffic to form a sample, so a page
    with no data is common and must never be reported as passing.
    """

    url: str
    lcp_ms: int | None = None
    cls: float | None = None
    inp_ms: int | None = None
    #: True when the source could only offer whole-site figures. Those are
    #: real, but they are not this page's, so the audit does not attribute
    #: them to it.
    origin_level_only: bool = False


class PageSpeedConnector(BaseConnector):
    """A source of Core Web Vitals field data."""

    @abstractmethod
    def read_vitals(self, url: str) -> PageVitals:
        """Field metrics for one URL. Unmeasured fields come back as None."""


class AdsConnector(BaseConnector):
    """An ad platform or exchange the platform can read and steer."""

    #: Which internal channel key this connector spends through.
    channel: str = ""

    @abstractmethod
    def read_performance(self, *, days: int = 7) -> ChannelPerformance:
        ...

    @abstractmethod
    def set_budget_share(self, *, percent: int, daily_budget: float | None = None) -> bool:
        ...

    def upload_creative(
        self,
        *,
        headline: str,
        body_copy: str,
        call_to_action: str,
        dimensions: str,
        audience_segment: str = "",
        final_url: str = "",
        extra_headlines: tuple[str, ...] = (),
        extra_descriptions: tuple[str, ...] = (),
    ) -> str:
        """Return the platform's id for the created creative.

        ``final_url`` is the page the ad sends people to — the product page the
        copy was written from. ``extra_headlines`` / ``extra_descriptions`` are
        sibling variants for platforms whose ad format combines several (a
        Google responsive search ad needs at least three headlines and two
        descriptions).
        """
        raise NotImplementedError(f"{self.name} cannot accept uploaded creatives")

    def push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        """Activate a first-party segment. Returns the platform's audience id."""
        raise NotImplementedError(f"{self.name} cannot accept custom audiences")

    def sample_traffic(self, *, limit: int = 50) -> list[TrafficSample]:
        """Recent traffic for fraud assessment."""
        return []

    def block_placement(self, *, source: str, reason: str) -> bool:
        """Exclude a placement or source from future bidding."""
        raise NotImplementedError(f"{self.name} cannot block placements")


class AnalyticsConnector(BaseConnector):
    """A measurement platform: sessions, referrers, search performance."""

    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        return []

    def read_referrers(self, *, days: int = 1, limit: int = 100) -> list[TrafficSample]:
        return []

    def read_search_performance(self, *, days: int = 28) -> dict[str, Any]:
        return {}

    def read_backlinks(self, *, domain: str, limit: int = 50) -> list[dict[str, Any]]:
        """The sites linking to ``domain`` — any domain, not only the customer's.

        One row per linking domain, each a dict with at least ``domain`` (the
        linking site, lower-case, no ``www.``) and ``authority`` (0–100; 0
        means the source did not report one). Optional keys: ``anchor``,
        ``url`` (the linking page), ``url_to``, ``dofollow``, ``first_seen``.

        Taking the target domain is the point: the competitor monitor asks
        who links to *each competitor*, and a source that can only describe
        the customer's own site cannot answer that. Only connectors that
        declare ``Capability.READ_BACKLINKS`` implement it; the default
        raises rather than returning an empty list that reads as "no links".
        """
        raise NotImplementedError(f"{self.name} cannot read backlinks")

    def exclude_referrer(self, domain: str) -> bool:
        """Add a spam referrer to the platform's exclusion list."""
        return False


class CrmConnector(BaseConnector):
    """A CRM supplying conversions and first-party interaction signals."""

    def read_conversions(self, *, days: int = 30) -> int:
        return 0

    def read_first_party_signals(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Interaction markers, already aggregated — never raw identifiers."""
        return []


class AeoMonitorConnector(BaseConnector):
    """An answer engine whose citations of the customer can be checked."""

    #: Engine key stored on generated Q&A pairs.
    engine: str = ""

    @abstractmethod
    def check_citations(self, *, queries: list[str], domain: str) -> list[CitationHit]:
        ...


class NotificationConnector(BaseConnector):
    """Somewhere to tell a human what an agent just did."""

    @abstractmethod
    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        ...


# ── Social posts: performance read-back ────────────────────────────────────
#: One post's figures: every key in ``app.models.social.POST_METRIC_FIELDS``
#: (impressions, reach, engagements, clicks, likes, comments, shares,
#: saves), each an int, or None when the platform does not report it. A
#: connector may add extra keys of its own (Reddit: ``score``,
#: ``upvote_ratio``); collectors store only the known ones.
PostMetrics = dict[str, int | float | None]


@runtime_checkable
class PostMetricsReader(Protocol):
    """A social connector that can read back how its published posts are doing.

    ``read_post_metrics(remote_ids)`` takes the platform ids ``publish_post``
    returned (``MarketingAsset.remote_id``) and answers
    ``{remote_id: PostMetrics}`` for the posts it could read. A post the
    platform no longer has, or could not report on, is left out rather than
    filled with zeros. Every call is bounded (ids per run, timeouts, the
    connector's retries); each connector caps how many ids it reads.

    A figure the platform does not give is ``None``, never 0: Instagram no
    longer reports impressions and Reddit has no reach, and 0 would tell a
    marketer nobody saw the post.

    Raises :class:`InsightsAccessMissing` when the connection was made
    without the permission to read insights (the person signs in again), and
    the usual :class:`~app.core.exceptions.ConnectorError` for anything else.
    """

    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, PostMetrics]:
        ...


class InsightsAccessMissing(ConnectorConfigError):
    """The connection may post but not read insights: sign in again to allow it."""

    code = "insights_access_missing"

    def __init__(self, platform: str) -> None:
        super().__init__(f"Reconnect {platform} to allow insights.")
        self.platform = platform


def empty_post_metrics() -> PostMetrics:
    """Every known figure, unknown (None) until the platform reports it."""
    from app.models.social import POST_METRIC_FIELDS

    return {key: None for key in POST_METRIC_FIELDS}


def count_or_none(value: object) -> int | None:
    """A platform figure as a non-negative int, or None when it is not one.

    Booleans, negatives, floats with a fraction, strings that are not plain
    digits and anything else unexpected are None (unknown), never 0.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 18:
        return int(value.strip())
    return None


def sum_or_none(*values: int | None) -> int | None:
    """The total of the known values; None when none is known."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


@runtime_checkable
class SearchDailyReader(Protocol):
    """A search source that reports clicks, impressions and position per day.

    ``read_search_daily(days)`` returns one :class:`SearchDay` per day the
    source has figures for, oldest first, covering the last ``days`` days
    the source has finished (Search Console lags about three days).
    """

    def read_search_daily(self, days: int = 28) -> list[SearchDay]:
        ...
