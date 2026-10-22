"""Phase 2A sign-ins: Meta, LinkedIn, X, Pinterest, TikTok, Microsoft, Bing.

Pure unit tests. Every vendor call goes to an ``httpx.MockTransport``;
nothing touches the network or the database.
"""
from __future__ import annotations

import base64
import json
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
    SIGN_IN_EXPIRY_KEY,
    Credentials,
)
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.services import oauth_flow
from app.services.oauth_flow import OAuthFlowError, StatePayload

CONSOLE = "https://console.example.com"
API = "https://api.example.com"

#: provider -> (client id, client secret)
CLIENTS = {
    "meta": ("meta-app", "meta-secret"),
    "linkedin": ("li-ads-app", "li-ads-secret"),
    "linkedin_pages": ("li-pages-app", "li-pages-secret"),
    "x": ("x-client", "x-secret"),
    "pinterest": ("pin-app", "pin-secret"),
    "tiktok": ("tt-app", "tt-secret"),
    "microsoft": ("ms-app", "ms-secret"),
    "bing_webmaster": ("bing-app", "bing-secret"),
}

#: slug -> provider
SLUGS = {
    "meta_ads": "meta",
    "facebook_pages": "meta",
    "instagram_business": "meta",
    "linkedin_ads": "linkedin",
    "linkedin_pages": "linkedin_pages",
    "x_twitter": "x",
    "pinterest": "pinterest",
    "tiktok_ads": "tiktok",
    "microsoft_ads": "microsoft",
    "bing_webmaster": "bing_webmaster",
}


