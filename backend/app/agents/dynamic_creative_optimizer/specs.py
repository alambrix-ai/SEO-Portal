"""Platform creative specs, product extraction, and copy limits.

Every ad platform enforces its own dimensions and character limits. Encoding
them here means a variant is rejected before it is queued for a human, rather
than after a person approves something the platform will refuse.
"""
from __future__ import annotations

from app.agents.base.style import humanise

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.agents.base.context import AgentContext
    from app.connectors.base.interfaces import AdsConnector


# ── Platform specs ─────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PlatformSpec:
    connector_slug: str
    dimensions: tuple[str, ...]
    headline_max: int
    body_max: int


PLATFORMS: dict[str, PlatformSpec] = {
    "Google Ads": PlatformSpec("google_ads", ("300x250", "728x90", "160x600", "336x280"), 30, 90),
    "Meta Ads": PlatformSpec("meta_ads", ("1080x1080", "1200x628", "1080x1350"), 40, 125),
    "LinkedIn Ads": PlatformSpec("linkedin_ads", ("1200x627", "300x250"), 70, 150),
    "TikTok Ads": PlatformSpec("tiktok_ads", ("1080x1920",), 40, 100),
    "Microsoft Ads": PlatformSpec("microsoft_ads", ("300x250", "728x90"), 30, 90),
    "DSP / SSP Exchange": PlatformSpec("dsp_exchange", ("300x600", "970x250", "300x250"), 35, 100),
}

# Accepts the shorter names a model is likely to return.
_ALIASES = {
    "google": "Google Ads",
    "meta": "Meta Ads",
    "facebook": "Meta Ads",
    "instagram": "Meta Ads",
    "linkedin": "LinkedIn Ads",
    "tiktok": "TikTok Ads",
    "microsoft": "Microsoft Ads",
    "bing": "Microsoft Ads",
    "dsp": "DSP / SSP Exchange",
}


def canonical_platform(value: str) -> str | None:
    name = (value or "").strip()
    if name in PLATFORMS:
        return name
    key = name.lower().replace(" ads", "").strip()
    return _ALIASES.get(key)


def connector_for_platform(ctx: AgentContext, platform: str) -> AdsConnector | None:
    spec = PLATFORMS.get(platform)
    if spec is None:
        return None
    return ctx.optional_connector(spec.connector_slug)  # type: ignore[return-value]


def aspect_of(dimensions: str) -> str:
    try:
        width, height = (int(part) for part in dimensions.split("x", 1))
    except (ValueError, AttributeError):
        return "unknown"
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


# ── Product extraction ─────────────────────────────────────────────────────
@dataclass(slots=True)
class Product:
    name: str
    url: str
    price: str = ""
    features: list[str] = field(default_factory=list)
    category: str = ""


_PRICE_RE = re.compile(r"[$€£]\s?([0-9][0-9,]{2,})(?:\.\d{2})?")
_LIST_ITEM_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_product(*, title: str, url: str, body: str) -> Product:
    """Pull advertisable facts straight out of the page's own markup.

    This is what makes the creative track the site: the price in the ad is the
    price on the page, not a number someone typed into a campaign brief.
    """
    price_match = _PRICE_RE.search(body or "")
    features = [
        _TAG_RE.sub("", item).strip()
        for item in _LIST_ITEM_RE.findall(body or "")[:8]
    ]
    return Product(
        name=title.strip(),
        url=url,
        price=price_match.group(0).strip() if price_match else "",
        features=[f for f in features if 3 < len(f) < 120][:5],
        category=_category_of(f"{title} {url}"),
    )


def _category_of(text: str) -> str:
    lowered = text.lower()
    for needles, label in (
        (("service", "repair", "maintenance", "oil"), "service"),
        (("finance", "lease", "loan", "payment"), "finance"),
        (("part", "accessor"), "parts"),
        (("location", "dealer", "showroom"), "location"),
    ):
        if any(n in lowered for n in needles):
            return label
    return "product"


def commercial_score(title: str, url: str) -> float:
    """How advertisable a page looks, for choosing sources."""
    text = f"{title} {url}".lower()
    score = 0.0
    for token, weight in (
        ("model", 0.3), ("inventory", 0.3), ("offer", 0.25), ("lease", 0.25),
        ("price", 0.2), ("new", 0.15), ("certified", 0.15), ("service", 0.1),
    ):
        if token in text:
            score += weight
    for token in ("about", "careers", "privacy", "terms", "blog", "contact"):
        if token in text:
            score -= 0.5
    return score


# ── Variant validation ─────────────────────────────────────────────────────
@dataclass(slots=True)
class Variant:
    platform: str
    dimensions: str
    headline: str
    body_copy: str
    call_to_action: str
    audience_segment: str = ""


def normalise_variant(entry: dict, *, allowed: set[str]) -> Variant | None:
    """Coerce a model's variant into a valid one, or reject it."""
    platform = canonical_platform(str(entry.get("platform") or ""))
    if platform is None or platform not in allowed:
        return None

    spec = PLATFORMS[platform]
    dimensions = str(entry.get("dimensions") or "").strip()
    if dimensions not in spec.dimensions:
        # Snap to the platform's primary size rather than dropping otherwise
        # good copy over a dimension the model guessed.
        dimensions = spec.dimensions[0]

    headline = humanise(str(entry.get("headline") or "").strip())
    body_copy = humanise(str(entry.get("body_copy") or "").strip())
    cta = humanise(str(entry.get("call_to_action") or "").strip()) or "Learn more"
    if not headline or not body_copy:
        return None

    return Variant(
        platform=platform,
        dimensions=dimensions,
        headline=headline,
        body_copy=body_copy,
        call_to_action=cta[:80],
        audience_segment=str(entry.get("audience_segment") or "").strip()[:120],
    )


# Claims that need substantiation the platform will ask for.
_UNSUPPORTABLE = (
    "guaranteed", "best price", "lowest price", "no.1", "number one",
    "risk-free", "free money", "cure", "miracle",
)


def check_copy(*, headline: str, body_copy: str, platform: str) -> str | None:
    """Return a reason to reject the copy, or ``None`` if it will pass review."""
    spec = PLATFORMS.get(platform)
    if spec is None:
        return f"unknown platform {platform!r}"

    if len(headline) > spec.headline_max:
        return f"headline is {len(headline)} chars, {platform} allows {spec.headline_max}"
    if len(body_copy) > spec.body_max:
        return f"body is {len(body_copy)} chars, {platform} allows {spec.body_max}"

    combined = f"{headline} {body_copy}".lower()
    for claim in _UNSUPPORTABLE:
        if claim in combined:
            return f"contains an unsupportable claim: {claim!r}"

    if headline.isupper():
        return "headline is all caps, which most platforms reject"
    if combined.count("!") > 1:
        return "excessive exclamation marks"
    return None
