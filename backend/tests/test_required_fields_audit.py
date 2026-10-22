"""Fields that the vendors' current docs do not require are optional here too.

Each field below used to be required in the connect form. For each one, the
connector now works with it left blank: it uses a documented default, reads
the value from the vendor's API, or leaves out something the API itself treats
as optional. These tests check that a blank value never fails at run time and
that ``missing_credentials`` no longer reports it.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError
from app.services.connectors import missing_credentials

#: slug -> the fields this audit made optional.
RELAXED: dict[str, tuple[str, ...]] = {
    "github": ("branch",),
    "bitbucket": ("branch",),
    "pagespeed_insights": ("apiKey", "strategy"),
    "site_crawler": ("discovery",),
    "smtp_email": ("port", "fromName"),
    "mailchimp": ("fromName", "replyTo"),
    "brevo": ("senderName",),
    "hubspot": ("hubId", "wonStageId"),
    "google_places": ("radiusKm",),
    "google_ads": ("apiVersion",),
    "salesforce": ("instanceUrl",),
    "adobe_analytics": ("orgId", "globalCompanyId"),
    "facebook_pages": ("pageId",),
}


def _make(slug: str, values: dict, handler):  # noqa: ANN001, ANN202
    klass = registry.get_class(slug)
    assert klass is not None, slug
    connector = klass(Credentials(values=values), org_id="o")
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


# ── The spec and missing_credentials ───────────────────────────────────────
@pytest.mark.parametrize(("slug", "keys"), sorted(RELAXED.items()))
def test_relaxed_fields_are_optional_and_never_reported_missing(slug: str, keys: tuple[str, ...]):
    spec = registry.get_spec(slug)
    assert spec is not None, slug
    fields = {f.key: f for f in spec.fields}

    class Record:
        pass

    Record.slug = slug
    missing = missing_credentials(Record, Credentials(values={}))
    for key in keys:
        assert key in fields, f"{slug} no longer declares {key}"
        assert not fields[key].required, f"{slug}.{key} is still required"
        assert key not in missing, f"{slug}.{key} is still reported missing"
        assert "blank" in fields[key].help_text.lower() or "optional" in fields[key].help_text.lower(), (
            f"{slug}.{key} help text does not say what blank does"
        )


# ── Git: the repository's own default branch ──────────────────────────────
def test_github_blank_branch_uses_the_repository_default_branch():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/repos/acme/site":
            return httpx.Response(200, json={"default_branch": "trunk", "full_name": "acme/site"})
        return httpx.Response(200, json={"object": {"sha": "abc"}})

    github = _make("github", {"repository": "acme/site", "token": "t"}, handler)
    assert github.branch == "trunk"
    assert github.branch == "trunk"  # read once, not per use
    assert [r.url.path for r in seen] == ["/repos/acme/site"]
    assert github._branch_head() == "abc"
    assert seen[-1].url.path == "/repos/acme/site/git/ref/heads/trunk"


def test_github_configured_branch_is_used_without_a_lookup():
    github = _make(
        "github",
        {"repository": "acme/site", "token": "t", "branch": "deploy"},
        lambda r: pytest.fail("no lookup expected"),
    )
    assert github.branch == "deploy"


def test_bitbucket_blank_branch_uses_the_main_branch():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"mainbranch": {"name": "master"}})

    bitbucket = _make("bitbucket", {"repository": "ws/site", "token": "t"}, handler)
    assert bitbucket.branch == "master"
    assert seen[0].url.path == "/2.0/repositories/ws/site"
    assert _query(seen[0])["fields"] == ["mainbranch.name"]


# ── PageSpeed Insights: no key, mobile ─────────────────────────────────────
def test_pagespeed_runs_without_a_key_or_strategy():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"loadingExperience": {}})

    speed = _make("pagespeed_insights", {}, handler)
    vitals = speed.read_vitals("https://a.test/")
    assert vitals.url == "https://a.test/"
    query = _query(seen[0])
    assert query["strategy"] == ["mobile"]
    assert "key" not in query
    assert speed.check_health().ok


def test_pagespeed_sends_an_entered_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    _make("pagespeed_insights", {"apiKey": "AIzaX", "strategy": "desktop"}, handler).read_vitals("https://a.test/")
    assert _query(seen[0])["key"] == ["AIzaX"]
    assert _query(seen[0])["strategy"] == ["desktop"]


# ── Site crawler ───────────────────────────────────────────────────────────
def test_site_crawler_blank_discovery_is_auto():
    crawler = _make("site_crawler", {"siteUrl": "https://a.test"}, lambda r: httpx.Response(404))
    assert crawler.discovery == "auto"


# ── SMTP: port 587 and an address without a display name ──────────────────
def test_smtp_blank_port_and_from_name(monkeypatch):
    import smtplib

    sent: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):  # noqa: ANN001
            sent["host"], sent["port"] = host, port

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        def starttls(self, context=None):  # noqa: ANN001, ANN201
            sent["tls"] = True

        def login(self, user, password):  # noqa: ANN001, ANN201
            sent["user"] = user

        def send_message(self, message):  # noqa: ANN001, ANN201
            sent["from"] = message["From"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    klass = registry.get_class("smtp_email")
    smtp = klass(
        Credentials(values={"host": "mail.a.test", "user": "bot@a.test", "password": "p", "notifyTo": "x@a.test"}),
        org_id="o",
    )
    assert smtp.port == 587
    assert smtp.notify(subject="s", message="m") is True
    assert sent == {"host": "mail.a.test", "port": 587, "tls": True, "user": "bot@a.test", "from": "bot@a.test"}
    assert smtp.check_health().ok


# ── Mailchimp: the audience's campaign defaults ────────────────────────────
def test_mailchimp_blank_sender_uses_the_audience_defaults():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/3.0/lists/aud1":
            return httpx.Response(
                200, json={"campaign_defaults": {"from_name": "Acme", "from_email": "news@acme.test"}}
            )
        if request.method == "POST":
            return httpx.Response(200, json={"id": "c1", "web_id": 9})
        return httpx.Response(200, json={})

    chimp = _make("mailchimp", {"apiKey": "0123456789abcdef" + "0123456789abcdef-us21", "audienceId": "aud1"}, handler)
    campaign_id, url = chimp.create_campaign_draft(
        name="n", subject="s", preview_text="p", html="<p>x</p>", text="x", from_name="", reply_to=""
    )
    assert campaign_id == "c1" and "us21" in url
    created = json.loads(next(r for r in seen if r.method == "POST").content)
    assert created["settings"]["from_name"] == "Acme"
    assert created["settings"]["reply_to"] == "news@acme.test"


def test_mailchimp_entered_sender_needs_no_lookup():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "c1"})

    chimp = _make(
        "mailchimp",
        {"apiKey": "0123456789abcdef" + "0123456789abcdef-us21", "audienceId": "aud1", "fromName": "F", "replyTo": "r@a.test"},
        handler,
    )
    chimp.create_campaign_draft(name="n", subject="s", preview_text="", html="", text="", from_name="", reply_to="")
    assert not any(r.method == "GET" for r in seen)
    settings_ = json.loads(seen[0].content)["settings"]
    assert settings_["from_name"] == "F" and settings_["reply_to"] == "r@a.test"


# ── Brevo: a sender is an address; the name is optional ────────────────────
def test_brevo_blank_sender_name_sends_the_address_only():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 5})

    brevo = _make("brevo", {"apiKey": "xkeysib-1", "senderEmail": "hi@a.test"}, handler)
    brevo.create_campaign_draft(
        name="n", subject="s", preview_text="", html="<p>x</p>", text="x", from_name="", reply_to=""
    )
    body = json.loads(seen[0].content)
    assert body["sender"] == {"email": "hi@a.test"}


# ── HubSpot: the account's closed-won stages ───────────────────────────────
PIPELINES = {
    "results": [
        {
            "id": "default",
            "label": "Sales",
            "stages": [
                {"id": "appointmentscheduled", "metadata": {"isClosed": "false", "probability": "0.2"}},
                {"id": "closedwon", "metadata": {"isClosed": "true", "probability": "1.0"}},
                {"id": "closedlost", "metadata": {"isClosed": "true", "probability": "0.0"}},
            ],
        },
        {
            "id": "p2",
            "label": "Renewals",
            "stages": [{"id": "renewal_won", "metadata": {"isClosed": "true", "probability": "1.0"}}],
        },
    ]
}


def _hubspot(values: dict, pipelines: dict) -> tuple:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json=pipelines)
        return httpx.Response(200, json={"total": 4})

    return _make("hubspot", values, handler), seen


def test_hubspot_blank_won_stage_counts_every_pipelines_won_stage():
    hubspot, seen = _hubspot({"accessToken": "k"}, PIPELINES)
    assert hubspot.read_conversions(days=7) == 4
    search = json.loads(seen[-1].content)
    stage = search["filterGroups"][0]["filters"][0]
    assert stage == {"propertyName": "dealstage", "operator": "IN", "values": ["closedwon", "renewal_won"]}


def test_hubspot_one_won_stage_uses_eq():
    one = {"results": [PIPELINES["results"][0]]}
    hubspot, seen = _hubspot({"accessToken": "k"}, one)
    hubspot.read_conversions(days=7)
    stage = json.loads(seen[-1].content)["filterGroups"][0]["filters"][0]
    assert stage == {"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"}


def test_hubspot_entered_won_stage_needs_no_lookup():
    hubspot, seen = _hubspot({"accessToken": "k", "wonStageId": "custom"}, PIPELINES)
    hubspot.read_conversions(days=7)
    assert [r.url.path for r in seen] == ["/crm/v3/objects/deals/search"]


# ── Google Places and Google Ads defaults ──────────────────────────────────
def test_google_places_blank_radius_is_three_km():
    places = registry.get_class("google_places")(Credentials(values={"apiKey": "k", "businessName": "B"}), org_id="o")
    assert places.radius_m == 3000


def test_google_ads_blank_api_version_uses_the_pinned_current_version():
    from app.connectors.google_ads.connector import DEFAULT_API_VERSION

    ads = registry.get_class("google_ads")(Credentials(values={"customerId": "1"}), org_id="o")
    assert ads.api_version == DEFAULT_API_VERSION == "v25"


# ── Salesforce: instance_url comes back with the token ─────────────────────
def test_salesforce_blank_instance_url_is_read_from_the_token_response():
    klass = registry.get_class("salesforce")
    sf = klass(
        Credentials(values={"refreshToken": "r", "clientId": "c", "clientSecret": "s"}), org_id=""
    )
    calls: list[str] = []

    def refresh(token: str) -> dict:
        calls.append(token)
        return {"access_token": "at", "instance_url": "https://acme.my.salesforce.com", "expires_in": 3600}

    sf._refresh_call = refresh
    assert sf.base_url == "https://acme.my.salesforce.com"
    assert sf.access_token == "at"
    assert calls == ["r"]  # the same exchange served both

    sf._client = httpx.Client(
        base_url=sf.base_url,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"DailyApiRequests": {"Remaining": 5}})),
    )
    assert sf.check_health().ok


def test_salesforce_without_refresh_token_or_instance_url_says_what_to_enter():
    sf = registry.get_class("salesforce")(Credentials(values={"accessToken": "a"}), org_id="o")
    with pytest.raises(ConnectorConfigError, match="Instance URL"):
        _ = sf.base_url


# ── Adobe Analytics: discovery/me ──────────────────────────────────────────
def _adobe(values: dict, companies: list[tuple[str, str]], holding: str = ""):  # noqa: ANN202
    ims_calls: list[httpx.Request] = []
    api_calls: list[httpx.Request] = []

    def ims(request: httpx.Request) -> httpx.Response:
        ims_calls.append(request)
        if request.url.host == "ims-na1.adobelogin.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86399})
        if request.url.path == "/discovery/me":
            orgs: dict[str, list] = {}
            for company, org in companies:
                orgs.setdefault(org, []).append({"globalCompanyId": company})
            return httpx.Response(200, json={"imsOrgs": [{"imsOrgId": o, "companies": c} for o, c in orgs.items()]})
        # The report-suite probe used to choose between several companies.
        return httpx.Response(200 if f"/{holding}/" in request.url.path else 404, json={})

    def api(request: httpx.Request) -> httpx.Response:
        api_calls.append(request)
        return httpx.Response(200, json={"rows": []})

    klass = registry.get_class("adobe_analytics")
    adobe = klass(Credentials(values=values), org_id="o")
    adobe._client = httpx.Client(base_url=adobe.base_url, transport=httpx.MockTransport(api))
    adobe._ims_client = lambda: httpx.Client(transport=httpx.MockTransport(ims))
    return adobe, ims_calls, api_calls


BASE_ADOBE = {"reportSuiteId": "acmeprod", "clientId": "cid", "clientSecret": "sec"}


def test_adobe_blank_company_and_org_are_discovered():
    adobe, _, api_calls = _adobe(BASE_ADOBE, [("acme", "1@AdobeOrg")])
    adobe.read_sessions(days=1)
    headers = api_calls[0].headers
    assert api_calls[0].url.path == "/api/acme/reports"
    assert headers["x-proxy-global-company-id"] == "acme"
    assert headers["x-gw-ims-org-id"] == "1@AdobeOrg"
    assert adobe.check_health().ok


def test_adobe_several_companies_picks_the_one_with_the_report_suite():
    adobe, _, api_calls = _adobe(BASE_ADOBE, [("other", "1@AdobeOrg"), ("acme", "2@AdobeOrg")], holding="acme")
    adobe.read_sessions(days=1)
    assert api_calls[0].url.path == "/api/acme/reports"
    assert api_calls[0].headers["x-gw-ims-org-id"] == "2@AdobeOrg"


def test_adobe_entered_company_takes_its_own_org():
    adobe, _, api_calls = _adobe(
        {**BASE_ADOBE, "globalCompanyId": "acme"}, [("other", "1@AdobeOrg"), ("acme", "2@AdobeOrg")]
    )
    adobe.read_sessions(days=1)
    assert api_calls[0].headers["x-gw-ims-org-id"] == "2@AdobeOrg"


# ── Facebook Pages: the Page token's own Page ──────────────────────────────
def test_facebook_blank_page_id_uses_the_page_token_page():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "555", "name": "Acme"})
        return httpx.Response(200, json={"name": "Acme"})

    page = _make("facebook_pages", {"pageAccessToken": "pt"}, handler)
    assert page.check_health().ok
    assert seen[0].url.path.endswith("/me")
    assert seen[1].url.path.endswith("/555")


def test_facebook_never_uses_a_person_token_as_the_page():
    page = _make("facebook_pages", {"accessToken": "user"}, lambda r: pytest.fail("no call expected"))
    with pytest.raises(ConnectorConfigError):
        _ = page.page_id


# ── AI models: required, picked from the vendor's own list ────────────────
# The audit once made Model optional for Claude, Gemini and Perplexity with a
# model name written into the code. Reverted: the model always comes from the
# person's own connection, chosen from the vendor's live list.
@pytest.mark.parametrize("slug", ["anthropic_claude", "google_gemini", "perplexity", "openai"])
def test_model_is_required_and_has_no_default(slug: str):
    from app.llm.base import LLMError
    from app.llm.from_connector import provider_from_connector

    spec = registry.get_spec(slug)
    field = next(f for f in spec.fields if f.key == "model")
    assert field.required
    assert field.default == "" and field.placeholder == ""
    assert field.kind.value == "model"
    record = type("R", (), {"slug": slug, "credential_hints": {}})()
    assert "model" in missing_credentials(record, Credentials(values={"apiKey": "k"}))
    instance = registry.get_class(slug)(Credentials(values={"apiKey": "k"}), org_id="o")
    with pytest.raises(LLMError, match="Choose a model"):
        provider_from_connector(slug, {slug: instance})


def test_perplexity_probe_sends_the_chosen_model():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": []})

    perplexity = _make("perplexity", {"apiKey": "k", "model": "typed-model"}, handler)
    assert perplexity.check_health().ok
    assert json.loads(seen[0].content)["model"] == "typed-model"


def test_openai_model_is_still_required():
    """Kept required: see the audit notes on the OpenAI model field."""
    from app.llm.base import LLMError
    from app.llm.from_connector import provider_from_connector

    spec = registry.get_spec("openai")
    assert next(f for f in spec.fields if f.key == "model").required
    instance = registry.get_class("openai")(Credentials(values={"apiKey": "k"}), org_id="o")
    with pytest.raises(LLMError):
        provider_from_connector("openai", {"openai": instance})