@pytest.fixture(autouse=True)
def platform(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(settings, "public_base_url", CONSOLE)
    monkeypatch.setattr(settings, "public_api_base_url", API)
    for name, (cid, secret) in CLIENTS.items():
        monkeypatch.setattr(settings, f"oauth_{name}_client_id", cid)
        monkeypatch.setattr(settings, f"oauth_{name}_client_secret", secret)
    monkeypatch.setattr(settings, "microsoft_ads_developer_token", "platform-ms-dev-token")


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


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


def _signed_in(slug: str, **values: str):  # noqa: ANN003, ANN202
    base = {OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "accessToken": "stored-token"}
    base.update(values)
    return registry.get_class(slug)(Credentials(values=base), org_id="")


def _wire(connector, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    connector._client = httpx.Client(
        base_url=connector.base_url, transport=httpx.MockTransport(recording)
    )
    return seen


def _basic(request: httpx.Request) -> tuple[str, str]:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    assert scheme == "Basic"
    cid, _, secret = base64.b64decode(value).decode().partition(":")
    return cid, secret


# ── Registry and bindings ──────────────────────────────────────────────────
@pytest.mark.parametrize("slug,provider", sorted(SLUGS.items()))
def test_each_connector_is_bound_to_its_provider(slug: str, provider: str):
    spec = registry.get_spec(slug)
    assert spec.oauth is not None and spec.oauth.provider == provider
    assert oauth_providers.get(provider) is not None
    assert spec.oauth.access_summary
    assert oauth_providers.sign_in_available(spec), oauth_providers.missing_settings(spec)


def test_redirect_uris_follow_the_documented_pattern():
    for name in CLIENTS:
        provider = oauth_providers.get(name)
        assert provider.redirect_uri == f"{API}/api/v1/oauth/callback/{name}"


def test_microsoft_ads_sign_in_also_needs_the_platform_developer_token(monkeypatch):  # noqa: ANN001
    spec = registry.get_spec("microsoft_ads")
    monkeypatch.setattr(settings, "microsoft_ads_developer_token", "")
    assert oauth_providers.missing_settings(spec) == ["MICROSOFT_ADS_DEVELOPER_TOKEN"]
    assert not oauth_providers.sign_in_available(spec)


def test_linkedin_ads_and_pages_use_separate_apps():
    """LinkedIn requires the Community Management API alone on its app."""
    ads = oauth_providers.provider_for(registry.get_spec("linkedin_ads"))
    pages = oauth_providers.provider_for(registry.get_spec("linkedin_pages"))
    assert ads.name != pages.name
    assert ads.settings_keys == ("OAUTH_LINKEDIN_CLIENT_ID", "OAUTH_LINKEDIN_CLIENT_SECRET")
    assert pages.settings_keys == (
        "OAUTH_LINKEDIN_PAGES_CLIENT_ID",
        "OAUTH_LINKEDIN_PAGES_CLIENT_SECRET",
    )


# ── Authorize URLs ─────────────────────────────────────────────────────────
def _authorize(slug: str, challenge: str = "chal") -> dict[str, str]:
    spec = registry.get_spec(slug)
    provider = oauth_providers.provider_for(spec)
    url = oauth_flow.build_authorize_url(provider, spec, state="S", code_challenge=challenge)
    assert url.startswith(provider.authorize_url + "?")
    return _query(url)


def test_meta_authorize_url_asks_only_for_this_connectors_permissions():
    q = _authorize("facebook_pages")
    assert q["client_id"] == "meta-app"
    assert q["redirect_uri"] == f"{API}/api/v1/oauth/callback/meta"
    assert q["response_type"] == "code" and q["state"] == "S"
    assert q["scope"].split(",") == [
        "pages_show_list", "pages_manage_posts", "pages_read_engagement", "read_insights",
    ]
    # PKCE is documented only for Meta's OIDC flow.
    assert "code_challenge" not in q


@pytest.mark.parametrize(
    "slug,scopes",
    [
        ("meta_ads", ["ads_read", "ads_management", "pages_show_list", "pages_read_engagement"]),
        (
            "instagram_business",
            [
                "instagram_basic",
                "instagram_content_publish",
                "pages_show_list",
                "pages_read_engagement",
                "instagram_manage_insights",
            ],
        ),
    ],
)
def test_meta_scopes_per_connector(slug: str, scopes: list[str]):
    assert _authorize(slug)["scope"].split(",") == scopes


def test_linkedin_authorize_urls_are_space_separated_without_pkce():
    ads = _authorize("linkedin_ads")
    assert ads["client_id"] == "li-ads-app"
    assert ads["scope"].split(" ") == ["r_ads", "r_ads_reporting", "rw_ads", "w_organization_social"]
    assert "code_challenge" not in ads
    pages = _authorize("linkedin_pages")
    assert pages["client_id"] == "li-pages-app"
    assert pages["redirect_uri"].endswith("/oauth/callback/linkedin_pages")
    assert pages["scope"].split(" ") == [
        "w_organization_social",
        "r_organization_social",
        "rw_organization_admin",
    ]


def test_x_authorize_url_uses_pkce_and_the_posting_scopes():
    q = _authorize("x_twitter")
    assert q["code_challenge"] == "chal" and q["code_challenge_method"] == "S256"
    assert q["scope"].split(" ") == ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"]
    # Only what posting with an image needs: no DMs, likes or follows.
    assert not any(s.startswith(("dm.", "like.", "follows.")) for s in q["scope"].split(" "))


def test_pinterest_scopes_are_comma_separated():
    q = _authorize("pinterest")
    assert q["scope"] == (
        "boards:read,boards:write,boards:read_secret,boards:write_secret,"
        "pins:read,pins:write,pins:read_secret,pins:write_secret,user_accounts:read"
    )
    assert "code_challenge" not in q


def test_tiktok_uses_its_own_authorize_page():
    q = _authorize("tiktok_ads")
    assert q == {
        "app_id": "tt-app",
        "state": "S",
        "redirect_uri": f"{API}/api/v1/oauth/callback/tiktok",
    }


def test_microsoft_asks_for_ads_and_offline_access_with_pkce():
    q = _authorize("microsoft_ads")
    assert set(q["scope"].split(" ")) == {
        "openid",
        "email",
        "offline_access",
        "https://ads.microsoft.com/msads.manage",
    }
    assert q["code_challenge_method"] == "S256"
    assert q["prompt"] == "select_account"


def test_bing_asks_for_read_only_access():
    q = _authorize("bing_webmaster")
    assert q["scope"] == "webmaster.read" and q["client_id"] == "bing-app"


# ── Token exchange ─────────────────────────────────────────────────────────
def test_meta_exchanges_for_a_long_lived_token_and_reads_granted_permissions(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        if path.endswith("/oauth/access_token") and params.get("code"):
            assert request.method == "GET"
            assert params["client_secret"] == "meta-secret"
            assert params["redirect_uri"] == f"{API}/api/v1/oauth/callback/meta"
            return httpx.Response(200, json={"access_token": "short", "expires_in": 3600})
        if path.endswith("/oauth/access_token"):
            assert params["grant_type"] == "fb_exchange_token"
            assert params["fb_exchange_token"] == "short"
            return httpx.Response(200, json={"access_token": "long", "expires_in": 5_183_944})
        if path.endswith("/me/permissions"):
            assert request.headers["authorization"] == "Bearer long"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"permission": "pages_show_list", "status": "granted"},
                        {"permission": "pages_manage_posts", "status": "declined"},
                    ]
                },
            )
        if path.endswith("/me"):
            return httpx.Response(200, json={"name": "Jane Doe", "id": "1"})
        return httpx.Response(404)

    seen = _mock_client(monkeypatch, "app.services.oauth_vendors.httpx.Client", handler)
    tokens = oauth_flow.exchange_code(oauth_providers.META, code="c", verifier="", now=1000.0)
    assert tokens.access_token == "long" and tokens.refresh_token == ""
    assert tokens.expires_at == 1000.0 + 5_183_944
    assert tokens.scopes == ("pages_show_list",)
    assert tokens.account == "Jane Doe"
    assert len(seen) == 4
    # The declined permission is named before anything is stored.
    spec = registry.get_spec("facebook_pages")
    assert oauth_flow.missing_scopes(spec, tokens.scopes) == [
        "pages_manage_posts",
        "pages_read_engagement",
        "read_insights",
    ]


def test_a_refused_meta_exchange_is_a_plain_sentence(monkeypatch):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_vendors.httpx.Client",
        lambda r: httpx.Response(400, json={"error": {"message": "secret internals", "code": 100}}),
    )
    with pytest.raises(OAuthFlowError) as exc:
        oauth_flow.exchange_code(oauth_providers.META, code="c", verifier="")
    assert "Facebook did not accept" in str(exc.value)
    assert "internals" not in str(exc.value)


