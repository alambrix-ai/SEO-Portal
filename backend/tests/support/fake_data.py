"""Deterministic data for the fake connectors.

This is a **test double**, not product code. It used to live in the
application as a "sandbox mode"; that was a demo affordance, and shipping a
fake-data generator inside a platform that reports marketing performance is a
liability — an operator could leave it enabled and read invented numbers as
real. So it lives here, where only the test suite can reach it.

Values are seeded from the organisation id and the day, so a test sees a
stable, self-consistent picture rather than numbers that change per call.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

from app.connectors.base.interfaces import (
    ChannelPerformance,
    CitationHit,
    RemotePage,
    SessionMetrics,
    TrafficSample,
)

def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(seed)


def _today_key() -> str:
    return date.today().isoformat()


# ── CMS ────────────────────────────────────────────────────────────────────
_PAGE_TEMPLATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("/inventory/2026-suv-lineup", "2026 SUV Lineup", ("Product", "FAQ")),
    ("/service/oil-change", "Oil Change Service", ("FAQ",)),
    ("/certified-pre-owned", "Certified Pre-Owned", ("ItemList",)),
    ("/financing/lease-offers", "Lease Offers", ("FAQ", "LocalBusiness")),
    ("/models/ev-crossover", "EV Crossover Page", ("Product",)),
    ("/locations/downtown", "Downtown Location", ("LocalBusiness",)),
    ("/parts/genuine-oem", "Genuine OEM Parts", ("Product", "FAQ")),
    ("/trade-in/value-estimator", "Trade-In Estimator", ("FAQ",)),
    ("/models/hybrid-sedan", "Hybrid Sedan", ("Product",)),
    ("/service/tyre-fitting", "Tyre Fitting", ("FAQ",)),
)


def _body_for(title: str, url: str, rng: random.Random) -> str:
    price = rng.randrange(18_000, 74_000, 500)
    return (
        f"<h1>{title}</h1>\n"
        f"<p>{title} at our dealership — what is included, what it costs, and how "
        "to arrange it.</p>\n"
        f"<p>Starting from ${price:,}.</p>\n"
        "<ul>\n"
        "  <li>Manufacturer warranty included</li>\n"
        "  <li>Multi-point inspection before handover</li>\n"
        "  <li>Finance and lease options available</li>\n"
        "</ul>\n"
        f"<p>Visit us to see the {title.lower()} in person, or book online.</p>"
    )


def pages(*, org_id: str, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
    """A listing, with no bodies — which is what every real connector returns.

    This used to hand back full bodies, word counts and schema types, and
    that single act of generosity hid a missing step for the entire life of
    the SEO pipeline. ``list_pages`` on WordPress, Shopify, Webflow, Magento,
    GitHub, Bitbucket and the Site Crawler returns none of those: a listing of
    two hundred pages would be two hundred reads. The body arrives from
    ``read_page``, and until recently nothing called it — so on real data
    every page had an empty body, every gap analysis was skipped, and every
    word count was nought.

    A double that is more capable than the thing it stands in for tests a
    system nobody has. This one is now exactly as thin as the contract.
    """
    out: list[RemotePage] = []
    for index, (url, title, schema) in enumerate(_PAGE_TEMPLATES):
        if path_prefix and not url.startswith(path_prefix):
            continue
        out.append(
            RemotePage(
                remote_id=f"fx-{index + 101}",
                url=url,
                # Real listings do carry a title; only the body is deferred.
                title=title,
                metadata={"fake": True, "writable": True},
            )
        )
    return out[:limit]


def page(*, org_id: str, remote_id: str) -> RemotePage:
    """One page, read in full — this is where the body comes from.

    The counterpart to the thin listing above: the same split every real
    connector has, so the read step is genuinely exercised.
    """
    for index, (url, title, schema) in enumerate(_PAGE_TEMPLATES):
        if f"fx-{index + 101}" != remote_id:
            continue
        rng = _rng(org_id, url)
        body = _body_for(title, url, rng)
        return RemotePage(
            remote_id=remote_id,
            url=url,
            title=title,
            body=body,
            word_count=len(body.split()),
            schema_types=list(schema),
            metadata={"fake": True, "writable": True},
        )
    rng = _rng(org_id, remote_id)
    title = "Fixture page"
    body = _body_for(title, "/fixture-page", rng)
    return RemotePage(
        remote_id=remote_id,
        url="/fixture-page",
        title=title,
        body=body,
        word_count=len(body.split()),
        metadata={"fake": True},
    )


# ── Ads ────────────────────────────────────────────────────────────────────
# Per-channel CAC bands, so the budget engine has a genuine spread to optimise
# against rather than noise around one number.
_CAC_BANDS: dict[str, tuple[float, float]] = {
    "google": (16.0, 21.0),
    "meta": (19.0, 24.0),
    "linkedin": (30.0, 39.0),
    "tiktok": (14.0, 18.0),
    "dsp": (24.0, 30.0),
}


def performance(*, org_id: str, channel: str, days: int = 7) -> ChannelPerformance:
    rng = _rng(org_id, channel, _today_key(), days)
    low, high = _CAC_BANDS.get(channel, (20.0, 28.0))
    cac = round(rng.uniform(low, high), 2)
    conversions = rng.randint(40, 320) * max(days // 7, 1)
    spend = round(cac * conversions, 2)
    clicks = conversions * rng.randint(18, 45)
    return ChannelPerformance(
        channel=channel,
        spend=spend,
        impressions=clicks * rng.randint(12, 30),
        clicks=clicks,
        conversions=conversions,
        cac=cac,
    )


_FRAUD_SOURCES: tuple[tuple[str, dict], ...] = (
    ("exchange-pool-4471", {"is_datacenter": True, "asn_type": "hosting"}),
    ("app-bundle-99213", {"fingerprint_repeat_count": 34, "has_mouse_events": False}),
    ("placement-7781", {"clicks_per_minute": 41.0, "ua_family": "chrome"}),
    ("mobile-sdk-2210", {"ua_family": "headless"}),
    ("audience-ext-556", {"geo_velocity_kmh": 1840.0}),
    ("retarget-pool-31", {"viewport_area": 1.0, "fingerprint_repeat_count": 12}),
    ("news-widget-882", {"asn_type": "residential", "has_mouse_events": True}),
    ("video-preroll-14", {"asn_type": "residential", "has_mouse_events": True}),
)


def traffic(*, org_id: str, channel: str, limit: int = 50) -> list[TrafficSample]:
    rng = _rng(org_id, channel, _today_key())
    out: list[TrafficSample] = []
    for source, signals in _FRAUD_SOURCES:
        suspicious = any(
            key in signals
            for key in ("is_datacenter", "fingerprint_repeat_count", "geo_velocity_kmh")
        ) or signals.get("ua_family") == "headless"
        out.append(
            TrafficSample(
                source=f"{source}",
                sessions=rng.randint(30, 260) if suspicious else rng.randint(4, 40),
                bounce_rate=round(rng.uniform(0.95, 1.0) if suspicious else rng.uniform(0.3, 0.7), 3),
                avg_session_seconds=round(
                    rng.uniform(0.2, 1.0) if suspicious else rng.uniform(25, 190), 1
                ),
                channel=channel,
                signals=dict(signals),
            )
        )
    rng.shuffle(out)
    return out[:limit]


# ── Analytics ──────────────────────────────────────────────────────────────
def sessions(*, org_id: str, days: int = 30) -> list[SessionMetrics]:
    rng = _rng(org_id, "sessions")
    today = date.today()
    out: list[SessionMetrics] = []
    # A gentle upward trend with weekday seasonality, so the Reports sparkline
    # shows a shape rather than a random walk.
    base = rng.randint(1_800, 2_600)
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        drift = 1 + ((days - offset) / days) * 0.35
        weekend = 0.72 if day.weekday() >= 5 else 1.0
        jitter = rng.uniform(0.92, 1.08)
        count = int(base * drift * weekend * jitter)
        conversions = max(1, int(count * rng.uniform(0.012, 0.028)))
        out.append(
            SessionMetrics(
                day=day,
                sessions=count,
                conversions=conversions,
                revenue=round(conversions * rng.uniform(240, 900), 2),
            )
        )
    return out


_SPAM_REFERRERS: tuple[tuple[str, bool], ...] = (
    ("best-cams-live.xyz", True),
    ("free-crypto-signals.top", True),
    ("hot-webcam-deals.click", True),
    ("seo-secrets-4u.info", True),
    ("xxx-tube-hits.online", True),
    ("rank-boost-now.top", True),
    ("semalt.com", True),
    ("news.ycombinator.com", False),
    ("reddit.com", False),
    ("autoblog-central.com", False),
    ("google.com", False),
)


def referrers(*, org_id: str, limit: int = 100) -> list[TrafficSample]:
    rng = _rng(org_id, "referrers", _today_key())
    out: list[TrafficSample] = []
    for domain, is_spam in _SPAM_REFERRERS:
        out.append(
            TrafficSample(
                source=domain,
                referrer=domain,
                sessions=rng.randint(6, 120) if is_spam else rng.randint(20, 400),
                bounce_rate=round(
                    rng.uniform(0.97, 1.0) if is_spam else rng.uniform(0.35, 0.68), 3
                ),
                avg_session_seconds=round(
                    rng.uniform(0.1, 0.9) if is_spam else rng.uniform(40, 210), 1
                ),
                channel="organic",
            )
        )
    return out[:limit]


def search_performance(*, org_id: str, days: int = 28) -> dict:
    rng = _rng(org_id, "search")
    queries = [
        "certified pre owned suv", "ev crossover lease deals", "oil change near me",
        "trade in value estimator", "2026 suv lineup", "genuine oem parts",
        "hybrid sedan mpg", "car finance calculator",
    ]
    rng.shuffle(queries)
    return {
        "top_queries": queries[:6],
        "clicks": rng.randint(2_400, 9_800),
        "impressions": rng.randint(80_000, 320_000),
        "average_position": round(rng.uniform(6.2, 18.4), 1),
        "days": days,
    }


def backlinks(*, org_id: str, limit: int = 100) -> list[dict]:
    rng = _rng(org_id, "backlinks", _today_key())
    stems = ["motortrend-forums", "carbuyersguide", "greenmilesblog", "evnewsdaily",
             "autoblog-central", "garagegearblog", "motorpressreview", "cityguide-autos"]
    out = []
    for stem in stems:
        tld = rng.choice(["com", "net", "org"])
        out.append(
            {
                "domain": f"{stem}.{tld}",
                "authority": rng.randint(48, 86),
                "anchor": rng.choice(
                    ["read the full review", "local dealer", "see the lineup", stem.replace("-", " ")]
                ),
                "url": f"https://{stem}.{tld}/article/{rng.randint(1000, 9999)}",
            }
        )
    return out[:limit]


# ── CRM ────────────────────────────────────────────────────────────────────
def conversions(*, org_id: str, days: int = 30) -> int:
    rng = _rng(org_id, "conversions", days)
    return rng.randint(320, 1_450)


_SIGNAL_PROFILES: tuple[tuple[str, ...], ...] = (
    ("inventory_view", "inventory_depth", "return_visit"),
    ("finance_calculator", "comparison_tool", "brochure_download"),
    ("service_booking", "call_click"),
    ("trade_in_valuation", "contact_form"),
    ("location_view", "inventory_view"),
    ("cart_started", "checkout_started", "purchase"),
)


def first_party_signals(*, org_id: str, limit: int = 500) -> list[dict]:
    """Aggregated interaction markers — no identifiers of any kind."""
    rng = _rng(org_id, "signals", _today_key())
    out: list[dict] = []
    for index in range(limit):
        profile = _SIGNAL_PROFILES[index % len(_SIGNAL_PROFILES)]
        out.append(
            {
                # A per-profile opaque key, stable only within this sample.
                "profile_key": f"fx-{index}",
                "signals": {
                    marker: rng.randint(1, 14) for marker in profile
                },
            }
        )
    return out


# ── Answer engines ─────────────────────────────────────────────────────────
def citations(*, org_id: str, engine: str, queries: list[str], domain: str) -> list[CitationHit]:
    rng = _rng(org_id, engine, _today_key())
    hits: list[CitationHit] = []
    for query in queries:
        # Not every query earns a citation; roughly a third do.
        if rng.random() > 0.34:
            continue
        hits.append(
            CitationHit(
                engine=engine,
                query=query,
                cited_url=f"https://{domain}{rng.choice([t[0] for t in _PAGE_TEMPLATES])}",
                position=rng.randint(1, 5),
            )
        )
    return hits
