"""Search Campaign Builder: checks, budget, exports, publishing and the run."""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.agents.search_campaign_builder import GUIDE, library, validate
from app.agents.search_campaign_builder.agent import AGENT, daily_budget
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.google_ads.connector import GoogleAdsConnector
from app.core.exceptions import ConflictError, ConnectorError
from app.models.content import AssetStatus, MarketingAsset
from app.models.seo import SeoPage
from app.services import assets

PROFILE_TEXT = (
    "BUSINESS: GEET Salon\nPRODUCTS/SERVICES: Haircuts; Hair colour; Bridal makeup\n"
    "VALUE PROPOSITIONS: Open since 2015; Haircuts from ₹499\nLOCATIONS SERVED: Noida"
)
PAGES = ["https://geet.example/", "https://geet.example/haircuts", "https://geet.example/hair-colour",
         "https://geet.example/bridal"]


def _rules() -> validate.Rules:
    return validate.Rules.build(
        profile_text=PROFILE_TEXT, business_name="GEET Salon", pages=PAGES, fallback_url=PAGES[0]
    )


def _headlines(prefix: str, n: int = 15) -> list[str]:
    return [f"{prefix} option {chr(97 + i)}" for i in range(n)]


def _group(name: str, url: str, *, keywords: int = 10, headlines: int = 15) -> dict:
    return {
        "name": name,
        "theme": name,
        "landing_url": url,
        "keywords": [
            {"text": f"{name.lower()} service {chr(97 + i)}", "match_type": ("exact", "phrase", "broad")[i % 3]}
            for i in range(keywords)
        ],
        "ad": {
            "headlines": _headlines(name[:12], headlines),
            "descriptions": [f"{name} by trained stylists in Noida.", f"Book your {name.lower()} visit online.",
                             "Friendly team, clean studio.", "Walk in or book ahead."],
            "path1": "hair cuts",
            "path2": "noida",
        },
    }


def _draft(groups: list[dict]) -> dict:
    return {
        "campaign_name": "Salon services",
        "recommended_daily_budget": 750,
        "ad_groups": groups,
        "negative_keywords": [{"text": "jobs", "match_type": "broad"}, {"text": "free", "match_type": "exact"},
                              {"text": "haircuts service a", "match_type": "exact"}],
        "sitelinks": [
            {"text": "Haircuts", "description1": "Cuts for everyone", "description2": "Book a slot", "url": PAGES[1]},
            {"text": "Hair colour", "description1": "Colour by experts", "description2": "See shades", "url": PAGES[2]},
            {"text": "Elsewhere", "description1": "x", "description2": "y", "url": "https://other.example/"},
        ],
        "callouts": ["Online booking", "BEST IN TOWN"],
    }


# ── Validation and repair ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("A headline that is far too long to fit", "longer than 30"),
        ("Book now!", "exclamation mark in a headline"),
        ("AMAZING haircuts", "capitals"),
        ("Best salon in Noida", "claim"),
        ("Haircuts at ₹299", "number"),
        ("20 years of styling", "number"),
        ("Guaranteed great hair", "claim"),
        ("Free consultation", "claim"),
        ("Haircuts ★ Noida", "symbols"),
        ("Hair, colour,, more", "repeated punctuation"),
    ],
)
def test_bad_headlines_are_refused(line: str, reason: str):
    assert reason in (validate.text_problem(line, _rules(), limit=30, headline=True) or "")


@pytest.mark.parametrize(
    "line",
    ["GEET Salon haircuts", "SEO friendly booking", "Haircuts from ₹499", "Open since 2015", "Haircuts in Noida"],
)
def test_lines_the_profile_supports_pass(line: str):
    assert validate.text_problem(line, _rules(), limit=30, headline=True) is None


