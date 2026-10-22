"""The Reddit connector (step 3.6, research R10).

Every call goes to a mock transport: the connector's own client through
``tests/support/verify_http.install`` and the token calls through a patched
``httpx.Client``. Covers: off by default, the User-Agent requirement, the
sign-in provider (authorize URL, Basic-auth exchange, refresh that keeps the
original refresh token), subreddit validation, the pre-submit checks against
``post_requirements`` and ``about``, rate-limit deferral, per-subreddit
spacing, metrics, and that no token reaches the logs.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import oauth_providers, registry
from app.connectors.base.credentials import ACCESS_TOKEN_EXPIRY_KEY, Credentials
from app.connectors.reddit import connector as reddit
from app.connectors.reddit.connector import (
    RedditConnector,
    RedditPostRefused,
    RedditRateLimited,
    check_post,
    normalise_subreddit,
    parse_rules,
)
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.models.social import POST_METRIC_FIELDS
from app.services import oauth_flow, provider_setup
from app.services.oauth_flow import OAuthFlowError
from tests.support.verify_http import install

UA = "web:com.example.marketing:1.0 (by /u/example_owner)"
REFRESH = "rt-SECRET-refresh-111111"
ACCESS = "at-SECRET-access-222222"
CLIENT_SECRET = "reddit-client-secret-333"
ABOUT = {"kind": "t5", "data": {"display_name": "marketing", "submission_type": "any", "allow_images": True}}
SUBMITTED = {
    "json": {
        "errors": [],
        "data": {"id": "abc123", "name": "t3_abc123", "url": "https://www.reddit.com/r/marketing/comments/abc123/hi/"},
    }
}


@pytest.fixture(autouse=True)
def _reddit_on(monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setattr(settings, "reddit_enabled", True)
    monkeypatch.setattr(settings, "reddit_user_agent", UA)
    monkeypatch.setattr(settings, "reddit_min_minutes_between_posts", 10)
    monkeypatch.setattr(settings, "oauth_reddit_client_id", "reddit-client-id")
    monkeypatch.setattr(settings, "oauth_reddit_client_secret", CLIENT_SECRET)
    reddit.reset_state()
    yield
    reddit.reset_state()


def _connector(**values: str) -> RedditConnector:
    base = {
        "oauthSource": "platform",
        "refreshToken": REFRESH,
        "accessToken": ACCESS,
        ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3000),
        "subreddit": "marketing",
    }
    base.update(values)
    return RedditConnector(Credentials(values=base, oauth_completed=True), org_id="org-1")


def _post_routes(requirements: object = None, about: object = None, submit: object = None) -> list:
    return [
        ("GET", r"/r/marketing/about", about if about is not None else ABOUT),
        ("GET", r"/api/v1/marketing/post_requirements", requirements if requirements is not None else {}),
        ("POST", r"/api/submit$", submit if submit is not None else SUBMITTED),
    ]


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _mock_token_client(monkeypatch, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []
    real = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs.pop("timeout", None)
        return real(transport=httpx.MockTransport(recording), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return seen


# ── Off by default ─────────────────────────────────────────────────────────
def test_reddit_is_off_by_default():
    assert type(settings).model_fields["reddit_enabled"].default is False


def test_switched_off_the_connector_refuses_without_calling_reddit(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "reddit_enabled", False)
    connector = _connector()
    recorder = install(connector, _post_routes())
    with pytest.raises(ConnectorConfigError) as caught:
        connector.publish_post(text="Hello", title="Hello there")
    assert "switched off" in str(caught.value) and "approve" in str(caught.value)
    with pytest.raises(ConnectorConfigError):
        connector.read_post_metrics(["t3_abc"])
    assert recorder.calls == []


def test_switched_off_the_sign_in_is_not_offered(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "reddit_enabled", False)
    spec = registry.get_spec("reddit")
    assert not oauth_providers.REDDIT.configured
    assert not oauth_providers.sign_in_available(spec)
    assert "REDDIT_ENABLED" in oauth_providers.missing_settings(spec)
    with pytest.raises(OAuthFlowError):
        oauth_flow.build_authorize_url(oauth_providers.REDDIT, spec, state="s")


def test_the_description_says_reddit_must_approve_it():
    spec = registry.get_spec("reddit")
    assert "Needs Reddit's approval" in spec.description
    assert spec.category == "Social media"
    assert spec.base_url == "https://oauth.reddit.com"
    setup = provider_setup.setup_for("reddit")
    assert setup is not None and "approval" in setup.review_note
    assert "reddit_user_agent" in {f.key for f in setup.fields}


# ── User-Agent ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["", "short", "bad\r\nX-Injected: 1", "x" * 300])
def test_a_missing_or_malformed_user_agent_is_a_setup_message(monkeypatch, value: str):  # noqa: ANN001
    monkeypatch.setattr(settings, "reddit_user_agent", value)
    connector = _connector()
    recorder = install(connector, _post_routes())
    with pytest.raises(ConnectorConfigError) as caught:
        connector.publish_post(text="Hello", title="Hello there")
    assert "isn't fully set up" in str(caught.value)
    assert recorder.calls == []


def test_every_call_carries_the_user_agent_and_bearer_token():
    connector = _connector()
    recorder = install(connector, _post_routes())
    connector.publish_post(text="Body text", title="A good title")
    assert recorder.calls
    for call in recorder.calls:
        assert call.headers["User-Agent"] == UA
        assert call.headers["Authorization"] == "Bearer " + ACCESS
        assert str(call.url).startswith("https://oauth.reddit.com/")


# ── Sign-in provider ───────────────────────────────────────────────────────
def test_the_authorize_url_asks_for_a_permanent_grant_and_the_four_scopes():
    spec = registry.get_spec("reddit")
    assert oauth_providers.sign_in_available(spec)
    url = oauth_flow.build_authorize_url(oauth_providers.REDDIT, spec, state="the-state", code_challenge="c")
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://www.reddit.com/api/v1/authorize"
    assert query["duration"] == "permanent"
    assert query["scope"] == "identity mysubreddits submit read"
    assert query["state"] == "the-state"
    assert query["response_type"] == "code"
    assert "code_challenge" not in query


def test_the_code_exchange_uses_basic_auth_and_the_user_agent(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"name": "brand_account"})
        return httpx.Response(
            200,
            json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600, "scope": "identity submit read mysubreddits"},
        )

    seen = _mock_token_client(monkeypatch, handler)
    tokens = oauth_flow.exchange_code(oauth_providers.REDDIT, code="the-code", verifier="", now=1000.0)
    token_call = seen[0]
    assert str(token_call.url) == "https://www.reddit.com/api/v1/access_token"
    assert token_call.headers["Authorization"] == oauth_flow.basic_auth("reddit-client-id", CLIENT_SECRET)
    assert token_call.headers["User-Agent"] == UA
    sent = _form(token_call)
    assert sent == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": oauth_providers.REDDIT.redirect_uri,
    }
    assert tokens.refresh_token == REFRESH and tokens.expires_at == 4600.0
    assert tokens.account == "u/brand_account"
    assert seen[1].headers["User-Agent"] == UA


def test_a_refused_exchange_is_plain_and_logs_no_code_or_token(monkeypatch, caplog):  # noqa: ANN001
    # Reddit can refuse with a 200 and an error field.
    _mock_token_client(monkeypatch, lambda r: httpx.Response(200, json={"error": "invalid_grant"}))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(OAuthFlowError) as caught:
        oauth_flow.exchange_code(oauth_providers.REDDIT, code="secret-code-xyz", verifier="")
    assert "did not accept" in str(caught.value)
    assert "secret-code-xyz" not in caplog.text and CLIENT_SECRET not in caplog.text


def test_the_exchange_is_refused_without_a_user_agent(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "reddit_user_agent", "")
    seen = _mock_token_client(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "x"}))
    with pytest.raises(OAuthFlowError) as caught:
        oauth_flow.exchange_code(oauth_providers.REDDIT, code="c", verifier="")
    assert "not fully set up" in str(caught.value)
    assert seen == []


def test_a_refresh_keeps_the_original_refresh_token(monkeypatch, caplog):  # noqa: ANN001
    connector = _connector(**{ACCESS_TOKEN_EXPIRY_KEY: "0"})
    recorder = install(connector, [("GET", r"/api/v1/me$", {"name": "brand_account"})])
    seen = _mock_token_client(
        monkeypatch, lambda r: httpx.Response(200, json={"access_token": "at-NEW-999999", "expires_in": 3600})
    )
    caplog.set_level(logging.DEBUG)
    assert connector.check_health().ok
    refresh_call = seen[0]
    assert str(refresh_call.url) == "https://www.reddit.com/api/v1/access_token"
    assert refresh_call.headers["User-Agent"] == UA
    assert refresh_call.headers["Authorization"].startswith("Basic ")
    assert _form(refresh_call) == {"grant_type": "refresh_token", "refresh_token": REFRESH}
    assert connector.credentials.values["refreshToken"] == REFRESH
    assert recorder.calls[0].headers["Authorization"] == "Bearer at-NEW-999999"
    for secret in (REFRESH, "at-NEW-999999", CLIENT_SECRET):
        assert secret not in caplog.text


def test_a_refused_refresh_asks_to_sign_in_again_and_logs_no_token(monkeypatch, caplog):  # noqa: ANN001
    connector = _connector(**{ACCESS_TOKEN_EXPIRY_KEY: "0"})
    install(connector, [])
    _mock_token_client(monkeypatch, lambda r: httpx.Response(400, text=f"invalid_grant {REFRESH}"))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.publish_post(text="x", title="A title")
    assert "Sign in again" in str(caught.value)
    assert REFRESH not in caplog.text and REFRESH not in str(caught.value)


# ── Subreddit names ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["ab", "a" * 22, "bad-name", "../etc", "ok?x=1", "spaces here", "名前名前", ""])
def test_an_invalid_subreddit_is_refused_before_any_call(name: str):
    connector = _connector()
    recorder = install(connector, _post_routes())
    with pytest.raises(ConnectorConfigError) as caught:
        connector.publish_post(text="Hello", title="Hello there", subreddit=name or "  ")
    assert "valid subreddit name" in str(caught.value)
    assert recorder.calls == []


def test_r_slash_prefixes_are_accepted():
    assert normalise_subreddit("r/Marketing") == "Marketing"
    assert normalise_subreddit(" /r/ask_me/ ") == "ask_me"


# ── Posting ────────────────────────────────────────────────────────────────
def test_a_text_post_reads_the_rules_then_submits():
    connector = _connector()
    recorder = install(connector, _post_routes(requirements={"title_text_min_length": 5}))
    remote_id, url = connector.publish_post(text="Our spring guide is out.", title="Spring guide")
    assert (remote_id, url) == ("t3_abc123", "https://www.reddit.com/r/marketing/comments/abc123/hi/")
    assert recorder.index("GET", r"/about") < recorder.index("POST", r"/api/submit")
    assert recorder.index("GET", r"/post_requirements") < recorder.index("POST", r"/api/submit")
    assert _form(recorder.find("POST", r"/api/submit")[0]) == {
        "sr": "marketing",
        "kind": "self",
        "title": "Spring guide",
        "api_type": "json",
        "text": "Our spring guide is out.",
    }


def test_an_image_is_posted_as_a_link_to_its_public_address():
    connector = _connector()
    recorder = install(connector, _post_routes())
    image = "https://api.example.com/api/v1/public/images/t/i/9999999999/sig"
    connector.publish_post(text="See it", title="New banner", image_url=image, image_bytes=b"\x89PNG")
    sent = _form(recorder.find("POST", r"/api/submit")[0])
    assert sent["kind"] == "link" and sent["url"] == image and "text" not in sent


def test_image_bytes_alone_post_without_the_image_and_say_so():
    connector = _connector()
    recorder = install(connector, _post_routes())
    connector.publish_post(text="See it", title="New banner", link="https://example.com/a", image_bytes=b"\x89PNG")
    sent = _form(recorder.find("POST", r"/api/submit")[0])
    assert sent["kind"] == "link" and sent["url"] == "https://example.com/a"
    assert "without its image" in connector.last_post_note


def test_a_text_only_subreddit_gets_the_link_in_the_text():
    connector = _connector()
    about = {"kind": "t5", "data": {"submission_type": "self"}}
    recorder = install(connector, _post_routes(about=about))
    connector.publish_post(text="Read more", title="A title here", link="https://example.com/a")
    sent = _form(recorder.find("POST", r"/api/submit")[0])
    assert sent["kind"] == "self" and sent["text"].endswith("https://example.com/a")


def test_a_title_failing_post_requirements_is_refused_before_submitting():
    connector = _connector()
    recorder = install(connector, _post_routes(requirements={"title_text_min_length": 20}))
    with pytest.raises(RedditPostRefused) as caught:
        connector.publish_post(text="Body", title="Too short")
    assert "at least 20 characters" in str(caught.value)
    assert recorder.find("POST", r"/api/submit") == []


@pytest.mark.parametrize(
    ("requirements", "about", "kwargs", "expected"),
    [
        ({"is_flair_required": True}, None, {}, "requires a post flair"),
        ({"title_blacklisted_strings": ["sale"]}, None, {"title": "Big SALE today"}, "does not allow"),
        ({"title_required_strings": ["[OC]"]}, None, {}, "include one of"),
        ({"body_restriction_policy": "required"}, None, {"text": ""}, "requires text"),
        (
            {"link_restriction_policy": "whitelist", "domain_whitelist": ["youtube.com"]},
            None,
            {"link": "https://example.com/a"},
            "only accepts links",
        ),
        ({}, {"kind": "t5", "data": {"user_is_banned": True}}, {}, "banned"),
        ({}, {"kind": "t5", "data": {"subreddit_type": "restricted"}}, {}, "approved members"),
        ({}, {"kind": "Listing", "data": {"children": []}}, {}, "doesn't exist"),
        ({}, {"kind": "t5", "data": {"submission_type": "link"}}, {}, "link posts only"),
    ],
)
def test_subreddit_rules_refuse_with_a_plain_reason(requirements, about, kwargs, expected: str):  # noqa: ANN001
    connector = _connector()
    recorder = install(connector, _post_routes(requirements=requirements, about=about))
    args = {"text": "Some body text", "title": "A perfectly fine title", **kwargs}
    with pytest.raises(RedditPostRefused) as caught:
        connector.publish_post(**args)
    assert expected in str(caught.value)
    assert recorder.find("POST", r"/api/submit") == []


def test_post_requirements_are_parsed_defensively():
    rules = parse_rules(
        {
            "title_text_min_length": "ten",
            "title_text_max_length": True,
            "title_required_strings": "not a list",
            "domain_whitelist": [1, None, "WWW.Example.com"],
            "body_restriction_policy": "something new",
            "is_flair_required": "yes",
        },
        ["not", "a", "dict"],
    )
    assert rules.title_min is None and rules.title_max is None
    assert rules.title_required == () and rules.domains_allowed == ("example.com",)
    assert rules.body_policy == "" and rules.flair_required is False
    assert parse_rules(None, None).exists is True


def test_moderator_title_patterns_are_never_run_here():
    """A catastrophic pattern would stall this process; Reddit checks it on submit."""
    rules = parse_rules({"title_regexes": ["(a+)+$"]}, ABOUT)
    started = time.monotonic()
    assert check_post(rules, subreddit="marketing", title="a" * 60 + "!", kind="self", url="", body="") == ""
    assert time.monotonic() - started < 1
    assert rules.title_regex_count == 1


def test_a_missing_post_requirements_endpoint_does_not_block_the_post():
    connector = _connector()
    routes = _post_routes()
    routes[1] = ("GET", r"/post_requirements", (404, {"message": "Not Found"}))
    install(connector, routes)
    assert connector.publish_post(text="Body", title="A good title")[0] == "t3_abc123"


@pytest.mark.parametrize(
    ("code", "expected", "error"),
    [
        ("SUBREDDIT_NOTALLOWED", "isn't allowed to post", RedditPostRefused),
        ("SUBMIT_VALIDATION_TITLE_REGEX_REQUIREMENT", "title rules", RedditPostRefused),
        ("SOMETHING_NEW", "did not accept the post", ConnectorError),
    ],
)
def test_submit_errors_become_plain_sentences(code: str, expected: str, error: type):
    connector = _connector()
    submit = {"json": {"errors": [[code, "raw vendor text <b>", "sr"]], "data": {}}}
    install(connector, _post_routes(submit=submit))
    with pytest.raises(error) as caught:
        connector.publish_post(text="Body", title="A good title")
    assert expected in str(caught.value) and "raw vendor text" not in str(caught.value)


# ── Rate limits and spacing ────────────────────────────────────────────────
def _with_headers(connector: RedditConnector, routes: dict[str, tuple[int, object]], headers: dict) -> list:
    """Like ``install``, for responses that carry rate-limit headers."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for suffix, (status, body) in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(status, json=body, headers=headers)
        raise AssertionError(f"Unexpected call: {request.method} {request.url}")

    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return calls


