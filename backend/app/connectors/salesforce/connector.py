"""Salesforce connector — REST API v62 with SOQL for conversions and signals.

Same two jobs as the HubSpot connector, against the other CRM enterprises
actually run. Signals are aggregated inside the SOQL query where possible, so
per-person rows are never transferred in the first place.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import CrmConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API_VERSION = "v62.0"


class SalesforceConnector(OAuthTokenMixin, CrmConnector):
    spec = ConnectorSpec(
        slug="salesforce",
        name="Salesforce",
        category="CRM",
        description="Closed-won conversions and aggregated engagement signals via SOQL.",
        fields=(
            text("instanceUrl", "Instance URL", "https://yourorg.my.salesforce.com"),
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
    )

    token_url = "https://login.salesforce.com/services/oauth2/token"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

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
            clause += f" AND LeadSource = '{source}'"
        data = self._query(f"SELECT COUNT() FROM Opportunity {clause}")
        return int(data.get("totalSize") or 0)

    # ── First-party signals ────────────────────────────────────────────────
    def read_first_party_signals(self, *, limit: int = 500) -> list[dict]:
        # Aggregated per contact by activity type. No names, emails or ids
        # leave Salesforce — only the counts the modeller needs.
        soql = (
            "SELECT WhoId, Type, COUNT(Id) total FROM Task "
            "WHERE WhoId != null AND CreatedDate = LAST_N_DAYS:180 "
            f"GROUP BY WhoId, Type LIMIT {limit * 4}"
        )
        try:
            data = self._query(soql)
        except ConnectorError as exc:
            log.info("Salesforce activity aggregation unavailable: %s", exc)
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
        for row in data.get("records") or []:
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
