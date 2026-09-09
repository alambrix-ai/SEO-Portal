"""Google Business Profile connector — local listing data and posts.

Local intent is where a multi-location business actually converts, so this
supplies the ground truth behind ``LocalBusiness`` schema — real opening
hours, real addresses — instead of letting a model invent them.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import AnalyticsConnector, SessionMetrics
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class GoogleBusinessProfileConnector(OAuthTokenMixin, AnalyticsConnector):
    spec = ConnectorSpec(
        slug="google_business_profile",
        name="Google Business Profile",
        category="Analytics & Search",
        description="Location details, opening hours and local search insights.",
        fields=(
            text("locationId", "Business location ID", "locations/1234567890"),
            text("accountId", "Account ID", "accounts/1234567890", required=False),
            # The credential this connector has always required. It used
            # to be an "Authorize with Google" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Google',
                docs='Scope: business.manage.',
            ),
        ),
        capabilities=frozenset({Capability.READ_SESSIONS}),
        docs_url="https://developers.google.com/my-business",
        base_url="https://mybusinessbusinessinformation.googleapis.com/v1",
    )

    token_url = "https://oauth2.googleapis.com/token"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    @property
    def location_path(self) -> str:
        raw = self.credentials.require("locationId").strip()
        return raw if raw.startswith("locations/") else f"locations/{raw}"

    # ── Location facts ─────────────────────────────────────────────────────
    def read_location(self) -> dict:
        """The verified facts a LocalBusiness graph should be built from."""
        data = self.request(
            "GET",
            f"/{self.location_path}",
            params={
                "readMask": "title,storefrontAddress,phoneNumbers,regularHours,websiteUri"
            },
        )
        address_parts = (data or {}).get("storefrontAddress") or {}
        lines = address_parts.get("addressLines") or []
        return {
            "name": (data or {}).get("title") or "",
            "address": ", ".join([*lines, address_parts.get("locality") or ""]).strip(", "),
            "phone": ((data or {}).get("phoneNumbers") or {}).get("primaryPhone") or "",
            "opening_hours": self._format_hours((data or {}).get("regularHours") or {}),
            "website": (data or {}).get("websiteUri") or "",
        }

    @staticmethod
    def _format_hours(regular_hours: dict) -> str:
        """Condense the API's per-day periods into a schema.org hours string."""
        periods = regular_hours.get("periods") or []
        if not periods:
            return ""
        day_abbr = {
            "MONDAY": "Mo", "TUESDAY": "Tu", "WEDNESDAY": "We", "THURSDAY": "Th",
            "FRIDAY": "Fr", "SATURDAY": "Sa", "SUNDAY": "Su",
        }
        rendered: list[str] = []
        for period in periods:
            day = day_abbr.get(str(period.get("openDay") or ""), "")
            open_time = period.get("openTime") or {}
            close_time = period.get("closeTime") or {}
            if not day:
                continue
            rendered.append(
                f"{day} {open_time.get('hours', 9):02d}:{open_time.get('minutes', 0):02d}"
                f"-{close_time.get('hours', 18):02d}:{close_time.get('minutes', 0):02d}"
            )
        return ", ".join(rendered)

    # ── Local insights ─────────────────────────────────────────────────────
    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        """Local discovery impressions, reported as a session series.

        Folded into the same shape as web sessions so the Reports screen can
        show local and organic discovery on one axis.
        """
        try:
            data = self.request(
                "POST",
                f"https://businessprofileperformance.googleapis.com/v1/{self.location_path}"
                ":fetchMultiDailyMetricsTimeSeries",
                params={
                    "dailyMetrics": [
                        "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
                        "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
                    ]
                },
            )
        except ConnectorError as exc:
            log.info("Business Profile performance unavailable: %s", exc)
            return []

        from datetime import date as date_cls

        totals: dict[date_cls, int] = {}
        for series in (data or {}).get("multiDailyMetricTimeSeries") or []:
            for entry in (series.get("dailyMetricTimeSeries") or []):
                for point in (entry.get("timeSeries") or {}).get("datedValues") or []:
                    raw = point.get("date") or {}
                    try:
                        day = date_cls(
                            int(raw.get("year")), int(raw.get("month")), int(raw.get("day"))
                        )
                    except (TypeError, ValueError):
                        continue
                    totals[day] = totals.get(day, 0) + int(point.get("value") or 0)

        return [
            SessionMetrics(day=day, sessions=count) for day, count in sorted(totals.items())
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            location = self.read_location()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True,
            detail=f"Connected to {location.get('name') or self.location_path}",
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = GoogleBusinessProfileConnector