def test_a_429_defers_instead_of_retrying_and_the_next_call_waits():
    connector = _connector()
    calls = _with_headers(connector, {"/about": (429, {})}, {"X-Ratelimit-Reset": "120"})
    with pytest.raises(RedditRateLimited) as caught:
        connector.publish_post(text="Body", title="A good title")
    assert caught.value.retry_after_seconds == 120
    assert "slow down" in str(caught.value)
    assert len(calls) == 1
    with pytest.raises(RedditRateLimited):
        connector.read_post_metrics(["t3_abc"])
    assert len(calls) == 1


def test_remaining_zero_defers_the_next_call_without_sending_it():
    connector = _connector()
    headers = {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "300", "X-Ratelimit-Used": "100"}
    calls = _with_headers(connector, {"/about": (200, ABOUT)}, headers)
    with pytest.raises(RedditRateLimited) as caught:
        connector.publish_post(text="Body", title="A good title")
    assert 290 <= caught.value.retry_after_seconds <= 300
    assert [c.url.path for c in calls] == ["/r/marketing/about"]


def test_plenty_remaining_does_not_defer():
    connector = _connector()
    headers = {"X-Ratelimit-Remaining": "57", "X-Ratelimit-Reset": "300"}
    routes = {"/about": (200, ABOUT), "/post_requirements": (200, {}), "/api/submit": (200, SUBMITTED)}
    calls = _with_headers(connector, routes, headers)
    assert connector.publish_post(text="Body", title="A good title")[0] == "t3_abc123"
    assert len(calls) == 3


