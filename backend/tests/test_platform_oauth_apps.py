"""Sign-in provider apps set up in Portal Admin, and what customers see
while one is not set up yet.

Pure tests — no database. The table is replaced at its seams (the row
lookup and the snapshot loader), so what is exercised is the encryption,
the resolution order, the cache, the guards, what reaches the browser, and
how a vendor's answer to a credential check is read.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest

from app.connectors.base import oauth_providers, registry
from app.core.config import settings
from app.core.exceptions import ForbiddenError, InvalidInputError
from app.models.portal import PlatformOAuthApp
from app.services import platform_oauth_apps as apps
from app.services import provider_setup
from app.services.platform_oauth_apps import Snapshot

ALL_PROVIDER_SETTINGS = sorted(provider_setup.field_index())


@pytest.fixture(autouse=True)
def nothing_in_the_environment(monkeypatch):  # noqa: ANN001, ANN201
    """Every provider value starts unset, so each test says where its come from."""
    for key in ALL_PROVIDER_SETTINGS:
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.com")


class _Rows:
    """Stands in for the table: ``_find`` reads from here, ``add`` writes."""

    def __init__(self) -> None:
        self.rows: dict[str, PlatformOAuthApp] = {}

    def add(self, row: PlatformOAuthApp) -> None:
        self.rows[row.provider] = row

    def delete(self, row: PlatformOAuthApp) -> None:
        self.rows.pop(row.provider, None)

    def flush(self) -> None:
        pass


@pytest.fixture
def table(monkeypatch):  # noqa: ANN001, ANN201
    rows = _Rows()
    monkeypatch.setattr(apps, "_find", lambda db, provider: rows.rows.get(provider))
    # The resolver reads the same rows, as the real loader reads the table.
    apps.set_loader(lambda: apps.snapshot_from_rows(list(rows.rows.values())))
    return rows


GOOGLE = {
    "oauth_google_client_id": "1234-abc.apps.googleusercontent.com",
    "oauth_google_client_secret": "GOCSPX-super-secret-value-9f3a",
    "google_ads_developer_token": "dev-token-value-77xz",
}


# ── Encrypted storage ──────────────────────────────────────────────────────
def test_saved_values_round_trip_and_secrets_are_encrypted(table):  # noqa: ANN001
    changed = apps.save(table, provider="google", values=GOOGLE, actor="ops@example.com")
    assert changed == ["Client ID", "Client secret", "Developer token"]
    row = table.rows["google"]
    # The client ID is not a secret; the rest is ciphertext.
    assert row.client_id == GOOGLE["oauth_google_client_id"]
    assert row.client_secret_encrypted.startswith("v1:")
    assert row.extras_encrypted.startswith("v1:")
    stored = row.client_secret_encrypted + row.extras_encrypted
    assert "super-secret" not in stored and "dev-token" not in stored
    assert apps.decode_row(row) == GOOGLE
    assert row.updated_by == "ops@example.com"


def test_a_ciphertext_moved_to_another_provider_does_not_decrypt(table):  # noqa: ANN001
    apps.save(table, provider="google", values=GOOGLE, actor="ops")
    moved = PlatformOAuthApp(
        provider="meta", client_id="1", client_secret_encrypted=table.rows["google"].client_secret_encrypted
    )
    assert "oauth_meta_client_secret" not in apps.decode_row(moved)


def test_a_blank_box_keeps_what_is_saved(table):  # noqa: ANN001
    apps.save(table, provider="google", values=GOOGLE, actor="ops")
    changed = apps.save(
        table,
        provider="google",
        values={"oauth_google_client_id": "", "oauth_google_client_secret": "GOCSPX-new-secret-1234"},
        actor="ops",
    )
    assert changed == ["Client secret"]
    decoded = apps.decode_row(table.rows["google"])
    assert decoded["oauth_google_client_id"] == GOOGLE["oauth_google_client_id"]
    assert decoded["oauth_google_client_secret"] == "GOCSPX-new-secret-1234"
    assert decoded["google_ads_developer_token"] == GOOGLE["google_ads_developer_token"]
    assert apps.save(table, provider="google", values={"oauth_google_client_id": " "}, actor="ops") == []


def test_values_must_belong_to_the_provider(table):  # noqa: ANN001
    with pytest.raises(InvalidInputError):
        apps.save(table, provider="google", values={"oauth_meta_client_id": "x"}, actor="ops")
    with pytest.raises(InvalidInputError):
        apps.save(table, provider="wordpress", values={}, actor="ops")


def test_a_one_line_private_key_is_restored(table):  # noqa: ANN001
    apps.save(
        table,
        provider="github",
        values={"github_app_private_key": "-----BEGIN KEY-----\\nabc\\n-----END KEY-----"},
        actor="ops",
    )
    key = apps.decode_row(table.rows["github"])["github_app_private_key"]
    assert key == "-----BEGIN KEY-----\nabc\n-----END KEY-----"


def test_clearing_removes_the_row(table):  # noqa: ANN001
    apps.save(table, provider="google", values=GOOGLE, actor="ops")
    assert apps.clear(table, provider="google") is True
    assert apps.clear(table, provider="google") is False


# ── Resolution: saved in Portal Admin, else the server setting ─────────────
def test_a_saved_value_wins_over_the_environment(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "oauth_google_client_id", "env-id")
    monkeypatch.setattr(settings, "oauth_google_client_secret", "env-secret")
    apps.set_loader(lambda: Snapshot(values={"oauth_google_client_id": "db-id"}))
    assert oauth_providers.GOOGLE.client_id == "db-id"
    assert oauth_providers.GOOGLE.client_secret == "env-secret"
    apps.set_loader(Snapshot)
    assert oauth_providers.GOOGLE.client_id == "env-id"


def test_sign_in_switches_on_from_saved_values_alone(table):  # noqa: ANN001
    ga4, ads = registry.get_spec("google_analytics_4"), registry.get_spec("google_ads")
    assert not oauth_providers.sign_in_available(ga4)
    apps.save(table, provider="google", values={k: GOOGLE[k] for k in list(GOOGLE)[:2]}, actor="ops")
    apps.invalidate_cache()
    assert oauth_providers.sign_in_available(ga4)
    # Google Ads also needs the developer token.
    assert oauth_providers.missing_settings(ads) == ["GOOGLE_ADS_DEVELOPER_TOKEN"]
    apps.save(table, provider="google", values=GOOGLE, actor="ops")
    apps.invalidate_cache()
    assert oauth_providers.sign_in_available(ads)


def test_the_token_exchange_uses_the_saved_app(monkeypatch, table):  # noqa: ANN001
    from app.services import oauth_flow

    apps.save(table, provider="google", values=GOOGLE, actor="ops")
    apps.invalidate_cache()
    seen: list[httpx.Request] = []
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})

    monkeypatch.setattr(
        oauth_flow.httpx, "Client",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )
    oauth_flow.exchange_code(oauth_providers.GOOGLE, code="c", verifier="v")
    sent = dict(httpx.QueryParams(seen[0].content.decode()))
    assert sent["client_id"] == GOOGLE["oauth_google_client_id"]
    assert sent["client_secret"] == GOOGLE["oauth_google_client_secret"]


def test_platform_tokens_for_connectors_come_from_the_saved_values():
    apps.set_loader(
        lambda: Snapshot(values={"google_ads_developer_token": "saved-dev", "github_app_id": "42"})
    )
    assert oauth_providers.platform_setting("google_ads_developer_token") == "saved-dev"
    from app.connectors.github import connector as github

    assert github.platform_setting("github_app_id") == "42"


# ── Cache ──────────────────────────────────────────────────────────────────
def test_the_cache_is_reused_until_invalidated_or_stale(monkeypatch):  # noqa: ANN001
    calls: list[int] = []

    def loader() -> Snapshot:
        calls.append(1)
        return Snapshot(values={"oauth_meta_client_id": f"v{len(calls)}"})

    apps.set_loader(loader)
    assert apps.stored_value("oauth_meta_client_id") == "v1"
    assert apps.stored_value("oauth_meta_client_id") == "v1"
    assert len(calls) == 1
    apps.invalidate_cache()
    assert apps.stored_value("oauth_meta_client_id") == "v2"
    # Another worker's save shows up once the snapshot is older than the TTL.
    monkeypatch.setattr(apps, "CACHE_TTL_SECONDS", 0.0)
    assert apps.stored_value("oauth_meta_client_id") == "v3"


def test_an_unreadable_table_falls_back_to_the_environment(monkeypatch):  # noqa: ANN001
    def broken() -> Snapshot:
        raise RuntimeError("relation platform_oauth_apps does not exist")

    monkeypatch.setattr(settings, "oauth_meta_client_id", "env-meta")
    apps.set_loader(broken)
    assert oauth_providers.META.client_id == "env-meta"


# ── Guards ─────────────────────────────────────────────────────────────────
def _dependency_calls(dependant) -> set:  # noqa: ANN001
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def test_every_sign_in_provider_route_is_portal_admin_only():
    from app.api.deps import require_portal_admin
    from app.api.v1 import portal

    routes = [r for r in portal.router.routes if "sign-in-providers" in r.path]
    assert {(next(iter(r.methods)), r.path) for r in routes} == {
        ("GET", "/portal/sign-in-providers"),
        ("PUT", "/portal/sign-in-providers/{name}"),
        ("POST", "/portal/sign-in-providers/{name}/check"),
        ("DELETE", "/portal/sign-in-providers/{name}"),
    }
    for route in routes:
        assert require_portal_admin in _dependency_calls(route.dependant), route.path


def test_a_workspace_admin_is_not_a_portal_admin(monkeypatch):  # noqa: ANN001
    from types import SimpleNamespace

    from app.api.deps import CurrentUser, require_portal_admin

    class Caller:
        is_portal_admin = CurrentUser.is_portal_admin
        require_portal_admin = CurrentUser.require_portal_admin

        def __init__(self, email: str) -> None:
            self.user = SimpleNamespace(email=email)

    monkeypatch.setattr(settings, "portal_admin_emails", ["ops@platform.example"])
    with pytest.raises(ForbiddenError):
        require_portal_admin(Caller("owner@customer.example"))
    assert require_portal_admin(Caller("ops@platform.example")).user.email == "ops@platform.example"


# ── Nothing secret reaches the browser ─────────────────────────────────────
def test_the_admin_page_never_receives_a_secret(monkeypatch, table):  # noqa: ANN001
    from app.api.v1.portal import _provider_out
    from app.services import oauth_flow

    secrets = {
        **GOOGLE,
        "oauth_github_client_secret": "gh-client-secret-abcd",
        "github_app_private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecret\n-----END RSA PRIVATE KEY-----",
    }
    monkeypatch.setattr(settings, "oauth_meta_client_secret", "meta-env-secret-0042")
    apps.save(table, provider="google", values=GOOGLE, actor="ops")
    apps.save(
        table, provider="github",
        values={k: v for k, v in secrets.items() if k.startswith(("oauth_github", "github_"))}, actor="ops",
    )
    apps.invalidate_cache()
    body = json.dumps([_provider_out(s).model_dump() for s in oauth_flow.provider_statuses()])
    for key, value in [*secrets.items(), ("meta", "meta-env-secret-0042")]:
        if key.endswith("client_id"):
            continue
        assert value not in body, key
    assert "MIIEsecret" not in body
    google = next(s for s in oauth_flow.provider_statuses() if s.name == "google")
    by_key = {f["key"]: f for f in google.fields}
    assert by_key["oauth_google_client_id"]["preview"] == GOOGLE["oauth_google_client_id"]
    assert by_key["oauth_google_client_secret"]["preview"] == "Set · ends in 9f3a"
    assert by_key["oauth_google_client_secret"]["source"] == "portal"
    meta = next(s for s in oauth_flow.provider_statuses() if s.name == "meta")
    assert {f["key"]: f["source"] for f in meta.fields}["oauth_meta_client_secret"] == "server"


def test_providers_are_ordered_by_impact_and_say_what_they_unlock():
    from app.services import oauth_flow

    statuses = oauth_flow.provider_statuses()
    names = [s.name for s in statuses]
    assert names[:5] == ["google", "meta", "linkedin", "linkedin_pages", "microsoft"]
    assert names[-1] == "wordpress"
    google = statuses[0]
    assert google.status == "not_set_up" and google.steps and google.console_url
    assert google.redirect_uri == "https://api.example.com/api/v1/oauth/callback/google"
    assert {c["slug"] for c in google.connectors} >= {"google_analytics_4", "search_console", "google_ads"}
    assert "https://www.googleapis.com/auth/analytics.readonly" in google.permissions
    assert "openid" not in google.permissions
    assert [f["label"] for f in google.fields] == ["Client ID", "Client secret", "Developer token"]
    meta = statuses[1]
    assert [f["label"] for f in meta.fields] == ["App ID", "App secret"]


def test_a_provider_missing_only_an_extra_value_is_partly_ready():
    from app.services import oauth_flow

    apps.set_loader(lambda: Snapshot(values={k: GOOGLE[k] for k in list(GOOGLE)[:2]}))
    google = next(s for s in oauth_flow.provider_statuses() if s.name == "google")
    assert google.status == "partial"
    assert google.status_detail == "Google Ads also needs the Developer token."
    ads = next(c for c in google.connectors if c["slug"] == "google_ads")
    assert ads["needs"] == ["Developer token"]


# ── What customers see while a sign-in is not set up ───────────────────────
def _record(slug: str, **kw):  # noqa: ANN003, ANN202
    from app.models.connector import ConnectorRecord

    return ConnectorRecord(slug=slug, name=slug, category="x", credential_hints=kw.pop("hints", {}), **kw)


#: No developer words on the default path of a sign-in connector.
JARGON = re.compile(r"\b(tokens?|oauth|apis?|clients?|scopes?|endpoints?|webhooks?)\b", re.I)
SIGN_IN_SLUGS = sorted(
    s.slug for s in registry.all_specs() if s.oauth and s.oauth.provider != "wordpress"
)


def _default_copy(guide) -> list[str]:  # noqa: ANN001
    return [
        guide.button_label, guide.access_summary, guide.setup_message, guide.admin_hint,
        guide.admin_setup_label, guide.sign_in_note, guide.choice_noun,
        *(i.label for i in guide.sign_in_inputs), *(i.help_text for i in guide.sign_in_inputs),
    ]


@pytest.mark.parametrize("slug", SIGN_IN_SLUGS)
def test_not_set_up_shows_a_switched_off_button_and_no_developer_fields(slug: str):
    from app.services.views import ConnectContext, connect_guide

    spec = registry.get_spec(slug)
    member = connect_guide(_record(slug, connected=False))
    workspace_admin = connect_guide(_record(slug, connected=False), ConnectContext(show_admin_notes=True))
    portal_admin = connect_guide(
        _record(slug, connected=False), ConnectContext(show_admin_notes=True, is_portal_admin=True)
    )
    assert not member.sign_in_available
    if spec.guide is not None:
        # Bing Webmaster Tools, Mailchimp, Klaviyo: the guided key is the easy path.
        assert member.mode == "key" and len(member.steps) == 3 and not member.setup_pending
        assert portal_admin.admin_setup_url.endswith(f"provider={spec.oauth.provider}")
        assert portal_admin.developer_label == ""
    else:
        assert member.mode == "sign_in" and member.setup_pending and member.can_request_setup
        assert "isn't switched on for this workspace yet" in member.setup_message
        assert "you'll connect in one click" in member.setup_message
        # A member: no admin link, no hint, no developer route.
        assert member.admin_setup_url == member.admin_hint == member.developer_label == ""
        # A workspace admin: who to ask, and the developer route as a link.
        assert "platform administrator" in workspace_admin.admin_hint
        assert workspace_admin.admin_setup_url == "" and not workspace_admin.can_request_setup
        assert workspace_admin.developer_label.startswith("For developers")
        # A portal admin: the way to switch it on.
        assert portal_admin.admin_setup_url == f"/portal?tab=signin&provider={spec.oauth.provider}"
        assert portal_admin.admin_setup_label.startswith("Set up Sign in with")
        assert portal_admin.admin_hint == ""
        # The default path shows no form; and nothing the sign-in would
        # supply (a secret, a token) is left outside the hidden set.
        shown = [f for f in spec.fields if f.key not in member.sign_in_hidden_fields]
        assert not [f.key for f in shown if f.is_secret], slug
    for guide in (member, workspace_admin, portal_admin):
        for text in _default_copy(guide):
            assert not JARGON.search(text or ""), (slug, text)


def test_the_google_wording_is_exact():
    from app.services.views import connect_guide

    assert connect_guide(_record("google_analytics_4", connected=False)).setup_message == (
        "Sign in with Google isn't switched on for this workspace yet. Once your administrator "
        "switches it on, you'll connect in one click."
    )
    assert connect_guide(_record("slack", connected=False)).setup_message.startswith(
        "“Add to Slack” isn't switched on"
    )


def test_a_connection_made_with_own_app_credentials_keeps_its_form():
    from app.services.views import connect_guide

    guide = connect_guide(_record("google_analytics_4", connected=True))
    assert guide.mode == "sign_in" and not guide.setup_pending


@pytest.mark.parametrize("slug", ["google_analytics_4", "meta_ads", "slack"])
def test_once_set_up_the_sign_in_is_offered_with_no_jargon(monkeypatch, slug: str):  # noqa: ANN001
    from app.services.views import connect_guide

    provider = registry.get_spec(slug).oauth.provider
    apps.set_loader(
        lambda: Snapshot(values={f"oauth_{provider}_client_id": "id", f"oauth_{provider}_client_secret": "s"})
    )
    guide = connect_guide(_record(slug, connected=False))
    assert guide.sign_in_available and not guide.setup_pending and guide.setup_message == ""
    for text in _default_copy(guide):
        assert not JARGON.search(text or ""), (slug, text)


# ── Checking credentials with the vendor ───────────────────────────────────
@pytest.mark.parametrize(
    ("status", "body", "verdict"),
    [
        (401, {"error": "invalid_client", "error_description": "Unauthorized"}, "rejected"),
        (400, {"error": "unauthorized_client"}, "rejected"),
        (400, {"error": "invalid_grant", "error_description": "Malformed auth code."}, "accepted"),
        (200, {"error": "bad_verification_code"}, "accepted"),
        (200, {"error": "incorrect_client_credentials"}, "rejected"),
        (400, {"error": "redirect_uri_mismatch"}, "unknown"),
        (500, "<html>", "unknown"),
        (200, {"access_token": "x"}, "unknown"),
    ],
)
def test_reading_a_token_endpoint_answer(status: int, body: object, verdict: str):
    assert apps.classify_token_probe(status, body) == verdict


def _vendor(monkeypatch, handler) -> list[httpx.Request]:  # noqa: ANN001
    seen: list[httpx.Request] = []
    real = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(apps, "_http", lambda: real(transport=httpx.MockTransport(recording)))
    return seen


def _google_saved() -> None:
    apps.set_loader(lambda: Snapshot(values={k: GOOGLE[k] for k in list(GOOGLE)[:2]}))


def test_google_credentials_that_google_accepts(monkeypatch):  # noqa: ANN001
    _google_saved()
    seen = _vendor(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    result = apps.check("google")
    assert result.status == "ok"
    sent = dict(httpx.QueryParams(seen[0].content.decode()))
    assert str(seen[0].url) == "https://oauth2.googleapis.com/token"
    assert sent["code"] == apps.PROBE_CODE and sent["client_id"] == GOOGLE["oauth_google_client_id"]


def test_google_credentials_that_google_refuses(monkeypatch):  # noqa: ANN001
    _google_saved()
    _vendor(monkeypatch, lambda r: httpx.Response(401, json={"error": "invalid_client"}))
    result = apps.check("google")
    assert result.status == "failed" and "did not accept" in result.message


def test_an_unreachable_vendor_is_not_a_failure(monkeypatch):  # noqa: ANN001
    _google_saved()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    _vendor(monkeypatch, down)
    result = apps.check("google")
    assert result.status == "unchecked" and "connecting an integration" in result.message


def test_a_google_client_id_in_the_wrong_shape_is_caught_without_calling_google(monkeypatch):  # noqa: ANN001
    apps.set_loader(
        lambda: Snapshot(values={"oauth_google_client_id": "123", "oauth_google_client_secret": "s"})
    )
    seen = _vendor(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    assert apps.check("google").status == "failed" and not seen


def test_providers_without_a_reliable_check_say_so():
    apps.set_loader(lambda: Snapshot(values={"oauth_meta_client_id": "1", "oauth_meta_client_secret": "s"}))
    result = apps.check("meta")
    assert result.status == "unchecked" and "test it by connecting an integration" in result.message
    apps.set_loader(Snapshot)
    assert apps.check("meta").status == "failed"


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()


def _github(**over: str) -> None:
    values = {
        "oauth_github_client_id": "Iv1.abc",
        "oauth_github_client_secret": "gh-secret",
        "github_app_id": "123456",
        "github_app_slug": "automarket-ai",
        "github_app_private_key": _rsa_pem(),
        **over,
    }
    apps.set_loader(lambda: Snapshot(values=values))


def test_github_checks_the_client_and_the_app_key(monkeypatch):  # noqa: ANN001
    _github()

    def vendor(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"error": "bad_verification_code"})
        assert request.headers["authorization"].startswith("Bearer ey")
        return httpx.Response(200, json={"id": 123456, "slug": "automarket-ai"})

    seen = _vendor(monkeypatch, vendor)
    assert apps.check("github").status == "ok"
    assert [r.url.path for r in seen] == ["/login/oauth/access_token", "/app"]


def test_github_names_a_wrong_link_name(monkeypatch):  # noqa: ANN001
    _github(github_app_slug="wrong-name")

    def vendor(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(200, json={"slug": "automarket-ai"})

    _vendor(monkeypatch, vendor)
    result = apps.check("github")
    assert result.status == "failed" and "automarket-ai" in result.message


def test_github_refuses_an_unreadable_key_without_calling_github(monkeypatch):  # noqa: ANN001
    _github(github_app_private_key="not a key")

    def vendor(request: httpx.Request) -> httpx.Response:
        assert request.url.path != "/app"
        return httpx.Response(200, json={"error": "bad_verification_code"})

    _vendor(monkeypatch, vendor)
    assert apps.check("github").status == "failed"


def test_every_oauth_provider_can_be_set_up_in_portal_admin():
    oauth2 = {name for name, p in oauth_providers.PROVIDERS.items() if p.kind == "oauth2"}
    assert set(provider_setup.SETUPS) == oauth2
    for setup in provider_setup.SETUPS.values():
        assert setup.steps and all(f.key in type(settings).model_fields for f in setup.fields), setup.name
    # Every extra value a connector waits for can be entered there.
    for spec in registry.all_specs():
        for key in spec.oauth.requires_settings if spec.oauth else ():
            assert key in provider_setup.field_index(), (spec.slug, key)
