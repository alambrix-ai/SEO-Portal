"""Qualification rules for discovered placement targets.

Authority and topical relevance are both required. Kept separate from the
agent so the thresholds are testable and the link-farm heuristic is explicit.
"""
from __future__ import annotations

import re

import re

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:?#]+)", re.IGNORECASE)

# TLDs and name shapes typical of paid-link networks.
#: Matched after separators are stripped, so "buy-links-cheap.com",
#: "buylinks.com" and "buy.links.com" are all caught by "buylinks". Every
#: token here is six characters or more, because a short one produces false
#: positives once the hyphens are gone — and wrongly refusing a legitimate
#: publication withholds a real opportunity, which is the worse error.
_FARM_TOKENS = (
    "linkfarm",
    "buylinks",
    "buybacklinks",
    "sellinks",
    "seolinks",
    "cheaplinks",
    "cheapbacklinks",
    "linkexchange",
    "linkbuilding",
    "guestpostservice",
    "guestpostsforsale",
    "paidguestpost",
    "articledirectory",
    "articlesubmission",
    "bulkbacklinks",
    "backlinkspackage",
    "dofollowlinks",
)

#: Matched against the domain's own labels, where a short token is safe.
#: "pbn.example.com" and "example-pbn.net" are farms; "topbnb.com" is not.
_FARM_LABELS = ("pbn", "pbns")


def _flatten(domain: str) -> str:
    """The domain with every separator removed, for substring matching."""
    return re.sub(r"[^a-z0-9]", "", domain.lower())


def _labels(domain: str) -> set[str]:
    """Every dot- or hyphen-delimited part of the domain."""
    return {part for part in re.split(r"[.\-]", domain.lower()) if part}


def normalise_domain(value: str) -> str:
    if not value:
        return ""
    match = _DOMAIN_RE.match(value.strip().lower())
    domain = match.group(1) if match else value.strip().lower()
    # A trailing dot is valid DNS but never what a directory listing means.
    return domain.rstrip(".")


def looks_like_link_farm(domain: str) -> bool:
    """Heuristic for domains that sell links rather than earning them.

    Only the name is examined. This used to have a second arm — high
    authority with no topical connection — which was the better signal, and
    it is gone because the authority figure it read was invented by a
    language model. A dead branch inside a function called
    ``looks_like_link_farm`` is worse than no branch: it reads as protection
    that is not there.

    Nothing that arm caught now gets through, because the caller rejects
    anything below the relevance floor before reaching this, and that floor
    is higher than the 0.45 the arm tested.

    The real replacement is a measured authority source. Until one is
    connected, this is what can honestly be checked.
    """
    flat = _flatten(domain)
    if any(token in flat for token in _FARM_TOKENS):
        return True
    return bool(_labels(domain) & set(_FARM_LABELS))


def priority(authority: int, relevance: float) -> float:
    """Ranking score for the outreach queue.

    Relevance is weighted above authority: a well-matched DA-55 placement
    outperforms a mismatched DA-80 one, both for traffic and for safety.
    """
    return round((relevance * 0.6) + (min(authority, 100) / 100 * 0.4), 4)
