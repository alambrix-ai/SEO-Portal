"""Live checks (``verify_steps``) for the social connectors, against a recording fake vendor.

Facebook Pages, Instagram, LinkedIn Pages, X and Pinterest: request shapes,
create-then-delete order for every write step, and that nothing secret ends
up in a step's detail or evidence.
"""
from __future__ import annotations

import json

import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError
from tests.support.verify_http import assert_no_secrets, install, run_all

TOKEN = "tok-social-secret-123"
GRAPH = r"https://graph\.facebook\.com/v\d+\.\d/"


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values={"accessToken": TOKEN, **values}, oauth_completed=True))


def _steps(connector) -> dict:  # noqa: ANN001
    return {s.key: s for s in connector.verify_steps()}


# ── Facebook Pages ─────────────────────────────────────────────────────────
def _facebook_routes(*, read_back: object = None) -> list:
    return [
        ("GET", GRAPH + r"111\?", {"id": "111", "name": "Test Page"}),
        ("GET", GRAPH + r"111/feed", {"data": [{"id": "111_1"}, {"id": "111_2"}]}),
        ("POST", GRAPH + r"111/feed$", {"id": "111_99"}),
        ("GET", GRAPH + r"111_99\?", read_back or {"id": "111_99", "is_published": False}),
        ("DELETE", GRAPH + r"111_99$", {"success": True}),
    ]


def test_facebook_reads_page_and_posts_and_round_trips_an_unpublished_post():
    fb = _connector("facebook_pages", {"pageId": "111"})
    rec = install(fb, _facebook_routes())
    results = run_all(fb)

    assert set(results) == {"page", "recent_posts", "unpublished_post_roundtrip"}
    assert results["recent_posts"][1]["count"] == 2
    body = rec.body("POST", r"/111/feed$")
    assert body["published"] is False and "Synaprank.ai verification" in body["message"]
    assert rec.index("POST", r"/111/feed$") < rec.index("GET", r"/111_99\?") < rec.index("DELETE", r"/111_99$")
    assert "is_published" in str(rec.find("GET", r"/111_99\?")[0].url)
    assert results["unpublished_post_roundtrip"][1] == {"created_id": "111_99", "deleted": True}
    assert all(s.requires_test_account for s in fb.verify_steps() if s.kind == "write")
    assert_no_secrets(fb, results)


def test_facebook_deletes_the_post_even_when_it_came_back_published():
    fb = _connector("facebook_pages", {"pageId": "111"})
    rec = install(fb, _facebook_routes(read_back={"id": "111_99", "is_published": True}))
    with pytest.raises(ConnectorError, match="removed"):
        _steps(fb)["unpublished_post_roundtrip"].run()
    assert rec.index("POST", r"/111/feed$") < rec.index("DELETE", r"/111_99$")


# ── Instagram ──────────────────────────────────────────────────────────────
def test_instagram_prepares_a_container_and_never_publishes():
    ig = _connector("instagram_business", {"igUserId": "1784"})
    ig._sleep = lambda _s: None
    statuses = iter(["IN_PROGRESS", "FINISHED"])
    rec = install(ig, [
        ("GET", GRAPH + r"1784\?", {"id": "1784", "username": "tester", "media_count": 3}),
        ("GET", GRAPH + r"1784/media\?", {"data": [{"id": "m1"}]}),
        ("POST", GRAPH + r"1784/media$", {"id": "c55"}),
        ("GET", GRAPH + r"c55\?", lambda _r: {"status_code": next(statuses)}),
    ])
    results = run_all(ig)

    assert set(results) == {"account", "recent_media", "unpublished_container"}
    body = rec.body("POST", r"/1784/media$")
    assert body["image_url"].startswith("https://") and "Synaprank.ai verification" in body["caption"]
    assert len(rec.find("GET", r"/c55\?")) == 2
    assert not rec.find("POST", r"media_publish")
    assert results["unpublished_container"][1] == {"container_id": "c55", "published": False}
    write = _steps(ig)["unpublished_container"]
    assert write.requires_test_account and "24 hours" in write.label
    assert_no_secrets(ig, results)


# ── LinkedIn Pages ─────────────────────────────────────────────────────────
LI = r"https://api\.linkedin\.com/rest/"
LI_URN = "urn:li:share:777"
LI_URN_PATH = "urn%3Ali%3Ashare%3A777"


