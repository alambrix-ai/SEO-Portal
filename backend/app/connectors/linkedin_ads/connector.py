"""LinkedIn Ads connector — Marketing Solutions API (sponsored content)."""
from __future__ import annotations

from app.connectors.base.ads_base import BaseAdsConnector
from app.connectors.base.connector import Capability, ConnectorSpec
from app.connectors.base.credentials import monthly_budget, oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import ChannelPerformance
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API_VERSION = "202501"


class LinkedInAdsConnector(OAuthTokenMixin, BaseAdsConnector):
    channel = "linkedin"

    spec = ConnectorSpec(
        slug="linkedin_ads",
        name="LinkedIn Ads",
        category="Ad Platforms",
        description="Sponsored content: analytics, budgets, creatives, matched audiences.",
        fields=(
            text(
                "adAccountId",
                "Ad account ID",
                "509912345",
                help_text=(
                    "The nine-digit Account ID from Campaign Manager. A full "
                    "urn:li:sponsoredAccount:… is accepted too."
                ),
            ),
            text(
                "organizationId",
                "Organization ID",
                "12345",
                required=False,
                help_text=(
                    "The company page the creatives are published as, from "
                    "its admin page URL. Needed only to publish creatives; "
                    "a urn:li:organization:… is accepted too."
                ),
            ),
            monthly_budget(),
            # The credential this connector has always required. It used
            # to be an "Authorize with LinkedIn" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='LinkedIn',
                docs='From your LinkedIn app, with r_ads and rw_ads.',
            ),
        ),
        # LinkedIn exposes no placement-level traffic-quality data, so the
        # fraud agent routes around this connector rather than guessing.
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.UPLOAD_CREATIVE,
                Capability.PUSH_AUDIENCE,
            }
        ),
        docs_url="https://learn.microsoft.com/en-us/linkedin/marketing/",
        base_url="https://api.linkedin.com/rest",
    )

    @property
    def account_urn(self) -> str:
        raw = self.credentials.require("adAccountId").strip()
        return raw if raw.startswith("urn:") else f"urn:li:sponsoredAccount:{raw}"

    @property
    def account_id(self) -> str:
        return self.account_urn.rsplit(":", 1)[-1]

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "LinkedIn-Version": API_VERSION,
            # Required for the versioned REST surface.
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        data = self.request(
            "GET",
            "/adAnalytics",
            params={
                "q": "analytics",
                "pivot": "ACCOUNT",
                "timeGranularity": "ALL",
                "dateRange.start.day": 1,
                "accounts[0]": self.account_urn,
                "fields": "costInUsd,impressions,clicks,externalWebsiteConversions",
            },
        )
        rows = (data or {}).get("elements") or []
        if not rows:
            return ChannelPerformance(channel=self.channel)
        row = rows[0]
        return ChannelPerformance(
            channel=self.channel,
            spend=round(float(row.get("costInUsd") or 0), 2),
            impressions=int(row.get("impressions") or 0),
            clicks=int(row.get("clicks") or 0),
            conversions=int(row.get("externalWebsiteConversions") or 0),
        )

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = self.credentials.get("campaignId")
        if not campaign_id:
            raise ConnectorError(
                "Set campaignId on the LinkedIn Ads connector to let the "
                "budget engine write to it"
            )
        self.request(
            "POST",
            f"/adCampaigns/{campaign_id}",
            json_body={
                "patch": {
                    "$set": {
                        "dailyBudget": {
                            "amount": f"{daily_budget:.2f}",
                            "currencyCode": "USD",
                        }
                    }
                }
            },
        )
        return True

    # ── Creative ───────────────────────────────────────────────────────────
    def _live_upload_creative(
        self,
        *,
        headline: str,
        body_copy: str,
        call_to_action: str,
        dimensions: str,
        audience_segment: str,
    ) -> str:
        organization = (self.credentials.get("organizationId") or "").strip()
        if not organization:
            raise ConnectorError(
                "Set Organization ID on the LinkedIn Ads connector to publish "
                "creatives — LinkedIn requires the company page they are "
                "published as."
            )
        if not organization.startswith("urn:"):
            organization = f"urn:li:organization:{organization}"
        response = self.request(
            "POST",
            "/posts",
            json_body={
                "author": organization,
                "commentary": f"{headline}\n\n{body_copy}",
                "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "NONE"},
                # Created as a draft; going live is a campaign-level action.
                "lifecycleState": "DRAFT",
                "isReshareDisabledByAuthor": False,
            },
        )
        return str((response or {}).get("id") or "")

    # ── Audiences ──────────────────────────────────────────────────────────
    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        response = self.request(
            "POST",
            "/dmpSegments",
            json_body={
                "account": self.account_urn,
                "name": f"AutoMarket — {label}"[:100],
                "description": "First-party segment: " + ", ".join(signals[:5]),
                # USER_BEHAVIOUR keeps this a definition-only segment: no
                # member identifiers are transmitted.
                "type": "USER",
                "sourcePlatform": "OTHER",
                "accessPolicy": "PRIVATE",
            },
        )
        return str((response or {}).get("id") or "")


CONNECTOR_CLASS = LinkedInAdsConnector
