"""Sign-in for the business apps (phase 2B).

Pure tests — no database, every vendor call answered by httpx.MockTransport.
What is exercised: the authorize URL each vendor is sent to, the code
exchange and what is kept from it, Shopify's signed callback and store
address, the choose-step parsing per connector, refreshes, the GitHub App
installation tokens, the platform's included Google keys, and that no
default path shows developer credentials.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import oauth_providers, registry
from app.connectors.base.connector import KEY_SOURCE_KEY, PLATFORM_KEY_SOURCE, resolve_key
from app.connectors.base.credentials import (
    ACCESS_TOKEN_EXPIRY_KEY,
    OAUTH_SOURCE_KEY,
    PLATFORM_SOURCE,
    Credentials,
    validate,
)
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError, InvalidInputError
from app.services import oauth_business, oauth_flow
from app.services.oauth_flow import OAuthFlowError, StatePayload

CONSOLE = "https://console.example.com"
API = "https://api.example.com"
BUSINESS = ("slack", "hubspot", "salesforce", "mailchimp", "klaviyo", "shopify", "github", "bitbucket", "webflow")


@pytest.fixture(autouse=True)
def platform(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(settings, "public_base_url", CONSOLE)
    monkeypatch.setattr(settings, "public_api_base_url", API)
    for name in BUSINESS:
        monkeypatch.setattr(settings, f"oauth_{name}_client_id", f"{name}-client")
        monkeypatch.setattr(settings, f"oauth_{name}_client_secret", f"{name}-secret")
    monkeypatch.setattr(settings, "github_app_slug", "automarket-ai")
    monkeypatch.setattr(settings, "github_app_id", "123456")
    monkeypatch.setattr(settings, "github_app_private_key", _RSA_PEM)
    monkeypatch.setattr(settings, "platform_pagespeed_api_key", "")
    monkeypatch.setattr(settings, "platform_google_places_api_key", "")


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


_RSA_PEM = _rsa_pem()


_REAL_CLIENT = httpx.Client


def _mock_client(monkeypatch, target: str, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []
    real = _REAL_CLIENT

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs.pop("timeout", None)
        kwargs.pop("verify", None)
        return real(transport=httpx.MockTransport(recording), **kwargs)

    monkeypatch.setattr(target, factory)
    return seen


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _connector(slug: str, **values: str):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=dict(values)), org_id="org-1")


def _signed_in(slug: str, **values: str):  # noqa: ANN202
    base = {OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "accessToken": "at-stored"}
    return _connector(slug, **{**base, **values})


def _serve(connector, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    connector._client = _REAL_CLIENT(base_url=connector.base_url, transport=httpx.MockTransport(recording))
    return seen


# ── Providers and bindings ─────────────────────────────────────────────────
def test_every_business_app_has_a_provider_and_a_binding():
    for name in BUSINESS:
        provider = oauth_providers.get(name)
        assert provider is not None and provider.extension == name
        spec = registry.get_spec(name)
        assert spec.oauth is not None and spec.oauth.provider == name
        assert oauth_providers.sign_in_available(spec), name
        assert provider.redirect_uri == f"{API}/api/v1/oauth/callback/{name}"


@pytest.mark.parametrize(
    ("slug", "scopes"),
    [
        ("slack", ("chat:write", "chat:write.public", "channels:read")),
        ("hubspot", ("crm.objects.deals.read", "crm.objects.contacts.read")),
        ("salesforce", ("api", "refresh_token")),
        ("mailchimp", ()),
        ("klaviyo", ("accounts:read", "templates:read", "templates:write")),
        ("shopify", ("write_content", "write_products")),
        ("github", ()),
        ("bitbucket", ("repository:write", "pullrequest:write")),
        ("webflow", ("sites:read", "cms:read", "cms:write")),
    ],
)
def test_each_connector_asks_for_the_minimum_scopes(slug: str, scopes: tuple[str, ...]):
    assert registry.get_spec(slug).oauth.scopes == scopes


def test_github_sign_in_needs_the_app_settings_too(monkeypatch):  # noqa: ANN001
    spec = registry.get_spec("github")
    monkeypatch.setattr(settings, "github_app_private_key", "")
    assert not oauth_providers.sign_in_available(spec)
    assert oauth_providers.missing_settings(spec) == ["GITHUB_APP_PRIVATE_KEY"]


def test_a_business_provider_without_credentials_is_not_offered(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "oauth_shopify_client_secret", "")
    assert oauth_providers.missing_settings(registry.get_spec("shopify")) == [
        "OAUTH_SHOPIFY_CLIENT_ID",
        "OAUTH_SHOPIFY_CLIENT_SECRET",
    ]


# ── Authorize URLs ─────────────────────────────────────────────────────────
def _authorize(slug: str, vars_: dict | None = None, challenge: str = "") -> str:
    spec = registry.get_spec(slug)
    provider = oauth_providers.provider_for(spec)
    return oauth_business.authorize_url(
        provider, spec, state="S", code_challenge=challenge, vars_=vars_ or {}
    )


def _base(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def test_add_to_slack_uses_comma_separated_bot_scopes():
    url = _authorize("slack")
    q = _query(url)
    assert _base(url) == "https://slack.com/oauth/v2/authorize"
    assert q["scope"] == "chat:write,chat:write.public,channels:read"
    assert q["client_id"] == "slack-client" and q["state"] == "S"
    assert q["redirect_uri"] == f"{API}/api/v1/oauth/callback/slack"
    assert "user_scope" not in q and "code_challenge" not in q


def test_hubspot_adds_its_base_oauth_scope():
    q = _query(_authorize("hubspot"))
    assert q["scope"].split() == ["oauth", "crm.objects.deals.read", "crm.objects.contacts.read"]


@pytest.mark.parametrize(("sandbox", "host"), [("", "login.salesforce.com"), ("true", "test.salesforce.com")])
def test_salesforce_sandbox_switches_the_login_host(sandbox: str, host: str):
    provider = oauth_providers.SALESFORCE
    checked = oauth_business.prepare(provider, {"sandbox": sandbox})
    assert checked == {"login_host": host}
    url = _authorize("salesforce", checked, challenge="CH")
    q = _query(url)
    assert _base(url) == f"https://{host}/services/oauth2/authorize"
    assert q["code_challenge"] == "CH" and q["code_challenge_method"] == "S256"
    assert q["scope"] == "api refresh_token"
    assert oauth_business.token_url(provider, checked) == f"https://{host}/services/oauth2/token"


def test_mailchimp_and_bitbucket_send_no_scope_parameter():
    for slug, base in (
        ("mailchimp", "https://login.mailchimp.com/oauth2/authorize"),
        ("bitbucket", "https://bitbucket.org/site/oauth2/authorize"),
    ):
        url = _authorize(slug)
        assert _base(url) == base
        assert "scope" not in _query(url) and _query(url)["response_type"] == "code"


def test_klaviyo_requires_pkce():
    q = _query(_authorize("klaviyo", challenge="CH"))
    assert q["code_challenge"] == "CH" and q["scope"] == "accounts:read templates:read templates:write"


def test_shopify_authorize_goes_to_the_checked_store():
    checked = oauth_business.prepare(oauth_providers.SHOPIFY, {"shop": "https://MyStore.myshopify.com/"})
    url = _authorize("shopify", checked)
    q = _query(url)
    assert _base(url) == "https://mystore.myshopify.com/admin/oauth/authorize"
    assert q["scope"] == "write_content,write_products"
    assert "grant_options[]" not in q  # offline token, not per-user


def test_github_sends_the_person_to_the_app_installation_page():
    url = _authorize("github")
    assert url == "https://github.com/apps/automarket-ai/installations/new?state=S"


def test_webflow_asks_for_sites_and_cms():
    url = _authorize("webflow")
    assert _base(url) == "https://webflow.com/oauth/authorize"
    assert _query(url)["scope"] == "sites:read cms:read cms:write"


def test_a_host_template_without_checked_values_is_refused():
    with pytest.raises(OAuthFlowError):
        oauth_business.token_url(oauth_providers.SHOPIFY, {})


# ── Shopify: store address and signed callback ─────────────────────────────
@pytest.mark.parametrize(
    ("typed", "shop"),
    [
        ("mystore.myshopify.com", "mystore.myshopify.com"),
        ("https://my-store.myshopify.com/", "my-store.myshopify.com"),
        ("MyStore", "mystore.myshopify.com"),
        ("admin.shopify.com/store/mystore", "mystore.myshopify.com"),
    ],
)
def test_store_addresses_merchants_type_are_accepted(typed: str, shop: str):
    assert oauth_business.normalise_shop(typed) == shop


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "www.mystore.com",
        "mystore.myshopify.com.evil.com",
        "evil.com/mystore.myshopify.com",
        "mystore.myshopify.com:8443",
        "mystore.myshopify.com/admin",
        "user@mystore.myshopify.com",
        "-bad.myshopify.com",
        "my_store.myshopify.com",
        "a" * 61 + ".myshopify.com",
    ],
)
def test_anything_but_a_myshopify_address_is_refused(typed: str):
    with pytest.raises(InvalidInputError):
        oauth_business.normalise_shop(typed)


def test_shopify_hmac_matches_shopifys_documented_example():
    items = [
        ("code", "0907a61c0c8d55e99db179b68161bc00"),
        ("hmac", "700e2dadb827fcc8609e9d5ce208b2e9cdaab9df07390d2cbca10d7c328fc4bf"),
        ("shop", "some-shop.myshopify.com"),
        ("state", "0.6784241404160823"),
        ("timestamp", "1337178173"),
    ]
    assert oauth_business.shopify_hmac_valid(items, "hush")
    tampered = [(k, "other-shop.myshopify.com" if k == "shop" else v) for k, v in items]
    assert not oauth_business.shopify_hmac_valid(tampered, "hush")
    assert not oauth_business.shopify_hmac_valid(items, "wrong-secret")
    assert not oauth_business.shopify_hmac_valid([kv for kv in items if kv[0] != "hmac"], "hush")
    assert not oauth_business.shopify_hmac_valid(items + [("hmac", items[1][1])], "hush")


def _signed_shopify(shop: str, secret: str = "shopify-secret", **extra: str) -> list[tuple[str, str]]:
    items = [("code", "c0de"), ("shop", shop), ("state", "STATE"), ("timestamp", "1700000000"), *extra.items()]
    message = "&".join(f"{k}={v}" for k, v in sorted(items))
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return [*items, ("hmac", digest)]


def test_a_shopify_callback_must_be_signed_and_for_the_checked_store():
    provider = oauth_providers.SHOPIFY
    checked = {"shop": "mystore.myshopify.com"}
    oauth_business.verify_callback(provider, _signed_shopify("mystore.myshopify.com", host="YWRtaW4="), checked)
    with pytest.raises(OAuthFlowError, match="could not be verified"):
        oauth_business.verify_callback(provider, _signed_shopify("mystore.myshopify.com", "forged"), checked)
    with pytest.raises(OAuthFlowError, match="different store"):
        oauth_business.verify_callback(provider, _signed_shopify("other.myshopify.com"), checked)


def test_the_shopify_exchange_asks_for_an_expiring_offline_token(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={
            "access_token": "shpat_x", "refresh_token": "shprt_y", "expires_in": 3600,
            "scope": "write_content,write_products",
        }),
    )
    tokens = oauth_business.exchange(
        oauth_providers.SHOPIFY, code="c0de", verifier="", vars_={"shop": "mystore.myshopify.com"},
        params={}, now=1000.0,
    )
    assert str(seen[0].url) == "https://mystore.myshopify.com/admin/oauth/access_token"
    assert _form(seen[0]) == {
        "client_id": "shopify-client", "client_secret": "shopify-secret", "code": "c0de", "expiring": "1",
    }
    assert tokens.refresh_token == "shprt_y" and tokens.expires_at == 4600.0
    assert tokens.scopes == ("write_content", "write_products")
    assert tokens.extra == {"storeDomain": "mystore.myshopify.com"}


# ── Code exchanges ─────────────────────────────────────────────────────────
def test_klaviyo_exchange_uses_basic_auth_and_the_verifier(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={
            "access_token": "kat", "refresh_token": "krt", "expires_in": 3600,
            "scope": "accounts:read templates:read templates:write",
        }),
    )
    tokens = oauth_business.exchange(oauth_providers.KLAVIYO, code="c", verifier="v" * 43, vars_={}, params={})
    request = seen[0]
    assert str(request.url) == "https://a.klaviyo.com/oauth/token"
    expected = "Basic " + base64.b64encode(b"klaviyo-client:klaviyo-secret").decode()
    assert request.headers["authorization"] == expected
    form = _form(request)
    assert form["code_verifier"] == "v" * 43 and form["grant_type"] == "authorization_code"
    assert "client_secret" not in form
    assert tokens.refresh_token == "krt"


def test_slack_names_the_workspace_and_refuses_ok_false(monkeypatch):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={
            "ok": True, "access_token": "xoxb-1", "token_type": "bot",
            "scope": "chat:write,chat:write.public,channels:read",
            "team": {"id": "T1", "name": "Acme"},
        }),
    )
    tokens = oauth_business.exchange(oauth_providers.SLACK, code="c", verifier="", vars_={}, params={})
    assert tokens.account == "Acme" and tokens.extra == {"slackTeamId": "T1"}
    assert tokens.refresh_token == "" and tokens.expires_at == 0.0
    assert oauth_flow.missing_scopes(registry.get_spec("slack"), tokens.scopes) == []

    _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_code"}),
    )
    with pytest.raises(OAuthFlowError, match="did not accept"):
        oauth_business.exchange(oauth_providers.SLACK, code="c", verifier="", vars_={}, params={})


def test_hubspot_keeps_the_hub_id_from_the_token_response(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={
            "access_token": "hat", "refresh_token": "hrt", "expires_in": 1800, "hub_id": 4242,
            "scopes": ["oauth", "crm.objects.deals.read", "crm.objects.contacts.read"],
        }),
    )
    tokens = oauth_business.exchange(oauth_providers.HUBSPOT, code="c", verifier="", vars_={}, params={})
    assert str(seen[0].url) == "https://api.hubapi.com/oauth/v3/token"
    assert _form(seen[0])["client_secret"] == "hubspot-secret"
    assert tokens.extra == {"hubId": "4242"}
    assert oauth_flow.missing_scopes(registry.get_spec("hubspot"), tokens.scopes) == []


def test_salesforce_keeps_the_instance_and_sandbox_login(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "00D!x", "refresh_token": "5Aep", "scope": "api refresh_token",
            "instance_url": "https://acme--uat.sandbox.my.salesforce.com",
        })

    seen = _mock_client(monkeypatch, "app.services.oauth_business.httpx.Client", handler)
    tokens = oauth_business.exchange(
        oauth_providers.SALESFORCE, code="c", verifier="v" * 43,
        vars_={"login_host": "test.salesforce.com"}, params={}, now=1000.0,
    )
    assert str(seen[0].url) == "https://test.salesforce.com/services/oauth2/token"
    assert _form(seen[0])["code_verifier"] == "v" * 43
    assert tokens.extra == {
        "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com",
        "loginUrl": "https://test.salesforce.com",
    }
    # No lifetime is stated; the stored token is trusted for 15 minutes only.
    assert tokens.expires_at == 1900.0


@pytest.mark.parametrize(
    "instance",
    ["https://evil.example.com", "http://acme.my.salesforce.com", "https://acme.my.salesforce.com.evil.io",
     "https://acme.my.salesforce.com:8443", ""],
)
def test_salesforce_refuses_an_instance_that_is_not_salesforce(monkeypatch, instance: str):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "t", "refresh_token": "r", "instance_url": instance}),
    )
    with pytest.raises(OAuthFlowError):
        oauth_business.exchange(
            oauth_providers.SALESFORCE, code="c", verifier="", vars_={"login_host": "login.salesforce.com"}, params={}
        )


def test_mailchimp_reads_the_data_centre_from_the_metadata_endpoint(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "mc-token", "expires_in": 0, "scope": None})
        assert request.headers["authorization"] == "OAuth mc-token"
        return httpx.Response(200, json={"dc": "us21", "accountname": "Acme Ltd", "login": {"email": "a@b.c"}})

    seen = _mock_client(monkeypatch, "app.services.oauth_business.httpx.Client", handler)
    tokens = oauth_business.exchange(oauth_providers.MAILCHIMP, code="c", verifier="", vars_={}, params={})
    assert str(seen[1].url) == "https://login.mailchimp.com/oauth2/metadata"
    assert tokens.extra == {"dataCentre": "us21"} and tokens.account == "Acme Ltd"
    assert tokens.expires_at == 0.0 and tokens.refresh_token == ""


def test_mailchimp_refuses_a_bad_data_centre(monkeypatch):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "mc-token"})
        return httpx.Response(200, json={"dc": "evil.com/x"})

    _mock_client(monkeypatch, "app.services.oauth_business.httpx.Client", handler)
    with pytest.raises(OAuthFlowError, match="server"):
        oauth_business.exchange(oauth_providers.MAILCHIMP, code="c", verifier="", vars_={}, params={})


def _github_handler(installations: list[dict]):  # noqa: ANN202
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(200, json={"access_token": "ghu_user", "refresh_token": "ghr_x", "expires_in": 28800})
        assert request.headers["authorization"] == "Bearer ghu_user"
        return httpx.Response(200, json={"installations": installations})

    return handler


def test_a_github_install_is_checked_against_the_person_who_installed_it(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        _github_handler([{"id": 99, "account": {"login": "acme"}}]),
    )
    tokens = oauth_business.exchange(
        oauth_providers.GITHUB, code="c", verifier="", vars_={}, params={"installation_id": "99"}
    )
    assert str(seen[0].url) == "https://github.com/login/oauth/access_token"
    assert _form(seen[0])["client_id"] == "github-client"
    assert seen[1].url.path == "/user/installations"
    assert tokens.extra == {"installationId": "99"} and tokens.account == "acme"
    # The person's own GitHub token is not kept.
    assert tokens.access_token == oauth_business.GITHUB_INSTALLATION_MARKER
    assert tokens.refresh_token == "" and tokens.expires_at == 0.0


@pytest.mark.parametrize("installation", ["12345", "", "99; drop"])
def test_a_forged_or_missing_installation_id_is_refused(monkeypatch, installation: str):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        _github_handler([{"id": 99, "account": {"login": "acme"}}]),
    )
    with pytest.raises(OAuthFlowError):
        oauth_business.exchange(
            oauth_providers.GITHUB, code="c", verifier="", vars_={}, params={"installation_id": installation}
        )


def test_bitbucket_exchange_uses_basic_auth_and_ignores_scope_naming(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={
            "access_token": "bat", "refresh_token": "brt", "expires_in": 7200, "scopes": "repository:admin",
        }),
    )
    tokens = oauth_business.exchange(oauth_providers.BITBUCKET, code="c", verifier="", vars_={}, params={})
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert tokens.scopes == ()
    assert oauth_flow.missing_scopes(registry.get_spec("bitbucket"), tokens.scopes) == []


# ── The callback, end to end through the shared flow ───────────────────────
class _Store:
    def __init__(self, context: dict) -> None:
        self.context = context

    def redeem(self, db, payload):  # noqa: ANN001, ANN201
        return oauth_flow.RedeemedState(verifier_encrypted="", context=dict(self.context))


class _Db:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@pytest.fixture
def seams(monkeypatch):  # noqa: ANN001, ANN201
    calls: dict = {"written": [], "store": _Store({"vars": {"shop": "mystore.myshopify.com"}})}
    monkeypatch.setattr("app.db.session.bind_tenant", lambda db, tenant: None)
    monkeypatch.setattr(oauth_flow, "_redeem_state", lambda db, p: calls["store"].redeem(db, p))

    class Org:
        id = "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr(oauth_flow, "_load_actor", lambda db, payload: (Org(), "USER"))
    monkeypatch.setattr("app.services.connectors.get_record", lambda db, tenant_id, slug: f"record:{slug}")
    monkeypatch.setattr(
        oauth_flow,
        "write_platform_tokens",
        lambda db, *, org, record, spec, tokens: calls["written"].append((record, tokens)),
    )
    monkeypatch.setattr(oauth_flow, "_auto_choose", lambda db, **kw: "choose")
    return calls


def _state(slug: str, provider: str) -> str:
    return oauth_flow.sign_state(
        StatePayload(
            tenant_id="11111111-1111-1111-1111-111111111111",
            user_id="22222222-2222-2222-2222-222222222222",
            slug=slug, provider=provider, nonce="n", return_path="/connectors",
            expires_at=int(time.time()) + 600,
        )
    )


def test_a_signed_shopify_callback_stores_the_store_from_the_state(seams, monkeypatch):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.services.oauth_business.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "shpat", "refresh_token": "r", "expires_in": 3600,
                                            "scope": "write_content,write_products"}),
    )
    state = _state("shopify", "shopify")
    raw = [(k, state if k == "state" else v) for k, v in _signed_shopify("mystore.myshopify.com")]
    # Re-sign with the real state value.
    message = "&".join(f"{k}={v}" for k, v in sorted(kv for kv in raw if kv[0] != "hmac"))
    digest = hmac.new(b"shopify-secret", message.encode(), hashlib.sha256).hexdigest()
    raw = [kv for kv in raw if kv[0] != "hmac"] + [("hmac", digest)]
    params = {k: v for k, v in raw}
    target = oauth_flow.handle_callback(_Db(), "shopify", params, raw=raw)
    assert _query(target) == {"oauth_choose": "shopify"}
    record, tokens = seams["written"][0]
    assert record == "record:shopify" and tokens.extra == {"storeDomain": "mystore.myshopify.com"}


def test_an_unsigned_shopify_callback_never_reaches_the_token_endpoint(seams, monkeypatch):  # noqa: ANN001
    seen = _mock_client(monkeypatch, "app.services.oauth_business.httpx.Client", lambda r: httpx.Response(500))
    state = _state("shopify", "shopify")
    params = {"state": state, "code": "c", "shop": "mystore.myshopify.com", "hmac": "00" * 32}
    target = oauth_flow.handle_callback(_Db(), "shopify", params, raw=list(params.items()))
    # The vendor helper names the failure: a signature that does not verify
    # is an invalid link (contract 3.8), never the exception text.
    assert _query(target)["oauth_error"] == "link_invalid"
    assert seen == [] and seams["written"] == []


# ── The choose step, per connector ─────────────────────────────────────────
def test_slack_channels_are_listed_with_their_ids():
    pages = iter([
        {"ok": True, "channels": [{"id": "C2", "name": "marketing", "num_members": 4},
                                  {"id": "C9", "name": "old", "is_archived": True}],
         "response_metadata": {"next_cursor": "abc"}},
        {"ok": True, "channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": ""}},
    ])
    connector = _signed_in("slack")
    seen = _serve(connector, lambda r: httpx.Response(200, json=next(pages)))
    choices = connector.list_oauth_choices()
    assert [(c.label, c.values) for c in choices] == [
        ("#general", {"defaultChannel": "C1"}),
        ("#marketing", {"defaultChannel": "C2"}),
    ]
    assert seen[0].headers["authorization"] == "Bearer at-stored"
    assert seen[1].url.params["cursor"] == "abc"


def test_hubspot_offers_each_pipelines_won_stage():
    from app.connectors.hubspot.connector import parse_won_stages

    data = {"results": [
        {"id": "default", "label": "Sales Pipeline", "stages": [
            {"id": "appointmentscheduled", "label": "Appointment", "metadata": {"isClosed": "false", "probability": "0.2"}},
            {"id": "closedwon", "label": "Closed won", "metadata": {"isClosed": "true", "probability": "1.0"}},
            {"id": "closedlost", "label": "Closed lost", "metadata": {"isClosed": "true", "probability": "0.0"}},
        ]},
        {"id": "p2", "label": "Renewals", "archived": True, "stages": [
            {"id": "won2", "label": "Won", "metadata": {"isClosed": "true", "probability": "1.0"}},
        ]},
    ]}
    choices = parse_won_stages(data, hub_id="4242")
    assert [(c.label, c.values) for c in choices] == [
        ("Closed won — Sales Pipeline", {"wonStageId": "closedwon", "hubId": "4242"}),
    ]


def test_salesforce_has_one_org_and_needs_no_pick():
    connector = _signed_in("salesforce", instanceUrl="https://acme.my.salesforce.com", loginUrl="https://login.salesforce.com")
    _serve(connector, lambda r: httpx.Response(200, json={"organization_id": "00D1", "preferred_username": "a@acme.com"}))
    choices = connector.list_oauth_choices()
    assert len(choices) == 1 and choices[0].label == "acme.my.salesforce.com"
    assert choices[0].detail == "a@acme.com"


def test_mailchimp_audiences_bring_their_sender_defaults():
    connector = _signed_in("mailchimp", dataCentre="us21")
    assert connector.base_url == "https://us21.api.mailchimp.com/3.0"
    seen = _serve(connector, lambda r: httpx.Response(200, json={"lists": [
        {"id": "a1", "name": "Newsletter", "stats": {"member_count": 1200},
         "campaign_defaults": {"from_name": "Acme", "from_email": "news@acme.com"}},
    ]}))
    choices = connector.list_oauth_choices()
    assert choices[0].values == {"audienceId": "a1", "fromName": "Acme", "replyTo": "news@acme.com"}
    assert seen[0].headers["authorization"] == "Bearer at-stored"


def test_a_pasted_mailchimp_key_still_uses_basic_auth():
    connector = _connector("mailchimp", apiKey="0123456789abcdef" + "0123456789abcdef-us7")
    assert connector.base_url == "https://us7.api.mailchimp.com/3.0"
    assert connector.auth_headers()["Authorization"].startswith("Basic ")


def test_klaviyo_lists_its_one_account_and_signs_with_bearer():
    connector = _signed_in("klaviyo", **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3000), "refreshToken": "krt"})
    seen = _serve(connector, lambda r: httpx.Response(200, json={"data": [
        {"id": "AbC", "attributes": {"contact_information": {"organization_name": "Acme"}}}]}))
    choices = connector.list_oauth_choices()
    assert [c.label for c in choices] == ["Acme"]
    assert seen[0].headers["authorization"] == "Bearer at-stored"
    assert seen[0].headers["revision"]


def test_shopify_blogs_and_a_store_with_none():
    from app.connectors.shopify.connector import parse_blogs

    assert [c.values for c in parse_blogs({"blogs": [{"id": 7, "title": "News", "handle": "news"}]})] == [
        {"blogId": "7"}
    ]
    empty = parse_blogs({"blogs": []}, shop_name="Acme")
    assert len(empty) == 1 and empty[0].values == {"blogId": ""} and empty[0].label == "Acme"


def test_shopify_signed_in_uses_the_stored_token_header():
    connector = _signed_in("shopify", storeDomain="mystore.myshopify.com")
    assert connector.auth_headers()["X-Shopify-Access-Token"] == "at-stored"
    assert connector.base_url.startswith("https://mystore.myshopify.com/admin/api/")
    pasted = _connector("shopify", storeDomain="mystore.myshopify.com", adminToken="shpat_own")
    assert pasted.auth_headers()["X-Shopify-Access-Token"] == "shpat_own"


def test_github_repositories_of_the_installation():
    from app.connectors.github.connector import parse_installation_repositories

    choices = parse_installation_repositories({"repositories": [
        {"full_name": "acme/site", "default_branch": "main", "private": True},
        {"full_name": "acme/old", "archived": True},
    ]})
    assert [(c.id, c.values) for c in choices] == [("acme/site", {"repository": "acme/site", "branch": "main"})]


def test_bitbucket_repositories_with_their_main_branch():
    from app.connectors.bitbucket.connector import parse_repositories

    choices = parse_repositories({"values": [
        {"full_name": "acme/site", "mainbranch": {"name": "master"}, "workspace": {"name": "Acme"}},
    ]})
    assert choices[0].values == {"repository": "acme/site", "branch": "master"}
    assert "Workspace Acme" in choices[0].detail


def test_webflow_pairs_each_site_with_its_article_collection():
    from app.connectors.webflow.connector import site_choices

    blog = {"id": "col1", "displayName": "Blog Posts", "fields": [
        {"slug": "name", "type": "PlainText"},
        {"slug": "summary", "type": "RichText", "displayName": "Summary"},
        {"slug": "post-body", "type": "RichText", "displayName": "Post Body"},
    ]}
    authors = {"id": "col2", "displayName": "Authors", "fields": [{"slug": "name", "type": "PlainText"}]}
    choices = site_choices({"id": "site1", "displayName": "Acme"}, [blog, authors])
    assert [c.values for c in choices] == [
        {"siteId": "site1", "articlesCollectionId": "col1", "articlesBodyField": "post-body"}
    ]
    bare = site_choices({"id": "site2", "displayName": "Landing"}, [authors])
    assert bare[0].values == {"siteId": "site2", "articlesCollectionId": ""}


def test_webflow_choices_walk_sites_and_collections():
    connector = _signed_in("webflow")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sites"):
            return httpx.Response(200, json={"sites": [{"id": "s1", "displayName": "Acme"}]})
        if path.endswith("/sites/s1/collections"):
            return httpx.Response(200, json={"collections": [{"id": "c1"}]})
        return httpx.Response(200, json={"id": "c1", "displayName": "Blog", "fields": [
            {"slug": "post-body", "type": "RichText"}]})

    seen = _serve(connector, handler)
    assert [c.id for c in connector.list_oauth_choices()] == ["s1:c1"]
    assert all(r.headers["authorization"] == "Bearer at-stored" for r in seen)


# ── Refreshing ─────────────────────────────────────────────────────────────
def test_hubspot_refreshes_with_the_platform_app(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "fresh", "expires_in": 1800}),
    )
    connector = _connector("hubspot", **{OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "hrt"})
    assert connector.access_token == "fresh"
    assert str(seen[0].url) == "https://api.hubapi.com/oauth/v3/token"
    form = _form(seen[0])
    assert form["client_id"] == "hubspot-client" and form["client_secret"] == "hubspot-secret"
    assert form["refresh_token"] == "hrt" and form["grant_type"] == "refresh_token"


def test_a_pasted_hubspot_private_app_token_needs_no_refresh(monkeypatch):  # noqa: ANN001
    seen = _mock_client(monkeypatch, "app.connectors.base.oauth.httpx.Client", lambda r: httpx.Response(500))
    connector = _connector("hubspot", accessToken="pat-na1-x", wonStageId="closedwon")
    assert connector.auth_headers()["Authorization"] == "Bearer pat-na1-x"
    assert seen == []


def test_klaviyo_refresh_authenticates_with_basic(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "k2", "expires_in": 3600}),
    )
    connector = _connector("klaviyo", **{OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "krt"})
    assert connector.access_token == "k2"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert "client_secret" not in _form(seen[0])


def test_rotated_bitbucket_and_shopify_refresh_tokens_are_written_back(monkeypatch):  # noqa: ANN001
    saved: list[dict] = []
    monkeypatch.setattr(
        "app.services.oauth_tokens.save_rotated",
        lambda org, slug, **kw: saved.append({"slug": slug, **kw}),
    )
    _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600}),
    )
    bitbucket = _connector("bitbucket", **{OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "old-rt"})
    assert bitbucket.auth_headers()["Authorization"] == "Bearer new-at"
    shopify = _connector(
        "shopify", **{OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "old-rt", "storeDomain": "mystore.myshopify.com"}
    )
    assert shopify.token_url == "https://mystore.myshopify.com/admin/oauth/access_token"
    assert shopify.access_token == "new-at"
    assert [(s["slug"], s["used_refresh"], s["refresh"]) for s in saved] == [
        ("bitbucket", "old-rt", "new-rt"),
        ("shopify", "old-rt", "new-rt"),
    ]


def test_a_shopify_store_address_that_is_not_myshopify_never_gets_a_refresh():
    connector = _connector("shopify", storeDomain="evil.example.com", refreshToken="r")
    assert connector.token_url == ""


def test_salesforce_refreshes_on_the_sandbox_host(monkeypatch):  # noqa: ANN001
    # The login address is customer-typed, so the refresh goes through the
    # egress guard (covered end to end in test_connector_egress.py). Here the
    # guarded client is a MockTransport and DNS answers with a public address.
    import socket

    from app.core import egress

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "sf2"})

    monkeypatch.setattr(
        egress, "pinned_client",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False),
    )
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    connector = _connector(
        "salesforce",
        **{OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "refreshToken": "5Aep", "loginUrl": "https://test.salesforce.com",
           "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com"},
    )
    assert connector.access_token == "sf2"
    assert str(seen[0].url) == "https://test.salesforce.com/services/oauth2/token"


# ── GitHub App installation tokens ─────────────────────────────────────────
def test_the_app_jwt_is_short_lived_and_names_the_app():
    import jwt

    from app.connectors.github.connector import app_jwt

    token = app_jwt(now=1_700_000_000)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims == {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "123456"}
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_a_single_line_private_key_is_accepted(monkeypatch):  # noqa: ANN001
    from app.connectors.github.connector import app_jwt, app_private_key

    monkeypatch.setattr(settings, "github_app_private_key", _RSA_PEM.replace("\n", "\\n"))
    assert app_private_key() == _RSA_PEM.strip()
    assert app_jwt()


def test_an_installed_github_connection_uses_installation_tokens(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.github.connector.httpx.Client",
        lambda r: httpx.Response(201, json={
            "token": "ghs_install", "expires_at": "2030-01-01T00:00:00Z",
            "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        }),
    )
    connector = _signed_in(
        "github", accessToken=oauth_business.GITHUB_INSTALLATION_MARKER, installationId="99",
        repository="acme/site", branch="main",
    )
    assert connector.auth_headers()["Authorization"] == "Bearer ghs_install"
    assert connector.auth_headers()["Authorization"] == "Bearer ghs_install"
    assert len(seen) == 1  # cached for the hour
    assert seen[0].url.path == "/app/installations/99/access_tokens"
    assert seen[0].headers["authorization"].startswith("Bearer ey")

    api_calls = _serve(connector, lambda r: httpx.Response(200, json={"full_name": "acme/site"}))
    assert connector.check_health().ok
    assert api_calls[0].url.path == "/repos/acme/site"


def test_an_installation_without_write_permissions_is_unhealthy(monkeypatch):  # noqa: ANN001
    _mock_client(
        monkeypatch,
        "app.connectors.github.connector.httpx.Client",
        lambda r: httpx.Response(201, json={"token": "ghs_x", "permissions": {"contents": "read"}}),
    )
    connector = _signed_in("github", installationId="99", repository="acme/site", branch="main")
    _serve(connector, lambda r: httpx.Response(200, json={"full_name": "acme/site"}))
    report = connector.check_health()
    assert not report.ok and "write access" in report.detail


def test_an_uninstalled_app_says_so(monkeypatch):  # noqa: ANN001
    _mock_client(monkeypatch, "app.connectors.github.connector.httpx.Client", lambda r: httpx.Response(404))
    connector = _signed_in("github", installationId="99", repository="acme/site", branch="main")
    with pytest.raises(ConnectorError, match="Install it again"):
        connector.installation_token()


def test_a_pasted_github_token_is_used_as_before():
    connector = _connector("github", token="github_pat_x", repository="acme/site", branch="main")
    assert connector.auth_headers()["Authorization"] == "Bearer github_pat_x"


def test_installation_tokens_need_the_app_settings(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "github_app_id", "")
    connector = _signed_in("github", installationId="99", repository="acme/site", branch="main")
    with pytest.raises(ConnectorConfigError, match="no longer set up"):
        connector.installation_token()


# ── The included Google keys (PageSpeed Insights, Places) ──────────────────
def _record(slug: str, **kw):  # noqa: ANN003, ANN202
    from app.models.connector import ConnectorRecord

    return ConnectorRecord(slug=slug, name=slug, category="x", credential_hints=kw.pop("hints", {}), **kw)


@pytest.mark.parametrize(("slug", "setting"), [
    ("pagespeed_insights", "platform_pagespeed_api_key"),
    ("google_places", "platform_google_places_api_key"),
])
def test_the_included_access_is_offered_only_when_the_platform_has_a_key(monkeypatch, slug: str, setting: str):  # noqa: ANN001
    from app.services.views import connect_guide

    guide = connect_guide(_record(slug, connected=False))
    assert guide.mode == "key" and not guide.platform_key_available
    monkeypatch.setattr(settings, setting, "AIzaPlatformKey")
    guide = connect_guide(_record(slug, connected=False))
    assert guide.mode == "key" and guide.platform_key_available and guide.platform_key_summary
    # The guide for the workspace's own key stays, under Advanced.
    assert guide.steps and guide.key_field == "apiKey"


def test_pagespeed_with_the_included_key_needs_no_key_of_its_own(monkeypatch):  # noqa: ANN001
    from app.services.connectors import platform_supplied

    monkeypatch.setattr(settings, "platform_pagespeed_api_key", "AIzaPlatformKey")
    spec = registry.get_spec("pagespeed_insights")
    values = {KEY_SOURCE_KEY: PLATFORM_KEY_SOURCE, "strategy": "mobile"}
    supplied = platform_supplied(spec, values)
    assert supplied == frozenset({"apiKey"})
    validate(spec.fields, values, oauth_done=False, platform_supplied=supplied)

    connector = _connector("pagespeed_insights", **values)
    seen = _serve(connector, lambda r: httpx.Response(200, json={"loadingExperience": {}}))
    assert connector.check_health().ok
    assert seen[0].url.params["key"] == "AIzaPlatformKey"


def test_places_sends_the_included_key_and_an_own_key_wins(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "platform_google_places_api_key", "AIzaPlaces")
    included = _connector("google_places", **{KEY_SOURCE_KEY: PLATFORM_KEY_SOURCE, "businessName": "Acme"})
    assert included.auth_headers()["X-Goog-Api-Key"] == "AIzaPlaces"
    own = _connector("google_places", apiKey="AIzaOwn", businessName="Acme", **{KEY_SOURCE_KEY: PLATFORM_KEY_SOURCE})
    assert own.auth_headers()["X-Goog-Api-Key"] == "AIzaOwn"


def test_the_included_key_withdrawn_later_says_so(monkeypatch):  # noqa: ANN001
    connector = _connector("pagespeed_insights", **{KEY_SOURCE_KEY: PLATFORM_KEY_SOURCE})
    with pytest.raises(ConnectorConfigError, match="no longer offered"):
        _ = connector.api_key
    from app.services.connectors import platform_supplied

    assert platform_supplied(registry.get_spec("pagespeed_insights"), {KEY_SOURCE_KEY: PLATFORM_KEY_SOURCE}) == frozenset()


def test_without_the_marker_a_key_is_still_required():
    with pytest.raises(ConnectorConfigError):
        resolve_key(Credentials(values={}), registry.get_spec("google_places").platform_key)


# ── What the connect screen shows ──────────────────────────────────────────
BANNED = re.compile(
    r"client id|client secret|consumer key|refresh token|developer token|redirect uri|private key|"
    r"api key|access token|\btoken\b|secret|xoxb|shpat",
    re.I,
)


@pytest.mark.parametrize("slug", BUSINESS)
def test_no_business_default_path_shows_developer_credentials(slug: str):
    from app.services.views import connect_guide

    spec = registry.get_spec(slug)
    guide = connect_guide(_record(slug, connected=False))
    assert guide.mode == "sign_in" and guide.sign_in_available
    shown = [f for f in spec.fields if f.key not in guide.sign_in_hidden_fields and f.required and not f.default]
    for item in shown:
        assert not BANNED.search(item.label), (slug, item.label)
    for text in (guide.button_label, guide.access_summary, *(i.label for i in guide.sign_in_inputs),
                 *(i.help_text for i in guide.sign_in_inputs)):
        assert not BANNED.search(text), (slug, text)
    assert guide.access_summary


def test_branded_buttons_and_inputs():
    from app.services.views import connect_guide

    def guide(slug: str, **kw):  # noqa: ANN003, ANN202
        return connect_guide(_record(slug, connected=False, **kw))

    assert guide("slack").button_label == "Add to Slack"
    assert guide("github").button_label == "Install on GitHub"
    assert guide("hubspot").button_label == "Sign in with HubSpot"
    shop = guide("shopify", hints={"storeDomain": "mystore.myshopify.com"}).sign_in_inputs
    assert [(i.key, i.kind, i.value) for i in shop] == [("shop", "text", "mystore.myshopify.com")]
    sandbox = guide("salesforce", hints={"loginUrl": "https://test.salesforce.com"}).sign_in_inputs
    assert [(i.key, i.kind, i.value) for i in sandbox] == [("sandbox", "toggle", "true")]
    for slug in ("salesforce", "shopify", "slack", "github"):
        hidden = set(guide(slug).sign_in_hidden_fields)
        assert {"instanceUrl", "loginUrl", "clientId", "clientSecret"} <= hidden if slug == "salesforce" else True
    assert {"storeDomain", "adminToken", "blogId"} <= set(guide("shopify").sign_in_hidden_fields)


def test_mailchimp_and_klaviyo_keep_their_key_guide_under_advanced(monkeypatch):  # noqa: ANN001
    from app.services.views import connect_guide

    for slug in ("mailchimp", "klaviyo"):
        guide = connect_guide(_record(slug, connected=False))
        assert guide.mode == "sign_in" and guide.steps and guide.key_field == "apiKey"
        assert guide.manual_label == "Advanced: use an API key instead"
    monkeypatch.setattr(settings, "oauth_mailchimp_client_id", "")
    fallback = connect_guide(_record("mailchimp", connected=False))
    assert fallback.mode == "key" and fallback.steps


def test_zapier_and_adobe_have_plain_steps_but_stay_advanced():
    from app.services.views import connect_guide

    zapier = connect_guide(_record("zapier_webhooks", connected=False))
    assert zapier.mode == "advanced" and any("Catch Hook" in s for s in zapier.steps)
    assert zapier.key_field == "webhookUrl"
    adobe = connect_guide(_record("adobe_analytics", connected=False))
    assert adobe.mode == "advanced" and any("OAuth Server-to-Server" in s for s in adobe.steps)


def test_signed_in_values_validate_without_pasted_credentials():
    from app.services.connectors import platform_supplied

    cases = {
        "mailchimp": {"audienceId": "a1", "fromName": "A", "replyTo": "a@b.c", "dataCentre": "us21"},
        "klaviyo": {},
        "shopify": {"storeDomain": "mystore.myshopify.com"},
        "github": {"repository": "acme/site", "branch": "main", "installationId": "9"},
        "bitbucket": {"repository": "acme/site", "branch": "main"},
        "webflow": {"siteId": "s1"},
        "hubspot": {"wonStageId": "closedwon"},
        "slack": {"defaultChannel": "C1", "accessToken": "xoxb-1"},
        "salesforce": {"instanceUrl": "https://acme.my.salesforce.com", "refreshToken": "r"},
    }
    for slug, extra in cases.items():
        spec = registry.get_spec(slug)
        values = {OAUTH_SOURCE_KEY: PLATFORM_SOURCE, "accessToken": "t", **extra}
        validate(spec.fields, values, oauth_done=True, platform_supplied=platform_supplied(spec, values))


def test_admin_overview_lists_the_business_providers():
    statuses = {s.name: s for s in oauth_flow.provider_statuses()}
    for name in BUSINESS:
        assert statuses[name].configured
        assert statuses[name].redirect_uri == f"{API}/api/v1/oauth/callback/{name}"
    github = {c["slug"]: c for c in statuses["github"].connectors}["github"]
    assert github["available"] and github["missing"] == []
