"""Meta Ads connector — Marketing API (Facebook and Instagram placements)."""
from __future__ import annotations

from app.connectors.base.ads_base import FULL_AD_CAPABILITIES, BaseAdsConnector
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import monthly_budget, oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API_VERSION = "v21.0"


class MetaAdsConnector(OAuthTokenMixin, BaseAdsConnector):
    channel = "meta"

    spec = ConnectorSpec(
        slug="meta_ads",
        name="Meta Ads",
        category="Ad Platforms",
        description="Facebook and Instagram: insights, budgets, creatives, custom audiences.",
        fields=(
            text(
                "adAccountId",
                "Ad account ID",
                "act_1234567890",
                help_text=(
                    "As Ads Manager shows it, including the act_ prefix — "
                    "that prefix is part of the id in the Marketing API."
                ),
            ),
            text("pageId", "Page ID", "1234567890", required=False),
            monthly_budget(),
            # The credential this connector has always required. It used
            # to be an "Authorize with Meta" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='Meta',
                docs='A system user access token from Business Manager.',
            ),
        ),
        capabilities=FULL_AD_CAPABILITIES,
        docs_url="https://developers.facebook.com/docs/marketing-apis",
        base_url=f"https://graph.facebook.com/{API_VERSION}",
    )

    @property
    def account_id(self) -> str:
        raw = self.credentials.require("adAccountId").strip()
        # The Graph API requires the act_ prefix; people often omit it.
        return raw if raw.startswith("act_") else f"act_{raw}"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        data = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "fields": "spend,impressions,clicks,actions",
                "date_preset": self._date_preset(days),
            },
        )
        rows = (data or {}).get("data") or []
        if not rows:
            return ChannelPerformance(channel=self.channel)

        row = rows[0]
        conversions = 0
        for action in row.get("actions") or []:
            # Meta reports every action type; only purchase-shaped ones are
            # conversions for CAC purposes.
            if str(action.get("action_type", "")).endswith(("purchase", "lead", "complete_registration")):
                conversions += int(float(action.get("value") or 0))

        return ChannelPerformance(
            channel=self.channel,
            spend=round(float(row.get("spend") or 0), 2),
            impressions=int(row.get("impressions") or 0),
            clicks=int(row.get("clicks") or 0),
            conversions=conversions,
        )

    @staticmethod
    def _date_preset(days: int) -> str:
        if days <= 7:
            return "last_7d"
        if days <= 30:
            return "last_30d"
        return "last_90d"

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = self.credentials.get("campaignId")
        if not campaign_id:
            raise ConnectorError(
                "Set campaignId on the Meta Ads connector to let the budget "
                "engine write to it"
            )
        # Meta takes budgets in minor units (cents).
        self.request(
            "POST",
            f"/{campaign_id}",
            json_body={"daily_budget": int(daily_budget * 100)},
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
        page_id = self.credentials.get("pageId")
        if not page_id:
            raise ConnectorError("Set pageId on the Meta Ads connector to publish creatives")

        response = self.request(
            "POST",
            f"/{self.account_id}/adcreatives",
            json_body={
                "name": f"AutoMarket — {headline[:40]} ({dimensions})",
                "object_story_spec": {
                    "page_id": page_id,
                    "link_data": {
                        "message": body_copy,
                        "name": headline,
                        "link": self.credentials.get("finalUrl", ""),
                        "call_to_action": {"type": self._cta_type(call_to_action)},
                    },
                },
            },
        )
        return str((response or {}).get("id") or "")

    @staticmethod
    def _cta_type(label: str) -> str:
        """Map free-text CTAs onto Meta's fixed enum."""
        lowered = label.lower()
        for needles, enum in (
            (("book", "appointment", "test drive"), "BOOK_TRAVEL"),
            (("quote", "get a quote"), "GET_QUOTE"),
            (("call",), "CALL_NOW"),
            (("shop", "buy", "offer"), "SHOP_NOW"),
            (("sign up", "register"), "SIGN_UP"),
        ):
            if any(n in lowered for n in needles):
                return enum
        return "LEARN_MORE"

    # ── Audiences ──────────────────────────────────────────────────────────
    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        response = self.request(
            "POST",
            f"/{self.account_id}/customaudiences",
            json_body={
                "name": f"AutoMarket — {label}"[:100],
                "description": "First-party segment: " + ", ".join(signals[:5]),
                # A rule-based website audience: the definition is uploaded,
                # never the members.
                "subtype": "WEBSITE",
                "retention_days": 180,
                "rule": {
                    "inclusions": {
                        "operator": "or",
                        "rules": [
                            {
                                "event_sources": [
                                    {"id": self.credentials.get("pixelId", ""), "type": "pixel"}
                                ],
                                "retention_seconds": 15_552_000,
                                "filter": {
                                    "operator": "or",
                                    "filters": [
                                        {
                                            "field": "event",
                                            "operator": "eq",
                                            "value": signal,
                                        }
                                        for signal in signals[:5]
                                    ],
                                },
                            }
                        ],
                    }
                },
            },
        )
        return str((response or {}).get("id") or "")

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        data = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "fields": "clicks,impressions,inline_link_clicks",
                "breakdowns": "publisher_platform,platform_position",
                "date_preset": "last_7d",
                "limit": limit,
            },
        )
        out: list[TrafficSample] = []
        for row in (data or {}).get("data") or []:
            platform = row.get("publisher_platform") or "unknown"
            position = row.get("platform_position") or "feed"
            clicks = int(row.get("clicks") or 0)
            link_clicks = int(row.get("inline_link_clicks") or 0)
            # Clicks that never became link clicks are the signal Meta gives
            # for accidental or non-human interaction.
            wasted = max(0, clicks - link_clicks)
            out.append(
                TrafficSample(
                    source=f"{platform}:{position}",
                    sessions=clicks,
                    bounce_rate=round(wasted / clicks, 3) if clicks else 0.0,
                    avg_session_seconds=0.0,
                    channel=self.channel,
                    signals={"inline_link_clicks": link_clicks},
                )
            )
        return out

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        ad_set_id = self.credentials.get("adSetId")
        if not ad_set_id:
            raise ConnectorError("Set adSetId on the Meta Ads connector to exclude placements")
        platform, _, position = source.partition(":")
        self.request(
            "POST",
            f"/{ad_set_id}",
            json_body={
                "targeting": {
                    "excluded_publisher_categories": [platform],
                    "excluded_publisher_list_ids": [],
                }
            },
        )
        log.info("Excluded %s on Meta Ads (%s)", source, reason)
        return True


CONNECTOR_CLASS = MetaAdsConnector
