"""DSP / SSP exchange connector — programmatic buying over an OpenRTB seat.

This is **not** any vendor's API. It is a contract of this platform's own
(the endpoints below), shaped like what most demand-side platforms offer — a
seat, line items, creatives, segments and a supply-quality report. A customer
points it at a DSP, or at an adapter in front of one, that implements exactly
this contract; against anything else every call fails.

    GET   {host}/seats/{seat}/reports/summary?lookback_days=&metrics=
          -> {"totals": {"spend", "impressions", "clicks", "conversions"}}
    PATCH {host}/seats/{seat}/line-items/{lineItemId}
          <- {"daily_budget": {"amount", "currency"}, "pacing": "even"}
    POST  {host}/seats/{seat}/creatives      -> {"id"}
    POST  {host}/seats/{seat}/segments       -> {"segment_id"}
    GET   {host}/seats/{seat}/reports/supply-quality?lookback_hours=&limit=
          -> {"placements": [{placement_id|domain, clicks, bounce_rate,
              avg_dwell_seconds, datacenter_traffic, asn_type,
              duplicate_fingerprints, click_rate_per_minute,
              geo_velocity_kmh, viewport_area, mouse_events}]}
    POST  {host}/seats/{seat}/blocklist      <- {"entries": [{placement_id, reason}]}

Every request carries ``Authorization: Bearer {apiKey}`` and ``X-Seat-Id``.

This is the connector the click-fraud agent leans on hardest: exchange supply
is where invalid traffic concentrates, so it is the one that must support
placement-level sampling and exclusion.
"""
from __future__ import annotations

from app.connectors.base.ads_base import FULL_AD_CAPABILITIES, BaseAdsConnector
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import monthly_budget, secret, text
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorConfigError
from app.core.logging import get_logger

log = get_logger(__name__)


