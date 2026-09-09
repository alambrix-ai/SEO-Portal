"""Repositories as content systems.

The content-file logic is where the damage would be done — a rewrite that
drops a page's frontmatter breaks the customer's build, and a bad path-to-URL
mapping makes the whole SEO pipeline report on URLs that do not exist. Neither
needs a network to test, so both are tested hard.

The pull-request workflow is tested against a recorded transport rather than
a live repository: what matters is that a write produces a branch, a commit
and a pull request, and never a commit on the deploy branch.
"""
from __future__ import annotations

import base64
import json

import pytest

from app.connectors.base import content_files as cf
from app.connectors.base.credentials import Credentials
from app.connectors.bitbucket import BitbucketConnector
from app.connectors.github import GitHubConnector

# ── Frontmatter ────────────────────────────────────────────────────────────
PAGE = """---
title: "EV charging: what to know"
layout: post
date: 2026-01-02
draft: false
---

# EV charging

Charging at home is cheaper than a public rapid.
"""


def test_a_file_round_trips_unchanged():
    """The safest possible property: parsing and rendering is identity.

    If this fails, every write silently reformats somebody's repository.
    """
    assert cf.parse("content/a.md", PAGE).render() == PAGE


def test_rewriting_the_body_keeps_every_frontmatter_key():
    """The keys carry layout, dates, redirects and feature flags.

    Rewriting the whole file with generated prose would drop all of it and the
    page would build wrong, or not at all.
    """
    parsed = cf.parse("content/a.md", PAGE)
    parsed.body = "\n# EV charging\n\nCompletely new copy.\n"
    rendered = parsed.render()

    assert "layout: post" in rendered
    assert "date: 2026-01-02" in rendered
    assert "draft: false" in rendered
    assert "Completely new copy." in rendered
    assert "cheaper than a public rapid" not in rendered


def test_a_title_containing_a_colon_is_quoted():
    """Unquoted, it is invalid YAML — and a broken build on a live site.

    Titles with colons are not an edge case; they are how half of all
    headlines are written.
    """
    parsed = cf.parse("content/a.md", PAGE)
    cf.set_frontmatter_value(parsed, "title", "Leasing: the 2026 rules")
    assert 'title: "Leasing: the 2026 rules"' in parsed.render()
    assert cf.frontmatter_value(cf.parse("a.md", parsed.render()), "title") == (
        "Leasing: the 2026 rules"
    )


def test_a_missing_key_is_appended_rather_than_replacing_the_block():
    parsed = cf.parse("content/a.md", PAGE)
    cf.set_frontmatter_value(parsed, "description", "A new meta description")
    rendered = parsed.render()
    assert 'description: "A new meta description"' in rendered
    assert "layout: post" in rendered


def test_a_file_with_no_frontmatter_gains_a_block_only_when_needed():
    plain = "# Just markdown\n\nNo block here.\n"
    parsed = cf.parse("content/a.md", plain)
    assert parsed.render() == plain

    cf.set_frontmatter_value(parsed, "title", "Now titled")
    assert parsed.render().startswith('---\ntitle: "Now titled"\n---\n')


def test_toml_frontmatter_is_preserved_with_its_own_fence():
    """Hugo uses +++. Rewriting it as --- would break the parser it feeds."""
    hugo = '+++\ntitle = "Hugo page"\ndraft = false\n+++\n\nBody.\n'
    parsed = cf.parse("content/a.md", hugo)
    assert parsed.fence == "+++"
    assert parsed.render() == hugo


@pytest.mark.parametrize(
    ("path", "root", "prefix", "url"),
    [
        ("content/blog/ev-charging.md", "content", "", "/blog/ev-charging"),
        ("content/about/index.md", "content", "", "/about"),
        ("content/_index.md", "content", "", "/"),
        ("src/pages/index.astro", "src/pages", "", "/"),
        ("src/pages/lease/deals.mdx", "src/pages", "", "/lease/deals"),
        ("blog/post.html", "", "", "/blog/post"),
        ("content/lease.md", "content", "en", "/en/lease"),
        ("_posts/2026-01-02-hello.md", "_posts", "", "/2026-01-02-hello"),
    ],
)
def test_a_repository_path_maps_to_the_url_it_is_served_at(
    path: str, root: str, prefix: str, url: str
):
    """Get this wrong and the pipeline reports on pages that do not exist."""
    assert cf.path_to_url(path, content_root=root, url_prefix=prefix) == url


