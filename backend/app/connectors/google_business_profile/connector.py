"""Google Business Profile connector — local listing data, insights and reviews.

Local intent is where a multi-location business actually converts, so this
supplies the ground truth behind ``LocalBusiness`` schema — real opening
hours, real addresses — instead of letting a model invent them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import AnalyticsConnector, SessionMetrics
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Reviews live on the v4 My Business API; replies are written there too.
REVIEWS_API = "https://mybusiness.googleapis.com/v4"

_STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

#: Lists the accounts a person manages, for the sign-in "choose" step.
ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"


def parse_accounts(data: dict) -> list[tuple[str, str]]:
    """``[(accounts/…, display name)]`` from an ``accounts.list`` response."""
    out: list[tuple[str, str]] = []
    for account in data.get("accounts") or []:
        name = str(account.get("name") or "")
        if name.startswith("accounts/"):
            out.append((name, str(account.get("accountName") or name)))
    return out


def parse_locations(data: dict, account_name: str, account_label: str) -> list[OAuthChoice]:
    """Locations from ``accounts.locations.list``, each tied to its account
    (the account is what review reading and replying needs)."""
    out: list[OAuthChoice] = []
    for location in data.get("locations") or []:
        name = str(location.get("name") or "")
        if not name.startswith("locations/"):
            continue
        address = location.get("storefrontAddress") or {}
        line = ", ".join(
            part
            for part in [*(address.get("addressLines") or [])[:1], address.get("locality") or ""]
            if part
        )
        out.append(
            OAuthChoice(
                id=f"{account_name}/{name}",
                label=str(location.get("title") or name),
                detail=" · ".join(part for part in (line, account_label) if part),
                values={"locationId": name, "accountId": account_name},
            )
        )
    return out


@dataclass(slots=True)
class BusinessReview:
    #: accounts/…/locations/…/reviews/… — what a reply is written to.
    name: str
    reviewer: str
    stars: int
    comment: str
    created_at: datetime | None
    #: Empty when the business has not replied yet.
    reply: str = ""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GoogleBusinessProfileConnector(OAuthTokenMixin, AnalyticsConnector):
    spec = ConnectorSpec(
        slug="google_business_profile",
        name="Google Business Profile",
        category="Local business",
        description="Location details, opening hours and local search insights.",
        fields=(
            text("locationId", "Business location ID", "locations/1234567890"),
            text(
                "accountId",
                "Account ID",
                "accounts/1234567890",
                required=False,
                help_text=(
                    "Needed to read and reply to reviews. Shown in Business Profile "
                    "Manager, or returned by Google's accounts.list call."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Google" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=True,
                vendor='Google',
                docs='Scope: business.manage.',
            ),
        ),
        capabilities=frozenset(
            {Capability.READ_SESSIONS, Capability.READ_REVIEWS, Capability.REPLY_REVIEW}
        ),
        requirements=(
            "Google Business Profile API access approved for your Google Cloud "
            "project — Google asks you to apply for it before the API can be used.",
            "The Google account behind the token must be an owner or manager of the "
            "business listing.",
            "To read and reply to reviews, also enter the Account ID.",
        ),
        docs_url="https://developers.google.com/my-business",
        base_url="https://mybusinessbusinessinformation.googleapis.com/v1",
        # business.manage is the only Business Profile scope, and this
        # connector does write: it publishes replies to reviews.
        oauth=OAuthBinding(
            provider="google",
            scopes=("https://www.googleapis.com/auth/business.manage",),
            access_summary=(
                "Access to your Business Profile, so agents can read your listing, "
                "local search figures and reviews, and post the review replies you approve."
            ),
            choice_noun="business location",
            choice_fields=("locationId", "accountId"),
        ),
    )

    token_url = "https://oauth2.googleapis.com/token"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
        }

    @property
    def location_path(self) -> str:
        raw = self.credentials.require("locationId").strip()
        return raw if raw.startswith("locations/") else f"locations/{raw}"

    # ── Location facts ─────────────────────────────────────────────────────
    def read_location(self) -> dict:
        """The verified facts a LocalBusiness graph should be built from."""
        data = self.request(
            "GET",
            f"/{self.location_path}",
            params={
                "readMask": "title,storefrontAddress,phoneNumbers,regularHours,websiteUri"
            },
        )
        address_parts = (data or {}).get("storefrontAddress") or {}
        lines = address_parts.get("addressLines") or []
        return {
            "name": (data or {}).get("title") or "",
            "address": ", ".join([*lines, address_parts.get("locality") or ""]).strip(", "),
            "phone": ((data or {}).get("phoneNumbers") or {}).get("primaryPhone") or "",
            "opening_hours": self._format_hours((data or {}).get("regularHours") or {}),
            "website": (data or {}).get("websiteUri") or "",
        }

    @staticmethod
    def _format_hours(regular_hours: dict) -> str:
        """Condense the API's per-day periods into a schema.org hours string."""
        periods = regular_hours.get("periods") or []
        if not periods:
            return ""
        day_abbr = {
            "MONDAY": "Mo", "TUESDAY": "Tu", "WEDNESDAY": "We", "THURSDAY": "Th",
            "FRIDAY": "Fr", "SATURDAY": "Sa", "SUNDAY": "Su",
        }
        # The API omits zero-valued fields (proto3 JSON), so a missing hours
        # or minutes is 0 — openTime {} is midnight, not 09:00. The old code
        # substituted 09:00-18:00 for anything absent, which turned a
        # 24-hour location into office hours in its published schema.
        def _hm(value: object) -> tuple[int, int]:
            data = value if isinstance(value, dict) else {}
            return int(data.get("hours") or 0), int(data.get("minutes") or 0)

        rendered: list[str] = []
        for period in periods:
            day = day_abbr.get(str(period.get("openDay") or ""), "")
            if not day:
                continue
            open_h, open_m = _hm(period.get("openTime"))
            close_h, close_m = _hm(period.get("closeTime"))
            # A close of 00:00 (or an absent closeTime) ends the day at
            # midnight; Google itself writes that as 24:00, and schema.org
            # reads "Mo 09:00-00:00" as a zero-length period.
            if (close_h, close_m) == (0, 0):
                close_h = 24
            rendered.append(f"{day} {open_h:02d}:{open_m:02d}-{close_h:02d}:{close_m:02d}")
        return ", ".join(rendered)

    # ── Reviews ────────────────────────────────────────────────────────────
    @property
    def review_parent(self) -> str:
        raw = (self.credentials.get("accountId") or "").strip()
        if not raw:
            raise ConnectorConfigError(
                "Enter the Account ID on the Google Business Profile integration to "
                "read and reply to reviews."
            )
        account = raw if raw.startswith("accounts/") else f"accounts/{raw}"
        return f"{account}/{self.location_path}"

    def read_reviews(self, *, limit: int = 50) -> list[BusinessReview]:
        """The newest reviews, most recently updated first.

        The API pages at 50; ``nextPageToken`` is followed until ``limit``
        reviews are collected or there are no more.
        """
        parent = self.review_parent
        out: list[BusinessReview] = []
        token = ""
        while len(out) < limit:
            params: dict = {
                "pageSize": max(1, min(limit - len(out), 50)),
                "orderBy": "updateTime desc",
            }
            if token:
                params["pageToken"] = token
            data = self.request("GET", f"{REVIEWS_API}/{parent}/reviews", params=params)
            for raw in (data or {}).get("reviews") or []:
                name = str(raw.get("name") or "")
                if not name:
                    continue
                reviewer = raw.get("reviewer") or {}
                out.append(
                    BusinessReview(
                        name=name,
                        reviewer="" if reviewer.get("isAnonymous") else str(reviewer.get("displayName") or ""),
                        stars=_STARS.get(str(raw.get("starRating") or ""), 0),
                        comment=str(raw.get("comment") or ""),
                        created_at=_parse_time(raw.get("createTime")),
                        reply=str((raw.get("reviewReply") or {}).get("comment") or ""),
                    )
                )
            token = str((data or {}).get("nextPageToken") or "")
            if not token:
                break
        return out[:limit]

    def reply_to_review(self, review_name: str, comment: str) -> None:
        """Publish (or replace) the business's public reply to one review."""
        if not review_name.startswith("accounts/"):
            raise ConnectorError("That is not a Google review reference")
        self.request("PUT", f"{REVIEWS_API}/{review_name}/reply", json_body={"comment": comment})

    # ── Local insights ─────────────────────────────────────────────────────
    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        """Local discovery impressions, reported as a session series.

        Folded into the same shape as web sessions so the Reports screen can
        show local and organic discovery on one axis.
        """
        from datetime import date as date_cls
        from datetime import timedelta

        # The latest day or two is not yet reported, so the window ends
        # yesterday.
        end = date_cls.today() - timedelta(days=1)
        start = end - timedelta(days=max(days, 1) - 1)
        try:
            data = self._daily_impressions(start, end)
        except ConnectorConfigError:
            raise
        except ConnectorError as exc:
            log.info("Business Profile performance unavailable: %s", exc)
            return []

        totals: dict[date_cls, int] = {}
        for series in (data or {}).get("multiDailyMetricTimeSeries") or []:
            for entry in (series.get("dailyMetricTimeSeries") or []):
                for point in (entry.get("timeSeries") or {}).get("datedValues") or []:
                    raw = point.get("date") or {}
                    try:
                        day = date_cls(
                            int(raw.get("year")), int(raw.get("month")), int(raw.get("day"))
                        )
                    except (TypeError, ValueError):
                        continue
                    totals[day] = totals.get(day, 0) + int(point.get("value") or 0)

        return [
            SessionMetrics(day=day, sessions=count) for day, count in sorted(totals.items())
        ]

    def _daily_impressions(self, start: date, end: date) -> dict:
        """The raw ``fetchMultiDailyMetricsTimeSeries`` answer for search
        impressions from ``start`` to ``end`` (inclusive). Raises on failure;
        ``read_sessions`` turns a failure into an empty series."""
        # A GET with query parameters — the old POST without dailyRange was
        # rejected, so this always came back empty.
        return self.request(
            "GET",
            f"https://businessprofileperformance.googleapis.com/v1/{self.location_path}"
            ":fetchMultiDailyMetricsTimeSeries",
            params={
                # Repeated parameter: httpx sends one per list item.
                "dailyMetrics": [
                    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
                    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
                ],
                "dailyRange.startDate.year": start.year,
                "dailyRange.startDate.month": start.month,
                "dailyRange.startDate.day": start.day,
                "dailyRange.endDate.year": end.year,
                "dailyRange.endDate.month": end.month,
                "dailyRange.endDate.day": end.day,
            },
        ) or {}

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Every location under every account the signed-in person manages."""
        accounts = parse_accounts(
            self.request("GET", f"{ACCOUNTS_API}/accounts", params={"pageSize": 20}) or {}
        )
        choices: list[OAuthChoice] = []
        for account_name, account_label in accounts[:20]:
            data = self.request(
                "GET",
                f"/{account_name}/locations",
                params={"readMask": "name,title,storefrontAddress", "pageSize": 100},
            ) or {}
            choices.extend(parse_locations(data, account_name, account_label))
        return choices

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Reads only: the locations this sign-in manages, the chosen
        location's details, yesterday's search impressions and the newest
        reviews.

        There is deliberately no write step. The only thing this connector
        writes is a reply to a customer's review, and a reply is public the
        moment it is posted and Google tells the reviewer about it — deleting
        it afterwards does not take that back. Business Profile has no test
        or sandbox listings to do it on instead.
        """
        from app.connectors.base import verify_helpers as vh

        def locations() -> tuple[str, dict]:
            found = self.list_oauth_choices()
            configured = (self.credentials.get("locationId") or "").strip()
            configured = configured if configured.startswith("locations/") or not configured else f"locations/{configured}"
            detail = f"This sign-in manages {vh.plural(len(found), 'business location')}."
            if configured:
                listed = any(c.values.get("locationId") == configured for c in found)
                detail += " The chosen location is one of them." if listed else " The chosen location is not among them."
            accounts = {c.values.get("accountId") for c in found}
            return detail, {"locations": len(found), "accounts": len(accounts)}

        def location() -> tuple[str, dict]:
            facts = self.read_location()
            return (
                "Read the chosen location's name, address, phone, hours and website.",
                {
                    "location": self.location_path,
                    "has_address": bool(facts["address"]),
                    "has_hours": bool(facts["opening_hours"]),
                    "has_website": bool(facts["website"]),
                },
            )

        def impressions() -> tuple[str, dict]:
            day = vh.yesterday()
            data = self._daily_impressions(day, day)
            series = data.get("multiDailyMetricTimeSeries") or []
            total = sum(
                int(point.get("value") or 0)
                for block in series
                for entry in block.get("dailyMetricTimeSeries") or []
                for point in (entry.get("timeSeries") or {}).get("datedValues") or []
            )
            return (
                f"Read yesterday's Google Search impressions for the location: {total}.",
                {"impressions": total, "series": len(series)},
            )

        def reviews() -> tuple[str, dict]:
            found = self.read_reviews(limit=5)
            return (
                f"Read the {vh.plural(len(found), 'newest review')} (nothing was replied to).",
                {"count": len(found), "answered": sum(1 for r in found if r.reply)},
            )

        return [
            vh.read_step("list_locations", "List the business locations this sign-in manages", locations),
            vh.read_step("read_location", "Read the chosen location's details", location),
            vh.read_step("impressions_yesterday", "Read yesterday's search impressions for the location", impressions),
            vh.read_step("read_reviews", "Read the five newest reviews (needs the Account ID)", reviews),
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            location = self.read_location()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True,
            detail=f"Connected to {location.get('name') or self.location_path}",
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = GoogleBusinessProfileConnector