def test_insights_and_image_scopes_are_optional_but_posting_scopes_are_not():
    from app.connectors.base.oauth_providers import optional_scopes, required_only

    fb = registry.get_spec("facebook_pages")
    posting_only = ("pages_show_list", "pages_manage_posts", "pages_read_engagement")
    # Missing only read_insights: it connects (nothing required is absent).
    assert oauth_flow.missing_scopes(fb, posting_only) == ["read_insights"]
    assert required_only(fb, oauth_flow.missing_scopes(fb, posting_only)) == []
    # Missing a posting scope still fails, insights or not.
    no_posting = ("pages_show_list", "pages_read_engagement", "read_insights")
    assert required_only(fb, oauth_flow.missing_scopes(fb, no_posting)) == ["pages_manage_posts"]
    assert required_only(fb, oauth_flow.missing_scopes(fb, ("pages_show_list",))) == [
        "pages_manage_posts",
        "pages_read_engagement",
    ]

    ig = registry.get_spec("instagram_business")
    ig_posting = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement")
    assert required_only(ig, oauth_flow.missing_scopes(ig, ig_posting)) == []
    assert required_only(ig, oauth_flow.missing_scopes(ig, ig_posting[1:])) == ["instagram_basic"]

    x = registry.get_spec("x_twitter")
    x_posting = ("tweet.read", "tweet.write", "users.read", "offline.access")
    assert required_only(x, oauth_flow.missing_scopes(x, x_posting)) == []
    assert required_only(x, oauth_flow.missing_scopes(x, ("tweet.read", "users.read", "offline.access", "media.write"))) == [
        "tweet.write"
    ]

    # Optional scopes are still asked for, and are only ever the extras.
    for spec in (fb, ig, x):
        assert optional_scopes(spec) <= set(spec.oauth.scopes)
        assert set(spec.oauth.scopes) - optional_scopes(spec)
    assert optional_scopes(registry.get_spec("linkedin_pages")) == frozenset()
    assert "read_insights" in _authorize("facebook_pages")["scope"].split(",")


def test_x_exchange_uses_basic_auth_and_the_verifier(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/oauth2/token":
            assert _basic(request) == ("x-client", "x-secret")
            form = _form(request)
            assert "client_secret" not in form
            assert form["code_verifier"] == "v" and form["grant_type"] == "authorization_code"
            return httpx.Response(
                200,
                json={
                    "access_token": "xa",
                    "refresh_token": "xr",
                    "expires_in": 7200,
                    "scope": "tweet.read tweet.write users.read offline.access media.write",
                },
            )
        assert request.url.path == "/2/users/me"
        return httpx.Response(200, json={"data": {"id": "9", "username": "brand"}})

    # One patch covers both modules: they share the httpx module.
    _mock_client(monkeypatch, "app.services.oauth_flow.httpx.Client", handler)
    tokens = oauth_flow.exchange_code(oauth_providers.X, code="c", verifier="v", now=1.0)
    assert (tokens.access_token, tokens.refresh_token, tokens.expires_at) == ("xa", "xr", 7201.0)
    assert tokens.account == "@brand"
    assert not oauth_flow.missing_scopes(registry.get_spec("x_twitter"), tokens.scopes)
    # A sign-in from before images were supported lacks only media.write,
    # which is optional: it still connects (posts go out as text).
    older = ("tweet.read", "tweet.write", "users.read", "offline.access")
    assert oauth_flow.missing_scopes(registry.get_spec("x_twitter"), older) == ["media.write"]
    assert oauth_providers.required_only(registry.get_spec("x_twitter"), ["media.write"]) == []


def test_pinterest_exchange_uses_basic_auth(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/oauth/token":
            assert _basic(request) == ("pin-app", "pin-secret")
            return httpx.Response(
                200,
                json={
                    "access_token": "pina",
                    "refresh_token": "pinr",
                    "expires_in": 2592000,
                    "scope": (
                        "boards:read boards:write boards:read_secret boards:write_secret "
                        "pins:read pins:write pins:read_secret pins:write_secret user_accounts:read"
                    ),
                },
            )
        return httpx.Response(200, json={"username": "brandpins"})

    # One patch covers both modules: they share the httpx module.
    _mock_client(monkeypatch, "app.services.oauth_flow.httpx.Client", handler)
    tokens = oauth_flow.exchange_code(oauth_providers.PINTEREST, code="c", verifier="")
    assert tokens.refresh_token == "pinr" and tokens.account == "brandpins"
    assert not oauth_flow.missing_scopes(registry.get_spec("pinterest"), tokens.scopes)


def test_linkedin_comma_separated_scopes_are_understood(monkeypatch):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_flow.httpx.Client",
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "AQ",
                "expires_in": 5184000,
                "scope": "r_ads,r_ads_reporting,rw_ads,w_organization_social",
            },
        ),
    )
    tokens = oauth_flow.exchange_code(oauth_providers.LINKEDIN, code="c", verifier="", now=1.0)
    assert tokens.refresh_token == "" and tokens.expires_at == 5184001.0
    assert not oauth_flow.missing_scopes(registry.get_spec("linkedin_ads"), tokens.scopes)


def test_tiktok_exchanges_the_auth_code_as_json(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open_api/v1.3/oauth2/access_token/"
        assert json.loads(request.content) == {
            "app_id": "tt-app",
            "secret": "tt-secret",
            "auth_code": "ac",
        }
        return httpx.Response(
            200,
            json={"code": 0, "message": "OK", "data": {"access_token": "tt", "advertiser_ids": ["1"], "scope": [4]}},
        )

    _mock_client(monkeypatch, "app.services.oauth_vendors.httpx.Client", handler)
    tokens = oauth_flow.exchange_code(oauth_providers.TIKTOK, code="ac", verifier="")
    # The long-term token does not expire; nothing is invented.
    assert tokens.access_token == "tt" and tokens.expires_at == 0.0 and tokens.scopes == ()


def test_a_tiktok_error_envelope_is_refused(monkeypatch):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_vendors.httpx.Client",
        lambda r: httpx.Response(200, json={"code": 40001, "message": "bad", "data": {}}),
    )
    with pytest.raises(OAuthFlowError, match="TikTok did not accept"):
        oauth_flow.exchange_code(oauth_providers.TIKTOK, code="ac", verifier="")


