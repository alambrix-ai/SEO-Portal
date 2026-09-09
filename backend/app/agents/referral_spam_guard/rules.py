"""Deterministic referral-spam classification.

This carries the load. Static signals and behavioural heuristics settle the
vast majority of referrers for free, which keeps a continuously-running agent
cheap and its verdicts explainable — every block in the log traces to a named
signal rather than a model's opinion.

Only what these rules genuinely cannot decide is escalated to the model.
"""
from __future__ import annotations

import re
from enum import StrEnum

from app.connectors.base.interfaces import TrafficSample


class Verdict(StrEnum):
    CLEAN = "clean"
    UNKNOWN = "unknown"
    ADULT = "Adult / Porn spam"
    GHOST = "Ghost referral bot"
    LINK_DROP = "Spam / link-drop bot"
    CRYPTO = "Crypto scam referrer"
    SCRAPER = "Scraper / crawler farm"


# ── Signals ────────────────────────────────────────────────────────────────
_ADULT_TOKENS = (
    "porn", "xxx", "sex", "webcam", "cams", "adult", "nude", "escort",
    "hookup", "milf", "dating-hot", "fuck",
)
_CRYPTO_TOKENS = (
    "crypto", "bitcoin", "btc-", "forex", "binaryoption", "trading-signals",
    "airdrop", "nft-drop", "investment-profit",
)
_LINK_DROP_TOKENS = (
    "seo-", "-seo", "backlink", "traffic-bot", "buy-traffic", "rank-boost",
    "free-followers", "get-clicks", "linkbuild", "guestpost-cheap",
)
_SCRAPER_TOKENS = ("scrape", "crawler", "spider-", "proxy-list", "datacenter")

# TLDs that carry a disproportionate share of referral spam. A hit here is a
# contributing signal, never a block on its own — legitimate sites use them.
_SUSPECT_TLDS = frozenset(
    {"xyz", "top", "click", "online", "site", "loan", "work", "gq", "cf", "tk", "ml"}
)

# Domains that are frequently spoofed as ghost referrers.
_KNOWN_GHOSTS = frozenset(
    {
        "free-share-buttons.com", "free-social-buttons.com", "success-seo.com",
        "traffic2money.com", "hulfingtonpost.com", "best-seo-offer.com",
        "event-tracking.com", "get-free-traffic-now.com", "4webmasters.org",
        "semalt.com", "buttons-for-website.com", "share-buttons.xyz",
        "site3.free-share-buttons.com", "trafficmonetizer.org",
    }
)

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:?#]+)", re.IGNORECASE)


def normalise_domain(referrer: str) -> str:
    if not referrer:
        return ""
    match = _DOMAIN_RE.match(referrer.strip().lower())
    return match.group(1) if match else referrer.strip().lower()


def is_own_domain(domain: str, primary_domain: str) -> bool:
    """Self-referrals are a configuration issue, not spam."""
    if not primary_domain:
        return False
    own = normalise_domain(primary_domain)
    return domain == own or domain.endswith(f".{own}")


def parse_scope(scope: str) -> frozenset[str]:
    """Operator-supplied always-block domains from the Configure dialog."""
    if not scope:
        return frozenset()
    return frozenset(
        normalise_domain(part) for part in scope.split(",") if part.strip()
    )


def _token_verdict(domain: str) -> Verdict | None:
    for tokens, verdict in (
        (_ADULT_TOKENS, Verdict.ADULT),
        (_CRYPTO_TOKENS, Verdict.CRYPTO),
        (_LINK_DROP_TOKENS, Verdict.LINK_DROP),
        (_SCRAPER_TOKENS, Verdict.SCRAPER),
    ):
        if any(token in domain for token in tokens):
            return verdict
    return None


def classify(
    sample: TrafficSample, *, extra_blocklist: frozenset[str] = frozenset()
) -> tuple[Verdict, float]:
    """Return ``(verdict, confidence)`` for one referrer."""
    domain = normalise_domain(sample.referrer)
    if not domain:
        return Verdict.CLEAN, 0.0

    if domain in extra_blocklist:
        return Verdict.LINK_DROP, 1.0
    if domain in _KNOWN_GHOSTS:
        return Verdict.GHOST, 0.99

    token_hit = _token_verdict(domain)
    tld = domain.rsplit(".", 1)[-1]
    suspect_tld = tld in _SUSPECT_TLDS

    if token_hit is not None:
        # A spam token plus a spam TLD is about as certain as this gets.
        return token_hit, 0.98 if suspect_tld else 0.92

    # Behavioural signature of a ghost referral: traffic that never actually
    # loaded the page — a full bounce with effectively no time on site.
    ghost_shaped = (
        sample.bounce_rate >= 0.97
        and sample.avg_session_seconds <= 1.0
        and sample.sessions >= 3
    )
    if ghost_shaped:
        return Verdict.GHOST, 0.95 if suspect_tld else 0.88

    # Bot-shaped: many sessions, uniformly near-zero engagement.
    bot_shaped = (
        sample.sessions >= 25
        and sample.bounce_rate >= 0.9
        and sample.avg_session_seconds <= 3.0
    )
    if bot_shaped:
        return Verdict.SCRAPER, 0.85

    if suspect_tld and sample.bounce_rate >= 0.85:
        # Enough to be worth a model opinion, not enough to block outright.
        return Verdict.UNKNOWN, 0.5

    return Verdict.CLEAN, 0.0
