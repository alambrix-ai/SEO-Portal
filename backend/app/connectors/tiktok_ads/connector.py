"""TikTok Ads connector — Business API v1.3."""
from __future__ import annotations

from app.connectors.base.ads_base import FULL_AD_CAPABILITIES, BaseAdsConnector
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import monthly_budget, oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)


class TikTokAdsConnector(OAuthTokenMixin, BaseAdsConnector):
    channel = "tiktok"

    spec = ConnectorSpec(
        slug="tiktok_ads",
        name="TikTok Ads",
        category="Ad Platforms",
        description="In-feed video campaigns: reporting, budgets, creatives, audiences.",
        fields=(
            text(
                "advertiserId",
                "Advertiser ID",
                "7012345678901234567",
                help_text="TikTok Ads Manager → Account info shows this as Advertiser ID.",
            ),
            monthly_budget(),
            # The credential this connector has always required. It used
            # to be an "Authorize with TikTok" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='TikTok',
                docs='The long-term access token from the TikTok Business API.',
            ),
        ),
        capabilities=FULL_AD_CAPABILITIES,
        docs_url="https://business-api.tiktok.com/portal/docs",
        base_url="https://business-api.tiktok.com/open_api/v1.3",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            # TikTok uses its own header rather than Bearer auth.
            "Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    @property
    def advertiser_id(self) -> str:
        return self.credentials.require("advertiserId")

    def _unwrap(self, response: object) -> dict:
        """TikTok wraps everything in ``{code, message, data}``."""
        payload = response if isinstance(response, dict) else {}
        if payload.get("code") not in (0, None):
            raise ConnectorError(
                f"TikTok Ads error {payload.get('code')}: {payload.get('message')}"
            )
        return payload.get("data") or {}

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        data = self._unwrap(
            self.request(
                "GET",
                "/report/integrated/get/",
                params={
                    "advertiser_id": self.advertiser_id,
                    "report_type": "BASIC",
                    "data_level": "AUCTION_ADVERTISER",
                    "dimensions": '["advertiser_id"]',
                    "metrics": '["spend","impressions","clicks","conversion"]',
                    "query_lifetime": "false",
                    "page_size": 1,
                },
            )
        )
        rows = data.get("list") or []
        if not rows:
            return ChannelPerformance(channel=self.channel)
        metrics = rows[0].get("metrics") or {}
        return ChannelPerformance(
            channel=self.channel,
            spend=round(float(metrics.get("spend") or 0), 2),
            impressions=int(float(metrics.get("impressions") or 0)),
            clicks=int(float(metrics.get("clicks") or 0)),
            conversions=int(float(metrics.get("conversion") or 0)),
        )

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = self.credentials.get("campaignId")
        if not campaign_id:
            raise ConnectorError(
                "Set campaignId on the TikTok Ads connector to let the budget "
                "engine write to it"
            )
        self._unwrap(
            self.request(
                "POST",
                "/campaign/update/",
                json_body={
                    "advertiser_id": self.advertiser_id,
                    "campaign_id": campaign_id,
                    "budget": round(daily_budget, 2),
                    "budget_mode": "BUDGET_MODE_DAY",
                },
            )
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
        adgroup_id = self.credentials.get("adgroupId")
        if not adgroup_id:
            raise ConnectorError("Set adgroupId on the TikTok Ads connector to publish creatives")
        data = self._unwrap(
            self.request(
                "POST",
                "/ad/create/",
                json_body={
                    "advertiser_id": self.advertiser_id,
                    "adgroup_id": adgroup_id,
                    "creatives": [
                        {
                            "ad_name": f"AutoMarket — {headline[:40]}",
                            "ad_text": body_copy[:100],
                            "call_to_action": call_to_action[:24],
                            "ad_format": "SINGLE_VIDEO",
                            # Video assets come from the customer's library;
                            # this creates the copy shell around one.
                            "video_id": self.credentials.get("videoId", ""),
                            "operation_status": "DISABLE",
                        }
                    ],
                },
            )
        )
        ids = data.get("ad_ids") or []
        return str(ids[0]) if ids else ""

    # ── Audiences ──────────────────────────────────────────────────────────
    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        data = self._unwrap(
            self.request(
                "POST",
                "/dmp/custom_audience/rule/create/",
                json_body={
                    "advertiser_id": self.advertiser_id,
                    "custom_audience_name": f"AutoMarket — {label}"[:100],
                    # Rule-based: the definition travels, the members do not.
                    "audience_rule": {
                        "engagement_type": "IMPRESSION",
                        "retention_in_days": 180,
                        "signals": signals[:5],
                    },
                },
            )
        )
        return str(data.get("custom_audience_id") or "")

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        data = self._unwrap(
            self.request(
                "GET",
                "/report/integrated/get/",
                params={
                    "advertiser_id": self.advertiser_id,
                    "report_type": "AUDIENCE",
                    "data_level": "AUCTION_AD",
                    "dimensions": '["ad_id","placement"]',
                    "metrics": '["clicks","impressions","video_watched_2s"]',
                    "page_size": limit,
                },
            )
        )
        out: list[TrafficSample] = []
        for row in data.get("list") or []:
            dims = row.get("dimensions") or {}
            metrics = row.get("metrics") or {}
            clicks = int(float(metrics.get("clicks") or 0))
            watched = int(float(metrics.get("video_watched_2s") or 0))
            out.append(
                TrafficSample(
                    source=f"{dims.get('placement') or 'tiktok'}:{dims.get('ad_id') or ''}",
                    sessions=clicks,
                    # No two-second view after a click is TikTok's clearest
                    # signal of an interaction that never really happened.
                    bounce_rate=round(1 - (watched / clicks), 3) if clicks else 0.0,
                    avg_session_seconds=0.0,
                    channel=self.channel,
                    signals={"video_watched_2s": watched},
                )
            )
        return out

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        adgroup_id = self.credentials.get("adgroupId")
        if not adgroup_id:
            raise ConnectorError("Set adgroupId on the TikTok Ads connector to block placements")
        _, _, ad_id = source.partition(":")
        if not ad_id:
            return False
        self._unwrap(
            self.request(
                "POST",
                "/ad/status/update/",
                json_body={
                    "advertiser_id": self.advertiser_id,
                    "ad_ids": [ad_id],
                    "operation_status": "DISABLE",
                },
            )
        )
        log.info("Disabled TikTok ad %s (%s)", ad_id, reason)
        return True


CONNECTOR_CLASS = TikTokAdsConnector
