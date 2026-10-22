"""SEO Article Writer: planning, drafting checks, HTML safety, exports, publishing."""
from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.agents.seo_article_writer import GUIDE, RESULTS
from app.agents.seo_article_writer import agent as writer
from app.agents.seo_article_writer import html_tools as ht
from app.agents.seo_article_writer.agent import AGENT
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.bitbucket import BitbucketConnector
from app.connectors.github import GitHubConnector
from app.connectors.shopify.connector import ShopifyConnector
from app.connectors.webflow.connector import WebflowConnector
from app.connectors.wordpress.connector import WordPressConnector
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.services import assets

TODAY = date(2026, 10, 5)


# ── Sanitizing ───────────────────────────────────────────────────────────────
def test_sanitize_keeps_the_allowlist_and_drops_everything_dangerous():
    raw = (
        '<h1>Main</h1><div class="hero" onclick="steal()"><p style="color:red">Hi <b>there</b> '
        '<a href="javascript:alert(1)">click</a> <a href="/pricing" target="_blank">prices</a></p></div>'
        "<script>alert(1)</script><style>p{}</style><iframe src=x><p>framed</p></iframe>"
        "<img src=x onerror=boom()><p></p><h5>Deep</h5><p>Text</p>"
    )
    clean = ht.sanitize(raw)
    assert "<h1>" not in clean and "<h2>Main</h2>" in clean
    assert "<strong>there</strong>" in clean
    assert '<a href="/pricing">prices</a>' in clean  # attributes other than href dropped
    assert "javascript" not in clean and "click" in clean  # the link goes, its text stays
    for gone in ("script", "alert", "style", "iframe", "framed", "img", "onerror", "onclick", "class=", "<div", "<p></p>"):
        assert gone not in clean, gone
    assert "<h4>Deep</h4>" in clean


def test_a_link_rule_decides_which_links_survive():
    raw = '<p><a href="/real">real</a> <a href="/invented">made up</a> <a href="https://other.example/">x</a></p>'
    clean = ht.sanitize(raw, allow_href=lambda href: href == "/real")
    assert ht.links_in(clean) == ["/real"]
    assert "made up" in clean and "invented" not in clean


def test_loose_text_and_stray_items_are_given_structure():
    clean = ht.sanitize("Just text<li>stray</li>")
    assert clean.startswith("<p>Just text</p>") and "<ul><li>stray</li>" in clean


def test_empty_sections_are_found_but_subsections_count_as_content():
    body = "<h2>A</h2><h3>A1</h3><p>x</p><h2>B</h2><h2>C</h2><p>y</p><h3>D</h3>"
    assert ht.empty_sections(body) == ["B", "D"]


# ── Markdown ─────────────────────────────────────────────────────────────────
def test_markdown_conversion_of_every_allowlisted_tag():
    html = (
        "<h2>Title</h2><p>Some <strong>bold</strong>, <em>soft</em> and <code>x=1</code> "
        '<a href="/a b">link</a>.<br>Next line</p>'
        "<ul><li>One</li><li>Two<ul><li>Nested</li></ul></li></ul>"
        "<ol><li>First</li><li>Second</li></ol>"
        "<blockquote><p>Quoted</p></blockquote><pre>line 1\nline 2</pre>"
        "<h3>Sub</h3><table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2|3</td></tr></tbody></table>"
        "<p>Stars * and _under_ [brackets]</p><p># not a heading</p>"
    )
    md = ht.to_markdown(html)
    assert "## Title" in md
    assert "Some **bold**, *soft* and `x=1` [link](/a%20b).  \nNext line" in md
    assert "- One\n- Two\n   - Nested" in md
    assert "1. First\n2. Second" in md
    assert "> Quoted" in md
    assert "```\nline 1\nline 2\n```" in md
    assert "### Sub" in md
    assert "| A | B |\n| --- | --- |\n| 1 | 2\\|3 |" in md
    assert "Stars \\* and \\_under\\_ \\[brackets\\]" in md
    assert "\\# not a heading" in md


