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
from typing import Any

from app.connectors.base.connector import BaseConnector


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


@dataclass(slots=True)
class SessionMetrics:
    day: date
    sessions: int = 0
    conversions: int = 0
    revenue: float = 0.0


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
    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        """Publish new copy. Returns True when the CMS accepted it."""

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
    ) -> str:
        """Return the platform's id for the created creative."""
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

    def read_backlinks(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return []

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
