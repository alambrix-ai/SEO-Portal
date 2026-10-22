"""Post metrics read back from each social platform, X image upload, Search Console by day,
and the codes sign-in failures carry (contract 3.8).

Every platform is a mocked HTTP transport: the requests are checked against
the documented endpoints (research R11) and the answers mapped into
``POST_METRIC_FIELDS``, with what the platform does not report left None.
"""
from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from urllib.parse import unquote

import httpx
import pytest

from app.connectors.base.credentials import Credentials
from app.connectors.base.interfaces import (
    InsightsAccessMissing,
    PostMetricsReader,
    SearchDailyReader,
    SearchDay,
    count_or_none,
    empty_post_metrics,
    sum_or_none,
)
from app.connectors.facebook_pages.connector import FacebookPagesConnector
from app.connectors.instagram_business.connector import InstagramBusinessConnector
from app.connectors.linkedin_pages.connector import LinkedInPagesConnector
from app.connectors.pinterest.connector import PinterestConnector
from app.connectors.search_console.connector import SearchConsoleConnector
from app.connectors.x_twitter import connector as x_module
from app.connectors.x_twitter.connector import XTwitterConnector
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.db.base import utcnow
from app.models.social import POST_METRIC_FIELDS


def _wire(connector, handler):  # noqa: ANN001, ANN202
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


def _png(size: int = 64) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * max(0, size - 8)


# ── The contract ───────────────────────────────────────────────────────────
def test_every_social_connector_reads_post_metrics_with_the_same_shape():
    for cls in (
        FacebookPagesConnector,
        InstagramBusinessConnector,
        LinkedInPagesConnector,
        PinterestConnector,
        XTwitterConnector,
    ):
        assert isinstance(cls(Credentials()), PostMetricsReader), cls.__name__
    assert isinstance(SearchConsoleConnector(Credentials()), SearchDailyReader)
    assert set(empty_post_metrics()) == set(POST_METRIC_FIELDS)
    assert all(value is None for value in empty_post_metrics().values())


def test_figures_are_counts_or_unknown_never_invented():
    assert count_or_none(12) == 12 and count_or_none(3.0) == 3 and count_or_none("40") == 40
    for bad in (None, True, -1, 2.5, "12a", "", [], {}, "9" * 30):
        assert count_or_none(bad) is None, bad
    assert sum_or_none(None, None) is None and sum_or_none(None, 2, 3) == 5


def test_insights_refusal_is_a_reconnect_message():
    error = InsightsAccessMissing("Facebook")
    assert str(error) == "Reconnect Facebook to allow insights."
    assert isinstance(error, ConnectorConfigError) and error.status_code == 422


# ── Facebook ───────────────────────────────────────────────────────────────
def _facebook(handler):  # noqa: ANN001, ANN202
    return _wire(FacebookPagesConnector(Credentials(values={"pageId": "1", "pageAccessToken": "tok"})), handler)


def test_facebook_reads_the_current_post_insights_and_counts():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"data": [
                {"name": "post_media_view", "period": "lifetime", "values": [{"value": 900}]},
                {"name": "post_total_media_view_unique", "period": "lifetime", "values": [{"value": 700}]},
                {"name": "post_reactions_by_type_total", "period": "lifetime",
                 "values": [{"value": {"like": 10, "love": 2}}]},
                {"name": "post_clicks", "period": "lifetime", "values": [{"value": 31}]},
            ]})
        return httpx.Response(200, json={"id": "1_2", "shares": {"count": 4},
                                         "comments": {"data": [], "summary": {"total_count": 6}}})

    fb = _facebook(handler)
    got = fb.read_post_metrics(["1_2", "1_2", "../me", ""])
    assert list(got) == ["1_2"]  # deduplicated; a malformed id is never sent
    assert got["1_2"] == {
        "impressions": 900, "reach": 700, "engagements": None, "clicks": 31,
        "likes": 12, "comments": 6, "shares": 4, "saves": None,
    }
    insights = seen[0]
    metrics = insights.url.params["metric"].split(",")
    assert metrics == ["post_media_view", "post_total_media_view_unique", "post_reactions_by_type_total", "post_clicks"]
    assert not any(m.startswith("post_impressions") for m in metrics)
    assert insights.url.params["period"] == "lifetime"
    assert insights.headers["Authorization"] == "Bearer tok"
    assert len(seen) == 2


