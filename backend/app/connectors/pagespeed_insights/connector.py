"""PageSpeed Insights — Core Web Vitals from the Chrome UX Report.

The audit's speed checks need field data: what real devices on real networks
actually recorded. That cannot be computed from a page's HTML, which is why
this connector exists rather than the auditor estimating anything.

Two things this connector is careful about, both of which would otherwise put
a confident wrong number in front of a customer:

**Origin fallback.** When a URL has too little traffic to form its own sample,
the API answers with whole-site figures and sets ``origin_fallback``. Those
are real numbers, but they are not *that page's* numbers — so they are
reported as origin-level and the audit declines to attribute them to the page.

**Absent data.** A page with no field sample gets ``None``, not a zero and not
a lab estimate. ``None`` means unmeasured, and the audit says nothing about a
page it cannot measure.
"""
from __future__ import annotations

from typing import Any

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import PageSpeedConnector, PageVitals
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API = "https://www.googleapis.com"


class PageSpeedInsightsConnector(PageSpeedConnector):
    spec = ConnectorSpec(
        slug="pagespeed_insights",
        name="PageSpeed Insights",
        category="Analytics",
        description=(
            "Core Web Vitals from the Chrome UX Report — the field data behind "
            "the audit's mobile speed checks."
        ),
        fields=(
            secret("apiKey", "API key", "AIza…"),
            text(
                "strategy",
                "Strategy",
                "mobile",
                help_text=(
                    "mobile or desktop. Mobile is what Google ranks on, so it "
                    "is the default and usually the right answer."
                ),
            ),
        ),
        capabilities=frozenset({Capability.READ_PAGE_SPEED}),
        requirements=(
            "A Google API key with the PageSpeed Insights API enabled on its "
            "Cloud project.",
            "Field data only exists for URLs with enough real traffic to form "
            "a sample. Quieter pages come back unmeasured rather than good — "
            "the audit reports them as unmeasured.",
        ),
        docs_url="https://developers.google.com/speed/docs/insights/v5/get-started",
        base_url=API,
    )

    @property
    def strategy(self) -> str:
        return self.credentials.require("strategy").strip().lower()

    def read_vitals(self, url: str) -> PageVitals:
        data = self.request(
            "GET",
            "/pagespeedonline/v5/runPagespeed",
            params={
                "url": url,
                "strategy": self.strategy,
                "key": self.credentials.require("apiKey"),
                # Only the performance category — the others are a much slower
                # call and the audit has its own checks for the rest.
                "category": "PERFORMANCE",
            },
        )
        experience = (data or {}).get("loadingExperience") or {}
        metrics = experience.get("metrics") or {}
        if not metrics:
            # No field sample for this URL. Reported as unmeasured, which is
            # what it is.
            return PageVitals(url=url)

        return PageVitals(
            url=url,
            lcp_ms=_percentile(metrics, "LARGEST_CONTENTFUL_PAINT_MS"),
            # The API reports CLS ×100 as an integer, because the metric is
            # unitless and the field is typed as one.
            cls=_score(metrics, "CUMULATIVE_LAYOUT_SHIFT_SCORE"),
            inp_ms=_percentile(metrics, "INTERACTION_TO_NEXT_PAINT"),
            origin_level_only=bool(experience.get("origin_fallback")),
        )

    def check_health(self) -> HealthReport:
        # The API's own documentation URL is a page it will always have data
        # for, which makes it a probe that tests the key without depending on
        # the customer's site being reachable.
        try:
            self.read_vitals("https://developers.google.com/speed")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True, detail=f"Field data reachable ({self.strategy})", checked_at=utcnow()
        )

    def diagnose(self, error: Exception) -> str | None:
        text_error = str(error)
        if "403" in text_error:
            return (
                "Google rejected the key. Enable the PageSpeed Insights API on "
                "the key's Cloud project — a key that works for other Google "
                "APIs is not automatically allowed to call this one."
            )
        if "429" in text_error:
            return (
                "Google is rate limiting this key. PageSpeed Insights allows a "
                "few hundred requests a day by default; request a higher quota "
                "or audit fewer pages per run."
            )
        return None


def _percentile(metrics: dict[str, Any], key: str) -> int | None:
    value = (metrics.get(key) or {}).get("percentile")
    return int(value) if value is not None else None


def _score(metrics: dict[str, Any], key: str) -> float | None:
    value = (metrics.get(key) or {}).get("percentile")
    return round(int(value) / 100, 3) if value is not None else None


CONNECTOR_CLASS = PageSpeedInsightsConnector
