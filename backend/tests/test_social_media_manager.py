"""Social Media Manager: platform rules, scheduling, images, connectors, publishing, downloads."""
from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.agents.base.contracts import Impact
from app.agents.social_media_manager import AGENT, GUIDE, platforms, publishing
from app.agents.social_media_manager import agent as agent_module
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.facebook_pages.connector import FacebookPagesConnector
from app.connectors.instagram_business.connector import InstagramBusinessConnector
from app.connectors.linkedin_pages.connector import LinkedInPagesConnector, little_text
from app.connectors.pinterest.connector import PinterestConnector
from app.connectors.x_twitter.connector import XTwitterConnector, oauth1_header
from app.core.config import settings
from app.core.exceptions import ConflictError, ConnectorConfigError, InvalidInputError
from app.db.base import utcnow
from app.models.content import AssetStatus, GeneratedImage, MarketingAsset
from app.services import assets, images
from tests.conftest import requires_db


def _wire(connector, handler):  # noqa: ANN001, ANN202
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


# ── Platform rules ─────────────────────────────────────────────────────────
def test_x_counts_every_link_as_23_and_wide_characters_twice():
    assert platforms.x_length("hi https://example.com/a/very/long/path/that/goes/on") == 3 + 23
    assert platforms.x_length("日本") == 4


def test_an_x_post_is_cut_to_280_dropping_hashtags_first():
    text = " ".join(["word"] * 80)  # 399 characters
    post = platforms.fit(
        "x",
        {"text": text, "hashtags": ["one", "two", "three"], "link": "https://example.com/" + "x" * 200},
        hashtag_limit=3,
    )
    body = platforms.compose("x", post["text"], post["hashtags"], post["link"])
    assert platforms.x_length(body) <= 280
    assert post["hashtags"] == [] and post["text"].endswith("…")
    assert post["link"] in body  # the link survives; it is what the post is for

    short = platforms.fit("x", {"text": "Short.", "hashtags": ["#a b", "a", "1"]}, hashtag_limit=5)
    assert short["hashtags"] == ["#ab", "#a"]  # cleaned, deduplicated, digits-only dropped


def test_instagram_has_no_links_and_at_most_30_hashtags():
    post = platforms.fit(
        "instagram",
        {"text": "New menu! See https://cafe.example/menu or www.cafe.example", "link": "https://cafe.example",
         "hashtags": [f"tag{i}" for i in range(40)]},
        hashtag_limit=30,
    )
    assert "http" not in post["text"] and "www." not in post["text"]
    assert post["link"] == "" and len(post["hashtags"]) == 30
    assert platforms.problem("instagram", {**post, "hashtags": post["hashtags"] + ["#extra"]}).startswith(
        "Instagram allows at most 30"
    )
    caption = platforms.compose("instagram", post["text"], post["hashtags"])
    assert len(caption) <= 2200


def test_linkedin_facebook_and_pinterest_limits():
    long = platforms.fit("linkedin", {"text": "word " * 1000, "hashtags": ["a"]}, hashtag_limit=3)
    assert len(platforms.compose("linkedin", long["text"], long["hashtags"])) <= 3000

    fb = platforms.fit("facebook", {"text": "word " * 500}, hashtag_limit=0)
    assert len(fb["text"].split()) <= 2 * platforms.FACEBOOK_TARGET_WORDS

    pin = platforms.fit(
        "pinterest", {"text": "desc " * 200, "title": "T" * 150, "hashtags": ["a", "b"], "link": "https://x.example/"},
        hashtag_limit=2,
    )
    assert len(pin["title"]) <= 100
    assert len(platforms.compose("pinterest", pin["text"], pin["hashtags"])) <= 800
    assert platforms.problem("pinterest", {**pin, "link": ""}) == "A Pin needs a link to your website."


def test_an_edited_post_over_the_limit_is_refused_with_the_count():
    issue = platforms.problem("x", {"text": "a" * 300, "hashtags": []})
    assert "300 characters" in issue and "allows 280" in issue
    assert platforms.problem("x", {"text": "fine", "hashtags": ["#ok"], "link": "https://e.example"}) is None


# ── Scheduling ─────────────────────────────────────────────────────────────
def test_posts_are_spread_over_the_week_at_local_times():
    tz = ZoneInfo("Asia/Kolkata")
    days = platforms.week_days(date(2026, 10, 5))  # a Monday; plans Tue 6 – Mon 12
    assert days[0] == date(2026, 10, 6) and len(days) == 7
    three = platforms.slots("facebook", 3, days, tz)
    local = [s.astimezone(tz) for s in three]
    assert len({d.date() for d in local}) == 3  # one a day, on different days
    assert all(d.hour == 13 for d in local)
    assert all(s.tzinfo == timezone.utc for s in three)
    assert (local[-1].date() - local[0].date()).days >= 4  # spread, not bunched

    linkedin = [s.astimezone(tz) for s in platforms.slots("linkedin", 5, days, tz)]
    assert all(d.weekday() < 5 for d in linkedin) and len({d.date() for d in linkedin}) == 5

    fourteen = platforms.slots("instagram", 14, days, tz)
    assert len(fourteen) == len(set(fourteen)) == 14
    per_day = {}
    for s in fourteen:
        per_day[s.astimezone(tz).date()] = per_day.get(s.astimezone(tz).date(), 0) + 1
    assert set(per_day.values()) == {2}