def test_facebook_without_read_insights_is_told_to_reconnect():
    fb = _facebook(lambda r: httpx.Response(400, json={"error": {"code": 200, "message": "requires read_insights"}}))
    with pytest.raises(InsightsAccessMissing, match="Reconnect Facebook to allow insights"):
        fb.read_post_metrics(["1_2"])


def test_facebook_skips_a_post_it_cannot_report_but_stops_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        if "1_9" in request.url.path:
            return httpx.Response(400, json={"error": {"code": 100, "message": "Unsupported get request"}})
        if request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={})

    got = _facebook(handler).read_post_metrics(["1_9", "1_3"])
    assert list(got) == ["1_3"]
    assert got["1_3"]["impressions"] is None and got["1_3"]["shares"] == 0

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(ConnectorError):
        _facebook(down).read_post_metrics(["1_3"])


def test_meta_connections_ask_for_the_insights_permissions():
    assert "read_insights" in FacebookPagesConnector.spec.oauth.scopes
    assert "instagram_manage_insights" in InstagramBusinessConnector.spec.oauth.scopes


# ── Instagram ──────────────────────────────────────────────────────────────
def _instagram(handler):  # noqa: ANN001, ANN202
    return _wire(InstagramBusinessConnector(Credentials(values={"igUserId": "5", "accessToken": "tok"})), handler)


def test_instagram_reads_views_not_impressions_and_maps_every_figure():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [
            {"name": "reach", "values": [{"value": 300}]},
            {"name": "views", "values": [{"value": 450}]},
            {"name": "likes", "values": [{"value": 40}]},
            {"name": "comments", "values": [{"value": 5}]},
            {"name": "saved", "values": [{"value": 7}]},
            {"name": "shares", "total_value": {"value": 3}},
            {"name": "total_interactions", "values": [{"value": 55}]},
        ]})

    got = _instagram(handler).read_post_metrics(["1789"])
    assert got["1789"] == {
        "impressions": 450, "reach": 300, "engagements": 55, "clicks": None,
        "likes": 40, "comments": 5, "shares": 3, "saves": 7,
    }
    metrics = seen[0].url.params["metric"].split(",")
    assert "impressions" not in metrics
    assert set(metrics) == {"reach", "views", "likes", "comments", "saved", "shares", "total_interactions"}
    assert seen[0].url.path.endswith("/1789/insights")


def test_instagram_without_insights_permission_is_told_to_reconnect():
    ig = _instagram(lambda r: httpx.Response(403, json={"error": {"code": 10, "message": "no permission"}}))
    with pytest.raises(InsightsAccessMissing, match="Reconnect Instagram"):
        ig.read_post_metrics(["1789"])


def test_instagram_skips_an_album_child_and_ignores_malformed_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/111/" in request.url.path:
            return httpx.Response(400, json={"error": {"code": 100, "message": "not supported for album children"}})
        return httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 9}]}]})

    got = _instagram(handler).read_post_metrics(["111", "222", "2/../../me"])
    assert list(got) == ["222"] and got["222"]["reach"] == 9 and got["222"]["impressions"] is None