def test_microsoft_names_the_account_from_preferred_username(monkeypatch):  # noqa: ANN001
    claims = base64.urlsafe_b64encode(json.dumps({"preferred_username": "ads@contoso.com"}).encode())
    id_token = "e30." + claims.decode().rstrip("=") + ".sig"
    _mock_client(
        monkeypatch,
        "app.services.oauth_flow.httpx.Client",
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "ms",
                "refresh_token": "msr",
                "expires_in": 3600,
                "scope": "https://ads.microsoft.com/msads.manage",
                "id_token": id_token,
            },
        ),
    )
    tokens = oauth_flow.exchange_code(oauth_providers.MICROSOFT, code="c", verifier="v")
    assert tokens.account == "ads@contoso.com"
    assert not oauth_flow.missing_scopes(registry.get_spec("microsoft_ads"), tokens.scopes)
    # Echoed by its last segment only, it still counts as granted.
    assert not oauth_flow.missing_scopes(registry.get_spec("microsoft_ads"), ("msads.manage",))


# ── The callback accepts TikTok's auth_code ────────────────────────────────
def test_the_callback_reads_tiktoks_auth_code(monkeypatch):  # noqa: ANN001
    payload = StatePayload(
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        slug="tiktok_ads",
        provider="tiktok",
        nonce="n",
        return_path="/connectors",
        expires_at=int(time.time()) + 600,
    )

    class Db:
        def commit(self) -> None: ...
        def rollback(self) -> None: ...

    class Org:
        id = payload.tenant_id

    codes: list[str] = []
    monkeypatch.setattr("app.db.session.bind_tenant", lambda db, tenant: None)
    monkeypatch.setattr(
        oauth_flow, "_redeem_state", lambda db, p: oauth_flow.RedeemedState("", {})
    )
    monkeypatch.setattr(oauth_flow, "_load_actor", lambda db, p: (Org(), "USER"))
    monkeypatch.setattr("app.services.connectors.get_record", lambda db, tenant_id, slug: "rec")
    monkeypatch.setattr(oauth_flow, "write_platform_tokens", lambda db, **kw: None)
    monkeypatch.setattr(oauth_flow, "_auto_choose", lambda db, **kw: "connected")

    def exchange(provider, *, code, verifier):  # noqa: ANN001, ANN202
        codes.append(code)
        return oauth_flow.TokenSet(access_token="tt")

    monkeypatch.setattr(oauth_flow, "exchange_code", exchange)
    url = oauth_flow.handle_callback(
        Db(), "tiktok", {"state": oauth_flow.sign_state(payload), "auth_code": "ac", "code": ""}
    )
    assert _query(url)["connected"] == "tiktok_ads" and codes == ["ac"]


# ── Storing a sign-in that has no refresh token ────────────────────────────
class _Cipher:
    def encrypt_json(self, values: dict, *, context: str) -> str:
        return json.dumps(values)


class _FlushDb:
    def flush(self) -> None: ...


