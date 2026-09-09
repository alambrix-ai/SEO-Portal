"""Shared behaviour for ad-platform connectors.

Every ad platform exposes the same five things to the fleet — performance,
budget, creative upload, audience push, traffic quality — but each names them
differently and shapes the payloads differently. This base owns everything
that is genuinely common (capability declaration, the channel/connector
link, budget conversion, safe defaults), so each vendor file contains only
its own API surface.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, HealthReport
from app.connectors.base.interfaces import AdsConnector, ChannelPerformance, TrafficSample
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError
from app.core.logging import get_logger
from app.core.money import money
from app.db.base import utcnow

log = get_logger(__name__)

# The full capability set a mature ad connector offers. Vendors that cannot
# do one of these declare a narrower set and the agents route around them.
FULL_AD_CAPABILITIES = frozenset(
    {
        Capability.READ_AD_PERFORMANCE,
        Capability.WRITE_AD_BUDGET,
        Capability.UPLOAD_CREATIVE,
        Capability.PUSH_AUDIENCE,
        Capability.READ_TRAFFIC_QUALITY,
        Capability.BLOCK_PLACEMENT,
    }
)


class BaseAdsConnector(AdsConnector):
    """Safe defaults and shared plumbing for an ad platform.

    Subclasses implement ``_live_*`` methods for the calls they support. The
    """

    #: Monthly budget assumed when a platform needs an absolute figure and the
    #: caller only supplied a share. Overridden per installation via the
    #: ``monthlyBudget`` credential field where a connector offers one.
    default_monthly_budget = 10_000.0

    @property
    def monthly_budget(self) -> float:
        """The configured monthly budget, or the assumed default if blank.

        Digit grouping is stripped rather than rejected: somebody entering a
        budget in rupees types 10,00,000, and the field is right there next to
        figures the console renders exactly that way.

        A value that is not a number at all raises instead of falling back.
        The old code returned the 10,000 default on any ValueError, so
        "1,00,000" quietly became a tenth of the intended budget and pacing
        was wrong by that factor with nothing anywhere saying so.
        """
        raw = (self.credentials.get("monthlyBudget") or "").strip()
        if not raw:
            return self.default_monthly_budget
        cleaned = raw.replace(",", "").replace(" ", "").lstrip(
            settings.currency_symbol
        )
        try:
            return float(cleaned)
        except ValueError:
            raise ConnectorConfigError(
                f"Monthly budget {raw!r} on the {self.spec.name} connector is "
                "not a number. Enter the amount in figures — grouping like "
                "10,00,000 is fine."
            ) from None

    def daily_budget_for(self, percent: int) -> float:
        """Convert an allocation share into the daily figure platforms want."""
        return round((self.monthly_budget * (percent / 100)) / 30, 2)

    # ── Performance ────────────────────────────────────────────────────────
    def read_performance(self, *, days: int = 7) -> ChannelPerformance:
        return self._live_performance(days=days)

    def _live_performance(self, *, days: int) -> ChannelPerformance:
        raise NotImplementedError(f"{self.name} does not implement performance reads")

    # ── Budget ─────────────────────────────────────────────────────────────
    def set_budget_share(self, *, percent: int, daily_budget: float | None = None) -> bool:
        amount = daily_budget if daily_budget is not None else self.daily_budget_for(percent)
        return self._live_set_budget(percent=percent, daily_budget=amount)

    def _live_set_budget(self, *, percent: int, daily_budget: float) -> bool:
        raise NotImplementedError(f"{self.name} does not implement budget writes")

    # ── Creative ───────────────────────────────────────────────────────────
    def upload_creative(
        self,
        *,
        headline: str,
        body_copy: str,
        call_to_action: str,
        dimensions: str,
        audience_segment: str = "",
    ) -> str:
        return self._live_upload_creative(
            headline=headline,
            body_copy=body_copy,
            call_to_action=call_to_action,
            dimensions=dimensions,
            audience_segment=audience_segment,
        )

    def _live_upload_creative(
        self,
        *,
        headline: str,
        body_copy: str,
        call_to_action: str,
        dimensions: str,
        audience_segment: str,
    ) -> str:
        raise NotImplementedError(f"{self.name} does not accept uploaded creatives")

    # ── Audiences ──────────────────────────────────────────────────────────
    def push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        return self._live_push_audience(label=label, size=size, signals=signals)

    def _live_push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        raise NotImplementedError(f"{self.name} does not accept custom audiences")

    # ── Traffic quality ────────────────────────────────────────────────────
    def sample_traffic(self, *, limit: int = 50) -> list[TrafficSample]:
        try:
            return self._live_sample_traffic(limit=limit)
        except NotImplementedError:
            # A platform with no quality endpoint contributes nothing rather
            # than failing the fraud agent's whole run.
            return []

    def _live_sample_traffic(self, *, limit: int) -> list[TrafficSample]:
        raise NotImplementedError(f"{self.name} exposes no traffic-quality data")

    def block_placement(self, *, source: str, reason: str) -> bool:
        return self._live_block_placement(source=source, reason=reason)

    def _live_block_placement(self, *, source: str, reason: str) -> bool:
        raise NotImplementedError(f"{self.name} does not support placement exclusions")

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        return self._live_health()

    def _live_health(self) -> HealthReport:
        from app.core.exceptions import ConnectorError

        try:
            performance = self._live_performance(days=1)
        except NotImplementedError:
            return HealthReport(
                ok=True, detail="No health probe available", checked_at=utcnow()
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True,
            detail=f"Reporting reachable (spend {money(performance.spend, decimals=2)})",
            checked_at=utcnow(),
        )