def test_a_draft_is_repaired_by_dropping_lines_and_groups():
    good = _group("Haircuts", "https://GEET.example/haircuts/")
    good["ad"]["headlines"] += ["Book now!", "Haircuts option a"]  # bad, and a duplicate
    good["ad"]["descriptions"] = ["One great cut!", "Another visit!", "Plain line here.", "Last line."]
    good["keywords"].append({"text": "[Haircuts Service A]", "match_type": "EXACT"})  # a duplicate once cleaned
    good["keywords"].append({"text": '"short hair"', "match_type": "weird"})
    unknown_page = _group("Hair colour", "https://evil.example/hair-colour")
    too_few = _group("Bridal makeup", PAGES[3], keywords=5)
    cleaned, report, problems = validate.campaign(_draft([good, unknown_page, too_few]), _rules())

    names = [g["name"] for g in cleaned["ad_groups"]]
    assert names == ["Haircuts", "Hair colour"]
    assert any("Bridal makeup" in p and "keywords" in p for p in problems)
    haircuts = cleaned["ad_groups"][0]
    assert haircuts["landing_url"] == "https://geet.example/haircuts"  # the known page's own spelling
    # A page the site does not have is replaced by the closest one that it does.
    assert cleaned["ad_groups"][1]["landing_url"] == "https://geet.example/hair-colour"
    assert len(haircuts["ad"]["headlines"]) == 15 and "Book now!" not in haircuts["ad"]["headlines"]
    # One exclamation mark per ad: the second description with one is dropped.
    assert haircuts["ad"]["descriptions"] == ["One great cut!", "Plain line here.", "Last line."]
    assert {"text": "short hair", "match_type": "phrase"} in haircuts["keywords"]
    assert len(haircuts["keywords"]) == 11
    assert haircuts["ad"]["path1"] == "hair-cuts" and haircuts["ad"]["path2"] == "noida"
    # A negative that would block one of the campaign's own keywords is dropped.
    assert [k["text"] for k in cleaned["negative_keywords"]] == ["jobs", "free"]
    assert [s["text"] for s in cleaned["sitelinks"]] == ["Haircuts", "Hair colour"]
    assert cleaned["callouts"] == ["Online booking"]
    assert report.dropped_lines >= 5 and report.dropped_groups == 1
    assert validate.usable(cleaned)


def test_too_few_groups_is_not_usable_and_a_retry_is_merged():
    first, _, problems = validate.campaign(_draft([_group("Haircuts", PAGES[1])]), _rules())
    assert not validate.usable(first) and any("only 1 usable" in p for p in problems)
    second, _, _ = validate.campaign(_draft([_group("Haircuts", PAGES[1]), _group("Bridal", PAGES[3])]), _rules())
    merged = validate.merge(first, second)
    assert [g["name"] for g in merged["ad_groups"]] == ["Haircuts", "Bridal"]


def test_sitelinks_need_two():
    rules = _rules()
    report = validate.Report()
    one = [{"text": "Haircuts", "url": PAGES[1]}]
    assert validate.sitelinks(one, rules, report) == []


# ── Budget ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("monthly", "daily"), [(30400, 1000.0), (1000, 32.89), (50000, 1644.73), (0, 0.0)])
def test_daily_budget_never_exceeds_the_month(monthly: float, daily: float):
    assert daily_budget(monthly) == daily
    assert daily * 30.4 <= monthly + 1e-9


# ── Exports ──────────────────────────────────────────────────────────────────
def _content() -> dict:
    cleaned, _, _ = validate.campaign(_draft([_group("Haircuts", PAGES[1]), _group("Bridal", PAGES[3])]), _rules())
    return {**cleaned, "name": "Salon services 2026-10-05", "daily_budget": 1644.73, "goal": "Leads",
            "budget_is_recommendation": False, "locations": ["Noida"], "themes": ["Haircuts"],
            "microsoft_time_zone": "EasternTimeUSCanada"}


def _asset(channel: str = "google_ads", **detail) -> MarketingAsset:  # noqa: ANN003
    return MarketingAsset(
        id="a1", tenant_id="t", agent_slug="search_campaign_builder", kind="search_campaign", channel=channel,
        title="Salon services", content_encrypted=json.dumps(_content()), status=AssetStatus.DRAFT,
        detail=dict(detail), image_ids=[],
    )


class _Cipher:
    def encrypt(self, value, *, context):  # noqa: ANN001
        return value

    def decrypt(self, value, *, context, default=""):  # noqa: ANN001
        return value