def test_time_zone_comes_from_the_workspace_or_is_utc():
    assert platforms.zone_for({"timezone": "Asia/Kolkata"})[1] == "Asia/Kolkata"
    assert platforms.zone_for({"timezone": "Not/AZone"}) == (timezone.utc, "UTC")
    assert platforms.zone_for(None) == (timezone.utc, "UTC")


def test_days_already_planned_are_not_planned_again():
    tz = timezone.utc
    days = platforms.week_days(date(2026, 10, 5))
    start = datetime(2026, 10, 6, tzinfo=tz)
    end = start + timedelta(days=7)
    first = platforms.slots("facebook", 3, days, tz)[0]
    existing = MarketingAsset(scheduled_for=first, status=AssetStatus.DRAFT, detail={"network": "facebook"})
    rejected = MarketingAsset(scheduled_for=first + timedelta(days=2), status=AssetStatus.REJECTED, detail={})
    wanted = agent_module.open_slots(
        ["facebook", "x"], 3, days, tz, [(existing, {"network": "facebook"}), (rejected, {"network": "facebook"})],
        start, end,
    )
    assert len(wanted["facebook"]) == 2 and first not in wanted["facebook"]
    assert len(wanted["x"]) == 3

    kept, deferred = agent_module.share_out({"a": [1, 2, 3], "b": [4, 5, 6]}, 4)
    assert kept == {"a": [1, 2], "b": [4, 5]} and deferred == 2


def test_post_types_are_mixed_and_social_proof_needs_a_claim():
    profile = {"products_services": ["Haircut", "Colour"], "value_propositions": ["Friendly staff"]}
    assert prompts_proof(profile) == []
    items = agent_module.plan_items("facebook", 4, offset=0, profile=profile, articles=[], proof=[], homepage="")
    assert [i["type"] for i in items] == ["tip", "highlight", "question", "tip"]
    proof = prompts_proof({"differentiators": ["Rated 4.9 from 300 reviews on Google"]})
    assert proof == ["Rated 4.9 from 300 reviews on Google"]
    items = agent_module.plan_items(
        "x", 5, offset=0, profile=profile, articles=[{"title": "Hair care 101", "url": "https://s.example/a"}],
        proof=proof, homepage="https://s.example/",
    )
    kinds = [i["type"] for i in items]
    assert kinds == ["tip", "article", "highlight", "question", "social_proof"]
    assert items[1]["link"] == "https://s.example/a" and items[2]["subject"] == "Haircut"


def prompts_proof(profile: dict) -> list[str]:
    from app.agents.social_media_manager import prompts

    return prompts.proof_claims(profile)


# ── The agent ──────────────────────────────────────────────────────────────
class _Cipher:
    def encrypt(self, value, *, context):  # noqa: ANN001
        return value

    def decrypt(self, value, *, context, default=""):  # noqa: ANN001
        return value


class _Db:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:  # noqa: ANN001
        if getattr(row, "id", None) is None:
            row.id = f"row{len(self.added)}"
        self.added.append(row)

    def flush(self) -> None:
        pass


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


#: An image connection with an image model chosen (from the vendor's list).
_IMAGE_CONNECTOR = SimpleNamespace(
    slug="openai", name="OpenAI", credentials=SimpleNamespace(get=lambda key, default="": {"imageModel": "img"}.get(key, default))
)


def _ctx(db: _Db, *, connectors: dict, settings: dict, prompts_seen: list):  # noqa: ANN202
    def ask_json(prompt, **_):  # noqa: ANN001, ANN003
        prompts_seen.append(prompt)
        count = int(re.search(r"Write (\d+) ", prompt).group(1))
        return {
            "posts": [
                {
                    "index": i,
                    "topic": f"Topic {i}",
                    "text": f"Post number {i} about our salon. Visit https://salon.example/ today",
                    "hashtags": ["Salon", "Noida", "Hair"],
                    "link": "https://made-up.example/" if i == 1 else "https://salon.example/",
                    "title": f"Pin {i}",
                    "image_prompt": "A bright salon interior",
                }
                for i in range(1, count + 1)
            ]
        }

    return SimpleNamespace(
        db=db,
        tenant_id="t",
        run_id="run1",
        remaining_actions=25,
        org=SimpleNamespace(id="t", name="Geet salon", settings={"timezone": "Asia/Kolkata"}, primary_domain="salon.example"),
        cipher=_Cipher(),
        connectors=connectors,
        brand_brief="BUSINESS: Geet salon\nTONE OF VOICE: warm",
        brand_profile={"products_services": ["Haircut"]},
        setting=lambda key, default=None: settings.get(key, default),
        with_capability=lambda capability: _IMAGE_CONNECTOR if capability == Capability.GENERATE_IMAGE else None,
        progress=lambda *a, **k: None,
        log_warning=lambda *a, **k: None,
        ask_json=ask_json,
    )


