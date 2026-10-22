"""The live checks every connector declares (``verify_steps``).

These run each step against a recording fake vendor (tests/support/verify_http)
to prove the request shapes and, for write steps, that whatever is created is
removed again in the same step. The per-connector tests for the other groups
live in ``test_verify_steps_*.py``; this file holds the rules that apply to
every connector, the shared helpers, and WordPress as the worked example.
"""
from __future__ import annotations

import pytest

from app.connectors.base import registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import Credentials
from app.connectors.base.verification import VerifyStep
from app.core.exceptions import ConnectorConfigError, ConnectorError
from tests.support.verify_http import assert_no_secrets, install, run_all

#: Agent A owns the crawler's example step.
SKIP = {"site_crawler"}
SLUGS = sorted(s.slug for s in registry.all_specs() if s.slug not in SKIP)

#: Connectors whose write steps create something that could spend money or
#: be seen publicly: every one of their write steps needs a test account.
SPEND_OR_PUBLIC = {
    "google_ads", "meta_ads", "microsoft_ads", "tiktok_ads", "linkedin_ads", "dsp_exchange",
    "facebook_pages", "instagram_business", "linkedin_pages", "x_twitter", "pinterest",
    "github", "bitbucket", "slack", "custom_api", "zapier_webhooks", "smtp_email",
    "google_business_profile",
}


def _connector(slug: str, values: dict | None = None):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values or {}, oauth_completed=True))


@pytest.mark.parametrize("slug", SLUGS)
def test_every_connector_declares_at_least_one_read_step(slug: str):
    steps = _connector(slug).verify_steps()
    assert steps, f"{slug} has no live checks"
    assert all(isinstance(s, VerifyStep) for s in steps)
    assert any(s.kind == "read" for s in steps), slug
    keys = [s.key for s in steps]
    assert len(keys) == len(set(keys)), f"{slug} repeats a step key"
    for step in steps:
        assert step.kind in ("read", "write")
        assert step.label and step.label[0].isupper(), (slug, step.key)
        # Read steps are never gated on a test account; that flag is for writes.
        if step.kind == "read":
            assert not step.requires_test_account, (slug, step.key)


@pytest.mark.parametrize("slug", sorted(SPEND_OR_PUBLIC))
def test_writes_that_could_spend_or_publish_need_a_test_account(slug: str):
    for step in _connector(slug).verify_steps():
        if step.kind == "write":
            assert step.requires_test_account, (slug, step.key)


def test_building_the_steps_makes_no_calls():
    """verify_steps() only declares; nothing is sent until a step runs."""
    for slug in SLUGS:
        connector = _connector(slug)
        install(connector, [])  # any call would fail as unexpected
        connector.verify_steps()


# ── Shared helpers ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("typed", "allowed", "expected"),
    [
        ("", (vh.TEST_ACCOUNT,), "live"),
        ("Test account", (vh.TEST_ACCOUNT,), "test_account"),
        ("test", (vh.TEST_ACCOUNT, vh.SANDBOX), "test_account"),
        ("  SANDBOX ", (vh.SANDBOX,), "sandbox"),
        ("sandbox", (vh.TEST_ACCOUNT,), "live"),  # not offered by this vendor
        ("whatever", (vh.TEST_ACCOUNT, vh.SANDBOX), "live"),
    ],
)
def test_the_environment_is_read_generously_and_defaults_to_live(typed, allowed, expected):  # noqa: ANN001
    assert vh.normalise_environment(typed, allowed) == expected


def test_create_check_delete_always_deletes():
    deleted: list[str] = []

    def bad_check(_id: str) -> None:
        raise ConnectorError("came back wrong")

    with pytest.raises(ConnectorError, match="reading it back failed"):
        vh.create_check_delete(what="thing", create=lambda: "42", check=bad_check, delete=deleted.append)
    assert deleted == ["42"]


def test_create_check_delete_names_what_it_could_not_remove():
    def boom(_id: str) -> None:
        raise ConnectorError("nope")

    with pytest.raises(ConnectorError, match=r"id 7.*Delete it by hand"):
        vh.create_check_delete(what="thing", create=lambda: "7", check=None, delete=boom)


def test_refuse_unless_test():
    live = _connector("wordpress")
    with pytest.raises(ConnectorConfigError):
        vh.refuse_unless_test(live, "it spends money.")


def test_environment_field_label_has_no_developer_words():
    import re

    jargon = re.compile(r"\b(tokens?|oauth|apis?|clients?|secret)\b", re.I)
    for slug in SLUGS:
        for f in registry.get_class(slug).spec.fields:
            if f.key == "accountEnvironment":
                assert f.label == "Test account" and not f.required, slug
                assert not jargon.search(f.label), slug


# ── WordPress, the worked example ──────────────────────────────────────────
WP = {"siteUrl": "https://example.com", "username": "editor", "applicationPassword": "abcd efgh ijkl"}


def test_wordpress_steps_read_then_draft_and_delete():
    wp = _connector("wordpress", WP)
    rec = install(
        wp,
        [
            ("GET", r"/wp-json$", {"name": "Example", "namespaces": ["wp/v2", "oembed/1.0"]}),
            ("GET", r"/wp/v2/(pages|posts)\?", [{"id": 1, "link": "https://example.com/a/"}]),
            ("POST", r"/wp/v2/posts$", {"id": 99, "link": "https://example.com/?p=99"}),
            ("POST", r"/wp/v2/pages/99$", (404, {"code": "rest_post_invalid_id"})),
            ("POST", r"/wp/v2/posts/99$", {"id": 99}),
            ("GET", r"/wp/v2/posts/99\?context=edit", {"id": 99, "status": "draft"}),
            ("DELETE", r"/wp/v2/posts/99\?force=true", {"deleted": True}),
        ],
    )
    results = run_all(wp)
    assert set(results) == {"site_info", "list_pages", "draft_post_roundtrip"}
    created = rec.body("POST", r"/wp/v2/posts$")
    assert created["status"] == "draft" and created["title"].startswith(vh.VERIFY_LABEL)
    assert rec.index("POST", r"/wp/v2/posts$") < rec.index("DELETE", r"/posts/99")
    assert results["draft_post_roundtrip"][1] == {"created_id": "99", "deleted": True}
    assert_no_secrets(wp, results)


def test_wordpress_draft_is_deleted_even_when_it_comes_back_published():
    wp = _connector("wordpress", WP)
    rec = install(
        wp,
        [
            ("POST", r"/wp/v2/posts$", {"id": 5}),
            ("POST", r"/wp/v2/pages/5$", (404, {})),
            ("POST", r"/wp/v2/posts/5$", {"id": 5}),
            ("GET", r"/wp/v2/posts/5", {"id": 5, "status": "publish"}),
            ("DELETE", r"/wp/v2/posts/5", {"deleted": True}),
        ],
    )
    step = next(s for s in wp.verify_steps() if s.key == "draft_post_roundtrip")
    with pytest.raises(ConnectorError):
        step.run()
    assert rec.find("DELETE", r"/posts/5")
