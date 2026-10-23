"""Analysis report downloads: PDF and HTML, built from the stored report."""
from __future__ import annotations

import pytest

from app.core.exceptions import InvalidInputError
from app.services.demo_analysis import export

REPORT = {
    "website": "https://glow.example",
    "generated_at": "2026-10-05T10:00:00+00:00",
    "overall": {"score": 74, "grade": "C"},
    "summary": {"failed": 1, "warnings": 1, "passed": 1, "pages_read": 3, "links_checked": 9},
    "categories": {"technical": {"label": "Technical health", "about": "Reach", "score": 80, "passed": 2, "total": 3}},
    "checks": [
        {"id": "sitemap", "title": "XML sitemap", "status": "fail", "evidence": "No sitemap <found>", "fix": "Add one", "why": "x"},
        {"id": "https", "title": "HTTPS", "status": "pass", "evidence": "Secure → yes ▲", "fix": "n/a", "why": "x"},
    ],
    "measured": {"pagespeed": {"measured": False}},
    "ai": {
        "model": "Claude",
        "strategy": {
            "business": {"name": "Glow Salon", "industry": "Hair salon"},
            "executive_summary": "Good & growing.",
            "top_priorities": [{"title": "Add a sitemap", "why": "Discovery", "impact": "high"}],
            "keyword_opportunities": [{"keyword": "salon noida", "intent": "local", "why": "fits", "target_page": "new page"}],
            "roadmap": {"first_30_days": ["Fix sitemap"]},
        },
        "creative": {"search_campaign": {"ad_groups": [{"name": "Haircuts", "headlines": ["Haircut in Noida"], "descriptions": ["Book today"]}]}},
    },
    "automation": [{"agent": "technical_seo_auditor", "name": "Technical SEO Auditor", "what_it_does": "Checks", "addresses": ["XML sitemap"]}],
    "notes": ["Measured today."],
}


def test_pdf_is_a_pdf_with_every_section() -> None:
    content, media, name = export.render(REPORT, "pdf", extra={"workspace": "Glow", "context": {"connectors": [{"name": "WordPress"}]}})
    assert content.startswith(b"%PDF") and media == "application/pdf"
    assert name == "analysis-glow.example-2026-10-05.pdf"


def test_html_is_escaped_and_complete() -> None:
    content, media, name = export.render(REPORT, "html", extra={
        "comparison": {"previous_at": "2026-09-01", "overall": {"before": 60, "after": 74}, "fixed": [{"title": "HTTPS"}]},
    })
    page = content.decode()
    assert media.startswith("text/html") and name.endswith(".html")
    # Site and model text is escaped wherever it lands: HTML in the fallback, < in the embedded JSON.
    assert "No sitemap &lt;found&gt;" in page and "No sitemap \\u003cfound\\u003e" in page and "<found>" not in page
    for text in ("Glow Salon", "Executive summary", "Every check (", "Searches you should win", "Haircuts", "What changed since", "90-day plan"):
        assert text in page, text
    # Only the two script elements the page itself ships: the JSON data and the inline app.
    assert page.count("<script") == 2 and page.count("</script>") == 2
    assert "<script src" not in page and "<link" not in page


@pytest.mark.parametrize("broken", [
    {"ai": None},
    {"ai": {"strategy": "not a dict", "creative": []}},
    {"ai": {"strategy": {"top_priorities": ["oops", None], "business": "x", "roadmap": []}}},
    {"checks": None, "categories": None},
])
def test_malformed_model_output_never_breaks_a_download(broken: dict) -> None:
    report = {**REPORT, **broken}
    assert export.render(report, "pdf")[0].startswith(b"%PDF")
    page = export.render(report, "html")[0]
    assert page.startswith(b"<!doctype html>") and b"id='report-data'" in page and page.rstrip().endswith(b"</html>")


def test_unknown_format_is_refused() -> None:
    with pytest.raises(InvalidInputError):
        export.render(REPORT, "docx")


def test_download_routes_exist() -> None:
    from app.api.v1.router import api_router
    from tests.support.routes import route_paths

    paths = route_paths(api_router)
    assert {
        "/analyses/{analysis_id}/download",
        "/portal/analyses/website/{analysis_id}/download",
        "/portal/analyses/workspace/{org_id}/{analysis_id}/download",
        "/public/analysis/{analysis_id}/download",
    } <= paths