@pytest.fixture
def store(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(oauth_flow.OrgCipher, "for_org", classmethod(lambda cls, org: _Cipher()))
    monkeypatch.setattr(
        "app.services.connectors.load_credentials", lambda record, cipher: Credentials(values={})
    )

    def write(slug: str, tokens: oauth_flow.TokenSet):  # noqa: ANN202
        from app.models.connector import ConnectorRecord

        record = ConnectorRecord(
            slug=slug, name=slug, category="x", credential_hints={}, credentials_version=0
        )
        oauth_flow.write_platform_tokens(
            _FlushDb(), org=object(), record=record, spec=registry.get_spec(slug), tokens=tokens
        )
        return record, json.loads(record.credentials_encrypted)

    return write


def test_a_meta_sign_in_is_stored_with_its_end_date(store):  # noqa: ANN001
    ends = time.time() + 60 * 86400
    record, secrets = store("meta_ads", oauth_flow.TokenSet(access_token="long", expires_at=ends))
    assert secrets["accessToken"] == "long" and "refreshToken" not in secrets
    assert secrets[ACCESS_TOKEN_EXPIRY_KEY] == str(int(ends))
    assert record.credential_hints[SIGN_IN_EXPIRY_KEY] == str(int(ends))
    assert record.credential_hints[OAUTH_SOURCE_KEY] == PLATFORM_SOURCE
    # Nothing secret in the readable hints.
    assert "long" not in json.dumps(record.credential_hints)


def test_a_tiktok_sign_in_has_no_end_date(store):  # noqa: ANN001
    record, secrets = store("tiktok_ads", oauth_flow.TokenSet(access_token="tt"))
    assert secrets == {"accessToken": "tt"}
    assert SIGN_IN_EXPIRY_KEY not in record.credential_hints


def test_a_renewing_sign_in_has_no_end_date_hint(store):  # noqa: ANN001
    record, secrets = store(
        "x_twitter",
        oauth_flow.TokenSet(access_token="xa", refresh_token="xr", expires_at=time.time() + 7200),
    )
    assert secrets["refreshToken"] == "xr"
    assert SIGN_IN_EXPIRY_KEY not in record.credential_hints


def test_google_still_requires_a_refresh_token(store, monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "oauth_google_client_id", "g")
    monkeypatch.setattr(settings, "oauth_google_client_secret", "g")
    with pytest.raises(OAuthFlowError, match="Google did not grant lasting access"):
        store("search_console", oauth_flow.TokenSet(access_token="ya29", expires_at=time.time() + 3600))


def test_sign_in_tokens_are_never_stored_as_readable_hints():
    from app.services.connectors import split_values

    spec = registry.get_spec("facebook_pages")  # declares neither field
    secrets, hints = split_values(
        spec, {"accessToken": "user", "refreshToken": "r", "pageId": "1", "pageAccessToken": "p"}
    )
    assert set(secrets) == {"accessToken", "refreshToken", "pageAccessToken"}
    assert hints == {"pageId": "1"}


def test_single_use_refresh_tokens_are_never_shared_between_connections():
    assert oauth_flow._reusable_sign_in(
        None, org=object(), spec=registry.get_spec("x_twitter"), exclude="x_twitter"
    ) is None


# ── Choices per connector ──────────────────────────────────────────────────
def test_meta_ads_lists_ad_accounts_across_pages_and_skips_closed_ones():
    connector = _signed_in("meta_ads")

    def handler(request: httpx.Request) -> httpx.Response:
        if "after=c2" in str(request.url):
            return httpx.Response(
                200, json={"data": [{"id": "act_3", "account_id": "3", "name": "Old", "account_status": 101}]}
            )
        assert request.url.path.endswith("/me/adaccounts")
        assert request.headers["authorization"] == "Bearer stored-token"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "act_1", "account_id": "1", "name": "Main", "account_status": 1, "currency": "INR"},
                    {"id": "act_2", "account_id": "2", "name": "Paused", "account_status": 2},
                ],
                "paging": {"next": "https://graph.facebook.com/v26.0/me/adaccounts?after=c2"},
            },
        )

    _wire(connector, handler)
    choices = connector.list_oauth_choices()
    assert [c.values for c in choices] == [{"adAccountId": "act_1"}, {"adAccountId": "act_2"}]
    assert choices[0].detail == "Account ID 1 · INR"
    assert "Disabled" in choices[1].detail


def test_facebook_pages_offers_pages_the_person_can_post_to():
    connector = _signed_in("facebook_pages")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me/accounts")
        assert request.headers["authorization"] == "Bearer stored-token"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "10", "name": "Dealer", "access_token": "page-10", "tasks": ["CREATE_CONTENT", "ANALYZE"]},
                    {"id": "11", "name": "Analyst only", "access_token": "page-11", "tasks": ["ANALYZE"]},
                    {"id": "12", "name": "No token"},
                ]
            },
        )

    _wire(connector, handler)
    [choice] = connector.list_oauth_choices()
    assert choice.label == "Dealer"
    assert choice.values == {"pageId": "10", "pageAccessToken": "page-10", SIGN_IN_EXPIRY_KEY: ""}


def test_facebook_pages_posts_with_the_page_token_once_chosen():
    connector = _signed_in("facebook_pages", pageId="10", pageAccessToken="page-10")
    assert connector.auth_headers()["Authorization"] == "Bearer page-10"


def test_instagram_lists_accounts_linked_to_pages():
    connector = _signed_in("instagram_business")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "instagram_business_account" in request.url.params["fields"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "10",
                        "name": "Dealer",
                        "access_token": "page-10",
                        "instagram_business_account": {"id": "1784", "username": "dealer"},
                    },
                    {"id": "11", "name": "No Instagram", "access_token": "page-11"},
                ]
            },
        )

    _wire(connector, handler)
    [choice] = connector.list_oauth_choices()
    assert choice.label == "@dealer" and choice.detail == "Linked to the Facebook Page Dealer"
    assert choice.values == {"igUserId": "1784", "pageAccessToken": "page-10", SIGN_IN_EXPIRY_KEY: ""}
    chosen = _signed_in("instagram_business", igUserId="1784", pageAccessToken="page-10")
    assert chosen.auth_headers()["Authorization"] == "Bearer page-10"


def test_linkedin_ads_lists_accessible_accounts():
    connector = _signed_in("linkedin_ads")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/adAccounts"
        assert request.url.params["q"] == "search"
        assert request.headers["LinkedIn-Version"]
        if request.url.params.get("pageToken") == "t2":
            return httpx.Response(200, json={"elements": [{"id": 3, "name": "Gone", "status": "REMOVED"}]})
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"id": 509912345, "name": "Brand", "currency": "USD", "status": "ACTIVE"},
                    {"id": 2, "name": "Draft", "status": "DRAFT", "test": True},
                ],
                "metadata": {"nextPageToken": "t2"},
            },
        )

    _wire(connector, handler)
    choices = connector.list_oauth_choices()
    assert [c.values["adAccountId"] for c in choices] == ["509912345", "2"]
    assert choices[1].detail == "Account ID 2 · Draft · Test account"


