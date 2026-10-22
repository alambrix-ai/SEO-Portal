"""The analysis report as one self-contained, interactive HTML file.

The customer opens it from their downloads folder, often offline, so the page
carries everything it needs: inline CSS and vanilla JS (from
``report_assets/``), and the report itself as JSON in a
``<script type="application/json">`` block. The script renders the page with
``textContent``/``createElement`` only; model output and text read from the
site are untrusted and never become markup. A ``<noscript>`` summary is
rendered server side for readers with scripts switched off.

Everything is normalised here first (types checked, lengths capped), so the
script can trust the shape of what it is given and malformed model output
can never break a download.
"""
from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path
from typing import Any

from app.services.demo_analysis.export import STATUS_LABEL, _d, _date, _l, _s, _sections, host

_ASSETS = Path(__file__).with_name("report_assets")
_CSS = (_ASSETS / "report.css").read_text(encoding="utf-8")
_JS = (_ASSETS / "report.js").read_text(encoding="utf-8")
# The assets are inlined, so they must not be able to close their own element.
assert "</style" not in _CSS.lower() and "</script" not in _JS.lower() and "<!--" not in _JS

_STATUSES = ("fail", "warn", "info", "pass")
_ROADMAP = (("first_30_days", "Days 1–30"), ("days_31_60", "Days 31–60"), ("days_61_90", "Days 61–90"))