# ── LinkedIn ───────────────────────────────────────────────────────────────
def test_linkedin_reads_share_statistics_with_the_versioned_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"elements": [
            {"share": "urn:li:share:70", "totalShareStatistics": {
                "impressionCount": 1000, "uniqueImpressionsCount": 800, "clickCount": 20,
                "likeCount": 15, "commentCount": 4, "shareCount": 2, "engagement": 0.041}},
            {"ugcPost": "urn:li:ugcPost:71", "totalShareStatistics": {"impressionCount": 5}},
            {"share": "urn:li:share:999", "totalShareStatistics": {"impressionCount": 1}},
        ]})

    li = _wire(LinkedInPagesConnector(Credentials(values={"organizationId": "555", "accessToken": "tok"})), handler)
    got = li.read_post_metrics(["urn:li:share:70", "urn:li:ugcPost:71", "urn:li:organization:1"])
    assert got["urn:li:share:70"] == {
        "impressions": 1000, "reach": 800, "engagements": 41, "clicks": 20,
        "likes": 15, "comments": 4, "shares": 2, "saves": None,
    }
    assert got["urn:li:ugcPost:71"]["impressions"] == 5 and got["urn:li:ugcPost:71"]["engagements"] is None
    assert "urn:li:share:999" not in got  # not asked for
    request = seen[0]
    assert request.url.path == "/rest/organizationalEntityShareStatistics"
    query = unquote(request.url.query.decode())
    assert "q=organizationalEntity" in query
    assert "organizationalEntity=urn:li:organization:555" in query
    assert "shares=List(urn:li:share:70)" in query and "ugcPosts=List(urn:li:ugcPost:71)" in query
    assert "urn:li:organization:1" not in query.split("organizationalEntity=")[-1].replace("555", "")
    assert request.headers["LinkedIn-Version"] == settings.linkedin_api_version
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"


def test_linkedin_without_the_statistics_permission_is_told_to_reconnect():
    li = _wire(LinkedInPagesConnector(Credentials(values={"organizationId": "1", "accessToken": "t"})),
               lambda r: httpx.Response(403, json={"status": 403, "code": "ACCESS_DENIED"}))
    with pytest.raises(InsightsAccessMissing, match="Reconnect LinkedIn"):
        li.read_post_metrics(["urn:li:share:1"])


def test_linkedin_version_comes_from_settings_unless_the_connection_chose_one():
    def version(values: dict) -> str:
        return LinkedInPagesConnector(Credentials(values={"organizationId": "1", **values})).api_version

    assert version({}) == settings.linkedin_api_version
    assert version({"apiVersion": "202606"}) == settings.linkedin_api_version  # the old saved default
    assert version({"apiVersion": "202701"}) == "202701"
    with pytest.raises(ConnectorConfigError, match="not a LinkedIn API version"):
        version({"apiVersion": "2026\r\nX-Evil: 1"})


# ── X ──────────────────────────────────────────────────────────────────────
def _x(handler, values: dict | None = None):  # noqa: ANN001, ANN202
    return _wire(XTwitterConnector(Credentials(values=values or {"accessToken": "oauth2"})), handler)


def test_x_reads_public_metrics_only_in_batches():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [
            {"id": "11", "public_metrics": {"retweet_count": 2, "reply_count": 3, "like_count": 9,
                                            "quote_count": 1, "bookmark_count": 4, "impression_count": 500}},
            {"id": "12", "public_metrics": {"like_count": 1}},
        ]})

    got = _x(handler).read_post_metrics(["11", "12", "not-an-id"])
    assert got["11"] == {
        "impressions": 500, "reach": None, "engagements": None, "clicks": None,
        "likes": 9, "comments": 3, "shares": 3, "saves": 4,
    }
    assert got["12"]["likes"] == 1 and got["12"]["impressions"] is None
    assert len(seen) == 1
    assert seen[0].url.path == "/2/tweets"
    assert seen[0].url.params["ids"] == "11,12" and seen[0].url.params["tweet.fields"] == "public_metrics"


def test_x_reads_no_more_posts_than_the_per_run_cap(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "x_metrics_max_reads_per_run", 3)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    _x(handler).read_post_metrics([str(n) for n in range(1, 50)])
    assert seen[0].url.params["ids"] == "1,2,3"

    monkeypatch.setattr(settings, "x_metrics_max_reads_per_run", 0)
    seen.clear()
    assert _x(handler).read_post_metrics(["1", "2"]) == {} and seen == []


