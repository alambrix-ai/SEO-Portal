"""Pre-launch connector fixes: current vendor API versions, the Microsoft Ads
sandbox sign-in, the overridable TikTok sandbox address, and the added
Pinterest and Klaviyo sign-in permissions."""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import oauth_providers, registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import (
    ACCESS_TOKEN_EXPIRY_KEY,
    OAUTH_SOURCE_KEY,
    PLATFORM_SOURCE,
    Credentials,
)
from app.connectors.base.meta_graph import GRAPH_API_VERSION, effective_version
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.services import oauth_flow
from app.services.oauth_flow import StatePayload

LIVE_SCOPE = "https://ads.microsoft.com/msads.manage"
SANDBOX_SCOPE = "https://si.ads.microsoft.com/msads.manage"
RESIGN = "Sign in again to grant the new permission."


@pytest.fixture(autouse=True)
def platform(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(settings, "public_base_url", "https://console.example.com")
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.com")
    for name in ("microsoft", "pinterest", "klaviyo", "meta"):
        monkeypatch.setattr(settings, f"oauth_{name}_client_id", f"{name}-app")
        monkeypatch.setattr(settings, f"oauth_{name}_client_secret", f"{name}-secret")
    monkeypatch.setattr(settings, "microsoft_ads_developer_token", "platform-ms-dev-token")


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _connector(slug: str, values: dict[str, str]):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values), org_id="")


def _signed_in(slug: str, **values: str):  # noqa: ANN003, ANN202
    base = {
        OAUTH_SOURCE_KEY: PLATFORM_SOURCE,
        "accessToken": "stored-token",
        ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) + 3600),
    }
    base.update(values)
    return _connector(slug, base)