def test_linkedin_pages_lists_pages_the_member_can_post_for():
    connector = _signed_in("linkedin_pages")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/organizationAcls":
            assert request.url.params["q"] == "roleAssignee"
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"role": "ADMINISTRATOR", "state": "APPROVED", "organization": "urn:li:organization:1"},
                        {"role": "CONTENT_ADMINISTRATOR", "state": "APPROVED", "organizationTarget": "urn:li:organization:2"},
                        {"role": "ANALYST", "state": "APPROVED", "organization": "urn:li:organization:3"},
                    ]
                },
            )
        if request.url.path == "/rest/organizations/1":
            return httpx.Response(200, json={"localizedName": "Acme"})
        return httpx.Response(403, json={"message": "no"})

    _wire(connector, handler)
    choices = connector.list_oauth_choices()
    assert [(c.label, c.values["organizationId"]) for c in choices] == [
        ("Acme", "1"),
        ("Company page 2", "2"),
    ]


def test_x_connects_the_signed_in_account_without_a_picker():
    connector = _signed_in(
        "x_twitter", refreshToken="xr", **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3600)}
    )
    seen = _wire(
        connector,
        lambda r: httpx.Response(200, json={"data": {"id": "9", "username": "brand", "name": "Brand"}}),
    )
    [choice] = connector.list_oauth_choices()
    assert choice.label == "@brand"
    assert seen[0].headers["authorization"] == "Bearer stored-token"


def test_pinterest_lists_boards_through_bookmarks():
    connector = _signed_in("pinterest")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("bookmark") == "b2":
            return httpx.Response(200, json={"items": [{"id": "3", "name": "Secret", "privacy": "SECRET"}], "bookmark": None})
        assert request.url.params["page_size"] == "250"
        return httpx.Response(
            200,
            json={"items": [{"id": "1", "name": "Cars", "privacy": "PUBLIC", "pin_count": 4}], "bookmark": "b2"},
        )

    _wire(connector, handler)
    choices = connector.list_oauth_choices()
    assert [(c.label, c.values["boardId"]) for c in choices] == [("Cars", "1"), ("Secret", "3")]
    assert choices[0].detail == "4 Pins" and choices[1].detail == "Secret board"


def test_tiktok_lists_advertisers_with_the_platform_app():
    connector = _signed_in("tiktok_ads")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open_api/v1.3/oauth2/advertiser/get/"
        assert dict(request.url.params) == {"app_id": "tt-app", "secret": "tt-secret"}
        assert request.headers["Access-Token"] == "stored-token"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"list": [{"advertiser_id": "70", "advertiser_name": "Brand"}]}},
        )

    _wire(connector, handler)
    [choice] = connector.list_oauth_choices()
    assert choice.values == {"advertiserId": "70"} and choice.label == "Brand"


def test_microsoft_ads_lists_accounts_of_each_customer():
    connector = _signed_in(
        "microsoft_ads", refreshToken="msr", **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3600)}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer stored-token"
        assert request.headers["DeveloperToken"] == "platform-ms-dev-token"
        assert "CustomerId" not in request.headers
        body = json.loads(request.content)
        if request.url.path.endswith("/User/Query"):
            assert body == {"UserId": None}
            return httpx.Response(
                200,
                json={"User": {"Id": "5"}, "CustomerRoles": [{"CustomerId": "100", "AccountIds": ["200"]}]},
            )
        assert request.url.path.endswith("/AccountsInfo/Query")
        assert body["CustomerId"] == "100"
        return httpx.Response(
            200,
            json={
                "AccountsInfo": [
                    {"Id": "200", "Name": "Search", "Number": "F1", "AccountLifeCycleStatus": "Active"},
                    {"Id": "201", "Name": "Old", "AccountLifeCycleStatus": "Inactive"},
                ]
            },
        )

    _wire(connector, handler)
    [choice] = connector.list_oauth_choices()
    assert choice.values == {"customerId": "100", "accountId": "200"}
    assert choice.detail == "Account F1 · Customer 100"


def test_microsoft_ads_calls_carry_the_platform_developer_token_only_when_signed_in():
    signed = _signed_in(
        "microsoft_ads",
        refreshToken="r",
        customerId="1",
        accountId="2",
        **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3600)},
    )
    headers = signed.auth_headers()
    assert headers["DeveloperToken"] == "platform-ms-dev-token"
    assert headers["CustomerId"] == "1" and headers["CustomerAccountId"] == "2"
    pasted = registry.get_class("microsoft_ads")(
        Credentials(values={"accessToken": "t", "customerId": "1", "accountId": "2"}), org_id=""
    )
    assert "DeveloperToken" not in pasted.auth_headers()


def test_bing_lists_verified_sites_with_a_bearer_token():
    connector = _signed_in(
        "bing_webmaster", refreshToken="br", **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3600)}
    )
    assert connector.base_url == "https://www.bing.com/webmaster/api.svc/json"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/GetUserSites")
        assert "apikey" not in request.url.params
        assert request.headers["authorization"] == "Bearer stored-token"
        return httpx.Response(
            200,
            json={"d": [{"Url": "https://a.com/", "IsVerified": True}, {"Url": "https://b.com/", "IsVerified": False}]},
        )

    _wire(connector, handler)
    assert [c.values for c in connector.list_oauth_choices()] == [{"siteUrl": "https://a.com/"}]


def test_bing_with_an_api_key_is_unchanged():
    connector = registry.get_class("bing_webmaster")(
        Credentials(values={"apiKey": "k", "siteUrl": "https://a.com/"}), org_id=""
    )
    assert connector.base_url == "https://ssl.bing.com/webmaster/api.svc/json"
    seen = _wire(connector, lambda r: httpx.Response(200, json={"d": []}))
    connector.read_search_performance()
    assert seen[0].url.params["apikey"] == "k" and "authorization" not in seen[0].headers