def test_google_ads_editor_file_parses_back():
    file = assets.export(_asset(), _Cipher(), "google_ads_editor")
    assert file.filename.endswith("-google-ads-editor.csv")
    assert file.data[:2] == b"\xff\xfe"  # UTF-16 little-endian byte-order mark
    rows = list(csv.DictReader(io.StringIO(file.data.decode("utf-16")), delimiter="\t"))
    assert rows[0]["Campaign"] == "Salon services 2026-10-05"
    assert rows[0]["Campaign Type"] == "Search" and rows[0]["Campaign Status"] == "Paused"
    assert rows[0]["Campaign Daily Budget"] == "1644.73" and rows[0]["Networks"] == "Google search"
    groups = [r for r in rows if r["Ad Group Status"]]
    assert [g["Ad Group"] for g in groups] == ["Haircuts", "Bridal"]
    keywords = [r for r in rows if r["Keyword"] and r["Ad Group"]]
    assert {r["Criterion Type"] for r in keywords} == {"Exact", "Phrase", "Broad"}
    negatives = [r for r in rows if r["Criterion Type"].startswith("Negative")]
    assert [(r["Keyword"], r["Criterion Type"], r["Ad Group"]) for r in negatives] == [
        ("jobs", "Negative Broad", ""), ("free", "Negative Exact", "")]
    ads = [r for r in rows if r["Ad type"] == "Responsive search ad"]
    assert len(ads) == 2
    assert ads[0]["Headline 15"] and ads[0]["Description 4"] and ads[0]["Final URL"] == PAGES[1]
    assert ads[0]["Path 1"] == "hair-cuts"


def test_microsoft_bulk_file_parses_back():
    file = assets.export(_asset("microsoft_ads"), _Cipher(), "microsoft_bulk")
    rows = list(csv.DictReader(io.StringIO(file.data.decode("utf-8-sig"))))
    assert rows[0]["Type"] == "Format Version" and rows[0]["Name"] == "6.0"
    campaign = rows[1]
    assert (campaign["Type"], campaign["Status"], campaign["Id"]) == ("Campaign", "Paused", "-1")
    assert campaign["Budget"] == "1644.73" and campaign["Budget Type"] == "DailyBudgetStandard"
    assert campaign["Time Zone"] == "EasternTimeUSCanada" and campaign["Campaign Type"] == "Search"
    groups = [r for r in rows if r["Type"] == "Ad Group"]
    assert [(g["Id"], g["Parent Id"]) for g in groups] == [("-10", "-1"), ("-11", "-1")]
    keywords = [r for r in rows if r["Type"] == "Keyword"]
    assert {r["Parent Id"] for r in keywords} == {"-10", "-11"}
    assert {r["Match Type"] for r in keywords} == {"Exact", "Phrase", "Broad"}
    negatives = [r for r in rows if r["Type"] == "Campaign Negative Keyword"]
    # Microsoft has no broad negatives.
    assert [(r["Keyword"], r["Match Type"]) for r in negatives] == [("jobs", "Phrase"), ("free", "Exact")]
    ad = next(r for r in rows if r["Type"] == "Responsive Search Ad")
    assert len(json.loads(ad["Headline"])) == 15 and json.loads(ad["Description"])[0]["text"]
    assert ad["Final Url"] == PAGES[1] and ad["Parent Id"] == "-10"


def test_plain_spreadsheet_and_generic_formats():
    file = assets.export(_asset(), _Cipher(), "csv")
    rows = list(csv.reader(io.StringIO(file.data.decode("utf-8-sig"))))
    assert rows[0] == library.PLAIN_COLUMNS
    assert ["Campaign", "", "Daily budget", "1644.73", "", "", ""] in rows
    assert any(r[2] == "Headline 1" and r[5] == str(len(r[3])) for r in rows)
    assert set(assets.formats_for("search_campaign")) >= {"google_ads_editor", "microsoft_bulk", "csv", "json", "txt"}


# ── Publishing ───────────────────────────────────────────────────────────────
def _bundle(monkeypatch, instances: dict) -> None:  # noqa: ANN001
    import app.services.connectors as connectors

    monkeypatch.setattr(connectors, "build_connector_bundle", lambda db, org: SimpleNamespace(instances=instances))


def test_publisher_routes_google_assets_to_the_connector(monkeypatch):
    from app.connectors.google_ads.connector import SearchCampaignCreated

    calls: list[dict] = []

    class _Google:
        def supports(self, capability):  # noqa: ANN001
            return capability == Capability.CREATE_SEARCH_CAMPAIGN

        def create_search_campaign(self, **kwargs):  # noqa: ANN003
            calls.append(kwargs)
            return SearchCampaignCreated(resource_name="customers/1/campaigns/55")

    _bundle(monkeypatch, {"google_ads": _Google()})
    asset = _asset()
    assets.publish(SimpleNamespace(flush=lambda: None), SimpleNamespace(id="t"), asset, _Cipher())
    assert asset.status == AssetStatus.PUBLISHED
    assert asset.remote_id == "customers/1/campaigns/55" and "campaignId=55" in asset.remote_url
    assert calls[0]["daily_budget"] == 1644.73 and len(calls[0]["ad_groups"]) == 2
    assert asset.detail["extensions_added"] is True


