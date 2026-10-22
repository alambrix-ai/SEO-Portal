"""The interactive HTML report: one self-contained file, safe with untrusted text."""
from __future__ import annotations

import html
import json
import re

import pytest

from app.services.demo_analysis.export_html import payload, to_html

EVIL = "</script><script>alert(1)</script> <!-- x --> <img src=x onerror=alert(2)>    & done"

REPORT = {
    "website": "https://glow.example",
    "generated_at": "2026-10-05T10:00:00+00:00",
    "overall": {"score": 74, "grade": "C"},
    "summary": {"checks": 3, "failed": 1, "warnings": 1, "passed": 1, "pages_read": 3, "links_checked": 9},
    "categories": {
        "technical": {"label": "Technical health", "about": "Reach", "score": 80, "passed": 2, "total": 3},
        "performance": {"label": "Speed & experience", "about": "Speed", "score": None, "passed": 0, "total": 0},
    },
    "checks": [
        {"id": "sitemap", "category": "technical", "title": "XML sitemap", "status": "fail", "evidence": EVIL,
         "fix": "Add one", "why": "x", "priority": "high", "effort": "quick", "agent": "technical_seo_auditor",
         "pages": ["https://glow.example/a"]},
        {"id": "https", "category": "technical", "title": "HTTPS", "status": "pass", "evidence": "Secure", "fix": "n/a", "why": "x"},
        {"id": "odd", "title": "Odd status", "status": "exploded", "evidence": "?"},
    ],
    "measured": {
        "platform": "WordPress",
        "pagespeed": {"measured": True, "scores": {"performance": 61, "seo": 90}, "lab": {"lcp": {"value": 3.2, "display": "3.2 s"}},
                      "opportunities": [{"title": "Reduce JS", "saving_ms": 900}]},
        "pages": [{"url": "https://glow.example/", "title": "Home", "words": 400, "response_ms": 300}],
        "social_links": {"facebook": "https://facebook.com/glow", "evil": "javascript:alert(1)"},
    },
    "ai": {
        "model": "Claude",
        "strategy": {
            "business": {"name": "Glow Salon", "industry": "Hair salon"},
            "executive_summary": EVIL,
            "top_priorities": [{"title": "Add a sitemap", "why": "Discovery", "impact": "high"}],
            "keyword_opportunities": [{"keyword": "salon noida", "intent": "local", "why": "fits", "target_page": "new page"}],
            "competitors_to_research": [{"name": "Shine", "why": "nearby"}],
            "faq_suggestions": [{"q": "Open Sundays?", "a": "Yes."}],
            "page_fixes": [{"url": "https://glow.example/", "suggested_title": "Glow", "suggested_description": "Hair"}],
            "messaging": {"current_headline": "Hi", "suggested_headline": "Haircuts in Noida", "clarity_score": 6},
            "local_actions": ["Claim the Google profile", {"action": "Ask for reviews"}],
            "roadmap": {"first_30_days": ["Fix sitemap"], "days_31_60": [], "days_61_90": "not a list"},
        },
        "creative": {
            "content_plan": [{"title": "Guide", "type": "how-to", "outline": ["One", "Two"]}],
            "search_campaign": {"campaign_name": "Search", "ad_groups": [{
                "name": "Haircuts", "headlines": ["Haircut in Noida", "Book Today", "Walk-ins Welcome"],
                "descriptions": ["Book today"], "keywords": ["haircut noida"], "landing_page": "javascript:alert(1)"}]},
            "social_posts": [{"network": "Instagram", "text": "New look?", "hashtags": ["hair"]}],
            "email_campaigns": [{"name": "welcome", "subject": "Hello", "body_outline": ["Hi"]}],
        },
    },
    "automation": [{"agent": "technical_seo_auditor", "name": "Technical SEO Auditor", "what_it_does": "Checks", "addresses": ["XML sitemap"]}],
    "notes": ["Measured today."],
}

EXTRA = {
    "workspace": "Glow <Team>",
    "comparison": {
        "previous_at": "2026-09-01T00:00:00+00:00",
        "overall": {"before": 60, "after": 74, "change": 14},
        "categories": {"technical": {"label": "Technical health", "before": 70, "after": 80, "change": 10}},
        "fixed": [{"title": "HTTPS", "before": "No", "after": "Yes"}],
        "new_issues": [{"title": "XML sitemap", "evidence": "Missing", "status": "fail"}],
        "still_open": ["Speed"],
        "connectors_added": ["WordPress"],
        "connectors_removed": [],
    },
    "context": {
        "connectors": [{"name": "WordPress", "category": "cms", "health": "ok", "last_error": ""}],
        "agents": [{"name": "Technical SEO Auditor", "status": "running", "configured": True, "last_run_at": "2026-10-04T09:00:00+00:00"}],
        "llm": {"slug": "anthropic", "name": "Anthropic Claude"},
        "pagespeed": "Platform",
    },
}


def _page(report=REPORT, extra=None) -> str:
    return to_html(report, extra=extra).decode("utf-8")


