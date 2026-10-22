"""Google Ads connector — Google Ads API (GAQL reporting + mutates).

The API version is a setting rather than a constant: Google retires each
version roughly a year after release, and a hard-coded one turns every call
into a 404 the day it is sunset.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.connectors.base import verify_helpers as vh
from app.connectors.base.ads_base import FULL_AD_CAPABILITIES, BaseAdsConnector
from app.connectors.base.connector import Capability
from app.connectors.base.connector import ConnectorSpec, OAuthBinding, OAuthChoice
from app.connectors.base.credentials import (
    from_platform_sign_in,
    monthly_budget,
    secret,
    text,
)
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.connectors.base.oauth_providers import platform_setting
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Used when the API version field is blank. Google retires versions on a
#: published schedule (https://developers.google.com/google-ads/api/docs/sunset-dates);
#: v22 was sunset on 7 October 2026, and v25 (released July 2026) is the
#: current major version. The field lets an installation move forward without
#: a release.
DEFAULT_API_VERSION = "v25"

#: Responsive search ad limits (Google Ads API).
RSA_MIN_HEADLINES, RSA_MAX_HEADLINES, RSA_HEADLINE_CHARS = 3, 15, 30
RSA_MIN_DESCRIPTIONS, RSA_MAX_DESCRIPTIONS, RSA_DESCRIPTION_CHARS = 2, 4, 90


def _unique(texts, limit: int, chars: int) -> list[str]:  # noqa: ANN001
    out: list[str] = []
    for raw in texts:
        cleaned = " ".join(str(raw or "").split())[:chars].strip()
        if cleaned and cleaned.lower() not in {t.lower() for t in out}:
            out.append(cleaned)
        if len(out) == limit:
            break
    return out

# Google Ads reports in micros (millionths of the account currency).
MICROS = 1_000_000

#: The account itself (level 0) and, for a manager, its direct clients.
CUSTOMER_CLIENT_QUERY = (
    "SELECT customer_client.id, customer_client.descriptive_name, "
    "customer_client.manager, customer_client.level, customer_client.status "
    "FROM customer_client WHERE customer_client.level <= 1"
)


def _dashed(customer_id: str) -> str:
    digits = "".join(ch for ch in str(customer_id) if ch.isdigit())
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits


def parse_customer_clients(rows_by_login: dict[str, list[dict]]) -> list[OAuthChoice]:
    """Choices from ``customer_client`` rows, keyed by the account queried.

    Manager accounts are not offered themselves — they hold no campaigns and
    the API refuses performance queries against them — but their clients
    are, reached with the manager as ``login-customer-id``. An account the
    person can reach directly is preferred over the same account through a
    manager.
    """
    found: dict[str, OAuthChoice] = {}
    for login, rows in rows_by_login.items():
        manager_name = ""
        if not rows:
            found.setdefault(
                login,
                OAuthChoice(
                    id=login,
                    label=f"Account {_dashed(login)}",
                    detail="Direct access",
                    values={"customerId": login, "loginCustomerId": ""},
                ),
            )
            continue
        for row in rows:
            client = row.get("customerClient") or {}
            if int(client.get("level") or 0) == 0 and client.get("manager"):
                manager_name = str(client.get("descriptiveName") or "")
        for row in rows:
            client = row.get("customerClient") or {}
            customer = str(client.get("id") or "")
            if not customer or client.get("manager"):
                continue
            if str(client.get("status") or "ENABLED") != "ENABLED":
                continue
            is_direct = customer == login
            choice = OAuthChoice(
                id=customer if is_direct else f"{customer}@{login}",
                label=f"{client.get('descriptiveName') or 'Account'} ({_dashed(customer)})",
                detail=(
                    "Direct access"
                    if is_direct
                    else f"Through manager account {manager_name or _dashed(login)}"
                ),
                values={"customerId": customer, "loginCustomerId": "" if is_direct else login},
            )
            if is_direct or customer not in found:
                found[customer] = choice
    return sorted(found.values(), key=lambda c: c.label.lower())


#: Keyword match types, as KeywordMatchTypeEnum names them.
KEYWORD_MATCH_TYPES = {"exact": "EXACT", "phrase": "PHRASE", "broad": "BROAD"}
#: Display path fields (path1 / path2) on a responsive search ad.
RSA_PATH_CHARS = 15
#: Sitelink and callout asset limits.
SITELINK_TEXT_CHARS, SITELINK_DESCRIPTION_CHARS, CALLOUT_CHARS = 25, 35, 25


@dataclass(slots=True)
class SearchCampaignCreated:
    """What :meth:`GoogleAdsConnector.create_search_campaign` made."""

    #: ``customers/{id}/campaigns/{id}``.
    resource_name: str
    #: Sitelinks and callouts go in a second request after the campaign
    #: exists; False with ``note`` when that second request failed.
    extensions_added: bool = True
    note: str = ""

    @property
    def campaign_id(self) -> str:
        return self.resource_name.rsplit("/", 1)[-1]


class GoogleAdsConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, BaseAdsConnector):
    channel = "google"
    environments = (vh.TEST_ACCOUNT,)

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
            text(
                "campaignBudgetId",
                "Campaign budget ID",
                "1234567890",
                required=False,
                help_text=(
                    "The shared budget the budget engine adjusts. In Google Ads: Tools → "
                    "Shared library → Budgets, or the budget id of the campaign you want managed."
                ),
            ),
            text(
                "adGroupId",
                "Ad group ID",
                "1234567890",
                required=False,
                help_text=(
                    "The search ad group new ads are added to (paused, for you to review). "
                    "Needed only for the Dynamic Creative agent."
                ),
            ),
            text(
                "finalUrl",
                "Default landing page",
                "https://yourcompany.com",
                required=False,
                help_text=(
                    "Used only when an ad has no product page of its own; normally the ad "
                    "links to the page its copy was written from."
                ),
            ),
            text(
                "apiVersion",
                "API version",
                DEFAULT_API_VERSION,
                required=False,
                default=DEFAULT_API_VERSION,
                help_text=(
                    f"Google Ads API version. Leave blank to use {DEFAULT_API_VERSION}; "
                    "change it only if Google reports that version is no longer available."
                ),
            ),
            vh.environment_field(
                allowed=(vh.TEST_ACCOUNT,),
                help_text=(
                    "Choose Test account only for a Google Ads test account. A test "
                    "account is made under a test manager account; it never serves ads "
                    "or spends money. The developer token only needs test access, and "
                    "the Login customer ID must be the test manager account's id. "
                    "Leave blank for a real account."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Google" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Google',
                docs='Create it in Google Cloud with the adwords scope.',
            ),
        ),
        # Audience push is not offered: Google audiences are built from hashed
        # customer contact details (Customer Match), which the audience agent
        # deliberately never handles. An empty list would only look like one.
        capabilities=(FULL_AD_CAPABILITIES - {Capability.PUSH_AUDIENCE})
        | {Capability.CREATE_SEARCH_CAMPAIGN},
        requirements=(
            "A Google Ads developer token with Basic or Standard access — a token in "
            "test access only reaches test accounts.",
            "OAuth credentials (client ID, secret and refresh token) for a Google account "
            "with access to this Ads account, created with the adwords scope.",
        ),
        docs_url="https://developers.google.com/google-ads/api/docs/start",
        base_url="https://googleads.googleapis.com",
        # adwords is the Google Ads API's only scope; it covers reporting and
        # the budget, ad and campaign changes this connector makes.
        oauth=OAuthBinding(
            provider="google",
            scopes=("https://www.googleapis.com/auth/adwords",),
            # The developer token belongs to the platform's own manager
            # account, never to the customer.
            platform_fields=frozenset({"clientId", "clientSecret", "developerToken"}),
            requires_settings=("google_ads_developer_token",),
            access_summary=(
                "Access to the Google Ads accounts you choose, so agents can read "
                "performance and prepare budget and ad changes. Changes wait for "
                "your approval unless you turn that off."
            ),
            choice_noun="ad account",
            choice_fields=("customerId", "loginCustomerId"),
        ),
    )

    token_url = "https://oauth2.googleapis.com/token"

    @property
    def api_version(self) -> str:
        raw = (self.credentials.get("apiVersion") or DEFAULT_API_VERSION).strip().lower()
        return raw if raw.startswith("v") else f"v{raw}"

    def _path(self, tail: str) -> str:
        return f"/{self.api_version}/customers/{self.customer_id}/{tail}"

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Ad accounts the signed-in person can reach.

        ``listAccessibleCustomers`` returns only the accounts the person was
        added to directly. An agency usually has a manager (MCC) account
        instead, so each manager's own client accounts are listed too, with
        the manager remembered as the login customer the API needs.
        """
        data = self.request(
            "GET", f"/{self.api_version}/customers:listAccessibleCustomers"
        ) or {}
        direct = [
            str(name).rsplit("/", 1)[-1]
            for name in data.get("resourceNames") or []
            if str(name).startswith("customers/")
        ]
        rows_by_login: dict[str, list[dict]] = {}
        for customer in direct[:20]:
            try:
                response = self.request(
                    "POST",
                    f"/{self.api_version}/customers/{customer}/googleAds:searchStream",
                    json_body={"query": CUSTOMER_CLIENT_QUERY},
                    headers={"login-customer-id": customer},
                )
            except ConnectorError:
                # A cancelled or not-yet-enabled account cannot be queried;
                # it is still listed, by number.
                rows_by_login[customer] = []
                continue
            batches = response if isinstance(response, list) else [response]
            rows_by_login[customer] = [
                row for batch in batches for row in (batch or {}).get("results") or []
            ]
        return parse_customer_clients(rows_by_login)

    def _search(self, query: str) -> list[dict]:
        data = self.request("POST", self._path("googleAds:searchStream"), json_body={"query": query})
        # searchStream returns a list of batches, each with its own results.
        batches = data if isinstance(data, list) else [data]
        return [row for batch in batches for row in (batch or {}).get("results") or []]

    @staticmethod
    def _date_range(days: int) -> str:
        """An explicit range: GAQL's DURING only accepts fixed literals."""
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=max(1, days) - 1)
        return f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"

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
        if not developer_token and from_platform_sign_in(self.credentials.values):
            # Signed in with the platform's app: the platform's token.
            developer_token = platform_setting("google_ads_developer_token")
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
            f"metrics.conversions FROM customer WHERE {self._date_range(days)}"
        )
        spend = impressions = clicks = 0.0
        conversions = 0.0
        for row in self._search(query):
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
            raise ConnectorConfigError(
                "Enter the Campaign budget ID on the Google Ads integration so the "
                "budget engine can change it."
            )
        self.request(
            "POST",
            self._path("campaignBudgets:mutate"),
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
        final_url: str = "",
        extra_headlines: tuple[str, ...] = (),
        extra_descriptions: tuple[str, ...] = (),
    ) -> str:
        """Create a paused responsive search ad in the configured ad group.

        An RSA is assembled by Google from 3–15 headlines and 2–4 descriptions,
        so the variant's own copy is combined with its siblings for the same
        page. Too few distinct lines is refused here with the reason, rather
        than sent and rejected by Google.
        """
        ad_group_id = self.credentials.get("adGroupId")
        if not ad_group_id:
            raise ConnectorConfigError(
                "Enter the Ad group ID on the Google Ads integration so new ads have "
                "somewhere to go."
            )
        if not final_url:
            raise ConnectorConfigError(
                "This ad has no landing page. Enter a Default landing page on the "
                "Google Ads integration."
            )
        headlines = _unique((headline, *extra_headlines), RSA_MAX_HEADLINES, RSA_HEADLINE_CHARS)
        descriptions = _unique(
            (body_copy, *extra_descriptions), RSA_MAX_DESCRIPTIONS, RSA_DESCRIPTION_CHARS
        )
        if len(headlines) < RSA_MIN_HEADLINES or len(descriptions) < RSA_MIN_DESCRIPTIONS:
            raise ConnectorError(
                f"Google needs at least {RSA_MIN_HEADLINES} different headlines and "
                f"{RSA_MIN_DESCRIPTIONS} descriptions for a search ad; this page produced "
                f"{len(headlines)} and {len(descriptions)}."
            )
        response = self.request(
            "POST",
            self._path("adGroupAds:mutate"),
            json_body={
                "operations": [
                    {
                        "create": {
                            "adGroup": f"customers/{self.customer_id}/adGroups/{ad_group_id}",
                            "status": "PAUSED",  # never live without review
                            "ad": {
                                "responsiveSearchAd": {
                                    "headlines": [{"text": line} for line in headlines],
                                    "descriptions": [{"text": line} for line in descriptions],
                                },
                                "finalUrls": [final_url],
                            },
                        }
                    }
                ]
            },
        )
        results = (response or {}).get("results") or []
        return str(results[0].get("resourceName")) if results else ""

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        # Placement reports carry clicks and impressions. Bounce and time on
        # site are analytics metrics, not available on this view, so they are
        # left unknown (0) rather than invented.
        query = (
            "SELECT detail_placement_view.group_placement_target_url, "
            "metrics.clicks, metrics.impressions "
            f"FROM detail_placement_view WHERE {self._date_range(7)} "
            f"ORDER BY metrics.clicks DESC LIMIT {int(limit)}"
        )
        out: list[TrafficSample] = []
        for row in self._search(query):
            view = row.get("detailPlacementView") or {}
            metrics = row.get("metrics") or {}
            out.append(
                TrafficSample(
                    source=str(view.get("groupPlacementTargetUrl") or "unknown placement"),
                    sessions=int(metrics.get("clicks") or 0),
                    channel=self.channel,
                    engagement_known=False,
                )
            )
        return out

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        self.request(
            "POST",
            self._path("customerNegativeCriteria:mutate"),
            json_body={"operations": [{"create": {"placement": {"url": source}}}]},
        )
        log.info("Excluded placement %s on Google Ads (%s)", source, reason)
        return True

    # ── Search campaigns ───────────────────────────────────────────────────
    def create_search_campaign(
        self,
        *,
        name: str,
        daily_budget: float,
        ad_groups: list[dict],
        negative_keywords: list[dict] = (),  # type: ignore[assignment]
        sitelinks: list[dict] = (),  # type: ignore[assignment]
        callouts: list[str] = (),  # type: ignore[assignment]
    ) -> SearchCampaignCreated:
        """Create a whole search campaign, paused, in one atomic request.

        ``POST /{version}/customers/{id}/googleAds:mutate`` with
        ``mutateOperations`` in dependency order and temporary (negative)
        resource ids, so later operations can point at objects created
        earlier in the same request. The request is atomic (partialFailure is
        not set): if Google refuses any part, nothing at all is created.

        Created: a campaign budget (``explicitlyShared: false``), a SEARCH
        campaign with status PAUSED, Maximize clicks bidding (``targetSpend``)
        and Google Search only; ENABLED ad groups (SEARCH_STANDARD) inside
        it; their keywords; campaign-level negative keywords; and one
        responsive search ad per ad group. Nothing serves until somebody
        switches the campaign on. Location and language targeting are not
        set.

        Sitelinks and callouts are assets linked to the campaign, sent in a
        second request once the campaign exists. If that one fails the
        campaign is still returned, with ``extensions_added`` False and a
        note saying exactly what is missing.

        ``ad_groups`` items: ``{"name", "landing_url", "keywords": [{"text",
        "match_type"}], "ad": {"headlines", "descriptions", "path1",
        "path2"}}``; ``negative_keywords`` items ``{"text", "match_type"}``;
        ``sitelinks`` items ``{"text", "description1", "description2",
        "url"}``.
        """
        if daily_budget <= 0:
            raise ConnectorError("A search campaign needs a daily budget above zero.")
        if not ad_groups:
            raise ConnectorError("A search campaign needs at least one ad group.")
        customer = f"customers/{self.customer_id}"
        budget_ref = f"{customer}/campaignBudgets/-1"
        campaign_ref = f"{customer}/campaigns/-2"
        operations: list[dict] = [
            {
                "campaignBudgetOperation": {
                    "create": {
                        "resourceName": budget_ref,
                        # Budget names must be unique in the account.
                        "name": f"{name} budget"[:255],
                        # The account currency's smallest unit is 0.01 for
                        # most currencies; micros are rounded to it.
                        "amountMicros": int(round(daily_budget * 100)) * (MICROS // 100),
                        "deliveryMethod": "STANDARD",
                        "explicitlyShared": False,
                    }
                }
            },
            {
                "campaignOperation": {
                    "create": {
                        "resourceName": campaign_ref,
                        "name": name[:255],
                        "advertisingChannelType": "SEARCH",
                        "status": "PAUSED",
                        "campaignBudget": budget_ref,
                        # Maximize clicks. Needs no per-keyword bids.
                        "targetSpend": {},
                        "networkSettings": {
                            "targetGoogleSearch": True,
                            "targetSearchNetwork": False,
                            "targetContentNetwork": False,
                            "targetPartnerSearchNetwork": False,
                        },
                        # Required on new campaigns since the EU political
                        # advertising declaration was introduced.
                        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                    }
                }
            },
        ]
        counts = {"ad_groups": 0, "keywords": 0, "negatives": 0, "ads": 0}
        for index, group in enumerate(ad_groups):
            group_ref = f"{customer}/adGroups/-{index + 10}"
            final_url = str(group.get("landing_url") or "").strip()
            ad = group.get("ad") or {}
            headlines = _unique(ad.get("headlines") or [], RSA_MAX_HEADLINES, RSA_HEADLINE_CHARS)
            descriptions = _unique(
                ad.get("descriptions") or [], RSA_MAX_DESCRIPTIONS, RSA_DESCRIPTION_CHARS
            )
            label = str(group.get("name") or f"Ad group {index + 1}")
            if not final_url.startswith(("https://", "http://")):
                raise ConnectorError(f"Ad group {label!r} has no landing page.")
            if len(headlines) < RSA_MIN_HEADLINES or len(descriptions) < RSA_MIN_DESCRIPTIONS:
                raise ConnectorError(
                    f"Ad group {label!r} needs at least {RSA_MIN_HEADLINES} headlines and "
                    f"{RSA_MIN_DESCRIPTIONS} descriptions; it has {len(headlines)} and "
                    f"{len(descriptions)}."
                )
            operations.append(
                {
                    "adGroupOperation": {
                        "create": {
                            "resourceName": group_ref,
                            "name": label[:255],
                            "campaign": campaign_ref,
                            "status": "ENABLED",
                            "type": "SEARCH_STANDARD",
                        }
                    }
                }
            )
            counts["ad_groups"] += 1
            for keyword in group.get("keywords") or []:
                match = KEYWORD_MATCH_TYPES.get(str(keyword.get("match_type") or "").lower())
                text_ = str(keyword.get("text") or "").strip()
                if not match or not text_:
                    continue
                operations.append(
                    {
                        "adGroupCriterionOperation": {
                            "create": {
                                "adGroup": group_ref,
                                "status": "ENABLED",
                                "keyword": {"text": text_, "matchType": match},
                            }
                        }
                    }
                )
                counts["keywords"] += 1
            responsive: dict = {
                "headlines": [{"text": line} for line in headlines],
                "descriptions": [{"text": line} for line in descriptions],
            }
            # path2 is only valid with a path1.
            paths = [
                value
                for value in (str(ad.get(k) or "").strip()[:RSA_PATH_CHARS] for k in ("path1", "path2"))
                if value
            ]
            for key, value in zip(("path1", "path2"), paths, strict=False):
                responsive[key] = value
            operations.append(
                {
                    "adGroupAdOperation": {
                        "create": {
                            "adGroup": group_ref,
                            "status": "ENABLED",
                            "ad": {"finalUrls": [final_url], "responsiveSearchAd": responsive},
                        }
                    }
                }
            )
            counts["ads"] += 1
        for keyword in negative_keywords or []:
            match = KEYWORD_MATCH_TYPES.get(str(keyword.get("match_type") or "").lower())
            text_ = str(keyword.get("text") or "").strip()
            if not match or not text_:
                continue
            operations.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": campaign_ref,
                            "negative": True,
                            "keyword": {"text": text_, "matchType": match},
                        }
                    }
                }
            )
            counts["negatives"] += 1

        try:
            response = self.request(
                "POST", self._path("googleAds:mutate"), json_body={"mutateOperations": operations}
            )
        except ConnectorError as exc:
            raise ConnectorError(
                f"Google Ads did not accept the campaign, so nothing was created in your "
                f"account (it is sent as one change). {exc}"
            ) from exc
        resource_name = ""
        for item in (response or {}).get("mutateOperationResponses") or []:
            created = (item or {}).get("campaignResult") or {}
            if created.get("resourceName"):
                resource_name = str(created["resourceName"])
                break
        if not resource_name:
            raise ConnectorError(
                "Google Ads answered without naming the new campaign. Check the account "
                f"for a paused campaign called {name!r} before approving this again."
            )
        log.info(
            "Created paused search campaign %s (%s ad groups, %s keywords, %s negatives, %s ads)",
            resource_name,
            counts["ad_groups"],
            counts["keywords"],
            counts["negatives"],
            counts["ads"],
        )
        result = SearchCampaignCreated(resource_name=resource_name)
        extension_ops = _extension_operations(customer, resource_name, sitelinks, callouts)
        if extension_ops:
            try:
                self.request(
                    "POST",
                    self._path("googleAds:mutate"),
                    json_body={"mutateOperations": extension_ops},
                )
            except ConnectorError as exc:
                result.extensions_added = False
                result.note = (
                    f"The campaign, its {counts['ad_groups']} ad groups, keywords, negative "
                    "keywords and ads were created, but Google did not accept the sitelinks "
                    f"and callouts, so none were added. {exc}"
                )
                log.warning("Search campaign %s created without extensions: %s", resource_name, exc)
        return result

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read what the agents read; on a test account, make and remove a
        paused campaign.

        The write step has two guards besides the runner's own: the
        connection must be marked as a test account, and Google itself must
        report ``customer.test_account`` as true for the chosen customer. A
        test account never serves ads or spends, so even a leftover campaign
        costs nothing.
        """

        def accounts() -> tuple[str, dict]:
            choices = self.list_oauth_choices()
            return (
                f"This sign-in reaches {vh.plural(len(choices), 'ad account')}.",
                {"count": len(choices), "ids": [c.values.get("customerId", c.id) for c in choices][:20]},
            )

        def details() -> tuple[str, dict]:
            row = self._customer_row()
            customer = row.get("customer") or {}
            test = bool(customer.get("testAccount"))
            return (
                f"Read the chosen account ({customer.get('currencyCode') or 'unknown currency'}"
                f"{', a test account' if test else ''}).",
                {
                    "customer_id": str(customer.get("id") or self.customer_id),
                    "currency": customer.get("currencyCode") or "",
                    "test_account": test,
                    "manager": bool(customer.get("manager")),
                },
            )

        def performance() -> tuple[str, dict]:
            result = self.read_performance(days=1)
            return (
                f"Read yesterday's results: {result.impressions} impressions, "
                f"{result.clicks} clicks.",
                {"impressions": result.impressions, "clicks": result.clicks, "spend": result.spend},
            )

        def traffic() -> tuple[str, dict]:
            samples = self.sample_traffic(limit=5)
            return (
                f"Read the placement report ({vh.plural(len(samples), 'placement')}).",
                {"count": len(samples)},
            )

        def campaign() -> tuple[str, dict]:
            vh.refuse_unless_test(self, "it creates a campaign.")
            customer = (self._customer_row().get("customer") or {})
            if customer.get("testAccount") is not True:
                raise ConnectorConfigError(
                    "Google does not report this account as a test account, so no "
                    "campaign was created. Use an account made under a test manager account."
                )
            name = vh.marker("campaign")
            base = f"customers/{self.customer_id}"
            budget: dict[str, str] = {}

            def create() -> str:
                response = self.request(
                    "POST",
                    self._path("googleAds:mutate"),
                    json_body={
                        "mutateOperations": [
                            {
                                "campaignBudgetOperation": {
                                    "create": {
                                        "resourceName": f"{base}/campaignBudgets/-1",
                                        "name": f"{name} budget",
                                        "amountMicros": MICROS,  # one unit a day
                                        "deliveryMethod": "STANDARD",
                                        "explicitlyShared": False,
                                    }
                                }
                            },
                            {
                                "campaignOperation": {
                                    "create": {
                                        "resourceName": f"{base}/campaigns/-2",
                                        "name": name,
                                        "advertisingChannelType": "SEARCH",
                                        "status": "PAUSED",
                                        "campaignBudget": f"{base}/campaignBudgets/-1",
                                        "targetSpend": {},
                                        "networkSettings": {
                                            "targetGoogleSearch": True,
                                            "targetSearchNetwork": False,
                                            "targetContentNetwork": False,
                                            "targetPartnerSearchNetwork": False,
                                        },
                                        "containsEuPoliticalAdvertising": (
                                            "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
                                        ),
                                    }
                                }
                            },
                        ]
                    },
                ) or {}
                campaign_name = ""
                for item in response.get("mutateOperationResponses") or []:
                    item = item or {}
                    if (item.get("campaignBudgetResult") or {}).get("resourceName"):
                        budget["name"] = str(item["campaignBudgetResult"]["resourceName"])
                    if (item.get("campaignResult") or {}).get("resourceName"):
                        campaign_name = str(item["campaignResult"]["resourceName"])
                return campaign_name.rsplit("/", 1)[-1] if campaign_name else ""

            def check(campaign_id: str) -> None:
                rows = self._search(
                    "SELECT campaign.id, campaign.status, campaign.advertising_channel_type "
                    f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
                )
                found = (rows[0].get("campaign") or {}) if rows else {}
                if found.get("status") != "PAUSED":
                    raise ConnectorError(
                        f"The campaign came back as {found.get('status')!r}, not paused."
                    )

            def delete(campaign_id: str) -> None:
                self.request(
                    "POST",
                    self._path("googleAds:mutate"),
                    json_body={
                        "mutateOperations": [
                            {"campaignOperation": {"remove": f"{base}/campaigns/{campaign_id}"}}
                        ]
                    },
                )
                if budget.get("name"):
                    self.request(
                        "POST",
                        self._path("googleAds:mutate"),
                        json_body={
                            "mutateOperations": [
                                {"campaignBudgetOperation": {"remove": budget["name"]}}
                            ]
                        },
                    )

            evidence = vh.create_check_delete(
                what="paused campaign", create=create, check=check, delete=delete
            )
            evidence["budget_id"] = budget.get("name", "").rsplit("/", 1)[-1]
            return (
                "Created a paused search campaign and its budget in the test account, "
                "read it back, then removed both. Nothing served or spent.",
                evidence,
            )

        return [
            vh.read_step("list_accounts", "Read the ad accounts this sign-in reaches", accounts),
            vh.read_step("account_details", "Read the chosen ad account", details),
            vh.read_step("read_performance", "Read yesterday's results", performance),
            vh.read_step("traffic_quality", "Read the placement report", traffic),
            vh.write_step(
                "paused_campaign_roundtrip",
                "Create a paused campaign in the test account, read it, then remove it",
                campaign,
            ),
        ]

    def _customer_row(self) -> dict:
        rows = self._search(
            "SELECT customer.id, customer.currency_code, customer.manager, "
            "customer.test_account FROM customer"
        )
        return rows[0] if rows else {}

    def diagnose(self, error: Exception) -> str | None:
        message = str(error)
        if "404" in message or "not found" in message.lower():
            return (
                f"Google Ads did not recognise API version {self.api_version}. Google "
                "retires versions on a schedule — enter the current version (see the "
                "Google Ads API release notes) in the API version field."
            )
        if "DEVELOPER_TOKEN" in message.upper():
            return "Google rejected the developer token. Check it in the manager account's API Center."
        return None


def _extension_operations(
    customer: str, campaign: str, sitelinks, callouts  # noqa: ANN001
) -> list[dict]:
    """Sitelink and callout assets, each linked to ``campaign``.

    An ``assetOperation`` creates the asset under a temporary id and a
    ``campaignAssetOperation`` attaches it with its field type (SITELINK or
    CALLOUT). Google shows sitelinks only when there are at least two.
    """
    operations: list[dict] = []
    temp = -100

    def add(asset: dict, field_type: str) -> None:
        nonlocal temp
        ref = f"{customer}/assets/{temp}"
        temp -= 1
        operations.append({"assetOperation": {"create": {"resourceName": ref, **asset}}})
        operations.append(
            {
                "campaignAssetOperation": {
                    "create": {"campaign": campaign, "asset": ref, "fieldType": field_type}
                }
            }
        )

    links = []
    for link in sitelinks or []:
        text_ = " ".join(str(link.get("text") or "").split())
        url = str(link.get("url") or "").strip()
        if not text_ or len(text_) > SITELINK_TEXT_CHARS or not url.startswith(("https://", "http://")):
            continue
        sitelink: dict = {"linkText": text_}
        first = " ".join(str(link.get("description1") or "").split())
        second = " ".join(str(link.get("description2") or "").split())
        # Both description lines or neither.
        if first and second and max(len(first), len(second)) <= SITELINK_DESCRIPTION_CHARS:
            sitelink.update(description1=first, description2=second)
        links.append({"sitelinkAsset": sitelink, "finalUrls": [url]})
    if len(links) >= 2:
        for asset in links:
            add(asset, "SITELINK")
    for callout in callouts or []:
        text_ = " ".join(str(callout or "").split())
        if text_ and len(text_) <= CALLOUT_CHARS:
            add({"calloutAsset": {"calloutText": text_}}, "CALLOUT")
    return operations


CONNECTOR_CLASS = GoogleAdsConnector