def test_x_uploads_an_image_in_chunks_and_attaches_its_media_id(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(x_module, "CHUNK_BYTES", 40)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/2/media/upload/initialize":
            return httpx.Response(200, json={"data": {"id": "777", "expires_after_secs": 86400}})
        if path == "/2/media/upload/777/append":
            return httpx.Response(204)
        if path == "/2/media/upload/777/finalize":
            return httpx.Response(200, json={"data": {"id": "777", "media_key": "3_777"}})
        if path == "/2/tweets":
            return httpx.Response(201, json={"data": {"id": "1900"}})
        return httpx.Response(404)

    image = _png(100)
    assert _x(handler).publish_post(text="Hi", image_bytes=image, image_mime="image/png") == (
        "1900", "https://x.com/i/web/status/1900",
    )
    paths = [r.url.path for r in seen]
    assert paths == [
        "/2/media/upload/initialize",
        "/2/media/upload/777/append", "/2/media/upload/777/append", "/2/media/upload/777/append",
        "/2/media/upload/777/finalize",
        "/2/tweets",
    ]
    init = json.loads(seen[0].content)
    assert init == {"media_type": "image/png", "total_bytes": 100, "media_category": "tweet_image"}
    chunks = [json.loads(r.content) for r in seen[1:4]]
    assert [c["segment_index"] for c in chunks] == [0, 1, 2]
    assert b"".join(base64.b64decode(c["media"]) for c in chunks) == image
    assert json.loads(seen[-1].content) == {"text": "Hi", "media": {"media_ids": ["777"]}}


def test_x_waits_briefly_while_an_image_is_processed(monkeypatch):  # noqa: ANN001
    pauses: list[float] = []
    monkeypatch.setattr(XTwitterConnector, "_sleep", staticmethod(pauses.append))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/initialize"):
            return httpx.Response(200, json={"data": {"id": "5"}})
        if path.endswith("/finalize"):
            return httpx.Response(200, json={"data": {"id": "5", "processing_info": {"state": "pending", "check_after_secs": 60}}})
        if path == "/2/media/upload" and request.method == "GET":
            assert request.url.params["command"] == "STATUS" and request.url.params["media_id"] == "5"
            return httpx.Response(200, json={"data": {"processing_info": {"state": "succeeded"}}})
        return httpx.Response(200, json={})

    assert _x(handler).upload_image(_png()) == "5"
    assert pauses == [x_module.MAX_PROCESSING_PAUSE_SECONDS]  # 60s asked, capped


def test_x_refuses_images_it_cannot_take_before_calling_x():
    x = _x(lambda r: pytest.fail("no request should be made"))
    with pytest.raises(ConnectorConfigError, match="JPEG, PNG or WebP"):
        x.publish_post(text="Hi", image_bytes=b"GIF89a....", image_mime="image/gif")
    with pytest.raises(ConnectorConfigError, match="up to 5 MB"):
        x.publish_post(text="Hi", image_bytes=_png(x_module.MAX_IMAGE_BYTES + 1), image_mime="image/png")


def test_x_without_media_write_posts_text_only_and_says_to_reconnect():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/media/upload" in request.url.path:
            return httpx.Response(403, json={"title": "Forbidden"})
        return httpx.Response(201, json={"data": {"id": "1950"}})

    x = _x(handler)
    assert x.publish_post(text="Hi", image_bytes=_png(), image_mime="image/png")[0] == "1950"
    assert json.loads(seen[-1].content) == {"text": "Hi"}  # no media attached
    assert x.last_post_note == "Posted without its image. Reconnect X to allow images."
    assert x.publish_post(text="Again")[0] == "1950" and x.last_post_note == ""

    # A refused post itself still fails, with what it always said.
    refused = _x(lambda r: httpx.Response(403, json={"title": "Forbidden"}))
    with pytest.raises(ConnectorConfigError, match="Read and write"):
        refused.publish_post(text="Hi", image_bytes=_png(), image_mime="image/png")
    # An image X cannot take is not silently dropped.
    with pytest.raises(ConnectorConfigError, match="JPEG, PNG or WebP"):
        _x(handler).publish_post(text="Hi", image_bytes=b"GIF89a", image_mime="image/gif")


def test_x_asks_for_media_write():
    assert "media.write" in XTwitterConnector.spec.oauth.scopes


# ── Pinterest ──────────────────────────────────────────────────────────────
def _pinterest(handler):  # noqa: ANN001, ANN202
    return _wire(PinterestConnector(Credentials(values={"boardId": "55", "accessToken": "tok"})), handler)


def test_pinterest_reads_pin_analytics_within_90_days():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"all": {
            "daily_metrics": [],
            "summary_metrics": {"IMPRESSION": 120, "SAVE": 6, "PIN_CLICK": 14, "OUTBOUND_CLICK": 5},
        }})

    got = _pinterest(handler).read_post_metrics(["987", "abc"])
    assert got == {"987": {
        "impressions": 120, "reach": None, "engagements": None, "clicks": 14,
        "likes": None, "comments": None, "shares": None, "saves": 6,
    }}
    params = seen[0].url.params
    assert seen[0].url.path == "/v5/pins/987/analytics"
    start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
    assert end == utcnow().date() and timedelta(0) < end - start <= timedelta(days=90)
    assert params["metric_types"].split(",") == ["IMPRESSION", "SAVE", "PIN_CLICK", "OUTBOUND_CLICK"]


