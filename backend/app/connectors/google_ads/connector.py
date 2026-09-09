"""Google Ads connector — Google Ads API v18 (GAQL reporting + mutates)."""
from __future__ import annotations

from app.connectors.base.ads_base import FULL_AD_CAPABILITIES, BaseAdsConnector
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import monthly_budget, oauth, secret, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API_VERSION = "v18"

# Google Ads reports in micros (millionths of the account currency).
MICROS = 1_000_000


class GoogleAdsConnector(OAuthTokenMixin, BaseAdsConnector):
    channel = "google"

    spec = ConnectorSpec(
        slug="google_ads",
        name="Google Ads",
        category="Ad Platforms",
        description="Search and display campaigns: reporting, budgets, RSAs, exclusions.",
        fields=(
            text(
                "customerId",
                "Customer ID",
                "123-456-7890",
                help_text=(
                    "The ten-digit id at the top right of the Google Ads "
                    "account you are managing. Hyphens are fine — the API "
                    "wants it without them and they are stripped here."
                ),
            ),
            # Required on every Google Ads API call, without exception.
            secret(
                "developerToken",
                "Developer token",
                "••••••••",
                help_text=(
                    "From the manager account's API Center. Every call needs "
                    "one, and a token still in test access reaches only test "
                    "accounts."
                ),
            ),
            # The agency case. auth_headers() has always sent this header;
            # until now nothing rendered a field for it.
            text(
                "loginCustomerId",
                "Login customer ID",
                "123-456-7890",
                required=False,
                help_text=(
                    "Only when you reach the account through a manager (MCC) "
                    "account: set it to the manager's ten-digit id. Leave it "
                    "blank when the credentials belong to the account itself."
                ),
            ),
            monthly_budget(),
            # The credential this connector has always required. It used
            # to be an "Authorize with Google" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Google',
                docs='Create it in Google Cloud with the adwords scope.',
            ),
        ),
        capabilities=FULL_AD_CAPABILITIES,
        docs_url="https://developers.google.com/google-ads/api/docs/start",
        base_url=f"https://googleads.googleapis.com/{API_VERSION}",
    )

    token_url = "https://oauth2.googleapis.com/token"

    @property
    def customer_id(self) -> str:
        # The API rejects the hyphenated form people paste from the UI.
        return self.credentials.require("customerId").replace("-", "")

    def auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }
        developer_token = self.credentials.get("developerToken")
        if developer_token:
            headers["developer-token"] = developer_token
        login_customer = self.credentials.get("loginCustomerId")
        if login_customer:
            # Set when access is through a manager (MCC) account.
            headers["login-customer-id"] = login_customer.replace("-", "")
        return headers

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        query = (
            "SELECT metrics.cost_micros, metrics.impressions, metrics.clicks, "
            "metrics.conversions FROM customer "
            f"WHERE segments.date DURING LAST_{days}_DAYS"
        )
        data = self.request(
            "POST",
            f"/customers/{self.customer_id}/googleAds:searchStream",
            json_body={"query": query},
        )

        spend = impressions = clicks = 0.0
        conversions = 0.0
        # searchStream returns a list of batches, each with its own results.
        batches = data if isinstance(data, list) else [data]
        for batch in batches:
            for row in (batch or {}).get("results") or []:
                metrics = row.get("metrics") or {}
                spend += float(metrics.get("costMicros") or 0) / MICROS
                impressions += float(metrics.get("impressions") or 0)
                clicks += float(metrics.get("clicks") or 0)
                conversions += float(metrics.get("conversions") or 0)

        return ChannelPerformance(
            channel=self.channel,
            spend=round(spend, 2),
            impressions=int(impressions),
            clicks=int(clicks),
            conversions=int(conversions),
        )

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        budget_id = self.credentials.get("campaignBudgetId")
        if not budget_id:
            raise ConnectorError(
                "Set campaignBudgetId on the Google Ads connector to let the "
                "budget engine write to it"
            )
        self.request(
            "POST",
            f"/customers/{self.customer_id}/campaignBudgets:mutate",
            json_body={
                "operations": [
                    {
                        "update": {
                            "resourceName": (
                                f"customers/{self.customer_id}/campaignBudgets/{budget_id}"
                            ),
                            "amountMicros": int(daily_budget * MICROS),
                        },
                        "updateMask": "amountMicros",
                    }
                ]
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
        """Create a responsive search ad asset group.

        Google composes RSAs from headline and description assets rather than
        fixed banners, so the DCO variant maps onto assets here.
        """
        ad_group_id = self.credentials.get("adGroupId")
        if not ad_group_id:
            raise ConnectorError(
                "Set adGroupId on the Google Ads connector to publish creatives"
            )
        response = self.request(
            "POST",
            f"/customers/{self.customer_id}/adGroupAds:mutate",
            json_body={
                "operations": [
                    {
                        "create": {
                            "adGroup": f"customers/{self.customer_id}/adGroups/{ad_group_id}",
                            "status": "PAUSED",  # never live without review
                            "ad": {
                                "responsiveSearchAd": {
                                    "headlines": [{"text": headline[:30]}],
                                    "descriptions": [{"text": body_copy[:90]}],
                                },
                                "finalUrls": [self.credentials.get("finalUrl", "")],
                            },
                        }
                    }
                ]
            },
        )
        results = (response or {}).get("results") or []
        return str(results[0].get("resourceName")) if results else ""

    # ── Audiences ──────────────────────────────────────────────────────────
    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        response = self.request(
            "POST",
            f"/customers/{self.customer_id}/userLists:mutate",
            json_body={
                "operations": [
                    {
                        "create": {
                            "name": f"AutoMarket — {label}"[:80],
                            "description": "First-party segment: " + ", ".join(signals[:5]),
                            "membershipLifeSpan": 540,
                            # A rule-based list: no identifiers are uploaded,
                            # only the definition of who qualifies.
                            "crmBasedUserList": {"uploadKeyType": "CONTACT_INFO"},
                        }
                    }
                ]
            },
        )
        results = (response or {}).get("results") or []
        return str(results[0].get("resourceName")) if results else ""

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        query = (
            "SELECT detail_placement_view.group_placement_target_url, "
            "metrics.clicks, metrics.bounce_rate, metrics.average_time_on_site "
            "FROM detail_placement_view WHERE segments.date DURING LAST_7_DAYS "
            f"ORDER BY metrics.clicks DESC LIMIT {limit}"
        )
        data = self.request(
            "POST",
            f"/customers/{self.customer_id}/googleAds:searchStream",
            json_body={"query": query},
        )
        out: list[TrafficSample] = []
        batches = data if isinstance(data, list) else [data]
        for batch in batches:
            for row in (batch or {}).get("results") or []:
                view = row.get("detailPlacementView") or {}
                metrics = row.get("metrics") or {}
                out.append(
                    TrafficSample(
                        source=str(view.get("groupPlacementTargetUrl") or "unknown placement"),
                        sessions=int(metrics.get("clicks") or 0),
                        bounce_rate=float(metrics.get("bounceRate") or 0.0),
                        avg_session_seconds=float(metrics.get("averageTimeOnSite") or 0.0),
                        channel=self.channel,
                    )
                )
        return out

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        self.request(
            "POST",
            f"/customers/{self.customer_id}/customerNegativeCriteria:mutate",
            json_body={"operations": [{"create": {"placement": {"url": source}}}]},
        )
        log.info("Excluded placement %s on Google Ads (%s)", source, reason)
        return True


CONNECTOR_CLASS = GoogleAdsConnector
