"""Live checks for the ad platforms: Google Ads, Meta Ads, LinkedIn Ads and
the generic DSP exchange.

Each runs against a recording fake vendor (tests/support/verify_http), so the
request shapes are proved and every write step is shown to remove what it
created — also when the read-back fails, and never on an account the vendor
does not itself report as a test account.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError
from tests.support.verify_http import Recorder, assert_no_secrets, install, run_all


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _step(connector, key: str):  # noqa: ANN001, ANN202
    return next(s for s in connector.verify_steps() if s.key == key)


def _query(request: httpx.Request) -> str:
    return json.loads(request.content.decode())["query"]


# ── Google Ads ─────────────────────────────────────────────────────────────
GADS = {
    "customerId": "123-456-7890",
    "developerToken": "dev-token-value",
    "loginCustomerId": "999-888-7777",
    "accessToken": "ya29.access-token-value",
    "accountEnvironment": "test account",
}
CUSTOMER = "1234567890"


def _gads_search(test_account: bool = True, campaign_status: str = "PAUSED"):  # noqa: ANN202
    def handler(request: httpx.Request):  # noqa: ANN202
        query = _query(request)
        if "FROM customer_client" in query:
            return [{"results": [{"customerClient": {"id": CUSTOMER, "level": 0, "status": "ENABLED"}}]}]
        if "FROM customer " in query + " " and "customer.test_account" in query:
            return [{"results": [{"customer": {
                "id": CUSTOMER, "currencyCode": "USD", "manager": False, "testAccount": test_account,
            }}]}]
        if "FROM campaign WHERE campaign.id = 22" in query:
            return [{"results": [{"campaign": {"id": "22", "status": campaign_status}}]}]
        if "metrics.cost_micros" in query:
            return [{"results": [{"metrics": {"costMicros": "0", "impressions": "0", "clicks": "0"}}]}]
        if "detail_placement_view" in query:
            return [{"results": []}]
        raise AssertionError(query)

    return handler


def _gads_mutate(request: httpx.Request):  # noqa: ANN202
    ops = json.loads(request.content.decode())["mutateOperations"]
    if "create" in (ops[0].get("campaignBudgetOperation") or {}):
        return {"mutateOperationResponses": [
            {"campaignBudgetResult": {"resourceName": f"customers/{CUSTOMER}/campaignBudgets/11"}},
            {"campaignResult": {"resourceName": f"customers/{CUSTOMER}/campaigns/22"}},
        ]}
    return {"mutateOperationResponses": [{}]}


def _gads_routes(**kw) -> list:  # noqa: ANN003
    return [
        ("GET", r"/v25/customers:listAccessibleCustomers$", {"resourceNames": [f"customers/{CUSTOMER}"]}),
        ("POST", r"/googleAds:searchStream$", _gads_search(**kw)),
        ("POST", rf"/v25/customers/{CUSTOMER}/googleAds:mutate$", _gads_mutate),
    ]


def test_google_ads_reads_then_creates_and_removes_a_paused_campaign():
    ads = _connector("google_ads", GADS)
    rec = install(ads, _gads_routes())
    results = run_all(ads)
    assert set(results) == {
        "list_accounts", "account_details", "read_performance", "traffic_quality",
        "paused_campaign_roundtrip",
    }
    assert all(str(c.url).startswith("https://googleads.googleapis.com/v25/") for c in rec.calls)
    assert results["account_details"][1]["test_account"] is True

    mutates = rec.find("POST", r"googleAds:mutate$")
    assert len(mutates) == 3
    create = json.loads(mutates[0].content)["mutateOperations"]
    budget = create[0]["campaignBudgetOperation"]["create"]
    campaign = create[1]["campaignOperation"]["create"]
    assert budget["explicitlyShared"] is False and budget["amountMicros"] == 1_000_000
    assert campaign["status"] == "PAUSED" and campaign["advertisingChannelType"] == "SEARCH"
    assert campaign["name"].startswith(vh.VERIFY_LABEL)
    assert campaign["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    assert json.loads(mutates[1].content)["mutateOperations"] == [
        {"campaignOperation": {"remove": f"customers/{CUSTOMER}/campaigns/22"}}
    ]
    assert json.loads(mutates[2].content)["mutateOperations"] == [
        {"campaignBudgetOperation": {"remove": f"customers/{CUSTOMER}/campaignBudgets/11"}}
    ]
    # The test-account read comes before the create; the read-back after it.
    calls = rec.calls
    test_read = next(i for i, c in enumerate(calls) if c.method == "POST"
                     and "searchStream" in str(c.url) and "customer.test_account" in _query(c))
    readback = next(i for i, c in enumerate(calls) if c.method == "POST"
                    and "searchStream" in str(c.url) and "campaign.id = 22" in _query(c))
    assert test_read < calls.index(mutates[0]) < readback < calls.index(mutates[1])
    assert results["paused_campaign_roundtrip"][1] == {"created_id": "22", "deleted": True, "budget_id": "11"}
    assert_no_secrets(ads, results)


def test_google_ads_removes_the_campaign_even_when_it_reads_back_wrong():
    ads = _connector("google_ads", GADS)
    rec = install(ads, _gads_routes(campaign_status="ENABLED"))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _step(ads, "paused_campaign_roundtrip").run()
    assert len(rec.find("POST", r"googleAds:mutate$")) == 3


def test_google_ads_refuses_when_google_says_it_is_not_a_test_account():
    ads = _connector("google_ads", GADS)  # marked as test, but Google disagrees
    rec = install(ads, _gads_routes(test_account=False))
    with pytest.raises(ConnectorConfigError, match="not report this account as a test account"):
        _step(ads, "paused_campaign_roundtrip").run()
    assert not rec.find("POST", r"googleAds:mutate$")


def test_google_ads_write_refuses_on_a_live_connection_without_calling():
    ads = _connector("google_ads", {**GADS, "accountEnvironment": ""})
    rec = install(ads, [])
    step = _step(ads, "paused_campaign_roundtrip")
    assert step.kind == "write" and step.requires_test_account
    with pytest.raises(ConnectorConfigError):
        step.run()
    assert rec.calls == []


def test_google_ads_environment_switch():
    assert _connector("google_ads", GADS).account_environment() == vh.TEST_ACCOUNT
    assert _connector("google_ads", {**GADS, "accountEnvironment": "sandbox"}).account_environment() == vh.LIVE
    field = next(f for f in registry.get_class("google_ads").spec.fields if f.key == "accountEnvironment")
    assert field.label == "Test account" and not field.required
    # Test accounts use the same address as real ones.
    assert _connector("google_ads", GADS).base_url == "https://googleads.googleapis.com"


# ── Meta Ads ───────────────────────────────────────────────────────────────
META = {"adAccountId": "act_42", "accessToken": "EAAB-access-token-value", "accountEnvironment": "sandbox"}
GRAPH = r"https://graph\.facebook\.com/v[\d.]+"


def _meta_routes(status: str = "PAUSED", insights=None) -> list:  # noqa: ANN001
    insights = insights if insights is not None else {"data": []}
    return [
        ("GET", GRAPH + r"/me/adaccounts\?", {"data": [{"id": "act_42", "account_id": "42", "name": "Sandbox"}]}),
        ("GET", GRAPH + r"/act_42/insights\?", insights),
        ("GET", GRAPH + r"/act_42\?fields=", {"id": "act_42", "account_status": 1, "currency": "USD"}),
        ("POST", GRAPH + r"/act_42/campaigns$", {"id": "777"}),
        ("GET", GRAPH + r"/777\?fields=", {"id": "777", "status": status, "objective": "OUTCOME_TRAFFIC"}),
        ("DELETE", GRAPH + r"/777$", {"success": True}),
    ]


def test_meta_ads_reads_then_creates_and_deletes_a_paused_campaign():
    ads = _connector("meta_ads", META)
    rec = install(ads, _meta_routes())
    results = run_all(ads)
    assert set(results) == {
        "list_accounts", "account_details", "read_performance", "traffic_quality",
        "paused_campaign_roundtrip",
    }
    body = rec.body("POST", r"/act_42/campaigns$")
    assert body["status"] == "PAUSED" and body["special_ad_categories"] == []
    assert body["objective"] == "OUTCOME_TRAFFIC" and body["is_adset_budget_sharing_enabled"] is False
    assert body["name"].startswith(vh.VERIFY_LABEL)
    assert rec.index("POST", r"/act_42/campaigns$") < rec.index("GET", r"/777\?") < rec.index("DELETE", r"/777$")
    assert results["paused_campaign_roundtrip"][1] == {"created_id": "777", "deleted": True}
    assert_no_secrets(ads, results)


def test_meta_ads_deletes_the_campaign_even_when_it_reads_back_active():
    ads = _connector("meta_ads", META)
    rec = install(ads, _meta_routes(status="ACTIVE"))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _step(ads, "paused_campaign_roundtrip").run()
    assert rec.find("DELETE", r"/777$")


def test_meta_sandbox_has_no_insights_and_that_is_not_a_failure():
    ads = _connector("meta_ads", META)
    install(ads, _meta_routes(insights=(400, {"error": {"message": "not supported"}})))
    detail, evidence = _step(ads, "read_performance").run()
    assert evidence == {"sandbox": True} and "sandbox" in detail.lower()
    # On a real account the same refusal is a failure.
    live = _connector("meta_ads", {**META, "accountEnvironment": ""})
    install(live, _meta_routes(insights=(400, {"error": {"message": "not supported"}})))
    with pytest.raises(ConnectorError):
        _step(live, "read_performance").run()


def test_meta_ads_write_refuses_on_a_live_connection():
    ads = _connector("meta_ads", {**META, "accountEnvironment": ""})
    rec = install(ads, [])
    with pytest.raises(ConnectorConfigError):
        _step(ads, "paused_campaign_roundtrip").run()
    assert rec.calls == []


def test_meta_ads_environment_switch():
    assert _connector("meta_ads", META).account_environment() == vh.SANDBOX
    assert _connector("meta_ads", {**META, "accountEnvironment": "test"}).account_environment() == vh.LIVE
    # Sandbox ad accounts use the same Graph API address.
    assert _connector("meta_ads", META).base_url.startswith("https://graph.facebook.com/")


# ── LinkedIn Ads ───────────────────────────────────────────────────────────
LI = {"adAccountId": "509912345", "accessToken": "AQV-access-token-value", "accountEnvironment": "test_account"}
LI_BASE = r"https://api\.linkedin\.com/rest"
GROUP = "urn:li:sponsoredCampaignGroup:6001"


def _li_routes(test: bool = True, status: str = "DRAFT") -> list:
    return [
        ("GET", LI_BASE + r"/adAccounts\?q=search", {"elements": [{"id": 509912345, "status": "ACTIVE", "test": test}]}),
        ("GET", LI_BASE + r"/adAccounts/509912345$", {"id": 509912345, "currency": "USD", "status": "ACTIVE", "test": test}),
        ("GET", LI_BASE + r"/adAnalytics\?", {"elements": []}),
        ("POST", LI_BASE + r"/adAccounts/509912345/adCampaignGroups$",
         lambda _r: httpx.Response(201, headers={"x-restli-id": GROUP})),
        ("GET", LI_BASE + r"/adAccounts/509912345/adCampaignGroups/6001$", {"id": 6001, "status": status}),
        ("DELETE", LI_BASE + r"/adAccounts/509912345/adCampaignGroups/6001$", (204, None)),
    ]


class _HeaderRecorder(Recorder):
    """A Recorder whose callable routes may return a ready httpx.Response —
    LinkedIn names what it created in a response header."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        for method, pattern, response in self.routes:
            if method == request.method and pattern.search(str(request.url)) and callable(response):
                out = response(request)
                if isinstance(out, httpx.Response):
                    self.calls.append(request)
                    return out
        return super().__call__(request)


