"""HubSpot connector — CRM v3 for conversions and first-party signals.

Supplies two things nothing else can: the conversion counts that turn ad spend
into a real CAC, and the behavioural markers the audience modeller clusters.

Signals are aggregated *before* they leave HubSpot: this connector counts
events per contact and returns those counts, never names, emails or ids. The
audience modeller therefore never sees a person, only a shape.
"""
from __future__ import annotations

from datetime import timedelta

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import CrmConnector
from app.connectors.base.oauth import OAuthTokenMixin
from app.core.exceptions import ConnectorConfigError, ConnectorError
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


def parse_won_stages(data: dict, *, hub_id: str = "") -> list[OAuthChoice]:
    """The closed-won stage of each deal pipeline (``GET /crm/v3/pipelines/deals``).

    A won stage is closed with a probability of 1. Each pipeline usually has
    exactly one, so a portal with one pipeline is connected without a pick.
    """
    out: list[OAuthChoice] = []
    for pipeline in data.get("results") or []:
        if pipeline.get("archived"):
            continue
        pipeline_label = str(pipeline.get("label") or pipeline.get("id") or "")
        for stage in pipeline.get("stages") or []:
            meta = stage.get("metadata") or {}
            closed = str(meta.get("isClosed") or "").lower() == "true"
            try:
                probability = float(meta.get("probability") or 0)
            except (TypeError, ValueError):
                probability = 0.0
            stage_id = str(stage.get("id") or "")
            if not stage_id or stage.get("archived") or not closed or probability < 1.0:
                continue
            values = {"wonStageId": stage_id}
            if hub_id:
                values["hubId"] = hub_id
            out.append(
                OAuthChoice(
                    id=stage_id,
                    label=f"{stage.get('label') or stage_id} — {pipeline_label}",
                    detail="Deals reaching this stage count as conversions",
                    values=values,
                )
            )
    return out


class HubSpotConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, CrmConnector):
    #: A HubSpot developer test account. Same API host as a live account, so
    #: nothing switches on it; it only marks the connection for the live checks.
    environments = (vh.TEST_ACCOUNT,)

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
                required=False,
                help_text=(
                    "Shown as Hub ID in the account menu; formerly Portal ID. "
                    "Optional: the access token already identifies the account."
                ),
            ),
            text(
                "wonStageId",
                "Closed-won stage ID",
                "closedwon",
                required=False,
                help_text=(
                    "The deal stage that counts as a conversion. Leave blank to "
                    "count deals in every pipeline's closed-won stage, read from "
                    "your HubSpot pipelines."
                ),
            ),
            vh.environment_field(
                allowed=(vh.TEST_ACCOUNT,),
                help_text=(
                    "Choose Test account only for a HubSpot developer test account "
                    "(made under Development, then Testing, then Test accounts in a "
                    "free HubSpot account). Leave blank for your real account."
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
        # Read-only: deal search and pipelines (deals.read), contact listing
        # (contacts.read). The account (Hub ID) comes back with the token.
        oauth=OAuthBinding(
            provider="hubspot",
            scopes=("crm.objects.deals.read", "crm.objects.contacts.read"),
            platform_fields=frozenset({"hubId"}),
            token_fields=frozenset({"accessToken", "refreshToken"}),
            access_summary=(
                "Read-only access to your HubSpot deals and contacts, so agents can count "
                "won deals and see engagement totals. No names or emails are copied, "
                "and nothing in HubSpot is changed."
            ),
            choice_noun="won deal stage",
            choice_fields=("wonStageId",),
        ),
    )

    token_url = "https://api.hubapi.com/oauth/v3/token"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The won stage of each deal pipeline in the connected account."""
        data = self.request("GET", "/crm/v3/pipelines/deals") or {}
        return parse_won_stages(data, hub_id=self.credentials.get("hubId"))

    # ── Conversions ────────────────────────────────────────────────────────
    def won_stage_ids(self) -> list[str]:
        """The stage ids that count as a conversion.

        The configured stage when there is one. Otherwise every pipeline's
        closed-won stage: HubSpot defines a deal stage with probability 1.0
        as Closed Won, and ``GET /crm/v3/pipelines/deals`` lists them — the
        same won stages a sign-in offers to pick from. That is read, not
        guessed, so a custom pipeline is counted correctly.
        https://developers.hubspot.com/docs/api-reference/crm-pipelines-v3/guide
        """
        configured = (self.credentials.get("wonStageId") or "").strip()
        if configured:
            return [configured]
        data = self.request("GET", "/crm/v3/pipelines/deals") or {}
        stages = list(dict.fromkeys(choice.id for choice in parse_won_stages(data)))
        if not stages:
            raise ConnectorConfigError(
                "HubSpot has no closed-won deal stage in any pipeline. Enter the "
                "Closed-won stage ID on this integration."
            )
        return stages

    def read_conversions(self, *, days: int = 30) -> int:
        since = int((utcnow() - timedelta(days=days)).timestamp() * 1000)
        stages = self.won_stage_ids()
        # One stage: EQ. Several (one per pipeline): IN with a values array.
        # https://developers.hubspot.com/docs/api-reference/search/guide
        stage_filter: dict = (
            {"propertyName": "dealstage", "operator": "EQ", "value": stages[0]}
            if len(stages) == 1
            else {"propertyName": "dealstage", "operator": "IN", "values": stages}
        )
        data = self.request(
            "POST",
            "/crm/v3/objects/deals/search",
            json_body={
                "filterGroups": [
                    {
                        "filters": [
                            stage_filter,
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

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read only, like the connector: the deal pipelines and their won
        stages, the won-deal search the conversions agent runs (limit 1), and
        one contact's engagement properties. Nothing is written."""

        def pipelines() -> tuple[str, dict]:
            data = self.request("GET", "/crm/v3/pipelines/deals") or {}
            rows = [p for p in data.get("results") or [] if not p.get("archived")]
            won = parse_won_stages(data)
            if not won and not (self.credentials.get("wonStageId") or "").strip():
                raise ConnectorConfigError(
                    "HubSpot has no closed-won deal stage in any pipeline. Enter the "
                    "Closed-won stage ID on this integration."
                )
            return (
                f"Read {vh.plural(len(rows), 'deal pipeline')} with "
                f"{vh.plural(len(won), 'closed-won stage')}.",
                {"pipelines": len(rows), "won_stages": len(won)},
            )

        def conversions() -> tuple[str, dict]:
            count = self.read_conversions(days=30)
            return (
                f"Counted {vh.plural(count, 'won deal')} closed in the last 30 days.",
                {"won_deals_30d": count},
            )

        def contacts() -> tuple[str, dict]:
            data = self.request(
                "GET",
                "/crm/v3/objects/contacts",
                params={"limit": 1, "properties": ",".join(_SIGNAL_PROPERTIES)},
            ) or {}
            rows = data.get("results") or []
            return (
                f"Read engagement totals for {vh.plural(len(rows), 'contact')} "
                "(counts only, no names or emails).",
                {"contacts": len(rows)},
            )

        return [
            vh.read_step("deal_pipelines", "Read the deal pipelines and their won stages", pipelines),
            vh.read_step("won_deals", "Count won deals from the last 30 days", conversions),
            vh.read_step("contact_signals", "Read one contact's engagement totals", contacts),
        ]

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
