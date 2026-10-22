"""TikTok Ads connector — Business API v1.3."""
from __future__ import annotations

import json
from datetime import date, timedelta

from app.connectors.base import verify_helpers as vh
from app.connectors.base.ads_base import BaseAdsConnector
from app.connectors.base.connector import Capability, ConnectorSpec, OAuthBinding, OAuthChoice
from app.connectors.base.credentials import monthly_budget, text
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: The integrated report's page-size ceiling.
_MAX_PAGE_SIZE = 1000

LIVE_URL = "https://business-api.tiktok.com/open_api/v1.3"
#: Every developer app's sandbox ad account is served from its own host
#: (Developer Portal -> My Apps -> the app -> Sandbox Ad Account). The paths
#: are the same; the sandbox's advertiser id and access token are issued on
#: that page, not through the sign-in, so the sandbox has no sign-in or
#: token address of its own here. TikTok's public docs could not confirm
#: this host (business-api.tiktok.com/portal/docs is not publicly readable),
#: so it is the default of the TIKTOK_SANDBOX_BASE_URL setting.
SANDBOX_URL = "https://sandbox-ads.tiktok.com/open_api/v1.3"
#: Added to a failure in the sandbox, where the address itself may be wrong.
SANDBOX_ADDRESS_HINT = (
    "If TikTok rejects the sandbox address, set TIKTOK_SANDBOX_BASE_URL to the "
    "address shown in your TikTok developer portal."
)


def sandbox_url() -> str:
    """The sandbox address: the TIKTOK_SANDBOX_BASE_URL setting, else the default."""
    from app.core.config import settings

    raw = str(getattr(settings, "tiktok_sandbox_base_url", "") or "").strip().rstrip("/")
    if not raw:
        return SANDBOX_URL
    if not raw.startswith("https://"):
        raise ConnectorConfigError(
            "TIKTOK_SANDBOX_BASE_URL must be an https:// address, like "
            f"{SANDBOX_URL}."
        )
    return raw


def _date_window(days: int) -> tuple[str, str]:
    """``start_date``/``end_date`` (YYYY-MM-DD, inclusive) covering ``days``."""
    end = date.today()
    start = end - timedelta(days=max(days, 1) - 1)
    return start.isoformat(), end.isoformat()


def parse_advertisers(rows: list[dict]) -> list[OAuthChoice]:
    """Advertisers out of ``oauth2/advertiser/get`` (``data.list``)."""
    out: list[OAuthChoice] = []
    for row in rows:
        advertiser = str(row.get("advertiser_id") or "")
        if not advertiser:
            continue
        out.append(
            OAuthChoice(
                id=advertiser,
                label=str(row.get("advertiser_name") or f"Advertiser {advertiser}"),
                detail=f"Advertiser ID {advertiser}",
                values={"advertiserId": advertiser},
            )
        )
    return out


class TikTokAdsConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, BaseAdsConnector):
    channel = "tiktok"
    environments = (vh.SANDBOX,)

    spec = ConnectorSpec(
        slug="tiktok_ads",
        name="TikTok Ads",
        category="Ad Platforms",
        description="In-feed video campaigns: reporting, budgets, creatives, placement quality.",
        fields=(
            text(
                "advertiserId",
                "Advertiser ID",
                "7012345678901234567",
                help_text="TikTok Ads Manager → Account info shows this as Advertiser ID.",
            ),
            text(
                "campaignId",
                "Campaign ID",
                "1780000000000000",
                required=False,
                help_text=(
                    "The campaign whose daily budget the budget engine sets. Leave "
                    "blank to keep budgets manual."
                ),
            ),
            text(
                "adgroupId",
                "Ad group ID",
                "1780000000000001",
                required=False,
                help_text="The ad group uploaded creatives are added to (as paused ads).",
            ),
            text(
                "videoId",
                "Video ID",
                "v10033g50000xxxxxxxx",
                required=False,
                help_text=(
                    "A video already in the account's Creative library. TikTok ads "
                    "are video ads; uploaded copy is placed around this video."
                ),
            ),
            text(
                "coverImageId",
                "Cover image ID",
                "ad-site-i18n-sg/2024xxxxxxxx",
                required=False,
                help_text="The image id of the video's cover, from the Creative library.",
            ),
            text(
                "identityId",
                "Identity ID",
                "7012345678901234567",
                required=False,
                help_text=(
                    "The identity (display name and avatar) ads are shown as. From "
                    "Ads Manager → Assets → Identities."
                ),
            ),
            text(
                "identityType",
                "Identity type",
                "CUSTOMIZED_USER",
                required=False,
                default="CUSTOMIZED_USER",
                help_text=(
                    "CUSTOMIZED_USER for a custom identity, or TT_USER / BC_AUTH_TT "
                    "for an authorised TikTok account."
                ),
            ),
            monthly_budget(),
            vh.environment_field(
                allowed=(vh.SANDBOX,),
                help_text=(
                    "Leave blank for a real ad account. Type Sandbox for the sandbox ad "
                    "account TikTok gives every developer app (My Apps, then your app, "
                    "then Sandbox Ad Account), and use that page's Advertiser ID and "
                    "generated access token. Sandbox ads never run and nothing is billed."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with TikTok" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='TikTok',
                docs='The long-term access token from the TikTok Business API.',
            ),
        ),
        # Removed:
        # * BLOCK_PLACEMENT — what was called a placement block switched off
        #   the whole ad. TikTok's placements are chosen per ad group (TikTok,
        #   Pangle, …); there is no per-publisher exclusion to push.
        # * PUSH_AUDIENCE — the rule-audience body was invented. TikTok's
        #   rule-based audiences are built from its own engagement/pixel
        #   events, not from our signal names, and customer-file audiences
        #   need member identifiers this platform never sends.
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.UPLOAD_CREATIVE,
                Capability.READ_TRAFFIC_QUALITY,
            }
        ),
        requirements=(
            "A TikTok For Business developer app with a long-term access token "
            "for this advertiser.",
            "Uploading creatives needs the Ad group ID, a Video ID and its Cover "
            "image ID from the Creative library, and an Identity ID.",
        ),
        docs_url="https://business-api.tiktok.com/portal/docs",
        base_url=LIVE_URL,
        # TikTok sets permissions on the developer app, not per sign-in:
        # Ad Account Management, Reporting, Ads Management and Creative
        # Management (docs/oauth-setup-social-ads.md). The long-term token
        # does not expire until the advertiser removes the authorization.
        oauth=OAuthBinding(
            provider="tiktok",
            scopes=(),
            platform_fields=frozenset(),
            access_summary=(
                "Access to the TikTok ad accounts you allow, so agents can read results "
                "and prepare budget and ad changes. Changes wait for your approval "
                "unless you turn that off."
            ),
            choice_noun="ad account",
            choice_fields=("advertiserId",),
            token_fields=frozenset({"accessToken"}),
        ),
    )

    @property
    def base_url(self) -> str:
        if self.account_environment() == vh.SANDBOX:
            return sandbox_url()
        return super().base_url

    def diagnose(self, error: Exception) -> str | None:
        """In the sandbox, a failure may mean the sandbox address is wrong."""
        if self.account_environment() != vh.SANDBOX or SANDBOX_ADDRESS_HINT in str(error):
            return None
        from app.core.user_messages import public_error_message

        detail = public_error_message(error, fallback="TikTok's sandbox refused the request.")
        return f"{detail.rstrip()} {SANDBOX_ADDRESS_HINT}"

    def auth_headers(self) -> dict[str, str]:
        return {
            # TikTok uses its own header rather than Bearer auth.
            "Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The advertisers this sign-in authorised. The call is made with the
        platform's own app id and secret, never anything of the customer's."""
        from app.connectors.base import oauth_providers

        provider = oauth_providers.TIKTOK
        data = self._unwrap(
            self.request(
                "GET",
                "/oauth2/advertiser/get/",
                params={"app_id": provider.client_id, "secret": provider.client_secret},
            )
        )
        return parse_advertisers([r for r in data.get("list") or [] if isinstance(r, dict)])

    @property
    def advertiser_id(self) -> str:
        return self.credentials.require("advertiserId")

    def _setting(self, key: str, label: str, purpose: str) -> str:
        value = (self.credentials.get(key) or "").strip()
        if not value:
            raise ConnectorConfigError(
                f"Enter the {label} on the TikTok Ads integration {purpose}."
            )
        return value

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
        start, end = _date_window(days)
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
                    # Without a lifetime query the report needs a window.
                    "query_lifetime": "false",
                    "start_date": start,
                    "end_date": end,
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
        campaign_id = self._setting(
            "campaignId", "Campaign ID", "to let the budget engine set its budget"
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
    @staticmethod
    def _cta_type(label: str) -> str:
        """Map free-text CTAs onto TikTok's call_to_action enum."""
        lowered = label.lower()
        for needles, enum in (
            (("book", "appointment", "test drive", "schedule", "reserve"), "BOOK_NOW"),
            (("quote",), "GET_QUOTE"),
            (("call", "contact"), "CONTACT_US"),
            (("shop", "buy", "offer"), "SHOP_NOW"),
            (("sign up", "register"), "SIGN_UP"),
            (("apply",), "APPLY_NOW"),
            (("download", "brochure"), "DOWNLOAD_NOW"),
        ):
            if any(n in lowered for n in needles):
                return enum
        return "LEARN_MORE"

    def _live_upload_creative(
        self,
        *,
        headline: str,
        body_copy: str,
        call_to_action: str,
        dimensions: str,
        audience_segment: str,
        final_url: str = "",
        extra_headlines: tuple[str, ...] = (),
        extra_descriptions: tuple[str, ...] = (),
    ) -> str:
        purpose = "to publish creatives"
        adgroup_id = self._setting("adgroupId", "Ad group ID", purpose)
        video_id = self._setting("videoId", "Video ID", purpose)
        cover_id = self._setting("coverImageId", "Cover image ID", purpose)
        identity_id = self._setting("identityId", "Identity ID", purpose)
        identity_type = (self.credentials.get("identityType") or "CUSTOMIZED_USER").strip()
        if not final_url:
            raise ConnectorConfigError(
                "A TikTok ad needs a landing page, and none was given for this creative."
            )
        data = self._unwrap(
            self.request(
                "POST",
                "/ad/create/",
                json_body={
                    "advertiser_id": self.advertiser_id,
                    "adgroup_id": adgroup_id,
                    # Created paused; nothing serves until somebody enables it.
                    "operation_status": "DISABLE",
                    "creatives": [
                        {
                            "ad_name": f"Synaprank — {headline[:40]}",
                            "identity_id": identity_id,
                            "identity_type": identity_type,
                            "ad_format": "SINGLE_VIDEO",
                            "video_id": video_id,
                            "image_ids": [cover_id],
                            "ad_text": body_copy[:100],
                            "call_to_action": self._cta_type(call_to_action),
                            "landing_page_url": final_url,
                        }
                    ],
                },
            )
        )
        ids = data.get("ad_ids") or []
        return str(ids[0]) if ids else ""

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        start, end = _date_window(7)
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
                    "start_date": start,
                    "end_date": end,
                    "page_size": max(1, min(limit, _MAX_PAGE_SIZE)),
                },
            )
        )
        out: list[TrafficSample] = []
        for row in (data.get("list") or [])[:limit]:
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
                    bounce_rate=round(max(0.0, 1 - (watched / clicks)), 3) if clicks else 0.0,
                    avg_session_seconds=0.0,
                    channel=self.channel,
                    signals={"video_watched_2s": watched},
                )
            )
        return out

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read the ad account and today's report; in the sandbox ad account,
        create a campaign switched off, read it back and delete it.

        ``read_performance`` is the call the budget and click-fraud agents
        make.
        """

        def advertiser() -> tuple[str, dict]:
            data = self._unwrap(
                self.request(
                    "GET",
                    "/advertiser/info/",
                    params={"advertiser_ids": json.dumps([self.advertiser_id])},
                )
            )
            rows = [r for r in data.get("list") or [] if isinstance(r, dict)]
            if not rows:
                raise ConnectorError(
                    "TikTok answered, but did not return the ad account with this Advertiser ID."
                )
            row = rows[0]
            return (
                "Read the ad account's details.",
                {
                    "advertiser_id": str(row.get("advertiser_id") or self.advertiser_id),
                    "status": str(row.get("status") or ""),
                    "currency": str(row.get("currency") or ""),
                    "environment": self.account_environment(),
                },
            )

        def performance() -> tuple[str, dict]:
            result = self.read_performance(days=1)
            return (
                "Ran today's account report and read it back.",
                {
                    "impressions": result.impressions,
                    "clicks": result.clicks,
                    "conversions": result.conversions,
                },
            )

        def campaign_roundtrip() -> tuple[str, dict]:
            vh.refuse_unless_test(self, "it creates a campaign and deletes it again.")
            if self.account_environment() != vh.SANDBOX:
                raise ConnectorConfigError(
                    "This check only runs in the TikTok sandbox ad account, where ads "
                    "never run. Set 'Test account' to Sandbox first."
                )
            advertiser_id = self.advertiser_id

            def create() -> str:
                data = self._unwrap(
                    self.request(
                        "POST",
                        "/campaign/create/",
                        json_body={
                            "advertiser_id": advertiser_id,
                            "campaign_name": vh.marker("campaign"),
                            "objective_type": "TRAFFIC",
                            # No budget of its own, switched off, and with no
                            # ad groups or ads: nothing can serve or spend.
                            "budget_mode": "BUDGET_MODE_INFINITE",
                            "operation_status": "DISABLE",
                        },
                    )
                )
                return str(data.get("campaign_id") or "")

            def check(campaign_id: str) -> None:
                data = self._unwrap(
                    self.request(
                        "GET",
                        "/campaign/get/",
                        params={
                            "advertiser_id": advertiser_id,
                            "filtering": json.dumps({"campaign_ids": [campaign_id]}),
                        },
                    )
                )
                rows = [r for r in data.get("list") or [] if isinstance(r, dict)]
                if not rows:
                    raise ConnectorError("TikTok did not return the new campaign.")
                if rows[0].get("operation_status") != "DISABLE":
                    raise ConnectorError(
                        f"The campaign came back as {rows[0].get('operation_status')!r}, "
                        "not switched off."
                    )

            def delete(campaign_id: str) -> None:
                self._unwrap(
                    self.request(
                        "POST",
                        "/campaign/status/update/",
                        json_body={
                            "advertiser_id": advertiser_id,
                            "campaign_ids": [campaign_id],
                            "operation_status": "DELETE",
                        },
                    )
                )

            evidence = vh.create_check_delete(
                what="campaign", create=create, check=check, delete=delete
            )
            return (
                "Created a switched-off campaign with no budget in the sandbox ad "
                "account, read it back, and deleted it.",
                {**evidence, "operation_status": "DISABLE"},
            )

        return [
            vh.read_step("advertiser_info", "Read the ad account's details", advertiser),
            vh.read_step("performance", "Run today's account report", performance),
            vh.write_step(
                "campaign_roundtrip",
                "Create a switched-off campaign in the sandbox, read it, then delete it",
                campaign_roundtrip,
                requires_test_account=True,
            ),
        ]


CONNECTOR_CLASS = TikTokAdsConnector
