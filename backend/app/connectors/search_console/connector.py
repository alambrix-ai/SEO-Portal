"""Google Search Console connector — Search Analytics and Links.

Supplies the real queries a site ranks for, which is what the backlink
discovery agent uses to build its topic model rather than inferring one, and
the inbound-link data the competitor monitor compares against.
"""
from __future__ import annotations

from urllib.parse import quote

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import AnalyticsConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class SearchConsoleConnector(OAuthTokenMixin, AnalyticsConnector):
    spec = ConnectorSpec(
        slug="search_console",
        name="Search Console",
        category="Analytics & Search",
        description="Query performance, indexing state and inbound links for a verified site.",
        fields=(
            text("siteUrl", "Verified site URL", "https://yourdealership.com/"),
            # The credential this connector has always required. It used
            # to be an "Authorize with Google" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Google',
                docs='Scope: webmasters.readonly.',
            ),
        ),
        capabilities=frozenset(
            {Capability.READ_SEARCH_PERFORMANCE, Capability.READ_BACKLINKS}
        ),
        requirements=(
            "The property must already be verified in Search Console.",
            "The credential's account needs at least Restricted access "
            "to that property — ownership is not required.",
        ),
        docs_url="https://developers.google.com/webmaster-tools/search-console-api-original",
        base_url="https://searchconsole.googleapis.com/webmasters/v3",
    )

    token_url = "https://oauth2.googleapis.com/token"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    @property
    def site_param(self) -> str:
        # The property identifier has to be URL-encoded in the path.
        return quote(self.credentials.require("siteUrl"), safe="")

    # ── Search performance ─────────────────────────────────────────────────
    def read_search_performance(self, *, days: int = 28) -> dict:
        from datetime import timedelta

        end = utcnow().date()
        # Search Console data lags by about three days; asking for today
        # returns an empty set and looks like a broken connector.
        end = end - timedelta(days=3)
        start = end - timedelta(days=days)

        data = self.request(
            "POST",
            f"/sites/{self.site_param}/searchAnalytics/query",
            json_body={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": ["query"],
                "rowLimit": 25,
                "dataState": "final",
            },
        )
        rows = (data or {}).get("rows") or []
        clicks = sum(int(row.get("clicks") or 0) for row in rows)
        impressions = sum(int(row.get("impressions") or 0) for row in rows)
        positions = [float(row.get("position") or 0) for row in rows if row.get("position")]
        return {
            "top_queries": [
                (row.get("keys") or [""])[0] for row in rows if (row.get("keys") or [""])[0]
            ],
            "clicks": clicks,
            "impressions": impressions,
            "average_position": round(sum(positions) / len(positions), 1) if positions else 0.0,
            "days": days,
        }

    # ── Links ──────────────────────────────────────────────────────────────
    def read_backlinks(self, *, limit: int = 100) -> list[dict]:
        """Inbound linking sites.

        Search Console's Links report is not exposed through the public API,
        so this uses the top-linking-pages surface where available and
        reports nothing when it is not — the competitor monitor treats an
        empty list as "no data", never as "no links".
        """
        try:
            data = self.request(
                "GET", f"/sites/{self.site_param}/links", params={"limit": limit}
            )
        except ConnectorError as exc:
            log.info("Search Console exposes no links API on this property: %s", exc)
            return []

        out: list[dict] = []
        for row in (data or {}).get("links") or []:
            out.append(
                {
                    "domain": str(row.get("domain") or "").lower(),
                    "authority": int(row.get("authority") or 0),
                    "anchor": row.get("anchorText") or "",
                    "url": row.get("url") or "",
                }
            )
        return out[:limit]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/sites")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        entries = (data or {}).get("siteEntry") or []
        target = self.credentials.get("siteUrl")
        verified = any(entry.get("siteUrl") == target for entry in entries)
        return HealthReport(
            ok=verified,
            detail=(
                f"{target} is verified"
                if verified
                else f"{target} is not among the {len(entries)} verified properties"
            ),
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = SearchConsoleConnector