def _content(**overrides) -> dict:
    content = {
        "title": 'Hair colour aftercare: the "first week" rules',
        "slug": "hair-colour-aftercare",
        "meta_title": "Hair colour aftercare tips",
        "meta_description": "How to keep salon hair colour bright for longer, from the first wash onwards.",
        "target_keyword": "hair colour aftercare",
        "secondary_keywords": ["colour safe shampoo"],
        "body_html": "<h2>Wash less</h2><p>Wait 72 hours.</p><script>x()</script>",
        "faq_heading": "Questions",
        "faq": [{"q": "When can I wash?", "a": "After 72 hours."}],
    }
    content.update(overrides)
    return content


def _asset(content: dict | None = None, **kw) -> MarketingAsset:
    values = dict(
        id="a1", tenant_id="t", agent_slug=writer.SLUG, kind=AssetKind.ARTICLE, channel="wordpress",
        title="Hair colour aftercare", content_encrypted=json.dumps(content or _content()),
        status=AssetStatus.DRAFT, detail={}, image_ids=[], remote_id="", remote_url="",
    )
    values.update(kw)
    return MarketingAsset(**values)


class _PlainCipher:
    def encrypt(self, value, *, context):  # noqa: ANN001
        return value

    def decrypt(self, value, *, context, default=""):  # noqa: ANN001
        return value


def test_html_export_is_a_complete_document():
    file = assets.export(_asset(), _PlainCipher(), "html")
    text = file.data.decode()
    assert file.filename == "hair-colour-aftercare.html" and file.mime.startswith("text/html")
    assert text.startswith("<!doctype html>")
    assert "<title>Hair colour aftercare tips</title>" in text
    assert '<meta name="description" content="How to keep salon hair colour' in text
    assert "<h1>Hair colour aftercare: the &quot;first week&quot; rules</h1>" in text or (
        '<h1>Hair colour aftercare: the "first week" rules</h1>' in text
    )
    assert '<section class="faq">' in text and "<h3>When can I wash?</h3>" in text
    assert '"@type": "FAQPage"' in text
    assert "x()" not in text  # the body is sanitized on the way out too


def test_markdown_export_has_front_matter_and_the_faq():
    from datetime import datetime, timezone

    asset = _asset(scheduled_for=datetime(2026, 10, 9, 9, tzinfo=timezone.utc))
    file = assets.export(asset, _PlainCipher(), "markdown")
    text = file.data.decode()
    assert file.filename == "hair-colour-aftercare.md"
    assert text.startswith('---\ntitle: "Hair colour aftercare: the \\"first week\\" rules"\n')
    assert 'description: "How to keep salon hair colour' in text
    assert "date: 2026-10-09" in text
    assert 'keywords: ["hair colour aftercare", "colour safe shampoo"]' in text
    assert "## Wash less\n\nWait 72 hours." in text
    assert "## Questions\n\n### When can I wash?\n\nAfter 72 hours." in text
    assert assets.formats_for(AssetKind.ARTICLE)["markdown"] == "Markdown"


# ── Drafting checks ──────────────────────────────────────────────────────────
def _links() -> writer.SiteLinks:
    return writer.SiteLinks([("/services/colour", "Hair colour"), ("/", "Home")], domain="geet.example")