def _linkedin_routes(*, read_back: object = None) -> list:
    return [
        ("GET", LI + r"organizationAcls\?", {"elements": [
            {"role": "ADMINISTRATOR", "state": "APPROVED", "organization": "urn:li:organization:42"},
        ]}),
        ("GET", LI + r"organizations/42$", {"localizedName": "Test Co"}),
        ("GET", LI + r"posts\?author=", {"elements": [{"id": "urn:li:share:1"}]}),
        ("POST", LI + r"posts$", lambda _r: (201, b"")),
        ("GET", LI + "posts/" + LI_URN_PATH, read_back or {"id": LI_URN, "author": "urn:li:organization:42"}),
        ("DELETE", LI + "posts/" + LI_URN_PATH, (204, None)),
    ]


def _install_linkedin(li, routes):  # noqa: ANN001, ANN202
    rec = install(li, routes)
    original = rec.__call__

    def with_header(request):  # noqa: ANN001, ANN202
        response = original(request)
        if request.method == "POST" and request.url.path.endswith("/posts"):
            response.headers["x-restli-id"] = LI_URN
        return response

    li._client._transport.handler = with_header
    return rec


def test_linkedin_checks_role_reads_posts_and_deletes_its_post():
    li = _connector("linkedin_pages", {"organizationId": "42"})
    rec = _install_linkedin(li, _linkedin_routes())
    results = run_all(li)

    assert set(results) == {"page_role", "recent_posts", "public_post_roundtrip"}
    post = rec.body("POST", r"/rest/posts$")
    assert post["author"] == "urn:li:organization:42" and post["lifecycleState"] == "PUBLISHED"
    read_back = rec.find("GET", LI + "posts/" + LI_URN_PATH)[0]
    assert read_back.url.params["viewContext"] == "AUTHOR"
    delete = rec.find("DELETE", LI + "posts/" + LI_URN_PATH)[0]
    assert delete.headers["X-RestLi-Method"] == "DELETE"
    assert rec.index("POST", r"/rest/posts$") < rec.index("DELETE", LI + "posts/")
    assert results["public_post_roundtrip"][1] == {"created_id": LI_URN, "deleted": True}
    label = _steps(li)["public_post_roundtrip"].label.lower()
    assert "publicly" in label and "delete it immediately" in label
    assert_no_secrets(li, results)


def test_linkedin_refuses_a_page_the_sign_in_cannot_post_to():
    li = _connector("linkedin_pages", {"organizationId": "99"})
    _install_linkedin(li, _linkedin_routes())
    with pytest.raises(ConnectorConfigError, match="99"):
        _steps(li)["page_role"].run()


def test_linkedin_deletes_the_post_even_when_the_read_back_fails():
    li = _connector("linkedin_pages", {"organizationId": "42"})
    rec = _install_linkedin(li, _linkedin_routes(read_back=(404, {"message": "gone"})))
    with pytest.raises(ConnectorError, match="removed"):
        _steps(li)["public_post_roundtrip"].run()
    assert rec.find("DELETE", LI + "posts/" + LI_URN_PATH)


# ── X ──────────────────────────────────────────────────────────────────────
X = r"https://api\.x\.com/2/"


def _x_routes() -> list:
    return [
        ("GET", X + r"users/me", {"data": {"id": "9", "username": "tester"}}),
        ("POST", X + r"tweets$", {"data": {"id": "1234"}}),
        ("DELETE", X + r"tweets/1234$", {"data": {"deleted": True}}),
    ]


def test_x_post_is_refused_without_the_explicit_opt_in():
    x = _connector("x_twitter", {})
    rec = install(x, _x_routes())
    steps = _steps(x)
    assert steps["account"].run()[1] == {"user_id": "9"}
    from app.connectors.base.verification import VerifySkipped

    # Skipped by choice, not a failure: the runner records it as skipped.
    with pytest.raises(VerifySkipped, match="verifyAllowPaidPost"):
        steps["paid_public_post_roundtrip"].run()
    assert not rec.find("POST", r"tweets")
    label = steps["paid_public_post_roundtrip"].label.lower()
    assert "publicly" in label and "costs money" in label and "delete it immediately" in label
    assert steps["paid_public_post_roundtrip"].requires_test_account


