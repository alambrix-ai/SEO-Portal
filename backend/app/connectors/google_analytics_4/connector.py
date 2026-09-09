"""Google Analytics 4 connector — Data API v1beta plus the Admin API.

This is the platform's main source of truth for organic sessions and referral
traffic, so it feeds both the Reports screen and the referral-spam guard.
"""
from __future__ import annotations

from datetime import date

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import AnalyticsConnector, SessionMetrics, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


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
                    {"name": "conversions"},
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

    def exclude_referrer(self, domain: str) -> bool:
        """Add the domain to the property's unwanted-referrals list.

        The Admin API surface for this is limited, so a rejection is reported
        rather than raised: the platform-side block already stands.
        """
        try:
            self.request(
                "POST",
                f"https://analyticsadmin.googleapis.com/v1alpha/{self.property_path}"
                "/dataStreams:updateEnhancedMeasurementSettings",
                json_body={"unwantedReferrals": [{"domain": domain}]},
            )
        except ConnectorError as exc:
            log.info("GA4 declined a referral exclusion for %s: %s", domain, exc)
            return False
        return True

    # ── Conversions ────────────────────────────────────────────────────────
    def read_conversions(self, *, days: int = 30) -> int:
        report = self._run_report(
            {
                "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                "metrics": [{"name": "conversions"}],
            }
        )
        rows = self._rows(report)
        if not rows:
            return 0
        metrics = rows[0].get("metricValues") or []
        return int(float(metrics[0].get("value") or 0)) if metrics else 0

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