def test_pinterest_refusals():
    with pytest.raises(InsightsAccessMissing, match="Reconnect Pinterest"):
        _pinterest(lambda r: httpx.Response(403, json={"code": 3})).read_post_metrics(["1"])
    assert _pinterest(lambda r: httpx.Response(404, json={"code": 50})).read_post_metrics(["1"]) == {}


def test_pinterest_descriptions_may_be_800_characters():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "1"})

    _pinterest(handler).publish_post(text="d" * 900, title="T", link="https://s.example/", image_bytes=b"IMG")
    assert len(json.loads(seen[0].content)["description"]) == 800


# ── Search Console ─────────────────────────────────────────────────────────
def test_search_console_reads_clicks_impressions_and_position_by_day():
    seen: list[httpx.Request] = []
    end = utcnow().date() - timedelta(days=3)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": [
            {"keys": [end.isoformat()], "clicks": 4, "impressions": 90, "ctr": 0.04, "position": 7.256},
            {"keys": [(end - timedelta(days=1)).isoformat()], "clicks": 0, "impressions": 10, "position": 0},
            {"keys": ["not a date"], "clicks": 1},
            {"keys": ["1999-01-01"], "clicks": 1},
        ]})

    gsc = _wire(
        SearchConsoleConnector(Credentials(values={"siteUrl": "sc-domain:example.com", "accessToken": "tok"})), handler
    )
    days = gsc.read_search_daily(7)
    assert days == [
        SearchDay(day=end - timedelta(days=1), clicks=0, impressions=10, position=None),
        SearchDay(day=end, clicks=4, impressions=90, position=7.26),
    ]
    body = json.loads(seen[0].content)
    assert body["dimensions"] == ["date"] and body["endDate"] == end.isoformat()
    assert body["startDate"] == (end - timedelta(days=6)).isoformat() and body["rowLimit"] == 7
    assert str(seen[0].url).endswith("/sites/sc-domain%3Aexample.com/searchAnalytics/query")

    gsc.read_search_daily(100_000)
    assert json.loads(seen[-1].content)["rowLimit"] == 480


# ── Sign-in failure codes (contract 3.8) ───────────────────────────────────
#: The real client, kept before any test patches it (the OAuth modules share
#: one httpx module, so a second patch would otherwise wrap the first).
_REAL_CLIENT = httpx.Client