def test_a_ratelimit_submit_error_is_a_deferral():
    connector = _connector()
    submit = {"json": {"errors": [["RATELIMIT", "Take a break for 7 minutes before trying again.", "ratelimit"]]}}
    install(connector, _post_routes(submit=submit))
    with pytest.raises(RedditRateLimited) as caught:
        connector.publish_post(text="Body", title="A good title")
    assert caught.value.retry_after_seconds == 420


def test_posts_to_one_subreddit_are_spaced_apart():
    connector = _connector()
    recorder = install(
        connector,
        [*_post_routes(), ("GET", r"/r/other_sub/about", ABOUT), ("GET", r"/api/v1/other_sub/post_requirements", {})],
    )
    connector.publish_post(text="Body", title="A good title")
    with pytest.raises(RedditRateLimited) as caught:
        connector.publish_post(text="Body two", title="Another good title")
    assert "10 minutes apart" in str(caught.value)
    assert 500 < caught.value.retry_after_seconds <= 600
    assert len(recorder.find("POST", r"/api/submit")) == 1
    # Another subreddit is not held back.
    connector.publish_post(text="Body", title="A good title", subreddit="other_sub")
    assert len(recorder.find("POST", r"/api/submit")) == 2


# ── Choosing a subreddit ───────────────────────────────────────────────────
def test_the_choices_are_moderated_then_joined_subreddits():
    def listing(*names: str, after: str | None = None) -> dict:
        return {
            "kind": "Listing",
            "data": {
                "after": after,
                "children": [{"kind": "t5", "data": {"display_name": n, "subscribers": 1200}} for n in names],
            },
        }

    connector = _connector(subreddit="")
    recorder = install(
        connector,
        [
            ("GET", r"/subreddits/mine/moderator", listing("brand_hq")),
            ("GET", r"/subreddits/mine/subscriber.*after=t5_next", listing("second_page")),
            ("GET", r"/subreddits/mine/subscriber", listing("Marketing", "brand_hq", "bad-name!", after="t5_next")),
        ],
    )
    choices = connector.list_oauth_choices()
    assert [c.label for c in choices] == ["r/brand_hq", "r/Marketing", "r/second_page"]
    assert choices[0].values == {"subreddit": "brand_hq"} and "You moderate it" in choices[0].detail
    assert all(c.headers["User-Agent"] == UA for c in recorder.calls)


