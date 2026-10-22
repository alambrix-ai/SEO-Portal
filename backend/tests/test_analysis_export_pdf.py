"""The enterprise PDF layout: cover, contents, numbered sections, robustness."""
from __future__ import annotations

import re
import time

import pytest

from app.services.demo_analysis import export_pdf
from app.services.demo_analysis.export_pdf import _clean, _fancy, _Report, _soft, to_pdf

REPORT = {
    "website": "https://glow.example",
    "generated_at": "2026-10-05T10:00:00+00:00",
    "duration_s": 12.5,
    "overall": {"score": 74, "grade": "C"},
    "summary": {"checks": 3, "failed": 1, "warnings": 1, "passed": 1, "pages_read": 3, "links_checked": 9},
    "categories": {
        "technical": {"label": "Technical health", "about": "Reach", "score": 80, "passed": 2, "total": 3},
        "performance": {"label": "Speed & experience", "about": "Speed", "score": None, "passed": 0, "total": 0},
    },
    "checks": [
        {"id": "sitemap", "category": "technical", "title": "XML sitemap", "status": "fail", "evidence": "No sitemap <found>",
         "fix": "Add one", "why": "x", "priority": "high", "effort": "quick", "agent": "technical_seo_auditor"},
        {"id": "speed", "category": "performance", "title": "Speed", "status": "info", "evidence": "Not measured", "fix": "Retry", "why": "x"},
        {"id": "https", "category": "technical", "title": "HTTPS", "status": "pass", "evidence": "Secure → yes ▲", "fix": "n/a", "why": "x"},
    ],
    "measured": {"platform": "WordPress", "https": True, "pagespeed": {"measured": True, "scores": {"performance": 48, "seo": 100},
                                                                     "lab": {"lcp": "4.1 s"}, "opportunities": ["Compress images"]}},
    "ai": {
        "model": "Claude",
        "usage": {"input_tokens": 12000, "output_tokens": 3000},
        "strategy": {
            "business": {"name": "Glow Salon", "industry": "Hair salon", "audience": ["locals"]},
            "executive_summary": "Good & growing.",
            "strengths": ["Fast booking"],
            "top_priorities": [{"title": "Add a sitemap", "why": "Discovery", "how": "Use a plugin", "impact": "high", "effort": "low", "timeframe": "this week"}],
            "messaging": {"clarity_score": 6, "clarity_verdict": "Mostly", "current_headline": "Welcome", "suggested_headline": "Haircuts in Noida",
                          "suggested_cta": "Book now", "trust_gaps": ["No reviews"]},
            "keyword_opportunities": [{"keyword": "salon noida", "intent": "local", "why": "fits", "target_page": "new page: /a-very-long-slug-for-the-salon-page"}],
            "page_fixes": [{"url": "https://glow.example/services", "suggested_title": "Services | Glow", "suggested_description": "Cuts and colour."}],
            "faq_suggestions": [{"q": "Do you take walk-ins?", "a": "Yes."}],
            "competitors_to_research": [{"name": "Shine", "why": "Nearby"}],
            "local_actions": ["Claim the Google Business Profile"],
            "roadmap": {"first_30_days": ["Fix sitemap"], "days_31_60": ["Write FAQ"], "days_61_90": []},
        },
        "creative": {
            "content_plan": [{"title": "Haircare guide", "type": "guide", "target_keyword": "hair care", "outline": ["Intro"]}],
            "search_campaign": {"campaign_name": "Salon", "ad_groups": [{"name": "Haircuts", "keywords": ["haircut noida"],
                                                                         "headlines": ["Haircut in Noida"], "descriptions": ["Book today"],
                                                                         "landing_page": "https://glow.example/book"}],
                                "negative_keywords": ["free"]},
            "social_posts": [{"network": "Instagram", "text": "New look 💇", "hashtags": ["salon"]}],
            "email_campaigns": [{"name": "welcome", "subject": "Hi", "body_outline": ["Hello"], "cta": "Book"}],
            "ad_creative_angles": [{"angle": "Speed", "hook": "In and out in 30 min", "audience": "Busy people"}],
        },
    },
    "automation": [{"agent": "technical_seo_auditor", "name": "Technical SEO Auditor", "what_it_does": "Checks", "addresses": ["XML sitemap"]}],
    "notes": ["Measured today."],
}

EXTRA = {
    "workspace": "Glow",
    "comparison": {"previous_at": "2026-09-01", "overall": {"before": 60, "after": 74},
                   "categories": {"technical": {"label": "Technical health", "before": 70, "after": 80}},
                   "fixed": [{"title": "HTTPS", "before": "fail", "after": "pass"}], "new_issues": [{"title": "Sitemap", "status": "fail"}],
                   "still_open": ["Speed"], "connectors_added": ["WordPress"], "connectors_removed": []},
    "context": {"connectors": [{"name": "WordPress", "category": "cms", "health": "failing", "last_error": "401"}],
                "agents": [{"name": "Writer", "status": "paused", "configured": True, "last_run_at": None}],
                "llm": {"name": "Claude"}, "pagespeed": True},
}


def _outline(pdf: bytes) -> list[str]:
    """Bookmark titles (one per numbered section), read from the PDF's outline."""
    return re.findall(rb"/Title \(([^)]*)\)", pdf)


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b(?!s)", pdf))