@pytest.fixture
def no_history(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(agent_module, "recent_posts", lambda ctx: [])
    monkeypatch.setattr(agent_module, "recent_articles", lambda ctx: [])


def test_a_run_plans_each_network_with_images_and_download_fallbacks(monkeypatch, no_history):  # noqa: ANN001
    made: list[tuple[str, str]] = []

    def generate(db, org, prompt, *, shape="square", provider=""):  # noqa: ANN001
        made.append((prompt, shape))
        if len(made) == 4:  # the first Instagram image fails
            raise ConflictError("image service unavailable")
        image = GeneratedImage(tenant_id="t", created_at=utcnow(), prompt=prompt, provider="openai", mime="image/png",
                               width=8, height=8, data=_png())
        db.add(image)
        return image

    monkeypatch.setattr(images, "generate", generate)
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: True)
    db = _Db()
    seen: list[str] = []
    ctx = _ctx(
        db,
        connectors={"facebook_pages": object(), "instagram_business": object()},
        settings={"networks": "facebook, instagram, pinterest, myspace", "posts_per_week": "3"},
        prompts_seen=seen,
    )
    result = AGENT.run(ctx)

    posts = [row for row in db.added if isinstance(row, MarketingAsset)]
    assert len(posts) == 9 and len(result.actions) == 9
    by_network: dict[str, list[MarketingAsset]] = {}
    for row in posts:
        by_network.setdefault(row.detail["network"], []).append(row)

    assert {row.channel for row in by_network["facebook"]} == {"facebook_pages"}
    assert all(not row.detail.get("export_only") for row in by_network["facebook"])
    # The Instagram post whose image failed is a download, with the reason.
    ig = by_network["instagram"]
    assert [row.channel for row in ig].count("export:instagram") == 1
    failed = next(row for row in ig if row.channel == "export:instagram")
    assert failed.detail["export_reason"] == "no_image" and failed.image_ids == []
    assert "needs an image" in json.loads(failed.content_encrypted)["note"]
    # Instagram images are stored as JPEG, the only format Instagram accepts.
    ig_images = [r for r in db.added if isinstance(r, GeneratedImage) and r.id in {i for p in ig for i in p.image_ids}]
    assert ig_images and all(image.mime == "image/jpeg" for image in ig_images)
    # Pinterest is not connected: downloads, made with portrait images.
    assert {row.channel for row in by_network["pinterest"]} == {"export:pinterest"}
    assert [shape for _, shape in made].count("portrait") == 3

    for row in posts:
        content = json.loads(row.content_encrypted)
        assert row.kind == "social_post" and row.scheduled_for > utcnow()
        assert content["timezone"] == "Asia/Kolkata"
        assert content["link"] in ("", "https://salon.example/")  # an invented link is never kept
        if content["network"] == "instagram":
            assert "http" not in content["text"] and content["link"] == ""
        assert "text" not in row.detail  # structural facts only

    action = result.actions[0]
    assert action.impact is Impact.HIGH and action.target_kind == assets.TARGET_KIND
    assert set(action.payload) >= {"asset_id", "network", "text", "scheduled_for"}
    assert not action.metrics
    assert "BUSINESS: Geet salon" in seen[0]
    assert "myspace" in result.summary


def test_without_connections_or_choices_it_writes_downloads_for_three_networks(monkeypatch, no_history):  # noqa: ANN001
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: False)
    db = _Db()
    ctx = _ctx(db, connectors={}, settings={"posts_per_week": "1"}, prompts_seen=[])
    ctx.with_capability = lambda capability: None  # no image integration
    result = AGENT.run(ctx)
    posts = [row for row in db.added if isinstance(row, MarketingAsset)]
    assert sorted(row.channel for row in posts) == ["export:facebook", "export:instagram", "export:linkedin"]
    assert all(row.detail["export_only"] for row in posts)
    assert len(result.actions) == 3


def test_connected_instagram_without_a_public_address_is_a_download(monkeypatch, no_history):  # noqa: ANN001
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: False)
    monkeypatch.setattr(
        images, "generate",
        lambda db, org, prompt, shape="square", provider="": GeneratedImage(id="img", tenant_id="t", mime="image/jpeg", data=b"j"),
    )
    db = _Db()
    ctx = _ctx(db, connectors={"instagram_business": object(), "x_twitter": object()},
               settings={"posts_per_week": "1"}, prompts_seen=[])
    AGENT.run(ctx)
    posts = {row.detail["network"]: row for row in db.added if isinstance(row, MarketingAsset)}
    assert posts["instagram"].detail["export_reason"] == "no_public_address"
    # X posts publish as text, so no image is made for them.
    assert posts["x"].channel == "x_twitter" and posts["x"].image_ids == []


def test_a_week_already_planned_is_skipped(monkeypatch):  # noqa: ANN001
    tz = ZoneInfo("Asia/Kolkata")
    days = platforms.week_days(utcnow().astimezone(tz).date())
    rows = [
        MarketingAsset(scheduled_for=when, status=AssetStatus.SCHEDULED, detail={"network": "facebook"},
                       content_encrypted=json.dumps({"network": "facebook", "topic": "Old"}))
        for when in platforms.slots("facebook", 3, days, tz)
    ]
    monkeypatch.setattr(agent_module, "recent_posts", lambda ctx: rows)
    monkeypatch.setattr(agent_module, "recent_articles", lambda ctx: [])
    ctx = _ctx(_Db(), connectors={"facebook_pages": object()}, settings={}, prompts_seen=[])
    result = AGENT.run(ctx)
    assert result.skipped


def test_the_guide_declares_its_settings_and_optional_integrations():
    keys = {field.key for field in GUIDE.settings}
    # Networks are chosen with the connector selection now; the old
    # "networks" setting is only read back for configurations saved before.
    assert keys == {"posts_per_week", "hashtags_per_post"}
    assert {o.capability for o in GUIDE.optional} == {Capability.PUBLISH_SOCIAL, Capability.GENERATE_IMAGE}
    posts = next(f for f in GUIDE.settings if f.key == "posts_per_week")
    with pytest.raises(ValueError):
        posts.clean("15")
    assert AGENT.spec.default_schedule == "Weekly" and AGENT.spec.requires_llm


