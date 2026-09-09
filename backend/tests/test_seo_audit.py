"""The technical SEO checks.

Tested one rule at a time, because these are findings a customer will argue
with — "why is this page thin", "that link isn't broken" — and being able to
point at the exact test is the difference between a defensible audit and an
opinion.
"""
from __future__ import annotations

import pytest

from app.agents.technical_seo_auditor import checks
from app.agents.technical_seo_auditor.checks import (
    IssueKind,
    PageSnapshot,
    Severity,
    audit,
)


def page(**kw) -> PageSnapshot:  # noqa: ANN003
    defaults = {
        "id": kw.pop("id", "p1"),
        "url": kw.pop("url", "/page"),
        "title": kw.pop("title", "A page"),
        "body": kw.pop("body", ""),
        "word_count": kw.pop("word_count", 800),
        "schema_types": kw.pop("schema_types", ["Article"]),
    }
    return PageSnapshot(**defaults, **kw)


def kinds(findings) -> set[str]:  # noqa: ANN001
    return {f.kind.value for f in findings}


# ── Thin content ───────────────────────────────────────────────────────────
def test_a_page_under_the_threshold_is_thin_and_says_what_to_do():
    finding = checks.check_thin(page(word_count=120), minimum=300)
    assert finding is not None
    assert finding.kind is IssueKind.THIN_CONTENT
    # Almost nothing on it is urgent; just under the line is not.
    assert finding.severity is Severity.HIGH
    assert "300" in finding.summary
    # The recommendation warns against the obvious wrong fix.
    assert "Padding it" in finding.recommendation


def test_a_page_just_under_the_threshold_is_not_urgent():
    assert checks.check_thin(page(word_count=280), minimum=300).severity is Severity.MEDIUM


def test_a_page_at_the_threshold_is_not_flagged():
    assert checks.check_thin(page(word_count=300), minimum=300) is None


def test_the_threshold_is_configurable():
    """Google publishes no minimum, so this is a working number, not a law."""
    assert checks.check_thin(page(word_count=400), minimum=300) is None
    assert checks.check_thin(page(word_count=400), minimum=600) is not None


# ── Internal links ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("href", "internal"),
    [
        ("/about", True),
        ("about", True),
        ("https://site.com/about", True),
        ("https://www.site.com/about", True),
        ("https://other.com/about", False),
        ("#section", False),
        ("mailto:a@b.com", False),
        ("tel:+123", False),
        ("javascript:void(0)", False),
    ],
)
def test_internal_links_are_recognised_including_absolute_ones(href: str, internal: bool):
    """Plenty of CMS content is written with the full URL.

    Treating those as external would hide real broken links.
    """
    assert checks.is_internal(href, domain="site.com") is internal


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("/about/", "/about"),
        ("/about#team", "/about"),
        ("/about?utm=x", "/about"),
        ("/", "/"),
        ("/a/b/", "/a/b"),
    ],
)
def test_urls_are_compared_in_a_normalised_form(raw: str, normalised: str):
    """Otherwise the link checker generates false positives nobody trusts."""
    assert checks.normalise_url(raw) == normalised


def test_links_in_code_fences_are_not_audited():
    """A page documenting an example URL is not linking to it."""
    body = "```\n<a href='/not-real'>x</a>\n```\n<a href='/real'>y</a>"
    assert checks.links_in(body) == ["/real"]


def test_a_link_to_a_page_that_does_not_exist_is_broken():
    finding = checks.check_broken_links(
        page(body='<a href="/gone">x</a>'), known_urls={"/page"}
    )
    assert len(finding) == 1
    assert finding[0].severity is Severity.HIGH
    assert finding[0].evidence["target"] == "/gone"


def test_one_broken_target_linked_many_times_is_one_finding():
    """A navigation link repeated on forty pages is one URL to fix."""
    body = '<a href="/gone">a</a><a href="/gone">b</a><a href="/gone">c</a>'
    assert len(checks.check_broken_links(page(body=body), known_urls={"/page"})) == 1


def test_a_link_with_a_trailing_slash_is_not_broken():
    assert (
        checks.check_broken_links(
            page(body='<a href="/about/">x</a>'), known_urls={"/about"}
        )
        == []
    )


# ── Orphans ────────────────────────────────────────────────────────────────
def test_a_page_nothing_links_to_is_an_orphan():
    pages = [
        page(id="1", url="/", body='<a href="/reachable">x</a>'),
        page(id="2", url="/reachable"),
        page(id="3", url="/orphan"),
    ]
    findings = checks.check_orphans(pages)
    assert [f.url for f in findings] == ["/orphan"]
    assert findings[0].severity is Severity.HIGH


