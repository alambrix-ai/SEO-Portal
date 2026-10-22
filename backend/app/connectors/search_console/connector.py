"""Google Search Console connector — Search Analytics.

Supplies the real queries a site ranks for, which is what the backlink
discovery agent uses to build its topic model rather than inferring one.

It does **not** provide backlinks. Search Console's Links report is not part
of the public Search Console API: there is no links endpoint, and the
``GET /sites/{site}/links`` call this connector used to make simply failed,
was swallowed, and returned an empty list — while the connector advertised
``READ_BACKLINKS`` and so satisfied the competitor monitor's preflight with a
source that could never answer. Even the UI report only covers the verified
property's own links, never a competitor's. Backlink data comes from a
dedicated index (see :mod:`app.connectors.dataforseo_backlinks`).
"""
from __future__ import annotations

from urllib.parse import quote

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import AnalyticsConnector, SearchDay, count_or_none
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Search Console keeps 16 months of data; a daily read asks for at most this.
MAX_DAILY_DAYS = 480
#: Search Console's figures lag about three days.
DATA_LAG_DAYS = 3

_PERMISSION = {
    "siteOwner": "Owner",
    "siteFullUser": "Full access",
    "siteRestrictedUser": "Restricted access",
}


def parse_sites(data: dict) -> list[OAuthChoice]:
    """Sites out of a ``sites.list`` response. Unverified ones are left out:
    Search Console reports nothing for them."""
    out: list[OAuthChoice] = []
    for entry in data.get("siteEntry") or []:
        url = str(entry.get("siteUrl") or "")
        level = str(entry.get("permissionLevel") or "")
        if not url or level == "siteUnverifiedUser":
            continue
        label = url[len("sc-domain:"):] if url.startswith("sc-domain:") else url
        kind = "Domain property" if url.startswith("sc-domain:") else "URL-prefix property"
        out.append(
            OAuthChoice(
                id=url,
                label=label,
                detail=f"{kind} · {_PERMISSION.get(level, level)}",
                values={"siteUrl": url},
            )
        )
    return out


class SearchConsoleConnector(OAuthTokenMixin, AnalyticsConnector):
    spec = ConnectorSpec(
        slug="search_console",
        name="Search Console",
        category="Analytics & Search",
        description="Query performance for a verified site: the searches it appears for, clicks and position.",
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
        # READ_BACKLINKS deliberately absent: the public API has no links
        # endpoint. See the module docstring.
        capabilities=frozenset({Capability.READ_SEARCH_PERFORMANCE}),
        requirements=(
            "The property must already be verified in Search Console.",
            "The credential's account needs at least Restricted access "
            "to that property — ownership is not required.",
        ),
        docs_url="https://developers.google.com/webmaster-tools/search-console-api-original",
        base_url="https://searchconsole.googleapis.com/webmasters/v3",
        # Read-only: search analytics and the site list.
        oauth=OAuthBinding(
            provider="google",
            scopes=("https://www.googleapis.com/auth/webmasters.readonly",),
            access_summary=(
                "Read-only access to your Search Console data, so agents can see "
                "the searches your site appears for. Nothing in Search Console is changed."
            ),
            choice_noun="site",
            choice_fields=("siteUrl",),
        ),
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

    def read_search_daily(self, days: int = 28) -> list[SearchDay]:
        """Clicks, impressions and average position per day, oldest first.

        The ``date`` dimension over the last ``days`` finished days (1 to
        :data:`MAX_DAILY_DAYS`), ending :data:`DATA_LAG_DAYS` before today.
        A day without a row had no impressions and is left out.
        """
        from datetime import date, timedelta

        days = min(max(int(days), 1), MAX_DAILY_DAYS)
        end = utcnow().date() - timedelta(days=DATA_LAG_DAYS)
        start = end - timedelta(days=days - 1)
        data = self.request(
            "POST",
            f"/sites/{self.site_param}/searchAnalytics/query",
            json_body={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": ["date"],
                "rowLimit": days,
                "dataState": "final",
            },
        )
        out: list[SearchDay] = []
        for row in (data or {}).get("rows") or []:
            if not isinstance(row, dict):
                continue
            keys = row.get("keys") or [""]
            try:
                day = date.fromisoformat(str(keys[0]))
            except (ValueError, IndexError, TypeError):
                continue
            if not start <= day <= end:
                continue
            position = row.get("position")
            out.append(
                SearchDay(
                    day=day,
                    clicks=count_or_none(row.get("clicks")) or 0,
                    impressions=count_or_none(row.get("impressions")) or 0,
                    position=round(float(position), 2)
                    if isinstance(position, (int, float)) and not isinstance(position, bool) and position > 0
                    else None,
                )
            )
        return sorted(out, key=lambda item: item.day)

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Verified sites the signed-in account can read."""
        return parse_sites(self.request("GET", "/sites") or {})

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """List the verified sites, then read one day of query figures with
        the agents' own report. Read-only: nothing here writes."""
        from app.connectors.base import verify_helpers as vh

        def sites() -> tuple[str, dict]:
            found = self.list_oauth_choices()
            configured = self.credentials.get("siteUrl") or ""
            detail = f"This sign-in can read {vh.plural(len(found), 'verified site')}."
            if configured:
                listed = any(c.id == configured for c in found)
                detail += " The chosen site is one of them." if listed else " The chosen site is not among them."
            return detail, {"count": len(found)}

        def performance() -> tuple[str, dict]:
            # Search Console's figures lag about three days, so "one day" is
            # the most recent finished day, not literally yesterday.
            data = self.read_search_performance(days=1)
            return (
                f"Read the latest finished day of search figures: "
                f"{vh.plural(len(data['top_queries']), 'search phrase')}, {data['clicks']} clicks.",
                {
                    "queries": len(data["top_queries"]),
                    "clicks": data["clicks"],
                    "impressions": data["impressions"],
                },
            )

        return [
            vh.read_step("list_sites", "List the verified sites this sign-in can read", sites),
            vh.read_step("search_performance", "Read one day of search figures (up to 25 phrases)", performance),
        ]

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
