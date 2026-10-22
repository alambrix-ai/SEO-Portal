"""The platform sign-in flows: "Sign in with Google" and WordPress approval.

Pure tests — no database. The state store, the actor lookup and the token
exchange are replaced at their seams, so what is exercised is the logic that
decides whether a sign-in is trusted, what is asked of the vendor, what is
stored, and where the browser is sent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import oauth_providers, registry
from app.connectors.base.credentials import (
    ACCESS_TOKEN_EXPIRY_KEY,
    OAUTH_SOURCE_KEY,
    PLATFORM_SOURCE,
    Credentials,
    validate,
)
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, InvalidInputError
from app.services import oauth_flow
from app.services.oauth_flow import OAuthFlowError, StatePayload

CONSOLE = "https://console.example.com"


@pytest.fixture(autouse=True)
def platform(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(settings, "public_base_url", CONSOLE)
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.com")
    monkeypatch.setattr(settings, "oauth_google_client_id", "platform-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "oauth_google_client_secret", "platform-secret")
    monkeypatch.setattr(settings, "google_ads_developer_token", "platform-dev-token")


def _payload(**over) -> StatePayload:  # noqa: ANN003
    base = dict(
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        slug="google_analytics_4",
        provider="google",
        nonce="nonce-abc",
        return_path="/connectors",
        expires_at=int(time.time()) + 600,
    )
    base.update(over)
    return StatePayload(**base)


def _mock_client(monkeypatch, target: str, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []
    real = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs.pop("timeout", None)
        return real(transport=httpx.MockTransport(recording), **kwargs)

    monkeypatch.setattr(target, factory)
    return seen


# ── State: signed, expiring, single-use ────────────────────────────────────
def test_a_state_round_trips():
    payload = _payload()
    assert oauth_flow.verify_state(oauth_flow.sign_state(payload)) == payload


def test_a_tampered_state_is_refused():
    token = oauth_flow.sign_state(_payload())
    body, sig = token.split(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    data["t"] = "33333333-3333-3333-3333-333333333333"  # someone else's workspace
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    with pytest.raises(OAuthFlowError):
        oauth_flow.verify_state(f"{forged}.{sig}")
    with pytest.raises(OAuthFlowError):
        oauth_flow.verify_state(f"{body}.{sig[:-2]}xx")
    with pytest.raises(OAuthFlowError):
        oauth_flow.verify_state("not-a-state")


def test_a_state_signed_with_another_key_is_refused(monkeypatch):  # noqa: ANN001
    token = oauth_flow.sign_state(_payload())
    monkeypatch.setattr(settings, "secret_key", "a-different-secret-key-of-enough-length!!")
    with pytest.raises(OAuthFlowError):
        oauth_flow.verify_state(token)


def test_an_expired_state_is_refused():
    token = oauth_flow.sign_state(_payload(expires_at=int(time.time()) - 1))
    with pytest.raises(OAuthFlowError, match="took too long"):
        oauth_flow.verify_state(token)


def test_the_row_key_is_a_hash_not_the_nonce():
    assert oauth_flow.nonce_key("abc") == hashlib.sha256(b"abc").hexdigest()


@pytest.mark.parametrize(
    "raw",
    [
        "//evil.example/x",
        "https://evil.example/",
        "/\\evil.example",
        "javascript:alert(1)",
        "connectors",
        "",
        "/ok\n/x",
    ],
)
def test_the_return_path_cannot_leave_the_console(raw: str):
    assert oauth_flow.safe_return_path(raw) == "/connectors"
    assert oauth_flow.console_url(raw, connected="x").startswith(CONSOLE + "/")


def test_the_console_url_keeps_the_page_and_replaces_old_results():
    url = oauth_flow.console_url("/agents/seo?tab=setup&oauth_error=old", connected="search_console")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == CONSOLE
    assert parts.path == "/agents/seo"
    query = parse_qs(parts.query)
    assert query == {"tab": ["setup"], "connected": ["search_console"]}


# ── The authorize URL ──────────────────────────────────────────────────────
def test_the_google_authorize_url_asks_for_offline_access_with_pkce():
    spec = registry.get_spec("google_analytics_4")
    verifier, challenge = oauth_flow.pkce_pair()
    url = oauth_flow.build_authorize_url(
        oauth_providers.GOOGLE, spec, state="S", code_challenge=challenge, login_hint="a@b.com"
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q["client_id"] == "platform-id.apps.googleusercontent.com"
    assert q["redirect_uri"] == "https://api.example.com/api/v1/oauth/callback/google"
    assert q["response_type"] == "code"
    assert q["scope"].split() == [
        "openid",
        "email",
        "https://www.googleapis.com/auth/analytics.readonly",
    ]
    assert q["access_type"] == "offline"
    assert q["prompt"] == "consent"
    assert q["include_granted_scopes"] == "true"
    assert q["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == expected.decode()
    assert q["state"] == "S"
    assert q["login_hint"] == "a@b.com"
    assert "client_secret" not in q


@pytest.mark.parametrize(
    ("slug", "scope"),
    [
        ("google_analytics_4", "https://www.googleapis.com/auth/analytics.readonly"),
        ("search_console", "https://www.googleapis.com/auth/webmasters.readonly"),
        ("google_ads", "https://www.googleapis.com/auth/adwords"),
        ("google_business_profile", "https://www.googleapis.com/auth/business.manage"),
    ],
)
def test_each_google_connector_asks_only_for_its_own_scope(slug: str, scope: str):
    spec = registry.get_spec(slug)
    assert spec.oauth.provider == "google"
    assert spec.oauth.scopes == (scope,)
    assert oauth_providers.scopes_for(spec) == ("openid", "email", scope)


def test_a_provider_without_credentials_is_not_offered(monkeypatch):  # noqa: ANN001
    spec = registry.get_spec("search_console")
    assert oauth_providers.sign_in_available(spec)
    monkeypatch.setattr(settings, "oauth_google_client_secret", "")
    assert not oauth_providers.sign_in_available(spec)
    assert oauth_providers.missing_settings(spec) == [
        "OAUTH_GOOGLE_CLIENT_ID",
        "OAUTH_GOOGLE_CLIENT_SECRET",
    ]


def test_google_ads_sign_in_also_needs_the_platform_developer_token(monkeypatch):  # noqa: ANN001
    spec = registry.get_spec("google_ads")
    assert oauth_providers.sign_in_available(spec)
    monkeypatch.setattr(settings, "google_ads_developer_token", "")
    assert not oauth_providers.sign_in_available(spec)
    assert oauth_providers.missing_settings(spec) == ["GOOGLE_ADS_DEVELOPER_TOKEN"]
    # And no other Google connector depends on it.
    assert oauth_providers.sign_in_available(registry.get_spec("search_console"))


def test_the_redirect_uri_falls_back_to_the_console_address(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "public_api_base_url", "")
    assert oauth_providers.GOOGLE.redirect_uri == f"{CONSOLE}/api/v1/oauth/callback/google"


# ── The token exchange ─────────────────────────────────────────────────────
def _id_token(email: str) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"e30.{claims}.sig"


def test_the_code_is_exchanged_with_the_verifier(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.new",
                "refresh_token": "1//refresh",
                "expires_in": 3599,
                "scope": "openid https://www.googleapis.com/auth/analytics.readonly",
                "id_token": _id_token("owner@example.com"),
            },
        )

    seen = _mock_client(monkeypatch, "app.services.oauth_flow.httpx.Client", handler)
    tokens = oauth_flow.exchange_code(oauth_providers.GOOGLE, code="4/code", verifier="v" * 43, now=1000.0)

    sent = dict(httpx.QueryParams(seen[0].content.decode()))
    assert str(seen[0].url) == "https://oauth2.googleapis.com/token"
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "4/code"
    assert sent["code_verifier"] == "v" * 43
    assert sent["client_id"] == "platform-id.apps.googleusercontent.com"
    assert sent["client_secret"] == "platform-secret"
    assert sent["redirect_uri"] == "https://api.example.com/api/v1/oauth/callback/google"
    assert tokens.access_token == "ya29.new"
    assert tokens.refresh_token == "1//refresh"
    assert tokens.expires_at == 1000.0 + 3599
    assert tokens.account == "owner@example.com"
    assert "https://www.googleapis.com/auth/analytics.readonly" in tokens.scopes


def test_a_refused_exchange_is_a_plain_sentence(monkeypatch, caplog):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad"})

    _mock_client(monkeypatch, "app.services.oauth_flow.httpx.Client", handler)
    with pytest.raises(OAuthFlowError) as caught:
        oauth_flow.exchange_code(oauth_providers.GOOGLE, code="4/secret-code", verifier="v")
    assert "did not accept" in str(caught.value)
    assert "4/secret-code" not in caplog.text


def test_an_unticked_permission_is_named():
    spec = registry.get_spec("google_business_profile")
    assert oauth_flow.missing_scopes(spec, ("openid", "email")) == [
        "https://www.googleapis.com/auth/business.manage"
    ]
    assert oauth_flow.missing_scopes(spec, ("https://www.googleapis.com/auth/business.manage",)) == []


# ── The callback ───────────────────────────────────────────────────────────
class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeStore:
    """The server-side half of the state: redeemable once."""

    def __init__(self, context: dict | None = None) -> None:
        self.rows: dict[str, oauth_flow.RedeemedState] = {}
        self.context = context or {}

    def put(self, payload: StatePayload) -> None:
        self.rows[oauth_flow.nonce_key(payload.nonce)] = oauth_flow.RedeemedState(
            verifier_encrypted="", context=dict(self.context)
        )

    def redeem(self, db, payload):  # noqa: ANN001, ANN201
        return self.rows.pop(oauth_flow.nonce_key(payload.nonce), None)


@pytest.fixture
def seams(monkeypatch):  # noqa: ANN001, ANN201
    """Replace the database-touching seams of the callback."""
    store = FakeStore()
    calls: dict = {"written": [], "auto": []}
    monkeypatch.setattr("app.db.session.bind_tenant", lambda db, tenant: None)
    monkeypatch.setattr(oauth_flow, "_redeem_state", store.redeem)
    class Org:
        id = "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr(oauth_flow, "_load_actor", lambda db, payload: (Org(), "USER"))
    monkeypatch.setattr(
        "app.services.connectors.get_record", lambda db, tenant_id, slug: f"record:{slug}"
    )

    def write(db, *, org, record, spec, tokens):  # noqa: ANN001, ANN202
        calls["written"].append((record, spec.slug, tokens))

    monkeypatch.setattr(oauth_flow, "write_platform_tokens", write)

    def auto(db, *, org, user, record, spec):  # noqa: ANN001, ANN202
        calls["auto"].append(record)
        return calls.get("auto_result", "connected")

    monkeypatch.setattr(oauth_flow, "_auto_choose", auto)
    monkeypatch.setattr(
        oauth_flow,
        "exchange_code",
        lambda provider, *, code, verifier: oauth_flow.TokenSet(
            access_token="ya29.x",
            refresh_token="1//r",
            expires_at=time.time() + 3600,
            scopes=("openid", "email", "https://www.googleapis.com/auth/analytics.readonly"),
            account="owner@example.com",
        ),
    )
    return store, calls


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_a_successful_sign_in_stores_tokens_and_returns_to_the_console(seams):  # noqa: ANN001
    store, calls = seams
    payload = _payload(return_path="/connectors?view=catalog")
    store.put(payload)
    db = FakeDb()

    url = oauth_flow.handle_callback(db, "google", {"state": oauth_flow.sign_state(payload), "code": "4/c"})

    assert url.startswith(CONSOLE + "/connectors?")
    assert _query(url) == {"view": "catalog", "connected": "google_analytics_4"}
    record, slug, tokens = calls["written"][0]
    assert (record, slug, tokens.refresh_token) == ("record:google_analytics_4", "google_analytics_4", "1//r")
    assert db.commits == 2  # the redemption, then the tokens


def test_several_choices_send_the_person_to_the_picker(seams):  # noqa: ANN001
    store, calls = seams
    calls["auto_result"] = "choose"
    payload = _payload()
    store.put(payload)
    url = oauth_flow.handle_callback(FakeDb(), "google", {"state": oauth_flow.sign_state(payload), "code": "c"})
    assert _query(url) == {"oauth_choose": "google_analytics_4"}


def test_a_replayed_state_is_refused(seams):  # noqa: ANN001
    store, calls = seams
    payload = _payload()
    store.put(payload)
    state = oauth_flow.sign_state(payload)
    first = oauth_flow.handle_callback(FakeDb(), "google", {"state": state, "code": "c"})
    second = oauth_flow.handle_callback(FakeDb(), "google", {"state": state, "code": "c"})
    assert "connected" in _query(first)
    assert _query(second)["oauth_error"] == "link_expired"
    assert len(calls["written"]) == 1


def test_cancelling_consent_is_explained(seams):  # noqa: ANN001
    store, calls = seams
    payload = _payload()
    store.put(payload)
    url = oauth_flow.handle_callback(
        FakeDb(), "google", {"state": oauth_flow.sign_state(payload), "error": "access_denied"}
    )
    query = _query(url)
    assert query["oauth_error"] == "cancelled"
    assert query["oauth_slug"] == "google_analytics_4"
    assert calls["written"] == []


def test_a_forged_state_never_reaches_the_store(seams):  # noqa: ANN001
    store, calls = seams
    url = oauth_flow.handle_callback(FakeDb(), "google", {"state": "abc.def", "code": "c"})
    assert _query(url)["oauth_error"] == "link_expired"
    assert url.startswith(CONSOLE + "/connectors?")
    assert calls["written"] == []


def test_a_state_for_one_provider_cannot_be_used_on_another(seams):  # noqa: ANN001
    store, calls = seams
    payload = _payload()
    store.put(payload)
    url = oauth_flow.handle_callback(
        FakeDb(), "wordpress", {"state": oauth_flow.sign_state(payload), "code": "c"}
    )
    assert _query(url)["oauth_error"] == "link_invalid"
    assert calls["written"] == []


def test_an_unticked_scope_is_reported_not_stored(seams, monkeypatch):  # noqa: ANN001
    store, calls = seams
    monkeypatch.setattr(
        oauth_flow,
        "exchange_code",
        lambda provider, *, code, verifier: oauth_flow.TokenSet(
            access_token="a", refresh_token="r", scopes=("openid", "email")
        ),
    )
    payload = _payload()
    store.put(payload)
    url = oauth_flow.handle_callback(FakeDb(), "google", {"state": oauth_flow.sign_state(payload), "code": "c"})
    assert _query(url)["oauth_error"] == "permissions_missing"
    assert calls["written"] == []


def _facebook_callback(seams, monkeypatch, granted: tuple[str, ...]) -> tuple[str, dict]:  # noqa: ANN001
    store, calls = seams
    monkeypatch.setattr(settings, "oauth_meta_client_id", "meta-app")
    monkeypatch.setattr(settings, "oauth_meta_client_secret", "meta-secret")
    monkeypatch.setattr(
        oauth_flow,
        "exchange_code",
        lambda provider, *, code, verifier: oauth_flow.TokenSet(
            access_token="EAAG", expires_at=time.time() + 3600, scopes=granted
        ),
    )
    payload = _payload(slug="facebook_pages", provider="meta")
    store.put(payload)
    url = oauth_flow.handle_callback(FakeDb(), "meta", {"state": oauth_flow.sign_state(payload), "code": "c"})
    return url, calls


def test_a_grant_without_only_the_insights_permission_still_connects(seams, monkeypatch):  # noqa: ANN001
    url, calls = _facebook_callback(
        seams, monkeypatch, ("pages_show_list", "pages_manage_posts", "pages_read_engagement")
    )
    assert "oauth_error" not in _query(url)
    assert [(record, slug) for record, slug, _ in calls["written"]] == [("record:facebook_pages", "facebook_pages")]


def test_a_grant_without_a_posting_permission_is_still_refused(seams, monkeypatch):  # noqa: ANN001
    url, calls = _facebook_callback(
        seams, monkeypatch, ("pages_show_list", "pages_read_engagement", "read_insights")
    )
    assert _query(url)["oauth_error"] == "permissions_missing"
    assert calls["written"] == []


def test_an_unexpected_failure_is_a_sentence_not_a_trace(seams, monkeypatch):  # noqa: ANN001
    store, _ = seams

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("Traceback: secret internals ya29.token")

    monkeypatch.setattr(oauth_flow, "write_platform_tokens", boom)
    payload = _payload()
    store.put(payload)
    db = FakeDb()
    url = oauth_flow.handle_callback(db, "google", {"state": oauth_flow.sign_state(payload), "code": "c"})
    assert _query(url)["oauth_error"] == "connect_failed"
    assert "secret" not in url and "Traceback" not in url
    assert db.rollbacks == 1


def test_a_vendor_denial_with_other_text_is_a_fixed_code(seams):  # noqa: ANN001
    store, calls = seams
    payload = _payload()
    store.put(payload)
    url = oauth_flow.handle_callback(
        FakeDb(),
        "google",
        {"state": oauth_flow.sign_state(payload), "error": "<script>alert(1)</script>"},
    )
    assert _query(url)["oauth_error"] == "provider_refused"
    assert "script" not in url
    assert calls["written"] == []


def test_html_in_an_app_error_message_becomes_connect_failed(seams, monkeypatch):  # noqa: ANN001
    from app.core.exceptions import ConnectorError

    store, _ = seams

    def refuse(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise ConnectorError('<img src=x onerror="alert(1)"> bad key sk-live-123')

    monkeypatch.setattr(oauth_flow, "write_platform_tokens", refuse)
    payload = _payload()
    store.put(payload)
    db = FakeDb()
    url = oauth_flow.handle_callback(db, "google", {"state": oauth_flow.sign_state(payload), "code": "c"})
    assert _query(url) == {"oauth_error": "connect_failed", "oauth_slug": "google_analytics_4"}
    assert "img" not in url and "sk-live" not in url
    assert db.rollbacks == 1


def test_every_redirect_error_code_is_in_the_fixed_set():
    assert oauth_flow.error_code("link_expired") == "link_expired"
    assert oauth_flow.error_code("<b>nope</b>") == "connect_failed"
    assert OAuthFlowError("text", code="made_up").code == "connect_failed"
    assert OAuthFlowError("text").code == "connect_failed"
    assert all(c.isascii() and c.replace("_", "").isalpha() for c in oauth_flow.OAUTH_ERROR_CODES)


# ── WordPress: application-password approval ──────────────────────────────
def test_a_plain_http_site_is_explained():
    with pytest.raises(InvalidInputError, match="https://"):
        oauth_flow.normalise_site("http://example.com")
    assert oauth_flow.normalise_site("Example.com/") == "https://example.com"
    assert oauth_flow.normalise_site("https://example.com/blog/") == "https://example.com/blog"


def test_the_authorization_page_is_read_from_the_rest_index(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr("app.core.netguard.check_public_url", lambda url: url)

    def fake_get(url, **kw):  # noqa: ANN001, ANN003, ANN202
        assert url == "https://example.com/wp-json/"
        return httpx.Response(
            200,
            json={
                "namespaces": ["wp/v2"],
                "authentication": {
                    "application-passwords": {
                        "endpoints": {
                            "authorization": "https://example.com/wp/wp-admin/authorize-application.php"
                        }
                    }
                },
            },
        )

    monkeypatch.setattr("app.core.netguard.safe_get", fake_get)
    assert (
        oauth_flow.discover_wordpress_authorize("https://example.com")
        == "https://example.com/wp/wp-admin/authorize-application.php"
    )


def test_a_site_with_application_passwords_off_says_so(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr("app.core.netguard.check_public_url", lambda url: url)
    monkeypatch.setattr(
        "app.core.netguard.safe_get",
        lambda url, **kw: httpx.Response(200, json={"namespaces": ["wp/v2"], "authentication": []}),
    )
    with pytest.raises(InvalidInputError, match="switched off"):
        oauth_flow.discover_wordpress_authorize("https://example.com")


def test_an_endpoint_on_another_host_is_not_followed(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr("app.core.netguard.check_public_url", lambda url: url)
    monkeypatch.setattr(
        "app.core.netguard.safe_get",
        lambda url, **kw: httpx.Response(
            200,
            json={
                "namespaces": [],
                "authentication": {
                    "application-passwords": {"endpoints": {"authorization": "https://evil.example/a.php"}}
                },
            },
        ),
    )
    assert (
        oauth_flow.discover_wordpress_authorize("https://example.com")
        == "https://example.com/wp-admin/authorize-application.php"
    )


def test_a_private_site_address_is_refused(monkeypatch):  # noqa: ANN001
    with pytest.raises(InvalidInputError):
        oauth_flow.discover_wordpress_authorize("https://127.0.0.1")


@pytest.fixture
def wp_seams(seams, monkeypatch):  # noqa: ANN001, ANN201
    store, calls = seams
    store.context = {"site": "https://example.com"}
    connected: list[dict] = []
    monkeypatch.setattr("app.core.netguard.check_public_url", lambda url: url)

    def connect(db, *, org, actor, slug, values, **kw):  # noqa: ANN001, ANN003, ANN202
        connected.append({"slug": slug, **values})

    monkeypatch.setattr("app.services.connectors.connect", connect)
    return store, connected


def _wp_state(store: FakeStore) -> str:
    payload = _payload(slug="wordpress", provider="wordpress")
    store.put(payload)
    return oauth_flow.sign_state(payload)


def test_wordpress_approval_stores_the_application_password(wp_seams):  # noqa: ANN001
    store, connected = wp_seams
    url = oauth_flow.handle_callback(
        FakeDb(),
        "wordpress",
        {
            "state": _wp_state(store),
            "site_url": "https://www.example.com",
            "user_login": "editor",
            "password": "abcd EFGH ijkl MNOP qrst UVWX",
        },
    )
    assert _query(url) == {"connected": "wordpress"}
    assert connected == [
        {
            "slug": "wordpress",
            "siteUrl": "https://www.example.com",
            "username": "editor",
            "applicationPassword": "abcd EFGH ijkl MNOP qrst UVWX",
        }
    ]


def test_wordpress_rejection_is_explained(wp_seams):  # noqa: ANN001
    store, connected = wp_seams
    url = oauth_flow.handle_callback(FakeDb(), "wordpress", {"state": _wp_state(store), "rejected": "1"})
    assert _query(url)["oauth_error"] == "cancelled"
    assert connected == []


def test_wordpress_approval_from_another_site_is_refused(wp_seams):  # noqa: ANN001
    store, connected = wp_seams
    url = oauth_flow.handle_callback(
        FakeDb(),
        "wordpress",
        {
            "state": _wp_state(store),
            "site_url": "https://attacker.example",
            "user_login": "x",
            "password": "y",
        },
    )
    assert _query(url)["oauth_error"] == "site_mismatch"
    assert connected == []


def test_wordpress_approval_without_a_state_is_refused(wp_seams):  # noqa: ANN001
    _, connected = wp_seams
    url = oauth_flow.handle_callback(
        FakeDb(), "wordpress", {"site_url": "https://example.com", "user_login": "x", "password": "y"}
    )
    assert _query(url)["oauth_error"] == "link_expired"
    assert connected == []


# ── Choices ────────────────────────────────────────────────────────────────
def test_ga4_properties_are_listed_from_account_summaries():
    from app.connectors.google_analytics_4.connector import parse_account_summaries

    choices = parse_account_summaries(
        {
            "accountSummaries": [
                {
                    "displayName": "Acme",
                    "propertySummaries": [
                        {"property": "properties/123", "displayName": "acme.com"},
                        {"property": "properties/456", "displayName": "shop.acme.com"},
                    ],
                },
                {"displayName": "Empty"},
            ]
        }
    )
    assert [(c.id, c.label, c.values) for c in choices] == [
        ("properties/123", "acme.com", {"propertyId": "123"}),
        ("properties/456", "shop.acme.com", {"propertyId": "456"}),
    ]
    assert "Acme" in choices[0].detail


def test_search_console_lists_only_verified_sites():
    from app.connectors.search_console.connector import parse_sites

    choices = parse_sites(
        {
            "siteEntry": [
                {"siteUrl": "sc-domain:acme.com", "permissionLevel": "siteOwner"},
                {"siteUrl": "https://blog.acme.com/", "permissionLevel": "siteFullUser"},
                {"siteUrl": "https://other.com/", "permissionLevel": "siteUnverifiedUser"},
            ]
        }
    )
    assert [c.values["siteUrl"] for c in choices] == ["sc-domain:acme.com", "https://blog.acme.com/"]
    assert choices[0].label == "acme.com"
    assert "Domain property" in choices[0].detail


def test_business_profile_locations_carry_their_account():
    from app.connectors.google_business_profile.connector import parse_accounts, parse_locations

    accounts = parse_accounts({"accounts": [{"name": "accounts/9", "accountName": "Acme Group"}]})
    assert accounts == [("accounts/9", "Acme Group")]
    choices = parse_locations(
        {
            "locations": [
                {
                    "name": "locations/77",
                    "title": "Acme Noida",
                    "storefrontAddress": {"addressLines": ["Sector 18"], "locality": "Noida"},
                }
            ]
        },
        "accounts/9",
        "Acme Group",
    )
    assert choices[0].values == {"locationId": "locations/77", "accountId": "accounts/9"}
    assert choices[0].detail == "Sector 18, Noida · Acme Group"


def test_google_ads_offers_clients_of_a_manager_and_not_the_manager():
    from app.connectors.google_ads.connector import parse_customer_clients

    manager_rows = [
        {"customerClient": {"id": "1111111111", "descriptiveName": "Agency MCC", "manager": True, "level": 0}},
        {"customerClient": {"id": "2222222222", "descriptiveName": "Client A", "manager": False, "level": 1, "status": "ENABLED"}},
        {"customerClient": {"id": "3333333333", "descriptiveName": "Closed", "manager": False, "level": 1, "status": "CANCELED"}},
        {"customerClient": {"id": "4444444444", "descriptiveName": "Own", "manager": False, "level": 1, "status": "ENABLED"}},
    ]
    direct_rows = [
        {"customerClient": {"id": "4444444444", "descriptiveName": "Own", "manager": False, "level": 0, "status": "ENABLED"}},
    ]
    choices = parse_customer_clients({"1111111111": manager_rows, "4444444444": direct_rows})
    by_customer = {c.values["customerId"]: c for c in choices}
    assert set(by_customer) == {"2222222222", "4444444444"}
    assert by_customer["2222222222"].values["loginCustomerId"] == "1111111111"
    assert "Agency MCC" in by_customer["2222222222"].detail
    # Reached directly as well as through the manager: the direct route wins.
    assert by_customer["4444444444"].values["loginCustomerId"] == ""
    assert by_customer["2222222222"].label == "Client A (222-222-2222)"


def _signed_in(slug: str, **values: str):  # noqa: ANN202
    return registry.get_class(slug)(
        Credentials(
            values={
                OAUTH_SOURCE_KEY: PLATFORM_SOURCE,
                "refreshToken": "1//r",
                "accessToken": "ya29.stored",
                ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3000),
                **values,
            }
        ),
        org_id="o",
    )


def test_google_ads_choices_use_the_platform_developer_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1111111111"]})
        return httpx.Response(
            200,
            json=[
                {
                    "results": [
                        {"customerClient": {"id": "1111111111", "descriptiveName": "Solo", "manager": False, "level": 0}}
                    ]
                }
            ],
        )

    connector = _signed_in("google_ads")
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    choices = connector.list_oauth_choices()

    assert [c.values for c in choices] == [{"customerId": "1111111111", "loginCustomerId": ""}]
    assert all(r.headers["developer-token"] == "platform-dev-token" for r in seen)
    assert all(r.headers["authorization"] == "Bearer ya29.stored" for r in seen)
    assert seen[1].headers["login-customer-id"] == "1111111111"


def test_ga4_choices_are_paged_through_the_admin_api():
    pages = iter(
        [
            {"accountSummaries": [{"displayName": "A", "propertySummaries": [{"property": "properties/1"}]}], "nextPageToken": "p2"},
            {"accountSummaries": [{"displayName": "B", "propertySummaries": [{"property": "properties/2"}]}]},
        ]
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=next(pages))

    connector = _signed_in("google_analytics_4")
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    assert [c.values["propertyId"] for c in connector.list_oauth_choices()] == ["1", "2"]
    assert seen[0].url.host == "analyticsadmin.googleapis.com"
    assert seen[1].url.params["pageToken"] == "p2"


# ── Refreshing with the platform's client ──────────────────────────────────
def test_a_signed_in_connection_refreshes_with_the_platform_client(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600})

    seen = _mock_client(monkeypatch, "app.connectors.base.oauth.httpx.Client", handler)
    connector = registry.get_class("search_console")(
        Credentials(values={OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "1//r", "siteUrl": "x"}),
        org_id="o",
    )
    assert connector.access_token == "ya29.fresh"
    sent = dict(httpx.QueryParams(seen[0].content.decode()))
    assert sent["client_id"] == "platform-id.apps.googleusercontent.com"
    assert sent["client_secret"] == "platform-secret"
    assert sent["refresh_token"] == "1//r"


def test_a_fresh_stored_token_is_used_without_a_refresh(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch, "app.connectors.base.oauth.httpx.Client", lambda r: httpx.Response(500)
    )
    connector = _signed_in("google_analytics_4", propertyId="1")
    assert connector.access_token == "ya29.stored"
    assert seen == []


def test_an_expired_stored_token_is_refreshed(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "ya29.next", "expires_in": 3600}),
    )
    connector = _signed_in("google_analytics_4", **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) - 10)})
    assert connector.access_token == "ya29.next"
    assert len(seen) == 1


def test_a_signed_in_connection_without_the_platform_app_says_so(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "oauth_google_client_id", "")
    connector = registry.get_class("search_console")(
        Credentials(values={OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "1//r"}), org_id="o"
    )
    with pytest.raises(ConnectorConfigError, match="sign-in app"):
        _ = connector.access_token


def test_a_pasted_refresh_token_still_uses_its_own_client(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "ya29.own", "expires_in": 3600}),
    )
    connector = registry.get_class("search_console")(
        Credentials(values={"refreshToken": "1//r", "clientId": "own-id", "clientSecret": "own-secret"}),
        org_id="o",
    )
    assert connector.access_token == "ya29.own"
    assert dict(httpx.QueryParams(seen[0].content.decode()))["client_id"] == "own-id"


def test_google_ads_uses_the_platform_developer_token_only_when_signed_in():
    signed_in = _signed_in("google_ads", customerId="1")
    assert signed_in.auth_headers()["developer-token"] == "platform-dev-token"
    pasted = registry.get_class("google_ads")(
        Credentials(values={"customerId": "1", "accessToken": "t"}), org_id="o"
    )
    assert "developer-token" not in pasted.auth_headers()


# ── Validation treats platform-supplied fields as present ──────────────────
def test_a_signed_in_connection_needs_no_client_id_or_developer_token():
    spec = registry.get_spec("google_ads")
    values = {"customerId": "1", "refreshToken": "r", "apiVersion": "v22"}
    with pytest.raises(InvalidInputError):
        validate(spec.fields, values, oauth_done=True)
    validate(spec.fields, values, oauth_done=True, platform_supplied=spec.oauth.platform_fields)


def test_a_signed_in_record_is_not_reported_as_missing_its_client():
    from app.services.connectors import missing_credentials

    class Record:
        slug = "search_console"

    signed = Credentials(values={OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "r", "siteUrl": "x"})
    pasted = Credentials(values={"refreshToken": "r", "siteUrl": "x"})
    assert missing_credentials(Record(), signed) == []
    assert missing_credentials(Record(), pasted) == ["clientId", "clientSecret"]


# ── What the connect screen is told ────────────────────────────────────────
def _record(slug: str, **kw):  # noqa: ANN003, ANN202
    from app.models.connector import ConnectorRecord

    return ConnectorRecord(slug=slug, name=slug, category="x", credential_hints=kw.pop("hints", {}), **kw)


def test_the_connect_screen_modes():
    from app.services.views import connect_guide

    assert connect_guide(_record("google_analytics_4", connected=False)).mode == "sign_in"
    assert connect_guide(_record("wordpress", connected=False)).mode == "wordpress"
    assert connect_guide(_record("openai", connected=False)).mode == "key"
    assert connect_guide(_record("site_crawler", connected=False)).mode == "form"
    for slug in ("custom_api", "zapier_webhooks", "dsp_exchange", "magento", "smtp_email"):
        assert connect_guide(_record(slug, connected=False)).mode == "advanced", slug


def test_the_sign_in_path_hides_every_technical_field():
    from app.services.views import connect_guide

    guide = connect_guide(_record("google_ads", connected=False))
    assert guide.sign_in_available and guide.button_label == "Sign in with Google"
    for key in ("refreshToken", "clientId", "clientSecret", "accessToken", "developerToken", "customerId"):
        assert key in guide.sign_in_hidden_fields
    assert "monthlyBudget" not in guide.sign_in_hidden_fields


def test_the_setup_note_is_for_admins_only(monkeypatch):  # noqa: ANN001
    from app.services.views import ConnectContext, connect_guide

    monkeypatch.setattr(settings, "oauth_google_client_id", "")
    record = _record("search_console", connected=False)
    member = connect_guide(record)
    assert member.setup_pending and not member.sign_in_available
    assert member.admin_hint == "" and member.admin_setup_url == "" and member.developer_label == ""
    admin = connect_guide(record, ConnectContext(show_admin_notes=True))
    assert "platform administrator" in admin.admin_hint and admin.admin_setup_url == ""
    # No environment key names on the console, even for admins.
    assert "OAUTH_" not in admin.admin_hint + admin.admin_note + admin.setup_message


def test_a_signed_in_record_awaiting_a_choice_is_flagged():
    from app.services.views import ConnectContext, connect_guide

    hints = {OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "oauthAccount": "a@b.com"}
    pending = connect_guide(_record("google_analytics_4", connected=False, hints=hints))
    assert pending.pending_choice and pending.signed_in_account == "a@b.com"

    ctx = ConnectContext(not_connected={"google": [("search_console", "Search Console")]})
    done = connect_guide(_record("google_analytics_4", connected=True, hints=hints), ctx)
    assert not done.pending_choice
    assert [link.slug for link in done.also_connect] == ["search_console"]


# ── Guides for key-only connectors ─────────────────────────────────────────
KEY_ONLY = {
    "openai": "sk-" + "proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "anthropic_claude": "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz",
    "google_gemini": "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
    "perplexity": "pplx-" + "abcdefghijklmnopqrstuvwxyz012345",
    "brevo": "xkeysib-" + "0123456789abcdef0123456789abcdef-AbCdEfGh12345678",
    "dataforseo_backlinks": None,
    "pagespeed_insights": "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
    "bing_webmaster": None,
    "google_places": "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
    "mailchimp": "0123456789abcdef" + "0123456789abcdef-us21",
    "klaviyo": "pk_" + "abcdefghijklmnopqrstuvwxyz012345",
}


@pytest.mark.parametrize("slug", sorted(KEY_ONLY))
def test_every_key_only_connector_has_a_guide(slug: str):
    spec = registry.get_spec(slug)
    guide = spec.guide
    assert guide is not None, slug
    # A connector may also offer a sign-in (Bing Webmaster Tools, phase 2A);
    # its key guide is then the fallback until the platform's app is set up
    # (tests/test_oauth_social_ads.py).
    assert len(guide.steps) == 3
    assert guide.key_url.startswith("https://")
    assert guide.key_url_label
    assert guide.key_field in {f.key for f in spec.fields}
    assert guide.key_hint
    example = KEY_ONLY[slug]
    if guide.key_pattern:
        assert re.fullmatch(guide.key_pattern, example), slug
        assert not re.fullmatch(guide.key_pattern, "not a key"), slug


def test_no_default_path_asks_for_developer_credentials():
    """The words a marketer should never meet before choosing Advanced.

    Covers every connector with a sign-in or a key guide. Connectors still
    on a plain form (Microsoft Ads, Salesforce, the social platforms) are the
    next phase's, and are added here as they move over.
    """
    from app.services.views import connect_guide

    banned = re.compile(r"client id|client secret|refresh token|developer token|redirect uri", re.I)
    for spec in registry.all_specs():
        guide = connect_guide(_record(spec.slug, connected=False))
        if guide.mode == "sign_in":
            shown = [f for f in spec.fields if f.key not in guide.sign_in_hidden_fields]
        elif guide.mode == "wordpress":
            shown = [f for f in spec.fields if f.key == "siteUrl"]
        elif guide.mode == "key" or spec.slug == "site_crawler":
            shown = [f for f in spec.fields if f.required and not f.default]
        else:
            continue
        for f in shown:
            assert not banned.search(f.label), (spec.slug, f.label)
        assert not banned.search(" ".join(guide.steps)), spec.slug
        # Nor in what is said while a sign-in is not switched on yet (see
        # tests/test_platform_oauth_apps.py for the stricter word list).
        assert not banned.search(guide.setup_message + guide.admin_hint), spec.slug


def test_the_site_crawler_needs_only_the_address():
    spec = registry.get_spec("site_crawler")
    essential = [f.key for f in spec.fields if f.required and not f.default]
    assert essential == ["siteUrl"]


# ── Logs ───────────────────────────────────────────────────────────────────
def test_callback_query_strings_are_kept_out_of_the_access_log():
    from app.core.logging import _RedactCallbackQuery

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/api/v1/oauth/callback/wordpress?state=s&password=abcd efgh", "1.1", 303),
        None,
    )
    _RedactCallbackQuery().filter(record)
    line = record.getMessage()
    assert "abcd" not in line and "state=" not in line
    assert "/api/v1/oauth/callback/wordpress?[redacted]" in line


def test_admin_overview_shows_the_exact_redirect_uri():
    statuses = {s.name: s for s in oauth_flow.provider_statuses()}
    google = statuses["google"]
    assert google.configured
    assert google.redirect_uri == "https://api.example.com/api/v1/oauth/callback/google"
    assert {c["slug"] for c in google.connectors} == {
        "google_analytics_4",
        "search_console",
        "google_ads",
        "google_business_profile",
    }
    from dataclasses import asdict

    assert "platform-secret" not in json.dumps([asdict(s) for s in statuses.values()])


# ── Against the real schema (runs when TEST_DATABASE_URL is set) ───────────
from tests.conftest import requires_db  # noqa: E402


@requires_db
def test_the_stored_state_redeems_exactly_once(org, clean_db):  # noqa: ANN001
    from app.models.workspace import User

    user = clean_db.query(User).filter_by(tenant_id=org.id).first()
    url = oauth_flow.start(clean_db, org=org, user=user, slug="search_console").authorize_url
    state = parse_qs(urlsplit(url).query)["state"][0]
    payload = oauth_flow.verify_state(state)
    clean_db.commit()

    assert oauth_flow._redeem_state(clean_db, payload) is not None
    assert oauth_flow._redeem_state(clean_db, payload) is None


@requires_db
def test_sign_in_then_choose_connects_the_property(org, clean_db, monkeypatch):  # noqa: ANN001
    from app.models.workspace import User
    from app.services import connectors as connector_service

    user = clean_db.query(User).filter_by(tenant_id=org.id).first()
    url = oauth_flow.start(clean_db, org=org, user=user, slug="search_console").authorize_url
    state = parse_qs(urlsplit(url).query)["state"][0]
    clean_db.commit()

    monkeypatch.setattr(
        oauth_flow,
        "exchange_code",
        lambda provider, *, code, verifier: oauth_flow.TokenSet(
            access_token="ya29.x",
            refresh_token="1//r",
            expires_at=time.time() + 3600,
            scopes=("https://www.googleapis.com/auth/webmasters.readonly",),
            account="owner@example.com",
        ),
    )
    sites = {"siteEntry": [
        {"siteUrl": "https://a.example/", "permissionLevel": "siteOwner"},
        {"siteUrl": "https://b.example/", "permissionLevel": "siteOwner"},
    ]}
    monkeypatch.setattr(
        "app.connectors.search_console.connector.SearchConsoleConnector.request",
        lambda self, method, path, **kw: sites,
    )

    target = oauth_flow.handle_callback(clean_db, "google", {"state": state, "code": "c"})
    assert _query(target) == {"oauth_choose": "search_console"}
    record = connector_service.get_record(clean_db, tenant_id=org.id, slug="search_console")
    assert not record.connected and record.credential_hints[OAUTH_SOURCE_KEY] == PLATFORM_SOURCE
    assert "1//r" not in json.dumps(record.credential_hints)

    oauth_flow.choose(clean_db, org=org, actor=user, slug="search_console", choice_id="https://b.example/")
    assert record.connected and record.credential_hints["siteUrl"] == "https://b.example/"