def test_the_home_page_is_never_an_orphan():
    """It is reached directly, so flagging it would be a permanent false positive."""
    assert checks.check_orphans([page(id="1", url="/")]) == []


def test_a_page_linking_only_to_itself_is_still_an_orphan():
    pages = [
        page(id="1", url="/", body="no links"),
        page(id="2", url="/lonely", body='<a href="/lonely">me</a>'),
    ]
    assert [f.url for f in checks.check_orphans(pages)] == ["/lonely"]


# ── Head tags and headings ─────────────────────────────────────────────────
def test_missing_description_canonical_and_h1_are_each_reported():
    found = kinds(checks.check_head_tags(page(body="<p>Just a paragraph.</p>")))
    assert found == {
        "missing_meta_description",
        "missing_canonical",
        "missing_h1",
    }


def test_frontmatter_satisfies_the_tags_a_template_would_render():
    """On a repository-backed site the template writes the tag from the
    frontmatter, so the raw file has no meta tag and reporting one would be
    wrong."""
    snapshot = page(
        body="# Title\n\nBody.",
        frontmatter_keys={"description", "canonical"},
    )
    assert checks.check_head_tags(snapshot) == []


def test_two_h1s_are_reported_but_not_as_urgent():
    findings = checks.check_head_tags(page(body="<h1>a</h1><h1>b</h1>"))
    multiple = next(f for f in findings if f.kind is IssueKind.MULTIPLE_H1)
    assert multiple.severity is Severity.LOW
    assert multiple.evidence["count"] == 2


# ── Images, titles ─────────────────────────────────────────────────────────
def test_images_without_alt_are_counted_in_html_and_markdown():
    body = '<img src="a.jpg"><img src="b.jpg" alt="described">![](c.jpg)![ok](d.jpg)'
    finding = checks.check_images(page(body=body))
    assert finding.evidence["count"] == 2


def test_a_long_title_is_reported_with_the_limit():
    finding = checks.check_title(page(title="x" * 90))
    assert finding.evidence["length"] == 90
    assert finding.evidence["limit"] == checks.TITLE_MAX_CHARS


def test_pages_sharing_a_title_are_both_flagged():
    """They compete for the same query, and neither wins."""
    findings = checks.check_duplicate_titles(
        [page(id="1", url="/a", title="Same"), page(id="2", url="/b", title="same")]
    )
    assert {f.url for f in findings} == {"/a", "/b"}
    assert findings[0].evidence["pages_sharing"] == 2


# ── Core Web Vitals ────────────────────────────────────────────────────────
def test_an_unmeasured_page_produces_no_speed_findings():
    """The most important negative test here.

    Vitals are field data from real devices. A page with no sample is
    unmeasured, and reporting it as passing would be a fabrication about the
    customer's site speed.
    """
    assert checks.check_vitals(page(lcp_ms=None, cls=None, inp_ms=None)) == []


def test_a_good_lcp_produces_nothing_and_a_poor_one_is_urgent():
    assert checks.check_vitals(page(lcp_ms=1800)) == []
    borderline = checks.check_vitals(page(lcp_ms=3200))[0]
    assert borderline.severity is Severity.MEDIUM
    poor = checks.check_vitals(page(lcp_ms=5200))[0]
    assert poor.severity is Severity.HIGH
    assert "5.2s" in poor.summary
    # The recommendation names the usual culprit rather than saying "optimise".
    assert "hero image" in poor.recommendation


def test_layout_shift_and_interaction_are_reported_separately():
    found = kinds(checks.check_vitals(page(lcp_ms=1000, cls=0.4, inp_ms=500)))
    assert found == {"poor_cls_mobile", "slow_inp_mobile"}


# ── The whole audit ────────────────────────────────────────────────────────
def test_the_audit_orders_by_expected_impact():
    """A broken link and a missing alt attribute are not equally urgent.

    Treating them the same is how audit tools train people to ignore them.
    """
    pages = [
        page(id="1", url="/", body='<a href="/dead">x</a><h1>H</h1>', word_count=900),
        page(id="2", url="/thin", word_count=50, schema_types=[]),
    ]
    findings = audit(pages, domain="site.com")
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
    assert "broken_internal_link" in kinds(findings)
    assert "thin_content" in kinds(findings)


def test_an_excluded_page_is_left_out_of_every_check():
    pages = [
        page(id="1", url="/", body="no links"),
        page(id="2", url="/ignored", word_count=10, schema_types=[], excluded=True),
    ]
    assert all(f.url != "/ignored" for f in audit(pages))


def test_an_empty_site_produces_no_findings_rather_than_failing():
    assert audit([]) == []
