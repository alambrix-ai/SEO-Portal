"""Reading the live site, and refusing to rewrite source.

The case behind all of this: a Next.js repository where ``src/app/page.jsx``
is a composition — ``<Header /><Hero /><Outcomes /><SiteFooter />`` — with no
copy in it at all. The words live in a dozen components and no single file is
the page, so reading source files gives you the length of an import block and
rewriting one would replace working code with prose.
"""
from __future__ import annotations

import httpx
import pytest

from app.connectors.base import content_files as cf
from app.connectors.base import html_pages as hp
from app.connectors.base.credentials import Credentials
from app.connectors.site_crawler import SiteCrawlerConnector
from app.core.exceptions import ConnectorError

HOME = """<!doctype html>
<html><head>
  <title>Alambrix — AI operations for dealer groups</title>
  <meta name="description" content="We run the pipeline end to end.">
  <link rel="canonical" href="https://alambrix.ai/">
  <script type="application/ld+json">{"@type":"Organization","name":"Alambrix"}</script>
</head><body>
  <h1>AI operations for dealer groups</h1>
  <p>We connect your systems and run the work continuously, with a person in
     the loop wherever it matters. Every action is recorded.</p>
  <a href="/blog">Blog</a><a href="https://twitter.com/x">Twitter</a>
  <img src="hero.png"><img src="logo.png" alt="Alambrix">
  <script src="/app.js"></script>
</body></html>"""

SPA_SHELL = """<!doctype html>
<html><head><title>App</title></head>
<body><div id="root"></div><script src="/bundle.js"></script></body></html>"""


# ── Parsing what a crawler receives ────────────────────────────────────────
def test_a_rendered_page_yields_what_the_pipeline_needs():
    page = hp.parse("https://alambrix.ai/", HOME)

    assert page.title == "Alambrix — AI operations for dealer groups"
    assert page.description == "We run the pipeline end to end."
    assert page.canonical == "https://alambrix.ai/"
    assert page.headings == ["AI operations for dealer groups"]
    assert page.schema_types == ["Organization"]
    assert page.images_without_alt == 1
    # The copy, with markup and scripts gone.
    assert "person in the loop" in page.text
    assert "app.js" not in page.text
    assert page.word_count > 20


def test_script_and_style_content_is_not_counted_as_copy():
    """Otherwise a bundle inflates the word count and hides thin content."""
    noisy = "<body><style>.a{color:red}</style><script>var x=1</script><p>Two words</p></body>"
    assert hp.visible_text(noisy) == "Two words"


def test_a_client_rendered_shell_is_reported_as_exactly_that():
    """The finding nothing else in the platform can make.

    An empty shell is invisible to crawlers that do not run JavaScript. That
    is a completely different problem from thin content, with a completely
    different fix, so it must not be reported as thin content.
    """
    page = hp.parse("https://example.com/", SPA_SHELL)
    assert page.client_side_rendered is True
    assert hp.parse("https://alambrix.ai/", HOME).client_side_rendered is False


def test_noindex_is_noticed():
    html = '<head><meta name="robots" content="noindex, follow"></head><body>x</body>'
    assert hp.parse("https://e.com/", html).robots_noindex is True


# ── Discovery ──────────────────────────────────────────────────────────────
def test_a_sitemap_index_is_told_apart_from_a_sitemap():
    """Treating an index's entries as pages is how a crawl audits nothing."""
    index = """<sitemapindex><sitemap><loc>https://e.com/a.xml</loc></sitemap></sitemapindex>"""
    urlset = """<urlset><url><loc>https://e.com/one</loc></url></urlset>"""

    locations, is_index = hp.sitemap_urls(index)
    assert is_index is True
    assert locations == ["https://e.com/a.xml"]

    locations, is_index = hp.sitemap_urls(urlset)
    assert is_index is False
    assert locations == ["https://e.com/one"]


@pytest.mark.parametrize(
    ("candidate", "same"),
    [
        ("https://alambrix.ai/blog", True),
        ("https://www.alambrix.ai/blog", True),
        ("/relative", True),
        ("https://other.com/x", False),
    ],
)
def test_links_are_kept_on_site_ignoring_www(candidate: str, same: bool):
    assert hp.same_site(candidate, "https://alambrix.ai") is same


def test_robots_disallow_rules_are_read_and_respected():
    """A crawler that ignores robots.txt on its own client's site is one
    nobody should trust with anything else."""
    robots = """
    User-agent: Googlebot
    Disallow: /google-only

    User-agent: *
    Disallow: /admin
    Disallow: /cart   # checkout
    """
    rules = hp.disallowed_paths(robots)
    assert rules == ["/admin", "/cart"]
    assert hp.is_allowed("/blog", rules) is True
    assert hp.is_allowed("/admin/users", rules) is False
    # A rule for another agent does not apply to us.
    assert hp.is_allowed("/google-only", rules) is True


