"""Microsoft Advertising connector — Bing search and audience network.

The Microsoft Advertising API is SOAP for campaign management and asynchronous
for reporting, so this connector talks to the newer OData reporting surface for
reads and keeps writes to the operations it can perform synchronously.
"""
from __future__ import annotations

from app.connectors.base.ads_base import BaseAdsConnector
from app.connectors.base.connector import Capability, ConnectorSpec
from app.connectors.base.credentials import monthly_budget, oauth, secret, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API_VERSION = "v13"


class MicrosoftAdsConnector(OAuthTokenMixin, BaseAdsConnector):
    channel = "microsoft"

    spec = ConnectorSpec(
        slug="microsoft_ads",
        name="Microsoft Ads",
        category="Ad Platforms",
        description="Bing search and audience network: reporting, budgets, exclusions.",
        fields=(
            text(
                "customerId",
                "Customer ID",
                "123456789",
                help_text="Sent as the CustomerId header. Account settings shows it.",
            ),
            text(
                "accountId",
                "Account ID",
                "987654321",
                help_text=(
                    "The account being managed. Sent as CustomerAccountId, "
                    "which is the name you will see in Microsoft's docs."
                ),
            ),
            secret(
                "developerToken",
                "Developer token",
                "••••••••",
                help_text=(
                    "Required on every call. From Microsoft Advertising → "
                    "Tools → Developer settings."
                ),
            ),
            monthly_budget(),
            # The credential this connector has always required. It used
            # to be an "Authorize with Microsoft" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Microsoft',
                docs='From your Entra app registration.',
            ),
        ),
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.READ_TRAFFIC_QUALITY,
                Capability.BLOCK_PLACEMENT,
            }
        ),
        docs_url="https://learn.microsoft.com/en-us/advertising/guides/",
        base_url=f"https://campaign.api.bingads.microsoft.com/CampaignManagement/{API_VERSION}",
    )

    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    token_extra = {"scope": "https://ads.microsoft.com/msads.manage offline_access"}

    def auth_headers(self) -> dict[str, str]:
        headers = {
            "AuthenticationToken": self.access_token,
            "CustomerId": self.credentials.require("customerId"),
            "CustomerAccountId": self.credentials.require("accountId"),
            "Content-Type": "application/json",
        }
        token = self.credentials.get("developerToken")
        if token:
            headers["DeveloperToken"] = token
        return headers

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        data = self.request(
            "POST",
            "/Reporting/Query",
            json_body={
                "ReportName": "AutoMarketAccountPerformance",
                "Aggregation": "Summary",
                "Columns": ["Spend", "Impressions", "Clicks", "Conversions"],
                "Scope": {"AccountIds": [int(self.credentials.require("accountId"))]},
                "Time": {"PredefinedTime": self._predefined_time(days)},
            },
        )
        rows = (data or {}).get("Rows") or []
        if not rows:
            return ChannelPerformance(channel=self.channel)
        row = rows[0]
        return ChannelPerformance(
            channel=self.channel,
            spend=round(float(row.get("Spend") or 0), 2),
            impressions=int(float(row.get("Impressions") or 0)),
            clicks=int(float(row.get("Clicks") or 0)),
            conversions=int(float(row.get("Conversions") or 0)),
        )

    @staticmethod
    def _predefined_time(days: int) -> str:
        if days <= 7:
            return "LastSevenDays"
        if days <= 30:
            return "LastThirtyDays"
        return "LastThreeMonths"

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = self.credentials.get("campaignId")
        if not campaign_id:
            raise ConnectorError(
                "Set campaignId on the Microsoft Ads connector to let the "
                "budget engine write to it"
            )
        self.request(
            "POST",
            "/Campaigns",
            json_body={
                "Campaigns": [
                    {
                        "Id": int(campaign_id),
                        "DailyBudget": round(daily_budget, 2),
                        "BudgetType": "DailyBudgetStandard",
                    }
                ]
            },
        )
        return True

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        data = self.request(
            "POST",
            "/Reporting/Query",
            json_body={
                "ReportName": "AutoMarketPlacementPerformance",
                "Aggregation": "Summary",
                "Columns": ["Website", "Clicks", "Impressions", "Spend"],
                "Scope": {"AccountIds": [int(self.credentials.require("accountId"))]},
                "Time": {"PredefinedTime": "LastSevenDays"},
            },
        )
        out: list[TrafficSample] = []
        for row in ((data or {}).get("Rows") or [])[:limit]:
            clicks = int(float(row.get("Clicks") or 0))
            impressions = int(float(row.get("Impressions") or 0))
            out.append(
                TrafficSample(
                    source=str(row.get("Website") or "unknown site"),
                    sessions=clicks,
                    # An implausibly high click-through rate on a display
                    # placement is the classic signature of click injection.
                    bounce_rate=round(min(clicks / impressions, 1.0), 3) if impressions else 0.0,
                    avg_session_seconds=0.0,
                    channel=self.channel,
                    signals={"impressions": impressions},
                )
            )
        return out

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        self.request(
            "POST",
            "/NegativeSites",
            json_body={
                "AccountId": int(self.credentials.require("accountId")),
                "NegativeSites": [source],
            },
        )
        log.info("Excluded site %s on Microsoft Ads (%s)", source, reason)
        return True


CONNECTOR_CLASS = MicrosoftAdsConnector