# ── Connectors ─────────────────────────────────────────────────────────────
def test_facebook_text_photo_and_health_requests():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer page-token"
        if request.url.path.endswith("/feed"):
            return httpx.Response(200, json={"id": "123_456"})
        if request.url.path.endswith("/photos"):
            return httpx.Response(200, json={"id": "789", "post_id": "123_789"})
        return httpx.Response(200, json={"name": "Geet salon", "id": "123"})

    fb = _wire(FacebookPagesConnector(Credentials(values={"pageId": "123", "pageAccessToken": "page-token"})), handler)
    assert fb.publish_post(text="Hello", link="https://s.example/") == ("123_456", "https://www.facebook.com/123_456")
    assert seen[-1].url.path == "/v26.0/123/feed"
    assert json.loads(seen[-1].content) == {"message": "Hello", "link": "https://s.example/"}

    post_id, _ = fb.publish_post(text="Photo", link="https://s.example/", image_bytes=b"PNGDATA", image_mime="image/png")
    assert post_id == "123_789"
    photo = seen[-1]
    assert photo.headers["Content-Type"].startswith("multipart/form-data")
    body = photo.content
    assert b'name="source"' in body and b"PNGDATA" in body and b'name="caption"' in body
    assert b"Photo\n\nhttps://s.example/" in body.replace(b"\r\n", b"\n")

    report = fb.check_health()
    assert report.ok and "Geet salon" in report.detail
    assert seen[-1].url.params["fields"] == "name"


def test_facebook_explains_an_expired_token_and_a_bad_version():
    fb = _wire(
        FacebookPagesConnector(Credentials(values={"pageId": "1", "pageAccessToken": "t"})),
        lambda r: httpx.Response(400, json={"error": {"message": "Session has expired", "code": 190}}),
    )
    report = fb.check_health()
    assert not report.ok and "expired" in report.detail
    with pytest.raises(ConnectorConfigError, match="not a Graph API version"):
        _ = FacebookPagesConnector(Credentials(values={"apiVersion": "latest"})).base_url
    old = _wire(
        FacebookPagesConnector(Credentials(values={"pageId": "1", "pageAccessToken": "t", "apiVersion": "v2.0"})),
        lambda r: httpx.Response(400, json={"error": {"message": "(#12) deprecated", "code": 12}}),
    )
    with pytest.raises(ConnectorConfigError, match="no longer accepts Graph API version v2.0"):
        old.publish_post(text="x")


def test_instagram_creates_waits_for_and_publishes_the_container(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: True)
    seen: list[httpx.Request] = []
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/1784/media"):
            return httpx.Response(200, json={"id": "container9"})
        if path.endswith("/container9"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "media5"})
        if path.endswith("/media5"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/abc/"})
        return httpx.Response(200, json={"username": "geetsalon"})

    ig = _wire(InstagramBusinessConnector(Credentials(values={"igUserId": "1784", "accessToken": "tok"})), handler)
    ig._sleep = lambda seconds: None
    result = ig.publish_post(text="Caption #hair", image_url="https://app.example/api/v1/public/images/x")
    assert result == ("media5", "https://www.instagram.com/p/abc/")
    assert json.loads(seen[0].content) == {"image_url": "https://app.example/api/v1/public/images/x", "caption": "Caption #hair"}
    assert json.loads(seen[3].content) == {"creation_id": "container9"}
    assert ig.check_health().detail == "Connected to @geetsalon"


def test_instagram_refuses_without_a_public_address(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: False)
    ig = _wire(InstagramBusinessConnector(Credentials(values={"igUserId": "1", "accessToken": "t"})),
               lambda r: pytest.fail("no request should be made"))
    with pytest.raises(ConnectorConfigError, match="not reachable from the internet"):
        ig.publish_post(text="x", image_url="http://localhost/img")


def test_linkedin_uploads_the_image_and_reads_the_post_id_from_the_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/rest/images":
            return httpx.Response(200, json={"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/abc",
                                                       "image": "urn:li:image:C4E"}})
        if request.url.host == "www.linkedin.com":
            return httpx.Response(201)
        if request.url.path == "/rest/posts" and request.method == "POST":
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7000"})
        return httpx.Response(200, json={"elements": []})

    li = _wire(
        LinkedInPagesConnector(Credentials(values={"organizationId": "555", "accessToken": "tok"})), handler
    )
    urn, url = li.publish_post(text="New (v2) offer #Salon", link="https://s.example/", image_bytes=b"IMG", image_mime="image/png")
    assert urn == "urn:li:share:7000" and url == "https://www.linkedin.com/feed/update/urn:li:share:7000/"
    init, upload, post = seen
    assert init.url.params["action"] == "initializeUpload"
    assert json.loads(init.content) == {"initializeUploadRequest": {"owner": "urn:li:organization:555"}}
    assert upload.method == "PUT" and upload.content == b"IMG"
    assert post.headers["LinkedIn-Version"] == settings.linkedin_api_version
    assert post.headers["X-Restli-Protocol-Version"] == "2.0.0"
    body = json.loads(post.content)
    assert body["author"] == "urn:li:organization:555" and body["lifecycleState"] == "PUBLISHED"
    assert body["distribution"] == {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []}
    assert body["content"] == {"media": {"id": "urn:li:image:C4E"}}
    assert body["visibility"] == "PUBLIC" and body["isReshareDisabledByAuthor"] is False
    assert body["commentary"].startswith("New \\(v2\\) offer {hashtag|\\#|Salon}")

    assert li.check_health().ok
    assert seen[-1].headers["X-RestLi-Method"] == "FINDER"
    assert "author=urn%3Ali%3Aorganization%3A555" in str(seen[-1].url)


