"""Salesforce connector — REST API v62 with SOQL for conversions and signals.

Same two jobs as the HubSpot connector, against the other CRM enterprises
actually run. Signals are aggregated inside the SOQL query where possible, so
per-person rows are never transferred in the first place.
"""
from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import urlsplit

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
    SignInInput,
)
from app.connectors.base.credentials import text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import CrmConnector
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API_VERSION = "v62.0"
DEFAULT_LOGIN_URL = "https://login.salesforce.com"
#: Pages of query results followed before stopping.
_MAX_PAGES = 20


def _soql_string(value: str) -> str:
    """A SOQL string literal, with backslashes and quotes escaped."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _signals_soql(row_limit: int) -> str:
    """Activity totals per contact and type: the query signals are read with."""
    return (
        "SELECT WhoId, Type, COUNT(Id) total FROM Task "
        "WHERE WhoId != null AND CreatedDate = LAST_N_DAYS:180 "
        f"GROUP BY WhoId, Type LIMIT {row_limit}"
    )


def _host(url: str) -> str:
    raw = (url or "").strip().lower()
    return urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""


class SalesforceConnector(OAuthTokenMixin, CrmConnector):
    spec = ConnectorSpec(
        slug="salesforce",
        name="Salesforce",
        category="CRM",
        description="Closed-won conversions and aggregated engagement signals via SOQL.",
        fields=(
            text(
                "instanceUrl",
                "Instance URL",
                "https://yourorg.my.salesforce.com",
                required=False,
                help_text=(
                    "Your org's address. Leave blank when you enter a refresh token: "
                    "Salesforce returns the address with every new access token."
                ),
            ),
            text(
                "leadSource",
                "Lead Source",
                "Web",
                required=False,
                help_text=(
                    "Written to the standard LeadSource field, so it has to "
                    "be one of that picklist's values or Salesforce rejects "
                    "the record."
                ),
            ),
            text(
                "loginUrl",
                "Login URL",
                DEFAULT_LOGIN_URL,
                required=False,
                default=DEFAULT_LOGIN_URL,
                help_text=(
                    "Where access tokens are obtained. Use "
                    "https://test.salesforce.com for a sandbox, or your My Domain "
                    "login URL if the org requires it."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Salesforce" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Salesforce',
                docs='From a connected app with the refresh_token scope.',
            ),
        ),
        capabilities=frozenset(
            {Capability.READ_CONVERSIONS, Capability.READ_FIRST_PARTY_SIGNALS}
        ),
        docs_url="https://developer.salesforce.com/docs/apis",
        base_url_field="instanceUrl",
        # api: the REST/SOQL calls; refresh_token: lasting access. The org's
        # address (instance_url) arrives with the token; the sandbox switch
        # picks test.salesforce.com as the login host.
        oauth=OAuthBinding(
            provider="salesforce",
            scopes=("api", "refresh_token"),
            platform_fields=frozenset({"clientId", "clientSecret", "instanceUrl", "loginUrl"}),
            access_summary=(
                "Lets agents count your won opportunities and activity totals with "
                "read-only queries. No contact names or emails are copied, and nothing "
                "in Salesforce is changed."
            ),
            choice_noun="org",
            sign_in_inputs=(
                SignInInput(
                    key="sandbox",
                    label="This is a sandbox",
                    kind="toggle",
                    help_text="Tick this only for a test (sandbox) org.",
                    field="loginUrl",
                ),
            ),
        ),
    )

    @property
    def token_url(self) -> str:  # type: ignore[override]
        """The org's token endpoint — a sandbox signs in at test.salesforce.com.

        Hardcoded to login.salesforce.com, a sandbox's refresh token was
        always rejected as invalid_grant.
        """
        base = (self.credentials.get("loginUrl") or DEFAULT_LOGIN_URL).strip().rstrip("/")
        if not base.startswith("http"):
            base = "https://" + base
        return f"{base}/services/oauth2/token"

    _instance_url: str = ""

    @property
    def base_url(self) -> str:  # type: ignore[override]
        """The org's REST host: as configured, else as Salesforce reported it.

        The token response — including the refresh-token flow — carries
        ``instance_url``, "the Salesforce instance to which API calls are
        sent", so a blank Instance URL is filled from it rather than guessed.
        https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_refresh_token_flow.htm&type=5
        """
        configured = (self.credentials.get("instanceUrl") or "").strip().rstrip("/")
        if configured:
            return configured if configured.startswith("http") else f"https://{configured}"
        if not self._instance_url:
            refresh = self.credentials.get("refreshToken")
            if not refresh:
                raise ConnectorConfigError(
                    "Salesforce needs a refresh token, or the Instance URL when only an "
                    "access token is entered."
                )
            self._token, self._token_expires = self._exchange(refresh, time.time())
        if not self._instance_url:
            raise ConnectorError(
                "Salesforce did not say which org address to use. Enter the Instance URL."
            )
        return self._instance_url

    def _accept(self, data: dict, used: str, now: float) -> tuple[str, float]:
        instance = str((data or {}).get("instance_url") or "").strip().rstrip("/")
        if instance.startswith("https://"):
            self._instance_url = instance
        return super()._accept(data, used, now)

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The one org the sign-in reached, named from its user info.

        There is nothing to pick — a Salesforce sign-in belongs to exactly
        one org — so this single choice connects without a picker.
        """
        instance = self.base_url
        try:
            info = self.request("GET", "/services/oauth2/userinfo") or {}
        except ConnectorError:
            info = {}
        username = str(info.get("preferred_username") or info.get("email") or "")
        org_id = str(info.get("organization_id") or "")
        host = instance.removeprefix("https://")
        sandbox = "test." in (self.credentials.get("loginUrl") or "")
        return [
            OAuthChoice(
                id=org_id or host,
                label=host,
                detail=" · ".join(
                    part for part in (("Sandbox" if sandbox else ""), username) if part
                ),
                values={},
            )
        ]

    def _query(self, soql: str) -> dict:
        return (
            self.request(
                "GET", f"/services/data/{API_VERSION}/query", params={"q": soql}
            )
            or {}
        )

    # ── Conversions ────────────────────────────────────────────────────────
    def read_conversions(self, *, days: int = 30) -> int:
        since = (utcnow() - timedelta(days=days)).date().isoformat()
        source = self.credentials.get("leadSource")
        # COUNT() keeps this one aggregate row rather than a page of records.
        clause = f"WHERE IsWon = true AND CloseDate >= {since}"
        if source:
            # Escaped: a value with a quote in it used to break the query, and
            # could change what it selected.
            clause += f" AND LeadSource = {_soql_string(source)}"
        data = self._query(f"SELECT COUNT() FROM Opportunity {clause}")
        return int(data.get("totalSize") or 0)

    # ── First-party signals ────────────────────────────────────────────────
    def read_first_party_signals(self, *, limit: int = 500) -> list[dict]:
        # Aggregated per contact by activity type. No names, emails or ids
        # leave Salesforce — only the counts the modeller needs.
        soql = _signals_soql(min(limit * 4, 2000))
        records: list[dict] = []
        try:
            data = self._query(soql)
            pages = 1
            records.extend(data.get("records") or [])
            # Results arrive in batches; nextRecordsUrl fetches the rest.
            while (
                not data.get("done", True)
                and data.get("nextRecordsUrl")
                and pages < _MAX_PAGES
            ):
                data = self.request("GET", str(data["nextRecordsUrl"])) or {}
                records.extend(data.get("records") or [])
                pages += 1
        except ConnectorConfigError:
            raise
        except ConnectorError as exc:
            log.info("Salesforce activity aggregation unavailable: %s", exc)
            if not records:
                return []

        # Salesforce activity types mapped onto the signal vocabulary.
        type_map = {
            "Call": "call_click",
            "Email": "email_open",
            "Web": "inventory_view",
            "Meeting": "contact_form",
            "Other": "return_visit",
        }

        grouped: dict[str, dict[str, int]] = {}
        for row in records:
            who = str(row.get("WhoId") or "")
            if not who:
                continue
            signal = type_map.get(str(row.get("Type") or "Other"), "return_visit")
            bucket = grouped.setdefault(who, {})
            bucket[signal] = bucket.get(signal, 0) + int(row.get("total") or 0)

        return [
            {"profile_key": f"sf-{index}", "signals": signals}
            for index, signals in enumerate(list(grouped.values())[:limit])
        ]

    # ── Live checks ────────────────────────────────────────────────────────
    def account_environment(self) -> str:
        """``sandbox`` / ``test_account`` / ``live``, read from the org's address.

        Salesforce already has a sandbox switch: the sign-in's "This is a
        sandbox" toggle (``sign_in_inputs`` key ``sandbox``) writes
        ``loginUrl = https://test.salesforce.com``. That, or a sandbox My
        Domain (``<domain>--<sandbox>.sandbox.my.salesforce.com``) as the login
        or instance address, means a sandbox — so no second "Test account"
        field is shown. A Developer Edition or scratch org
        (``*.develop.`` / ``*.scratch.my.salesforce.com``) is a free test
        org. Otherwise the optional ``accountEnvironment`` value decides, and
        blank means live. Host formats: Salesforce Help, "My Domain URL
        Formats" (enhanced domains, mandatory since Winter '24).
        """
        from app.connectors.base import verify_helpers as vh

        hosts = [
            _host(self.credentials.get("loginUrl") or ""),
            _host(self.credentials.get("instanceUrl") or self._instance_url),
        ]
        if hosts[0] == "test.salesforce.com" or any(
            h.endswith(".sandbox.my.salesforce.com") for h in hosts
        ):
            return vh.SANDBOX
        if any(
            h.endswith((".develop.my.salesforce.com", ".scratch.my.salesforce.com"))
            for h in hosts
        ):
            return vh.TEST_ACCOUNT
        return vh.normalise_environment(
            self.credentials.get("accountEnvironment") or "", (vh.TEST_ACCOUNT, vh.SANDBOX)
        )

    def verify_steps(self) -> list:
        """Read only, like the connector: the org's API limits, the closed-won
        COUNT() the conversions agent runs, and the activity aggregation the
        signals agent runs, capped at a few rows. Nothing is written."""
        from app.connectors.base import verify_helpers as vh

        def limits() -> tuple[str, dict]:
            data = self.request("GET", f"/services/data/{API_VERSION}/limits") or {}
            daily = data.get("DailyApiRequests") or {}
            if not daily:
                raise ConnectorError("The org answered, but did not report its API limits.")
            return (
                f"Reached the org; {daily.get('Remaining')} of {daily.get('Max')} "
                "API calls left today.",
                {"daily_api_remaining": daily.get("Remaining"), "daily_api_max": daily.get("Max")},
            )

        def conversions() -> tuple[str, dict]:
            count = self.read_conversions(days=30)
            source = " with the chosen Lead Source" if self.credentials.get("leadSource") else ""
            return (
                f"Counted {vh.plural(count, 'won opportunity', 'won opportunities')}{source} "
                "closed in the last 30 days.",
                {"won_opportunities_30d": count},
            )

        def signals() -> tuple[str, dict]:
            data = self._query(_signals_soql(5))
            rows = data.get("records") or []
            return (
                f"Read {vh.plural(len(rows), 'activity total')} (contact and type, no "
                "names) from the last 180 days, capped at 5.",
                {"rows": len(rows)},
            )

        return [
            vh.read_step("org_limits", "Reach the org and read its API limits", limits),
            vh.read_step(
                "won_opportunities", "Count won opportunities from the last 30 days", conversions
            ),
            vh.read_step(
                "activity_totals", "Read a few activity totals the audience agent uses", signals
            ),
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", f"/services/data/{API_VERSION}/limits")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        api_limits = (data or {}).get("DailyApiRequests") or {}
        remaining = api_limits.get("Remaining")
        return HealthReport(
            ok=True,
            detail=(
                f"Org reachable ({remaining} API calls left today)"
                if remaining is not None
                else "Org reachable"
            ),
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = SalesforceConnector
