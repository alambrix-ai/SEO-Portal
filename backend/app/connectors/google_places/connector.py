"""Google Places — where a local business appears in Google Maps searches.

Uses the Places API (New) Text Search endpoint, which returns the places
Google Maps lists for a text query, ranked by relevance, around a location.
That is what "salon near me" looks like for someone standing near the
business: the agent searches each phrase around the business's own map pin
and records where the business appears among the results.

What it is and is not, so nothing on screen overstates it:

* It is Google's own ranking for that query and area, from Google's own API.
* It is not the exact list any one person sees: Google personalises results
  by each searcher's precise location, history and device. A position here is
  a consistent measurement, made the same way every time, which is what makes
  week-on-week movement meaningful.
* Text Search returns at most 20 places per query, so "not in the results"
  means "not in the top 20".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    PlatformKey,
    SetupGuide,
    resolve_key,
)
from app.connectors.base.credentials import number, secret, text
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API = "https://places.googleapis.com/v1"
MAX_RESULTS = 20
#: The Places API accepts a bias radius of up to 50 km.
MAX_RADIUS_M = 50_000

_FIELDS = ",".join(
    f"places.{name}"
    for name in (
        "id",
        "displayName",
        "formattedAddress",
        "rating",
        "userRatingCount",
        "primaryTypeDisplayName",
        "googleMapsUri",
        "location",
    )
)


@dataclass(slots=True)
class LocalPlace:
    place_id: str
    name: str
    address: str = ""
    rating: float | None = None
    review_count: int | None = None
    category: str = ""
    maps_url: str = ""
    latitude: float | None = None
    longitude: float | None = None


@dataclass(slots=True)
class LocalSearch:
    phrase: str
    results: list[LocalPlace] = field(default_factory=list)

    def position_of(self, place_id: str) -> int | None:
        for index, place in enumerate(self.results, start=1):
            if place.place_id == place_id:
                return index
        return None


def _place(raw: dict) -> LocalPlace:
    location = raw.get("location") or {}
    rating = raw.get("rating")
    count = raw.get("userRatingCount")
    return LocalPlace(
        place_id=str(raw.get("id") or ""),
        name=str((raw.get("displayName") or {}).get("text") or ""),
        address=str(raw.get("formattedAddress") or ""),
        rating=float(rating) if rating is not None else None,
        review_count=int(count) if count is not None else None,
        category=str((raw.get("primaryTypeDisplayName") or {}).get("text") or ""),
        maps_url=str(raw.get("googleMapsUri") or ""),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
    )


GUIDE = SetupGuide(
    steps=(
        'Open Google Maps Platform credentials with the button below, and pick the project that has billing turned on.',
        'Click “Create credentials”, then “API key”. Make sure “Places API (New)” is enabled for the project.',
        'Copy the key and paste it here, then enter your business name as it appears on Google Maps.',
    ),
    key_url='https://console.cloud.google.com/google/maps-apis/credentials',
    key_url_label='Open Google Maps Platform credentials',
    key_field='apiKey',
    key_pattern='^AIza[0-9A-Za-z_\\-]{30,}$',
    key_hint='Google API keys start with AIza',
)


class GooglePlacesConnector(BaseConnector):
    spec = ConnectorSpec(
        slug="google_places",
        guide=GUIDE,
        name="Google Maps search (Places API)",
        category="Local business",
        description=(
            "Checks where your business appears when people search Google Maps "
            "for things like “salon near me”, and who appears above you."
        ),
        fields=(
            secret("apiKey", "API key", "AIza…"),
            text(
                "businessName",
                "Your business on Google Maps",
                "Geet unisex salon, Noida",
                help_text=(
                    "The name exactly as it appears on Google Maps, followed by the "
                    "area — this is how your listing is found."
                ),
            ),
            number(
                "radiusKm",
                "Search area (km)",
                "3",
                required=False,
                default="3",
                help_text=(
                    "How far around your business the searches cover. Most local "
                    "customers search within a few kilometres. Leave blank for 3 km."
                ),
            ),
        ),
        capabilities=frozenset({Capability.READ_LOCAL_RANKINGS}),
        requirements=(
            "A Google Cloud project with “Places API (New)” enabled and billing "
            "turned on. Google charges per search; see Google Maps Platform pricing.",
            "An API key from that project. If the key is restricted, allow the "
            "Places API (New).",
            "Your business must already appear on Google Maps.",
        ),
        docs_url="https://developers.google.com/maps/documentation/places/web-service/text-search",
        base_url=API,
        platform_key=PlatformKey(
            setting="platform_google_places_api_key",
            field="apiKey",
            summary=(
                "Searches Google Maps for your business and the phrases you track. "
                "Nothing on your listing is changed."
            ),
        ),
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "X-Goog-Api-Key": resolve_key(self.credentials, self.spec.platform_key),
            "X-Goog-FieldMask": _FIELDS,
            "Content-Type": "application/json",
        }

    @property
    def business_name(self) -> str:
        return self.credentials.require("businessName").strip()

    @property
    def radius_m(self) -> int:
        raw = (self.credentials.get("radiusKm") or "3").strip()
        try:
            km = float(raw)
        except ValueError as exc:
            raise ConnectorConfigError(f"Search area must be a number of kilometres, not {raw!r}") from exc
        return max(100, min(int(km * 1000), MAX_RADIUS_M))

    # ── Searches ───────────────────────────────────────────────────────────
    def _search(self, query: str, *, size: int, center: LocalPlace | None = None) -> list[LocalPlace]:
        body: dict = {"textQuery": query, "pageSize": size}
        if center is not None and center.latitude is not None and center.longitude is not None:
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": center.latitude, "longitude": center.longitude},
                    "radius": float(self.radius_m),
                }
            }
        data = self.request("POST", "/places:searchText", json_body=body)
        return [_place(raw) for raw in (data or {}).get("places") or [] if raw.get("id")]

    def find_business(self) -> LocalPlace:
        """The business's own listing, from the name and area entered."""
        found = self._search(self.business_name, size=1)
        if not found:
            raise ConnectorConfigError(
                f"Google Maps returned no listing for “{self.business_name}”. Enter the "
                "name exactly as it appears on Google Maps, followed by the area."
            )
        return found[0]

    def search_around(self, phrase: str, business: LocalPlace) -> LocalSearch:
        """The places Google lists for ``phrase`` around the business's pin."""
        return LocalSearch(phrase=phrase, results=self._search(phrase, size=MAX_RESULTS, center=business))

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """One Text Search for the business, as the local search tracker
        makes it. Google charges per search (the field list puts this in the
        Text Search Enterprise tier), so this is the only call."""
        from app.connectors.base import verify_helpers as vh

        def business() -> tuple[str, dict]:
            found = self.find_business()
            return (
                "Found the business's own listing on Google Maps.",
                {"place_id": found.place_id, "has_location": found.latitude is not None},
            )

        return [
            vh.read_step(
                "find_business",
                "Find the business on Google Maps with one search (paid: about 3.5 US cents, "
                "inside Google's free monthly allowance for most projects)",
                business,
            )
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            business = self.find_business()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True,
            detail=f"Found {business.name}" + (f" — {business.address}" if business.address else ""),
            checked_at=utcnow(),
        )

    def diagnose(self, error: Exception) -> str | None:
        message = str(error)
        if "403" in message or "PERMISSION_DENIED" in message:
            return (
                "Google refused the key. Enable “Places API (New)” on the key's Google "
                "Cloud project, make sure billing is on, and if the key is restricted, "
                "allow that API."
            )
        if "429" in message:
            return "Google is rate limiting this key. Try again later or raise the quota."
        return None


CONNECTOR_CLASS = GooglePlacesConnector