def test_linkedin_little_text_escapes_reserved_characters():
    assert little_text("a_b @c [d] #tag") == "a\\_b \\@c \\[d\\] {hashtag|\\#|tag}"


def test_linkedin_reports_a_retired_version():
    li = _wire(LinkedInPagesConnector(Credentials(values={"organizationId": "1", "accessToken": "t", "apiVersion": "202301"})),
               lambda r: httpx.Response(426))
    with pytest.raises(ConnectorConfigError, match="202301"):
        li.publish_post(text="x")


def test_x_posts_text_with_a_signed_or_bearer_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/2/tweets":
            return httpx.Response(201, json={"data": {"id": "1800", "text": "x"}})
        return httpx.Response(200, json={"data": {"id": "1", "username": "geet"}})

    signed = _wire(
        XTwitterConnector(Credentials(values={"accessToken": "tok", "accessTokenSecret": "ts", "apiKey": "ck", "apiKeySecret": "cs"})),
        handler,
    )
    assert signed.publish_post(text="Hello", link="https://s.example/") == (
        "1800", "https://x.com/i/web/status/1800",
    )
    assert json.loads(seen[-1].content) == {"text": "Hello https://s.example/"}
    auth = seen[-1].headers["Authorization"]
    assert auth.startswith("OAuth ") and 'oauth_consumer_key="ck"' in auth and 'oauth_token="tok"' in auth

    bearer = _wire(XTwitterConnector(Credentials(values={"accessToken": "oauth2"})), handler)
    assert bearer.check_health().detail == "Connected to @geet"
    assert seen[-1].headers["Authorization"] == "Bearer oauth2"

    partial = XTwitterConnector(Credentials(values={"accessToken": "t", "apiKey": "ck"}))
    with pytest.raises(ConnectorConfigError, match="all four"):
        partial.auth_headers()


