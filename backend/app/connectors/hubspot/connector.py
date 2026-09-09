"""HubSpot connector — CRM v3 for conversions and first-party signals.

Supplies two things nothing else can: the conversion counts that turn ad spend
into a real CAC, and the behavioural markers the audience modeller clusters.

Signals are aggregated *before* they leave HubSpot: this connector counts
events per contact and returns those counts, never names, emails or ids. The
audience modeller therefore never sees a person, only a shape.
"""
from __future__ import annotations

from datetime import timedelta

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import CrmConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

# HubSpot property names mapped onto the platform's signal vocabulary.
_SIGNAL_PROPERTIES: dict[str, str] = {
    "num_conversion_events": "purchase",
    "hs_analytics_num_page_views": "inventory_view",
    "hs_analytics_num_visits": "return_visit",
    "num_notes": "contact_form",
    "hs_analytics_num_event_completions": "comparison_tool",
    "hs_email_open": "email_open",
}


class HubSpotConnector(CrmConnector):
    spec = ConnectorSpec(
        slug="hubspot",
        name="HubSpot",
        category="CRM",
        description="Conversions and aggregated first-party engagement signals.",
        fields=(
            text(
                "hubId",
                "Hub ID",
                "12345678",
                help_text="Shown as Hub ID in the account menu; formerly Portal ID.",
            ),
            text(
                "wonStageId",
                "Closed-won stage ID",
                "closedwon",
                help_text=(
                    "The deal stage that counts as a conversion. Required: a "
                    "guessed stage id counts the wrong deals, and the number "
                    "looks plausible either way."
                ),
            ),
            secret(
                "accessToken",
                "Private app access token",
                "pat-na1-••••••••",
                help_text=(
                    "Settings → Integrations → Private apps → your app → "
                    "Auth. Needs the crm.objects.deals.read and "
                    "crm.objects.contacts.read scopes."
                ),
            ),
        ),
        capabilities=frozenset(
            {Capability.READ_CONVERSIONS, Capability.READ_FIRST_PARTY_SIGNALS}
        ),
        docs_url="https://developers.hubspot.com/docs/api/crm/contacts",
        base_url="https://api.hubapi.com",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("accessToken"),
            "Content-Type": "application/json",
        }

    # ── Conversions ────────────────────────────────────────────────────────
    def read_conversions(self, *, days: int = 30) -> int:
        since = int((utcnow() - timedelta(days=days)).timestamp() * 1000)
        data = self.request(
            "POST",
            "/crm/v3/objects/deals/search",
            json_body={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "dealstage",
                                "operator": "EQ",
                                "value": self.credentials.require("wonStageId"),
                            },
                            {
                                "propertyName": "closedate",
                                "operator": "GTE",
                                "value": str(since),
                            },
                        ]
                    }
                ],
                "limit": 1,
            },
        )
        # The search endpoint reports the full match count, so no paging is
        # needed just to count.
        return int((data or {}).get("total") or 0)

    # ── First-party signals ────────────────────────────────────────────────
    def read_first_party_signals(self, *, limit: int = 500) -> list[dict]:
        out: list[dict] = []
        after: str | None = None
        properties = list(_SIGNAL_PROPERTIES)

        while len(out) < limit:
            params: dict = {
                "limit": min(100, limit - len(out)),
                "properties": ",".join(properties),
            }
            if after:
                params["after"] = after

            data = self.request("GET", "/crm/v3/objects/contacts", params=params)
            results = (data or {}).get("results") or []
            if not results:
                break

            for row in results:
                props = row.get("properties") or {}
                signals = {}
                for hubspot_name, signal_name in _SIGNAL_PROPERTIES.items():
                    value = props.get(hubspot_name)
                    if value in (None, "", "0"):
                        continue
                    try:
                        signals[signal_name] = int(float(value))
                    except (TypeError, ValueError):
                        continue
                if not signals:
                    continue
                out.append(
                    {
                        # An opaque per-run key. The contact id is deliberately
                        # not carried through: the modeller has no use for it
                        # and it would put an identifier in the vector store.
                        "profile_key": f"hs-{len(out)}",
                        "signals": signals,
                    }
                )

            paging = ((data or {}).get("paging") or {}).get("next") or {}
            after = paging.get("after")
            if not after:
                break

        return out[:limit]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request(
                "GET", "/crm/v3/objects/contacts", params={"limit": 1}
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        reachable = isinstance(data, dict)
        return HealthReport(
            ok=reachable,
            detail="CRM reachable" if reachable else "Unexpected response",
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = HubSpotConnector
