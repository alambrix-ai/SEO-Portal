"""Live checks for the ad platforms with a sandbox: Microsoft Ads and TikTok Ads.

Both vendors serve their sandbox from different hosts, so the "Test account"
setting switches every address the connector calls. The write steps only run
in the sandbox, and whatever they create is deleted in the same step.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.connectors.base import registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import Credentials
from app.connectors.microsoft_ads import connector as ms_module
from app.connectors.tiktok_ads import connector as tt_module
from app.core.exceptions import ConnectorConfigError, ConnectorError
from tests.support.verify_http import assert_no_secrets, install, run_all

MS = {
    "accessToken": "ms-access-token-value",
    "customerId": "100",
    "accountId": "200",
    "developerToken": "ms-developer-token",
}
TT = {"accessToken": "tt-access-token-value", "advertiserId": "7000000000000000001"}


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _steps(connector) -> dict:  # noqa: ANN001
    return {s.key: s for s in connector.verify_steps()}


# ── Microsoft Ads ───────────────────────────────────────────────────────────
_CSV = b"AccountId,Spend,Impressions,Clicks,Conversions\n200,1.50,40,3,1\n"


def _ms_routes(*, query_by_ids=None, created_id="555") -> list:  # noqa: ANN001
    return [
        ("POST", r"/CustomerManagement/v13/User/Query$", {"CustomerRoles": [{"CustomerId": 100}]}),
        (
            "POST",
            r"/CustomerManagement/v13/AccountsInfo/Query$",
            {"AccountsInfo": [{"Id": 200, "Name": "Test", "AccountLifeCycleStatus": "Active"}]},
        ),
        ("POST", r"/Campaigns/QueryByAccountId$", {"Campaigns": [{"Id": "1"}, {"Id": "2"}]}),
        ("POST", r"/GenerateReport/Submit$", {"ReportRequestId": "rq-1"}),
        (
            "POST",
            r"/GenerateReport/Poll$",
            {"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": "https://dl.example/r.csv"}},
        ),
        ("GET", r"^https://dl\.example/r\.csv$", _CSV),
        ("POST", r"/CampaignManagement/v13/Campaigns$", {"CampaignIds": [created_id], "PartialErrors": []}),
        (
            "POST",
            r"/Campaigns/QueryByIds$",
            query_by_ids or {"Campaigns": [{"Id": created_id, "Status": "Paused"}], "PartialErrors": [None]},
        ),
        ("DELETE", r"/CampaignManagement/v13/Campaigns$", {"PartialErrors": []}),
    ]


def test_microsoft_sandbox_switches_every_address_and_the_refresh_scope():
    live = _connector("microsoft_ads", MS)
    sandbox = _connector("microsoft_ads", {**MS, "accountEnvironment": "Sandbox"})

    assert live.account_environment() == "live"
    assert sandbox.account_environment() == "sandbox"
    assert live.base_url == "https://campaign.api.bingads.microsoft.com/CampaignManagement/v13"
    assert sandbox.base_url == (
        "https://campaign.api.sandbox.bingads.microsoft.com/CampaignManagement/v13"
    )
    assert live.reporting_url == "https://reporting.api.bingads.microsoft.com/Reporting/v13"
    assert sandbox.reporting_url == (
        "https://reporting.api.sandbox.bingads.microsoft.com/Reporting/v13"
    )
    assert live.customer_url == (
        "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13"
    )
    assert sandbox.customer_url == (
        "https://clientcenter.api.sandbox.bingads.microsoft.com/CustomerManagement/v13"
    )
    # Microsoft documents the same identity endpoint for both; what changes
    # is the resource the sandbox refresh token was issued for.
    assert live.token_url == sandbox.token_url == ms_module.TOKEN_URL
    assert live.token_extra["scope"] == "https://ads.microsoft.com/msads.manage offline_access"
    assert sandbox.token_extra["scope"] == (
        "https://si.ads.microsoft.com/msads.manage offline_access"
    )
    # "Test account" is not offered: Microsoft has no such thing, only sandbox.
    assert _connector("microsoft_ads", {**MS, "accountEnvironment": "test"}).account_environment() == "live"


def test_microsoft_sandbox_uses_the_universal_developer_token_when_none_is_entered():
    values = {k: v for k, v in MS.items() if k != "developerToken"}
    sandbox = _connector("microsoft_ads", {**values, "accountEnvironment": "sandbox"})
    assert sandbox.auth_headers()["DeveloperToken"] == "BBD37VB98"
    entered = _connector("microsoft_ads", {**MS, "accountEnvironment": "sandbox"})
    assert entered.auth_headers()["DeveloperToken"] == "ms-developer-token"


def test_microsoft_steps_read_then_add_check_and_delete_in_the_sandbox(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(ms_module, "_POLL_SECONDS", 0)
    connector = _connector("microsoft_ads", {**MS, "accountEnvironment": "sandbox"})
    rec = install(connector, _ms_routes())

    results = run_all(connector)

    assert set(results) == {"accounts", "campaigns", "performance", "campaign_roundtrip"}
    hosts = {urlparse(str(c.url)).hostname for c in rec.calls}
    assert hosts == {
        "clientcenter.api.sandbox.bingads.microsoft.com",
        "campaign.api.sandbox.bingads.microsoft.com",
        "reporting.api.sandbox.bingads.microsoft.com",
        "dl.example",
    }
    assert results["accounts"][1]["accounts"] == 1
    assert results["campaigns"][1]["campaigns"] == 2
    assert results["performance"][1] == {"impressions": 40, "clicks": 3, "conversions": 1}

    submitted = rec.body("POST", r"/GenerateReport/Submit$")["ReportRequest"]
    assert submitted["Type"] == "AccountPerformanceReportRequest"
    assert submitted["Scope"] == {"AccountIds": [200]}

    added = rec.body("POST", r"/CampaignManagement/v13/Campaigns$")
    assert added["AccountId"] == 200
    campaign = added["Campaigns"][0]
    assert campaign["Status"] == "Paused"
    assert campaign["DailyBudget"] == 1.0
    assert campaign["BudgetType"] == "DailyBudgetStandard"
    assert campaign["Name"].startswith(vh.VERIFY_LABEL)
    assert rec.body("POST", r"/Campaigns/QueryByIds$")["CampaignIds"] == [555]
    assert rec.body("DELETE", r"/CampaignManagement/v13/Campaigns$") == {
        "AccountId": 200,
        "CampaignIds": [555],
    }
    create = rec.index("POST", r"/CampaignManagement/v13/Campaigns$")
    check = rec.index("POST", r"/Campaigns/QueryByIds$")
    delete = rec.index("DELETE", r"/CampaignManagement/v13/Campaigns$")
    assert create < check < delete
    assert results["campaign_roundtrip"][1] == {"created_id": "555", "deleted": True, "status": "Paused"}

    sent = rec.find("POST", r"/Campaigns/QueryByAccountId$")[0]
    assert sent.headers["CustomerAccountId"] == "200"
    assert sent.headers["DeveloperToken"] == "ms-developer-token"
    assert_no_secrets(connector, results)


def test_microsoft_live_reads_stay_on_the_live_hosts(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(ms_module, "_POLL_SECONDS", 0)
    connector = _connector("microsoft_ads", MS)
    rec = install(connector, _ms_routes())
    results = run_all(connector, kinds=("read",))
    hosts = {urlparse(str(c.url)).hostname for c in rec.calls}
    assert hosts == {
        "clientcenter.api.bingads.microsoft.com",
        "campaign.api.bingads.microsoft.com",
        "reporting.api.bingads.microsoft.com",
        "dl.example",
    }
    assert_no_secrets(connector, results)


def test_microsoft_campaign_is_deleted_even_when_the_read_back_fails():
    connector = _connector("microsoft_ads", {**MS, "accountEnvironment": "sandbox"})
    rec = install(connector, _ms_routes(query_by_ids=(400, {"error": "boom"})))
    with pytest.raises(ConnectorError, match="created and then removed"):
        _steps(connector)["campaign_roundtrip"].run()
    assert rec.index("POST", r"/CampaignManagement/v13/Campaigns$") < rec.index(
        "DELETE", r"/CampaignManagement/v13/Campaigns$"
    )


def test_microsoft_campaign_is_deleted_when_it_comes_back_active():
    connector = _connector("microsoft_ads", {**MS, "accountEnvironment": "sandbox"})
    rec = install(
        connector, _ms_routes(query_by_ids={"Campaigns": [{"Id": "555", "Status": "Active"}]})
    )
    with pytest.raises(ConnectorError, match="not paused"):
        _steps(connector)["campaign_roundtrip"].run()
    assert rec.find("DELETE", r"/CampaignManagement/v13/Campaigns$")


def test_microsoft_write_step_refuses_a_live_account_without_calling():
    connector = _connector("microsoft_ads", MS)
    rec = install(connector, [])
    step = _steps(connector)["campaign_roundtrip"]
    assert step.kind == "write" and step.requires_test_account
    with pytest.raises(ConnectorConfigError):
        step.run()
    assert rec.calls == []


# ── TikTok Ads ──────────────────────────────────────────────────────────────
def _ok(data: dict) -> dict:
    return {"code": 0, "message": "OK", "data": data}


def _tt_routes(*, campaign_get=None) -> list:  # noqa: ANN001
    return [
        (
            "GET",
            r"/advertiser/info/",
            _ok({"list": [{"advertiser_id": TT["advertiserId"], "status": "STATUS_ENABLE", "currency": "USD"}]}),
        ),
        (
            "GET",
            r"/report/integrated/get/",
            _ok({"list": [{"metrics": {"spend": "2.5", "impressions": "90", "clicks": "4", "conversion": "0"}}]}),
        ),
        ("POST", r"/campaign/create/$", _ok({"campaign_id": "1800000000000001"})),
        (
            "GET",
            r"/campaign/get/",
            campaign_get
            or _ok({"list": [{"campaign_id": "1800000000000001", "operation_status": "DISABLE"}]}),
        ),
        ("POST", r"/campaign/status/update/$", _ok({"campaign_ids": ["1800000000000001"]})),
    ]


def test_tiktok_sandbox_switches_the_address():
    live = _connector("tiktok_ads", TT)
    sandbox = _connector("tiktok_ads", {**TT, "accountEnvironment": "sandbox account"})
    assert live.base_url == "https://business-api.tiktok.com/open_api/v1.3"
    assert sandbox.base_url == "https://sandbox-ads.tiktok.com/open_api/v1.3"
    assert sandbox.account_environment() == "sandbox"


def test_tiktok_steps_read_then_create_check_and_delete_in_the_sandbox():
    connector = _connector("tiktok_ads", {**TT, "accountEnvironment": "Sandbox"})
    rec = install(connector, _tt_routes())

    results = run_all(connector)

    assert set(results) == {"advertiser_info", "performance", "campaign_roundtrip"}
    assert {urlparse(str(c.url)).hostname for c in rec.calls} == {"sandbox-ads.tiktok.com"}
    assert all(urlparse(str(c.url)).path.startswith("/open_api/v1.3/") for c in rec.calls)

    info = parse_qs(urlparse(str(rec.find("GET", r"/advertiser/info/")[0].url)).query)
    assert json.loads(info["advertiser_ids"][0]) == [TT["advertiserId"]]
    report = parse_qs(urlparse(str(rec.find("GET", r"/report/integrated/get/")[0].url)).query)
    assert report["data_level"] == ["AUCTION_ADVERTISER"]
    assert results["performance"][1] == {"impressions": 90, "clicks": 4, "conversions": 0}

    created = rec.body("POST", r"/campaign/create/$")
    assert created["advertiser_id"] == TT["advertiserId"]
    assert created["operation_status"] == "DISABLE"
    assert created["budget_mode"] == "BUDGET_MODE_INFINITE"
    assert created["campaign_name"].startswith(vh.VERIFY_LABEL)
    got = parse_qs(urlparse(str(rec.find("GET", r"/campaign/get/")[0].url)).query)
    assert json.loads(got["filtering"][0]) == {"campaign_ids": ["1800000000000001"]}
    assert rec.body("POST", r"/campaign/status/update/$") == {
        "advertiser_id": TT["advertiserId"],
        "campaign_ids": ["1800000000000001"],
        "operation_status": "DELETE",
    }
    assert rec.index("POST", r"/campaign/create/$") < rec.index("GET", r"/campaign/get/")
    assert rec.index("GET", r"/campaign/get/") < rec.index("POST", r"/campaign/status/update/$")

    sent = rec.calls[0]
    assert sent.headers["Access-Token"] == TT["accessToken"]
    assert_no_secrets(connector, results)


def test_tiktok_campaign_is_deleted_even_when_the_read_back_fails():
    connector = _connector("tiktok_ads", {**TT, "accountEnvironment": "sandbox"})
    rec = install(connector, _tt_routes(campaign_get={"code": 40002, "message": "bad"}))
    with pytest.raises(ConnectorError, match="created and then removed"):
        _steps(connector)["campaign_roundtrip"].run()
    assert rec.index("POST", r"/campaign/create/$") < rec.index(
        "POST", r"/campaign/status/update/$"
    )


def test_tiktok_live_reads_stay_on_the_live_host_and_writes_are_refused():
    connector = _connector("tiktok_ads", TT)
    rec = install(connector, _tt_routes())
    results = run_all(connector, kinds=("read",))
    assert {urlparse(str(c.url)).hostname for c in rec.calls} == {"business-api.tiktok.com"}
    assert_no_secrets(connector, results)

    step = _steps(connector)["campaign_roundtrip"]
    assert step.kind == "write" and step.requires_test_account
    before = len(rec.calls)
    with pytest.raises(ConnectorConfigError):
        step.run()
    assert len(rec.calls) == before


def test_sandbox_switch_is_offered_on_both_forms():
    for slug in ("microsoft_ads", "tiktok_ads"):
        fields = [f.key for f in registry.get_spec(slug).fields]
        assert "accountEnvironment" in fields, slug
        assert fields.index("accountEnvironment") < fields.index("accessToken"), slug
    assert tt_module.SANDBOX_URL.startswith("https://sandbox-ads.tiktok.com/")