def _mock_httpx(monkeypatch, module: str, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []
    real = _REAL_CLIENT

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs.pop("timeout", None)
        return real(transport=httpx.MockTransport(recording), **kwargs)

    monkeypatch.setattr(f"{module}.httpx.Client", factory)
    return seen


@pytest.fixture
def business_apps(monkeypatch):  # noqa: ANN001, ANN201
    for name in ("shopify", "mailchimp", "github", "slack"):
        monkeypatch.setattr(settings, f"oauth_{name}_client_id", f"{name}-client")
        monkeypatch.setattr(settings, f"oauth_{name}_client_secret", f"{name}-secret")


def _shopify_items(shop: str, secret: str) -> list[tuple[str, str]]:
    import hashlib
    import hmac

    items = [("code", "c0de"), ("shop", shop), ("state", "STATE"), ("timestamp", "1700000000")]
    message = "&".join(f"{k}={v}" for k, v in sorted(items))
    return [*items, ("hmac", hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest())]


def test_business_sign_in_failures_carry_their_codes(business_apps, monkeypatch):  # noqa: ANN001
    from app.connectors.base import oauth_providers
    from app.services import oauth_business, oauth_flow

    shop = oauth_providers.SHOPIFY
    checked = {"shop": "mystore.myshopify.com"}
    with pytest.raises(oauth_flow.OAuthFlowError) as forged:
        oauth_business.verify_callback(shop, _shopify_items("mystore.myshopify.com", "forged"), checked)
    assert forged.value.code == oauth_flow.LINK_INVALID
    with pytest.raises(oauth_flow.OAuthFlowError) as other_store:
        oauth_business.verify_callback(shop, _shopify_items("other.myshopify.com", "shopify-secret"), checked)
    assert other_store.value.code == oauth_flow.SITE_MISMATCH
    with pytest.raises(oauth_flow.OAuthFlowError) as bad_host:
        oauth_business.token_url(shop, {})
    assert bad_host.value.code == oauth_flow.LINK_INVALID

    _mock_httpx(monkeypatch, "app.services.oauth_business", lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(oauth_flow.OAuthFlowError) as refused:
        oauth_business.exchange(shop, code="c", verifier="", vars_=checked, params={})
    assert refused.value.code == oauth_flow.PROVIDER_REFUSED

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    _mock_httpx(monkeypatch, "app.services.oauth_business", down)
    with pytest.raises(oauth_flow.OAuthFlowError) as unreachable:
        oauth_business.exchange(shop, code="c", verifier="", vars_=checked, params={})
    assert unreachable.value.code == oauth_flow.PROVIDER_UNREACHABLE
    with pytest.raises(oauth_flow.OAuthFlowError) as mailchimp_down:
        oauth_business.mailchimp_metadata(oauth_providers.MAILCHIMP, "token")
    assert mailchimp_down.value.code == oauth_flow.PROVIDER_UNREACHABLE

    _mock_httpx(monkeypatch, "app.services.oauth_business", lambda r: httpx.Response(200, json={"dc": "../x"}))
    with pytest.raises(oauth_flow.OAuthFlowError) as mailchimp_odd:
        oauth_business.mailchimp_metadata(oauth_providers.MAILCHIMP, "token")
    assert mailchimp_odd.value.code == oauth_flow.PROVIDER_REFUSED

    _mock_httpx(monkeypatch, "app.services.oauth_business", lambda r: httpx.Response(200, json={"installations": []}))
    with pytest.raises(oauth_flow.OAuthFlowError) as not_theirs:
        oauth_business.github_verify_installation("token", "99")
    assert not_theirs.value.code == oauth_flow.LINK_INVALID


def test_vendor_sign_in_failures_carry_their_codes(monkeypatch):  # noqa: ANN001
    from app.connectors.base import oauth_providers
    from app.services import oauth_flow, oauth_vendors

    _mock_httpx(monkeypatch, "app.services.oauth_vendors", lambda r: httpx.Response(400, json={"error": {"code": 100}}))
    with pytest.raises(oauth_flow.OAuthFlowError) as refused:
        oauth_vendors.exchange(oauth_providers.META, code="c", verifier="", now=1.0)
    assert refused.value.code == oauth_flow.PROVIDER_REFUSED
    assert "Facebook did not accept" in str(refused.value)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    _mock_httpx(monkeypatch, "app.services.oauth_vendors", down)
    with pytest.raises(oauth_flow.OAuthFlowError) as unreachable:
        oauth_vendors.exchange(oauth_providers.TIKTOK, code="c", verifier="", now=1.0)
    assert unreachable.value.code == oauth_flow.PROVIDER_UNREACHABLE

    monkeypatch.setattr(settings, "reddit_enabled", False)
    with pytest.raises(oauth_flow.OAuthFlowError) as switched_off:
        oauth_vendors.exchange(oauth_providers.REDDIT, code="c", verifier="", now=1.0)
    assert switched_off.value.code == oauth_flow.NOT_SET_UP
