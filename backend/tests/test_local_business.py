"""Local business: Google Maps positions and Google review replies."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.agents.local_search_tracker.agent import AGENT as TRACKER
from app.agents.local_search_tracker.agent import default_phrases
from app.agents.review_responder import prompts
from app.agents.review_responder.agent import AGENT as RESPONDER
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.google_business_profile.connector import GoogleBusinessProfileConnector
from app.connectors.google_places.connector import GooglePlacesConnector
from app.core.exceptions import ConnectorConfigError
from app.models.local import LocalRankCheck, ReviewReply


def _place(pid: str, name: str, rating: float = 4.5, count: int = 100) -> dict:
    return {
        "id": pid,
        "displayName": {"text": name},
        "rating": rating,
        "userRatingCount": count,
        "primaryTypeDisplayName": {"text": "Hair salon"},
        "location": {"latitude": 28.57, "longitude": 77.32},
    }


def _places(handler, **values) -> GooglePlacesConnector:  # noqa: ANN001, ANN003
    creds = {"apiKey": "k", "businessName": "Geet unisex salon, Noida", "radiusKm": "3", **values}
    connector = GooglePlacesConnector(Credentials(values=creds), org_id="o")
    connector._client = httpx.Client(base_url="https://places.googleapis.com/v1", transport=httpx.MockTransport(handler))
    return connector


# ── Google Places ────────────────────────────────────────────────────────────
def test_search_sends_the_documented_request_and_finds_the_position():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/places:searchText"
        assert request.headers["X-Goog-Api-Key"] == "k"
        assert "places.userRatingCount" in request.headers["X-Goog-FieldMask"]
        body = json.loads(request.content)
        seen.append(body)
        if body["textQuery"].startswith("Geet"):
            return httpx.Response(200, json={"places": [_place("me", "Geet unisex salon", 4.9, 111)]})
        return httpx.Response(
            200,
            json={"places": [_place("a", "Toni&Guy"), _place("b", "Alpha"), _place("me", "Geet unisex salon")]},
        )

    places = _places(handler)
    me = places.find_business()
    search = places.search_around("salon near me", me)
    assert search.position_of("me") == 3
    assert search.position_of("nobody") is None
    bias = seen[1]["locationBias"]["circle"]
    assert bias["radius"] == 3000.0 and bias["center"]["latitude"] == 28.57
    assert seen[1]["pageSize"] == 20


def test_a_business_google_cannot_find_is_a_clear_setup_error():
    places = _places(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ConnectorConfigError, match="no listing"):
        places.find_business()
    report = places.check_health()
    assert report.ok is False and "Google Maps" in report.detail


def test_radius_is_clamped_to_what_the_api_accepts():
    assert _places(lambda r: httpx.Response(200), radiusKm="900").radius_m == 50_000
    assert _places(lambda r: httpx.Response(200), radiusKm="0").radius_m == 100


# ── Google Business Profile reviews ──────────────────────────────────────────
def _profile(handler, account: str = "accounts/9") -> GoogleBusinessProfileConnector:  # noqa: ANN001
    values = {"locationId": "locations/1", "accountId": account, "accessToken": "t"}
    connector = GoogleBusinessProfileConnector(Credentials(values=values), org_id="o")
    connector._client = httpx.Client(transport=httpx.MockTransport(handler))
    return connector


def test_reviews_are_read_and_replied_to_on_the_v4_endpoints(monkeypatch):
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.content))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "reviews": [
                        {
                            "name": "accounts/9/locations/1/reviews/r1",
                            "reviewer": {"displayName": "Asha"},
                            "starRating": "FIVE",
                            "comment": "Great haircut",
                            "createTime": "2026-10-01T10:00:00Z",
                        },
                        {
                            "name": "accounts/9/locations/1/reviews/r2",
                            "reviewer": {"isAnonymous": True, "displayName": "x"},
                            "starRating": "TWO",
                            "reviewReply": {"comment": "Sorry"},
                        },
                    ]
                },
            )
        return httpx.Response(200, json={})

    profile = _profile(handler)
    monkeypatch.setattr(type(profile), "access_token", property(lambda self: "t"))
    reviews = profile.read_reviews()
    assert [r.stars for r in reviews] == [5, 2]
    assert reviews[0].reviewer == "Asha" and reviews[0].reply == ""
    assert reviews[1].reviewer == "" and reviews[1].reply == "Sorry"
    assert calls[0][1].startswith("https://mybusiness.googleapis.com/v4/accounts/9/locations/1/reviews")

    profile.reply_to_review("accounts/9/locations/1/reviews/r1", "Thank you, Asha!")
    method, url, body = calls[-1]
    assert method == "PUT"
    assert url == "https://mybusiness.googleapis.com/v4/accounts/9/locations/1/reviews/r1/reply"
    assert json.loads(body) == {"comment": "Thank you, Asha!"}


def test_reviews_need_the_account_id():
    with pytest.raises(ConnectorConfigError, match="Account ID"):
        _ = _profile(lambda r: httpx.Response(200), account="").review_parent


# ── Reply safety check ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "reply",
    [
        "Thanks! Enjoy 20% off next time.",
        "So sorry — we will give you a full refund.",
        "Call us on +91 98765 43210 to sort it out.",
        "See www.example.com for our offers.",
        "Hi",
    ],
)
def test_unsafe_or_empty_replies_are_discarded(reply):
    assert prompts.check_reply(reply) is not None


def test_a_normal_reply_passes():
    assert prompts.check_reply("Thank you so much, Asha! We're glad you loved your haircut. — Geet unisex salon") is None


# ── Agents ───────────────────────────────────────────────────────────────────
class _Rows:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def scalars(self):
        return iter(self.rows)


class _FakeDb:
    def __init__(self, existing: list | None = None) -> None:
        self.added: list = []
        self.existing = existing or []

    def add(self, row) -> None:  # noqa: ANN001
        if getattr(row, "id", None) is None:
            row.id = f"row{len(self.added)}"
        self.added.append(row)

    def flush(self) -> None:
        pass

    def execute(self, statement):  # noqa: ANN001
        return _Rows(self.existing)


class _Cipher:
    def encrypt(self, value, *, context):  # noqa: ANN001
        return f"enc:{value}"


def _ctx(connector, db: _FakeDb, scope: str = "", answers: list | None = None):  # noqa: ANN001
    answers = list(answers or [])
    return SimpleNamespace(
        db=db,
        scope=scope,
        run_id="run1",
        tenant_id="t",
        remaining_actions=10,
        org=SimpleNamespace(name="Geet unisex salon"),
        cipher=_Cipher(),
        progress=lambda *a, **k: None,
        log_warning=lambda *a, **k: None,
        with_capability=lambda capability: connector,
        ask_json=lambda *a, **k: answers.pop(0),
    )


def test_tracker_records_each_phrase_and_reports_the_position():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["textQuery"].startswith("Geet"):
            return httpx.Response(200, json={"places": [_place("me", "Geet unisex salon", 4.9, 111)]})
        if body["textQuery"] == "salon near me":
            return httpx.Response(200, json={"places": [_place("a", "Toni&Guy"), _place("me", "Geet")]})
        return httpx.Response(200, json={"places": [_place("a", "Toni&Guy")]})

    db = _FakeDb()
    result = TRACKER.run(_ctx(_places(handler), db, scope="salon near me, unisex salon"))
    checks = [row for row in db.added if isinstance(row, LocalRankCheck)]
    assert [(c.phrase, c.position) for c in checks] == [("salon near me", 2), ("unisex salon", None)]
    assert checks[0].rating == 4.9 and checks[0].review_count == 111
    assert checks[0].top_results[0]["name"] == "Toni&Guy"
    assert result.actions == []  # it only reports
    assert "#2 of 2" in result.summary and "not in the top 1" in result.summary


def test_tracker_falls_back_to_the_listing_category():
    assert default_phrases("Hair salon") == ["hair salon near me", "hair salon"]
    assert default_phrases("") == []


def test_responder_drafts_only_for_unanswered_reviews_and_proposes_high_impact():
    from app.connectors.google_business_profile.connector import BusinessReview

    reviews = [
        BusinessReview(name="accounts/9/locations/1/reviews/r1", reviewer="Asha", stars=5, comment="Great", created_at=None),
        BusinessReview(name="accounts/9/locations/1/reviews/r2", reviewer="Ravi", stars=4, comment="Good", created_at=None),
        BusinessReview(name="accounts/9/locations/1/reviews/r3", reviewer="Neha", stars=3, comment="Ok", created_at=None, reply="Thanks"),
        BusinessReview(name="accounts/9/locations/1/reviews/r4", reviewer="Om", stars=1, comment="Bad", created_at=None),
    ]
    profile = SimpleNamespace(read_reviews=lambda limit: reviews)
    from app.db.base import utcnow

    pending = ReviewReply(review_name="accounts/9/locations/1/reviews/r2", status="drafted")
    pending.created_at = pending.updated_at = utcnow()
    db = _FakeDb(existing=[pending])  # already drafted, still waiting for approval
    answers = [
        {"reply": "Thank you so much, Asha! Lovely to hear. — Geet unisex salon"},
        {"reply": "We're sorry, Om. Here is 50% off your next visit."},  # unsafe: discarded
    ]
    result = RESPONDER.run(_ctx(profile, db, answers=answers))
    rows = {row.review_name.rsplit("/", 1)[-1]: row for row in db.added if isinstance(row, ReviewReply)}
    drafted = [row for row in rows.values() if row.status == "drafted"]
    assert [row.review_name.rsplit("/", 1)[-1] for row in drafted] == ["r1"]
    # The unsafe draft is recorded, so it is not redrafted (and re-billed) every run.
    assert rows["r4"].status == "discarded" and rows["r4"].reply_encrypted == ""
    assert drafted[0].reply_encrypted.startswith("enc:")
    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.kind == "reply_review" and action.impact.value == "high"
    assert action.payload["reply"].startswith("Thank you")
    assert "discarded" in result.summary


def test_both_agents_declare_what_they_need():
    assert TRACKER.spec.required_capabilities == (Capability.READ_LOCAL_RANKINGS,)
    assert set(RESPONDER.spec.required_capabilities) == {Capability.READ_REVIEWS, Capability.REPLY_REVIEW}
    assert RESPONDER.spec.requires_llm and not TRACKER.spec.requires_llm


def test_an_expired_draft_and_an_old_discard_are_tried_again():
    from datetime import timedelta

    from app.connectors.google_business_profile.connector import BusinessReview
    from app.db.base import utcnow

    old = utcnow() - timedelta(days=40)
    expired = ReviewReply(review_name="accounts/9/locations/1/reviews/a", status="drafted")
    expired.created_at = expired.updated_at = old
    discarded = ReviewReply(review_name="accounts/9/locations/1/reviews/b", status="discarded")
    discarded.created_at = discarded.updated_at = old
    recent_discard = ReviewReply(review_name="accounts/9/locations/1/reviews/c", status="discarded")
    recent_discard.created_at = recent_discard.updated_at = utcnow()
    reviews = [
        BusinessReview(name=f"accounts/9/locations/1/reviews/{key}", reviewer="R", stars=5, comment="Nice", created_at=None)
        for key in ("a", "b", "c")
    ]
    profile = SimpleNamespace(read_reviews=lambda limit: reviews)
    answers = [{"reply": "Thank you so much! We are glad you enjoyed it. — Geet"}] * 2
    result = RESPONDER.run(_ctx(profile, _FakeDb(existing=[expired, discarded, recent_discard]), answers=answers))
    assert len(result.actions) == 2  # a and b; c was discarded too recently
    assert expired.status == "drafted" and discarded.status == "drafted"


def test_tracker_skips_cleanly_when_the_listing_cannot_be_found():
    places = _places(lambda request: httpx.Response(200, json={}))
    result = TRACKER.run(_ctx(places, _FakeDb(), scope="salon near me"))
    assert result.skipped and "no listing" in result.summary