def _wire(connector, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    connector._client = httpx.Client(
        base_url=connector.base_url, transport=httpx.MockTransport(recording)
    )
    return seen


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


def _step(connector, key: str):  # noqa: ANN001, ANN202
    return next(s for s in connector.verify_steps() if s.key == key)


# ── 1. Meta: one current Graph API version everywhere ──────────────────────
def test_every_meta_caller_uses_the_shared_current_graph_version():
    assert GRAPH_API_VERSION == "v26.0"
    meta = oauth_providers.META
    assert meta.authorize_url == f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"
    assert meta.token_url == f"https://graph.facebook.com/{GRAPH_API_VERSION}/oauth/access_token"
    ads = _connector("meta_ads", {"accessToken": "t", "adAccountId": "act_1"})
    assert ads.base_url == f"https://graph.facebook.com/{GRAPH_API_VERSION}"
    for slug in ("facebook_pages", "instagram_business"):
        field = next(f for f in registry.get_spec(slug).fields if f.key == "apiVersion")
        assert field.default == GRAPH_API_VERSION


@pytest.mark.parametrize(
    ("saved", "used"),
    [("", "v26.0"), ("v23.0", "v26.0"), ("23.0", "v26.0"), ("v24.0", "v24.0"), ("v27.0", "v27.0")],
)
def test_the_old_form_default_reads_as_the_current_version(saved: str, used: str):
    assert effective_version(saved).lstrip("v") == used.lstrip("v")
    page = _connector("facebook_pages", {"accessToken": "t", "pageId": "1", "apiVersion": saved})
    assert page.base_url == f"https://graph.facebook.com/{used}"


def test_meta_creatives_opt_out_of_the_v26_shop_destination_default():
    ads = _connector(
        "meta_ads",
        {"accessToken": "t", "adAccountId": "act_9", "pageId": "55", "finalUrl": "https://dealer.example/"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/{GRAPH_API_VERSION}/act_9/adcreatives"
        return httpx.Response(200, json={"id": "cr1"})

    seen = _wire(ads, handler)
    creative = ads._live_upload_creative(
        headline="Test drive", body_copy="Book now", call_to_action="Book a test drive",
        dimensions="1080x1080", audience_segment="all",
    )
    assert creative == "cr1"
    body = json.loads(seen[0].content)
    assert body["destination_spec"] == {"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"}
    assert body["object_story_spec"]["link_data"]["link"] == "https://dealer.example/"


def test_meta_sandbox_campaign_sends_the_v24_budget_sharing_flag():
    ads = _connector(
        "meta_ads", {"accessToken": "t", "adAccountId": "act_9", "accountEnvironment": "Sandbox"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "c1"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": "c1", "status": "PAUSED"})
        return httpx.Response(200, json={"success": True})

    seen = _wire(ads, handler)
    _step(ads, "paused_campaign_roundtrip").run()
    created = json.loads(seen[0].content)
    assert seen[0].url.path == f"/{GRAPH_API_VERSION}/act_9/campaigns"
    assert created["is_adset_budget_sharing_enabled"] is False
    assert created["status"] == "PAUSED"


# ── 2. Shopify and Klaviyo versions ────────────────────────────────────────
def test_shopify_calls_the_current_stable_admin_api():
    from app.connectors.shopify import connector as shopify

    assert shopify.API_VERSION == "2026-10"
    shop = _connector("shopify", {"storeDomain": "demo.myshopify.com", "accessToken": "t"})
    assert shop.base_url == "https://demo.myshopify.com/admin/api/2026-10"


@pytest.mark.parametrize(
    ("saved", "sent"),
    [("", "2026-07-15"), ("2025-07-15", "2026-07-15"), ("2024-10-15", "2024-10-15")],
)
def test_klaviyo_sends_the_current_revision_unless_one_was_chosen(saved: str, sent: str):
    from app.connectors.klaviyo.connector import DEFAULT_REVISION

    assert DEFAULT_REVISION == "2026-07-15"
    klaviyo = _connector("klaviyo", {"apiKey": "pk_" + "a" * 30, "revision": saved})
    assert klaviyo.auth_headers()["revision"] == sent


# ── 3. Microsoft Ads sandbox through the sign-in button ────────────────────
def test_microsoft_ads_offers_a_sandbox_toggle():
    binding = registry.get_spec("microsoft_ads").oauth
    [toggle] = binding.sign_in_inputs
    assert (toggle.key, toggle.kind, toggle.label) == ("sandbox", "toggle", "This is a sandbox account")
    assert toggle.field == "accountEnvironment" and toggle.value_when_on == "sandbox"
    assert binding.scopes == (LIVE_SCOPE,)


@pytest.mark.parametrize(("ticked", "scope"), [("true", SANDBOX_SCOPE), ("", LIVE_SCOPE)])
def test_the_authorize_url_asks_for_the_sandbox_scope_only_when_ticked(ticked: str, scope: str):
    spec = registry.get_spec("microsoft_ads")
    url = oauth_flow.build_authorize_url(
        oauth_providers.MICROSOFT, spec, state="S", code_challenge="C", inputs={"sandbox": ticked}
    )
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert set(_query(url)["scope"].split(" ")) == {"openid", "email", "offline_access", scope}


def test_starting_a_sandbox_sign_in_keeps_the_checked_answer_with_the_state(monkeypatch):  # noqa: ANN001
    kept: dict = {}

    def new_state(db, **kw):  # noqa: ANN001, ANN003, ANN202
        kept.update(kw.get("context") or {})
        return "STATE"

    monkeypatch.setattr("app.services.connectors.get_record", lambda db, tenant_id, slug: "rec")
    monkeypatch.setattr(oauth_flow, "_new_state", new_state)

    class Org:
        id = "11111111-1111-1111-1111-111111111111"

    result = oauth_flow.start(
        None, org=Org(), user=object(), slug="microsoft_ads", inputs={"sandbox": "on", "other": "x"}
    )
    assert kept == {"inputs": {"sandbox": "true"}}
    assert SANDBOX_SCOPE in _query(result.authorize_url)["scope"].split(" ")


def _callback(monkeypatch, context: dict, granted: str) -> tuple[str, list]:  # noqa: ANN001
    payload = StatePayload(
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        slug="microsoft_ads",
        provider="microsoft",
        nonce="n",
        return_path="/connectors",
        expires_at=int(time.time()) + 600,
    )

    class Db:
        def commit(self) -> None: ...
        def rollback(self) -> None: ...

    class Org:
        id = payload.tenant_id

    written: list = []
    monkeypatch.setattr("app.db.session.bind_tenant", lambda db, tenant: None)
    monkeypatch.setattr(oauth_flow, "_redeem_state", lambda db, p: oauth_flow.RedeemedState("", context))
    monkeypatch.setattr(oauth_flow, "_load_actor", lambda db, p: (Org(), "USER"))
    monkeypatch.setattr("app.services.connectors.get_record", lambda db, tenant_id, slug: "rec")
    monkeypatch.setattr(oauth_flow, "write_platform_tokens", lambda db, **kw: written.append(kw["tokens"]))
    monkeypatch.setattr(oauth_flow, "_auto_choose", lambda db, **kw: "connected")
    monkeypatch.setattr(
        oauth_flow,
        "exchange_code",
        lambda provider, *, code, verifier: oauth_flow.TokenSet(
            access_token="ms", refresh_token="msr", scopes=tuple(granted.split())
        ),
    )
    url = oauth_flow.handle_callback(Db(), "microsoft", {"state": oauth_flow.sign_state(payload), "code": "c"})
    return url, written


def test_a_sandbox_sign_in_is_stored_as_a_sandbox_connection(monkeypatch):  # noqa: ANN001
    url, written = _callback(
        monkeypatch, {"inputs": {"sandbox": "true"}}, f"{SANDBOX_SCOPE} offline_access openid"
    )
    assert _query(url)["connected"] == "microsoft_ads"
    assert written[0].extra == {"accountEnvironment": "sandbox"}


def test_a_live_sign_in_clears_an_earlier_sandbox_setting(monkeypatch):  # noqa: ANN001
    url, written = _callback(monkeypatch, {"inputs": {"sandbox": ""}}, f"{LIVE_SCOPE} offline_access")
    assert _query(url)["connected"] == "microsoft_ads"
    assert written[0].extra == {"accountEnvironment": ""}


def test_a_sandbox_sign_in_missing_the_sandbox_scope_is_refused(monkeypatch):  # noqa: ANN001
    url, written = _callback(monkeypatch, {"inputs": {"sandbox": "true"}}, "openid offline_access")
    assert _query(url)["oauth_error"] == "permissions_missing" and not written


def test_storing_the_sign_in_writes_and_clears_the_environment(monkeypatch):  # noqa: ANN001
    from app.models.connector import ConnectorRecord

    class Cipher:
        def encrypt_json(self, values: dict, *, context: str) -> str:
            return json.dumps(values)

    class Db:
        def flush(self) -> None: ...

    monkeypatch.setattr(oauth_flow.OrgCipher, "for_org", classmethod(lambda cls, org: Cipher()))
    monkeypatch.setattr(
        "app.services.connectors.load_credentials", lambda record, cipher: Credentials(values={})
    )
    spec = registry.get_spec("microsoft_ads")
    record = ConnectorRecord(
        slug="microsoft_ads", name="m", category="x", credential_hints={"campaignId": "7"},
        credentials_version=0,
    )
    tokens = oauth_flow.TokenSet(access_token="a", refresh_token="r", extra={"accountEnvironment": "sandbox"})
    oauth_flow.write_platform_tokens(Db(), org=object(), record=record, spec=spec, tokens=tokens)
    assert record.credential_hints["accountEnvironment"] == "sandbox"
    assert record.credential_hints["campaignId"] == "7"
    tokens = oauth_flow.TokenSet(access_token="a", refresh_token="r", extra={"accountEnvironment": ""})
    oauth_flow.write_platform_tokens(Db(), org=object(), record=record, spec=spec, tokens=tokens)
    assert "accountEnvironment" not in record.credential_hints
    assert record.credential_hints["campaignId"] == "7"


def test_signing_in_again_starts_with_the_toggle_as_saved():
    from app.services.views import _sign_in_input_value

    [toggle] = registry.get_spec("microsoft_ads").oauth.sign_in_inputs
    assert _sign_in_input_value(toggle, {"accountEnvironment": "sandbox"}) == "true"
    assert _sign_in_input_value(toggle, {"accountEnvironment": "Sandbox"}) == "true"
    assert _sign_in_input_value(toggle, {}) == ""
    # Salesforce's toggle is still read from its login address.
    [salesforce] = registry.get_spec("salesforce").oauth.sign_in_inputs
    assert _sign_in_input_value(salesforce, {"loginUrl": "https://test.salesforce.com"}) == "true"


def test_a_sandbox_sign_in_refreshes_with_the_sandbox_scope(monkeypatch):  # noqa: ANN001
    seen = _mock_client(
        monkeypatch,
        "app.connectors.base.oauth.httpx.Client",
        lambda r: httpx.Response(200, json={"access_token": "ms2", "refresh_token": "msr2", "expires_in": 3600}),
    )
    connector = _signed_in(
        "microsoft_ads", refreshToken="msr", accountEnvironment="sandbox",
        **{ACCESS_TOKEN_EXPIRY_KEY: str(int(time.time()) - 10)},
    )
    assert connector.access_token == "ms2"
    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert str(seen[0].url) == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert form["scope"] == f"{SANDBOX_SCOPE} offline_access"


def test_the_sandbox_account_picker_calls_the_sandbox_customer_host():
    connector = _signed_in("microsoft_ads", refreshToken="msr", accountEnvironment="sandbox")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "clientcenter.api.sandbox.bingads.microsoft.com"
        # Microsoft's universal sandbox token, not the platform's live one.
        assert request.headers["DeveloperToken"] == "BBD37VB98"
        if request.url.path.endswith("/User/Query"):
            return httpx.Response(200, json={"CustomerRoles": [{"CustomerId": "100"}]})
        return httpx.Response(200, json={"AccountsInfo": [{"Id": "200", "Name": "Sandbox search"}]})

    seen = _wire(connector, handler)
    [choice] = connector.list_oauth_choices()
    assert choice.values == {"customerId": "100", "accountId": "200"}
    assert len(seen) == 2


# ── 4. TikTok sandbox address ──────────────────────────────────────────────
def test_the_tiktok_sandbox_address_can_be_overridden(monkeypatch):  # noqa: ANN001
    from app.connectors.tiktok_ads import connector as tt

    sandbox = _connector("tiktok_ads", {"accessToken": "t", "advertiserId": "1", "accountEnvironment": "Sandbox"})
    assert sandbox.base_url == tt.SANDBOX_URL == "https://sandbox-ads.tiktok.com/open_api/v1.3"
    monkeypatch.setattr(settings, "tiktok_sandbox_base_url", "https://sandbox.example.tiktok.com/open_api/v1.3/")
    assert sandbox.base_url == "https://sandbox.example.tiktok.com/open_api/v1.3"
    live = _connector("tiktok_ads", {"accessToken": "t", "advertiserId": "1"})
    assert live.base_url == tt.LIVE_URL
    monkeypatch.setattr(settings, "tiktok_sandbox_base_url", "http://insecure.example")
    with pytest.raises(ConnectorConfigError, match="https://"):
        _ = sandbox.base_url


def test_a_failed_tiktok_sandbox_check_names_the_setting():
    from app.connectors.tiktok_ads.connector import SANDBOX_ADDRESS_HINT
    from app.services.verification import _failure_detail

    assert SANDBOX_ADDRESS_HINT == (
        "If TikTok rejects the sandbox address, set TIKTOK_SANDBOX_BASE_URL to the "
        "address shown in your TikTok developer portal."
    )
    sandbox = _connector("tiktok_ads", {"accessToken": "t", "advertiserId": "1", "accountEnvironment": "Sandbox"})
    _wire(sandbox, lambda r: httpx.Response(404, text="not found"))
    with pytest.raises(ConnectorError) as caught:
        _step(sandbox, "advertiser_info").run()
    assert _failure_detail(sandbox, caught.value).endswith(SANDBOX_ADDRESS_HINT)
    live = _connector("tiktok_ads", {"accessToken": "t", "advertiserId": "1"})
    assert live.diagnose(caught.value) is None


# ── 5. Added sign-in permissions ───────────────────────────────────────────
def test_pinterest_asks_for_the_secret_board_permissions():
    binding = registry.get_spec("pinterest").oauth
    assert binding.scopes == (
        "boards:read", "boards:write", "boards:read_secret", "boards:write_secret",
        "pins:read", "pins:write", "pins:read_secret", "pins:write_secret", "user_accounts:read",
    )
    assert "including secret boards" in binding.access_summary
    assert "without posting publicly" in binding.access_summary


def test_klaviyo_asks_to_read_templates_back():
    binding = registry.get_spec("klaviyo").oauth
    assert binding.scopes == ("accounts:read", "templates:read", "templates:write")
    assert "read those templates back" in binding.access_summary
    assert "cannot send emails" in binding.access_summary


def test_admin_setup_lists_the_new_permissions():
    statuses = {s.name: s for s in oauth_flow.provider_statuses()}
    assert {"boards:read_secret", "pins:write_secret"} <= set(statuses["pinterest"].permissions)
    assert "templates:read" in statuses["klaviyo"].permissions


def test_an_older_pinterest_sign_in_refused_a_secret_board_says_sign_in_again():
    pinterest = _signed_in("pinterest", boardId="b1", refreshToken="r", accountEnvironment="Sandbox")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/boards"):
            return httpx.Response(403, json={"code": 3, "message": "not permitted"})
        return httpx.Response(200, json={})

    _wire(pinterest, handler)
    with pytest.raises(ConnectorError) as caught:
        _step(pinterest, "secret_board_pin_roundtrip").run()
    assert pinterest.diagnose(caught.value).endswith(RESIGN)
    pasted = _connector("pinterest", {"accessToken": "t", "boardId": "b1"})
    assert pasted.diagnose(caught.value) is None


def test_an_older_klaviyo_sign_in_refused_the_read_back_says_sign_in_again():
    klaviyo = _signed_in("klaviyo", refreshToken="r")
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "T1"}})
        if request.method == "GET":
            return httpx.Response(403, json={"errors": [{"detail": "scope"}]})
        deleted.append(request.url.path)
        return httpx.Response(204)

    _wire(klaviyo, handler)
    with pytest.raises(ConnectorError, match=RESIGN):
        _step(klaviyo, "template_roundtrip").run()
    assert deleted == ["/api/templates/T1"]


def test_a_klaviyo_key_refused_the_read_back_still_names_the_key_access():
    klaviyo = _connector("klaviyo", {"apiKey": "pk_" + "a" * 30})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "T1"}})
        if request.method == "GET":
            return httpx.Response(403, json={})
        return httpx.Response(204)

    _wire(klaviyo, handler)
    with pytest.raises(ConnectorError, match="give the key read access to Templates"):
        _step(klaviyo, "template_roundtrip").run()


