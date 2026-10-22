"""Adobe Analytics connector — Analytics 2.0 reporting API.

The enterprise alternative to GA4 for organisations already standardised on
Adobe Experience Cloud. It serves the same two jobs to the fleet: session
trend, and the referrer feed the spam guard assesses.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import httpx

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport, SetupGuide
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import AnalyticsConnector, SessionMetrics, TrafficSample
from app.core.config import settings
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Adobe IMS token endpoint for OAuth Server-to-Server credentials.
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
IMS_SCOPES = "openid,AdobeID,additional_info.projectedProductContext"
API = "https://analytics.adobe.io/api"
#: Lists the IMS orgs and Analytics companies the access token can reach.
DISCOVERY_URL = "https://analytics.adobe.io/discovery/me"

#: Adobe has no per-customer sign-in for the Analytics 2.0 API that a
#: platform could register once, so this stays a server-to-server
#: credential the customer's Adobe admin creates. The steps follow Adobe
#: Developer Console's own wording.
GUIDE = SetupGuide(
    steps=(
        "Ask an Adobe admin (a system or product admin for Adobe Analytics) to open "
        "Adobe Developer Console with the button below and click “Create new project”.",
        "Click “Add API”, choose “Adobe Analytics”, then “OAuth Server-to-Server”, tick "
        "a product profile that includes your report suite, and save.",
        "On the project's “OAuth Server-to-Server” credential page, copy the Client ID "
        "and click “Retrieve client secret” and copy it. Paste them below.",
        "In Adobe Analytics, Admin → Report suites shows the Report Suite ID. Paste it "
        "below. The Organization ID and Global Company ID are optional: left blank, "
        "they are read from Adobe.",
    ),
    key_url="https://developer.adobe.com/console",
    key_url_label="Open Adobe Developer Console",
)


class AdobeAnalyticsConnector(AnalyticsConnector):
    spec = ConnectorSpec(
        slug="adobe_analytics",
        advanced=True,
        guide=GUIDE,
        name="Adobe Analytics",
        category="Analytics & Search",
        description="Sessions and referrer detail from an Adobe Analytics report suite.",
        fields=(
            text(
                "orgId",
                "IMS Organization ID",
                "12345@AdobeOrg",
                required=False,
                help_text=(
                    "Adobe Developer Console → your project → Credentials. Leave blank "
                    "to read it from Adobe with the Client ID and Secret."
                ),
            ),
            text(
                "globalCompanyId",
                "Global Company ID",
                "yourcompany",
                required=False,
                help_text=(
                    "Every Analytics 2.0 endpoint carries this in the path. It is not "
                    "the report suite id. Leave blank to read it from Adobe "
                    "(GET /discovery/me) — the company that holds your report suite."
                ),
            ),
            text(
                "reportSuiteId",
                "Report Suite ID",
                "yourcompanyprod",
                help_text="The rsid reports are run against.",
            ),
            # Sent as x-api-key on every request, so not optional.
            text(
                "clientId",
                "Client ID",
                "",
                help_text=(
                    "From the project's OAuth Server-to-Server credential in Adobe "
                    "Developer Console."
                ),
            ),
            secret(
                "clientSecret",
                "Client Secret",
                "••••••••",
                help_text="From the same credential. Used to obtain access tokens.",
            ),
        ),
        capabilities=frozenset({Capability.READ_SESSIONS, Capability.READ_REFERRERS}),
        requirements=(
            "An Adobe Developer Console project with the Adobe Analytics API and an "
            "OAuth Server-to-Server credential (JWT credentials are retired).",
            "The credential's product profile must include the report suite.",
        ),
        docs_url="https://developer.adobe.com/analytics-apis/docs/2.0/",
        base_url=API,
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._token = ""
        self._token_expires = 0.0
        self._discovered: tuple[str, str] | None = None

    @property
    def access_token(self) -> str:
        """An IMS access token from the OAuth Server-to-Server credential.

        This used to require an ``accessToken`` credential that no field
        declared, so nothing could ever have supplied it and every call
        failed. Adobe's server-to-server credential is a client id and secret
        exchanged for a token that lives 24 hours; it is minted here and
        cached in memory until shortly before it expires.
        """
        now = time.time()
        if self._token and now < self._token_expires:
            return self._token
        client_id = self.credentials.require("clientId")
        client_secret = self.credentials.require("clientSecret")
        try:
            with self._ims_client() as client:
                response = client.post(
                    IMS_TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": IMS_SCOPES,
                    },
                )
        except httpx.HTTPError as exc:
            raise ConnectorError(
                "Could not reach Adobe to sign in. Check your connection and try again."
            ) from exc
        if response.status_code >= 400:
            log.warning(
                "Adobe IMS token exchange failed: HTTP %s %s",
                response.status_code,
                response.text[:300],
            )
            raise ConnectorError(
                "Adobe rejected the Client ID and Client Secret. Check they belong to "
                "an OAuth Server-to-Server credential with the Adobe Analytics API "
                "added to the project."
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        token = str(data.get("access_token") or "")
        if not token:
            raise ConnectorError("Adobe did not return an access token.")
        lifetime = float(data.get("expires_in") or 86_400)
        self._token = token
        self._token_expires = now + max(lifetime - 300, 30)
        return token

    @staticmethod
    def _ims_client() -> httpx.Client:
        return httpx.Client(timeout=settings.connector_timeout_seconds, verify=True)

    def _discover(self) -> tuple[str, str]:
        """``(globalCompanyId, imsOrgId)`` from Adobe, for blank settings.

        ``GET https://analytics.adobe.io/discovery/me`` lists the IMS orgs and
        Analytics companies the credential can reach, each with its
        ``globalCompanyId``. An entered company id picks its own org; otherwise
        one company is used as it is, and with several the one holding the
        configured report suite is chosen.
        https://developer.adobe.com/analytics-apis/docs/2.0/guides/endpoints/discovery
        """
        if self._discovered is not None:
            return self._discovered
        base = {"Authorization": "Bearer " + self.access_token, "x-api-key": self.credentials.require("clientId")}
        entered = (self.credentials.get("globalCompanyId") or "").strip()
        try:
            with self._ims_client() as client:
                response = client.get(DISCOVERY_URL, headers=base)
                data = response.json() if response.status_code < 400 else {}
                pairs = [
                    (str(company.get("globalCompanyId") or ""), str(org.get("imsOrgId") or ""))
                    for org in (data or {}).get("imsOrgs") or []
                    for company in org.get("companies") or []
                    if company.get("globalCompanyId")
                ]
                if entered:
                    pairs = [pair for pair in pairs if pair[0] == entered]
                elif len(pairs) > 1:
                    rsid = self.report_suite
                    pairs = [
                        pair for pair in pairs
                        if client.get(
                            f"{API}/{pair[0]}/reportsuites/collections/suites/{rsid}",
                            headers={**base, "x-proxy-global-company-id": pair[0]},
                        ).status_code < 400
                    ]
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(
                "Could not reach Adobe to look up the company for this credential. "
                "Try again, or enter the Global Company ID and Organization ID."
            ) from exc
        if not pairs:
            raise ConnectorError(
                "Adobe did not list an Analytics company for this credential that holds "
                "the report suite. Enter the Global Company ID."
            )
        self._discovered = pairs[0]
        return self._discovered

    @property
    def global_company_id(self) -> str:
        return (self.credentials.get("globalCompanyId") or "").strip() or self._discover()[0]

    @property
    def ims_org_id(self) -> str:
        """The IMS org header value: as entered, else discovered.

        Adobe's reference shows only the API key and bearer token as needed
        headers, so when a company id was entered and the lookup fails the
        org header is simply left off rather than failing the call.
        """
        configured = (self.credentials.get("orgId") or "").strip()
        if configured:
            return configured
        try:
            return self._discover()[1]
        except ConnectorError:
            if self.credentials.get("globalCompanyId"):
                return ""
            raise

    def auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.access_token,
            "x-api-key": self.credentials.require("clientId"),
            "x-proxy-global-company-id": self.global_company_id,
            "Content-Type": "application/json",
        }
        org = self.ims_org_id
        if org:
            headers["x-gw-ims-org-id"] = org
        return headers

    @property
    def report_suite(self) -> str:
        return self.credentials.require("reportSuiteId")

    def _report(self, body: dict) -> dict:
        # No fallback to the report suite id. They are different
        # identifiers, and substituting one for the other produced a 404 that
        # read like a permissions problem for as long as anybody looked.
        company = self.global_company_id
        return self.request("POST", f"/{company}/reports", json_body=body) or {}

    @staticmethod
    def _window(days: int) -> str:
        end = date.today()
        start = end - timedelta(days=days)
        return f"{start.isoformat()}T00:00:00.000/{end.isoformat()}T23:59:59.999"

    # ── Sessions ───────────────────────────────────────────────────────────
    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        data = self._report(
            {
                "rsid": self.report_suite,
                "globalFilters": [{"type": "dateRange", "dateRange": self._window(days)}],
                "metricContainer": {
                    "metrics": [
                        {"id": "metrics/visits", "columnId": "visits"},
                        {"id": "metrics/orders", "columnId": "orders"},
                        {"id": "metrics/revenue", "columnId": "revenue"},
                    ]
                },
                "dimension": "variables/daterangeday",
                "settings": {"limit": days, "page": 0},
            }
        )

        out: list[SessionMetrics] = []
        for row in (data or {}).get("rows") or []:
            values = row.get("data") or []
            raw = str(row.get("value") or "")
            parsed = self._parse_day(raw)
            if parsed is None:
                continue
            out.append(
                SessionMetrics(
                    day=parsed,
                    sessions=int(float(values[0] or 0)) if values else 0,
                    conversions=int(float(values[1] or 0)) if len(values) > 1 else 0,
                    revenue=round(float(values[2] or 0), 2) if len(values) > 2 else 0.0,
                )
            )
        return out

    @staticmethod
    def _parse_day(raw: str) -> date | None:
        """Adobe returns day labels in several formats depending on locale."""
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%d/%m/%Y"):
            try:
                from datetime import datetime

                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    # ── Referrers ──────────────────────────────────────────────────────────
    def read_referrers(self, *, days: int = 1, limit: int = 100) -> list[TrafficSample]:
        data = self._report(
            {
                "rsid": self.report_suite,
                "globalFilters": [
                    {"type": "dateRange", "dateRange": self._window(max(days, 1))}
                ],
                "metricContainer": {
                    "metrics": [
                        {"id": "metrics/visits", "columnId": "visits"},
                        {"id": "metrics/bouncerate", "columnId": "bounce"},
                        {"id": "metrics/averagetimespentonsite", "columnId": "dwell"},
                    ]
                },
                "dimension": "variables/referrerdomain",
                "settings": {"limit": limit, "page": 0},
            }
        )

        out: list[TrafficSample] = []
        for row in (data or {}).get("rows") or []:
            values = row.get("data") or []
            domain = str(row.get("value") or "")
            if not domain or domain.lower() in ("typed/bookmarked", "none", "unspecified"):
                continue
            out.append(
                TrafficSample(
                    source=domain,
                    referrer=domain,
                    sessions=int(float(values[0] or 0)) if values else 0,
                    bounce_rate=round(float(values[1] or 0) / 100, 3) if len(values) > 1 else 0.0,
                    avg_session_seconds=round(float(values[2] or 0), 1)
                    if len(values) > 2
                    else 0.0,
                    channel="organic",
                )
            )
        return out

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Sign in, find the company, list report suites, then run the two
        reports the agents run for yesterday. Read-only throughout."""
        from app.connectors.base import verify_helpers as vh

        def company() -> tuple[str, dict]:
            _ = self.access_token  # the sign-in itself (Adobe IMS)
            found = self.global_company_id
            source = "as entered" if (self.credentials.get("globalCompanyId") or "").strip() else "read from Adobe"
            return (
                f"Signed in to Adobe and found the Analytics company ({source}).",
                {"global_company_id": found},
            )

        def suites() -> tuple[str, dict]:
            company_id = self.global_company_id
            listing = self.request(
                "GET", f"/{company_id}/reportsuites/collections/suites", params={"limit": 10, "page": 0}
            ) or {}
            content = listing.get("content") or []
            total = int(listing.get("totalElements") or len(content))
            rsid = self.report_suite
            suite = self.request("GET", f"/{company_id}/reportsuites/collections/suites/{rsid}") or {}
            if str(suite.get("rsid") or "") != rsid:
                raise ConnectorError("Adobe did not return the chosen report suite for this sign-in.")
            return (
                f"This sign-in can read {vh.plural(total, 'report suite')}, including the chosen one.",
                {"count": total, "rsids": [str(s.get("rsid") or "") for s in content if isinstance(s, dict)]},
            )

        def sessions() -> tuple[str, dict]:
            rows = self.read_sessions(days=1)
            total = sum(r.sessions for r in rows)
            return (
                f"Read visits for yesterday and today: {total} over {vh.plural(len(rows), 'day')}.",
                {"days": len(rows), "visits": total},
            )

        def referrers() -> tuple[str, dict]:
            rows = self.read_referrers(days=1, limit=5)
            return (
                f"Read the top referring domains since yesterday: {vh.plural(len(rows), 'domain')}.",
                {"count": len(rows)},
            )

        return [
            vh.read_step("sign_in_company", "Sign in to Adobe and find the Analytics company", company),
            vh.read_step("list_report_suites", "List the report suites this sign-in can read", suites),
            vh.read_step("sessions_yesterday", "Read visits since yesterday", sessions),
            vh.read_step("referrers_yesterday", "Read the top five referring domains since yesterday", referrers),
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            self.read_sessions(days=1)
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True, detail=f"Report suite {self.report_suite} reachable", checked_at=utcnow()
        )


CONNECTOR_CLASS = AdobeAnalyticsConnector