# ── Normalising ─────────────────────────────────────────────────────────────
def _t(v: Any, limit: int = 4000) -> str:
    """Text, trimmed and capped."""
    s = _s(v)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _n(v: Any) -> int | float | None:
    """A finite number, or None (NaN/inf would make the JSON unparseable)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def _ts(v: Any, limit: int = 1000, most: int = 60) -> list[str]:
    """A list of non-empty strings."""
    return [t for t in (_t(x, limit) for x in _l(v)[:most]) if t]


def _url(v: Any) -> str:
    """Only http(s) URLs may become links."""
    s = _t(v, 2000)
    return s if re.match(r"^https?://", s, re.I) else ""


def _item_text(v: Any) -> str:
    """A list entry the model may give as a string or as a small object."""
    if isinstance(v, dict):
        return " — ".join(t for t in (_t(v.get(k)) for k in ("title", "action", "name", "text", "why")) if t)
    return _t(v)


def _pick(v: Any, keys: tuple[str, ...], lists: tuple[str, ...] = ()) -> dict[str, Any]:
    d = _d(v)
    out: dict[str, Any] = {k: _t(d.get(k)) for k in keys}
    out.update({k: _ts(d.get(k)) for k in lists})
    return out


def _objs(v: Any, keys: tuple[str, ...], lists: tuple[str, ...] = (), need: str = "", most: int = 60) -> list[dict[str, Any]]:
    rows = [_pick(x, keys, lists) for x in _l(v)[:most] if isinstance(x, dict)]
    return [r for r in rows if not need or r.get(need)]


def _pagespeed(v: Any) -> dict[str, Any]:
    p = _d(v)
    if not p.get("measured"):
        return {"measured": False, "reason": _t(p.get("reason"))}
    lab = {k: {"display": _t(_d(m).get("display"), 40), "value": _n(_d(m).get("value"))}
           for k, m in _d(p.get("lab")).items() if isinstance(m, dict)}
    field = {k: {"p75": _n(_d(m).get("p75")), "rating": _t(_d(m).get("rating"), 40)}
             for k, m in _d(p.get("field")).items() if isinstance(m, dict)}
    return {
        "measured": True,
        "strategy": _t(p.get("strategy"), 20) or "mobile",
        "scores": {k: _n(s) for k, s in _d(p.get("scores")).items()},
        "lab": lab,
        "field": field,
        "opportunities": [{"title": _t(_d(o).get("title"), 200), "saving_ms": _n(_d(o).get("saving_ms"))}
                          for o in _l(p.get("opportunities"))[:10] if _t(_d(o).get("title"))],
    }


def _measured(v: Any) -> dict[str, Any]:
    m = _d(v)
    pages = []
    for p in _l(m.get("pages"))[:60]:
        p = _d(p)
        if not _t(p.get("url")):
            continue
        pages.append({
            "url": _t(p.get("url"), 500), "status": _n(p.get("status")), "title": _t(p.get("title"), 300),
            "description": _t(p.get("meta_description"), 400), "h1": (_ts(p.get("h1"), 300) or [""])[0],
            "words": _n(p.get("words")), "response_ms": _n(p.get("response_ms")),
            "images": _n(p.get("images")), "images_without_alt": _n(p.get("images_without_alt")),
            "noindex": bool(p.get("noindex")),
        })
    return {
        "platform": _t(m.get("platform"), 100),
        "sitemap_pages": _n(m.get("sitemap_pages")),
        "trackers": _ts(m.get("trackers"), 100),
        "social_links": {_t(k, 40): _url(u) for k, u in _d(m.get("social_links")).items() if _t(k) and _url(u)},
        "pagespeed": _pagespeed(m.get("pagespeed")),
        "pages": pages,
    }


def _ai(report: dict[str, Any]) -> dict[str, Any]:
    sec = _sections(report)
    st, cr, camp, road = sec["strategy"], sec["creative"], sec["campaign"], sec["roadmap"]
    msg = sec["messaging"]
    return {
        "model": _t(sec["ai"].get("model"), 100),
        "business": _pick(sec["business"], ("name", "what_it_does", "industry", "tone", "business_model"),
                          ("audience", "offer", "locations")),
        "executive_summary": _t(st.get("executive_summary"), 6000),
        "strengths": _ts(st.get("strengths")),
        "top_priorities": _objs(st.get("top_priorities"), ("title", "why", "how", "impact", "effort", "timeframe"), need="title"),
        "messaging": {**_pick(msg, ("clarity_verdict", "current_headline", "suggested_headline", "suggested_subheadline",
                                    "suggested_cta"), ("trust_gaps",)),
                      "clarity_score": _n(msg.get("clarity_score"))},
        "keywords": _objs(st.get("keyword_opportunities"), ("keyword", "intent", "why", "target_page"), need="keyword"),
        "page_fixes": _objs(st.get("page_fixes"), ("url", "suggested_title", "suggested_description", "note")),
        "faq": _objs(st.get("faq_suggestions"), ("q", "a"), need="q"),
        "competitors": _objs(st.get("competitors_to_research"), ("name", "why"), need="name"),
        "local_actions": [t for t in map(_item_text, _l(st.get("local_actions"))[:30]) if t],
        "roadmap": [{"key": k, "label": label, "items": [t for t in map(_item_text, _l(road.get(k))[:40]) if t]}
                    for k, label in _ROADMAP],
        "content_plan": _objs(cr.get("content_plan"), ("title", "target_keyword", "type", "angle"), ("outline",), need="title"),
        "campaign": {
            **_pick(camp, ("campaign_name", "goal", "budget_note"), ("negative_keywords",)),
            "ad_groups": [{**g, "landing_page": _url(g.get("landing_page"))} for g in _objs(
                camp.get("ad_groups"), ("name", "landing_page"), ("keywords", "headlines", "descriptions"))
                if g["name"] or g["headlines"]],
        },
        "social_posts": _objs(cr.get("social_posts"), ("network", "text", "visual_idea"), ("hashtags",), need="text"),
        "emails": _objs(cr.get("email_campaigns"), ("name", "subject", "preview_text", "cta"), ("body_outline",)),
        "angles": _objs(cr.get("ad_creative_angles"), ("angle", "hook", "audience"), need="angle"),
    }


def _extra(extra: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"workspace": _t(extra.get("workspace"), 200), "comparison": None, "context": None}
    comp = _d(extra.get("comparison"))
    if comp:
        o = _d(comp.get("overall"))
        out["comparison"] = {
            "previous_at": _date(_s(comp.get("previous_at"))),
            "overall": {"before": _n(o.get("before")), "after": _n(o.get("after")), "change": _n(o.get("change"))},
            "categories": [{"key": _t(k, 60), "label": _t(_d(c).get("label"), 120) or _t(k, 60),
                            "before": _n(_d(c).get("before")), "after": _n(_d(c).get("after")), "change": _n(_d(c).get("change"))}
                           for k, c in _d(comp.get("categories")).items()],
            "fixed": _objs(comp.get("fixed"), ("title", "before", "after"), need="title", most=100),
            "new_issues": _objs(comp.get("new_issues"), ("title", "evidence", "status"), need="title", most=100),
            "still_open": [t for t in map(_item_text, _l(comp.get("still_open"))[:100]) if t],
            "connectors_added": _ts(comp.get("connectors_added"), 200),
            "connectors_removed": _ts(comp.get("connectors_removed"), 200),
        }
    ctx = extra.get("context")
    if isinstance(ctx, dict):
        ctx_llm = ctx.get("llm")
        out["context"] = {
            "connectors": _objs(ctx.get("connectors"), ("name", "category", "health", "last_error"), need="name", most=200),
            "agents": [{**_pick(a, ("name", "status", "last_run_at")), "configured": bool(_d(a).get("configured")),
                        "last_run": _date(_s(_d(a).get("last_run_at")))}
                       for a in _l(ctx.get("agents"))[:200] if _t(_d(a).get("name"))],
            "llm": _t(_d(ctx_llm).get("name") if isinstance(ctx_llm, dict) else ctx_llm, 200),
            "pagespeed": _t(ctx.get("pagespeed"), 200),
        }
    return out


def payload(report: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the page shows, with every type checked."""
    from app.core.config import settings

    report = _d(report)
    website = _t(report.get("website"), 500)
    summary = _d(report.get("summary"))
    checks = []
    for c in _l(report.get("checks"))[:300]:
        c = _d(c)
        if not _t(c.get("title")):
            continue
        status = _s(c.get("status"))
        checks.append({
            **_pick(c, ("id", "category", "title", "evidence", "why", "fix", "priority", "effort", "agent")),
            "status": status if status in _STATUSES else "info",
            "pages": _ts(c.get("pages"), 500, 50),
        })
    return {
        "app": _t(getattr(settings, "app_name", ""), 100) or "Synaprank.ai",
        "site": host(website) or "your website",
        "website": _url(website),
        "generated_at": _t(report.get("generated_at"), 60),
        "generated_label": _date(_s(report.get("generated_at"))),
        "overall": {"score": _n(_d(report.get("overall")).get("score")), "grade": _t(_d(report.get("overall")).get("grade"), 4)},
        "summary": {k: _n(summary.get(k)) or 0 for k in ("checks", "passed", "warnings", "failed", "pages_read", "links_checked")},
        "categories": [{"key": _t(k, 60), "label": _t(_d(c).get("label"), 120) or _t(k, 60), "about": _t(_d(c).get("about"), 400),
                        "score": _n(_d(c).get("score")), "passed": _n(_d(c).get("passed")) or 0, "total": _n(_d(c).get("total")) or 0}
                       for k, c in _d(report.get("categories")).items()],
        "checks": checks,
        "measured": _measured(report.get("measured")),
        "ai": _ai(report),
        "automation": _objs(report.get("automation"), ("agent", "name", "what_it_does"), ("addresses",), need="name"),
        "notes": _ts(report.get("notes"), 1000),
        "extra": _extra(_d(extra)),
    }