def test_an_unticked_toggle_never_changes_other_bindings():
    # A binding without sign-in inputs asks for exactly its own scopes.
    spec = registry.get_spec("pinterest")
    assert spec.oauth.scopes_for_inputs({"sandbox": "true"}) == spec.oauth.scopes
    assert spec.oauth.values_for_inputs({"sandbox": "true"}) == {}
    assert vh.SANDBOX == "sandbox"


# ── Safeguards for what could not be confirmed against Meta / Pinterest ──────
def test_meta_retries_a_creative_without_destination_spec_when_meta_refuses_it():
    ads = _connector(
        "meta_ads",
        {"accessToken": "t", "adAccountId": "act_9", "pageId": "55", "finalUrl": "https://dealer.example/"},
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "destination_spec" in body:
            return httpx.Response(400, json={"error": {"message": "(#100) Invalid parameter destination_spec", "code": 100}})
        return httpx.Response(200, json={"id": "cr2"})

    _wire(ads, handler)
    creative = ads._live_upload_creative(
        headline="Test drive", body_copy="Book now", call_to_action="Book a test drive",
        dimensions="1080x1080", audience_segment="all",
    )
    assert creative == "cr2"
    assert len(calls) == 2 and "destination_spec" not in calls[1]


def test_meta_other_creative_errors_are_not_retried():
    ads = _connector(
        "meta_ads",
        {"accessToken": "t", "adAccountId": "act_9", "pageId": "55", "finalUrl": "https://dealer.example/"},
    )
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "(#100) Invalid page_id", "code": 100}})

    _wire(ads, handler)
    import pytest

    from app.core.exceptions import ConnectorError

    with pytest.raises(ConnectorError):
        ads._live_upload_creative(
            headline="x", body_copy="y", call_to_action="Learn more", dimensions="1080x1080", audience_segment="all",
        )
    assert len(calls) == 1