# ── Refresh and expiry ─────────────────────────────────────────────────────
def _expired(slug: str, **values: str):  # noqa: ANN003, ANN202
    return _signed_in(slug, **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) - 10)}, **values)


def test_x_refresh_uses_basic_auth_and_stores_the_new_refresh_token(monkeypatch):  # noqa: ANN001
    saved: list[dict] = []
    monkeypatch.setattr(
        "app.services.oauth_tokens.save_rotated", lambda org_id, slug, **kw: saved.append(kw) or True
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert _basic(request) == ("x-client", "x-secret")
        form = _form(request)
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "old"
        assert "client_secret" not in form
        return httpx.Response(200, json={"access_token": "new-a", "refresh_token": "new-r", "expires_in": 7200})

    _mock_client(monkeypatch, "app.connectors.base.oauth.httpx.Client", handler)
    connector = _expired("x_twitter", refreshToken="old")
    connector.org_id = "org-1"
    assert connector.access_token == "new-a"
    assert connector.credentials.values["refreshToken"] == "new-r"
    assert saved == [
        {"used_refresh": "old", "refresh": "new-r", "access": "new-a", "expires_at": pytest.approx(time.time() + 7200, abs=5)}
    ]


def test_a_refresh_token_spent_by_another_worker_is_recovered(monkeypatch):  # noqa: ANN001
    fresh = str(int(time.time()) + 3600)
    monkeypatch.setattr(
        "app.services.oauth_tokens.load_current",
        lambda org_id, slug: {"refreshToken": "newer", "accessToken": "theirs", ACCESS_TOKEN_EXPIRY_KEY: fresh},
    )
    _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(400, json={"error": "invalid_request"}),
    )
    connector = _expired("x_twitter", refreshToken="spent")
    connector.org_id = "org-1"
    assert connector.access_token == "theirs"


def test_a_refused_refresh_with_nothing_newer_still_fails(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr("app.services.oauth_tokens.load_current", lambda org_id, slug: {})
    _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(400, json={"error": "invalid_grant"}),
    )
    connector = _expired("x_twitter", refreshToken="spent")
    connector.org_id = "org-1"
    with pytest.raises(ConnectorError, match="invalid_grant"):
        _ = connector.access_token


def test_pinterest_refresh_uses_basic_auth(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr("app.services.oauth_tokens.save_rotated", lambda *a, **k: True)
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "pina2", "refresh_token": "pinr2", "expires_in": 2592000}),
    )
    connector = _expired("pinterest", refreshToken="pinr")
    assert connector.access_token == "pina2"
    assert _basic(seen[0]) == ("pin-app", "pin-secret")
    assert seen[0].url == "https://api.pinterest.com/v5/oauth/token"


def test_microsoft_refresh_sends_the_scope_and_keeps_the_new_refresh_token(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "ms2", "refresh_token": "msr2", "expires_in": 3600}),
    )
    connector = _expired("microsoft_ads", refreshToken="msr")
    assert connector.access_token == "ms2"
    form = _form(seen[0])
    assert form["client_id"] == "ms-app" and form["client_secret"] == "ms-secret"
    assert form["scope"] == "https://ads.microsoft.com/msads.manage offline_access"
    # Not saved anywhere (no workspace here), but used from now on.
    assert connector.credentials.values["refreshToken"] == "msr2"


def test_linkedin_refreshes_when_it_issued_a_refresh_token(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "li2", "expires_in": 5184000, "refresh_token": "lir"}),
    )
    connector = _expired("linkedin_ads", refreshToken="lir")
    assert connector.access_token == "li2"
    assert _form(seen[0])["client_id"] == "li-ads-app"
    assert seen[0].url == "https://www.linkedin.com/oauth/v2/accessToken"


@pytest.mark.parametrize("slug,label", [("meta_ads", "Facebook"), ("linkedin_ads", "LinkedIn")])
def test_an_expired_sign_in_that_cannot_renew_says_sign_in_again(slug: str, label: str):
    connector = _expired(slug)
    with pytest.raises(ConnectorConfigError) as exc:
        _ = connector.access_token
    assert f"The {label} sign-in" in str(exc.value) and "Sign in again" in str(exc.value)


def test_a_current_long_lived_sign_in_is_used_as_stored():
    connector = _signed_in("meta_ads", **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 86400)})
    assert connector.access_token == "stored-token"


def test_a_pasted_token_has_no_expiry_check():
    connector = registry.get_class("meta_ads")(
        Credentials(values={"accessToken": "system-user", ACCESS_TOKEN_EXPIRY_KEY: "1"}), org_id=""
    )
    assert connector.access_token == "system-user"


def test_a_graph_token_error_on_a_signed_in_page_says_sign_in_again():
    connector = _signed_in("facebook_pages", pageId="10", pageAccessToken="page-10")
    _wire(connector, lambda r: httpx.Response(400, json={"error": {"code": 190, "message": "expired"}}))
    report = connector.check_health()
    assert not report.ok and "Sign in again" in report.detail


def test_x_pasted_oauth1_keys_still_sign_requests():
    connector = registry.get_class("x_twitter")(
        Credentials(
            values={"accessToken": "t", "accessTokenSecret": "ts", "apiKey": "k", "apiKeySecret": "ks"}
        ),
        org_id="",
    )
    seen = _wire(connector, lambda r: httpx.Response(200, json={"data": {"username": "u"}}))
    assert connector.check_health().ok
    assert seen[0].headers["authorization"].startswith("OAuth ")


