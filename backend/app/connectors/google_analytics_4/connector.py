"""Google Analytics 4 connector — Data API v1beta plus the Admin API.

This is the platform's main source of truth for organic sessions and referral
traffic, so it feeds both the Reports screen and the referral-spam guard.
"""
from __future__ import annotations

from datetime import date

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import AnalyticsConnector, SessionMetrics, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Lists the properties an account can reach, for the sign-in "choose" step.
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"


def parse_account_summaries(data: dict) -> list[OAuthChoice]:
    """Properties out of an ``accountSummaries.list`` response."""
    out: list[OAuthChoice] = []
    for account in data.get("accountSummaries") or []:
        account_name = str(account.get("displayName") or "")
        for prop in account.get("propertySummaries") or []:
            resource = str(prop.get("property") or "")
            if not resource.startswith("properties/"):
                continue
            number = resource.split("/", 1)[1]
            out.append(
                OAuthChoice(
                    id=resource,
                    label=str(prop.get("displayName") or resource),
                    detail=f"Account: {account_name} · Property ID {number}"
                    if account_name
                    else f"Property ID {number}",
                    values={"propertyId": number},
                )
            )
    return out


class GoogleAnalytics4Connector(OAuthTokenMixin, AnalyticsConnector):
    spec = ConnectorSpec(
        slug="google_analytics_4",
        name="Google Analytics 4",
        category="Analytics & Search",
        description="Sessions, conversions and referral traffic from a GA4 property.",
        fields=(
            text(
                "propertyId",
                "Property ID",
                "123456789",
                help_text=(
                    "Admin → Property settings shows this as PROPERTY ID. A "
                    "UA-… tracking id belongs to Universal Analytics and "
                    "cannot be reported on here."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Google" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Google',
                docs='Scope: analytics.readonly.',
            ),
        ),
        capabilities=frozenset(
            {Capability.READ_SESSIONS, Capability.READ_REFERRERS, Capability.READ_CONVERSIONS}
        ),
        requirements=(
            "A GA4 property. Universal Analytics is not supported: "
            "it stopped serving data and its API is retired.",
            "The credential needs Viewer access to the property, and "
            "the Data API must be enabled on the Google Cloud project.",
        ),
        docs_url="https://developers.google.com/analytics/devguides/reporting/data/v1",
        base_url="https://analyticsdata.googleapis.com/v1beta",
        # Read-only: every call here is a report or a listing. The referral
        # exclusion is not written through any API (see exclude_referrer).
        oauth=OAuthBinding(
            provider="google",
            scopes=("https://www.googleapis.com/auth/analytics.readonly",),
            access_summary=(
                "Read-only access to your Google Analytics reports, so agents can "
                "see visits, sources and conversions. Nothing in Analytics is changed."
            ),
            choice_noun="property",
            choice_fields=("propertyId",),
        ),
    )

    token_url = "https://oauth2.googleapis.com/token"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    @property
    def property_path(self) -> str:
        raw = self.credentials.require("propertyId").strip()
        return raw if raw.startswith("properties/") else f"properties/{raw}"

    def _run_report(self, body: dict) -> dict:
        return self.request("POST", f"/{self.property_path}:runReport", json_body=body) or {}

    @staticmethod
    def _rows(report: dict) -> list[dict]:
        return report.get("rows") or []

    # ── Sessions ───────────────────────────────────────────────────────────
    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        report = self._run_report(
            {
                "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                "dimensions": [{"name": "date"}],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "keyEvents"},
                    {"name": "totalRevenue"},
                ],
                # Organic only: paid sessions are reported by the ad platforms,
                # and double-counting them would corrupt the KPI.
                "dimensionFilter": {
                    "filter": {
                        "fieldName": "sessionDefaultChannelGroup",
                        "stringFilter": {"value": "Organic Search", "matchType": "EXACT"},
                    }
                },
                "orderBys": [{"dimension": {"dimensionName": "date"}}],
            }
        )

        out: list[SessionMetrics] = []
        for row in self._rows(report):
            dims = row.get("dimensionValues") or []
            metrics = row.get("metricValues") or []
            raw_date = dims[0].get("value") if dims else ""
            try:
                day = date(int(raw_date[0:4]), int(raw_date[4:6]), int(raw_date[6:8]))
            except (ValueError, IndexError):
                continue
            out.append(
                SessionMetrics(
                    day=day,
                    sessions=int(float(metrics[0].get("value") or 0)) if metrics else 0,
                    conversions=int(float(metrics[1].get("value") or 0)) if len(metrics) > 1 else 0,
                    revenue=round(float(metrics[2].get("value") or 0), 2)
                    if len(metrics) > 2
                    else 0.0,
                )
            )
        return out

    # ── Referrers ──────────────────────────────────────────────────────────
    def read_referrers(self, *, days: int = 1, limit: int = 100) -> list[TrafficSample]:
        report = self._run_report(
            {
                "dateRanges": [{"startDate": f"{max(days, 1)}daysAgo", "endDate": "today"}],
                "dimensions": [{"name": "sessionSource"}],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "bounceRate"},
                    {"name": "averageSessionDuration"},
                ],
                "limit": limit,
                "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            }
        )

        out: list[TrafficSample] = []
        for row in self._rows(report):
            dims = row.get("dimensionValues") or []
            metrics = row.get("metricValues") or []
            source = dims[0].get("value") if dims else ""
            if not source or source in ("(direct)", "(not set)"):
                continue
            out.append(
                TrafficSample(
                    source=source,
                    referrer=source,
                    sessions=int(float(metrics[0].get("value") or 0)) if metrics else 0,
                    bounce_rate=round(float(metrics[1].get("value") or 0), 3)
                    if len(metrics) > 1
                    else 0.0,
                    avg_session_seconds=round(float(metrics[2].get("value") or 0), 1)
                    if len(metrics) > 2
                    else 0.0,
                    channel="organic",
                )
            )
        return out

    #: GA4 has no public API for the unwanted-referrals list — it lives under
    #: the web stream's tag settings and is edited only in the GA4 interface.
    #: (The old call POSTed to an Admin API path that does not exist and so
    #: always failed.) Exposed so the console can say so instead of implying
    #: the exclusion was pushed.
    supports_referrer_exclusion = False

    def exclude_referrer(self, domain: str) -> bool:
        """Not possible through any public GA4 API; always returns False.

        Unwanted referrals are configured in GA4 → Admin → Data streams →
        (web stream) → Configure tag settings → List unwanted referrals. The
        platform-side block the spam guard records still stands; this only
        means GA4's own list has to be edited by hand.
        """
        log.info(
            "GA4 has no API for unwanted referrals; %s must be added in the GA4 "
            "interface (Data streams → Configure tag settings)",
            domain,
        )
        return False

    # ── Conversions ────────────────────────────────────────────────────────
    def read_conversions(self, *, days: int = 30) -> int:
        report = self._run_report(
            {
                "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                "metrics": [{"name": "keyEvents"}],
            }
        )
        rows = self._rows(report)
        if not rows:
            return 0
        metrics = rows[0].get("metricValues") or []
        return int(float(metrics[0].get("value") or 0)) if metrics else 0

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Every GA4 property the signed-in account can see (Admin API)."""
        choices: list[OAuthChoice] = []
        page = ""
        for _ in range(10):  # 200 per page; ten pages is far beyond a real account
            params = {"pageSize": 200, **({"pageToken": page} if page else {})}
            data = self.request(
                "GET", f"{ADMIN_API}/accountSummaries", params=params
            ) or {}
            choices.extend(parse_account_summaries(data))
            page = str(data.get("nextPageToken") or "")
            if not page:
                break
        return choices

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """List the properties this sign-in reaches, then read yesterday's
        figures with the same reports the agents run. Read-only throughout:
        GA4 has nothing this connector writes."""
        from app.connectors.base import verify_helpers as vh

        def properties() -> tuple[str, dict]:
            found = self.list_oauth_choices()
            ids = [c.values.get("propertyId", "") for c in found]
            configured = (self.credentials.get("propertyId") or "").strip().removeprefix("properties/")
            detail = f"This sign-in can read {vh.plural(len(found), 'Analytics property', 'Analytics properties')}."
            if configured:
                detail += (
                    " The chosen property is one of them."
                    if configured in ids
                    else " The chosen property is not among them."
                )
            return detail, {"count": len(found), "property_ids": ids[:20]}

        def sessions() -> tuple[str, dict]:
            rows = self.read_sessions(days=1)
            total = sum(r.sessions for r in rows)
            return (
                f"Read organic sessions for yesterday and today: {total} over {vh.plural(len(rows), 'day')}.",
                {"days": len(rows), "sessions": total},
            )

        def referrers() -> tuple[str, dict]:
            rows = self.read_referrers(days=1, limit=5)
            return (
                f"Read the top traffic sources since yesterday: {vh.plural(len(rows), 'source')} with a referrer.",
                {"count": len(rows)},
            )

        return [
            vh.read_step("list_properties", "List the Analytics properties this sign-in can read", properties),
            vh.read_step("sessions_yesterday", "Read organic sessions since yesterday", sessions),
            vh.read_step("referrers_yesterday", "Read the top five traffic sources since yesterday", referrers),
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            report = self._run_report(
                {
                    "dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
                    "metrics": [{"name": "sessions"}],
                }
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        rows = self._rows(report)
        sessions = rows[0]["metricValues"][0]["value"] if rows else "0"
        return HealthReport(
            ok=True, detail=f"Property reachable ({sessions} sessions/7d)", checked_at=utcnow()
        )


CONNECTOR_CLASS = GoogleAnalytics4Connector