def test_a_retired_graph_version_gets_a_fix_it_message():
    from app.connectors.base.meta_graph import version_problem
    from app.core.exceptions import ConnectorError

    error = ConnectorError("refused")
    error.vendor_body = '{"error":{"message":"(#2635) You are calling a deprecated version of the Ads API.","code": 2635}}'
    message = version_problem(error, "v26.0")
    assert "META_GRAPH_API_VERSION" in message and "v26.0" in message
    plain = ConnectorError("refused")
    plain.vendor_body = '{"error":{"message":"Invalid OAuth access token","code":190}}'
    assert version_problem(plain) is None
    ads = _connector("meta_ads", {"accessToken": "t", "adAccountId": "act_1"})
    assert "META_GRAPH_API_VERSION" in ads.diagnose(error)


def test_meta_graph_version_can_be_overridden_by_setting(monkeypatch):
    import importlib

    from app.connectors.base import meta_graph
    from app.core.config import settings

    monkeypatch.setattr(settings, "meta_graph_api_version", "27.0")
    try:
        assert importlib.reload(meta_graph).GRAPH_API_VERSION == "v27.0"
    finally:
        monkeypatch.setattr(settings, "meta_graph_api_version", "")
        importlib.reload(meta_graph)
    assert meta_graph.GRAPH_API_VERSION == meta_graph.BUILT_FOR_VERSION


def test_pinterest_scope_refusal_on_401_also_asks_to_sign_in_again():
    from app.core.exceptions import ConnectorError

    pin = _signed_in("pinterest", boardId="b1")
    scoped = ConnectorError("refused")
    scoped.http_status = 401
    scoped.vendor_body = '{"code":3,"message":"Authorization failed: missing scope boards:read_secret"}'
    assert "Sign in again" in pin.diagnose(scoped)
    expired = ConnectorError("refused")
    expired.http_status = 401
    expired.vendor_body = '{"code":2,"message":"Authentication failed."}'
    assert pin.diagnose(expired) is None