def test_publisher_refuses_microsoft_and_a_missing_google(monkeypatch):
    _bundle(monkeypatch, {})
    with pytest.raises(ConflictError, match="Microsoft Advertising bulk file"):
        library.publish(None, None, _asset("microsoft_ads"), _content())
    with pytest.raises(ConnectorError, match="Google Ads is not connected"):
        library.publish(None, None, _asset(), _content())


def test_export_only_assets_are_approved_not_published(monkeypatch):
    def boom(*_):  # noqa: ANN002
        raise AssertionError("must not publish")

    monkeypatch.setitem(assets.PUBLISHERS, "search_campaign", boom)
    asset = _asset("microsoft_ads", export_only=True)
    assets.release(SimpleNamespace(flush=lambda: None), SimpleNamespace(id="t"), asset, _Cipher())
    assert asset.status == AssetStatus.APPROVED


# ── Google Ads create (request bodies) ───────────────────────────────────────
def _google(handler, monkeypatch) -> GoogleAdsConnector:  # noqa: ANN001
    connector = GoogleAdsConnector(
        Credentials(values={"customerId": "123-456-7890", "developerToken": "dev", "accessToken": "t"}), org_id="o"
    )
    monkeypatch.setattr(GoogleAdsConnector, "access_token", property(lambda self: "t"))
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


def _create(connector: GoogleAdsConnector):  # noqa: ANN202
    content = _content()
    return connector.create_search_campaign(
        name=content["name"], daily_budget=content["daily_budget"], ad_groups=content["ad_groups"],
        negative_keywords=content["negative_keywords"], sitelinks=content["sitelinks"],
        callouts=content["callouts"],
    )


def test_google_creates_the_whole_campaign_paused_in_one_mutate(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, json={"mutateOperationResponses": [
                {"campaignBudgetResult": {"resourceName": "customers/1234567890/campaignBudgets/9"}},
                {"campaignResult": {"resourceName": "customers/1234567890/campaigns/77"}},
            ]})
        return httpx.Response(200, json={"mutateOperationResponses": []})

    result = _create(_google(handler, monkeypatch))
    assert result.resource_name == "customers/1234567890/campaigns/77" and result.extensions_added
    assert [r.url.path for r in seen] == ["/v25/customers/1234567890/googleAds:mutate"] * 2
    assert seen[0].headers["developer-token"] == "dev"
    ops = json.loads(seen[0].content)["mutateOperations"]
    budget = ops[0]["campaignBudgetOperation"]["create"]
    assert budget["resourceName"] == "customers/1234567890/campaignBudgets/-1"
    assert budget["explicitlyShared"] is False and budget["amountMicros"] == 1_644_730_000
    campaign = ops[1]["campaignOperation"]["create"]
    assert campaign["status"] == "PAUSED" and campaign["advertisingChannelType"] == "SEARCH"
    assert campaign["campaignBudget"] == budget["resourceName"] and campaign["targetSpend"] == {}
    assert campaign["networkSettings"]["targetGoogleSearch"] is True
    assert campaign["networkSettings"]["targetContentNetwork"] is False
    kinds = [next(iter(op)) for op in ops]
    assert kinds.count("adGroupOperation") == 2 and kinds.count("adGroupAdOperation") == 2
    assert kinds.count("campaignCriterionOperation") == 2
    group = ops[2]["adGroupOperation"]["create"]
    assert group["resourceName"] == "customers/1234567890/adGroups/-10"
    assert group["campaign"] == campaign["resourceName"] and group["type"] == "SEARCH_STANDARD"
    keyword = ops[3]["adGroupCriterionOperation"]["create"]
    assert keyword["adGroup"] == group["resourceName"] and keyword["keyword"]["matchType"] in {"EXACT", "PHRASE", "BROAD"}
    ad = next(op["adGroupAdOperation"]["create"] for op in ops if "adGroupAdOperation" in op)
    rsa = ad["ad"]["responsiveSearchAd"]
    assert len(rsa["headlines"]) == 15 and len(rsa["descriptions"]) == 4 and rsa["path1"] == "hair-cuts"
    assert ad["ad"]["finalUrls"] == [PAGES[1]]
    negative = next(op["campaignCriterionOperation"]["create"] for op in ops if "campaignCriterionOperation" in op)
    assert negative == {"campaign": campaign["resourceName"], "negative": True,
                        "keyword": {"text": "jobs", "matchType": "BROAD"}}
    # Every operation that references an object comes after the one creating it.
    assert kinds.index("campaignOperation") < kinds.index("adGroupOperation") < kinds.index("adGroupAdOperation")
    extensions = json.loads(seen[1].content)["mutateOperations"]
    sitelink = extensions[0]["assetOperation"]["create"]
    assert sitelink["sitelinkAsset"]["linkText"] == "Haircuts" and sitelink["finalUrls"] == [PAGES[1]]
    link = extensions[1]["campaignAssetOperation"]["create"]
    assert link == {"campaign": result.resource_name, "asset": sitelink["resourceName"], "fieldType": "SITELINK"}
    assert extensions[-1]["campaignAssetOperation"]["create"]["fieldType"] == "CALLOUT"