def _draft(words: int = 1000, **overrides) -> dict:
    para = " ".join(["word"] * (words // 4))
    draft = {
        "title": "Hair colour aftercare that works",
        "slug": "Hair Colour Aftercare!",
        "meta_title": "Hair colour aftercare: 7 rules",
        "meta_description": "Keep salon colour bright for longer with these aftercare steps — from the first wash.",
        "target_keyword": "hair colour aftercare",
        "body_html": (
            f"<h2>First wash</h2><p>{para} <a href=\"https://geet.example/services/colour\">our colour service</a></p>"
            f"<h2>Products</h2><p>{para} <a href=\"https://made-up.example/study\">a study</a></p>"
            f"<h2>Heat</h2><p>{para}</p><h3>Tools</h3><p>{para}</p>"
        ),
        "faq": [{"q": "When can I wash?", "a": "After 72 hours."}, {"q": "Which shampoo?", "a": "Sulphate free."}],
        "internal_links": [
            {"url": "https://geet.example/services/colour", "anchor": "our colour service"},
            {"url": "https://geet.example/invented", "anchor": "nope"},
        ],
    }
    draft.update(overrides)
    return draft


def test_a_good_draft_is_cleaned_and_passes():
    article = writer.clean_article(_draft(), topic={"keyword": "hair colour aftercare"}, links=_links(), language="English")
    assert writer.problems_with(article, min_words=1000) == []
    assert article["slug"] == "hair-colour-aftercare"
    assert "—" not in article["meta_description"]  # house style punctuation
    assert article["internal_links"] == [
        {"url": "https://geet.example/services/colour", "anchor": "our colour service"}
    ]
    assert "made-up.example" not in article["body_html"] and "a study" in article["body_html"]
    assert article["outline"] == ["First wash", "Products", "Heat"]
    assert article["word_count"] > article["body_word_count"] >= 800


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"meta_title": "x" * 61}, "meta_title is 61 characters"),
        ({"meta_description": "too short"}, "meta_description is 9 characters"),
        ({"faq": []}, "FAQ needs at least"),
        ({"body_html": "<h2>Only</h2><p>tiny</p>"}, "at least 2 <h2> sections"),
        ({"body_html": "<h2>A</h2><p>text</p><h2>Empty</h2>"}, "no content under them: Empty"),
        ({"title": ""}, None),
    ],
)
def test_bad_drafts_are_rejected_with_a_reason(overrides: dict, expected: str | None):
    article = writer.clean_article(_draft(**overrides), topic={"keyword": "k", "title": ""}, links=_links(), language="English")
    problems = writer.problems_with(article, min_words=1000)
    assert problems
    if expected:
        assert any(expected in p for p in problems), problems


def test_a_short_body_is_rejected():
    article = writer.clean_article(_draft(words=300), topic={"keyword": "k"}, links=_links(), language="English")
    assert any("words" in p for p in writer.problems_with(article, min_words=1000))


def test_site_links_only_allow_real_pages_on_the_site():
    links = _links()
    assert links.allows("https://geet.example/services/colour/")
    assert links.allows("/services/colour")
    assert not links.allows("https://geet.example/nowhere")
    assert not links.allows("https://evil.example/services/colour")
    assert links.candidates({"keyword": "hair colour", "title": "", "secondary": []})[0] == (
        "https://geet.example/services/colour",
        "Hair colour",
    )


# ── Batch size and dates ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("remaining", "per_week", "expected"),
    [(0, 2, 1), (10, 2, 2), (10, 14, 5), (3, 14, 3), (None, 4, 1)],
)
def test_batch_size_rule(remaining, per_week, expected):  # noqa: ANN001
    assert writer.batch_size(SimpleNamespace(remaining_actions=remaining), per_week) == expected


def test_dates_are_spread_across_the_week():
    assert writer.spread_dates(TODAY, 4, 2) == [TODAY + timedelta(days=d) for d in (0, 4, 7, 11)]
    assert writer.spread_dates(TODAY, 3, 7) == [TODAY + timedelta(days=d) for d in (0, 1, 2)]


def test_topics_that_target_the_same_search_are_too_close():
    assert writer.too_close("hair colour aftercare", "Aftercare for hair colour")
    assert not writer.too_close("hair colour aftercare", "bridal makeup trial")


# ── Running the agent ────────────────────────────────────────────────────────
class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _Db:
    def __init__(self, pages: list | None = None, articles: list | None = None) -> None:
        self.pages = pages or []
        self.articles = articles or []
        self.added: list = []

    def execute(self, statement):  # noqa: ANN001
        text = str(statement)
        if "seo_pages" in text:
            return _Rows(self.pages)
        if "marketing_assets" in text:
            return _Rows(self.articles)
        raise AssertionError(text)

    def add(self, row) -> None:  # noqa: ANN001
        row.id = row.id or f"asset{len(self.added) + 1}"
        self.added.append(row)

    def flush(self) -> None:
        pass


