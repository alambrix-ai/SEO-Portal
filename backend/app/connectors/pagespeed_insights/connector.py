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

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    PlatformKey,
    SetupGuide,
    resolve_key,
    uses_platform_key,
)
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import PageSpeedConnector, PageVitals
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API = "https://www.googleapis.com"
#: Used when Strategy is blank. Google's own default is desktop, but ranking
#: is mobile-first, so mobile is the useful answer here.
DEFAULT_STRATEGY = "mobile"


GUIDE = SetupGuide(
    steps=(
        'Open the Credentials page in Google Cloud with the button below, and pick (or create) a project.',
        'Click “Create credentials”, then “API key”. Make sure the PageSpeed Insights API is enabled for the project (APIs & Services → Library).',
        'Copy the key and paste it here.',
    ),
    key_url='https://console.cloud.google.com/apis/credentials',
    key_url_label='Open Google Cloud credentials',
    key_field='apiKey',
    key_pattern='^AIza[0-9A-Za-z_\\-]{30,}$',
    key_hint='Google API keys start with AIza',
)


class PageSpeedInsightsConnector(PageSpeedConnector):
    spec = ConnectorSpec(
        slug="pagespeed_insights",
        guide=GUIDE,
        name="PageSpeed Insights",
        category="Analytics",
        description=(
            "Core Web Vitals from the Chrome UX Report — the field data behind "
            "the audit's mobile speed checks."
        ),
        fields=(
            secret(
                "apiKey",
                "API key",
                "AIza…",
                required=False,
                help_text=(
                    "Optional. Google answers a small number of requests without a "
                    "key; add one if you audit many pages or see rate-limit errors."
                ),
            ),
            text(
                "strategy",
                "Strategy",
                "mobile",
                required=False,
                default=DEFAULT_STRATEGY,
                help_text=(
                    "mobile or desktop. Leave blank to use mobile, which is what "
                    "Google ranks on."
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
        platform_key=PlatformKey(
            setting="platform_pagespeed_api_key",
            field="apiKey",
            summary=(
                "Speed results come from Google's public PageSpeed Insights. Nothing "
                "is read from or changed on your site."
            ),
        ),
    )

    @property
    def api_key(self) -> str:
        """The key to send, or ``""`` to call without one.

        A key is optional: "The API can be used with or without an API key,
        although a key is recommended for frequent, automated queries."
        https://developers.google.com/speed/docs/insights/v5/get-started
        """
        option = self.spec.platform_key
        if self.credentials.get("apiKey") or (option is not None and uses_platform_key(self.credentials.values)):
            return resolve_key(self.credentials, option)
        return ""

    @property
    def strategy(self) -> str:
        raw = (self.credentials.get("strategy") or "").strip().lower()
        return raw if raw in {"mobile", "desktop"} else DEFAULT_STRATEGY

    def read_vitals(self, url: str) -> PageVitals:
        data = self.request(
            "GET",
            "/pagespeedonline/v5/runPagespeed",
            params={
                "url": url,
                "strategy": self.strategy,
                # Only the performance category — the others are a much slower
                # call and the audit has its own checks for the rest.
                "category": "PERFORMANCE",
                **({"key": key} if (key := self.api_key) else {}),
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

    #: Google's own page: it always has field data, so the check does not
    #: depend on the customer's site.
    VERIFY_URL = "https://developers.google.com/speed"

    def verify_steps(self) -> list:
        """One PageSpeed run, exactly as the auditor makes it. Free (within
        Google's daily quota) and read-only."""
        from app.connectors.base import verify_helpers as vh

        def vitals() -> tuple[str, dict]:
            result = self.read_vitals(self.VERIFY_URL)
            measured = any(v is not None for v in (result.lcp_ms, result.cls, result.inp_ms))
            detail = (
                f"Ran PageSpeed Insights ({self.strategy}) on an example page and got real-visitor speed figures."
                if measured
                else f"Ran PageSpeed Insights ({self.strategy}) on an example page; Google had no real-visitor figures for it."
            )
            return detail, {
                "measured": measured,
                "origin_level_only": result.origin_level_only,
                "lcp_ms": result.lcp_ms,
                "inp_ms": result.inp_ms,
                "cls": result.cls,
            }

        return [vh.read_step("run_pagespeed", "Run one speed check on an example page (free)", vitals)]

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