def test_google_refusal_says_nothing_was_created(monkeypatch):
    connector = _google(lambda request: httpx.Response(400, json={"error": {"message": "bad"}}), monkeypatch)
    with pytest.raises(ConnectorError, match="nothing was created"):
        _create(connector)


def test_extensions_failing_keeps_the_campaign_and_says_what_is_missing(monkeypatch):
    responses = iter([
        httpx.Response(200, json={"mutateOperationResponses": [{"campaignResult": {"resourceName": "customers/1/campaigns/5"}}]}),
        httpx.Response(400, json={}),
    ])
    result = _create(_google(lambda request: next(responses), monkeypatch))
    assert result.resource_name == "customers/1/campaigns/5"
    assert result.extensions_added is False and "sitelinks and callouts" in result.note


def test_google_ads_offers_campaign_creation():
    assert Capability.CREATE_SEARCH_CAMPAIGN in GoogleAdsConnector.spec.capabilities
    assert any(o.capability == Capability.CREATE_SEARCH_CAMPAIGN for o in GUIDE.optional)
    assert {s.key for s in GUIDE.settings} >= {"monthly_budget", "campaign_goal", "locations", "include_bing"}


# ── The run ──────────────────────────────────────────────────────────────────
class _Rows:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def scalars(self):
        return iter(self.rows)


class _FakeDb:
    def __init__(self, pages: list, existing: list | None = None) -> None:
        self.pages, self.existing, self.added = pages, existing or [], []

    def add(self, row) -> None:  # noqa: ANN001
        row.id = row.id or f"asset{len(self.added)}"
        self.added.append(row)

    def flush(self) -> None:
        pass

    def execute(self, statement):  # noqa: ANN001
        entity = statement.column_descriptions[0]["entity"]
        return _Rows(self.pages if entity is SeoPage else self.existing)


def _ctx(db: _FakeDb, answers: list, *, settings: dict | None = None, connectors: dict | None = None,
         brief: str = PROFILE_TEXT, profile: dict | None = None):  # noqa: ANN001, ANN202
    answers = list(answers)
    prompts_seen: list[str] = []

    def ask_json(prompt, **_):  # noqa: ANN001, ANN003
        prompts_seen.append(prompt)
        return answers.pop(0)

    connectors = connectors or {}
    ctx = SimpleNamespace(
        db=db, tenant_id="t", run_id="run1", scope="", today=date(2026, 10, 5),
        org=SimpleNamespace(name="Geet", primary_domain="geet.example", settings={}),
        cipher=_Cipher(), brand_brief=brief,
        brand_profile=profile if profile is not None else {
            "business_name": "GEET Salon", "products_services": ["Haircuts", "Hair colour", "Bridal makeup"],
            "locations": ["Noida"]},
        setting=lambda key, default=None: (settings or {}).get(key, default),
        progress=lambda *a, **k: None, log_warning=lambda *a, **k: None,
        connectors=connectors, has_connector=lambda slug: slug in connectors,
        uses_connector=lambda slug, capability: slug in connectors,
        with_capability=lambda capability: next(
            (c for c in connectors.values() if c.supports(capability)), None),
        ask_json=ask_json,
    )
    return ctx, prompts_seen


def _pages() -> list[SeoPage]:
    return [SeoPage(url="/haircuts", title="Haircuts"), SeoPage(url="https://geet.example/hair-colour", title="Colour"),
            SeoPage(url="/bridal", title="Bridal")]