class _Ctx(SimpleNamespace):
    def setting(self, key, default=None):  # noqa: ANN001
        return self.settings.get(key, default)

    def set_setting(self, key, value) -> None:  # noqa: ANN001
        self.settings[key] = value

    def with_capability(self, capability):  # noqa: ANN001
        found = self.all_with_capability(capability)
        return found[0] if found else None

    def all_with_capability(self, capability):  # noqa: ANN001
        return [c for c in self.connectors if c.supports(capability)]

    def candidates(self, capability):  # noqa: ANN001
        return self.all_with_capability(capability)

    def connector_name(self, slug):  # noqa: ANN001
        return {"wordpress": "WordPress", "webflow": "Webflow"}.get(slug, slug)

    def ask_json(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Cap:
    def __init__(self, slug: str, *caps: Capability, **methods) -> None:  # noqa: ANN003
        self.slug = slug
        self.caps = set(caps)
        for name, fn in methods.items():
            setattr(self, name, fn)

    def supports(self, capability: Capability) -> bool:
        return capability in self.caps


def _ctx(*, answers: list, connectors: list | None = None, settings: dict | None = None, brief: str = "BUSINESS: Geet",
         pages: list | None = None, articles: list | None = None, scope: str = "", remaining: int = 10) -> _Ctx:
    return _Ctx(
        db=_Db(pages=pages if pages is not None else [("/services/colour", "Hair colour"), ("/bridal", "Bridal makeup")],
               articles=articles),
        org=SimpleNamespace(id="t", name="Geet unisex salon", primary_domain="geet.example"),
        tenant_id="t",
        cipher=_PlainCipher(),
        run_id="run1",
        today=TODAY,
        scope=scope,
        industry="hair salon",
        brand_brief=brief,
        brand_profile={"keywords": ["salon near me"], "language": "English"},
        remaining_actions=remaining,
        settings=dict(settings or {}),
        connectors=connectors or [],
        answers=list(answers),
        prompts=[],
        progress=lambda *a, **k: None,
        log_warning=lambda *a, **k: None,
    )


def _plan(*topics: tuple[str, str]) -> dict:
    return {"topics": [{"target_keyword": k, "title": t, "angle": "a", "secondary_keywords": ["s"]} for k, t in topics]}


def test_a_run_plans_the_calendar_and_writes_what_is_due(monkeypatch):
    search = _Cap(
        "search_console",
        Capability.READ_SEARCH_PERFORMANCE,
        read_search_performance=lambda days: {"top_queries": ["keratin treatment cost", "hair colour", "salon near me"]},
    )
    cms = _Cap("wordpress", Capability.CREATE_PAGE)
    plan = _plan(
        ("keratin treatment cost", "What keratin costs"),
        ("hair colour services", "Our colour"),  # competes with the existing /services/colour page
        ("hair colour aftercare", "Colour aftercare"),
        ("balayage vs highlights", "Balayage or highlights"),
        ("Aftercare for hair colour", "Same again"),  # a duplicate of an earlier suggestion
    )
    ctx = _ctx(answers=[plan, _draft(), _draft()], connectors=[search, cms], settings={"articles_per_week": "2"})
    result = AGENT.run(ctx)

    # Proven demand reaches the planner; covered queries do not.
    assert "keratin treatment cost" in ctx.prompts[0]
    assert "- hair colour\n" not in ctx.prompts[0]
    assert "Hair colour (/services/colour)" in ctx.prompts[0]

    calendar = ctx.settings[writer.CALENDAR_KEY]
    assert [e["keyword"] for e in calendar] == ["keratin treatment cost", "hair colour aftercare", "balayage vs highlights"]
    assert [e["date"] for e in calendar] == ["2026-10-06", "2026-10-10", "2026-10-13"]
    assert calendar[0]["source"] == "search"
    # Two are due within a week and two a week is the rate: two written.
    assert [e["status"] for e in calendar] == ["written", "written", "planned"]
    assert len(result.actions) == 2
    asset = ctx.db.added[0]
    assert asset.kind == AssetKind.ARTICLE and asset.channel == "wordpress" and asset.run_id == "run1"
    assert asset.scheduled_for.date() == date(2026, 10, 6)
    assert "export_only" not in asset.detail and asset.detail["target_keyword"] == "hair colour aftercare"
    action = result.actions[0]
    assert action.impact.value == "high" and action.target_kind == assets.TARGET_KIND
    assert action.payload["asset_id"] == asset.id and action.payload["channel"] == "wordpress"
    assert action.metrics == {}
    assert "saved as drafts in WordPress" in result.summary
    assert "brand profile" not in result.summary
    content = json.loads(asset.content_encrypted)
    assert content["internal_links"][0]["url"] == "https://geet.example/services/colour"


def test_without_a_cms_articles_are_export_only_and_no_profile_is_mentioned():
    ctx = _ctx(answers=[_plan(("bridal hair trial", "Bridal trial")), _draft()], brief="", settings={"articles_per_week": "1"})
    result = AGENT.run(ctx)
    assert ctx.db.added[0].channel == "export" and ctx.db.added[0].detail["export_only"] is True
    assert "ready to download" in result.summary
    assert "Add a brand profile" in result.summary
    assert "BUSINESS: Geet unisex salon" in ctx.prompts[0] and "INDUSTRY: hair salon" in ctx.prompts[0]


def test_topics_already_written_are_not_planned_again():
    articles = [("Colour aftercare guide", {"target_keyword": "hair colour aftercare"})]
    ctx = _ctx(answers=[_plan(("hair colour aftercare", "Again")), ], articles=articles, settings={"articles_per_week": "1"})
    AGENT.run(ctx)
    assert ctx.settings[writer.CALENDAR_KEY] == []
    assert "hair colour aftercare" in ctx.prompts[0]  # it was told, and checked anyway


def test_the_calendar_carries_over_and_is_not_replanned_when_full():
    calendar = [
        {"keyword": f"topic {i}", "title": "", "date": (TODAY + timedelta(days=10 + i)).isoformat(), "status": "planned"}
        for i in range(4)
    ]
    ctx = _ctx(answers=[], settings={writer.CALENDAR_KEY: calendar, "articles_per_week": "2"})
    result = AGENT.run(ctx)
    assert ctx.prompts == []  # nothing due this week, nothing to plan
    assert result.actions == [] and "next on 2026-10-15" in result.summary


def test_a_rejected_draft_is_retried_once_then_the_topic_waits_for_another_run():
    bad = _draft(meta_title="x" * 90)
    ctx = _ctx(answers=[_plan(("bridal hair trial", "Bridal trial")), bad, bad], settings={"articles_per_week": "1"})
    result = AGENT.run(ctx)
    assert result.actions == []
    assert "meta_title is 90 characters" in ctx.prompts[-1]  # the retry was told why
    entry = ctx.settings[writer.CALENDAR_KEY][0]
    assert entry["status"] == "planned" and entry["failures"] == 1

    again = _ctx(answers=[_plan(), bad, bad], settings=ctx.settings)
    AGENT.run(again)
    assert again.settings[writer.CALENDAR_KEY][0]["status"] == "skipped"


def test_the_retry_can_rescue_a_draft():
    ctx = _ctx(answers=[_plan(("bridal hair trial", "Bridal trial")), _draft(faq=[]), _draft()], settings={"articles_per_week": "1"})
    assert len(AGENT.run(ctx).actions) == 1


def test_a_failed_header_image_does_not_fail_the_article(monkeypatch):
    from app.services import images

    made: list = []

    def generate(db, org, prompt, *, shape, provider=""):  # noqa: ANN001
        made.append(shape)
        if len(made) == 1:
            raise ConnectorError("quota")
        return SimpleNamespace(id="img1")

    monkeypatch.setattr(images, "generate", generate)
    gen = _Cap(
        "openai",
        Capability.GENERATE_IMAGE,
        name="OpenAI",
        credentials=SimpleNamespace(get=lambda key, default="": {"imageModel": "img"}.get(key, default)),
    )
    ctx = _ctx(
        answers=[_plan(("a b c", "One"), ("d e f", "Two")), _draft(), _draft()],
        connectors=[gen],
        settings={"articles_per_week": "2"},
        remaining=10,
    )
    ctx.today = TODAY
    result = AGENT.run(ctx)
    assert len(result.actions) == 2 and made == ["landscape", "landscape"]
    assert ctx.db.added[0].image_ids == [] and ctx.db.added[1].image_ids == ["img1"]
    assert "1 header image could not be created" in result.summary


def test_a_cms_that_is_not_set_up_to_create_pages_is_passed_over():
    webflow = _Cap("webflow", Capability.CREATE_PAGE, create_page_problem=lambda: "Webflow has no Collection ID")
    ctx = _ctx(answers=[_plan(("bridal hair trial", "Bridal trial")), _draft()], connectors=[webflow],
               settings={"articles_per_week": "1"})
    result = AGENT.run(ctx)
    assert ctx.db.added[0].channel == "export"
    assert "Webflow has no Collection ID" in result.summary


def test_nothing_to_write_from_is_a_skip():
    ctx = _ctx(answers=[], brief="", pages=[])
    ctx.industry = ""
    result = AGENT.run(ctx)
    assert result.skipped and "brand profile" in result.summary


def test_spec_guide_and_results_follow_the_contract():
    assert AGENT.spec.requires_llm and AGENT.spec.max_impact.value == "high"
    assert AGENT.spec.category.value == "Content & Social"
    assert {o.capability for o in GUIDE.optional} >= {Capability.CREATE_PAGE, Capability.GENERATE_IMAGE}
    keys = {s.key: s for s in GUIDE.settings}
    assert keys["articles_per_week"].clean("14") == "14"
    with pytest.raises(ValueError):
        keys["articles_per_week"].clean("15")
    with pytest.raises(ValueError):
        keys["min_words"].clean("500")
    assert callable(RESULTS)
    assert AGENT.validate_scope(", ".join(f"t{i}" for i in range(13)))


# ── Publisher ────────────────────────────────────────────────────────────────
def _bundle(monkeypatch, instances: dict) -> None:  # noqa: ANN001
    from app.services import connectors as connector_service

    monkeypatch.setattr(
        connector_service,
        "build_connector_bundle",
        lambda db, org: SimpleNamespace(instances=instances, names={"wordpress": "WordPress"}),
    )


def test_the_publisher_saves_a_sanitized_draft_and_records_where(monkeypatch):
    calls: list[dict] = []

    class FakeCms:
        def supports(self, capability):  # noqa: ANN001
            return capability is Capability.CREATE_PAGE

        def create_page(self, **kwargs):  # noqa: ANN003
            calls.append(kwargs)
            return "42", "https://geet.example/?p=42"

    _bundle(monkeypatch, {"wordpress": FakeCms()})
    asset = _asset()
    assets.publish(SimpleNamespace(flush=lambda: None), SimpleNamespace(id="t"), asset, _PlainCipher())
    assert asset.status == AssetStatus.PUBLISHED
    assert (asset.remote_id, asset.remote_url) == ("42", "https://geet.example/?p=42")
    sent = calls[0]
    assert sent["status"] == "draft" and sent["slug"] == "hair-colour-aftercare"
    assert "<script>" not in sent["body_html"] and "<h3>When can I wash?</h3>" in sent["body_html"]
    assert "body_markdown" not in sent


def test_the_publisher_explains_a_missing_connection(monkeypatch):
    _bundle(monkeypatch, {})
    with pytest.raises(ConnectorError, match="WordPress is not connected"):
        writer.publish_article(None, SimpleNamespace(id="t"), _asset(), _content())


def test_repositories_are_sent_markdown(monkeypatch):
    calls: list[dict] = []
    github = GitHubConnector(Credentials(values={"repository": "acme/site", "token": "t", "branch": "main"}))
    monkeypatch.setattr(github, "create_page", lambda **kw: calls.append(kw) or ("content/a.md", "https://pr"))
    _bundle(monkeypatch, {"github": github})
    asset = _asset(channel="github")
    writer.publish_article(None, SimpleNamespace(id="t"), asset, _content())
    assert calls[0]["body_markdown"].startswith("## Wash less")
    assert asset.remote_url == "https://pr"


# ── create_page on each CMS ──────────────────────────────────────────────────
def _mock(connector, handler):  # noqa: ANN001, ANN202
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


def test_wordpress_creates_a_draft_post():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 77, "link": "https://geet.example/?p=77"})

    wp = _mock(WordPressConnector(Credentials(values={
        "siteUrl": "https://geet.example", "username": "u", "applicationPassword": "p"})), handler)
    assert wp.create_page(title="T", body_html="<p>B</p>", slug="t", meta_description="D") == ("77", "https://geet.example/?p=77")
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/wp-json/wp/v2/posts"
    assert json.loads(request.content) == {"title": "T", "content": "<p>B</p>", "slug": "t", "excerpt": "D", "status": "draft"}
    assert request.headers["Authorization"].startswith("Basic ")
    with pytest.raises(ValueError):
        wp.create_page(title="T", body_html="b", slug="t", status="publish")
    assert Capability.CREATE_PAGE in WordPressConnector.spec.capabilities


