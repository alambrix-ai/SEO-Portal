"""Adobe Analytics connector — Analytics 2.0 reporting API.

The enterprise alternative to GA4 for organisations already standardised on
Adobe Experience Cloud. It serves the same two jobs to the fleet: session
trend, and the referrer feed the spam guard assesses.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import AnalyticsConnector, SessionMetrics, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class AdobeAnalyticsConnector(AnalyticsConnector):
    spec = ConnectorSpec(
        slug="adobe_analytics",
        name="Adobe Analytics",
        category="Analytics & Search",
        description="Sessions and referrer detail from an Adobe Analytics report suite.",
        fields=(
            text(
                "orgId",
                "IMS Organization ID",
                "12345@AdobeOrg",
                help_text="Adobe Developer Console → your project → Credentials.",
            ),
            text(
                "globalCompanyId",
                "Global Company ID",
                "yourcompany",
                help_text=(
                    "Every Analytics 2.0 endpoint carries this in the path. "
                    "It is not the report suite id — GET /discovery/me "
                    "returns it as globalCompanyId."
                ),
            ),
            text(
                "reportSuiteId",
                "Report Suite ID",
                "yourcompanyprod",
                help_text="The rsid reports are run against.",
            ),
            # Sent as x-api-key on every request, so not optional.
            text("clientId", "Client ID", ""),
            secret("clientSecret", "Client Secret", "••••••••"),
        ),
        capabilities=frozenset({Capability.READ_SESSIONS, Capability.READ_REFERRERS}),
        docs_url="https://developer.adobe.com/analytics-apis/docs/2.0/",
        base_url="https://analytics.adobe.io/api",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("accessToken"),
            "x-api-key": self.credentials.require("clientId"),
            "x-gw-ims-org-id": self.credentials.require("orgId"),
            "Content-Type": "application/json",
        }

    @property
    def report_suite(self) -> str:
        return self.credentials.require("reportSuiteId")

    def _report(self, body: dict) -> dict:
        # No fallback to the report suite id. They are different
        # identifiers, and substituting one for the other produced a 404 that
        # read like a permissions problem for as long as anybody looked.
        company = self.credentials.require("globalCompanyId")
        return self.request("POST", f"/{company}/reports", json_body=body) or {}

    @staticmethod
    def _window(days: int) -> str:
        end = date.today()
        start = end - timedelta(days=days)
        return f"{start.isoformat()}T00:00:00.000/{end.isoformat()}T23:59:59.999"

    # ── Sessions ───────────────────────────────────────────────────────────
    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        data = self._report(
            {
                "rsid": self.report_suite,
                "globalFilters": [{"type": "dateRange", "dateRange": self._window(days)}],
                "metricContainer": {
                    "metrics": [
                        {"id": "metrics/visits", "columnId": "visits"},
                        {"id": "metrics/orders", "columnId": "orders"},
                        {"id": "metrics/revenue", "columnId": "revenue"},
                    ]
                },
                "dimension": "variables/daterangeday",
                "settings": {"limit": days, "page": 0},
            }
        )

        out: list[SessionMetrics] = []
        for row in (data or {}).get("rows") or []:
            values = row.get("data") or []
            raw = str(row.get("value") or "")
            parsed = self._parse_day(raw)
            if parsed is None:
                continue
            out.append(
                SessionMetrics(
                    day=parsed,
                    sessions=int(float(values[0] or 0)) if values else 0,
                    conversions=int(float(values[1] or 0)) if len(values) > 1 else 0,
                    revenue=round(float(values[2] or 0), 2) if len(values) > 2 else 0.0,
                )
            )
        return out

    @staticmethod
    def _parse_day(raw: str) -> date | None:
        """Adobe returns day labels in several formats depending on locale."""
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%d/%m/%Y"):
            try:
                from datetime import datetime

                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    # ── Referrers ──────────────────────────────────────────────────────────
    def read_referrers(self, *, days: int = 1, limit: int = 100) -> list[TrafficSample]:
        data = self._report(
            {
                "rsid": self.report_suite,
                "globalFilters": [
                    {"type": "dateRange", "dateRange": self._window(max(days, 1))}
                ],
                "metricContainer": {
                    "metrics": [
                        {"id": "metrics/visits", "columnId": "visits"},
                        {"id": "metrics/bouncerate", "columnId": "bounce"},
                        {"id": "metrics/averagetimespentonsite", "columnId": "dwell"},
                    ]
                },
                "dimension": "variables/referrerdomain",
                "settings": {"limit": limit, "page": 0},
            }
        )

        out: list[TrafficSample] = []
        for row in (data or {}).get("rows") or []:
            values = row.get("data") or []
            domain = str(row.get("value") or "")
            if not domain or domain.lower() in ("typed/bookmarked", "none", "unspecified"):
                continue
            out.append(
                TrafficSample(
                    source=domain,
                    referrer=domain,
                    sessions=int(float(values[0] or 0)) if values else 0,
                    bounce_rate=round(float(values[1] or 0) / 100, 3) if len(values) > 1 else 0.0,
                    avg_session_seconds=round(float(values[2] or 0), 1)
                    if len(values) > 2
                    else 0.0,
                    channel="organic",
                )
            )
        return out

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            self.read_sessions(days=1)
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True, detail=f"Report suite {self.report_suite} reachable", checked_at=utcnow()
        )


CONNECTOR_CLASS = AdobeAnalyticsConnector
