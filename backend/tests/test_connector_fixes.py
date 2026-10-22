"""Connector defects fixed against the vendors' documented APIs.

Each test drives a connector through ``httpx.MockTransport`` and checks the
request it actually puts on the wire (method, path, query, body), plus how it
reads the vendor's answer — which is where every one of these was wrong.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from app.connectors.base.connector import BaseConnector, Capability, ConnectorSpec
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError


def _make(cls, values: dict, handler):  # noqa: ANN001, ANN202
    connector = cls(Credentials(values=values), org_id="o")
    connector._client = httpx.Client(
        base_url=connector.base_url, transport=httpx.MockTransport(handler)
    )
    return connector


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


def _fields(cls) -> dict:  # noqa: ANN001
    return {f.key: f for f in cls.spec.fields}


# ── Base request: raw content and response access ──────────────────────────
class _Plain(BaseConnector):
    spec = ConnectorSpec(slug="plain", name="Plain", category="x", base_url="https://p.test")


def test_request_sends_raw_content_and_can_return_the_response():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, headers={"x-restli-id": "urn:x:1"}, json={})

    plain = _make(_Plain, {}, handler)
    response = plain.request("POST", "/a", content=b'{"a":1}', return_response=True)
    assert seen[0].content == b'{"a":1}'
    assert response.headers["x-restli-id"] == "urn:x:1"
    with pytest.raises(ValueError):
        plain.request("POST", "/a", content=b"x", json_body={})


# ── Meta Ads ───────────────────────────────────────────────────────────────
from app.connectors.meta_ads.connector import MetaAdsConnector  # noqa: E402

META = {"adAccountId": "123", "accessToken": "t"}


def test_meta_declares_every_credential_it_reads():
    fields = _fields(MetaAdsConnector)
    for key in ("campaignId", "adSetId", "pixelId", "finalUrl", "pageId"):
        assert key in fields and not fields[key].required and fields[key].help_text


def test_meta_no_longer_claims_placement_blocking():
    assert Capability.BLOCK_PLACEMENT not in MetaAdsConnector.spec.capabilities


def test_meta_creative_links_to_the_product_page_and_maps_the_cta():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/adcreatives"):
            return httpx.Response(200, json={"id": "c1"})
        return httpx.Response(200, json={"id": "ad1"})

    meta = _make(
        MetaAdsConnector,
        {**META, "pageId": "p1", "finalUrl": "https://shop.test/", "adSetId": "as1"},
        handler,
    )
    creative_id = meta.upload_creative(
        headline="H",
        body_copy="B",
        call_to_action="Book a test drive",
        dimensions="1080x1080",
        final_url="https://shop.test/cars/x",
    )
    assert creative_id == "c1"
    body = json.loads(seen[0].content)
    link_data = body["object_story_spec"]["link_data"]
    assert link_data["link"] == "https://shop.test/cars/x"
    assert link_data["call_to_action"]["type"] == "BOOK_NOW"
    # Attached to the ad set, paused.
    ad = json.loads(seen[1].content)
    assert seen[1].url.path.endswith("/act_123/ads")
    assert ad == {
        "name": "Synaprank — H",
        "adset_id": "as1",
        "creative": {"creative_id": "c1"},
        "status": "PAUSED",
    }


def test_meta_cta_mapping_only_uses_valid_types():
    assert MetaAdsConnector._cta_type("Book a test drive") == "BOOK_NOW"
    assert MetaAdsConnector._cta_type("Call us") == "CONTACT_US"
    assert MetaAdsConnector._cta_type("Something else") == "LEARN_MORE"


def test_meta_creative_without_any_link_is_a_config_error():
    meta = _make(MetaAdsConnector, {**META, "pageId": "p1"}, lambda r: httpx.Response(200))
    with pytest.raises(ConnectorConfigError, match="landing page"):
        meta.upload_creative(headline="H", body_copy="B", call_to_action="", dimensions="1x1")


def test_meta_audience_needs_a_pixel_and_uses_standard_events():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "aud1"})

    with pytest.raises(ConnectorConfigError, match="Pixel ID"):
        _make(MetaAdsConnector, META, handler).push_audience(
            label="x", size=1, signals=["purchase"]
        )

    meta = _make(MetaAdsConnector, {**META, "pixelId": "px"}, handler)
    assert meta.push_audience(
        label="Buyers", size=500, signals=["purchase", "cart_started", "email_open"]
    ) == "aud1"
    body = json.loads(seen[0].content)
    rule = body["rule"]["inclusions"]["rules"][0]
    assert rule["event_sources"] == [{"id": "px", "type": "pixel"}]
    assert [f["value"] for f in rule["filter"]["filters"]] == ["Purchase", "AddToCart"]
    assert "subtype" not in body

    with pytest.raises(ConnectorError, match="standard Meta pixel event"):
        meta.push_audience(label="x", size=1, signals=["email_open"])


def test_meta_traffic_sample_follows_paging_next():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "after=" not in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [{"publisher_platform": "facebook", "platform_position": "feed", "clicks": "10", "inline_link_clicks": "5"}],
                    "paging": {"next": "https://graph.facebook.com/v26.0/act_123/insights?after=abc"},
                },
            )
        return httpx.Response(
            200,
            json={"data": [{"publisher_platform": "audience_network", "platform_position": "an_classic", "clicks": "4", "inline_link_clicks": "0"}]},
        )

    samples = _make(MetaAdsConnector, META, handler).sample_traffic(limit=10)
    assert [s.source for s in samples] == ["facebook:feed", "audience_network:an_classic"]
    assert samples[1].bounce_rate == 1.0
    assert len(calls) == 2


def test_meta_performance_uses_the_requested_window():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"spend": "12.5", "impressions": "100", "clicks": "3"}]})

    perf = _make(MetaAdsConnector, META, handler).read_performance(days=14)
    window = json.loads(_query(seen[0])["time_range"][0])
    until = date.today() - timedelta(days=1)
    assert window == {"since": (until - timedelta(days=13)).isoformat(), "until": until.isoformat()}
    assert perf.spend == 12.5


# ── LinkedIn Ads ───────────────────────────────────────────────────────────
from app.connectors.linkedin_ads.connector import (  # noqa: E402
    DEFAULT_API_VERSION,
    LinkedInAdsConnector,
)

LINKEDIN = {"adAccountId": "509", "accessToken": "t"}


def test_linkedin_fields_and_capabilities():
    fields = _fields(LinkedInAdsConnector)
    assert not fields["campaignId"].required
    assert fields["apiVersion"].default == DEFAULT_API_VERSION
    caps = LinkedInAdsConnector.spec.capabilities
    assert Capability.PUSH_AUDIENCE not in caps
    assert Capability.UPLOAD_CREATIVE in caps


def test_linkedin_analytics_uses_restli_date_range_and_days():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"elements": [{"costInLocalCurrency": "9.5", "impressions": 10, "clicks": 2}]})

    perf = _make(LinkedInAdsConnector, {**LINKEDIN, "apiVersion": "202608"}, handler).read_performance(days=7)
    raw = seen[0].url.query.decode()
    end = date.today()
    start = end - timedelta(days=6)
    assert (
        f"dateRange=(start:(year:{start.year},month:{start.month},day:{start.day}),"
        f"end:(year:{end.year},month:{end.month},day:{end.day}))"
    ) in raw
    assert "accounts=List(urn%3Ali%3AsponsoredAccount%3A509)" in raw
    assert seen[0].headers["LinkedIn-Version"] == "202608"
    assert perf.spend == 9.5


def test_linkedin_sunset_version_is_explained():
    li = _make(LinkedInAdsConnector, LINKEDIN, lambda r: httpx.Response(426, text="upgrade"))
    with pytest.raises(ConnectorConfigError, match="API version") as caught:
        li.read_performance(days=7)
    assert "LinkedIn API version" in (li.diagnose(caught.value) or "")


def test_linkedin_budget_uses_account_currency_and_partial_update():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": 509, "currency": "INR"})
        return httpx.Response(204)

    li = _make(LinkedInAdsConnector, {**LINKEDIN, "campaignId": "777"}, handler)
    assert li.set_budget_share(percent=10, daily_budget=1500.0)
    update = seen[-1]
    assert update.url.path == "/rest/adAccounts/509/adCampaigns/777"
    assert update.headers["X-RestLi-Method"] == "PARTIAL_UPDATE"
    assert json.loads(update.content)["patch"]["$set"]["dailyBudget"] == {
        "amount": "1500.00",
        "currencyCode": "INR",
    }


def test_linkedin_budget_without_campaign_names_the_field():
    li = _make(LinkedInAdsConnector, LINKEDIN, lambda r: httpx.Response(200))
    with pytest.raises(ConnectorConfigError, match="Campaign ID"):
        li.set_budget_share(percent=10, daily_budget=1.0)


def test_linkedin_creative_is_a_dark_post_plus_sponsored_creative():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/rest/posts":
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:42"})
        return httpx.Response(201, headers={"x-restli-id": "urn:li:sponsoredCreative:99"})

    li = _make(
        LinkedInAdsConnector, {**LINKEDIN, "organizationId": "5", "campaignId": "777"}, handler
    )
    created = li.upload_creative(
        headline="Title",
        body_copy="New (2026) model #1",
        call_to_action="Learn more",
        dimensions="1200x627",
        final_url="https://shop.test/x",
    )
    assert created == "urn:li:sponsoredCreative:99"
    post = json.loads(seen[0].content)
    assert post["distribution"]["feedDistribution"] == "NONE"
    assert post["adContext"]["dscAdAccount"] == "urn:li:sponsoredAccount:509"
    assert post["content"]["article"]["source"] == "https://shop.test/x"
    assert post["commentary"] == r"New \(2026\) model \#1"
    creative = json.loads(seen[1].content)
    assert seen[1].url.path == "/rest/adAccounts/509/creatives"
    assert creative == {
        "campaign": "urn:li:sponsoredCampaign:777",
        "content": {"reference": "urn:li:share:42"},
        "intendedStatus": "DRAFT",
    }


# ── Microsoft Ads ──────────────────────────────────────────────────────────
from app.connectors.microsoft_ads import connector as ms_module  # noqa: E402
from app.connectors.microsoft_ads.connector import MicrosoftAdsConnector  # noqa: E402

MICROSOFT = {"customerId": "1", "accountId": "2", "accessToken": "t", "developerToken": "d"}


def _zipped(csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.csv", csv_text)
    return buffer.getvalue()


def test_microsoft_reporting_submits_polls_and_downloads(monkeypatch):
    monkeypatch.setattr(ms_module, "_POLL_SECONDS", 0)
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/GenerateReport/Submit"):
            return httpx.Response(200, json={"ReportRequestId": "r1"})
        if path.endswith("/GenerateReport/Poll"):
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"ReportRequestStatus": {"Status": "Pending"}})
            return httpx.Response(
                200,
                json={"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": "https://dl.test/r1.zip"}},
            )
        assert request.url.host == "dl.test"
        assert "AuthenticationToken" not in request.headers
        return httpx.Response(
            200,
            content=_zipped(
                '"AccountId","Spend","Impressions","Clicks","Conversions"\n'
                '"2","1,234.50","1000","40","3"\n'
            ),
        )

    perf = _make(MicrosoftAdsConnector, MICROSOFT, handler).read_performance(days=30)
    assert (perf.spend, perf.impressions, perf.clicks, perf.conversions) == (1234.5, 1000, 40, 3)
    submit = json.loads(seen[0].content)["ReportRequest"]
    assert seen[0].url.host == "reporting.api.bingads.microsoft.com"
    assert submit["Type"] == "AccountPerformanceReportRequest"
    assert submit["Scope"] == {"AccountIds": [2]}
    end = date.today()
    assert submit["Time"]["CustomDateRangeEnd"] == {"Day": end.day, "Month": end.month, "Year": end.year}
    assert polls["n"] == 2


def test_microsoft_report_with_no_rows_is_empty(monkeypatch):
    monkeypatch.setattr(ms_module, "_POLL_SECONDS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Submit"):
            return httpx.Response(200, json={"ReportRequestId": "r1"})
        return httpx.Response(200, json={"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": None}})

    assert _make(MicrosoftAdsConnector, MICROSOFT, handler).sample_traffic(limit=5) == []


def test_microsoft_budget_is_an_update_not_an_add():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"PartialErrors": []})

    ms = _make(MicrosoftAdsConnector, {**MICROSOFT, "campaignId": "55"}, handler)
    assert ms.set_budget_share(percent=10, daily_budget=25.0)
    assert seen[0].method == "PUT" and seen[0].url.path.endswith("/Campaigns")
    body = json.loads(seen[0].content)
    assert body["AccountId"] == 2 and body["Campaigns"][0]["Id"] == 55


def test_microsoft_budget_partial_error_is_reported():
    ms = _make(
        MicrosoftAdsConnector,
        {**MICROSOFT, "campaignId": "55"},
        lambda r: httpx.Response(200, json={"PartialErrors": [{"Message": "Shared budget"}]}),
    )
    with pytest.raises(ConnectorError, match="Shared budget"):
        ms.set_budget_share(percent=10, daily_budget=25.0)


def test_microsoft_health_probes_a_real_operation_and_no_block_placement():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Campaigns": [{"Id": 1}]})

    report = _make(MicrosoftAdsConnector, MICROSOFT, handler).check_health()
    assert report.ok and seen[0].url.path.endswith("/Campaigns/QueryByAccountId")
    assert Capability.BLOCK_PLACEMENT not in MicrosoftAdsConnector.spec.capabilities
    assert "campaignId" in _fields(MicrosoftAdsConnector)


# ── TikTok Ads ─────────────────────────────────────────────────────────────
from app.connectors.tiktok_ads.connector import TikTokAdsConnector  # noqa: E402

TIKTOK = {"advertiserId": "7", "accessToken": "t"}


def test_tiktok_reports_send_a_date_window():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"list": [{"metrics": {"spend": "5"}}]}})

    tiktok = _make(TikTokAdsConnector, TIKTOK, handler)
    assert tiktok.read_performance(days=10).spend == 5.0
    tiktok.sample_traffic(limit=20)
    end = date.today()
    for request in seen:
        query = _query(request)
        assert query["end_date"] == [end.isoformat()]
    assert _query(seen[0])["start_date"] == [(end - timedelta(days=9)).isoformat()]


def test_tiktok_capabilities_and_fields():
    caps = TikTokAdsConnector.spec.capabilities
    assert Capability.BLOCK_PLACEMENT not in caps
    assert Capability.PUSH_AUDIENCE not in caps
    fields = _fields(TikTokAdsConnector)
    for key in ("campaignId", "adgroupId", "videoId", "coverImageId", "identityId"):
        assert key in fields and not fields[key].required


def test_tiktok_creative_needs_its_assets_and_sends_a_valid_ad():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"ad_ids": ["ad9"]}})

    with pytest.raises(ConnectorConfigError, match="Video ID"):
        _make(TikTokAdsConnector, {**TIKTOK, "adgroupId": "g"}, handler).upload_creative(
            headline="H", body_copy="B", call_to_action="Shop", dimensions="9x16"
        )

    values = {**TIKTOK, "adgroupId": "g", "videoId": "v", "coverImageId": "i", "identityId": "id"}
    created = _make(TikTokAdsConnector, values, handler).upload_creative(
        headline="H", body_copy="B", call_to_action="Shop now", dimensions="9x16",
        final_url="https://shop.test/p",
    )
    assert created == "ad9"
    creative = json.loads(seen[-1].content)["creatives"][0]
    assert creative["video_id"] == "v" and creative["image_ids"] == ["i"]
    assert creative["call_to_action"] == "SHOP_NOW"
    assert creative["landing_page_url"] == "https://shop.test/p"
    assert creative["identity_type"] == "CUSTOMIZED_USER"


# ── DSP exchange ───────────────────────────────────────────────────────────
from app.connectors.dsp_exchange.connector import DspExchangeConnector  # noqa: E402

DSP = {"seatId": "s", "apiHost": "https://dsp.test/v1", "apiKey": "k"}


def test_dsp_budget_uses_the_configured_currency_and_line_item():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ConnectorConfigError, match="Line item ID"):
        _make(DspExchangeConnector, DSP, handler).set_budget_share(percent=1, daily_budget=1.0)

    dsp = _make(DspExchangeConnector, {**DSP, "lineItemId": "li", "currency": "eur"}, handler)
    dsp.set_budget_share(percent=10, daily_budget=12.0)
    assert json.loads(seen[0].content)["daily_budget"] == {"amount": 12.0, "currency": "EUR"}
    assert "contract" in DspExchangeConnector.spec.description
    assert DspExchangeConnector.spec.requirements


def test_dsp_missing_mouse_events_is_unknown_not_true():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"placements": [{"placement_id": "a"}, {"placement_id": "b", "mouse_events": False}]},
        )

    samples = _make(DspExchangeConnector, DSP, handler).sample_traffic(limit=10)
    assert "has_mouse_events" not in samples[0].signals
    assert samples[1].signals["has_mouse_events"] is False


# ── Google Analytics 4 ─────────────────────────────────────────────────────
from app.connectors.google_analytics_4.connector import GoogleAnalytics4Connector  # noqa: E402


def test_ga4_referrer_exclusion_is_unsupported_and_makes_no_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call should be made")

    ga = _make(GoogleAnalytics4Connector, {"propertyId": "1", "accessToken": "t"}, handler)
    assert ga.exclude_referrer("spam.test") is False
    assert ga.supports_referrer_exclusion is False


def test_ga4_conversions_use_key_events():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": [{"metricValues": [{"value": "7"}]}]})

    ga = _make(GoogleAnalytics4Connector, {"propertyId": "1", "accessToken": "t"}, handler)
    assert ga.read_conversions(days=30) == 7
    assert json.loads(seen[0].content)["metrics"] == [{"name": "keyEvents"}]
    ga.read_sessions(days=7)
    assert {"name": "keyEvents"} in json.loads(seen[1].content)["metrics"]


# ── Adobe Analytics ────────────────────────────────────────────────────────
from app.connectors.adobe_analytics.connector import AdobeAnalyticsConnector  # noqa: E402

ADOBE = {
    "orgId": "o@AdobeOrg",
    "globalCompanyId": "acme",
    "reportSuiteId": "acmeprod",
    "clientId": "cid",
    "clientSecret": "sec",
}


def test_adobe_obtains_and_caches_an_ims_token():
    token_calls: list[httpx.Request] = []
    api_calls: list[httpx.Request] = []

    def ims(request: httpx.Request) -> httpx.Response:
        token_calls.append(request)
        return httpx.Response(200, json={"access_token": "ims-token", "expires_in": 86399})

    def api(request: httpx.Request) -> httpx.Response:
        api_calls.append(request)
        return httpx.Response(200, json={"rows": [{"value": "2026-10-01", "data": [5, 1, 9.5]}]})

    adobe = _make(AdobeAnalyticsConnector, ADOBE, api)
    adobe._ims_client = lambda: httpx.Client(transport=httpx.MockTransport(ims))
    adobe.read_sessions(days=3)
    adobe.read_referrers(days=1)

    assert len(token_calls) == 1
    form = parse_qs(token_calls[0].content.decode())
    assert str(token_calls[0].url) == "https://ims-na1.adobelogin.com/ims/token/v3"
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["cid"] and form["client_secret"] == ["sec"]
    assert form["scope"] == ["openid,AdobeID,additional_info.projectedProductContext"]
    headers = api_calls[0].headers
    assert headers["Authorization"] == "Bearer ims-token"
    assert headers["x-api-key"] == "cid"
    assert headers["x-proxy-global-company-id"] == "acme"
    assert api_calls[0].url.path == "/api/acme/reports"


def test_adobe_rejected_credentials_read_plainly():
    adobe = _make(AdobeAnalyticsConnector, ADOBE, lambda r: httpx.Response(200, json={}))
    adobe._ims_client = lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_client"}))
    )
    report = adobe.check_health()
    assert not report.ok and "Client ID" in report.detail


# ── Google Business Profile ────────────────────────────────────────────────
from app.connectors.google_business_profile.connector import (  # noqa: E402
    GoogleBusinessProfileConnector,
)

GBP = {"locationId": "locations/1", "accountId": "accounts/9", "accessToken": "t"}


def test_gbp_sessions_is_a_get_with_a_daily_range():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "multiDailyMetricTimeSeries": [
                    {
                        "dailyMetricTimeSeries": [
                            {"timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 10, "day": 1}, "value": "4"}]}},
                            {"timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 10, "day": 1}, "value": "6"}]}},
                        ]
                    }
                ]
            },
        )

    rows = _make(GoogleBusinessProfileConnector, GBP, handler).read_sessions(days=28)
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/locations/1:fetchMultiDailyMetricsTimeSeries"
    query = _query(request)
    assert query["dailyMetrics"] == [
        "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
        "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    ]
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=27)
    assert query["dailyRange.startDate.day"] == [str(start.day)]
    assert query["dailyRange.endDate.year"] == [str(end.year)]
    assert query["dailyRange.endDate.month"] == [str(end.month)]
    assert [(r.day, r.sessions) for r in rows] == [(date(2026, 10, 1), 10)]


def test_gbp_hours_default_to_midnight_not_office_hours():
    hours = GoogleBusinessProfileConnector._format_hours(
        {
            "periods": [
                {"openDay": "MONDAY", "openTime": {"hours": 9}, "closeTime": {"hours": 17, "minutes": 30}},
                # Open 24 hours: openTime is omitted, closeTime is 24:00.
                {"openDay": "TUESDAY", "closeDay": "TUESDAY", "closeTime": {"hours": 24}},
                # Closing at midnight: closeTime {} on the next day.
                {"openDay": "FRIDAY", "openTime": {"hours": 18}, "closeDay": "SATURDAY", "closeTime": {}},
            ]
        }
    )
    assert hours == "Mo 09:00-17:30, Tu 00:00-24:00, Fr 18:00-24:00"


def test_gbp_reviews_follow_next_page_token_up_to_the_limit():
    seen: list[httpx.Request] = []

    def review(i: int) -> dict:
        return {"name": f"accounts/9/locations/1/reviews/r{i}", "starRating": "FIVE"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "pageToken" not in _query(request):
            return httpx.Response(200, json={"reviews": [review(i) for i in range(50)], "nextPageToken": "n2"})
        return httpx.Response(200, json={"reviews": [review(i) for i in range(50, 100)], "nextPageToken": "n3"})

    reviews = _make(GoogleBusinessProfileConnector, GBP, handler).read_reviews(limit=70)
    assert len(reviews) == 70
    assert len(seen) == 2
    assert _query(seen[1])["pageToken"] == ["n2"]
    assert _query(seen[1])["pageSize"] == ["20"]


# ── Gemini ─────────────────────────────────────────────────────────────────
from app.connectors.google_gemini.connector import GoogleGeminiConnector  # noqa: E402


def test_gemini_matches_the_domain_carried_in_the_chunk_title():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "groundingMetadata": {
                            "groundingChunks": [
                                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA", "title": "other.test"}},
                                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/BBB", "title": "www.customer.test"}},
                            ]
                        }
                    }
                ]
            },
        )

    gemini = _make(GoogleGeminiConnector, {"apiKey": "k", "model": "gemini-2.5-flash"}, handler)
    hits = gemini.check_citations(queries=["best dealer"], domain="customer.test")
    assert len(hits) == 1
    assert hits[0].position == 2
    assert hits[0].cited_url == "www.customer.test"


# ── Signed webhooks ────────────────────────────────────────────────────────
from app.connectors.custom_api.connector import CustomApiConnector  # noqa: E402
from app.connectors.zapier_webhooks.connector import ZapierWebhooksConnector  # noqa: E402


def _verify(request: httpx.Request, secret: str) -> bool:
    timestamp = request.headers["X-AutoMarket-Timestamp"]
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    return request.headers["X-AutoMarket-Signature"] == f"sha256={expected}"


def test_zapier_signs_exactly_the_bytes_it_sends():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    zap = _make(
        ZapierWebhooksConnector,
        {"webhookUrl": "https://hooks.test/catch/1", "signingSecret": "s3"},
        handler,
    )
    assert zap.check_health().ok
    zap.notify(subject="Prix réduit — 10%", message="naïve café")
    for request in seen:
        assert request.headers["Content-Type"] == "application/json"
        assert _verify(request, "s3")
    assert json.loads(seen[0].content)["event"] == "connection.test"


def test_custom_api_signs_exactly_the_bytes_it_sends():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    api = _make(CustomApiConnector, {"webhookUrl": "https://own.test/am", "secret": "k"}, handler)
    api.write_page("7", body="<p>Café crème</p>", title="Ünïcode")
    request = seen[0]
    assert _verify(request, "k")
    assert json.loads(request.content) == {"body": "<p>Café crème</p>", "title": "Ünïcode"}


# ── WordPress ──────────────────────────────────────────────────────────────
from app.connectors.wordpress.connector import WordPressConnector  # noqa: E402

WP = {"siteUrl": "https://wp.test", "username": "u", "applicationPassword": "p"}


def _wp_row(i: int, kind: str) -> dict:
    return {"id": i, "link": f"https://wp.test/{kind}-{i}/", "title": {"rendered": "T"}, "content": {"rendered": "x"}}


def test_wordpress_pages_through_until_the_limit():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        query = _query(request)
        page = int(query["page"][0])
        per_page = int(query["per_page"][0])
        assert per_page <= 100
        if request.url.path.endswith("/pages"):
            rows = [_wp_row(page * 1000 + i, "page") for i in range(per_page)]
            return httpx.Response(200, json=rows, headers={"X-WP-TotalPages": "3"})
        return httpx.Response(200, json=[_wp_row(9, "post")], headers={"X-WP-TotalPages": "1"})

    pages = _make(WordPressConnector, WP, handler).list_pages(limit=150)
    assert len(pages) == 150
    assert [(_query(r)["page"][0], _query(r)["per_page"][0]) for r in seen] == [("1", "100"), ("2", "50")]

    seen.clear()
    every = _make(WordPressConnector, WP, handler).list_pages(limit=1000)
    assert len(every) == 301  # three pages of 100, then the one post


def test_wordpress_schema_falls_back_when_meta_is_not_registered():
    def dropped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "meta": []})

    with pytest.raises(NotImplementedError):
        _make(WordPressConnector, WP, dropped).inject_schema("1", json_ld={"@type": "Thing"})

    def kept(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["meta"]
        return httpx.Response(200, json={"id": 1, "meta": sent})

    assert _make(WordPressConnector, WP, kept).inject_schema("1", json_ld={"@type": "Thing"})


# ── Shopify ────────────────────────────────────────────────────────────────
from app.connectors.shopify.connector import ShopifyConnector  # noqa: E402

SHOP = {"storeDomain": "s.myshopify.com", "adminToken": "t"}


def test_shopify_follows_the_link_header_cursor():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/pages.json"):
            if "page_info" not in str(request.url):
                nxt = "https://s.myshopify.com/admin/api/2026-10/pages.json?limit=2&page_info=abc"
                return httpx.Response(
                    200,
                    json={"pages": [{"id": 1, "handle": "a"}, {"id": 2, "handle": "b"}]},
                    headers={"Link": f'<{nxt}>; rel="next"'},
                )
            return httpx.Response(200, json={"pages": [{"id": 3, "handle": "c"}]})
        return httpx.Response(200, json={"products": [{"id": 9, "handle": "p"}]})

    pages = _make(ShopifyConnector, SHOP, handler).list_pages(limit=4)
    assert [p.remote_id for p in pages] == ["1", "2", "3", "product:9"]
    assert "page_info=abc" in seen[1]


def test_shopify_schema_updates_an_existing_metafield_instead_of_reposting():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"metafields": [{"id": 55, "namespace": "automarket", "key": "json_ld"}]}
            )
        return httpx.Response(200, json={"metafield": {"id": 55}})

    shop = _make(ShopifyConnector, SHOP, handler)
    assert shop.inject_schema("product:9", json_ld={"@type": "Product"})
    assert seen[0].url.path.endswith("/products/9/metafields.json")
    assert seen[1].method == "PUT"
    assert seen[1].url.path.endswith("/products/9/metafields/55.json")

    seen.clear()

    def empty(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"metafields": []} if request.method == "GET" else {"metafield": {}})

    _make(ShopifyConnector, SHOP, empty).inject_schema("12", json_ld={"@type": "WebPage"})
    assert seen[1].method == "POST" and seen[1].url.path.endswith("/pages/12/metafields.json")
    assert json.loads(seen[1].content)["metafield"]["key"] == "json_ld"


# ── Webflow ────────────────────────────────────────────────────────────────
from app.connectors.webflow.connector import WebflowConnector  # noqa: E402

WEBFLOW = {"siteId": "site", "siteToken": "t"}


def _webflow_handler(seen: list[httpx.Request]):  # noqa: ANN202
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/collections"):
            return httpx.Response(200, json={"collections": [{"id": "c1", "slug": "blog"}]})
        if path.endswith("/items"):
            offset = int(_query(request)["offset"][0])
            limit = int(_query(request)["limit"][0])
            items = [
                {"id": f"i{n}", "fieldData": {"slug": f"post-{n}", "name": "N"}}
                for n in range(offset, min(offset + limit, 150))
            ]
            return httpx.Response(200, json={"items": items, "pagination": {"offset": offset, "limit": limit, "total": 150}})
        return httpx.Response(200, json={"id": "i3", "fieldData": {"slug": "post-3"}})

    return handler


def test_webflow_pages_by_offset():
    seen: list[httpx.Request] = []
    pages = _make(WebflowConnector, WEBFLOW, _webflow_handler(seen)).list_pages(limit=500)
    assert len(pages) == 150
    offsets = [_query(r)["offset"][0] for r in seen if r.url.path.endswith("/items")]
    assert offsets == ["0", "100"]


def test_webflow_read_page_url_matches_list_pages():
    seen: list[httpx.Request] = []
    webflow = _make(WebflowConnector, WEBFLOW, _webflow_handler(seen))
    page = webflow.read_page("c1:i3")
    assert page.url == "/blog/post-3"


# ── Magento ────────────────────────────────────────────────────────────────
from app.connectors.magento.connector import MagentoConnector  # noqa: E402


def test_magento_pages_with_current_page_until_total_count():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        current = int(_query(request)["searchCriteria[currentPage]"][0])
        size = int(_query(request)["searchCriteria[pageSize]"][0])
        start = (current - 1) * size
        items = [{"id": n, "identifier": f"p{n}"} for n in range(start, min(start + size, 230))]
        return httpx.Response(200, json={"items": items, "total_count": 230})

    magento = _make(MagentoConnector, {"baseUrl": "https://m.test", "accessToken": "t"}, handler)
    assert len(magento.list_pages(limit=1000)) == 230
    assert [_query(r)["searchCriteria[currentPage]"][0] for r in seen] == ["1", "2", "3"]

    seen.clear()
    assert len(magento.list_pages(limit=40)) == 40
    assert len(seen) == 1


# ── Salesforce ─────────────────────────────────────────────────────────────
from app.connectors.salesforce.connector import SalesforceConnector  # noqa: E402

SF = {"instanceUrl": "https://org.my.salesforce.com", "accessToken": "t"}


def test_salesforce_escapes_lead_source_in_soql():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"totalSize": 3})

    sf = _make(SalesforceConnector, {**SF, "leadSource": "Partner's Web"}, handler)
    assert sf.read_conversions(days=30) == 3
    soql = _query(seen[0])["q"][0]
    assert "LeadSource = 'Partner\\'s Web'" in soql


def test_salesforce_signals_follow_next_records_url():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(unquote(str(request.url)))
        if "/query/" in request.url.path:
            return httpx.Response(200, json={"done": True, "records": [{"WhoId": "b", "Type": "Email", "total": 1}]})
        return httpx.Response(
            200,
            json={
                "done": False,
                "nextRecordsUrl": "/services/data/v62.0/query/01g-2000",
                "records": [{"WhoId": "a", "Type": "Call", "total": 2}],
            },
        )

    rows = _make(SalesforceConnector, SF, handler).read_first_party_signals(limit=10)
    assert [r["signals"] for r in rows] == [{"call_click": 2}, {"email_open": 1}]
    assert seen[1].endswith("/services/data/v62.0/query/01g-2000")


def test_salesforce_token_url_follows_the_login_url_field():
    assert SalesforceConnector(Credentials(values=SF)).token_url == (
        "https://login.salesforce.com/services/oauth2/token"
    )
    sandbox = SalesforceConnector(Credentials(values={**SF, "loginUrl": "https://test.salesforce.com/"}))
    assert sandbox.token_url == "https://test.salesforce.com/services/oauth2/token"
    assert _fields(SalesforceConnector)["loginUrl"].default == "https://login.salesforce.com"