@pytest.mark.parametrize(
    ("path", "wanted"),
    [
        ("content/page.md", True),
        ("content/page.mdx", True),
        ("src/pages/index.astro", True),
        ("content/_index.md", True),          # Hugo's documented exception
        ("content/_draft.md", False),         # a partial or a draft
        ("src/components/Nav.tsx", False),
        ("package.json", False),
        ("node_modules/thing/readme.md", False),
        ("styles/site.css", False),
    ],
)
def test_only_content_files_count_as_pages(path: str, wanted: bool):
    """A repository is mostly not pages.

    Walking a tree without this filter turns every component and stylesheet
    into a page for an agent to rewrite.
    """
    assert cf.is_content_path(path) is wanted


def test_word_count_ignores_markup_and_code_fences():
    body = "# Title\n\nTwo words.\n\n```python\nthis is not prose at all here\n```\n"
    # Title, Two, words. The bare "#" has no alphanumerics and is not a word.
    assert cf.word_count(body) == 3
    assert cf.word_count("<p>One <em>two</em> three</p>") == 3


def test_json_ld_is_replaced_rather_than_appended():
    """Two conflicting graphs on a page is worse for a crawler than none.

    Appending on every run would also grow the file without bound.
    """
    parsed = cf.parse("a.html", "<h1>Hi</h1>\n")
    cf.inject_json_ld(parsed, {"@type": "FAQPage", "name": "first"})
    cf.inject_json_ld(parsed, {"@type": "Product", "name": "second"})

    assert parsed.body.count("application/ld+json") == 1
    assert cf.schema_types(parsed.body) == ["Product"]
    assert "second" in parsed.body


# ── The pull-request workflow ──────────────────────────────────────────────
class RecordingGitHub(GitHubConnector):
    """A GitHub connector whose calls are recorded instead of sent."""

    def __init__(self, file_text: str) -> None:
        super().__init__(
            Credentials(
                values={
                    "repository": "acme/site",
                    "token": "t",
                    "branch": "main",
                    "contentPath": "content",
                }
            ),
            org_id="org",
        )
        self.file_text = file_text
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append((method.upper(), path, kwargs))
        if path.endswith("/git/trees/main"):
            return {
                "tree": [
                    {"type": "blob", "path": "content/blog/a.md", "sha": "s1", "size": 10},
                    {"type": "blob", "path": "src/Nav.tsx", "sha": "s2", "size": 10},
                    {"type": "blob", "path": "content/style.css", "sha": "s3", "size": 10},
                ]
            }
        if "/contents/" in path and method.upper() == "GET":
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.file_text.encode()).decode(),
                "sha": "blob-sha",
            }
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "head-sha"}}
        if path.endswith("/pulls"):
            return {"html_url": "https://github.com/acme/site/pull/7"}
        return {}


def test_listing_a_repository_returns_pages_not_files():
    github = RecordingGitHub(PAGE)
    pages = github.list_pages()

    assert [p.remote_id for p in pages] == ["content/blog/a.md"]
    assert pages[0].url == "/blog/a"


def test_reading_a_page_takes_its_title_from_the_frontmatter():
    github = RecordingGitHub(PAGE)
    page = github.read_page("content/blog/a.md")

    assert page.title == "EV charging: what to know"
    assert page.url == "/blog/a"
    assert "public rapid" in page.body
    assert page.metadata["has_frontmatter"] is True
    # The frontmatter is carried so a caller can see what it must not lose.
    assert "layout: post" in page.metadata["frontmatter"]


def test_a_write_opens_a_pull_request_and_never_touches_the_deploy_branch():
    """The central guarantee of these two connectors.

    A site with its content in the repository has a deploy pipeline on the
    default branch. Committing there would publish generated prose to a live
    site with no review and no build check, on whatever schedule the agent
    runs.
    """
    github = RecordingGitHub(PAGE)
    assert github.write_page("content/blog/a.md", body="\nRewritten.\n", title="New title")

    kinds = [(m, p) for m, p, _ in github.calls]
    assert ("POST", "/repos/acme/site/git/refs") in kinds, kinds
    assert ("POST", "/repos/acme/site/pulls") in kinds, kinds

    commits = [k for k in github.calls if k[0] == "PUT" and "/contents/" in k[1]]
    assert len(commits) == 1
    body = commits[0][2]["json_body"]
    assert body["branch"].startswith("automarket/"), body["branch"]
    assert body["branch"] != "main"
    # Compare-and-set, so a concurrent edit is rejected rather than clobbered.
    assert body["sha"] == "blob-sha"

    committed = base64.b64decode(body["content"]).decode()
    assert "layout: post" in committed        # frontmatter survived
    assert 'title: "New title"' in committed  # only the title changed
    assert "Rewritten." in committed