def test_x_signature_matches_the_documented_example():
    # The worked example from X's "Creating a signature" documentation.
    url = httpx.URL(
        "https://api.twitter.com/1.1/statuses/update.json",
        params={"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
    )
    header = oauth1_header(
        "POST", url,
        consumer_key="xvz1evFS4wEEPTGEFPHBog", consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958",
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header


def test_x_explains_a_refusal():
    x = _wire(XTwitterConnector(Credentials(values={"accessToken": "t"})),
              lambda r: httpx.Response(403, json={"detail": "You are not permitted to perform this action."}))
    with pytest.raises(ConnectorConfigError, match="Read and write"):
        x.publish_post(text="hi")


def test_pinterest_creates_a_pin_with_the_image_inline():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v5/pins":
            return httpx.Response(201, json={"id": "987"})
        return httpx.Response(200, json={"username": "geetsalon"})

    pin = _wire(PinterestConnector(Credentials(values={"boardId": "55", "accessToken": "tok"})), handler)
    assert pin.publish_post(text="Desc", title="Title", link="https://s.example/", image_bytes=b"IMG", image_mime="image/png") == (
        "987", "https://www.pinterest.com/pin/987/",
    )
    body = json.loads(seen[-1].content)
    assert body == {
        "board_id": "55", "title": "Title", "description": "Desc", "link": "https://s.example/",
        "media_source": {"source_type": "image_base64", "content_type": "image/png",
                         "data": base64.b64encode(b"IMG").decode()},
    }
    assert seen[-1].headers["Authorization"] == "Bearer tok"
    with pytest.raises(ConnectorConfigError, match="link"):
        pin.publish_post(text="Desc", image_bytes=b"IMG")
    assert pin.check_health().ok and seen[-1].url.path == "/v5/user_account"


def test_the_new_connectors_are_social_media_and_can_publish():
    from app.connectors.base import registry

    for slug in ("facebook_pages", "instagram_business", "linkedin_pages", "x_twitter", "pinterest"):
        spec = registry.get_spec(slug)
        assert spec.category == "Social media" and Capability.PUBLISH_SOCIAL in spec.capabilities
        assert spec.requirements
    assert "Social media" in registry.categories()


# ── Publishing and downloads ───────────────────────────────────────────────
class _FakeConnector:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish_post(self, **kwargs) -> tuple[str, str]:  # noqa: ANN003
        self.calls.append(kwargs)
        return "remote1", "https://social.example/remote1"


def _post(network: str, channel: str, **content) -> MarketingAsset:  # noqa: ANN003
    body = {"network": network, "text": "Hello", "hashtags": ["#hi"], "link": "https://s.example/", **content}
    return MarketingAsset(
        id="a1", tenant_id="t", agent_slug="social_media_manager", kind="social_post", channel=channel,
        title="Facebook · Tue 06 Oct: Hello", content_encrypted=json.dumps(body), status=AssetStatus.DRAFT,
        detail={"network": network}, image_ids=["img1"], scheduled_for=datetime(2026, 10, 6, 7, 30, tzinfo=timezone.utc),
    )


def _publish_env(monkeypatch, connectors: dict):  # noqa: ANN001, ANN202
    import app.services.connectors as connector_service

    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: SimpleNamespace(instances=connectors, close=lambda: None))
    image = GeneratedImage(id="img1", tenant_id="t", mime="image/png", data=b"PNG")
    return SimpleNamespace(flush=lambda: None, get=lambda model, key: image if key == "img1" else None)


def test_the_publisher_routes_by_channel_and_records_the_remote_post(monkeypatch):  # noqa: ANN001
    fake = _FakeConnector()
    db = _publish_env(monkeypatch, {"facebook_pages": fake})
    asset = _post("facebook", "facebook_pages")
    assets.publish(db, SimpleNamespace(id="t"), asset, _Cipher())
    assert asset.status == AssetStatus.PUBLISHED
    assert (asset.remote_id, asset.remote_url) == ("remote1", "https://social.example/remote1")
    call = fake.calls[0]
    assert call["text"] == "Hello\n\n#hi" and call["link"] == "https://s.example/"
    assert call["image_bytes"] == b"PNG" and call["image_mime"] == "image/png"


def test_instagram_is_given_a_public_link_and_x_the_image_bytes(monkeypatch):  # noqa: ANN001
    ig, x = _FakeConnector(), _FakeConnector()
    db = _publish_env(monkeypatch, {"instagram_business": ig, "x_twitter": x})
    assets.publish(db, SimpleNamespace(id="t"), _post("instagram", "instagram_business", link=""), _Cipher())
    assert "/api/v1/public/images/t/img1/" in ig.calls[0]["image_url"]
    assert "image_bytes" not in ig.calls[0]
    assets.publish(db, SimpleNamespace(id="t"), _post("x", "x_twitter"), _Cipher())
    assert x.calls[0]["text"] == "Hello https://s.example/ #hi"
    assert x.calls[0]["image_bytes"] == b"PNG" and x.calls[0]["image_mime"] == "image/png"


def test_downloads_and_unconnected_networks_are_not_published(monkeypatch):  # noqa: ANN001
    db = _publish_env(monkeypatch, {})
    export_only = _post("pinterest", "export:pinterest")
    export_only.detail["export_only"] = True
    assets.release(db, SimpleNamespace(id="t"), export_only, _Cipher())
    assert export_only.status == AssetStatus.APPROVED  # ready to download

    missing = _post("linkedin", "linkedin_pages")
    with pytest.raises(ConflictError, match="not connected"):
        assets.publish(db, SimpleNamespace(id="t"), missing, _Cipher())
    assert missing.status == AssetStatus.FAILED

    too_long = _post("x", "x_twitter", text="a" * 400)
    db = _publish_env(monkeypatch, {"x_twitter": _FakeConnector()})
    with pytest.raises(ConflictError, match="allows 280"):
        assets.publish(db, SimpleNamespace(id="t"), too_long, _Cipher())


def test_an_edited_post_is_checked_by_the_publisher_directly(monkeypatch):  # noqa: ANN001
    db = _publish_env(monkeypatch, {"x_twitter": _FakeConnector()})
    with pytest.raises(InvalidInputError):
        publishing.publish_social_post(db, SimpleNamespace(id="t"), _post("x", "x_twitter"), {"network": "x", "text": "b" * 281})


def test_csv_and_text_downloads(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: False)
    asset = _post("facebook", "export:facebook", timezone="Asia/Kolkata", note="Ready to download.")
    file = assets.export(asset, _Cipher(), "csv")
    assert file.filename.endswith(".csv") and file.mime.startswith("text/csv")
    import csv
    import io

    rows = list(csv.reader(io.StringIO(file.data.decode("utf-8-sig"))))
    assert rows[0][:7] == ["Network", "Scheduled date", "Scheduled time", "Time zone", "Text", "Hashtags", "Link"]
    record = dict(zip(rows[0], rows[1], strict=True))
    assert record["Network"] == "Facebook"
    assert (record["Scheduled date"], record["Scheduled time"], record["Time zone"]) == ("2026-10-06", "13:00", "Asia/Kolkata")
    assert record["Hashtags"] == "#hi" and record["Link"] == "https://s.example/"
    assert record["Image"].endswith("(download it from this post in the library)")

    monkeypatch.setattr(images, "is_publicly_reachable", lambda: True)
    text = assets.export(asset, _Cipher(), "txt").data.decode()
    assert "Scheduled: 2026-10-06 13:00 (Asia/Kolkata)" in text and "Hello\n\n#hi" in text
    assert "Image: " in text and "/public/images/t/img1/" in text
    assert "csv" in assets.formats_for("social_post") and "buffer_csv" not in assets.formats_for("social_post")


# ── Reddit, X images and the published-picture link (step 4.6) ─────────────
def test_reddit_rules_title_body_and_no_hashtags():
    assert "reddit" not in platforms.NETWORKS  # the agent does not plan Reddit weeks
    assert platforms.rules_for("reddit") is platforms.REDDIT
    post = platforms.fit(
        "reddit",
        {"text": "Our new guide. " + "word " * 9000, "hashtags": ["a", "b"], "title": "T" * 400,
         "link": "https://s.example/"},
        hashtag_limit=5,
    )
    assert post["hashtags"] == [] and len(post["title"]) <= 300
    assert len(platforms.compose("reddit", post["text"], post["hashtags"], post["link"])) <= 40_000
    assert platforms.compose("reddit", "Body", [], "https://s.example/") == "Body"  # the link is its own field
    assert platforms.problem("reddit", post) is None

    assert platforms.problem("reddit", {"text": "Body", "title": ""}) == "A Reddit post needs a title. Add one and publish again."
    assert "allows 300" in platforms.problem("reddit", {"text": "Body", "title": "x" * 301})
    assert "hashtags" in platforms.problem("reddit", {"text": "Body", "title": "T", "hashtags": ["#a"]})
    assert "40000" in platforms.problem("reddit", {"text": "b" * 40_001, "title": "T"})


def test_r8_caption_limits_are_data():
    limits = {key: rules.limit for key, rules in platforms.PUBLISH_NETWORKS.items()}
    assert limits == {"facebook": 63206, "instagram": 2200, "linkedin": 3000, "x": 280, "pinterest": 800, "reddit": 40000}
    assert platforms.NETWORKS["instagram"].max_hashtags == 30
    assert platforms.NETWORKS["pinterest"].title_limit == 100 and platforms.REDDIT.title_limit == 300
    assert platforms.clean_hashtags(["a", "b"], 0) == []


class _RedditConnector(_FakeConnector):
    def __init__(self, note: str = "", error: Exception | None = None) -> None:
        super().__init__()
        self.credentials = SimpleNamespace(get=lambda key, default="": {"subreddit": "marketing"}.get(key, default))
        self.last_post_note = note
        self.error = error

    def publish_post(self, **kwargs) -> tuple[str, str]:  # noqa: ANN003
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return "t3_abc", "https://www.reddit.com/r/marketing/comments/abc/"


class _RedditDb:
    """Enough of a Session for the publisher: the image, and the recent-posts query."""

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.statements: list = []
        self.image = GeneratedImage(id="img1", tenant_id="t", mime="image/png", data=b"PNG")

    def get(self, model, key):  # noqa: ANN001, ANN201
        return self.image if key == "img1" else None

    def flush(self) -> None:
        pass

    def get_bind(self):  # noqa: ANN201
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: list(self.rows))


