"""LinkedIn Ads connector — Marketing API, versioned REST surface.

Two things about this API catch every integration out:

* It is **versioned by month** (``LinkedIn-Version: YYYYMM``) and each version
  is retired about a year after release, at which point every call answers
  426. The version is therefore a field with a current default, not a
  constant buried here.
* Ids of created entities come back in the ``x-restli-id`` **response
  header**, with an empty body — so reading ``id`` from the JSON, as this used
  to, always returned "".
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

import httpx

from app.connectors.base.ads_base import BaseAdsConnector
from app.connectors.base.connector import Capability, ConnectorSpec, OAuthBinding, OAuthChoice
from app.connectors.base.credentials import monthly_budget, text
from app.connectors.base.interfaces import ChannelPerformance
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Default ``LinkedIn-Version``. LinkedIn supports each monthly version for
#: roughly a year; when this one is sunset, the field lets an operator move
#: forward without a code change.
DEFAULT_API_VERSION = "202606"

#: Characters LinkedIn's "little text" format reserves in post commentary.
#: Unescaped, they are parsed as mentions/hashtags/markup and the call fails.
_LITTLE_TEXT_RESERVED = set("\\|{}@[]()<>#*_~")


def _little_text(value: str) -> str:
    return "".join(f"\\{ch}" if ch in _LITTLE_TEXT_RESERVED else ch for ch in value)


#: Pages of the ad account search followed during the choose step.
_MAX_LIST_PAGES = 10


def parse_ad_accounts(elements: list[dict]) -> list[OAuthChoice]:
    """Ad accounts out of ``GET /rest/adAccounts?q=search`` elements."""
    out: list[OAuthChoice] = []
    for row in elements:
        raw = str(row.get("id") or "")
        number = raw.rsplit(":", 1)[-1]
        status = str(row.get("status") or "").upper()
        if not number or status in ("REMOVED", "CANCELED"):
            continue
        parts = [f"Account ID {number}"]
        if row.get("currency"):
            parts.append(str(row["currency"]))
        if status and status != "ACTIVE":
            parts.append(status.replace("_", " ").capitalize())
        if row.get("test"):
            parts.append("Test account")
        out.append(
            OAuthChoice(
                id=number,
                label=str(row.get("name") or f"Ad account {number}"),
                detail=" · ".join(parts),
                values={"adAccountId": number},
            )
        )
    return out


def _restli_date(day: date) -> str:
    return f"(year:{day.year},month:{day.month},day:{day.day})"


class LinkedInAdsConnector(OAuthTokenMixin, BaseAdsConnector):
    channel = "linkedin"

    spec = ConnectorSpec(
        slug="linkedin_ads",
        name="LinkedIn Ads",
        category="Ad Platforms",
        description="Sponsored content: analytics, budgets and sponsored creatives.",
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
            text(
                "campaignId",
                "Campaign ID",
                "123456789",
                required=False,
                help_text=(
                    "A Sponsored Content campaign in this account, from Campaign "
                    "Manager. The budget engine sets its daily budget, and "
                    "uploaded creatives are added to it as drafts."
                ),
            ),
            text(
                "apiVersion",
                "LinkedIn API version",
                DEFAULT_API_VERSION,
                required=False,
                default=DEFAULT_API_VERSION,
                help_text=(
                    "Sent as the LinkedIn-Version header (YYYYMM). LinkedIn retires "
                    "each version after about a year; if calls start failing with "
                    "'version' errors, enter a recent month from LinkedIn's "
                    "Marketing API versioning page."
                ),
            ),
            monthly_budget(),
            # The credential this connector has always required. It used
            # to be an "Authorize with LinkedIn" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='LinkedIn',
                docs='From your LinkedIn app, with r_ads, rw_ads, r_ads_reporting and w_organization_social.',
            ),
        ),
        # LinkedIn exposes no placement-level traffic-quality data, so the
        # fraud agent routes around this connector rather than guessing.
        #
        # PUSH_AUDIENCE was removed: a DMP segment is filled by uploading
        # member identifiers (hashed emails or company lists), which this
        # platform deliberately never sends, so the segment it created was
        # always empty. LinkedIn has no API that builds an audience from a
        # behavioural definition.
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.UPLOAD_CREATIVE,
            }
        ),
        requirements=(
            "A LinkedIn developer app approved for the Advertising API.",
            "Budget writes and creatives need the Campaign ID of a Sponsored "
            "Content campaign; creatives also need the Organization ID.",
        ),
        docs_url="https://learn.microsoft.com/en-us/linkedin/marketing/",
        base_url="https://api.linkedin.com/rest",
        # Advertising API: r_ads lists the accounts, r_ads_reporting reads
        # analytics, rw_ads sets budgets and creates creatives, and
        # w_organization_social creates the page post a creative sponsors.
        oauth=OAuthBinding(
            provider="linkedin",
            scopes=("r_ads", "r_ads_reporting", "rw_ads", "w_organization_social"),
            access_summary=(
                "Access to the LinkedIn ad account you choose, so agents can read "
                "results and prepare budget and creative changes. Changes wait for "
                "your approval unless you turn that off."
            ),
            choice_noun="ad account",
            choice_fields=("adAccountId",),
        ),
    )

    # Used only when LinkedIn issued a refresh token (approved Marketing
    # Developer Platform apps). Otherwise the 60-day access token is used
    # as stored, and the mixin says "Sign in again" when it runs out.
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._currency: str | None = None

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Every ad account the signed-in member can reach (a search with no
        filter returns all of them), followed through its page tokens."""
        elements: list[dict] = []
        token = ""
        for _ in range(_MAX_LIST_PAGES):
            params: dict = {"q": "search", "pageSize": 100}
            if token:
                params["pageToken"] = token
            data = self.request("GET", "/adAccounts", params=params) or {}
            elements.extend(e for e in data.get("elements") or [] if isinstance(e, dict))
            token = str((data.get("metadata") or {}).get("nextPageToken") or "")
            if not token:
                break
        return parse_ad_accounts(elements)

    @property
    def api_version(self) -> str:
        return (self.credentials.get("apiVersion") or DEFAULT_API_VERSION).strip()

    @property
    def account_urn(self) -> str:
        raw = self.credentials.require("adAccountId").strip()
        return raw if raw.startswith("urn:") else f"urn:li:sponsoredAccount:{raw}"

    @property
    def account_id(self) -> str:
        return self.account_urn.rsplit(":", 1)[-1]

    @property
    def campaign_id(self) -> str:
        raw = (self.credentials.get("campaignId") or "").strip()
        if not raw:
            raise ConnectorConfigError(
                "Enter the Campaign ID on the LinkedIn Ads integration — budget "
                "writes and creatives are made against that campaign."
            )
        return raw.rsplit(":", 1)[-1]

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "LinkedIn-Version": self.api_version,
            # Required for the versioned REST surface.
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def _send(self, request: httpx.Request) -> httpx.Response:
        response = super()._send(request)
        # A retired version answers 426; one that does not exist yet answers
        # 400 NONEXISTENT_VERSION. Neither is fixed by retrying, and both are
        # fixed by the operator in one field, so say so.
        if response.status_code == 426 or (
            response.status_code == 400 and "NONEXISTENT_VERSION" in response.text
        ):
            raise ConnectorConfigError(self._version_message())
        return response

    def _version_message(self) -> str:
        return (
            f"LinkedIn no longer accepts API version {self.api_version}. Enter a "
            "recent version (YYYYMM) in the LinkedIn API version field on this "
            "integration."
        )

    def diagnose(self, error: Exception) -> str | None:
        if "API version" in str(error):
            return self._version_message()
        return None

    @staticmethod
    def _created_id(response: httpx.Response) -> str:
        """LinkedIn returns a created entity's URN in a header, not the body."""
        return response.headers.get("x-restli-id") or response.headers.get(
            "x-linkedin-id", ""
        )

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        end = date.today()
        start = end - timedelta(days=max(days, 1) - 1)
        # Rest.li 2.0 query syntax. The URN inside List() is percent-encoded,
        # the structural parentheses, colons and commas are not — httpx's
        # params encoding would mangle it, so the query is built here.
        query = (
            "q=analytics&pivot=ACCOUNT&timeGranularity=ALL"
            f"&dateRange=(start:{_restli_date(start)},end:{_restli_date(end)})"
            f"&accounts=List({quote(self.account_urn, safe='')})"
            "&fields=costInLocalCurrency,impressions,clicks,externalWebsiteConversions"
        )
        data = self.request("GET", f"/adAnalytics?{query}")
        rows = (data or {}).get("elements") or []
        if not rows:
            return ChannelPerformance(channel=self.channel)
        row = rows[0]
        return ChannelPerformance(
            channel=self.channel,
            spend=round(float(row.get("costInLocalCurrency") or 0), 2),
            impressions=int(row.get("impressions") or 0),
            clicks=int(row.get("clicks") or 0),
            conversions=int(row.get("externalWebsiteConversions") or 0),
        )

    # ── Budgets ────────────────────────────────────────────────────────────
    def _account_currency(self) -> str:
        """The account's own currency — a budget in any other is rejected."""
        if self._currency is None:
            account = self.request("GET", f"/adAccounts/{self.account_id}") or {}
            currency = str(account.get("currency") or "").strip()
            if not currency:
                raise ConnectorError(
                    "LinkedIn did not report this ad account's currency, so the "
                    "budget cannot be set safely."
                )
            self._currency = currency
        return self._currency

    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = self.campaign_id
        currency = self._account_currency()
        self.request(
            "POST",
            f"/adAccounts/{self.account_id}/adCampaigns/{campaign_id}",
            headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
            json_body={
                "patch": {
                    "$set": {
                        "dailyBudget": {
                            "amount": f"{daily_budget:.2f}",
                            "currencyCode": currency,
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
        final_url: str = "",
        extra_headlines: tuple[str, ...] = (),
        extra_descriptions: tuple[str, ...] = (),
    ) -> str:
        """A sponsored creative: a dark post, then a creative referencing it.

        The dark post (``feedDistribution: NONE`` with an ``adContext``) never
        appears on the company page; it exists only to be sponsored. The
        creative is created as a DRAFT in the configured campaign, so nothing
        serves until somebody activates it in Campaign Manager.
        """
        organization = (self.credentials.get("organizationId") or "").strip()
        if not organization:
            raise ConnectorConfigError(
                "Enter the Organization ID on the LinkedIn Ads integration to "
                "publish creatives — LinkedIn requires the company page they are "
                "published as."
            )
        if not organization.startswith("urn:"):
            organization = f"urn:li:organization:{organization}"
        campaign_id = self.campaign_id
        if not final_url:
            raise ConnectorConfigError(
                "A LinkedIn sponsored creative needs a landing page, and none was "
                "given for this creative."
            )

        post = self.request(
            "POST",
            "/posts",
            json_body={
                "author": organization,
                "commentary": _little_text(body_copy),
                "visibility": "PUBLIC",
                "distribution": {
                    "feedDistribution": "NONE",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "content": {"article": {"source": final_url, "title": headline[:200]}},
                "adContext": {"dscAdAccount": self.account_urn, "dscStatus": "ACTIVE"},
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
            return_response=True,
        )
        post_urn = self._created_id(post)
        if not post_urn:
            raise ConnectorError("LinkedIn created the post but did not return its id.")

        creative = self.request(
            "POST",
            f"/adAccounts/{self.account_id}/creatives",
            json_body={
                "campaign": f"urn:li:sponsoredCampaign:{campaign_id}",
                "content": {"reference": post_urn},
                "intendedStatus": "DRAFT",
            },
            return_response=True,
        )
        return self._created_id(creative)

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read what the agents read; in a test ad account, make and delete a
        DRAFT campaign group.

        Besides the connection being marked as a test account, the write step
        reads the ad account and refuses unless LinkedIn itself reports it as
        a test account (``test: true``), whose objects are never served or
        billed. LinkedIn only allows DELETE on a DRAFT campaign group, which is
        why the group is created as one.
        """
        from datetime import UTC, datetime

        from app.connectors.base import verify_helpers as vh

        def accounts() -> tuple[str, dict]:
            choices = self.list_oauth_choices()
            return (
                f"This sign-in reaches {vh.plural(len(choices), 'ad account')}.",
                {"count": len(choices), "ids": [c.id for c in choices][:20]},
            )

        def details() -> tuple[str, dict]:
            account = self.request("GET", f"/adAccounts/{self.account_id}") or {}
            test = bool(account.get("test"))
            return (
                f"Read the chosen ad account ({account.get('currency') or 'unknown currency'}"
                f"{', a test account' if test else ''}).",
                {
                    "ad_account_id": str(account.get("id") or self.account_id),
                    "status": account.get("status") or "",
                    "currency": account.get("currency") or "",
                    "test": test,
                },
            )

        def performance() -> tuple[str, dict]:
            result = self.read_performance(days=1)
            return (
                f"Read today's results: {result.impressions} impressions, {result.clicks} clicks.",
                {"impressions": result.impressions, "clicks": result.clicks, "spend": result.spend},
            )

        def campaign_group() -> tuple[str, dict]:
            vh.refuse_unless_test(self, "it creates a campaign group.")
            account = self.request("GET", f"/adAccounts/{self.account_id}") or {}
            if account.get("test") is not True:
                raise ConnectorConfigError(
                    "LinkedIn does not report this ad account as a test account, so "
                    "nothing was created. Use a test ad account (created through the "
                    "API with test set to true)."
                )
            base = f"/adAccounts/{self.account_id}/adCampaignGroups"
            # A week from now: nothing could serve before then even if it
            # were not a draft in a test account.
            start = int(datetime.now(UTC).timestamp() * 1000) + 7 * 24 * 3600 * 1000

            def create() -> str:
                response = self.request(
                    "POST",
                    base,
                    json_body={
                        "account": self.account_urn,
                        "name": vh.marker("campaign group"),
                        "status": "DRAFT",
                        "runSchedule": {"start": start},
                    },
                    return_response=True,
                )
                return self._created_id(response).rsplit(":", 1)[-1]

            def check(group_id: str) -> None:
                row = self.request("GET", f"{base}/{group_id}") or {}
                if row.get("status") != "DRAFT":
                    raise ConnectorError(
                        f"The campaign group came back as {row.get('status')!r}, not a draft."
                    )

            def delete(group_id: str) -> None:
                self.request("DELETE", f"{base}/{group_id}", expect_json=False)

            evidence = vh.create_check_delete(
                what="draft campaign group", create=create, check=check, delete=delete
            )
            return (
                "Created a draft campaign group in the test ad account, read it back, then "
                "deleted it. Nothing was served or billed.",
                evidence,
            )

        return [
            vh.read_step("list_accounts", "Read the ad accounts this sign-in reaches", accounts),
            vh.read_step("account_details", "Read the chosen ad account", details),
            vh.read_step("read_performance", "Read today's results", performance),
            vh.write_step(
                "draft_campaign_group_roundtrip",
                "Create a draft campaign group in the test ad account, read it, then delete it",
                campaign_group,
            ),
        ]


CONNECTOR_CLASS = LinkedInAdsConnector
