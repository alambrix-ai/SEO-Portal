"""DSP / SSP exchange connector — programmatic buying over an OpenRTB seat.

Written against the shape most demand-side platforms share (a seat, campaigns,
line items, a deals endpoint and a supply-quality report) rather than one
vendor's dialect, with the host configurable per installation. That is what
lets a customer point it at their own DSP without a bespoke integration.

This is the connector the click-fraud agent leans on hardest: exchange supply
is where invalid traffic concentrates, so it is the one that must support
placement-level sampling and exclusion.
"""
from __future__ import annotations

from app.connectors.base.ads_base import FULL_AD_CAPABILITIES, BaseAdsConnector
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import monthly_budget, secret, text
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)


class DspExchangeConnector(BaseAdsConnector):
    channel = "dsp"

    spec = ConnectorSpec(
        slug="dsp_exchange",
        name="DSP / SSP Exchange",
        category="Ad Platforms",
        description=(
            "Programmatic buying through your own demand-side seat: bid pacing, "
            "supply-quality sampling and domain exclusions."
        ),
        fields=(
            text("seatId", "DSP seat ID", "seat-90210"),
            text("apiHost", "API host", "https://api.your-dsp.com/v1"),
            secret("apiKey", "API key", "••••••••"),
            monthly_budget(),
        ),
        capabilities=FULL_AD_CAPABILITIES,
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
        line_item = self.credentials.get("lineItemId")
        if not line_item:
            raise ConnectorError(
                "Set lineItemId on the DSP connector to let the budget engine pace it"
            )
        self.request(
            "PATCH",
            f"/seats/{self.seat_id}/line-items/{line_item}",
            json_body={
                "daily_budget": {"amount": round(daily_budget, 2), "currency": "USD"},
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
    ) -> str:
        width, _, height = dimensions.partition("x")
        response = self.request(
            "POST",
            f"/seats/{self.seat_id}/creatives",
            json_body={
                "name": f"AutoMarket — {headline[:40]} ({dimensions})",
                "type": "banner",
                "width": int(width or 300),
                "height": int(height or 250),
                "assets": {
                    "headline": headline,
                    "body": body_copy,
                    "cta": call_to_action,
                    "landing_url": self.credentials.get("finalUrl", ""),
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
                "name": f"AutoMarket — {label}"[:100],
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
        for row in (data or {}).get("placements") or []:
            out.append(
                TrafficSample(
                    source=str(row.get("placement_id") or row.get("domain") or "unknown supply"),
                    sessions=int(float(row.get("clicks") or 0)),
                    bounce_rate=float(row.get("bounce_rate") or 0.0),
                    avg_session_seconds=float(row.get("avg_dwell_seconds") or 0.0),
                    channel=self.channel,
                    # Exchange supply reports carry the richest fraud signals
                    # of any channel, so they are passed through in full.
                    signals={
                        "is_datacenter": bool(row.get("datacenter_traffic")),
                        "asn_type": row.get("asn_type") or "",
                        "fingerprint_repeat_count": int(
                            float(row.get("duplicate_fingerprints") or 0)
                        ),
                        "clicks_per_minute": float(row.get("click_rate_per_minute") or 0.0),
                        "geo_velocity_kmh": float(row.get("geo_velocity_kmh") or 0.0),
                        "viewport_area": float(row.get("viewport_area") or 0.0),
                        "has_mouse_events": bool(row.get("mouse_events", True)),
                    },
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


CONNECTOR_CLASS = DspExchangeConnector