def test_an_unchanged_page_opens_no_pull_request():
    """An agent that re-derives the same copy must not open a PR an hour.

    Nine identical pull requests is how a team learns to ignore them.
    """
    github = RecordingGitHub(PAGE)
    parsed = cf.parse("content/blog/a.md", PAGE)
    assert github.write_page(
        "content/blog/a.md", body=parsed.body, title="EV charging: what to know"
    ) is False
    assert not [c for c in github.calls if c[0] == "POST"]


def test_a_read_only_token_is_reported_as_unhealthy_immediately():
    """Otherwise it looks fine until the first rewrite fails hours later."""

    class ReadOnly(RecordingGitHub):
        def request(self, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
            if path == "/repos/acme/site":
                return {"full_name": "acme/site", "permissions": {"push": False}}
            return super().request(method, path, **kwargs)

    report = ReadOnly(PAGE).check_health()
    assert report.ok is False
    assert "not write" in report.detail


# ── Bitbucket differences that matter ──────────────────────────────────────
class RecordingBitbucket(BitbucketConnector):
    def __init__(self) -> None:
        super().__init__(
            Credentials(
                values={
                    "repository": "acme/site",
                    "token": "t",
                    "branch": "main",
                    "contentPath": "content",
                }
            ),
            org_id="org",
        )
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append((method.upper(), path, kwargs))
        # Matched exactly, because Bitbucket's directory walk asks for a
        # different path each time and a substring match would re-serve the
        # parent listing for every child.
        if kwargs.get("expect_json") is False:
            return PAGE
        if path == "/2.0/repositories/acme/site/src/main/content":
            return {
                "values": [
                    {"type": "commit_file", "path": "content/blog/a.md", "size": 10},
                    {"type": "commit_directory", "path": "content/deep"},
                ],
                "next": None,
            }
        if path == "/2.0/repositories/acme/site/src/main/content/deep":
            return {
                "values": [
                    {"type": "commit_file", "path": "content/deep/b.mdx", "size": 10}
                ],
                "next": None,
            }
        if path.endswith("/refs/branches/main"):
            return {"target": {"hash": "head-hash"}}
        if path.endswith("/pullrequests"):
            return {"links": {"html": {"href": "https://bitbucket.org/acme/site/pull/3"}}}
        return {}


def test_bitbucket_walks_directories_because_it_has_no_recursive_listing():
    bitbucket = RecordingBitbucket()
    pages = bitbucket.list_pages()

    # It followed the subdirectory rather than stopping at the first level.
    assert [p.remote_id for p in pages] == ["content/blog/a.md", "content/deep/b.mdx"]
    assert [p.url for p in pages] == ["/blog/a", "/deep/b"]
    assert any(path.endswith("/content/deep") for _, path, _ in bitbucket.calls)


def test_bitbucket_commits_as_a_form_post_keyed_on_the_file_path():
    """Its commit endpoint takes the contents as a field named for the path."""
    bitbucket = RecordingBitbucket()
    assert bitbucket.write_page("content/blog/a.md", body="\nNew copy.\n", title="T")

    commits = [c for c in bitbucket.calls if c[1].endswith("/src") and c[0] == "POST"]
    assert len(commits) == 1
    form = commits[0][2]["form"]
    assert "content/blog/a.md" in form
    assert "New copy." in form["content/blog/a.md"]
    assert form["branch"].startswith("automarket/")
    assert form["branch"] != "main"

    prs = [c for c in bitbucket.calls if c[1].endswith("/pullrequests")]
    assert len(prs) == 1
    assert prs[0][2]["json_body"]["destination"]["branch"]["name"] == "main"


def test_both_connectors_present_themselves_as_a_cms():
    """So the existing agents pick them up with no change.

    An agent asks for "something that can write a page"; whether that is
    WordPress or a pull request against a repository is not its concern.
    """
    from app.connectors.base.connector import Capability

    for klass in (GitHubConnector, BitbucketConnector):
        assert klass.spec.category == "CMS"
        assert Capability.LIST_PAGES in klass.spec.capabilities
        assert Capability.WRITE_PAGE in klass.spec.capabilities
        assert Capability.INJECT_SCHEMA in klass.spec.capabilities
        # The token is the only secret; the rest is configuration the console
        # may show back to the operator.
        secrets = [f.key for f in klass.spec.fields if f.is_secret]
        assert secrets == ["token"], secrets


# ── Framework pages ────────────────────────────────────────────────────────
# The case that prompted these: a Next.js repository connected fine, the token
# worked, and the agent reported "synced 0 pages" as a success — because the
# only extensions recognised were Markdown and HTML, and every page in the
# repository was .jsx.
@pytest.mark.parametrize(
    ("path", "is_page"),
    [
        # Next App Router.
        ("src/app/page.jsx", True),
        ("src/app/blog/page.jsx", True),
        ("app/about/page.tsx", True),
        # Reserved App Router filenames are not pages.
        ("src/app/layout.tsx", False),
        ("src/app/loading.tsx", False),
        ("src/app/not-found.tsx", False),
        ("src/app/api/hook/route.ts", False),
        # Other frameworks' routing conventions.
        ("src/pages/index.tsx", True),
        ("src/routes/+page.svelte", True),
        ("src/views/Home.vue", True),
        # Components are not pages, wherever they live.
        ("src/components/Nav.tsx", False),
        ("src/App.tsx", False),
        ("src/hooks/useThing.ts", False),
        # Tooling is not a page.
        ("src/pages/Home.test.tsx", False),
        ("src/pages/Button.stories.tsx", False),
        # Repository documentation is not a page — and must never claim "/",
        # or an SEO agent would rewrite the project's own README as marketing
        # copy.
        ("README.md", False),
        ("CHANGELOG.md", False),
        ("CONTRIBUTING.md", False),
        # A dynamic route is a template for many URLs, not one page.
        ("src/app/blog/[slug]/page.jsx", False),
        ("src/app/[...all]/page.tsx", False),
        ("src/routes/blog/[id]/+page.svelte", False),
    ],
)
def test_a_framework_repository_yields_its_pages_and_nothing_else(path: str, is_page: bool):
    assert cf.is_content_path(path) is is_page


def test_a_configured_content_folder_is_taken_at_its_word():
    """The operator knows their own repository better than any convention.

    Without one, a routing directory has to appear in the path — otherwise
    pointing this at a React repository would report every component as a
    page.
    """
    assert cf.is_content_path("src/Home.tsx") is False
    assert cf.is_content_path("src/Home.tsx", content_root_configured=True) is True
    # But not tooling or components, even then.
    assert cf.is_content_path("src/Home.test.tsx", content_root_configured=True) is False
    assert cf.is_content_path("src/components/Nav.tsx", content_root_configured=True) is False


@pytest.mark.parametrize(
    ("path", "url"),
    [
        ("src/app/page.jsx", "/"),
        ("src/app/blog/page.jsx", "/blog"),
        ("src/app/schedulemeeting/page.jsx", "/schedulemeeting"),
        ("src/pages/index.astro", "/"),
        ("content/blog/a.md", "/blog/a"),
    ],
)
def test_the_route_root_is_derived_when_none_is_configured(path: str, url: str):
    """Otherwise a Next app's home page is reported as /src/app.

    Every SEO figure would then be attached to a URL that does not exist.
    """
    assert cf.path_to_url(path) == url


def test_an_empty_listing_is_explained_rather_than_reported_as_success():
    """The silence this replaces.

    "0 pages synced" on a healthy connector leaves somebody watching an agent
    do nothing for a week. The extension histogram is the useful part: it is
    the difference between "your repository is empty" and "your pages are .tsx
    and they live in src/pages".
    """

    class OnlyComponents(RecordingGitHub):
        def request(self, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
            if path.endswith("/git/trees/main"):
                return {
                    "tree": [
                        {"type": "blob", "path": f"content/lib/thing{i}.ts", "sha": "s", "size": 1}
                        for i in range(9)
                    ]
                    + [
                        {"type": "blob", "path": "content/tsconfig.json", "sha": "s", "size": 1},
                    ]
                }
            return super().request(method, path, **kwargs)

    github = OnlyComponents(PAGE)
    assert github.list_pages() == []

    explanation = github.describe_content()
    assert "10 files" in explanation
    assert ".ts (9)" in explanation
    assert "acme/site" in explanation


def test_the_explanation_names_the_content_folder_when_pages_are_framework_files():
    class FrameworkOnly(RecordingGitHub):
        def request(self, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
            if path.endswith("/git/trees/main"):
                return {
                    "tree": [
                        {"type": "blob", "path": "content/src/Widget.tsx", "sha": "s", "size": 1}
                    ]
                }
            return super().request(method, path, **kwargs)

    explanation = FrameworkOnly(PAGE).describe_content()
    assert "Set the content folder" in explanation
    assert "src/pages" in explanation
