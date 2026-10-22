"""Bing Webmaster Tools connector — query stats.

A second, independent source for search performance.

It does **not** provide backlinks. This connector used to claim
``READ_BACKLINKS`` and answer it with ``GetLinkCounts``, which returns the
verified site's *own pages* with a count of inbound links to each — not the
domains that link in, and nothing at all about a competitor. Those page URLs
were then filed as "linking domains", so the customer's own site showed up as
a competitor's new backlink. Backlink data comes from a dedicated index (see
:mod:`app.connectors.dataforseo_backlinks`).

Two ways in. **Sign in with Bing** (OAuth 2.0 through the platform's Bing
Webmaster OAuth client, scope ``webmaster.read``) sends a Bearer token to
``www.bing.com``, as Microsoft's OAuth page shows. The **API key** (the key
guide, used when the platform has no OAuth client, and under Advanced) is
sent as ``?apikey=`` to ``ssl.bing.com``, as the method reference shows.
"""
from __future__ import annotations

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
    SetupGuide,
)
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.connectors.base.oauth import OAuthTokenMixin
from app.connectors.base.interfaces import AnalyticsConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


GUIDE = SetupGuide(
    steps=(
        'Open Bing Webmaster Tools with the button below and sign in. Add and verify your site first if it is not there yet.',
        'Click Settings (top right), go to “API Access”, accept the terms if asked, and click “Generate API Key”.',
        'Copy the key and paste it here with your site address exactly as it appears in Bing Webmaster Tools.',
    ),
    key_url='https://www.bing.com/webmasters',
    key_url_label='Open Bing Webmaster Tools',
    key_field='apiKey',
    key_hint='One key works for all the sites you have verified in Bing',
)


#: The API host for OAuth (Bearer) calls, per Microsoft's OAuth page.
OAUTH_BASE_URL = "https://www.bing.com/webmaster/api.svc/json"


def parse_user_sites(data: dict) -> list[OAuthChoice]:
    """Verified sites out of ``GetUserSites`` (``d``). Unverified sites have
    no data to report, so they are not offered."""
    out: list[OAuthChoice] = []
    for site in data.get("d") or []:
        if not isinstance(site, dict) or not site.get("Url") or not site.get("IsVerified"):
            continue
        url = str(site["Url"])
        out.append(OAuthChoice(id=url, label=url, detail="Verified in Bing Webmaster Tools", values={"siteUrl": url}))
    return out


class BingWebmasterConnector(OAuthTokenMixin, AnalyticsConnector):
    spec = ConnectorSpec(
        slug="bing_webmaster",
        guide=GUIDE,
        name="Bing Webmaster Tools",
        category="Analytics & Search",
        description="Bing query stats for a verified site: the searches it appears for, clicks and position.",
        fields=(
            text("siteUrl", "Verified site URL", "https://yourdealership.com/"),
            secret("apiKey", "API key", "••••••••"),
        ),
        # READ_BACKLINKS deliberately absent: GetLinkCounts describes the
        # site's own pages, not who links to it. See the module docstring.
        capabilities=frozenset({Capability.READ_SEARCH_PERFORMANCE}),
        docs_url="https://learn.microsoft.com/en-us/bingwebmaster/",
        base_url="https://ssl.bing.com/webmaster/api.svc/json",
        # Read-only: query stats and the site list. webmaster.manage would
        # also allow submitting URLs and changing settings.
        oauth=OAuthBinding(
            provider="bing_webmaster",
            scopes=("webmaster.read",),
            platform_fields=frozenset({"apiKey"}),
            access_summary=(
                "Read-only access to your Bing Webmaster Tools data, so agents can see "
                "the searches your site appears for. Nothing in Bing is changed."
            ),
            choice_noun="site",
            choice_fields=("siteUrl",),
            token_fields=frozenset({"apiKey"}),
        ),
    )

    token_url = "https://www.bing.com/webmasters/oauth/token"

    @property
    def signed_in(self) -> bool:
        return from_platform_sign_in(self.credentials.values)

    @property
    def base_url(self) -> str:
        return OAUTH_BASE_URL if self.signed_in else super().base_url

    def auth_headers(self) -> dict[str, str]:
        if self.signed_in:
            return {"Content-Type": "application/json", "Authorization": "Bearer " + self.access_token}
        # Bing takes the key as a query parameter rather than a header; it is
        # appended per request in `_call`.
        return {"Content-Type": "application/json"}

    def _call(self, operation: str, params: dict | None = None, *, site: bool = True) -> dict:
        query: dict = {}
        if not self.signed_in:
            query["apikey"] = self.credentials.require("apiKey")
        if site:
            query["siteUrl"] = self.credentials.require("siteUrl")
        query.update(params or {})
        response = self.request("GET", f"/{operation}", params=query)
        return response if isinstance(response, dict) else {}

    def list_oauth_choices(self) -> list[OAuthChoice]:
        return parse_user_sites(self._call("GetUserSites", site=False))

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

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """List the verified sites, then read the query figures the agents
        read. Read-only: the sign-in only asks for webmaster.read."""
        from app.connectors.base import verify_helpers as vh

        def sites() -> tuple[str, dict]:
            found = self.list_oauth_choices()
            configured = (self.credentials.get("siteUrl") or "").rstrip("/")
            detail = f"This sign-in can read {vh.plural(len(found), 'verified site')}."
            if configured:
                listed = any(c.id.rstrip("/") == configured for c in found)
                detail += " The chosen site is one of them." if listed else " The chosen site is not among them."
            return detail, {"count": len(found)}

        def queries() -> tuple[str, dict]:
            data = self.read_search_performance()
            return (
                f"Read Bing's query figures for the site: {vh.plural(len(data['top_queries']), 'search phrase')}, "
                f"{data['clicks']} clicks.",
                {"queries": len(data["top_queries"]), "clicks": data["clicks"], "impressions": data["impressions"]},
            )

        return [
            vh.read_step("list_sites", "List the verified sites this sign-in can read", sites),
            vh.read_step("query_stats", "Read the site's Bing search figures", queries),
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self._call("GetUserSites", site=False)
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        sites = data.get("d") or []
        return HealthReport(
            ok=bool(sites),
            detail=f"{len(sites)} verified site(s)",
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = BingWebmasterConnector
