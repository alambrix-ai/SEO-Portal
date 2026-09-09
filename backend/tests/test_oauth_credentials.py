"""The access token these connectors always required, and never asked for.

Ten connectors rendered a button labelled "Authorize with Google" (or Meta,
or Slack). It was wired to ``onClick={() => setOauthDone(true)}``: it set a
boolean in the browser and relabelled itself "Authorised". There was no
redirect, no code exchange and no token.

All ten then called ``credentials.require("accessToken")`` on the first real
request — against a field that appeared in no connector's field list, so
nothing could ever have supplied it. The connector saved as connected and
failed on first use. The button did not fail to authorise; it asserted that
authorisation had happened.

These tests hold the shape of the fix: the credential is declared, it is
required, it is a secret, and where the vendor's tokens expire it is
obtained by refresh rather than pasted.
"""
from __future__ import annotations

import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError

#: Every connector that used to render the fake button.
OAUTH_SLUGS = (
    "google_ads",
    "google_analytics_4",
    "search_console",
    "google_business_profile",
    "microsoft_ads",
    "salesforce",
    "slack",
    "meta_ads",
    "linkedin_ads",
    "tiktok_ads",
)

#: The six whose access tokens expire, so a pasted one is useless by
#: lunchtime and a refresh token is what gets stored.
REFRESHABLE = (
    "google_ads",
    "google_analytics_4",
    "search_console",
    "google_business_profile",
    "microsoft_ads",
    "salesforce",
)


def test_no_connector_still_renders_the_button_that_obtained_nothing():
    """An OAuth field means "show the button". Nothing declares one now.

    If a real redirect flow is built, this test is the thing that should
    fail — and whoever re-enables the field should have to come here and say
    that the flow now exists.
    """
    offenders = [
        spec.slug
        for spec in registry.all_specs()
        if any(field.is_oauth for field in spec.fields)
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize("slug", OAUTH_SLUGS)
def test_the_token_is_a_declared_required_secret(slug: str):
    """Declared, so the form asks for it. Required, so the connector cannot
    save without it. Secret, so it is encrypted and never returned."""
    spec = registry.get_spec(slug)
    fields = {field.key: field for field in spec.fields}

    wanted = "refreshToken" if slug in REFRESHABLE else "accessToken"
    assert wanted in fields, (slug, sorted(fields))
    assert fields[wanted].required is True, slug
    assert fields[wanted].is_secret is True, slug
    assert fields[wanted].help_text, f"{slug}: no help on where to get it"


@pytest.mark.parametrize("slug", REFRESHABLE)
def test_a_refreshable_connector_asks_for_the_whole_credential(slug: str):
    """A refresh token alone cannot be exchanged. All three come from the
    same app registration, so all three are asked for together."""
    fields = {field.key: field for field in registry.get_spec(slug).fields}
    for key in ("refreshToken", "clientId", "clientSecret"):
        assert key in fields, (slug, key)
    assert registry.get_class(slug).token_url, slug


@pytest.mark.parametrize("slug", OAUTH_SLUGS)
def test_a_connector_with_no_token_says_which_field_to_fill_in(slug: str):
    """Rather than raising about a key nobody was ever shown."""
    connector = registry.get_class(slug)(Credentials(values={}), org_id="o")
    with pytest.raises(ConnectorConfigError) as caught:
        _ = connector.access_token
    assert "access token" in str(caught.value).lower()


def test_a_long_lived_token_is_used_as_given():
    """Slack, Meta, LinkedIn and TikTok issue tokens that do not expire on a
    schedule. Pasting one is the vendor's own documented path."""
    slack = registry.get_class("slack")(
        Credentials(values={"defaultChannel": "#seo", "accessToken": "xoxb-abc"}),
        org_id="o",
    )
    assert slack.access_token == "xoxb-abc"


def test_a_refresh_token_is_exchanged_for_an_access_token(monkeypatch):  # noqa: ANN001
    """The part that makes the Google family usable at all: their access
    tokens live an hour, so what is stored is the refresh token."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs.pop("timeout", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.connectors.base.oauth.httpx.Client", fake_client)

    connector = registry.get_class("search_console")(
        Credentials(
            values={
                "siteUrl": "https://example.test/",
                "refreshToken": "1//refresh",
                "clientId": "client.apps.googleusercontent.com",
                "clientSecret": "secret",
            }
        ),
        org_id="o",
    )
    assert connector.access_token == "ya29.fresh"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "1//refresh"

    # Cached until shortly before expiry: one exchange, not one per call.
    assert connector.access_token == "ya29.fresh"
    assert len(calls) == 1


def test_a_refresh_token_without_its_client_says_so(monkeypatch):  # noqa: ANN001
    """The most likely half-filled form, and the error names the fix."""
    connector = registry.get_class("google_ads")(
        Credentials(values={"customerId": "1", "refreshToken": "1//r"}), org_id="o"
    )
    with pytest.raises(ConnectorConfigError, match="client id and"):
        _ = connector.access_token


def test_a_revoked_refresh_token_reports_the_vendors_own_reason(monkeypatch):  # noqa: ANN001
    """``invalid_grant`` means somebody has to re-authorise, and that is not
    something to translate into "something went wrong"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs.pop("timeout", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.connectors.base.oauth.httpx.Client", fake_client)

    connector = registry.get_class("google_analytics_4")(
        Credentials(
            values={
                "propertyId": "123",
                "refreshToken": "1//revoked",
                "clientId": "c",
                "clientSecret": "s",
            }
        ),
        org_id="o",
    )
    with pytest.raises(ConnectorError, match="invalid_grant"):
        _ = connector.access_token