def _install_li(connector, routes):  # noqa: ANN001, ANN202
    rec = _HeaderRecorder(routes)
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(rec))
    return rec


def test_linkedin_ads_reads_then_creates_and_deletes_a_draft_campaign_group():
    ads = _connector("linkedin_ads", LI)
    rec = _install_li(ads, _li_routes())
    results = run_all(ads)
    assert set(results) == {"list_accounts", "account_details", "read_performance", "draft_campaign_group_roundtrip"}
    created = rec.find("POST", r"/adCampaignGroups$")[0]
    body = json.loads(created.content)
    assert body["status"] == "DRAFT" and body["account"] == "urn:li:sponsoredAccount:509912345"
    assert body["name"].startswith(vh.VERIFY_LABEL) and body["runSchedule"]["start"] > 0
    assert "totalBudget" not in body
    assert created.headers["LinkedIn-Version"] and created.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert rec.index("POST", r"/adCampaignGroups$") < rec.index("GET", r"/adCampaignGroups/6001$") \
        < rec.index("DELETE", r"/adCampaignGroups/6001$")
    assert results["draft_campaign_group_roundtrip"][1] == {"created_id": "6001", "deleted": True}
    assert_no_secrets(ads, results)


def test_linkedin_ads_refuses_unless_linkedin_reports_a_test_account():
    ads = _connector("linkedin_ads", LI)
    rec = _install_li(ads, _li_routes(test=False))
    with pytest.raises(ConnectorConfigError, match="not report this ad account as a test account"):
        _step(ads, "draft_campaign_group_roundtrip").run()
    assert not rec.find("POST", r"/adCampaignGroups$")