def test_shopify_creates_an_unpublished_blog_article():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/blogs.json"):
            return httpx.Response(200, json={"blogs": [{"id": 555, "title": "News"}]})
        if request.url.path.endswith("/shop.json"):
            return httpx.Response(200, json={"shop": {"name": "Geet"}})
        return httpx.Response(201, json={"article": {"id": 9001, "blog_id": 555}})

    shop = _mock(ShopifyConnector(Credentials(values={"storeDomain": "geet.myshopify.com", "adminToken": "shpat"})), handler)
    remote_id, url = shop.create_page(title="T", body_html="<p>B</p>", slug="t", meta_description="D & more")
    assert remote_id == "article:9001"
    assert url == "https://geet.myshopify.com/admin/articles/9001"
    post = seen[-1]
    assert post.method == "POST" and post.url.path == "/admin/api/2026-10/blogs/555/articles.json"
    assert json.loads(post.content) == {"article": {
        "title": "T", "body_html": "<p>B</p>", "handle": "t", "published": False, "author": "Geet",
        "summary_html": "<p>D &amp; more</p>"}}
    assert post.headers["X-Shopify-Access-Token"] == "shpat"


def test_shopify_uses_the_configured_blog_and_explains_a_store_without_one():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/blogs.json"):
            return httpx.Response(200, json={"blogs": []})
        return httpx.Response(201, json={"article": {"id": 1}})

    configured = _mock(ShopifyConnector(Credentials(values={
        "storeDomain": "geet.myshopify.com", "adminToken": "x", "blogId": "321"})), handler)
    configured.create_page(title="T", body_html="b", slug="t")
    assert "/admin/api/2026-10/blogs/321/articles.json" in paths and not any(p.endswith("/blogs.json") for p in paths)

    bare = _mock(ShopifyConnector(Credentials(values={"storeDomain": "geet.myshopify.com", "adminToken": "x"})), handler)
    with pytest.raises(ConnectorConfigError, match="no blog"):
        bare.create_page(title="T", body_html="b", slug="t")