def _reddit_env(monkeypatch, connector, rows: list[tuple] | None = None, *, public: bool = True):  # noqa: ANN001, ANN202
    import app.services.connectors as connector_service

    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: SimpleNamespace(instances={"reddit": connector}, close=lambda: None))
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: public)
    return _RedditDb(rows or [])


def _reddit_post(**content) -> MarketingAsset:  # noqa: ANN003
    asset = _post("reddit", "reddit", hashtags=[], title="Our spring guide", **content)
    asset.detail = {"network": "reddit"}
    return asset


def test_reddit_posts_get_their_subreddit_title_and_a_long_lived_image_link(monkeypatch):  # noqa: ANN001
    reddit = _RedditConnector(note="Reddit link posts carry one address, so the post links to the image.")
    db = _reddit_env(monkeypatch, reddit)
    asset = _reddit_post(subreddit="r/SmallBusiness")
    assets.publish(db, SimpleNamespace(id="t"), asset, _Cipher())
    assert asset.status == AssetStatus.PUBLISHED and asset.remote_id == "t3_abc"
    call = reddit.calls[0]
    assert call["subreddit"] == "SmallBusiness" and call["title"] == "Our spring guide"
    assert call["text"] == "Hello" and call["link"] == "https://s.example/"
    assert "image_bytes" not in call
    tenant, image, expires, _sig = call["image_url"].split("/public/images/")[1].split("/")
    assert (tenant, image) == ("t", "img1")
    assert int(expires) > int(utcnow().timestamp()) + 300 * 24 * 3600  # not the 24-hour link
    assert asset.detail["subreddit"] == "smallbusiness"
    assert asset.detail["publish_note"].startswith("Reddit link posts")


def test_an_x_post_that_went_out_without_its_image_keeps_the_note(monkeypatch):  # noqa: ANN001
    x = _FakeConnector()
    x.last_post_note = "Posted without its image. Reconnect X to allow images."
    db = _publish_env(monkeypatch, {"x_twitter": x})
    asset = _post("x", "x_twitter")
    assets.publish(db, SimpleNamespace(id="t"), asset, _Cipher())
    assert asset.status == AssetStatus.PUBLISHED
    assert asset.detail["publish_note"].endswith("Reconnect X to allow images.")
    assert "subreddit" not in asset.detail


def test_reddit_without_a_public_address_gets_the_bytes_and_the_connections_subreddit(monkeypatch):  # noqa: ANN001
    reddit = _RedditConnector()
    db = _reddit_env(monkeypatch, reddit, public=False)
    assets.publish(db, SimpleNamespace(id="t"), _reddit_post(), _Cipher())
    call = reddit.calls[0]
    assert call["subreddit"] == "marketing" and call["image_bytes"] == b"PNG" and "image_url" not in call