class DspExchangeConnector(BaseAdsConnector):
    channel = "dsp"

    spec = ConnectorSpec(
        slug="dsp_exchange",
        advanced=True,
        name="DSP / SSP Exchange",
        category="Ad Platforms",
        description=(
            "Programmatic buying through your own demand-side seat: bid pacing, "
            "supply-quality sampling and domain exclusions. Your endpoint must "
            "implement this connector's documented contract."
        ),
        fields=(
            text("seatId", "DSP seat ID", "seat-90210"),
            text(
                "apiHost",
                "API host",
                "https://api.your-dsp.com/v1",
                help_text=(
                    "The base URL of a service that implements this connector's "
                    "contract — a DSP that does, or an adapter in front of yours."
                ),
            ),
            secret("apiKey", "API key", "••••••••"),
            text(
                "lineItemId",
                "Line item ID",
                "li-12345",
                required=False,
                help_text=(
                    "The line item whose daily budget the budget engine paces. "
                    "Leave blank to keep budgets manual."
                ),
            ),
            text(
                "currency",
                "Budget currency",
                "USD",
                required=False,
                default="USD",
                help_text="ISO currency code the seat is billed in, e.g. USD, EUR, INR.",
            ),
            text(
                "finalUrl",
                "Default landing page URL",
                "https://www.example.com/",
                required=False,
                help_text=(
                    "Where creatives link when they are not made for a specific "
                    "page. A creative made for a page always links to that page."
                ),
            ),
            monthly_budget(),
        ),
        capabilities=FULL_AD_CAPABILITIES,
        requirements=(
            "This is a generic contract, not a specific vendor's API: the API host "
            "must implement the endpoints documented for this connector (reports, "
            "line items, creatives, segments, supply quality, blocklist).",
        ),
        base_url_field="apiHost",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("apiKey"),
            "X-Seat-Id": self.credentials.require("seatId"),
            "Content-Type": "application/json",
        }

    @property
    def seat_id(self) -> str:
        return self.credentials.require("seatId")

    # ── Reporting ──────────────────────────────────────────────────────────
    def _live_performance(self, *, days: int) -> ChannelPerformance:
        data = self.request(
            "GET",
            f"/seats/{self.seat_id}/reports/summary",
            params={"lookback_days": days, "metrics": "spend,impressions,clicks,conversions"},
        )
        totals = (data or {}).get("totals") or data or {}
        return ChannelPerformance(
            channel=self.channel,
            spend=round(float(totals.get("spend") or 0), 2),
            impressions=int(float(totals.get("impressions") or 0)),
            clicks=int(float(totals.get("clicks") or 0)),
            conversions=int(float(totals.get("conversions") or 0)),
        )

    # ── Budgets ────────────────────────────────────────────────────────────
    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        line_item = (self.credentials.get("lineItemId") or "").strip()
        if not line_item:
            raise ConnectorConfigError(
                "Enter the Line item ID on the DSP / SSP Exchange integration to "
                "let the budget engine pace it."
            )
        currency = (self.credentials.get("currency") or "USD").strip().upper()
        self.request(
            "PATCH",
            f"/seats/{self.seat_id}/line-items/{line_item}",
            json_body={
                "daily_budget": {"amount": round(daily_budget, 2), "currency": currency},
                "pacing": "even",
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
        width, _, height = dimensions.lower().partition("x")
        response = self.request(
            "POST",
            f"/seats/{self.seat_id}/creatives",
            json_body={
                "name": f"Synaprank — {headline[:40]} ({dimensions})",
                "type": "banner",
                "width": int(width) if width.strip().isdigit() else 300,
                "height": int(height) if height.strip().isdigit() else 250,
                "assets": {
                    "headline": headline,
                    "body": body_copy,
                    "cta": call_to_action,
                    "landing_url": final_url or self.credentials.get("finalUrl", ""),
                },
                "audience_hint": audience_segment,
                # Exchanges run their own creative review; nothing serves
                # until both that and our approval have passed.
                "status": "pending_review",
            },
        )
        return str((response or {}).get("id") or "")

    # ── Audiences ──────────────────────────────────────────────────────────
    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        response = self.request(
            "POST",
            f"/seats/{self.seat_id}/segments",
            json_body={
                "name": f"Synaprank — {label}"[:100],
                # Only the definition and an approximate size are sent.
                "definition": {"first_party_signals": signals[:8]},
                "estimated_size": size,
                "ttl_days": 180,
            },
        )
        return str((response or {}).get("segment_id") or "")

    # ── Traffic quality ────────────────────────────────────────────────────
    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        data = self.request(
            "GET",
            f"/seats/{self.seat_id}/reports/supply-quality",
            params={"lookback_hours": 24, "limit": limit},
        )
        out: list[TrafficSample] = []
        for row in ((data or {}).get("placements") or [])[:limit]:
            signals = {
                "is_datacenter": bool(row.get("datacenter_traffic")),
                "asn_type": row.get("asn_type") or "",
                "fingerprint_repeat_count": int(
                    float(row.get("duplicate_fingerprints") or 0)
                ),
                "clicks_per_minute": float(row.get("click_rate_per_minute") or 0.0),
                "geo_velocity_kmh": float(row.get("geo_velocity_kmh") or 0.0),
                "viewport_area": float(row.get("viewport_area") or 0.0),
            }
            # Absent means unknown, not "has mouse events": the detector
            # only acts on an explicit False, so a report that does not
            # carry the field must not be read as evidence either way.
            if row.get("mouse_events") is not None:
                signals["has_mouse_events"] = bool(row.get("mouse_events"))
            out.append(
                TrafficSample(
                    source=str(row.get("placement_id") or row.get("domain") or "unknown supply"),
                    sessions=int(float(row.get("clicks") or 0)),
                    bounce_rate=float(row.get("bounce_rate") or 0.0),
                    avg_session_seconds=float(row.get("avg_dwell_seconds") or 0.0),
                    channel=self.channel,
                    # Exchange supply reports carry the richest fraud signals
                    # of any channel, so they are passed through in full.
                    signals=signals,
                )
            )
        return out

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        self.request(
            "POST",
            f"/seats/{self.seat_id}/blocklist",
            json_body={"entries": [{"placement_id": source, "reason": reason}]},
        )
        log.info("Blocklisted %s on the exchange (%s)", source, reason)
        return True

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read the two reports the agents read. No write step.

        This connector's contract has no way to undo what it writes: no
        delete or pause for creatives, segments or blocklist entries, and the
        only budget call changes a real line item's pacing. A write step that
        cannot remove what it made would break the "leave it as you found it"
        rule, so only the reads are checked.
        """
        from app.connectors.base import verify_helpers as vh

        def performance() -> tuple[str, dict]:
            result = self.read_performance(days=1)
            return (
                f"Read the seat's summary for the last day: {result.impressions} "
                f"impressions, {result.clicks} clicks.",
                {"impressions": result.impressions, "clicks": result.clicks, "spend": result.spend},
            )

        def supply() -> tuple[str, dict]:
            samples = self.sample_traffic(limit=5)
            return (
                f"Read the supply-quality report ({vh.plural(len(samples), 'placement')}).",
                {"count": len(samples)},
            )

        return [
            vh.read_step("read_performance", "Read the seat's summary report", performance),
            vh.read_step("traffic_quality", "Read the supply-quality report", supply),
        ]


CONNECTOR_CLASS = DspExchangeConnector