def test_webflow_creates_a_draft_item_in_the_configured_collection():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"id": "item9", "isDraft": True})

    values = {"siteId": "s", "siteToken": "tok", "articlesCollectionId": "col1", "articlesBodyField": "content"}
    webflow = _mock(WebflowConnector(Credentials(values=values)), handler)
    assert webflow.create_page_problem() is None
    assert webflow.create_page(title="T", body_html="<p>B</p>", slug="t") == ("col1:item9", "")
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/v2/collections/col1/items"
    assert json.loads(request.content) == {
        "isArchived": False, "isDraft": True, "fieldData": {"name": "T", "slug": "t", "content": "<p>B</p>"}}
    assert request.headers["Authorization"] == "Bearer tok"
    assert len(seen) == 1  # never published


def test_webflow_defaults_the_body_field_and_needs_a_collection():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "i"})

    webflow = _mock(WebflowConnector(Credentials(values={"siteId": "s", "siteToken": "t", "articlesCollectionId": "c"})), handler)
    webflow.create_page(title="T", body_html="b", slug="t")
    assert "post-body" in bodies[0]["fieldData"]

    unset = _mock(WebflowConnector(Credentials(values={"siteId": "s", "siteToken": "t"})), handler)
    assert unset.create_page_problem()
    with pytest.raises(ConnectorConfigError):
        unset.create_page(title="T", body_html="b", slug="t")


