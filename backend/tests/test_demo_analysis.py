"""Public website analysis: crawl, checks, scores and report — no network, no DB."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.demo_analysis import ai, checks, collect, service

SITE = "https://glowsalon.example"

HOME = """<!doctype html><html lang="en"><head>
<title>Glow Salon</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="Glow Salon">
<link rel="icon" href="/favicon.ico">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"HairSalon","name":"Glow Salon",
"telephone":"+91 98100 00000","address":{"streetAddress":"12 Market Rd","addressLocality":"Noida"}}</script>
</head><body><h1>Hair and beauty in Noida</h1>
<h2>Why choose us?</h2><p>""" + "We cut, colour and style hair for everyone. " * 20 + """</p>
<a href="/services">Services</a> <a href="/about">About</a> <a href="/missing">Old page</a>
<a href="tel:+919810000000">Call</a> <a href="https://instagram.com/glowsalon">Instagram</a>
<img src="a.jpg"><img src="b.jpg" alt="Our salon">
</body></html>"""

SERVICES = """<html><head><title>Services</title></head><body><h1>Services</h1><p>Haircut, colour.</p>
<form action="/book"></form></body></html>"""

ABOUT = """<html><head><title>Glow Salon</title><meta name="robots" content="noindex"></head>
<body><h1>About</h1><h1>Team</h1><p>Family run since 2010.</p></body></html>"""

SITEMAP = """<?xml version="1.0"?><urlset><url><loc>https://glowsalon.example/</loc></url>
<url><loc>https://glowsalon.example/services</loc></url><url><loc>https://glowsalon.example/about</loc></url></urlset>"""

PAGES = {
    "/": (200, HOME, "text/html"),
    "/services": (200, SERVICES, "text/html"),
    "/about": (200, ABOUT, "text/html"),
    "/robots.txt": (200, "User-agent: *\nDisallow: /admin\nSitemap: https://glowsalon.example/sitemap.xml\n", "text/plain"),
    "/sitemap.xml": (200, SITEMAP, "application/xml"),
}


def fake_get(url: str, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> httpx.Response:
    if url.startswith("http://"):
        return httpx.Response(200, text="", request=httpx.Request("GET", SITE + "/"))
    path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
    path = path.rstrip("/") or "/"
    status, body, kind = PAGES.get(path, (404, "<html>Not found</html>", "text/html"))
    return httpx.Response(status, text=body, headers={"content-type": kind}, request=httpx.Request("GET", url))


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect, "safe_get", fake_get)
    monkeypatch.setattr(collect, "check_public_url", lambda url: url)
    monkeypatch.setattr(collect, "pagespeed", lambda url, key="": {
        "measured": True, "strategy": "mobile",
        "scores": {"performance": 42, "seo": 90, "accessibility": 85, "best_practices": 100},
        "lab": {"lcp": {"value": 5200, "display": "5.2 s"}, "cls": {"value": 0.02, "display": "0.02"},
                "tbt": {"value": 700, "display": "700 ms"}, "fcp": None, "speed_index": None},
        "field": None, "opportunities": [{"title": "Properly size images", "saving_ms": 1800}],
    })


def test_collect_measures_the_site(offline: None) -> None:
    steps: list[tuple[str, str]] = []
    facts, pages = collect.collect(SITE, "https://www.google.com/maps/place/Glow+Salon/@28.5,77.3",
                                   step=lambda k, s, d: steps.append((k, s)))
    assert facts.https and facts.robots_txt and facts.sitemap_url_count == 3
    assert {p.url.rstrip("/") for p in pages} >= {SITE, SITE + "/services", SITE + "/about"}
    assert "Google Analytics 4" in facts.trackers
    assert facts.social_links == {"Instagram": "https://instagram.com/glowsalon"}
    assert facts.phones == ["+919810000000"]
    assert facts.local_business_schema["name"] == "Glow Salon"
    assert "Noida" in facts.local_business_schema["address"]
    assert any(b["url"].endswith("/missing") for b in facts.broken_links)
    assert facts.business["platform"] == "Google Business Profile"
    assert facts.business["name_in_link"] == "Glow Salon"
    assert ("ai", "done") not in steps and ("crawl", "done") in steps
    about = next(p for p in pages if p.url.endswith("/about"))
    assert about.noindex and about.h1_count == 2


def test_checks_quote_evidence_and_score(offline: None) -> None:
    facts, pages = collect.collect(SITE)
    found = {c.id: c for c in checks.build(facts, pages)}
    assert found["noindex"].status == checks.FAIL and "/about" in found["noindex"].evidence
    assert found["dup_titles"].status == checks.WARN
    assert found["h1"].status == checks.WARN  # /about has two H1s, none is missing one
    assert found["psi_performance"].status == checks.FAIL and "42/100" in found["psi_performance"].evidence
    assert found["lcp"].status == checks.FAIL
    assert found["analytics"].status == checks.PASS
    assert found["local_schema"].status == checks.PASS
    assert found["no_listing"].status == checks.INFO
    scores = checks.scores(list(found.values()))
    assert set(scores) == set(checks.CATEGORIES)
    assert all(s["score"] is None or 0 <= s["score"] <= 100 for s in scores.values())
    assert checks.grade(checks.overall(scores)) in "ABCDE"


def test_every_check_names_a_real_agent(offline: None) -> None:
    from app.agents.base import registry

    facts, pages = collect.collect(SITE, "https://facebook.com/glowsalon")
    slugs = set(registry.agent_slugs())
    for check in checks.build(facts, pages):
        assert not check.agent or check.agent in slugs, check.agent


def test_report_assembles_without_network(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "write", lambda facts, pages, found, llm=None: (
        {"executive_summary": "Good start.", "top_priorities": []}, {"content_plan": []}, {"input_tokens": 1, "output_tokens": 2}
    ))
    report = service.build_report(SITE, "")
    assert report["overall"]["score"] is not None
    assert report["summary"]["pages_read"] >= 3
    order = [c["status"] for c in report["checks"]]
    assert order == sorted(order, key=["fail", "warn", "info", "pass"].index)
    assert report["ai"]["strategy"]["executive_summary"] == "Good start."
    names = {a["agent"]: a for a in report["automation"]}
    assert "technical_seo_auditor" in names and names["technical_seo_auditor"]["what_it_does"]


def test_normalise_rejects_junk() -> None:
    from app.core.exceptions import InvalidInputError

    assert collect.normalise("Example.com/path?x=1") == "https://example.com"
    for bad in ("", "not a url", "ftp://example.com"):
        with pytest.raises(InvalidInputError):
            collect.normalise(bad)


def test_demo_needs_the_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "")
    with pytest.raises(ai.DemoUnavailable):
        ai.provider()


def test_hashes_are_keyed_and_stable() -> None:
    assert service.keyed_hash("device-1") == service.keyed_hash("device-1")
    assert service.keyed_hash("device-1") != service.keyed_hash("device-2")
    assert "device-1" not in service.keyed_hash("device-1")


def test_routes_are_public() -> None:
    from app.api.v1.router import api_router

    paths: dict[str, Any] = {r.path: r for r in api_router.routes}
    for path in ("/public/analysis", "/public/analysis/mine", "/public/analysis/{analysis_id}"):
        assert path in paths
        names = {d.call.__name__ for d in paths[path].dependant.dependencies}
        assert "get_current_user" not in names
