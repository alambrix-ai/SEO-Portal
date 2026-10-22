"""Microsoft Advertising connector — Bing search and audience network.

Uses the JSON (REST) endpoints of Bing Ads API v13. Campaign management is
synchronous; **reporting is not**: a report is submitted
(``GenerateReport/Submit``), polled (``GenerateReport/Poll``) until it is
ready, and then downloaded from the URL the poll returns as a zipped CSV.
There is no synchronous "query" endpoint — the ``/Reporting/Query`` this used
to call does not exist, so every read failed.
"""
from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import date, timedelta

import httpx

from app.connectors.base import verify_helpers as vh
from app.connectors.base.ads_base import BaseAdsConnector
from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
    SignInInput,
)
from app.connectors.base.credentials import from_platform_sign_in, monthly_budget, secret, text
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.oauth_providers import platform_setting
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API_VERSION = "v13"
CAMPAIGN_URL = f"https://campaign.api.bingads.microsoft.com/CampaignManagement/{API_VERSION}"
REPORTING_URL = f"https://reporting.api.bingads.microsoft.com/Reporting/{API_VERSION}"
#: Customer Management (REST): who the signed-in user is and which
#: accounts they can reach. These calls take no CustomerId headers.
CUSTOMER_URL = f"https://clientcenter.api.bingads.microsoft.com/CustomerManagement/{API_VERSION}"

#: The sandbox (learn.microsoft.com/advertising/guides/sandbox) has its own
#: hosts for every service and its own customers, users and accounts; the
#: REST paths are the same. See the "Sandbox URL" tab of each operation.
SANDBOX_CAMPAIGN_URL = (
    f"https://campaign.api.sandbox.bingads.microsoft.com/CampaignManagement/{API_VERSION}"
)
SANDBOX_REPORTING_URL = f"https://reporting.api.sandbox.bingads.microsoft.com/Reporting/{API_VERSION}"
SANDBOX_CUSTOMER_URL = (
    f"https://clientcenter.api.sandbox.bingads.microsoft.com/CustomerManagement/{API_VERSION}"
)

#: Sign-in. Both environments use the Microsoft identity platform's common
#: endpoint and your own app registration; a sandbox user (an outlook.com
#: Microsoft account) consents to the sandbox resource's scope instead. This
#: is the Bing Ads SDKs' default for sandbox (``use_msa_prod``); the older
#: login.windows-ppe.net endpoint is only for legacy outlook-int.com users.
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
LIVE_SCOPE = "https://ads.microsoft.com/msads.manage"
SANDBOX_SCOPE = "https://si.ads.microsoft.com/msads.manage"
#: Microsoft's universal developer token, accepted in sandbox only.
SANDBOX_DEVELOPER_TOKEN = "BBD37VB98"
#: Customers whose accounts are listed in the choose step.
_MAX_CUSTOMERS = 20

#: How long to wait for a report: polls x seconds between them. Small
#: summary reports are usually ready within a few seconds.
_MAX_POLLS = 30
_POLL_SECONDS = 2.0


def _number(value: object) -> float:
    raw = str(value or "").replace(",", "").strip()
    if raw in ("", "--"):
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _ms_date(day: date) -> dict[str, int]:
    return {"Day": day.day, "Month": day.month, "Year": day.year}


def parse_accounts(customer_id: str, data: dict) -> list[OAuthChoice]:
    """Accounts of one customer out of a ``GetAccountsInfo`` response."""
    out: list[OAuthChoice] = []
    for info in data.get("AccountsInfo") or []:
        if not isinstance(info, dict) or not info.get("Id"):
            continue
        account = str(info["Id"])
        status = str(info.get("AccountLifeCycleStatus") or "")
        if status in ("Inactive",):
            continue
        parts = [f"Account {info.get('Number') or account}", f"Customer {customer_id}"]
        if status and status != "Active":
            parts.append(status)
        out.append(
            OAuthChoice(
                id=f"{customer_id}:{account}",
                label=str(info.get("Name") or f"Account {account}"),
                detail=" · ".join(parts),
                values={"customerId": customer_id, "accountId": account},
            )
        )
    return out


def customer_ids(user: dict) -> list[str]:
    """Customer ids out of a ``GetUser`` response's ``CustomerRoles``."""
    out: list[str] = []
    for role in user.get("CustomerRoles") or []:
        customer = str((role or {}).get("CustomerId") or "")
        if customer and customer not in out:
            out.append(customer)
    return out


class MicrosoftAdsConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, BaseAdsConnector):
    channel = "microsoft"
    environments = (vh.SANDBOX,)

    spec = ConnectorSpec(
        slug="microsoft_ads",
        name="Microsoft Ads",
        category="Ad Platforms",
        description="Bing search and audience network: reporting, budgets, placement quality.",
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
            text(
                "campaignId",
                "Campaign ID",
                "123456789",
                required=False,
                help_text=(
                    "The campaign whose daily budget the budget engine sets. It "
                    "must have its own budget, not a shared one. Leave blank to "
                    "keep budgets manual."
                ),
            ),
            monthly_budget(),
            vh.environment_field(
                allowed=(vh.SANDBOX,),
                help_text=(
                    "Leave blank for a real Microsoft Advertising account. Type Sandbox "
                    "for an account made at sandbox.bingads.microsoft.com: everything "
                    "then goes to Microsoft's sandbox, where ads never run and nothing "
                    "is billed, and the connection check may add and delete a paused "
                    "test campaign there."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Microsoft" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Microsoft',
                docs='From your Entra app registration.',
            ),
        ),
        # BLOCK_PLACEMENT was removed. Website exclusions are set with
        # SetNegativeSitesToCampaigns / ToAdGroups, which *replace* the whole
        # exclusion list of each campaign; doing that safely needs a read of
        # every campaign's list first, and the old call (POST /NegativeSites
        # with an AccountId) matched no documented operation.
        capabilities=frozenset(
            {
                Capability.READ_AD_PERFORMANCE,
                Capability.WRITE_AD_BUDGET,
                Capability.READ_TRAFFIC_QUALITY,
            }
        ),
        requirements=(
            "A Microsoft Advertising developer token, and an Entra app "
            "registration with the msads.manage scope for the refresh token.",
        ),
        docs_url="https://learn.microsoft.com/en-us/advertising/guides/",
        base_url=CAMPAIGN_URL,
        # msads.manage is the Microsoft Advertising API's only scope;
        # offline_access (added by the provider) gives the refresh token.
        oauth=OAuthBinding(
            provider="microsoft",
            scopes=(LIVE_SCOPE,),
            # The developer token belongs to the platform, never the customer.
            platform_fields=frozenset({"clientId", "clientSecret", "developerToken"}),
            requires_settings=("microsoft_ads_developer_token",),
            access_summary=(
                "Access to the Microsoft Advertising account you choose, so agents can "
                "read results and prepare budget changes. Changes wait for your "
                "approval unless you turn that off."
            ),
            choice_noun="ad account",
            choice_fields=("customerId", "accountId"),
            # A sandbox user consents to the sandbox resource's scope at the
            # same common endpoint (the Bing Ads SDKs' sandbox default,
            # github.com/BingAds/BingAds-Python-SDK bingads/authorization.py).
            # Ticked, the connection is stored as a sandbox one, so its
            # refreshes, account list and calls all go to the sandbox.
            sign_in_inputs=(
                SignInInput(
                    key="sandbox",
                    label="This is a sandbox account",
                    kind="toggle",
                    help_text=(
                        "Tick this only for a Microsoft Advertising sandbox account, made at "
                        "sandbox.bingads.microsoft.com and signed in with an outlook.com "
                        "address. Sandbox ads never run and nothing is billed."
                    ),
                    field="accountEnvironment",
                    value_when_on=vh.SANDBOX,
                    scopes_when_on=(SANDBOX_SCOPE,),
                ),
            ),
        ),
    )

    token_url = TOKEN_URL

    # ── Environment ────────────────────────────────────────────────────────
    @property
    def sandbox(self) -> bool:
        return self.account_environment() == vh.SANDBOX

    @property
    def base_url(self) -> str:
        return SANDBOX_CAMPAIGN_URL if self.sandbox else super().base_url

    @property
    def reporting_url(self) -> str:
        return SANDBOX_REPORTING_URL if self.sandbox else REPORTING_URL

    @property
    def customer_url(self) -> str:
        return SANDBOX_CUSTOMER_URL if self.sandbox else CUSTOMER_URL

    @property
    def token_extra(self) -> dict[str, str]:  # type: ignore[override]
        """The refresh call's scope: a sandbox refresh token was issued for
        the sandbox resource, and asking for the live one is refused."""
        scope = SANDBOX_SCOPE if self.sandbox else LIVE_SCOPE
        return {"scope": f"{scope} offline_access"}

    def _developer_token(self) -> str:
        token = self.credentials.get("developerToken")
        if not token and self.sandbox:
            # The platform's own token is a production one; the sandbox
            # takes Microsoft's universal sandbox token from everybody.
            return SANDBOX_DEVELOPER_TOKEN
        if not token and from_platform_sign_in(self.credentials.values):
            # Signed in with the platform's app: the platform's token.
            token = platform_setting("microsoft_ads_developer_token")
        return token

    def _identity_headers(self) -> dict[str, str]:
        # The REST (JSON) endpoints take the token as a Bearer header.
        headers = {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }
        token = self._developer_token()
        if token:
            headers["DeveloperToken"] = token
        return headers

    def auth_headers(self) -> dict[str, str]:
        return {
            **self._identity_headers(),
            "CustomerId": self.credentials.require("customerId"),
            "CustomerAccountId": self.credentials.require("accountId"),
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def _customer_call(self, operation: str, body: dict) -> dict:
        """One Customer Management call, without the account headers the
        choose step does not have yet."""
        request = self._http().build_request(
            "POST", f"{self.customer_url}/{operation}", json=body, headers=self._identity_headers()
        )
        try:
            response = self._send(request)
        except httpx.HTTPError as exc:
            raise ConnectorError("Microsoft Advertising could not be reached. Try again.") from exc
        if response.status_code >= 400:
            log.warning("microsoft_ads %s refused: HTTP %s", operation, response.status_code)
            raise ConnectorError(
                "Microsoft Advertising did not list your accounts. Check that this "
                "Microsoft account has access to Microsoft Advertising."
            )
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Accounts of every customer the signed-in user has a role in."""
        user = self._customer_call("User/Query", {"UserId": None})
        choices: list[OAuthChoice] = []
        for customer in customer_ids(user)[:_MAX_CUSTOMERS]:
            accounts = self._customer_call(
                "AccountsInfo/Query", {"CustomerId": customer, "OnlyParentAccounts": False}
            )
            choices.extend(parse_accounts(customer, accounts))
        return choices

    @property
    def account_id(self) -> int:
        raw = self.credentials.require("accountId").strip()
        try:
            return int(raw)
        except ValueError:
            raise ConnectorConfigError(
                f"The Account ID on the Microsoft Ads integration ({raw!r}) should "
                "be the number Microsoft Advertising shows, without letters."
            ) from None

    # ── Reporting (async: submit → poll → download) ────────────────────────
    def _run_report(self, report_type: str, columns: list[str], *, days: int) -> list[dict]:
        end = date.today()
        start = end - timedelta(days=max(days, 1) - 1)
        submitted = self.request(
            "POST",
            f"{self.reporting_url}/GenerateReport/Submit",
            json_body={
                "ReportRequest": {
                    "Type": report_type,
                    "ReportName": f"Synaprank {report_type}",
                    "Format": "Csv",
                    "FormatVersion": "2.0",
                    "ExcludeReportHeader": True,
                    "ExcludeReportFooter": True,
                    "ExcludeColumnHeaders": False,
                    "ReturnOnlyCompleteData": False,
                    "Aggregation": "Summary",
                    "Columns": columns,
                    "Scope": {"AccountIds": [self.account_id]},
                    "Time": {
                        "CustomDateRangeStart": _ms_date(start),
                        "CustomDateRangeEnd": _ms_date(end),
                    },
                }
            },
        )
        request_id = (submitted or {}).get("ReportRequestId")
        if not request_id:
            raise ConnectorError("Microsoft Ads did not accept the report request.")

        for attempt in range(_MAX_POLLS):
            polled = self.request(
                "POST",
                f"{self.reporting_url}/GenerateReport/Poll",
                json_body={"ReportRequestId": request_id},
            )
            status = (polled or {}).get("ReportRequestStatus") or {}
            state = str(status.get("Status") or "")
            if state == "Success":
                url = status.get("ReportDownloadUrl")
                # No URL with Success means the report has no rows.
                return self._download(url) if url else []
            if state == "Error":
                raise ConnectorError("Microsoft Ads could not build the report.")
            if attempt < _MAX_POLLS - 1:
                time.sleep(_POLL_SECONDS)
        raise ConnectorError(
            "Microsoft Ads is still preparing the report. Try again in a few minutes."
        )

    def _download(self, url: str) -> list[dict]:
        # A pre-signed storage URL: sent without the API's auth headers.
        try:
            response = self._send(self._http().build_request("GET", url))
        except httpx.HTTPError as exc:
            raise ConnectorError("Could not download the Microsoft Ads report.") from exc
        if response.status_code >= 400:
            raise ConnectorError("Could not download the Microsoft Ads report.")
        raw = response.content
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = archive.namelist()
                if not names:
                    return []
                raw = archive.read(names[0])
        content = raw.decode("utf-8-sig", errors="replace")
        return list(csv.DictReader(io.StringIO(content)))

    def _live_performance(self, *, days: int) -> ChannelPerformance:
        rows = self._run_report(
            "AccountPerformanceReportRequest",
            ["AccountId", "Spend", "Impressions", "Clicks", "Conversions"],
            days=days,
        )
        if not rows:
            return ChannelPerformance(channel=self.channel)
        return ChannelPerformance(
            channel=self.channel,
            spend=round(sum(_number(r.get("Spend")) for r in rows), 2),
            impressions=int(sum(_number(r.get("Impressions")) for r in rows)),
            clicks=int(sum(_number(r.get("Clicks")) for r in rows)),
            conversions=int(sum(_number(r.get("Conversions")) for r in rows)),
        )

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        campaign_id = (self.credentials.get("campaignId") or "").strip()
        if not campaign_id:
            raise ConnectorConfigError(
                "Enter the Campaign ID on the Microsoft Ads integration to let the "
                "budget engine set its budget."
            )
        # PUT /Campaigns is UpdateCampaigns; POST would be AddCampaigns.
        result = self.request(
            "PUT",
            "/Campaigns",
            json_body={
                "AccountId": self.account_id,
                "Campaigns": [
                    {
                        "Id": int(campaign_id),
                        "DailyBudget": round(daily_budget, 2),
                        "BudgetType": "DailyBudgetStandard",
                    }
                ],
            },
        )
        errors = (result or {}).get("PartialErrors") or []
        if errors:
            first = errors[0] or {}
            raise ConnectorError(
                "Microsoft Ads refused the budget change: "
                + str(first.get("Message") or first.get("ErrorCode") or "unknown reason")
            )
        return True

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        rows = self._run_report(
            "PublisherUsagePerformanceReportRequest",
            ["PublisherUrl", "Clicks", "Impressions", "Spend"],
            days=7,
        )
        out: list[TrafficSample] = []
        for row in rows[:limit]:
            clicks = int(_number(row.get("Clicks")))
            impressions = int(_number(row.get("Impressions")))
            out.append(
                TrafficSample(
                    source=str(row.get("PublisherUrl") or "unknown site"),
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

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        """A cheap synchronous read — not a report, which takes a poll loop."""
        try:
            data = self.request(
                "POST",
                "/Campaigns/QueryByAccountId",
                json_body={"AccountId": self.account_id, "CampaignType": "Search"},
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        count = len((data or {}).get("Campaigns") or [])
        return HealthReport(
            ok=True, detail=f"Account reachable ({count} search campaign(s))", checked_at=utcnow()
        )

    # ── Live checks ────────────────────────────────────────────────────────
    @staticmethod
    def _refused(result: object, action: str) -> None:
        """Raise on the PartialErrors a batch operation answers with."""
        errors = [e for e in ((result or {}).get("PartialErrors") or []) if e]  # type: ignore[union-attr]
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            raise ConnectorError(
                f"Microsoft Ads would not {action}: "
                + str(first.get("Message") or first.get("ErrorCode") or "unknown reason")
            )

    def verify_steps(self) -> list:
        """Reach the accounts, the campaigns and today's report; in a sandbox
        account, add a paused campaign with a one-unit daily budget, read it
        back and delete it.

        The reads are the calls the sign-in's choose step, the health check
        and the budget and click-fraud agents make (``read_performance``).
        """

        def accounts() -> tuple[str, dict]:
            choices = self.list_oauth_choices()
            if not choices:
                raise ConnectorError(
                    "Microsoft Advertising answered, but this sign-in reaches no ad accounts."
                )
            mine = (self.credentials.get("accountId") or "").strip()
            if mine and not any(c.values.get("accountId") == mine for c in choices):
                raise ConnectorError(
                    "This sign-in reaches other ad accounts, but not the Account ID "
                    "on this connection."
                )
            return (
                f"This sign-in reaches {vh.plural(len(choices), 'ad account')}, "
                "including the one this connection manages.",
                {"accounts": len(choices), "account_id": mine, "environment": self.account_environment()},
            )

        def campaigns() -> tuple[str, dict]:
            data = self.request(
                "POST",
                "/Campaigns/QueryByAccountId",
                json_body={"AccountId": self.account_id, "CampaignType": "Search"},
            )
            rows = [c for c in ((data or {}).get("Campaigns") or []) if isinstance(c, dict)]
            return (
                f"Read the account's search campaigns: {vh.plural(len(rows), 'campaign')}.",
                {"campaigns": len(rows), "ids": [str(c.get("Id")) for c in rows[:10]]},
            )

        def performance() -> tuple[str, dict]:
            result = self.read_performance(days=1)
            return (
                "Ran today's account performance report and read it back.",
                {
                    "impressions": result.impressions,
                    "clicks": result.clicks,
                    "conversions": result.conversions,
                },
            )

        def campaign_roundtrip() -> tuple[str, dict]:
            vh.refuse_unless_test(self, "it adds a campaign and deletes it again.")
            if not self.sandbox:
                raise ConnectorConfigError(
                    "This check only runs in a Microsoft Advertising sandbox account, "
                    "where ads never run. Set 'Test account' to Sandbox first."
                )
            account = self.account_id

            def create() -> str:
                result = self.request(
                    "POST",
                    "/Campaigns",
                    json_body={
                        "AccountId": account,
                        "Campaigns": [
                            {
                                "Name": vh.marker("campaign"),
                                "CampaignType": "Search",
                                # Paused, with the smallest sensible budget,
                                # and no ad groups, keywords or ads: it
                                # cannot serve even if somebody resumed it.
                                "Status": "Paused",
                                "BudgetType": "DailyBudgetStandard",
                                "DailyBudget": 1.0,
                                "TimeZone": "PacificTimeUSCanadaTijuana",
                                "Languages": ["English"],
                            }
                        ],
                    },
                )
                self._refused(result, "add the test campaign")
                ids = (result or {}).get("CampaignIds") or []
                return str(ids[0]) if ids and ids[0] is not None else ""

            def check(campaign_id: str) -> None:
                data = self.request(
                    "POST",
                    "/Campaigns/QueryByIds",
                    json_body={
                        "AccountId": account,
                        "CampaignIds": [int(campaign_id)],
                        "CampaignType": "Search",
                    },
                )
                rows = [c for c in ((data or {}).get("Campaigns") or []) if isinstance(c, dict)]
                if not rows:
                    raise ConnectorError("Microsoft Ads did not return the new campaign.")
                if rows[0].get("Status") != "Paused":
                    raise ConnectorError(
                        f"The campaign came back as {rows[0].get('Status')!r}, not paused."
                    )

            def delete(campaign_id: str) -> None:
                result = self.request(
                    "DELETE",
                    "/Campaigns",
                    json_body={"AccountId": account, "CampaignIds": [int(campaign_id)]},
                )
                self._refused(result, "delete the test campaign")

            evidence = vh.create_check_delete(
                what="campaign", create=create, check=check, delete=delete
            )
            return (
                "Added a paused search campaign with a one-unit daily budget to the "
                "sandbox account, read it back, and deleted it.",
                {**evidence, "status": "Paused"},
            )

        return [
            vh.read_step("accounts", "List the ad accounts this sign-in reaches", accounts),
            vh.read_step("campaigns", "Read the account's search campaigns", campaigns),
            vh.read_step("performance", "Run today's account performance report", performance),
            vh.write_step(
                "campaign_roundtrip",
                "Add a paused campaign in the sandbox, read it, then delete it",
                campaign_roundtrip,
                requires_test_account=True,
            ),
        ]


CONNECTOR_CLASS = MicrosoftAdsConnector