def _git_handler(calls: list, *, exists: bool = False):  # noqa: ANN202
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and "/contents/" in path:
            if exists:
                return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"x").decode(), "sha": "s"})
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and "/src/main/" in path:
            return httpx.Response(404, text="not found")
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "head"}})
        if path.endswith("/refs/branches/main"):
            return httpx.Response(200, json={"target": {"hash": "head"}})
        if path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/acme/site/pull/8"})
        if path.endswith("/pullrequests"):
            return httpx.Response(201, json={"links": {"html": {"href": "https://bitbucket.org/acme/site/pull-requests/4"}}})
        return httpx.Response(201, json={})

    return handler


def test_github_opens_a_pull_request_adding_a_markdown_article():
    calls: list[httpx.Request] = []
    values = {"repository": "acme/site", "token": "t", "branch": "main", "contentPath": "content", "articlesPath": "content/blog"}
    github = _mock(GitHubConnector(Credentials(values=values)), _git_handler(calls))
    path, url = github.create_page(
        title="Colour: the rules", body_html="<p>x</p>", slug="colour-rules", meta_description="Desc", body_markdown="Body text"
    )
    assert path == "content/blog/colour-rules.md" and url == "https://github.com/acme/site/pull/8"
    put = next(c for c in calls if c.method == "PUT")
    body = json.loads(put.content)
    assert put.url.path == "/repos/acme/site/contents/content/blog/colour-rules.md"
    assert "sha" not in body and body["branch"].startswith("synaprank/") and body["branch"] != "main"
    text = base64.b64decode(body["content"]).decode()
    assert text.startswith('---\ntitle: "Colour: the rules"\ndescription: "Desc"\ndate: ')
    assert text.endswith("---\n\nBody text\n")
    assert any(c.url.path == "/repos/acme/site/pulls" for c in calls)