# ── What the connect screen shows ──────────────────────────────────────────
def _record(slug: str, **kw):  # noqa: ANN003, ANN202
    from app.models.connector import ConnectorRecord

    return ConnectorRecord(slug=slug, name=slug, category="x", credential_hints=kw.pop("hints", {}), **kw)


#: The words a marketer should never meet before choosing Advanced. Same
#: list as tests/test_oauth_flow.py::test_no_default_path_asks_for_developer_credentials.
BANNED = re.compile(r"client id|client secret|refresh token|developer token|redirect uri", re.I)
#: And no raw tokens, ids or secrets on the default path either. A Pinterest
#: "secret board" is the product's own name for a private board, not a
#: credential, so it may be named.
TOKENISH = re.compile(r"access token|api key|app id|secret(?! boards?\b)", re.I)


@pytest.mark.parametrize("slug", sorted(SLUGS))
def test_default_path_has_no_developer_wording(slug: str):
    from app.services.views import connect_guide

    spec = registry.get_spec(slug)
    guide = connect_guide(_record(slug, connected=False))
    assert guide.mode == "sign_in" and guide.sign_in_available
    assert guide.button_label == f"Sign in with {guide.provider_label}"
    shown = [f for f in spec.fields if f.key not in guide.sign_in_hidden_fields]
    for f in shown:
        assert not BANNED.search(f.label), (slug, f.label)
        assert not TOKENISH.search(f.label), (slug, f.label)
        assert not f.is_secret, (slug, f.key)
    # (Key steps, where a connector has them, belong to Advanced here.)
    for text in (guide.access_summary, guide.button_label):
        assert not BANNED.search(text) and not TOKENISH.search(text), (slug, text)
    # Every required field is hidden: the sign-in and the picker supply it.
    assert not [f.key for f in shown if f.required and not f.default], slug


def test_bing_falls_back_to_the_key_guide_until_the_platform_app_is_set_up(monkeypatch):  # noqa: ANN001
    from app.services.views import ConnectContext, connect_guide

    monkeypatch.setattr(settings, "oauth_bing_webmaster_client_id", "")
    guide = connect_guide(_record("bing_webmaster", connected=False))
    assert guide.mode == "key" and guide.key_field == "apiKey" and guide.admin_hint == ""
    admin = connect_guide(_record("bing_webmaster", connected=False), ConnectContext(show_admin_notes=True))
    assert admin.mode == "key" and "Sign in with Bing" in admin.admin_hint
    # The key steps are the route here, so no developer form is offered.
    assert admin.developer_label == "" and not admin.setup_pending


def test_other_unconfigured_vendors_wait_for_sign_in_with_the_form_behind_a_developer_link(monkeypatch):  # noqa: ANN001
    from app.services.views import ConnectContext, connect_guide

    monkeypatch.setattr(settings, "oauth_meta_client_id", "")
    guide = connect_guide(_record("meta_ads", connected=False), ConnectContext(show_admin_notes=True))
    assert guide.mode == "sign_in" and not guide.sign_in_available and guide.setup_pending
    assert guide.setup_message.startswith("Sign in with Facebook isn't switched on")
    assert guide.developer_label == "For developers: connect with your own app credentials"


def test_the_advanced_link_names_the_vendor():
    from app.services.views import connect_guide

    assert connect_guide(_record("pinterest", connected=False)).manual_label == (
        "Advanced: connect with your own Pinterest app or token"
    )
    assert "API key" in connect_guide(_record("bing_webmaster", connected=False)).manual_label


def test_meta_offers_the_other_meta_integrations_after_connecting():
    from app.services.views import ConnectContext, connect_guide

    hints = {OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "oauthAccount": "Jane Doe"}
    ctx = ConnectContext(
        not_connected={"meta": [("facebook_pages", "Facebook Pages"), ("instagram_business", "Instagram")]}
    )
    guide = connect_guide(_record("meta_ads", connected=True, hints=hints), ctx)
    assert [link.slug for link in guide.also_connect] == ["facebook_pages", "instagram_business"]


def test_the_sign_in_note_counts_down_to_sign_in_again():
    from datetime import UTC, datetime, timedelta

    from app.services.views import sign_in_note

    now = datetime(2026, 10, 8, tzinfo=UTC)

    def note(days: float) -> str:
        ends = str(int((now + timedelta(days=days)).timestamp()))
        return sign_in_note(_record("meta_ads", hints={SIGN_IN_EXPIRY_KEY: ends}), "Facebook", now=now)

    assert "ends on 7 December 2026" in note(60) and "do not renew themselves" in note(60)
    assert note(5).startswith("Your Facebook sign-in ends on 13 October 2026")
    assert "has expired" in note(-1) and "Sign in again" in note(-1)
    assert sign_in_note(_record("meta_ads"), "Facebook", now=now) == ""


def test_admin_overview_lists_the_new_providers_and_their_callbacks():
    statuses = {s.name: s for s in oauth_flow.provider_statuses()}
    for name in CLIENTS:
        assert statuses[name].configured
        assert statuses[name].redirect_uri == f"{API}/api/v1/oauth/callback/{name}"
    assert {c["slug"] for c in statuses["meta"].connectors} == {
        "meta_ads",
        "facebook_pages",
        "instagram_business",
    }