def test_linkedin_ads_deletes_the_group_even_when_it_reads_back_wrong():
    ads = _connector("linkedin_ads", LI)
    rec = _install_li(ads, _li_routes(status="ACTIVE"))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _step(ads, "draft_campaign_group_roundtrip").run()
    assert rec.find("DELETE", r"/adCampaignGroups/6001$")


def test_linkedin_ads_write_refuses_on_a_live_connection():
    ads = _connector("linkedin_ads", {**LI, "accountEnvironment": ""})
    rec = install(ads, [])
    with pytest.raises(ConnectorConfigError):
        _step(ads, "draft_campaign_group_roundtrip").run()
    assert rec.calls == []


# ── DSP exchange ───────────────────────────────────────────────────────────
DSP = {"seatId": "seat-1", "apiHost": "https://dsp.example.com/v1", "apiKey": "dsp-key-value"}


def test_dsp_exchange_reads_its_two_reports_and_writes_nothing():
    dsp = _connector("dsp_exchange", DSP)
    rec = install(
        dsp,
        [
            ("GET", r"https://dsp\.example\.com/v1/seats/seat-1/reports/summary\?lookback_days=1",
             {"totals": {"spend": 1.5, "impressions": 100, "clicks": 3, "conversions": 0}}),
            ("GET", r"https://dsp\.example\.com/v1/seats/seat-1/reports/supply-quality\?",
             {"placements": [{"placement_id": "p1", "clicks": 2}]}),
        ],
    )
    results = run_all(dsp)
    assert set(results) == {"read_performance", "traffic_quality"}
    assert all(s.kind == "read" for s in dsp.verify_steps())
    assert all(c.method == "GET" for c in rec.calls)
    assert results["traffic_quality"][1] == {"count": 1}
    assert_no_secrets(dsp, results)