def test_github_never_overwrites_an_existing_file():
    calls: list[httpx.Request] = []
    values = {"repository": "acme/site", "token": "t", "branch": "main"}
    github = _mock(GitHubConnector(Credentials(values=values)), _git_handler(calls, exists=True))
    with pytest.raises(ConnectorError, match="already exists"):
        github.create_page(title="T", body_html="<p>x</p>", slug="t")
    assert not [c for c in calls if c.method in ("PUT", "POST")]


def test_bitbucket_adds_a_dated_jekyll_post_by_pull_request():
    calls: list[httpx.Request] = []
    values = {"repository": "acme/site", "token": "t", "branch": "main", "articlesPath": "_posts"}
    bitbucket = _mock(BitbucketConnector(Credentials(values=values)), _git_handler(calls))
    path, url = bitbucket.create_page(title="T", body_html="<p>x</p>", slug="t")
    assert path.startswith("_posts/") and path.endswith("-t.md") and len(path) == len("_posts/2026-01-01-t.md")
    assert url == "https://bitbucket.org/acme/site/pull-requests/4"
    commit = next(c for c in calls if c.method == "POST" and c.url.path.endswith("/src"))
    assert path.encode() in commit.content or path.replace("/", "%2F").encode() in commit.content
    assert Capability.CREATE_PAGE in BitbucketConnector.spec.capabilities
    assert Capability.CREATE_PAGE in GitHubConnector.spec.capabilities


def test_a_failing_search_source_does_not_stop_planning():
    def broken(days):  # noqa: ANN001
        raise ConnectorError("Search Console is down")

    search = _Cap("search_console", Capability.READ_SEARCH_PERFORMANCE, read_search_performance=broken)
    ctx = _ctx(answers=[_plan(("bridal hair trial", "Bridal trial")), _draft()], connectors=[search],
               settings={"articles_per_week": "1"})
    assert len(AGENT.run(ctx).actions) == 1


def test_overdue_topics_move_to_the_coming_days():
    calendar = [
        {"keyword": "late topic", "date": (TODAY - timedelta(days=3)).isoformat(), "status": "planned"},
        {"keyword": "later topic", "date": (TODAY + timedelta(days=12)).isoformat(), "status": "planned"},
    ]
    ctx = _ctx(answers=[_draft()], settings={writer.CALENDAR_KEY: calendar, "articles_per_week": "1"})
    result = AGENT.run(ctx)
    saved = {e["keyword"]: e for e in ctx.settings[writer.CALENDAR_KEY]}
    assert saved["late topic"]["date"] == "2026-10-06" and saved["late topic"]["status"] == "written"
    assert len(result.actions) == 1


def test_an_image_connection_without_an_image_model_still_writes_and_says_why(monkeypatch):  # noqa: ANN001
    from app.services import images

    monkeypatch.setattr(images, "generate", lambda *a, **k: pytest.fail("no image model, so no attempt"))
    gen = _Cap(
        "openai",
        Capability.GENERATE_IMAGE,
        name="OpenAI",
        credentials=SimpleNamespace(get=lambda key, default="": default),
    )
    ctx = _ctx(
        answers=[_plan(("a b c", "One")), _draft()],
        connectors=[gen],
        settings={"articles_per_week": "1"},
        remaining=10,
    )
    ctx.today = TODAY
    result = AGENT.run(ctx)
    assert len(result.actions) == 1
    assert "Choose an image model in your OpenAI connection to create images." in result.summary
