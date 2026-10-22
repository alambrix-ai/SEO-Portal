"""Meta Ads connector — Marketing API (Facebook and Instagram placements)."""
from __future__ import annotations

from datetime import date, timedelta

from app.connectors.base import verify_helpers as vh
from app.connectors.base.ads_base import BaseAdsConnector
from app.connectors.base.connector import Capability, ConnectorSpec, OAuthBinding, OAuthChoice
from app.connectors.base.credentials import monthly_budget, text
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.connectors.base.meta_graph import GRAPH_API_VERSION
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: The shared Graph API version (app/connectors/base/meta_graph.py).
API_VERSION = GRAPH_API_VERSION

#: Pages of placement insights followed before stopping, whatever the limit.
_MAX_PAGES = 20

#: Our first-party signal vocabulary mapped onto Meta's *standard* pixel
#: events. A website custom audience rule matches events the pixel actually
#: fires, so only signals with a standard-event equivalent can be expressed;
#: the rest are dropped rather than invented as custom event names.
_SIGNAL_TO_PIXEL_EVENT = {
    "inventory_view": "ViewContent",
    "inventory_depth": "ViewContent",
    "comparison_tool": "ViewContent",
    "finance_calculator": "CustomizeProduct",
    "service_booking": "Schedule",
    "trade_in_valuation": "SubmitApplication",
    "location_view": "FindLocation",
    "contact_form": "Contact",
    "call_click": "Contact",
    "chat_started": "Contact",
    "brochure_download": "Lead",
    "cart_started": "AddToCart",
    "checkout_started": "InitiateCheckout",
    "purchase": "Purchase",
}
#: Meta caps website-audience retention at 180 days.
_RETENTION_SECONDS = 180 * 24 * 3600

#: AdAccount.account_status codes (Marketing API reference). Closed accounts
#: are not offered; the others are, with their state named.
_ACCOUNT_STATUS = {
    1: "",
    2: "Disabled",
    3: "Unsettled",
    7: "Pending risk review",
    8: "Pending settlement",
    9: "In grace period",
    100: "Pending closure",
}
_CLOSED_STATUSES = {101, 202}
#: Pages of /me/adaccounts followed during the choose step.
_MAX_LIST_PAGES = 10


def parse_ad_accounts(rows: list[dict]) -> list[OAuthChoice]:
    """Ad accounts out of ``GET /me/adaccounts`` rows."""
    out: list[OAuthChoice] = []
    for row in rows:
        act = str(row.get("id") or "")
        if not act.startswith("act_"):
            continue
        try:
            status = int(row.get("account_status") or 1)
        except (TypeError, ValueError):
            status = 1
        if status in _CLOSED_STATUSES:
            continue
        number = str(row.get("account_id") or act[4:])
        parts = [f"Account ID {number}"]
        if row.get("currency"):
            parts.append(str(row["currency"]))
        state = _ACCOUNT_STATUS.get(status, f"Status {status}")
        if state:
            parts.append(state)
        out.append(
            OAuthChoice(
                id=act,
                label=str(row.get("name") or act),
                detail=" · ".join(parts),
                values={"adAccountId": act},
            )
        )
    return out


class MetaAdsConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, BaseAdsConnector):
    channel = "meta"
    environments = (vh.SANDBOX,)

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
            text(
                "pageId",
                "Page ID",
                "1234567890",
                required=False,
                help_text="The Facebook Page ads are published as. Needed to upload creatives.",
            ),
            text(
                "campaignId",
                "Campaign ID",
                "120200000000000000",
                required=False,
                help_text=(
                    "The campaign whose daily budget the budget engine sets. It "
                    "must use campaign budget (Advantage+ campaign budget). Leave "
                    "blank to keep budgets manual."
                ),
            ),
            text(
                "adSetId",
                "Ad set ID",
                "120200000000000001",
                required=False,
                help_text=(
                    "Optional. When set, each uploaded creative is also added to "
                    "this ad set as a paused ad, ready to switch on in Ads Manager."
                ),
            ),
            text(
                "pixelId",
                "Pixel (dataset) ID",
                "1234567890",
                required=False,
                help_text=(
                    "From Events Manager. Needed to build website custom "
                    "audiences from your segments."
                ),
            ),
            text(
                "finalUrl",
                "Default landing page URL",
                "https://www.example.com/",
                required=False,
                help_text=(
                    "Where ads link when a creative is not made for a specific "
                    "page. A creative made for a page always links to that page."
                ),
            ),
            monthly_budget(),
            vh.environment_field(
                allowed=(vh.SANDBOX,),
                help_text=(
                    "Choose Sandbox only for a Meta sandbox ad account, made in your "
                    "app dashboard under Marketing API > Tools. It uses the same "
                    "addresses as a real account but never delivers ads or spends "
                    "money. Leave blank for a real account."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Meta" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='Meta',
                docs='A system user access token from Business Manager.',
            ),
        ),
        # BLOCK_PLACEMENT is deliberately absent. What the fraud agent wants
        # to block is a platform position ("audience_network:an_classic"),
        # and the only per-publisher tool Meta offers — publisher block
        # lists — takes Audience Network publisher URLs, which the insights
        # breakdown never supplies. Rewriting an ad set's placement
        # targeting instead would mean mapping between two different
        # position vocabularies and replacing the whole targeting spec; a
        # wrong guess there changes who the ads reach, so it is not done.
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.UPLOAD_CREATIVE,
                Capability.PUSH_AUDIENCE,
                Capability.READ_TRAFFIC_QUALITY,
            }
        ),
        requirements=(
            "A system user access token with ads_management and ads_read.",
            "Uploading creatives needs the Page ID; audiences need the Pixel ID; "
            "budget writes need a Campaign ID that uses campaign budget.",
        ),
        docs_url="https://developers.facebook.com/docs/marketing-apis",
        base_url=f"https://graph.facebook.com/{API_VERSION}",
        # ads_read for insights; ads_management for budgets, creatives, ads
        # and custom audiences. Meta lists pages_show_list and
        # pages_read_engagement as dependencies of ads_management.
        oauth=OAuthBinding(
            provider="meta",
            scopes=("ads_read", "ads_management", "pages_show_list", "pages_read_engagement"),
            platform_fields=frozenset(),
            access_summary=(
                "Access to the Meta ad account you choose, so agents can read results "
                "and prepare budget, creative and audience changes. Changes wait for "
                "your approval unless you turn that off."
            ),
            choice_noun="ad account",
            choice_fields=("adAccountId",),
            token_fields=frozenset({"accessToken"}),
        ),
    )

    # Facebook Login issues no refresh token: the long-lived user token
    # (about 60 days) is used as stored, and the mixin says "Sign in again"
    # once it has run out.

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

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        rows: list[dict] = []
        data = self.request(
            "GET",
            "/me/adaccounts",
            params={"fields": "id,account_id,name,account_status,currency", "limit": 100},
        )
        for _ in range(_MAX_LIST_PAGES):
            rows.extend(r for r in (data or {}).get("data") or [] if isinstance(r, dict))
            next_url = ((data or {}).get("paging") or {}).get("next")
            if not next_url:
                break
            data = self.request("GET", next_url)
        return parse_ad_accounts(rows)

    @staticmethod
    def _time_range(days: int) -> str:
        """The exact window asked for, ending yesterday (today is partial)."""
        until = date.today() - timedelta(days=1)
        since = until - timedelta(days=max(days, 1) - 1)
        return f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}'

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        data = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "fields": "spend,impressions,clicks,actions",
                "time_range": self._time_range(days),
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

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = (self.credentials.get("campaignId") or "").strip()
        if not campaign_id:
            raise ConnectorConfigError(
                "Enter the Campaign ID on the Meta Ads integration to let the "
                "budget engine set its budget."
            )
        # Meta takes budgets in minor units (cents).
        self.request(
            "POST",
            f"/{campaign_id}",
            json_body={"daily_budget": int(round(daily_budget * 100))},
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
        final_url: str = "",
        extra_headlines: tuple[str, ...] = (),
        extra_descriptions: tuple[str, ...] = (),
    ) -> str:
        page_id = (self.credentials.get("pageId") or "").strip()
        if not page_id:
            raise ConnectorConfigError(
                "Enter the Page ID on the Meta Ads integration to upload creatives."
            )
        # The page the creative was written for beats the configured default.
        link = (final_url or self.credentials.get("finalUrl") or "").strip()
        if not link:
            raise ConnectorConfigError(
                "Enter the Default landing page URL on the Meta Ads integration — "
                "a link ad has to point somewhere."
            )

        body = {
            "name": f"Synaprank — {headline[:40]} ({dimensions})",
            # From v26.0 a creative defaults to WEBSITE_AND_SHOP when the
            # advertiser has a Meta shop, which can send people to the
            # shop instead of the page this ad was written for. Opting
            # out keeps the link going to that page, as before.
            # https://developers.facebook.com/docs/graph-api/changelog/version26.0
            "destination_spec": {"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"},
            "object_story_spec": {
                "page_id": page_id,
                "link_data": {
                    "message": body_copy,
                    "name": headline,
                    "link": link,
                    "call_to_action": {
                        "type": self._cta_type(call_to_action),
                        "value": {"link": link},
                    },
                },
            },
        }
        try:
            response = self.request("POST", f"/{self.account_id}/adcreatives", json_body=body)
        except ConnectorError as exc:
            # The opt-out shape comes from Meta's changelog and has not been
            # sent to Meta yet. If Meta refuses that one parameter (on this
            # API version or account), create the creative without it rather
            # than failing the upload; without it the pre-v26 behaviour applies.
            if not _rejected_destination_spec(exc):
                raise
            log.warning("Meta refused destination_spec on a creative; retrying without it")
            body.pop("destination_spec", None)
            response = self.request("POST", f"/{self.account_id}/adcreatives", json_body=body)
        creative_id = str((response or {}).get("id") or "")

        ad_set_id = (self.credentials.get("adSetId") or "").strip()
        if creative_id and ad_set_id:
            # Paused, so nothing serves until somebody switches it on.
            ad = self.request(
                "POST",
                f"/{self.account_id}/ads",
                json_body={
                    "name": f"Synaprank — {headline[:40]}",
                    "adset_id": ad_set_id,
                    "creative": {"creative_id": creative_id},
                    "status": "PAUSED",
                },
            )
            log.info(
                "Meta creative %s added to ad set %s as paused ad %s",
                creative_id,
                ad_set_id,
                (ad or {}).get("id"),
            )
        return creative_id

    @staticmethod
    def _cta_type(label: str) -> str:
        """Map free-text CTAs onto Meta's fixed call-to-action enum.

        BOOK_TRAVEL is for travel inventory, not appointments: a test drive or
        a service slot is BOOK_NOW. CALL_NOW needs a phone number rather than
        a link, so on a link ad a "call" CTA is sent as CONTACT_US.
        """
        lowered = label.lower()
        for needles, enum in (
            (("book", "appointment", "test drive", "schedule", "reserve"), "BOOK_NOW"),
            (("quote",), "GET_QUOTE"),
            (("call", "contact"), "CONTACT_US"),
            (("shop", "buy", "offer"), "SHOP_NOW"),
            (("sign up", "register"), "SIGN_UP"),
            (("subscribe",), "SUBSCRIBE"),
            (("apply",), "APPLY_NOW"),
            (("download", "brochure"), "DOWNLOAD"),
        ):
            if any(n in lowered for n in needles):
                return enum
        return "LEARN_MORE"

    # ── Audiences ──────────────────────────────────────────────────────────
    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        pixel_id = (self.credentials.get("pixelId") or "").strip()
        if not pixel_id:
            raise ConnectorConfigError(
                "Enter the Pixel ID on the Meta Ads integration to create audiences."
            )
        events = list(
            dict.fromkeys(
                _SIGNAL_TO_PIXEL_EVENT[s] for s in signals if s in _SIGNAL_TO_PIXEL_EVENT
            )
        )
        if not events:
            raise ConnectorError(
                "None of this segment's signals ("
                + (", ".join(signals[:5]) or "none")
                + ") match a standard Meta pixel event, so Meta cannot build "
                "an audience from it."
            )
        # A website custom audience: the rule is uploaded, never the members.
        # Shape per Meta's website custom audience rule reference.
        response = self.request(
            "POST",
            f"/{self.account_id}/customaudiences",
            json_body={
                "name": f"Synaprank — {label}"[:100],
                "description": "First-party segment: " + ", ".join(signals[:5]),
                "prefill": True,
                "rule": {
                    "inclusions": {
                        "operator": "or",
                        "rules": [
                            {
                                "event_sources": [{"id": pixel_id, "type": "pixel"}],
                                "retention_seconds": _RETENTION_SECONDS,
                                "filter": {
                                    "operator": "or",
                                    "filters": [
                                        {"field": "event", "operator": "eq", "value": event}
                                        for event in events
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
        out: list[TrafficSample] = []
        data = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "fields": "clicks,impressions,inline_link_clicks",
                "breakdowns": "publisher_platform,platform_position",
                "time_range": self._time_range(7),
                "limit": min(max(limit, 1), 500),
            },
        )
        pages = 0
        while True:
            for row in (data or {}).get("data") or []:
                if len(out) >= limit:
                    return out
                out.append(self._to_sample(row))
            next_url = ((data or {}).get("paging") or {}).get("next")
            pages += 1
            if not next_url or len(out) >= limit or pages >= _MAX_PAGES:
                return out
            # `next` is a complete URL carrying the cursor and the query.
            data = self.request("GET", next_url)

    # ── Live checks ────────────────────────────────────────────────────────
    def diagnose(self, error: Exception) -> str | None:
        """A retired Graph API version gets a fix-it message."""
        from app.connectors.base.meta_graph import version_problem

        return version_problem(error, API_VERSION)

    def verify_steps(self) -> list:
        """Read what the agents read; in a sandbox ad account, make and delete
        a paused campaign.

        Meta's sandbox ad accounts have no insights, so there the two
        reporting reads accept Meta's refusal and say so instead of failing.
        """

        def accounts() -> tuple[str, dict]:
            choices = self.list_oauth_choices()
            return (
                f"This sign-in reaches {vh.plural(len(choices), 'ad account')}.",
                {"count": len(choices), "ids": [c.id for c in choices][:20]},
            )

        def details() -> tuple[str, dict]:
            row = self.request(
                "GET", f"/{self.account_id}", params={"fields": "id,account_status,currency"}
            ) or {}
            return (
                f"Read the chosen ad account ({row.get('currency') or 'unknown currency'}).",
                {
                    "ad_account_id": str(row.get("id") or self.account_id),
                    "account_status": row.get("account_status"),
                    "currency": row.get("currency") or "",
                },
            )

        def reporting(read, describe):  # noqa: ANN001, ANN202
            def run() -> tuple[str, dict]:
                try:
                    return describe(read())
                except ConnectorError:
                    if self.account_environment() != vh.SANDBOX:
                        raise
                    return (
                        "Meta sandbox ad accounts have no insights, so this report was "
                        "refused as expected.",
                        {"sandbox": True},
                    )

            return run

        performance = reporting(
            lambda: self.read_performance(days=1),
            lambda r: (
                f"Read yesterday's results: {r.impressions} impressions, {r.clicks} clicks.",
                {"impressions": r.impressions, "clicks": r.clicks, "spend": r.spend},
            ),
        )
        traffic = reporting(
            lambda: self.sample_traffic(limit=5),
            lambda samples: (
                f"Read the placement breakdown ({vh.plural(len(samples), 'placement')}).",
                {"count": len(samples)},
            ),
        )

        def campaign() -> tuple[str, dict]:
            vh.refuse_unless_test(self, "it creates a campaign.")

            def create() -> str:
                response = self.request(
                    "POST",
                    f"/{self.account_id}/campaigns",
                    json_body={
                        "name": vh.marker("campaign"),
                        "objective": "OUTCOME_TRAFFIC",
                        "status": "PAUSED",
                        "special_ad_categories": [],
                        # Required from v24.0 when the budget is set on the ad
                        # sets rather than the campaign, as here (no budget).
                        # https://developers.facebook.com/docs/graph-api/changelog/version24.0
                        "is_adset_budget_sharing_enabled": False,
                    },
                ) or {}
                return str(response.get("id") or "")

            def check(campaign_id: str) -> None:
                row = self.request(
                    "GET", f"/{campaign_id}", params={"fields": "id,status,objective"}
                ) or {}
                if row.get("status") != "PAUSED":
                    raise ConnectorError(f"The campaign came back as {row.get('status')!r}, not paused.")

            def delete(campaign_id: str) -> None:
                self.request("DELETE", f"/{campaign_id}")

            evidence = vh.create_check_delete(
                what="paused campaign", create=create, check=check, delete=delete
            )
            return (
                "Created a paused campaign in the sandbox ad account, read it back, then "
                "deleted it. Nothing was delivered or spent.",
                evidence,
            )

        return [
            vh.read_step("list_accounts", "Read the ad accounts this sign-in reaches", accounts),
            vh.read_step("account_details", "Read the chosen ad account", details),
            vh.read_step("read_performance", "Read yesterday's results", performance),
            vh.read_step("traffic_quality", "Read the placement breakdown", traffic),
            vh.write_step(
                "paused_campaign_roundtrip",
                "Create a paused campaign in the sandbox ad account, read it, then delete it",
                campaign,
            ),
        ]

    def _to_sample(self, row: dict) -> TrafficSample:
        platform = row.get("publisher_platform") or "unknown"
        position = row.get("platform_position") or "feed"
        clicks = int(row.get("clicks") or 0)
        link_clicks = int(row.get("inline_link_clicks") or 0)
        # Clicks that never became link clicks are the signal Meta gives
        # for accidental or non-human interaction.
        wasted = max(0, clicks - link_clicks)
        return TrafficSample(
            source=f"{platform}:{position}",
            sessions=clicks,
            bounce_rate=round(wasted / clicks, 3) if clicks else 0.0,
            avg_session_seconds=0.0,
            channel=self.channel,
            signals={"inline_link_clicks": link_clicks},
        )


def _rejected_destination_spec(error: Exception) -> bool:
    """Meta refused the creative because of ``destination_spec`` itself."""
    body = str(getattr(error, "vendor_body", "") or "").lower()
    return getattr(error, "http_status", None) == 400 and "destination_spec" in body


CONNECTOR_CLASS = MetaAdsConnector