def test_x_posts_and_deletes_when_opted_in():
    x = _connector("x_twitter", {"verifyAllowPaidPost": "yes"})
    rec = install(x, _x_routes())
    results = run_all(x)
    assert json.loads(rec.find("POST", r"tweets$")[0].content)["text"].startswith("Synaprank.ai verification")
    assert rec.index("POST", r"tweets$") < rec.index("DELETE", r"tweets/1234$")
    assert results["paid_public_post_roundtrip"][1] == {"created_id": "1234", "deleted": True}
    assert_no_secrets(x, results)


# ── Pinterest ──────────────────────────────────────────────────────────────
def _pinterest_routes(host: str, *, pin_back: object = None) -> list:
    return [
        ("GET", host + r"user_account", {"username": "tester", "account_type": "BUSINESS"}),
        ("GET", host + r"boards/555$", {"id": "555", "name": "Main", "pin_count": 4}),
        ("POST", host + r"boards$", {"id": "900"}),
        ("POST", host + r"pins$", {"id": "p1"}),
        ("GET", host + r"pins/p1$", pin_back or {"id": "p1", "board_id": "900"}),
        ("DELETE", host + r"pins/p1$", (204, None)),
        ("DELETE", host + r"boards/900$", (204, None)),
    ]


def test_pinterest_secret_board_and_pin_are_made_and_removed_away_from_the_chosen_board():
    pin = _connector("pinterest", {"boardId": "555"})
    rec = install(pin, _pinterest_routes(r"https://api\.pinterest\.com/v5/"))
    results = run_all(pin)

    assert set(results) == {"account", "board", "secret_board_pin_roundtrip"}
    assert rec.body("POST", r"/boards$")["privacy"] == "SECRET"
    body = rec.body("POST", r"/pins$")
    assert body["board_id"] == "900"  # never the chosen board 555
    assert body["media_source"]["source_type"] == "image_url"
    order = [
        rec.index("POST", r"/boards$"), rec.index("POST", r"/pins$"), rec.index("GET", r"/pins/p1$"),
        rec.index("DELETE", r"/pins/p1$"), rec.index("DELETE", r"/boards/900$"),
    ]
    assert order == sorted(order)
    assert results["secret_board_pin_roundtrip"][1] == {"board_id": "900", "pin_id": "p1", "deleted": True}
    assert_no_secrets(pin, results)


def test_pinterest_removes_pin_and_board_when_the_pin_read_back_fails():
    pin = _connector("pinterest", {"boardId": "555"})
    rec = install(pin, _pinterest_routes(r"https://api\.pinterest\.com/v5/", pin_back={"id": "p1", "board_id": "1"}))
    with pytest.raises(ConnectorError, match="removed"):
        _steps(pin)["secret_board_pin_roundtrip"].run()
    assert rec.find("DELETE", r"/pins/p1$") and rec.find("DELETE", r"/boards/900$")


@pytest.mark.parametrize(
    ("typed", "host", "environment"),
    [
        ("", "https://api.pinterest.com/v5", "live"),
        ("Test account", "https://api.pinterest.com/v5", "test_account"),
        ("Sandbox", "https://api-sandbox.pinterest.com/v5", "sandbox"),
    ],
)
def test_pinterest_sandbox_switches_the_host(typed: str, host: str, environment: str):
    pin = _connector("pinterest", {"boardId": "555", "accountEnvironment": typed})
    assert pin.account_environment() == environment
    assert pin.base_url == host
    assert pin.token_url == host + "/oauth/token"


def test_pinterest_sandbox_calls_go_to_the_sandbox_host():
    pin = _connector("pinterest", {"boardId": "555", "accountEnvironment": "sandbox"})
    rec = install(pin, _pinterest_routes(r"https://api-sandbox\.pinterest\.com/v5/"))
    run_all(pin)
    assert all(str(c.url).startswith("https://api-sandbox.pinterest.com/v5/") for c in rec.calls)


def test_pinterest_offers_the_test_account_setting():
    keys = [f.key for f in registry.get_class("pinterest").spec.fields]
    assert keys[-1] == "accountEnvironment" and keys[:2] == ["boardId", "accessToken"]
