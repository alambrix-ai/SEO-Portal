"""Live checks (``verify_steps``) for the CMS group: Shopify, Webflow, Magento,
GitHub, Bitbucket, Custom API and Webhooks / Zapier.

Each runs against a recording fake vendor (tests/support/verify_http) to prove
the request shapes and, for write steps, that whatever is created is removed
again in the same step — also when the read-back fails.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

import pytest

from app.connectors.base import registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError
from tests.support.verify_http import assert_no_secrets, install, run_all

SLUGS = ("shopify", "webflow", "magento", "github", "bitbucket", "custom_api", "zapier_webhooks")


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _step(connector, key: str):  # noqa: ANN001, ANN202
    return next(s for s in connector.verify_steps() if s.key == key)


@pytest.mark.parametrize("slug", SLUGS)
def test_each_declares_reads_and_one_write(slug: str):
    steps = _connector(slug, {}).verify_steps()
    kinds = [s.kind for s in steps]
    assert "read" in kinds and kinds.count("write") == 1, slug


@pytest.mark.parametrize(
    ("slug", "needs_test"),
    [
        ("shopify", False), ("webflow", False), ("magento", False),  # hidden drafts
        ("github", True), ("bitbucket", True), ("custom_api", True), ("zapier_webhooks", True),
    ],
)
def test_which_writes_need_a_test_account(slug: str, needs_test: bool):
    write = next(s for s in _connector(slug, {}).verify_steps() if s.kind == "write")
    assert write.requires_test_account is needs_test


# ── Shopify ────────────────────────────────────────────────────────────────
SHOP = {"storeDomain": "demo.myshopify.com", "adminToken": "shpat_secret_value", "blogId": "77"}


def _shop_routes(article_state: dict | None = None) -> list:
    article = {"id": 555, "published_at": None, **(article_state or {})}
    return [
        ("GET", r"/admin/api/[\d-]+/shop\.json$", {"shop": {"id": 1, "name": "Demo"}}),
        ("GET", r"/pages\.json\?limit=", {"pages": [{"id": 11, "handle": "about", "body_html": "<p>a b</p>"}]}),
        ("GET", r"/products\.json\?limit=", {"products": []}),
        ("GET", r"/pages/11\.json$", {"page": {"id": 11, "handle": "about", "body_html": "<p>a b c</p>"}}),
        ("GET", r"/blogs\.json$", {"blogs": [{"id": 77, "title": "News", "handle": "news"}]}),
        ("POST", r"/blogs/77/articles\.json$", {"article": {"id": 555}}),
        ("GET", r"/blogs/77/articles/555\.json$", {"article": article}),
        ("DELETE", r"/blogs/77/articles/555\.json$", {}),
    ]


def test_shopify_reads_then_saves_and_deletes_a_hidden_article():
    shop = _connector("shopify", SHOP)
    rec = install(shop, _shop_routes())
    results = run_all(shop)
    assert set(results) == {"store_info", "list_pages", "read_page", "list_blogs", "hidden_article_roundtrip"}
    assert all(c.url.host == "demo.myshopify.com" for c in rec.calls)
    assert all("/admin/api/" in c.url.path for c in rec.calls)
    created = rec.body("POST", r"/blogs/77/articles\.json$")["article"]
    assert created["published"] is False and created["title"].startswith(vh.VERIFY_LABEL)
    assert rec.index("POST", r"/articles\.json$") < rec.index("DELETE", r"/articles/555\.json$")
    assert results["hidden_article_roundtrip"][1]["created_id"] == "555"
    assert results["list_blogs"][1] == {"count": 1, "ids": ["77"]}
    assert rec.find("GET", r"/pages\.json")[0].url.params["limit"] == "5"
    assert_no_secrets(shop, results)


def test_shopify_article_is_deleted_even_when_it_comes_back_published():
    shop = _connector("shopify", SHOP)
    rec = install(shop, _shop_routes({"published_at": "2026-10-12T10:00:00Z"}))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _step(shop, "hidden_article_roundtrip").run()
    assert rec.find("DELETE", r"/blogs/77/articles/555\.json$")


def test_shopify_test_account_switch():
    fields = {f.key: f for f in registry.get_class("shopify").spec.fields}
    env = fields["accountEnvironment"]
    assert env.label == "Test account" and not env.required
    assert "development store" in env.help_text.lower()
    live = _connector("shopify", SHOP)
    test = _connector("shopify", {**SHOP, "accountEnvironment": "Test account"})
    assert live.account_environment() == "live"
    assert test.account_environment() == "test_account"
    # A development store is a normal store: same Admin API address.
    assert test.base_url == live.base_url == "https://demo.myshopify.com/admin/api/2026-10"
    assert _connector("shopify", {**SHOP, "accountEnvironment": "sandbox"}).account_environment() == "live"
    # The store-address sign-in is untouched.
    oauth = registry.get_class("shopify").spec.oauth
    assert [i.key for i in oauth.sign_in_inputs] == ["shop"]
    assert "accountEnvironment" not in oauth.platform_fields


# ── Webflow ────────────────────────────────────────────────────────────────
WF = {
    "siteId": "site1",
    "siteToken": "wf_secret_token_value",
    "articlesCollectionId": "c9",
    "articlesBodyField": "post-body",
}


def _wf_routes(draft: bool = True) -> list:
    posted: dict = {}

    def create(request):  # noqa: ANN001, ANN202
        posted.update(json.loads(request.content))
        return {"id": "it9"}

    def read(_request):  # noqa: ANN001, ANN202
        return {"id": "it9", "isDraft": draft, "fieldData": posted.get("fieldData") or {}}

    return [
        ("GET", r"/v2/sites/site1/collections$", {"collections": [{"id": "c1", "slug": "blog"}]}),
        ("GET", r"/v2/collections/c1/items\?", {
            "items": [{"id": "i1", "fieldData": {"slug": "hello", "name": "Hello", "post-body": "<p>x y</p>"}}],
            "pagination": {"total": 1},
        }),
        ("GET", r"/v2/collections/c1/items/i1$", {"id": "i1", "fieldData": {"slug": "hello", "post-body": "<p>x</p>"}}),
        ("POST", r"/v2/collections/c9/items$", create),
        ("GET", r"/v2/collections/c9/items/it9$", read),
        ("DELETE", r"/v2/collections/c9/items/it9$", (204, None)),
    ]


def test_webflow_reads_then_saves_and_deletes_a_draft_item_without_publishing():
    wf = _connector("webflow", WF)
    rec = install(wf, _wf_routes())
    results = run_all(wf)
    assert set(results) == {"list_collections", "list_pages", "read_page", "draft_item_roundtrip"}
    assert all(c.url.host == "api.webflow.com" for c in rec.calls)
    body = rec.body("POST", r"/collections/c9/items$")
    assert body["isDraft"] is True and body["fieldData"]["name"].startswith(vh.VERIFY_LABEL)
    assert "post-body" in body["fieldData"]
    assert rec.index("POST", r"/c9/items$") < rec.index("DELETE", r"/c9/items/it9$")
    assert not rec.find("POST", r"/publish")
    assert results["draft_item_roundtrip"][1] == {"created_id": "c9:it9", "deleted": True}
    assert_no_secrets(wf, results)


def test_webflow_item_is_deleted_even_when_it_is_not_a_draft():
    wf = _connector("webflow", WF)
    rec = install(wf, _wf_routes(draft=False))
    with pytest.raises(ConnectorError):
        _step(wf, "draft_item_roundtrip").run()
    assert rec.find("DELETE", r"/c9/items/it9$")


def test_webflow_write_needs_an_articles_collection():
    wf = _connector("webflow", {**WF, "articlesCollectionId": ""})
    install(wf, [])
    with pytest.raises(ConnectorConfigError):
        _step(wf, "draft_item_roundtrip").run()


# ── Magento ────────────────────────────────────────────────────────────────
MAG = {"baseUrl": "https://shop.example.com", "accessToken": "mag_secret_token"}


def _mag_routes(active: bool = False) -> list:
    return [
        ("GET", r"/rest/V1/store/storeViews$", [{"id": 1, "code": "default"}]),
        ("GET", r"/rest/V1/cmsPage/search\?", {"items": [{"id": 3, "identifier": "about", "content": "<p>a</p>"}], "total_count": 1}),
        ("GET", r"/rest/V1/cmsPage/3$", {"id": 3, "identifier": "about", "content": "<p>a b</p>"}),
        ("POST", r"/rest/V1/cmsPage$", {"id": 44}),
        ("GET", r"/rest/V1/cmsPage/44$", {"id": 44, "active": active}),
        ("DELETE", r"/rest/V1/cmsPage/44$", "true"),
    ]


def test_magento_reads_then_saves_and_deletes_a_disabled_page():
    mag = _connector("magento", MAG)
    rec = install(mag, _mag_routes())
    results = run_all(mag)
    assert set(results) == {"store_views", "list_pages", "read_page", "disabled_page_roundtrip"}
    assert all(c.url.host == "shop.example.com" for c in rec.calls)
    page = rec.body("POST", r"/rest/V1/cmsPage$")["page"]
    assert page["active"] is False and page["title"].startswith(vh.VERIFY_LABEL)
    assert page["identifier"].startswith("synaprank-verify-")
    assert rec.index("POST", r"/cmsPage$") < rec.index("DELETE", r"/cmsPage/44$")
    assert results["disabled_page_roundtrip"][1] == {"created_id": "44", "deleted": True}
    assert_no_secrets(mag, results)


def test_magento_page_is_deleted_even_when_it_comes_back_enabled():
    mag = _connector("magento", MAG)
    rec = install(mag, _mag_routes(active=True))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _step(mag, "disabled_page_roundtrip").run()
    assert rec.find("DELETE", r"/cmsPage/44$")


# ── GitHub ─────────────────────────────────────────────────────────────────
GH = {"repository": "acme/site", "token": "github_pat_secret_value", "branch": "main", "contentPath": "content"}
BRANCH = r"synaprank-verify-\d{8}-\d{6}"


def _gh_routes(*, pr_state: str = "open", commit_fails: bool = False) -> list:
    return [
        ("GET", r"/repos/acme/site$", {"full_name": "acme/site", "permissions": {"push": True}}),
        ("GET", r"/repos/acme/site/git/ref/heads/main$", {"object": {"sha": "abc123def4567890"}}),
        ("GET", r"/git/trees/main\?recursive=1", {"tree": [
            {"type": "blob", "path": "content/about.md", "sha": "s1", "size": 10},
            {"type": "blob", "path": "README.md", "sha": "s2", "size": 5},
        ]}),
        ("POST", r"/repos/acme/site/git/refs$", (201, {"ref": "refs/heads/x"})),
        ("PUT", r"/repos/acme/site/contents/synaprank-verification/",
         (409, {"message": "conflict"}) if commit_fails else (201, {"content": {}})),
        ("POST", r"/repos/acme/site/pulls$", (201, {"number": 12, "html_url": "https://github.com/acme/site/pull/12"})),
        ("GET", r"/repos/acme/site/pulls/12$", {"number": 12, "state": pr_state}),
        ("PATCH", r"/repos/acme/site/pulls/12$", {"number": 12, "state": "closed"}),
        ("DELETE", rf"/repos/acme/site/git/refs/heads/{BRANCH}$", (204, None)),
    ]


def test_github_reads_then_opens_closes_and_cleans_up_a_pull_request():
    gh = _connector("github", GH)
    rec = install(gh, _gh_routes())
    results = run_all(gh)
    assert set(results) == {"repository", "branch_head", "list_pages", "pull_request_roundtrip"}
    assert all(c.url.host == "api.github.com" for c in rec.calls)
    ref = rec.body("POST", r"/git/refs$")
    assert re.fullmatch(rf"refs/heads/{BRANCH}", ref["ref"]) and ref["sha"] == "abc123def4567890"
    branch = ref["ref"].removeprefix("refs/heads/")
    commit = rec.body("PUT", r"/contents/")
    assert commit["branch"] == branch
    assert rec.find("PUT", r"/contents/")[0].url.path.endswith(f"synaprank-verification/{branch}.md")
    pr = rec.body("POST", r"/pulls$")
    assert pr["title"].startswith(vh.VERIFY_LABEL) and pr["head"] == branch and pr["base"] == "main"
    assert rec.body("PATCH", r"/pulls/12$") == {"state": "closed"}
    order = [rec.index("POST", r"/git/refs$"), rec.index("POST", r"/pulls$"),
             rec.index("PATCH", r"/pulls/12$"), rec.index("DELETE", r"/git/refs/heads/")]
    assert order == sorted(order)
    evidence = results["pull_request_roundtrip"][1]
    assert evidence["pull_request"] == "12" and evidence["pull_request_closed"] and evidence["branch_deleted"]
    assert results["list_pages"][1]["paths"] == ["content/about.md"]
    assert_no_secrets(gh, results)


def test_github_closes_and_deletes_even_when_the_pull_request_reads_back_wrong():
    gh = _connector("github", GH)
    rec = install(gh, _gh_routes(pr_state="closed"))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _step(gh, "pull_request_roundtrip").run()
    assert rec.find("PATCH", r"/pulls/12$")
    assert rec.index("PATCH", r"/pulls/12$") < rec.index("DELETE", r"/git/refs/heads/")


def test_github_branch_is_deleted_when_the_commit_fails():
    gh = _connector("github", GH)
    rec = install(gh, _gh_routes(commit_fails=True))
    with pytest.raises(ConnectorError):
        _step(gh, "pull_request_roundtrip").run()
    assert not rec.find("POST", r"/pulls$")
    assert rec.find("DELETE", rf"/git/refs/heads/{BRANCH}$")


# ── Bitbucket ──────────────────────────────────────────────────────────────
BB = {"repository": "ws/site", "token": "bb_access_token_value", "branch": "main", "contentPath": "content"}
REPO = r"/2\.0/repositories/ws/site"


def _bb_routes(*, branch_gone: bool = False) -> list:
    return [
        ("GET", rf"{REPO}$", {"full_name": "ws/site", "mainbranch": {"name": "main"}}),
        ("GET", rf"{REPO}/refs/branches/main$", {"target": {"hash": "feedface12345678"}}),
        ("GET", rf"{REPO}/src/main/content\?", {"values": [
            {"type": "commit_file", "path": "content/about.md", "size": 3},
        ]}),
        ("POST", rf"{REPO}/refs/branches$", (201, {"name": "x"})),
        ("POST", rf"{REPO}/src$", (201, None)),
        ("POST", rf"{REPO}/pullrequests$", (201, {
            "id": 9, "links": {"html": {"href": "https://bitbucket.org/ws/site/pull-requests/9"}},
        })),
        ("GET", rf"{REPO}/pullrequests/9$", {"id": 9, "state": "OPEN"}),
        ("POST", rf"{REPO}/pullrequests/9/decline$", {"id": 9, "state": "DECLINED"}),
        ("DELETE", rf"{REPO}/refs/branches/{BRANCH}$", (404, {}) if branch_gone else (204, None)),
    ]


def test_bitbucket_reads_then_opens_declines_and_cleans_up_a_pull_request():
    bb = _connector("bitbucket", BB)
    rec = install(bb, _bb_routes())
    results = run_all(bb)
    assert set(results) == {"repository", "branch_head", "list_pages", "pull_request_roundtrip"}
    assert all(c.url.host == "api.bitbucket.org" for c in rec.calls)
    created = rec.body("POST", rf"{REPO}/refs/branches$")
    assert re.fullmatch(BRANCH, created["name"]) and created["target"] == {"hash": "feedface12345678"}
    form = rec.body("POST", rf"{REPO}/src$")
    assert f"synaprank-verification%2F{created['name']}.md" in form and f"branch={created['name']}" in form
    pr = rec.body("POST", rf"{REPO}/pullrequests$")
    assert pr["title"].startswith(vh.VERIFY_LABEL)
    assert pr["source"]["branch"]["name"] == created["name"] and pr["destination"]["branch"]["name"] == "main"
    order = [rec.index("POST", rf"{REPO}/refs/branches$"), rec.index("POST", rf"{REPO}/pullrequests$"),
             rec.index("POST", r"/decline$"), rec.index("DELETE", rf"{REPO}/refs/branches/")]
    assert order == sorted(order)
    assert results["pull_request_roundtrip"][1]["pull_request"] == "9"
    assert_no_secrets(bb, results)


def test_bitbucket_branch_already_gone_after_decline_is_fine():
    bb = _connector("bitbucket", BB)
    install(bb, _bb_routes(branch_gone=True))
    detail, evidence = _step(bb, "pull_request_roundtrip").run()
    assert evidence["branch_deleted"] is True


# ── Custom API ─────────────────────────────────────────────────────────────
CA = {"webhookUrl": "https://api.example.com/automarket", "secret": "shared_secret_value"}
PAGE = {"id": "p1", "url": "/about", "title": "About", "body": "<p>About us</p>"}


def test_custom_api_reads_then_sends_one_signed_unchanged_write():
    ca = _connector("custom_api", CA)
    rec = install(ca, [
        ("GET", r"/automarket/health$", {"ok": True}),
        ("GET", r"/automarket/pages\?", {"pages": [PAGE]}),
        ("GET", r"/automarket/pages/p1$", PAGE),
        ("PUT", r"/automarket/pages/p1$", {"ok": True}),
    ])
    results = run_all(ca)
    assert set(results) == {"health", "list_pages", "read_page", "signed_unchanged_write"}
    put = rec.find("PUT", r"/pages/p1$")[0]
    sent = json.loads(put.content)
    assert sent["body"] == PAGE["body"] and sent["title"] == PAGE["title"] and sent["test"] is True
    assert sent["note"].startswith(vh.VERIFY_LABEL)
    stamp = put.headers["X-AutoMarket-Timestamp"]
    expected = hmac.new(b"shared_secret_value", f"{stamp}.".encode() + put.content, hashlib.sha256).hexdigest()
    assert put.headers["X-AutoMarket-Signature"] == f"sha256={expected}"
    assert_no_secrets(ca, results)


def test_custom_api_write_fails_when_the_page_reads_back_changed():
    ca = _connector("custom_api", CA)
    reads = iter([PAGE, {**PAGE, "body": "<p>Changed</p>"}])
    install(ca, [
        ("GET", r"/pages\?", {"pages": [PAGE]}),
        ("GET", r"/pages/p1$", lambda _r: next(reads)),
        ("PUT", r"/pages/p1$", {}),
    ])
    with pytest.raises(ConnectorError, match="reads differently"):
        _step(ca, "signed_unchanged_write").run()


# ── Webhooks / Zapier ──────────────────────────────────────────────────────
ZAP = {"webhookUrl": "https://hooks.zapier.com/hooks/catch/1/abc/", "signingSecret": "zap_signing_secret"}


def test_zapier_checks_the_address_offline_then_sends_a_marked_test_event():
    zap = _connector("zapier_webhooks", ZAP)
    rec = install(zap, [("POST", r"^https://hooks\.zapier\.com/hooks/catch/1/abc/?$", "ok")])
    detail, evidence = _step(zap, "check_address").run()
    assert not rec.calls and evidence == {"zapier": True, "signed": True}
    results = run_all(zap)
    assert len(rec.calls) == 1
    call = rec.calls[0]
    body = json.loads(call.content)
    assert body["test"] is True and body["subject"] == "Synaprank.ai verification ping"
    stamp = call.headers["X-AutoMarket-Timestamp"]
    expected = hmac.new(b"zap_signing_secret", f"{stamp}.".encode() + call.content, hashlib.sha256).hexdigest()
    assert call.headers["X-AutoMarket-Signature"] == f"sha256={expected}"
    assert_no_secrets(zap, results)


@pytest.mark.parametrize(
    "url", ["http://example.com/hook", "", "https://hooks.zapier.com/not-a-catch-hook"]
)
def test_zapier_address_check_refuses_bad_urls(url: str):
    zap = _connector("zapier_webhooks", {"webhookUrl": url})
    rec = install(zap, [])
    with pytest.raises(ConnectorConfigError):
        _step(zap, "check_address").run()
    assert not rec.calls
