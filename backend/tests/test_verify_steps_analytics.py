"""Live checks for the analytics group, run against a recording fake vendor.

Every step here is a read. The tests prove each one calls the endpoint the
agents call, with small request sizes, and that nothing secret ends up in the
report. Google Business Profile has no write step on purpose (a review reply
is public and the reviewer is told about it); that is asserted too.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from tests.support.verify_http import assert_no_secrets, install, run_all

TOKEN = "ya29.test-access-token-value"


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


# ── Google Analytics 4 ─────────────────────────────────────────────────────
def test_ga4_lists_properties_and_reads_yesterday():
    ga = _connector("google_analytics_4", {"propertyId": "123", "accessToken": TOKEN})
    rec = install(
        ga,
        [
            (
                "GET",
                r"^https://analyticsadmin\.googleapis\.com/v1beta/accountSummaries\?",
                {
                    "accountSummaries": [
                        {"displayName": "Acme", "propertySummaries": [{"property": "properties/123", "displayName": "Web"}]}
                    ]
                },
            ),
            (
                "POST",
                r"^https://analyticsdata\.googleapis\.com/v1beta/properties/123:runReport$",
                lambda req: {
                    "rows": [
                        {"dimensionValues": [{"value": "20261012"}], "metricValues": [{"value": "7"}, {"value": "1"}, {"value": "0"}]}
                    ]
                }
                if b'"date"' in req.content
                else {
                    "rows": [
                        {"dimensionValues": [{"value": "bing.com"}], "metricValues": [{"value": "3"}, {"value": "0.4"}, {"value": "30"}]}
                    ]
                },
            ),
        ],
    )
    results = run_all(ga)
    assert list(results) == ["list_properties", "sessions_yesterday", "referrers_yesterday"]
    assert "one of them" in results["list_properties"][0]
    assert results["list_properties"][1]["property_ids"] == ["123"]
    assert results["sessions_yesterday"][1] == {"days": 1, "sessions": 7}
    bodies = [r for r in rec.calls if r.method == "POST"]
    assert len(bodies) == 2
    sessions_body = rec.body("POST", r":runReport$", 0)
    assert sessions_body["dateRanges"] == [{"startDate": "1daysAgo", "endDate": "today"}]
    referrers_body = rec.body("POST", r":runReport$", 1)
    assert referrers_body["limit"] == 5
    assert referrers_body["dimensions"] == [{"name": "sessionSource"}]
    assert all(c.headers["Authorization"] == "Bearer " + TOKEN for c in rec.calls)
    assert_no_secrets(ga, results)


# ── Search Console ─────────────────────────────────────────────────────────
def test_search_console_lists_sites_and_reads_one_day():
    site = "https://example.com/"
    gsc = _connector("search_console", {"siteUrl": site, "accessToken": TOKEN})
    rec = install(
        gsc,
        [
            ("GET", r"/webmasters/v3/sites$", {"siteEntry": [{"siteUrl": site, "permissionLevel": "siteOwner"}]}),
            (
                "POST",
                r"/webmasters/v3/sites/https%3A%2F%2Fexample\.com%2F/searchAnalytics/query$",
                {"rows": [{"keys": ["salon"], "clicks": 2, "impressions": 10, "position": 3.0}]},
            ),
        ],
    )
    results = run_all(gsc)
    assert "one of them" in results["list_sites"][0]
    body = rec.body("POST", r"searchAnalytics/query")
    assert body["rowLimit"] == 25 and body["dimensions"] == ["query"]
    end = date.fromisoformat(body["endDate"])
    assert date.fromisoformat(body["startDate"]) == end - timedelta(days=1)
    assert results["search_performance"][1] == {"queries": 1, "clicks": 2, "impressions": 10}
    assert_no_secrets(gsc, results)


# ── Adobe Analytics ────────────────────────────────────────────────────────
ADOBE = {"reportSuiteId": "acmeprod", "clientId": "client-id-1", "clientSecret": "adobe-secret-value"}


def _adobe(routes: list) -> tuple:  # noqa: ANN001
    adobe = _connector("adobe_analytics", dict(ADOBE))
    rec = install(adobe, routes)
    # Adobe's sign-in and company lookup use the connector's own short-lived
    # client; route it through the same recorder.
    adobe._ims_client = lambda: httpx.Client(transport=httpx.MockTransport(rec))
    return adobe, rec


def test_adobe_signs_in_finds_company_lists_suites_and_reads_yesterday():
    adobe, rec = _adobe(
        [
            ("POST", r"^https://ims-na1\.adobelogin\.com/ims/token/v3$", {"access_token": "adobe-at", "expires_in": 86400}),
            (
                "GET",
                r"^https://analytics\.adobe\.io/discovery/me$",
                {"imsOrgs": [{"imsOrgId": "ORG@AdobeOrg", "companies": [{"globalCompanyId": "acmeco"}]}]},
            ),
            (
                "GET",
                r"/api/acmeco/reportsuites/collections/suites\?",
                {"content": [{"rsid": "acmeprod"}, {"rsid": "acmedev"}], "totalElements": 2},
            ),
            ("GET", r"/api/acmeco/reportsuites/collections/suites/acmeprod$", {"rsid": "acmeprod", "name": "Prod"}),
            (
                "POST",
                r"^https://analytics\.adobe\.io/api/acmeco/reports$",
                {"rows": [{"value": "2026-10-12", "data": [5, 0, 0]}]},
            ),
        ]
    )
    results = run_all(adobe)
    assert list(results) == ["sign_in_company", "list_report_suites", "sessions_yesterday", "referrers_yesterday"]
    assert results["sign_in_company"][1] == {"global_company_id": "acmeco"}
    assert results["list_report_suites"][1] == {"count": 2, "rsids": ["acmeprod", "acmedev"]}
    listing = rec.find("GET", r"collections/suites\?")[0]
    assert _query(listing)["limit"] == ["10"]
    reports = rec.find("POST", r"/reports$")
    assert [rec.body("POST", r"/reports$", i)["dimension"] for i in range(2)] == [
        "variables/daterangeday",
        "variables/referrerdomain",
    ]
    assert rec.body("POST", r"/reports$", 1)["settings"]["limit"] == 5
    assert all(r.headers["x-proxy-global-company-id"] == "acmeco" for r in reports)
    assert all(r.headers["x-api-key"] == "client-id-1" for r in reports)
    assert_no_secrets(adobe, results)


def test_adobe_report_suite_not_reachable_fails_clearly():
    import pytest

    from app.core.exceptions import ConnectorError

    adobe, _ = _adobe(
        [
            ("POST", r"ims/token/v3$", {"access_token": "adobe-at"}),
            ("GET", r"discovery/me$", {"imsOrgs": [{"imsOrgId": "O", "companies": [{"globalCompanyId": "acmeco"}]}]}),
            ("GET", r"collections/suites\?", {"content": [], "totalElements": 0}),
            ("GET", r"collections/suites/acmeprod$", (404, {"errorCode": "not_found"})),
        ]
    )
    step = next(s for s in adobe.verify_steps() if s.key == "list_report_suites")
    with pytest.raises(ConnectorError):
        step.run()


# ── Bing Webmaster Tools ───────────────────────────────────────────────────
def test_bing_with_a_key_lists_sites_and_reads_query_stats():
    bing = _connector("bing_webmaster", {"siteUrl": "https://example.com/", "apiKey": "bing-key-1234"})
    rec = install(
        bing,
        [
            ("GET", r"^https://ssl\.bing\.com/webmaster/api\.svc/json/GetUserSites\?", {"d": [{"Url": "https://example.com/", "IsVerified": True}]}),
            ("GET", r"^https://ssl\.bing\.com/webmaster/api\.svc/json/GetQueryStats\?", {"d": [{"Query": "salon", "Clicks": 4, "Impressions": 9, "AvgClickPosition": 2}]}),
        ],
    )
    results = run_all(bing)
    assert "one of them" in results["list_sites"][0]
    assert _query(rec.find("GET", "GetUserSites")[0]) == {"apikey": ["bing-key-1234"]}
    assert _query(rec.find("GET", "GetQueryStats")[0])["siteUrl"] == ["https://example.com/"]
    assert results["query_stats"][1] == {"queries": 1, "clicks": 4, "impressions": 9}
    assert_no_secrets(bing, results)


def test_bing_signed_in_uses_bearer_on_www_bing_com():
    bing = _connector(
        "bing_webmaster",
        {"siteUrl": "https://example.com/", "accessToken": TOKEN, "oauthSource": "platform"},
    )
    rec = install(bing, [("GET", r"^https://www\.bing\.com/webmaster/api\.svc/json/", {"d": []})])
    results = run_all(bing)
    assert all("apikey" not in _query(c) for c in rec.calls)
    assert all(c.headers["Authorization"] == "Bearer " + TOKEN for c in rec.calls)
    assert_no_secrets(bing, results)


# ── DataForSEO Backlinks ───────────────────────────────────────────────────
def test_dataforseo_reads_account_free_then_one_backlink_row():
    dfs = _connector("dataforseo_backlinks", {"login": "me@example.com", "password": "dfs-password"})
    rec = install(
        dfs,
        [
            (
                "GET",
                r"^https://api\.dataforseo\.com/v3/appendix/user_data$",
                {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"login": "me@example.com", "money": {"balance": 12.5}}]}]},
            ),
            (
                "POST",
                r"^https://api\.dataforseo\.com/v3/backlinks/backlinks/live$",
                {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": [{"domain_from": "news.example.org", "domain_from_rank": 400}]}]}]},
            ),
        ],
    )
    results = run_all(dfs)
    assert rec.index("GET", "user_data") < rec.index("POST", "backlinks/live")
    assert rec.body("POST", "backlinks/live") == [{"target": "dataforseo.com", "limit": 1, "mode": "one_per_domain"}]
    assert results["account"][1] == {"account_found": True, "balance_usd": 12.5}
    assert results["backlinks_one_row"][1]["count"] == 1
    # The login is an email address: kept out of the report.
    assert "me@example.com" not in str(results)
    assert_no_secrets(dfs, results)


def test_dataforseo_paid_step_says_it_costs_money():
    dfs = _connector("dataforseo_backlinks", {"login": "a", "password": "b"})
    labels = {s.key: s.label for s in dfs.verify_steps()}
    assert "free" in labels["account"].lower()
    assert "paid" in labels["backlinks_one_row"].lower()


# ── PageSpeed Insights ─────────────────────────────────────────────────────
def test_pagespeed_runs_once_on_the_example_page():
    psi = _connector("pagespeed_insights", {"apiKey": "AIzaTestKey0123456789abcdefghijklmnop"})
    rec = install(
        psi,
        [
            (
                "GET",
                r"^https://www\.googleapis\.com/pagespeedonline/v5/runPagespeed\?",
                {"loadingExperience": {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 1800}}}},
            )
        ],
    )
    results = run_all(psi)
    assert len(rec.calls) == 1
    q = _query(rec.calls[0])
    assert q["url"] == ["https://developers.google.com/speed"]
    assert q["category"] == ["PERFORMANCE"] and q["strategy"] == ["mobile"]
    assert q["key"] == ["AIzaTestKey0123456789abcdefghijklmnop"]
    assert results["run_pagespeed"][1]["measured"] is True
    assert_no_secrets(psi, results)


# ── Google Business Profile ────────────────────────────────────────────────
GBP = {"locationId": "locations/55", "accountId": "accounts/9", "accessToken": TOKEN}


def test_business_profile_reads_only():
    gbp = _connector("google_business_profile", dict(GBP))
    yesterday = date.today() - timedelta(days=1)
    rec = install(
        gbp,
        [
            ("GET", r"^https://mybusinessaccountmanagement\.googleapis\.com/v1/accounts\?", {"accounts": [{"name": "accounts/9", "accountName": "Acme"}]}),
            (
                "GET",
                r"^https://mybusinessbusinessinformation\.googleapis\.com/v1/accounts/9/locations\?",
                {"locations": [{"name": "locations/55", "title": "Acme Salon"}]},
            ),
            (
                "GET",
                r"^https://mybusinessbusinessinformation\.googleapis\.com/v1/locations/55\?",
                {"title": "Acme Salon", "storefrontAddress": {"addressLines": ["1 Main St"]}, "websiteUri": "https://example.com"},
            ),
            (
                "GET",
                r"^https://businessprofileperformance\.googleapis\.com/v1/locations/55:fetchMultiDailyMetricsTimeSeries\?",
                {
                    "multiDailyMetricTimeSeries": [
                        {
                            "dailyMetricTimeSeries": [
                                {"timeSeries": {"datedValues": [{"date": {"year": yesterday.year, "month": yesterday.month, "day": yesterday.day}, "value": "6"}]}},
                                {"timeSeries": {"datedValues": [{"date": {"year": yesterday.year, "month": yesterday.month, "day": yesterday.day}, "value": "4"}]}},
                            ]
                        }
                    ]
                },
            ),
            (
                "GET",
                r"^https://mybusiness\.googleapis\.com/v4/accounts/9/locations/55/reviews\?",
                {"reviews": [{"name": "accounts/9/locations/55/reviews/r1", "reviewer": {"displayName": "Jane Doe"}, "starRating": "FIVE", "reviewReply": {"comment": "Thanks"}}]},
            ),
        ],
    )
    steps = gbp.verify_steps()
    assert all(s.kind == "read" for s in steps)
    results = run_all(gbp)
    assert list(results) == ["list_locations", "read_location", "impressions_yesterday", "read_reviews"]
    assert "one of them" in results["list_locations"][0]
    perf = _query(rec.find("GET", "fetchMultiDailyMetricsTimeSeries")[0])
    assert perf["dailyRange.startDate.day"] == perf["dailyRange.endDate.day"] == [str(yesterday.day)]
    assert perf["dailyMetrics"] == ["BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH"]
    assert results["impressions_yesterday"][1] == {"impressions": 10, "series": 1}
    reviews = _query(rec.find("GET", "/reviews")[0])
    assert reviews["pageSize"] == ["5"]
    assert results["read_reviews"][1] == {"count": 1, "answered": 1}
    # Nothing written, and no reviewer names in the report.
    assert all(c.method == "GET" for c in rec.calls)
    assert "Jane" not in str(results)
    assert_no_secrets(gbp, results)


def test_business_profile_impressions_step_surfaces_a_failure():
    """read_sessions swallows errors for the Reports screen; the check must not."""
    import pytest

    from app.core.exceptions import ConnectorError

    gbp = _connector("google_business_profile", dict(GBP))
    install(gbp, [("GET", r"fetchMultiDailyMetricsTimeSeries", (403, {"error": {"status": "PERMISSION_DENIED"}}))])
    step = next(s for s in gbp.verify_steps() if s.key == "impressions_yesterday")
    with pytest.raises(ConnectorError):
        step.run()


# ── Google Places ──────────────────────────────────────────────────────────
def test_places_makes_one_text_search_for_the_business():
    places = _connector(
        "google_places", {"apiKey": "AIzaPlacesKey0123456789abcdefghijklmn", "businessName": "Acme Salon, Noida"}
    )
    rec = install(
        places,
        [
            (
                "POST",
                r"^https://places\.googleapis\.com/v1/places:searchText$",
                {"places": [{"id": "ChIJ123", "displayName": {"text": "Acme Salon"}, "location": {"latitude": 1.0, "longitude": 2.0}}]},
            )
        ],
    )
    results = run_all(places)
    assert len(rec.calls) == 1
    assert rec.body("POST", "searchText") == {"textQuery": "Acme Salon, Noida", "pageSize": 1}
    assert rec.calls[0].headers["X-Goog-FieldMask"].startswith("places.id")
    assert results["find_business"][1] == {"place_id": "ChIJ123", "has_location": True}
    assert "paid" in places.verify_steps()[0].label.lower()
    assert_no_secrets(places, results)