def _titles(report: dict, extra: dict) -> list[str]:
    return [title for title, *_ in _Report(report, extra).sections()]


def test_full_report_has_cover_contents_and_numbered_sections() -> None:
    pdf = _fancy(REPORT, EXTRA)
    assert pdf.startswith(b"%PDF")
    titles = [t.decode() for t in _outline(pdf)]
    expected = ["Executive summary", "Scorecard", "What changed", "Top priorities", "Detailed findings", "Messaging review",
                "Keyword opportunities", "Ready-to-paste titles and descriptions", "Content plan", "Search campaign",
                "Social posts and emails", "AI search and local", "90-day roadmap", "Automation", "Connected at the time",
                "Methodology and notes"]
    assert titles[-len(expected):] == [f"{i}. {t}" for i, t in enumerate(expected, 1)]
    # cover + contents + one page per section at least
    assert _pages(pdf) >= 2 + len(expected)


def test_comparison_and_context_only_when_given() -> None:
    titles = _titles(REPORT, {})
    assert "What changed" not in titles and "Connected at the time" not in titles
    assert titles[0] == "Executive summary" and titles[-1] == "Methodology and notes"
    with_extra = _titles(REPORT, EXTRA)
    assert "What changed" in with_extra and "Connected at the time" in with_extra


def test_page_numbers_settle_in_two_passes() -> None:
    """Contents page numbers come from a first pass; the second pass prints them and the total."""
    rep = _Report(REPORT, EXTRA)
    _, marks, total = export_pdf._build(rep, {}, 0)
    _, marks2, total2 = export_pdf._build(rep, marks, total)
    assert marks == marks2 and total == total2
    assert marks[1] == 3  # cover, contents, then section 1
    assert list(marks.values()) == sorted(marks.values())


def test_ai_sections_drop_out_when_model_output_is_missing() -> None:
    report = {**REPORT, "ai": None}
    titles = _titles(report, {})
    for gone in ("Top priorities", "Messaging review", "Keyword opportunities", "Search campaign", "90-day roadmap"):
        assert gone not in titles
    assert {"Executive summary", "Scorecard", "Detailed findings", "Methodology and notes"} <= set(titles)
    assert _fancy(report, {}).startswith(b"%PDF")


@pytest.mark.parametrize("broken", [
    {"ai": {"strategy": {"top_priorities": ["oops", None, 3], "messaging": "x", "keyword_opportunities": [None, "k"],
                         "roadmap": {"first_30_days": "not a list"}, "page_fixes": [{"url": None, "suggested_title": 5}]},
            "creative": {"search_campaign": {"ad_groups": [{"headlines": None, "descriptions": [None]}]}, "social_posts": "x"}}},
    {"overall": {"score": "high", "grade": None}, "summary": None, "categories": {"a": "b", "c": {"score": "n/a"}}},
    {"checks": [None, "x", {"status": "weird", "category": 5}], "measured": {"pagespeed": {"measured": True, "scores": None, "lab": "x"}}},
    {"website": None, "generated_at": 12345, "duration_s": "slow", "automation": [{"name": None}], "notes": "text"},
])
def test_malformed_input_still_uses_the_full_layout(broken: dict) -> None:
    """Bad model output drops or blanks a block; it never reaches the plain fallback."""
    assert _fancy({**REPORT, **broken}, {"comparison": {"overall": "x", "fixed": "y"}, "context": {"connectors": None}}).startswith(b"%PDF")


def test_huge_text_does_not_break_the_layout() -> None:
    big = "word " * 5000
    report = {**REPORT, "checks": [{**REPORT["checks"][0], "evidence": big, "fix": big, "why": big}] * 3}
    report["ai"] = {**REPORT["ai"], "strategy": {**REPORT["ai"]["strategy"], "executive_summary": big, "strengths": [big] * 20}}
    assert _fancy(report, {}).startswith(b"%PDF")


def test_to_pdf_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("layout bug")

    monkeypatch.setattr(export_pdf, "_fancy", boom)
    assert to_pdf(REPORT, extra=EXTRA).startswith(b"%PDF")
    assert to_pdf(None).startswith(b"%PDF")  # type: ignore[arg-type]


def test_text_is_made_safe_for_the_font() -> None:
    assert _clean("a → b ▲ ✓ 💇 ok") == "a -> b + ok"
    assert len(_clean("x" * 5000, 100)) == 100
    assert _clean(None) == "" and _clean(42) == "42"


def test_narrow_columns_break_at_hyphens_not_mid_word() -> None:
    out = _soft("new page: /a-very-long-slug-for-the-salon-page", 60, "Helvetica", 8)
    pieces = [p for line in out.split("\n") for p in line.split(" ")]
    assert "".join(out.split("\n")).replace(" ", "") == "newpage:/a-very-long-slug-for-the-salon-page"
    for p in pieces:
        assert p.endswith(("-", "/", ":")) or p in ("new", "page") or p == pieces[-1]


def test_build_is_fast_enough() -> None:
    start = time.perf_counter()
    _fancy(REPORT, EXTRA)
    assert time.perf_counter() - start < 3


def test_document_metadata_names_the_business() -> None:
    pdf = _fancy(REPORT, EXTRA)
    assert b"Website Growth Analysis - Glow Salon" in pdf
