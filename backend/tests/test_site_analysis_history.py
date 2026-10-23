"""In-portal site analyses: history, comparison and the workspace's own model — no DB, no network."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.db.base import utcnow
from app.models.demo import DemoStatus, SiteAnalysis
from app.services.demo_analysis import ai, workspace


def _report(score: int, checks: list[tuple[str, str]], tech: int) -> dict[str, Any]:
    return {
        "overall": {"score": score, "grade": "B"},
        "summary": {"failed": sum(1 for _, s in checks if s == "fail"), "warnings": sum(1 for _, s in checks if s == "warn")},
        "categories": {"technical": {"label": "Technical health", "score": tech}},
        "checks": [{"id": i, "title": i.title(), "status": s, "evidence": f"{i} is {s}"} for i, s in checks],
    }


def _row(website: str, *, minutes_ago: int, report: dict | None, connectors: list[str], status: str = DemoStatus.DONE) -> SiteAnalysis:
    row = SiteAnalysis(
        id=f"id-{website}-{minutes_ago}", tenant_id="t1", website_url=website, status=status, report=report,
        context={"connectors": [{"slug": c, "name": c.title()} for c in connectors]}, progress=[], model="Claude",
        requested_by_name="Asha",
    )
    row.created_at = utcnow() - timedelta(minutes=minutes_ago)
    return row


def test_compare_lists_fixed_new_and_connector_changes() -> None:
    before = _row("https://shop.example", minutes_ago=60, connectors=["wordpress"],
                  report=_report(60, [("sitemap", "fail"), ("titles", "pass"), ("schema", "warn")], tech=50))
    after = _row("https://shop.example", minutes_ago=1, connectors=["wordpress", "google_search_console"],
                 report=_report(72, [("sitemap", "pass"), ("titles", "warn"), ("schema", "warn"), ("llms_txt", "fail")], tech=80))
    diff = workspace.compare(after, before)
    assert diff["overall"] == {"before": 60, "after": 72, "change": 12}
    assert diff["categories"]["technical"]["change"] == 30
    assert [f["title"] for f in diff["fixed"]] == ["Sitemap"]
    assert {n["title"] for n in diff["new_issues"]} == {"Titles", "Llms_Txt"}
    assert diff["still_open"] == ["Schema"]
    assert diff["connectors_added"] == ["Google_Search_Console"] and diff["connectors_removed"] == []


def test_no_comparison_without_a_previous_report() -> None:
    row = _row("https://a.example", minutes_ago=1, connectors=[], report=_report(50, [], 50))
    assert workspace.compare(row, None) is None


def test_summary_change_is_against_the_same_site_only() -> None:
    old_same = _row("https://www.shop.example/", minutes_ago=90, connectors=[], report=_report(55, [], 50))
    now = _row("https://shop.example", minutes_ago=1, connectors=["wordpress"], report=_report(70, [("x", "fail")], 50))
    assert workspace._site_key(old_same.website_url) == workspace._site_key(now.website_url)
    out = workspace.summary(now, old_same)
    assert out["change"] == 15 and out["issues"] == 1 and out["connectors"] == 1 and out["requested_by"] == "Asha"


def test_failed_rows_have_no_score() -> None:
    row = _row("https://a.example", minutes_ago=1, connectors=[], report=None, status=DemoStatus.FAILED)
    out = workspace.summary(row)
    assert out["score"] is None and out["change"] is None


def test_workspace_model_gets_the_long_timeout() -> None:
    from app.llm.providers import AnthropicProvider

    llm = AnthropicProvider(api_key="k", model="m", base_url="https://api.anthropic.com")
    ai.with_long_timeout(llm)
    assert llm._client is not None and llm._client.timeout.read == ai.LLM_TIMEOUT_SECONDS


def test_report_names_the_workspace_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.demo_analysis import service
    from tests.test_demo_analysis import SITE, fake_get

    from app.services.demo_analysis import collect

    monkeypatch.setattr(collect, "safe_get", fake_get)
    monkeypatch.setattr(collect, "check_public_url", lambda url: url)
    monkeypatch.setattr(collect, "pagespeed", lambda url, key="": {"measured": False, "reason": "skipped in test"})
    seen: dict[str, Any] = {}

    def fake_write(facts, pages, found, *, llm=None):  # noqa: ANN001, ANN202
        seen["llm"] = llm
        return {}, {}, {"input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(ai, "write", fake_write)
    marker = object()
    report = service.build_report(SITE, "", llm=marker, model_label="Google Gemini", pagespeed_key="", in_portal=True)
    assert seen["llm"] is marker
    assert report["ai"]["model"] == "Google Gemini"
    assert any("Google Gemini" in note for note in report["notes"])


def test_routes_exist_and_admin_routes_are_guarded() -> None:
    from app.api.v1.router import api_router
    from tests.support.routes import routes_by_path

    paths = routes_by_path(api_router)
    for path in ("/analyses", "/analyses/{analysis_id}", "/portal/analyses",
                 "/portal/analyses/website/{analysis_id}", "/portal/analyses/workspace/{org_id}/{analysis_id}"):
        assert path in paths, path
    for path in ("/portal/analyses", "/portal/analyses/website/{analysis_id}"):
        names = {d.call.__name__ for d in paths[path].dependant.dependencies}
        assert "require_portal_admin" in names
