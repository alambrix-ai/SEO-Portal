"""Bing Webmaster Tools connector — query stats and inbound links.

A second, independent source for search performance and backlinks. That
independence matters: the competitor monitor only reports a link it can see in
real data, so having two providers is the difference between detecting a
competitor's new placement and missing it.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import AnalyticsConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class BingWebmasterConnector(AnalyticsConnector):
    spec = ConnectorSpec(
        slug="bing_webmaster",
        name="Bing Webmaster Tools",
        category="Analytics & Search",
        description="Bing query stats and inbound-link data for a verified site.",
        fields=(
            text("siteUrl", "Verified site URL", "https://yourdealership.com/"),
            secret("apiKey", "API key", "••••••••"),
        ),
        capabilities=frozenset(
            {Capability.READ_SEARCH_PERFORMANCE, Capability.READ_BACKLINKS}
        ),
        docs_url="https://learn.microsoft.com/en-us/bingwebmaster/",
        base_url="https://ssl.bing.com/webmaster/api.svc/json",
    )

    def auth_headers(self) -> dict[str, str]:
        # Bing takes the key as a query parameter rather than a header; it is
        # appended per request in `_call`.
        return {"Content-Type": "application/json"}

    def _call(self, operation: str, params: dict | None = None) -> dict:
        query = {
            "apikey": self.credentials.require("apiKey"),
            "siteUrl": self.credentials.require("siteUrl"),
            **(params or {}),
        }
        response = self.request("GET", f"/{operation}", params=query)
        return response if isinstance(response, dict) else {}

    # ── Search performance ─────────────────────────────────────────────────
    def read_search_performance(self, *, days: int = 28) -> dict:
        data = self._call("GetQueryStats")
        rows = data.get("d") or []
        clicks = sum(int(row.get("Clicks") or 0) for row in rows)
        impressions = sum(int(row.get("Impressions") or 0) for row in rows)
        positions = [
            float(row.get("AvgClickPosition") or 0)
            for row in rows
            if row.get("AvgClickPosition")
        ]
        # Bing returns queries unsorted; the topic model wants the strongest.
        ranked = sorted(rows, key=lambda r: int(r.get("Clicks") or 0), reverse=True)
        return {
            "top_queries": [str(row.get("Query") or "") for row in ranked[:20] if row.get("Query")],
            "clicks": clicks,
            "impressions": impressions,
            "average_position": round(sum(positions) / len(positions), 1) if positions else 0.0,
            "days": days,
        }

    # ── Links ──────────────────────────────────────────────────────────────
    def read_backlinks(self, *, limit: int = 100) -> list[dict]:
        data = self._call("GetLinkCounts", {"page": 0})
        out: list[dict] = []
        for row in (data.get("d") or [])[:limit]:
            domain = str(row.get("Url") or "").lower()
            if domain.startswith("http"):
                from urllib.parse import urlsplit

                domain = urlsplit(domain).netloc
            if not domain:
                continue
            out.append(
                {
                    "domain": domain.removeprefix("www."),
                    # Bing reports link counts rather than an authority score;
                    # the count is a usable proxy, capped into the same range.
                    "authority": min(int(row.get("Count") or 0), 100),
                    "anchor": row.get("AnchorText") or "",
                    "url": row.get("Url") or "",
                }
            )
        return out

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self._call("GetUserSites")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        sites = data.get("d") or []
        return HealthReport(
            ok=bool(sites),
            detail=f"{len(sites)} verified site(s)",
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = BingWebmasterConnector
