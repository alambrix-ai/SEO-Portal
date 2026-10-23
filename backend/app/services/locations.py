"""Helpers for multi-location local SEO entities."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class LocationInput:
    name: str
    slug: str
    brand: str = ""
    region: str = ""
    city: str = ""
    place_id: str = ""
    gbp_location_id: str = ""
    lat: float | None = None
    lng: float | None = None


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", (name or "").lower()).strip("-")
    return (slug or "location")[:80]


def validate_location(
    *,
    name: str,
    slug: str = "",
    brand: str = "",
    region: str = "",
    city: str = "",
    place_id: str = "",
    gbp_location_id: str = "",
    lat: float | None = None,
    lng: float | None = None,
) -> LocationInput:
    name = (name or "").strip()[:200]
    if len(name) < 2:
        raise ValueError("Location name is required")
    if lat is not None and not (-90.0 <= float(lat) <= 90.0):
        raise ValueError("Latitude out of range")
    if lng is not None and not (-180.0 <= float(lng) <= 180.0):
        raise ValueError("Longitude out of range")
    return LocationInput(
        name=name,
        slug=(slugify(slug) if slug else slugify(name)),
        brand=(brand or "").strip()[:80],
        region=(region or "").strip()[:80],
        city=(city or "").strip()[:120],
        place_id=(place_id or "").strip()[:255],
        gbp_location_id=(gbp_location_id or "").strip()[:255],
        lat=float(lat) if lat is not None else None,
        lng=float(lng) if lng is not None else None,
    )


def default_phrases_for(city: str, name: str) -> list[str]:
    city = (city or "").strip()
    name = (name or "").strip()
    phrases = []
    if city and name:
        phrases.append(f"{name} {city}"[:160])
        phrases.append(f"{name} near {city}"[:160])
    elif name:
        phrases.append(f"{name} near me"[:160])
    return phrases[:5]