def test_a_reddit_post_too_soon_after_the_last_one_there_is_deferred_not_sent(monkeypatch):  # noqa: ANN001
    from app.connectors.reddit.connector import RedditRateLimited

    reddit = _RedditConnector()
    recent = utcnow() - timedelta(minutes=2)
    rows = [
        (AssetStatus.PUBLISHED, recent, recent, {"subreddit": "marketing"}),
        (AssetStatus.PUBLISHED, recent, recent, {"subreddit": "other"}),
    ]
    db = _reddit_env(monkeypatch, reddit, rows)
    with pytest.raises(RedditRateLimited) as caught:
        publishing.publish_social_post(db, SimpleNamespace(id="t"), _reddit_post(), {
            "network": "reddit", "text": "Hello", "title": "T", "subreddit": "Marketing",
        })
    assert reddit.calls == []
    gap = settings.reddit_min_minutes_between_posts * 60
    assert 0 < caught.value.retry_after_seconds <= gap
    assert "kept at least" in str(caught.value)
    # Our own spacing is marked, so the publisher does not count it as a deferral.
    assert caught.value.details[assets.SPACING_DETAIL] is True
    # The query is the workspace's own Reddit posts, never another's.
    sql = str(db.statements[-1].compile(compile_kwargs={"literal_binds": True}))
    assert "marketing_assets.tenant_id = 't'" in sql and "marketing_assets.channel = 'reddit'" in sql
    assert "marketing_assets.id != 'a1'" in sql


def test_reddit_spacing_ignores_other_subreddits_and_old_posts(monkeypatch):  # noqa: ANN001
    reddit = _RedditConnector()
    old = utcnow() - timedelta(minutes=settings.reddit_min_minutes_between_posts + 5)
    rows = [
        (AssetStatus.PUBLISHED, utcnow(), utcnow(), {"subreddit": "other"}),
        (AssetStatus.PUBLISHED, old, old, {"subreddit": "marketing"}),
    ]
    db = _reddit_env(monkeypatch, reddit, rows)
    publishing.publish_social_post(db, SimpleNamespace(id="t"), _reddit_post(), {"network": "reddit", "text": "Hi", "title": "T"})
    assert len(reddit.calls) == 1


def test_a_post_reddit_refuses_fails_with_its_reason(monkeypatch):  # noqa: ANN001
    from app.connectors.reddit.connector import RedditPostRefused

    reddit = _RedditConnector(error=RedditPostRefused("r/marketing does not allow link posts."))
    db = _reddit_env(monkeypatch, reddit)
    asset = _reddit_post()
    with pytest.raises(ConflictError):
        assets.publish(db, SimpleNamespace(id="t"), asset, _Cipher())
    assert asset.status == AssetStatus.FAILED and asset.error == "r/marketing does not allow link posts."


def test_a_bad_subreddit_name_is_refused_before_anything_is_sent(monkeypatch):  # noqa: ANN001
    reddit = _RedditConnector()
    db = _reddit_env(monkeypatch, reddit)
    with pytest.raises(ConnectorConfigError, match="valid subreddit"):
        publishing.publish_social_post(db, SimpleNamespace(id="t"), _reddit_post(), {
            "network": "reddit", "text": "Hi", "title": "T", "subreddit": "../../api/v1/me",
        })
    assert reddit.calls == [] and db.statements == []


def test_pinterest_gets_a_link_for_a_webp_and_bytes_otherwise(monkeypatch):  # noqa: ANN001
    pin = _FakeConnector()
    db = _publish_env(monkeypatch, {"pinterest": pin})
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: True)
    assets.publish(db, SimpleNamespace(id="t"), _post("pinterest", "pinterest", title="Pin"), _Cipher())
    assert pin.calls[0]["image_bytes"] == b"PNG" and "image_url" not in pin.calls[0]

    webp = GeneratedImage(id="img1", tenant_id="t", mime="image/webp", data=b"RIFF....WEBP")
    db = SimpleNamespace(flush=lambda: None, get=lambda model, key: webp)
    assets.publish(db, SimpleNamespace(id="t"), _post("pinterest", "pinterest", title="Pin"), _Cipher())
    assert "/public/images/t/img1/" in pin.calls[1]["image_url"] and "image_bytes" not in pin.calls[1]

    monkeypatch.setattr(images, "is_publicly_reachable", lambda: False)
    with pytest.raises(ConflictError, match="Download the post"):
        assets.publish(db, SimpleNamespace(id="t"), _post("pinterest", "pinterest", title="Pin"), _Cipher())


@requires_db
def test_reddit_spacing_is_read_from_the_database(clean_db, org, second_org):  # noqa: ANN001
    from app.connectors.reddit.connector import RedditRateLimited
    from app.db.session import bind_tenant

    def published(tenant_id: str, subreddit: str) -> MarketingAsset:
        row = MarketingAsset(
            tenant_id=tenant_id, agent_slug="social_media_manager", kind="social_post", channel="reddit",
            title="Reddit post", status=AssetStatus.PUBLISHED, published_at=utcnow() - timedelta(minutes=1),
            detail={"network": "reddit", "subreddit": subreddit},
        )
        clean_db.add(row)
        return row

    bind_tenant(clean_db, second_org.id)
    published(second_org.id, "marketing")  # another workspace's post does not count
    clean_db.commit()
    bind_tenant(clean_db, org.id)
    mine = MarketingAsset(tenant_id=org.id, agent_slug="social_media_manager", kind="social_post",
                          channel="reddit", title="Next", status=AssetStatus.SCHEDULED, detail={"network": "reddit"})
    clean_db.add(mine)
    clean_db.flush()
    publishing._check_reddit_spacing(clean_db, org, mine, "marketing")  # nothing of this workspace yet

    published(org.id, "marketing")
    clean_db.flush()
    with pytest.raises(RedditRateLimited):
        publishing._check_reddit_spacing(clean_db, org, mine, "Marketing")
    publishing._check_reddit_spacing(clean_db, org, mine, "other_sub")
    clean_db.rollback()
