"""What the browser is actually sent.

The ask behind these tests was "can the Network tab show only encrypted
text". It cannot, and the reason is worth writing down where the next person
will find it: whatever the console can display, the browser must hold in
readable form, and whatever key an in-browser decryption used would be
sitting in the same bundle as the code that used it. Encrypting the payload
on top of TLS hides the data from the person holding the browser for exactly
as long as it takes them to open the Sources tab.

So the guarantee is not "the response is unreadable". It is:

* **secrets are never in a response at all** — not encrypted in it, not in
  it. A token, key or password goes in and never comes back out;
* **the response carries nothing the screen does not use**, because the only
  real way to reduce what is visible is to send less. Creative body copy was
  being decrypted on every Ads page load and sent to a browser that
  displayed none of it.

Transport is TLS, with HSTS and an http→https redirect in production, and
every API response is ``Cache-Control: no-store`` so none of it lands in the
browser's disk cache. Those are asserted elsewhere; these tests cover the
payload itself.
"""
from __future__ import annotations

import pytest

from app.connectors.base import registry
from app.schemas import workspace as schemas
from tests.conftest import requires_db

#: A value distinctive enough that finding it anywhere in a response body is
#: unambiguous.
CANARY = "ZZ-canary-secret-value-9f3a2b-do-not-leak"


def test_no_response_schema_declares_a_secret_field():
    """The structural version of the guarantee.

    Every connector field marked secret is checked against every response
    model's field names: if a schema ever gains a `token`, `apiKey`,
    `clientSecret` or `password`, this fails before it can ship.
    """
    secret_keys = {
        field.key
        for spec in registry.all_specs()
        for field in spec.fields
        if field.is_secret
    }
    assert secret_keys, "no secret fields found — the check would be vacuous"

    offenders: list[tuple[str, str]] = []
    for name in dir(schemas):
        model = getattr(schemas, name)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict):
            continue
        for key in fields:
            # Compared case-insensitively: the credential keys are camelCase
            # and a schema would spell the same thing snake_case.
            flat = key.replace("_", "").lower()
            for secret_key in secret_keys:
                if flat == secret_key.replace("_", "").lower():
                    offenders.append((name, key))
    assert offenders == [], offenders


@requires_db
def test_a_stored_credential_never_comes_back_out(client, clean_db):  # noqa: ANN001
    """The behavioural version: submit a secret, then read everything the
    console reads and look for it."""
    from tests.test_api_flows import auth, register

    token = register(client)["tokens"]["access_token"]

    saved = client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://example.test", "apiKey": CANARY}},
        headers=auth(token),
    )
    assert saved.status_code == 200, saved.text
    # Not even in the response to the request that submitted it.
    assert CANARY not in saved.text

    # Every screen the console loads, including the one that renders the
    # connector's own form with its stored hints.
    for path in (
        "/api/v1/connectors",
        "/api/v1/connectors/fake_cms",
        "/api/v1/dashboard",
        "/api/v1/agents",
        "/api/v1/notifications",
        "/api/v1/onboarding",
        "/api/v1/admin",
        "/api/v1/auth/me",
    ):
        response = client.get(path, headers=auth(token))
        assert response.status_code == 200, (path, response.text)
        assert CANARY not in response.text, path


@requires_db
def test_an_api_response_is_never_cached_to_disk(client, clean_db):  # noqa: ANN001
    """A response the browser writes to its disk cache outlives the session
    that was allowed to see it."""
    from tests.test_api_flows import auth, register

    token = register(client)["tokens"]["access_token"]
    response = client.get("/api/v1/dashboard", headers=auth(token))
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize(
    ("model", "gone"),
    [
        # Findings evidence is an open dict; for a broken-link or thin-content
        # finding it carries page text and link lists, and nothing rendered it.
        ("SeoIssueOut", "evidence"),
        ("SeoIssueOut", "page_id"),
        # The card shows platform, dimensions, headline and status. The body
        # copy was decrypted on every page load for nobody to read.
        ("CreativeOut", "body_copy"),
        ("CreativeOut", "call_to_action"),
        ("CreativeOut", "audience_segment"),
        ("CreativeOut", "source_product"),
        # A pre-rendered polyline, dead once the chart computed its own.
        ("ReportsOut", "trend_points"),
    ],
)
def test_unread_content_is_no_longer_sent(model: str, gone: str):
    """Sending less is the only thing that actually reduces what a Network
    tab can show, so what was removed stays removed."""
    fields = getattr(schemas, model).model_fields
    assert gone not in fields, f"{model}.{gone} is being serialised again"