# ── The connector ──────────────────────────────────────────────────────────
def _crawler(handler, **overrides):  # noqa: ANN001, ANN003
    values = {
        "siteUrl": "https://alambrix.ai",
        "discovery": "sitemap",
        **overrides,
    }
    connector = SiteCrawlerConnector(Credentials(values=values), org_id="o")
    connector._client = httpx.Client(
        base_url="https://alambrix.ai", transport=httpx.MockTransport(handler)
    )
    return connector


def test_pages_come_from_the_sitemap_and_read_as_rendered():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text="<urlset>"
                "<url><loc>https://alambrix.ai/</loc></url>"
                "<url><loc>https://alambrix.ai/blog</loc></url>"
                "</urlset>",
            )
        if path == "/robots.txt":
            return httpx.Response(404, text="")
        return httpx.Response(200, text=HOME)

    crawler = _crawler(handler)
    pages = crawler.list_pages()
    assert [p.url for p in pages] == ["/", "/blog"]

    page = crawler.read_page("https://alambrix.ai/")
    assert page.title == "Alambrix — AI operations for dealer groups"
    assert "person in the loop" in page.body
    assert page.metadata["source"] == "rendered"
    # An agent must know before it proposes a rewrite it could never apply.
    assert page.metadata["writable"] is False


def test_a_disallowed_path_is_not_listed():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text="<urlset>"
                "<url><loc>https://alambrix.ai/</loc></url>"
                "<url><loc>https://alambrix.ai/admin/secret</loc></url>"
                "</urlset>",
            )
        if path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\nDisallow: /admin"
            )
        return httpx.Response(200, text=HOME)

    assert [p.url for p in _crawler(handler).list_pages()] == ["/"]


def test_a_missing_sitemap_says_what_to_do_instead():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(ConnectorError) as caught:
        _crawler(handler).list_pages()
    assert "crawl" in str(caught.value)


def test_crawl_discovery_follows_internal_links_only():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="")
        return httpx.Response(200, text=HOME)

    pages = _crawler(handler, discovery="crawl").list_pages()
    urls = [p.url for p in pages]
    assert "/" in urls
    assert "/blog" in urls
    # The external link in HOME was not followed.
    assert not any("twitter" in path for path in seen)


def test_health_reports_a_client_rendered_site_as_a_finding_not_a_failure():
    """It is reachable. The problem is with the site, and it is severe."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SPA_SHELL)

    report = _crawler(handler).check_health()
    assert report.ok is True
    assert "assembled in the browser" in report.detail
    assert report.metadata["client_side_rendered"] is True


def test_the_crawler_refuses_to_write():
    """There is no way to PUT a change back to a rendered page."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HOME)

    with pytest.raises(ConnectorError) as caught:
        _crawler(handler).write_page("https://alambrix.ai/", body="new copy")
    assert "cannot write" in str(caught.value)
    assert "repository" in str(caught.value)


# ── Source files are never rewritten ───────────────────────────────────────
@pytest.mark.parametrize(
    ("path", "rewritable"),
    [
        ("content/post.md", True),
        ("about.html", True),
        ("content/post.rst", True),
        ("src/app/page.jsx", False),
        ("src/pages/index.tsx", False),
        ("src/views/Home.vue", False),
        ("resources/views/home.blade.php", False),
    ],
)
def test_only_prose_files_are_rewritable(path: str, rewritable: bool):
    assert cf.is_rewritable(path) is rewritable


def test_a_repository_connector_refuses_to_rewrite_a_component():
    """The write that would have replaced working code with marketing prose."""
    from tests.test_git_connectors import PAGE, RecordingGitHub

    github = RecordingGitHub(PAGE)
    with pytest.raises(ConnectorError) as caught:
        github.write_page("src/app/page.jsx", body="Rewritten copy", title="New")
    assert "source, not prose" in str(caught.value)
    assert "Site Crawler" in str(caught.value)
    # Nothing was committed and no branch was made.
    assert not [call for call in github.calls if call[0] in ("POST", "PUT")]


def test_a_repository_connector_refuses_to_inject_schema_into_a_component():
    from tests.test_git_connectors import PAGE, RecordingGitHub

    github = RecordingGitHub(PAGE)
    with pytest.raises(ConnectorError, match="would not compile"):
        github.inject_schema("src/app/page.jsx", json_ld={"@type": "Organization"})
