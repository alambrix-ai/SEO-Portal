"""Fake connectors, registered only for tests.

The real connectors call real vendor APIs and fail honestly without
credentials — which is correct for a production platform, and useless for
testing the agent loop. So the test suite registers these doubles instead.

They implement the same capability interfaces the agents program against, so
an agent under test exercises exactly the code path it would in production;
only the far side of the boundary is fake.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import (
    AdsConnector,
    AnalyticsConnector,
    CmsConnector,
    CrmConnector,
    NotificationConnector,
    ChannelPerformance,
    RemotePage,
    SessionMetrics,
    TrafficSample,
)
from app.db.base import utcnow
from tests.support import fake_data

# Slugs the fakes register under. Tests connect these instead of a real
# vendor, and provisioning picks them up like any other integration.
FAKE_CMS = "fake_cms"
FAKE_ADS = "fake_ads"
FAKE_ANALYTICS = "fake_analytics"
FAKE_CRM = "fake_crm"
FAKE_NOTIFIER = "fake_notifier"

ALL_FAKE_SLUGS = (FAKE_CMS, FAKE_ADS, FAKE_ANALYTICS, FAKE_CRM, FAKE_NOTIFIER)


class FakeCmsConnector(CmsConnector):
    """A CMS whose pages come from the deterministic generator."""

    spec = ConnectorSpec(
        slug=FAKE_CMS,
        name="Fake CMS",
        category="CMS",
        description="Test double for a content system.",
        fields=(
            text("siteUrl", "Site URL", "https://example.test"),
            # Present so the tests that check credential encryption have a
            # secret to check, on a connector whose health probe succeeds.
            # Credentials are verified at submit now, so those tests cannot
            # use a real vendor pointed at a hostname that does not resolve.
            secret("apiKey", "API key", "", required=False),
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.INJECT_SCHEMA,
            }
        ),
    )

    #: Every write the agents performed, so a test can assert on them.
    writes: list[tuple[str, str]] = []
    schema_injections: list[tuple[str, dict]] = []

    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        return fake_data.pages(org_id=self.org_id, limit=limit, path_prefix=path_prefix)

    def read_page(self, remote_id: str) -> RemotePage:
        return fake_data.page(org_id=self.org_id, remote_id=remote_id)

    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        type(self).writes.append((remote_id, body))
        return True

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        type(self).schema_injections.append((remote_id, json_ld))
        return True

    def check_health(self) -> HealthReport:
        return HealthReport(ok=True, detail="Fake CMS", checked_at=utcnow())


class FakeAdsConnector(AdsConnector):
    """An ad platform with a spread of CACs for the budget engine to optimise."""

    channel = "google"

    spec = ConnectorSpec(
        slug=FAKE_ADS,
        name="Fake Ads",
        category="Ad Platforms",
        description="Test double for an ad platform.",
        fields=(text("accountId", "Account ID", "123"),),
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.UPLOAD_CREATIVE,
                Capability.PUSH_AUDIENCE,
                Capability.READ_TRAFFIC_QUALITY,
                Capability.BLOCK_PLACEMENT,
            }
        ),
    )

    budgets: list[tuple[str, int]] = []
    creatives: list[dict] = []
    audiences: list[str] = []
    blocked: list[str] = []

    def read_performance(self, *, days: int = 7) -> ChannelPerformance:
        return fake_data.performance(org_id=self.org_id, channel=self.channel, days=days)

    def set_budget_share(self, *, percent: int, daily_budget: float | None = None) -> bool:
        type(self).budgets.append((self.channel, percent))
        return True

    def upload_creative(
        self,
        *,
        headline: str,
        body_copy: str,
        call_to_action: str,
        dimensions: str,
        audience_segment: str = "",
    ) -> str:
        type(self).creatives.append(
            {"headline": headline, "dimensions": dimensions, "segment": audience_segment}
        )
        return f"fake-creative-{len(type(self).creatives)}"

    def push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        type(self).audiences.append(label)
        return f"fake-audience-{len(type(self).audiences)}"

    def sample_traffic(self, *, limit: int = 50) -> list[TrafficSample]:
        return fake_data.traffic(org_id=self.org_id, channel=self.channel, limit=limit)

    def block_placement(self, *, source: str, reason: str) -> bool:
        type(self).blocked.append(source)
        return True

    def check_health(self) -> HealthReport:
        return HealthReport(ok=True, detail="Fake Ads", checked_at=utcnow())


class FakeAnalyticsConnector(AnalyticsConnector):
    """Sessions, referrers, search queries and inbound links."""

    spec = ConnectorSpec(
        slug=FAKE_ANALYTICS,
        name="Fake Analytics",
        category="Analytics & Search",
        description="Test double for an analytics and search property.",
        fields=(text("propertyId", "Property ID", "properties/1"),),
        capabilities=frozenset(
            {
                Capability.READ_SESSIONS,
                Capability.READ_REFERRERS,
                Capability.READ_SEARCH_PERFORMANCE,
                Capability.READ_BACKLINKS,
                Capability.READ_CONVERSIONS,
            }
        ),
    )

    excluded: list[str] = []

    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        return fake_data.sessions(org_id=self.org_id, days=days)

    def read_referrers(self, *, days: int = 1, limit: int = 100) -> list[TrafficSample]:
        return fake_data.referrers(org_id=self.org_id, limit=limit)

    def read_search_performance(self, *, days: int = 28) -> dict:
        return fake_data.search_performance(org_id=self.org_id, days=days)

    def read_backlinks(self, *, limit: int = 100) -> list[dict]:
        return fake_data.backlinks(org_id=self.org_id, limit=limit)

    def read_conversions(self, *, days: int = 30) -> int:
        return fake_data.conversions(org_id=self.org_id, days=days)

    def exclude_referrer(self, domain: str) -> bool:
        type(self).excluded.append(domain)
        return True

    def check_health(self) -> HealthReport:
        return HealthReport(ok=True, detail="Fake Analytics", checked_at=utcnow())


class FakeCrmConnector(CrmConnector):
    """Aggregated first-party signals for the audience modeller."""

    spec = ConnectorSpec(
        slug=FAKE_CRM,
        name="Fake CRM",
        category="CRM",
        description="Test double for a CRM.",
        fields=(secret("apiKey", "API key", "••••"),),
        capabilities=frozenset(
            {Capability.READ_CONVERSIONS, Capability.READ_FIRST_PARTY_SIGNALS}
        ),
    )

    def read_conversions(self, *, days: int = 30) -> int:
        return fake_data.conversions(org_id=self.org_id, days=days)

    def read_first_party_signals(self, *, limit: int = 500) -> list[dict]:
        return fake_data.first_party_signals(org_id=self.org_id, limit=limit)

    def check_health(self) -> HealthReport:
        return HealthReport(ok=True, detail="Fake CRM", checked_at=utcnow())


class FakeNotifier(NotificationConnector):
    """Captures every notification so a test can assert what was sent."""

    spec = ConnectorSpec(
        slug=FAKE_NOTIFIER,
        name="Fake Notifier",
        category="Collaboration",
        description="Test double for a notification channel.",
        fields=(text("channel", "Channel", "#test"),),
        capabilities=frozenset({Capability.SEND_NOTIFICATION, Capability.SEND_EMAIL}),
    )

    sent: list[tuple[str, str, str]] = []

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        type(self).sent.append((channel, subject, message))
        return True

    def check_health(self) -> HealthReport:
        return HealthReport(ok=True, detail="Fake Notifier", checked_at=utcnow())


FAKE_CLASSES = (
    FakeCmsConnector,
    FakeAdsConnector,
    FakeAnalyticsConnector,
    FakeCrmConnector,
    FakeNotifier,
)


def install() -> None:
    """Register the fakes so provisioning and agents can use them."""
    from app.connectors.base import registry

    for klass in FAKE_CLASSES:
        registry.register(klass)


def reset_recordings() -> None:
    """Clear what the fakes captured, between tests."""
    FakeCmsConnector.writes = []
    FakeCmsConnector.schema_injections = []
    FakeAdsConnector.budgets = []
    FakeAdsConnector.creatives = []
    FakeAdsConnector.audiences = []
    FakeAdsConnector.blocked = []
    FakeAnalyticsConnector.excluded = []
    FakeNotifier.sent = []