class _GoogleStub:
    def supports(self, capability):  # noqa: ANN001
        return capability in (Capability.CREATE_SEARCH_CAMPAIGN,)


class _Search:
    def supports(self, capability):  # noqa: ANN001
        return capability == Capability.READ_SEARCH_PERFORMANCE

    def read_search_performance(self, *, days):  # noqa: ANN001
        return {"top_queries": ["unisex salon noida"]}


def test_run_regenerates_once_and_writes_both_platforms():
    db = _FakeDb(_pages())
    bad = _draft([_group("Haircuts", PAGES[1], keywords=3)])
    good = _draft([_group("Haircuts", PAGES[1]), _group("Hair colour", PAGES[2])])
    ctx, seen = _ctx(db, [bad, good], settings={"monthly_budget": "30400.0", "include_bing": "yes"},
                     connectors={"google_ads": _GoogleStub(), "search_console": _Search()})
    result = AGENT.run(ctx)

    assert len(seen) == 2 and "previous draft could not be used" in seen[1]
    assert "GEET Salon" in seen[0] and "unisex salon noida" in seen[0]  # brand brief and queries
    assert "https://geet.example/haircuts" in seen[0]  # relative page joined to the domain
    google, microsoft = db.added
    assert (google.channel, microsoft.channel) == ("google_ads", "microsoft_ads")
    assert google.detail["export_only"] is False and microsoft.detail["export_only"] is True
    assert google.detail["keywords"] == 20 and google.detail["regenerated"] is True
    content = assets.content_of(google, _Cipher())
    assert content["daily_budget"] == 1000.0 and content["budget_is_recommendation"] is False
    assert content["themes"] == ["Haircuts", "Hair colour"] and content["name"].endswith("2026-10-05")
    action = result.actions[0]
    assert action.impact.value == "high" and action.target_kind == assets.TARGET_KIND
    assert action.target_id == google.id and action.payload["asset_id"] == google.id
    assert "1,000.00 a day" in action.payload["daily_budget"] and action.payload["keyword_count"] == 20
    assert action.metrics == {}
    assert "paused" in action.payload["what_happens"]
    assert "Bulk Operations" in result.actions[1].payload["what_happens"]
    assert "brand profile" in result.summary


def test_without_a_budget_the_daily_figure_is_a_recommendation_and_google_is_export_only():
    db = _FakeDb(_pages())
    good = _draft([_group("Haircuts", PAGES[1]), _group("Hair colour", PAGES[2])])
    ctx, _ = _ctx(db, [good], brief="", profile={"products_services": ["Haircuts", "Hair colour"]})
    result = AGENT.run(ctx)
    (asset,) = db.added  # Microsoft is neither connected nor asked for
    assert asset.detail["export_only"] is True and asset.detail["budget_is_recommendation"] is True
    assert assets.content_of(asset, _Cipher())["daily_budget"] == 750.0
    assert "Recommended" in result.actions[0].payload["budget_note"]
    assert "recommended" in result.summary and "no brand profile" in result.summary
    assert "Editor" in result.actions[0].payload["what_happens"]


def test_covered_themes_are_not_planned_again():
    done = MarketingAsset(kind="search_campaign", status=AssetStatus.APPROVED, detail={},
                          content_encrypted=json.dumps({"themes": ["haircuts", "Hair colour", "Bridal makeup"]}))
    rejected = MarketingAsset(kind="search_campaign", status=AssetStatus.REJECTED, detail={},
                              content_encrypted=json.dumps({"themes": ["Nails"]}))
    ctx, seen = _ctx(_FakeDb(_pages(), [done, rejected]), [],
                     profile={"products_services": ["Haircuts", "Hair colour", "Bridal makeup"]})
    result = AGENT.run(ctx)
    assert result.actions == [] and "already have a search campaign" in result.summary and not seen

    ctx, seen = _ctx(_FakeDb(_pages(), [done, rejected]), [{}, {}],
                     profile={"products_services": ["Haircuts", "Nails"]})
    result = AGENT.run(ctx)
    assert "- Nails" in seen[0] and "- Haircuts" not in seen[0]
    assert result.actions == [] and "did not pass the checks twice" in result.summary


def test_no_pages_and_no_domain_skips():
    ctx, _ = _ctx(_FakeDb([]), [])
    ctx.org.primary_domain = ""
    result = AGENT.run(ctx)
    assert result.skipped and "landing pages" in result.summary
