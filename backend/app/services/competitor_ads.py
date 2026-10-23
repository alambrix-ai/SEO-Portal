"""Fetch and normalise public Meta / Google ads-transparency creatives."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlencode

from app.core import egress, netguard
from app.llm import untrusted

PLATFORMS = frozenset({"meta", "google"})
#: Public library entry points (operator-configured query appended).
LIBRARY_URLS = {
    "meta": "https://www.facebook.com/ads/library/",
    "google": "https://adstransparency.google.com/",
}

FetchFn = Callable[[str], bytes]


@dataclass(frozen=True, slots=True)
class AdSnapshot:
    platform: str
    advertiser: str
    external_id: str
    headline: str
    body: str
    cta: str
    source_url: str

    @property
    def copy_text(self) -> str:
        parts = [p for p in (self.headline, self.body, self.cta) if p]
        return "\n".join(parts)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.copy_text.encode("utf-8")).hexdigest()


def library_url(platform: str, advertiser: str) -> str:
    platform = (platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")
    advertiser = (advertiser or "").strip()[:200]
    if not advertiser:
        raise ValueError("Advertiser query is required")
    base = LIBRARY_URLS[platform]
    if platform == "meta":
        return f"{base}?{urlencode({'active_status': 'all', 'q': advertiser, 'search_type': 'keyword_unordered'})}"
    return f"{base}?{urlencode({'query': advertiser})}"


def check_library_url(url: str) -> str:
    """Refuse private hosts / non-https before any egress."""
    return netguard.check_public_url(url)


def parse_library_payload(platform: str, advertiser: str, raw: bytes | str) -> list[AdSnapshot]:
    """Parse a JSON library response or a minimal HTML fallback list.

    Production libraries return HTML; connectors may supply JSON fixtures of
    the shape ``{"ads":[{"id","headline","body","cta","url"}]}``.
    """
    platform = platform.strip().lower()
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    ads: list[AdSnapshot] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("ads"), list):
        for item in data["ads"][:50]:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get("id") or item.get("external_id") or "").strip()[:128]
            if not external_id:
                continue
            source = str(item.get("url") or library_url(platform, advertiser))[:1000]
            ads.append(
                AdSnapshot(
                    platform=platform,
                    advertiser=advertiser[:200],
                    external_id=external_id,
                    headline=str(item.get("headline") or "")[:300],
                    body=str(item.get("body") or item.get("text") or "")[:4000],
                    cta=str(item.get("cta") or "")[:80],
                    source_url=source,
                )
            )
        return ads
    # HTML fallback: extract data-ad-id style markers only — never execute HTML.
    for match in re.finditer(r'data-ad-id=["\']([A-Za-z0-9_-]{4,64})["\']', text):
        ads.append(
            AdSnapshot(
                platform=platform,
                advertiser=advertiser[:200],
                external_id=match.group(1),
                headline="",
                body="",
                cta="",
                source_url=library_url(platform, advertiser)[:1000],
            )
        )
        if len(ads) >= 50:
            break
    return ads


def fetch_snapshots(
    platform: str,
    advertiser: str,
    *,
    fetch: FetchFn | None = None,
) -> list[AdSnapshot]:
    url = library_url(platform, advertiser)
    check_library_url(url)
    if fetch is not None:
        raw = fetch(url)
    else:
        raw = _egress_get(url)
    return parse_library_payload(platform, advertiser, raw)


def as_untrusted(snapshot: AdSnapshot) -> str:
    """Fence third-party ad copy before any model sees it."""
    return untrusted.wrap(
        "competitor_ad",
        f"PLATFORM: {snapshot.platform}\n"
        f"ADVERTISER: {snapshot.advertiser}\n"
        f"HEADLINE: {snapshot.headline}\n"
        f"BODY: {snapshot.body}\n"
        f"CTA: {snapshot.cta}",
    )


def _egress_get(url: str) -> bytes:
    check_library_url(url)
    with egress.pinned_client(timeout=20.0) as client:
        response = egress.fetch(client, "GET", url, max_bytes=512_000, truncate=True)
        return response.content


# Silence unused import warning if quote unused — keep for future path builders.
_ = quote