def _json_for_script(data: Any) -> str:
    """JSON that cannot end the <script> element it sits in, whatever the text.

    ``<``, ``>`` and ``&`` only ever occur inside JSON strings, where the
    ``\\uXXXX`` forms are equivalent, so ``</script>`` and ``<!--`` cannot
    appear; U+2028/U+2029 are escaped for older JavaScript parsers.
    """
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, default=str, separators=(",", ":"))
    for raw, safe in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(raw, safe)
    return text


# ── Server-rendered fallback ────────────────────────────────────────────────
def _noscript(data: dict[str, Any]) -> str:
    def e(v: str) -> str:
        return html.escape(v).replace(" ", "&#8232;").replace(" ", "&#8233;")

    ai, ov, sm = data["ai"], data["overall"], data["summary"]
    name = ai["business"].get("name") or data["site"]
    parts = [
        "<noscript><div class='ns'>",
        f"<p class='ns-k'>{e(data['app'])} · Website growth analysis</p><h1>{e(name)}</h1>",
        f"<p class='ns-m'>{e(data['site'])} · {e(data['generated_label'])}</p>",
        f"<p class='ns-score'><b>{e(_s(ov['score']) or '–')}</b> / 100 · Grade {e(ov['grade'] or '–')}</p>",
        f"<p>{sm['failed']} to fix · {sm['warnings']} to improve · {sm['passed']} good · {sm['pages_read']} pages read</p>",
    ]
    if ai["executive_summary"]:
        parts.append(f"<h2>Executive summary <small>AI suggestion</small></h2><p>{e(ai['executive_summary'])}</p>")
    issues = [c for c in data["checks"] if c["status"] in ("fail", "warn")]
    issues.sort(key=lambda c: (c["status"] != "fail", {"high": 0, "medium": 1}.get(c["priority"], 2)))
    if issues:
        parts.append("<h2>Top issues <small>Measured</small></h2><ul>" + "".join(
            f"<li><b>{e(STATUS_LABEL[c['status']])}: {e(c['title'])}</b> — {e(c['evidence'])}"
            + (f"<br><i>How to fix:</i> {e(c['fix'])}" if c["fix"] else "") + "</li>" for c in issues[:10]) + "</ul>")
    if ai["top_priorities"]:
        parts.append("<h2>Top priorities <small>AI suggestion</small></h2><ol>" + "".join(
            f"<li><b>{e(p['title'])}</b>{' — ' + e(p['why']) if p['why'] else ''}</li>" for p in ai["top_priorities"]) + "</ol>")
    parts.append("<p class='ns-m'>Turn on JavaScript to explore every check, copy the ready-to-use marketing and "
                 "track the 90-day plan.</p></div></noscript>")
    return "".join(parts)


# ── The page ────────────────────────────────────────────────────────────────
def to_html(report: dict[str, Any], *, extra: dict[str, Any] | None = None) -> bytes:
    """One standalone, interactive page: no external files or requests."""
    data = payload(report, extra)
    title = f"Website analysis – {data['site']}"
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        "<meta name='referrer' content='no-referrer'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f"{_noscript(data)}<div id='app'></div>"
        f"<script type='application/json' id='report-data'>{_json_for_script(data)}</script>"
        f"<script>{_JS}</script></body></html>"
    )
    return page.encode("utf-8")