def _data(page: str) -> dict:
    m = re.search(r"<script type='application/json' id='report-data'>(.*?)</script>", page, re.S)
    assert m, "embedded report data missing"
    return json.loads(m.group(1))


def test_untrusted_text_cannot_escape_the_data_block() -> None:
    page = _page(extra=EXTRA)
    # Exactly the page's own two scripts; model text added none and closed none.
    assert page.count("<script") == 2 and page.count("</script>") == 2
    assert "<!--" not in page and "<img" not in page
    assert " " not in page and " " not in page
    # ...and the text survives intact for the script to render with textContent.
    data = _data(page)
    assert data["ai"]["executive_summary"] == EVIL
    assert data["checks"][0]["evidence"] == EVIL
    assert data["extra"]["workspace"] == "Glow <Team>"


def test_noscript_fallback_is_escaped_and_has_the_essentials() -> None:
    page = _page()
    fallback = page[page.index("<noscript>"):page.index("</noscript>")]
    assert html.escape(EVIL).replace(" ", "&#8232;").replace(" ", "&#8233;") in fallback
    for text in ("Glow Salon", "74", "Grade C", "Executive summary", "Top issues", "XML sitemap", "How to fix", "Add a sitemap"):
        assert text in fallback, text


def test_page_is_self_contained() -> None:
    page = _page(extra=EXTRA)
    assert page.startswith("<!doctype html>")
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert not re.search(r"<(script|img|iframe|link)[^>]+\bsrc=", page)
    assert "@import" not in page and "url(" not in page.split("id='report-data'")[0]
    # The app renders with the DOM API, never by parsing strings as HTML.
    script = page.rsplit("<script>", 1)[1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "document.write" not in script


def test_brand_sections_and_labels() -> None:
    from app.core.config import settings

    page = _page(extra=EXTRA)
    assert _data(page)["app"] == settings.app_name
    for text in ("Measured", "AI suggestion", "Suggested, not measured", "Search volumes are not estimated",
                 "Every check (", "Searches you should win", "What changed since", "Connected at the time",
                 "90-day plan", "Next combination", "Expand all", "localStorage", "prefers-color-scheme", "@media print"):
        assert text in page, text


def test_payload_normalises_untrusted_shapes() -> None:
    data = payload(REPORT, EXTRA)
    assert [c["status"] for c in data["checks"]] == ["fail", "pass", "info"]
    assert data["checks"][0]["pages"] == ["https://glow.example/a"]
    assert data["measured"]["social_links"] == {"facebook": "https://facebook.com/glow"}
    assert data["ai"]["campaign"]["ad_groups"][0]["landing_page"] == ""  # javascript: never becomes a link
    assert data["ai"]["local_actions"] == ["Claim the Google profile", "Ask for reviews"]
    assert [p["items"] for p in data["ai"]["roadmap"]] == [["Fix sitemap"], [], []]
    assert data["measured"]["pagespeed"]["measured"] is True and data["measured"]["pagespeed"]["scores"]["performance"] == 61
    assert data["extra"]["comparison"]["overall"]["change"] == 14
    assert data["extra"]["context"]["llm"] == "Anthropic Claude"
    assert data["extra"]["context"]["agents"][0]["last_run"] == "04 Oct 2026"


def test_public_demo_has_no_workspace_sections() -> None:
    data = _data(_page())
    assert data["extra"] == {"workspace": "", "comparison": None, "context": None}


@pytest.mark.parametrize("broken", [
    {},
    {"ai": "nonsense", "measured": [], "checks": "x", "categories": [], "overall": None, "summary": "x"},
    {"overall": {"score": float("nan"), "grade": 5}, "summary": {"failed": float("inf")}},
    {"checks": [None, 3, {"title": None}, {"title": "T", "pages": "not a list", "status": None}]},
    {"ai": {"strategy": {"top_priorities": [None, "oops", {"title": 7}], "roadmap": {"first_30_days": [None, {"x": 1}]},
                         "keyword_opportunities": [{"keyword": ["list"]}], "messaging": [], "business": None},
            "creative": {"search_campaign": {"ad_groups": [{"headlines": None}, "x"]}, "social_posts": {"a": 1}}}},
    {"measured": {"pagespeed": {"measured": True, "scores": None, "lab": [], "opportunities": [None]}}},
])
def test_malformed_input_still_renders(broken: dict) -> None:
    report = {**REPORT, **broken} if broken else {}
    page = _page(report, extra={"comparison": "nope", "context": {"connectors": None, "agents": [None], "llm": 5}})
    data = _data(page)  # strict JSON: NaN/inf were dropped
    assert page.rstrip().endswith("</html>") and isinstance(data["checks"], list)


def test_size_stays_reasonable() -> None:
    big = json.loads(json.dumps(REPORT))
    big["checks"] = [dict(REPORT["checks"][0], id=f"c{i}", evidence="e" * 200) for i in range(40)]
    big["ai"]["strategy"]["keyword_opportunities"] *= 20
    assert len(to_html(big, extra=EXTRA)) < 400_000