# ── Metrics ────────────────────────────────────────────────────────────────
def test_metrics_are_score_ratio_and_comments_only():
    connector = _connector()
    info = {
        "kind": "Listing",
        "data": {
            "children": [
                {"kind": "t3", "data": {"name": "t3_abc123", "score": 42, "upvote_ratio": 0.93, "num_comments": 7}},
                {"kind": "t3", "data": {"name": "t3_zzz", "score": "lots", "upvote_ratio": 5, "num_comments": -1}},
            ]
        },
    }
    recorder = install(connector, [("GET", r"/api/info", info)])
    out = connector.read_post_metrics(["t3_abc123", "zzz", "not a valid id!", "t3_abc123"])
    assert recorder.find("GET", r"/api/info")[0].url.params["id"] == "t3_abc123,t3_zzz"
    figures = out["t3_abc123"]
    assert figures["score"] == 42 and figures["upvote_ratio"] == 0.93 and figures["comments"] == 7
    for key in POST_METRIC_FIELDS:
        if key != "comments":
            assert figures[key] is None, key
    assert out["zzz"]["score"] is None and out["zzz"]["upvote_ratio"] is None and out["zzz"]["comments"] is None


def test_no_ids_means_no_call():
    connector = _connector()
    recorder = install(connector, [])
    assert connector.read_post_metrics([]) == {}
    assert recorder.calls == []


# ── Checks and logs ────────────────────────────────────────────────────────
def test_the_live_check_only_reads():
    connector = _connector()
    install(connector, [("GET", r"/api/v1/me$", {"name": "brand_account"}), *_post_routes()[:2]])
    steps = connector.verify_steps()
    assert {s.kind for s in steps} == {"read"}
    results = {s.key: s.run() for s in steps}
    assert "u/brand_account" in results["account"][0]
    assert "r/marketing" in results["subreddit"][0]


def test_no_token_is_logged_on_a_refused_post(caplog):  # noqa: ANN001
    connector = _connector()
    install(
        connector,
        [*_post_routes()[:2], ("POST", r"/api/submit", (403, f"forbidden for {ACCESS} {REFRESH}"))],
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConnectorError) as caught:
        connector.publish_post(text="Body", title="A good title")
    for secret in (ACCESS, REFRESH, CLIENT_SECRET):
        assert secret not in caplog.text
        assert secret not in str(caught.value)
