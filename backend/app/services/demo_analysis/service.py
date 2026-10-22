"""Run a public website analysis: limits, background job, progress, report.

One analysis per device. "Device" is enforced three ways, because no single
signal is reliable from a browser:

1. the id the website stores in the visitor's browser (``X-Device-Id``);
2. a hash of network address + browser, which catches a cleared id;
3. a cap per network address per day, which catches scripted id rotation.

An analysis that fails (site unreachable, model error) does not count, so a
visitor is never locked out by our failure. Raw identifiers are never stored —
only keyed hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.demo import DemoAnalysis, DemoStatus
from app.services.demo_analysis import ai, checks as checks_mod, collect

log = get_logger(__name__)

REPORT_VERSION = 1
#: A job not finished in this long is treated as lost (server restarted).
STALE_AFTER = timedelta(minutes=15)
#: Shown when an analysis fails for a reason we did not author a message for.
ANALYSIS_FAILED_MESSAGE = "Something went wrong while analysing the site. Please try again."

STEPS: list[tuple[str, str]] = [
    ("fetch", "Opening your website"),
    ("discover", "Reading robots.txt and sitemap"),
    ("crawl", "Reading your pages"),
    ("links", "Checking links"),
    ("speed", "Measuring mobile speed with Google"),
    ("listing", "Checking your business listing"),
    ("score", "Scoring the checks"),
    ("ai", "Writing your strategy, keywords, ads and content plan"),
]

_pool: ThreadPoolExecutor | None = None


class AlreadyAnalysed(AppError):
    status_code = 409
    code = "already_analysed"


class LimitReached(AppError):
    status_code = 429
    code = "demo_limit_reached"


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=max(1, settings.demo_max_concurrent), thread_name_prefix="demo")
    return _pool


def keyed_hash(value: str) -> str:
    return hmac.new(settings.secret_key.encode(), ("demo:" + value).encode(), hashlib.sha256).hexdigest()


def _counts(db: Session, *conditions: Any) -> int:
    return int(db.scalar(
        select(func.count()).select_from(DemoAnalysis).where(DemoAnalysis.status != DemoStatus.FAILED, *conditions)
    ) or 0)


def _expire_stale(db: Session) -> None:
    cutoff = utcnow() - STALE_AFTER
    for row in db.scalars(select(DemoAnalysis).where(
        DemoAnalysis.status.in_((DemoStatus.QUEUED, DemoStatus.RUNNING)), DemoAnalysis.created_at < cutoff
    )):
        row.status = DemoStatus.FAILED
        row.error = "The analysis was interrupted. Please run it again."


def for_device(db: Session, device_id: str) -> DemoAnalysis | None:
    return db.scalars(
        select(DemoAnalysis)
        .where(DemoAnalysis.device_hash == keyed_hash(device_id), DemoAnalysis.status != DemoStatus.FAILED)
        .order_by(DemoAnalysis.created_at.desc())
    ).first()


def start(
    db: Session, *, website: str, business_url: str, device_id: str, ip: str, user_agent: str
) -> tuple[DemoAnalysis, str]:
    """Create the analysis and queue it. Returns ``(row, access_token)``."""
    if not settings.demo_enabled:
        raise ai.DemoUnavailable("The free website analysis is switched off right now.")
    ai.provider()  # fail before queueing if the server has no model configured
    device_id = (device_id or "").strip()
    if not 16 <= len(device_id) <= 128:
        raise InvalidInputError("Missing device id (X-Device-Id header, 16-128 characters).")
    site = collect.normalise(website)
    collect.check_public_url(site)
    business_url = (business_url or "").strip()[:1000]

    device_hash = keyed_hash(device_id)
    ip_hash = keyed_hash("ip:" + ip)
    fingerprint = keyed_hash(f"fp:{ip}|{(user_agent or '')[:300]}")

    # Serialise starts from the same device so two quick clicks make one run.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "demo:" + device_hash[:32]})
    _expire_stale(db)

    existing = for_device(db, device_id)
    if existing is not None:
        raise AlreadyAnalysed(
            "This device has already run its free analysis.",
            details={"analysis_id": existing.id, "status": existing.status},
        )
    if _counts(db, DemoAnalysis.fingerprint_hash == fingerprint):
        raise LimitReached("This device has already run its free analysis.")
    day_ago = utcnow() - timedelta(days=1)
    if _counts(db, DemoAnalysis.ip_hash == ip_hash, DemoAnalysis.created_at >= day_ago) >= settings.demo_max_per_ip_per_day:
        raise LimitReached("Too many analyses from this network today. Please try again tomorrow.")
    if _counts(db, DemoAnalysis.created_at >= day_ago) >= settings.demo_max_per_day:
        raise LimitReached("We have reached today's limit of free analyses. Please try again tomorrow.")

    token = secrets.token_urlsafe(24)
    row = DemoAnalysis(
        device_hash=device_hash,
        fingerprint_hash=fingerprint,
        ip_hash=ip_hash,
        token_hash=keyed_hash("token:" + token),
        website_url=site,
        business_url=business_url,
        status=DemoStatus.QUEUED,
        progress=[{"key": k, "label": label, "state": "pending", "detail": ""} for k, label in STEPS],
        model=ai.model_name()[:80],
    )
    db.add(row)
    db.commit()
    _executor().submit(run, row.id)
    return row, token


def get(db: Session, analysis_id: str, *, token: str = "", device_id: str = "") -> DemoAnalysis:
    row = db.get(DemoAnalysis, analysis_id)
    allowed = row is not None and (
        (token and hmac.compare_digest(row.token_hash, keyed_hash("token:" + token)))
        or (device_id and hmac.compare_digest(row.device_hash, keyed_hash(device_id.strip())))
    )
    if not allowed:
        raise NotFoundError("Analysis not found")
    if row.status in (DemoStatus.QUEUED, DemoStatus.RUNNING) and row.created_at < utcnow() - STALE_AFTER:
        _expire_stale(db)
        db.commit()
    return row


# ── The job ─────────────────────────────────────────────────────────────────
def run(analysis_id: str) -> None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.get(DemoAnalysis, analysis_id)
        if row is None:
            return
        row.status = DemoStatus.RUNNING
        row.started_at = utcnow()
        db.commit()

        def step(key: str, state: str, detail: str = "") -> None:
            progress = [dict(s) for s in row.progress]
            for item in progress:
                if item["key"] == key:
                    item["state"], item["detail"] = state, detail[:200]
            row.progress = progress
            db.commit()

        try:
            row.report = build_report(row.website_url, row.business_url, step=step)
            row.status = DemoStatus.DONE
        except AppError as exc:
            row.status, row.error = DemoStatus.FAILED, exc.message
        except Exception as exc:  # noqa: BLE001 - the visitor gets a message, the log gets the type
            # Public, unauthenticated page: exception text can carry keys,
            # vendor bodies or internals, so it is never stored or shown.
            log.error("Demo analysis %s failed (%s)", analysis_id, type(exc).__name__)
            row.status = DemoStatus.FAILED
            row.error = ANALYSIS_FAILED_MESSAGE
        if row.status == DemoStatus.FAILED:
            progress = [dict(s) for s in row.progress]
            for item in progress:
                if item["state"] == "running":
                    item["state"] = "failed"
            row.progress = progress
        row.finished_at = utcnow()
        db.commit()
    finally:
        db.close()


def build_report(  # noqa: ANN201
    website: str,
    business_url: str,
    *,
    step=lambda *a: None,  # noqa: ANN001
    llm: Any = None,
    model_label: str = "",
    pagespeed_key: str | None = None,
    in_portal: bool = False,
) -> dict[str, Any]:
    """Measure, score and write the report.

    The public demo passes no ``llm`` (the platform's Claude is used); a
    workspace passes its own connected model and, when it has one, its
    PageSpeed Insights key.
    """
    facts, pages = collect.collect(
        website,
        business_url,
        pagespeed_key=settings.demo_pagespeed_api_key if pagespeed_key is None else pagespeed_key,
        step=step,
    )

    step("score", "running", "Scoring")
    found = checks_mod.build(facts, pages)
    category_scores = checks_mod.scores(found)
    total = checks_mod.overall(category_scores)
    failing = [c for c in found if c.status in (checks_mod.FAIL, checks_mod.WARN)]
    step("score", "done", f"{len(found)} checks · {len(failing)} to fix")

    step("ai", "running", "Writing your strategy")
    strategy, creative, usage = ai.write(facts, pages, found, llm=llm)
    model = model_label or (getattr(llm, "model", "") if llm is not None else ai.model_name())
    step("ai", "done", "Done")

    order = {checks_mod.FAIL: 0, checks_mod.WARN: 1, checks_mod.INFO: 2, checks_mod.PASS: 3}
    prio = {"high": 0, "medium": 1, "low": 2}
    found.sort(key=lambda c: (order[c.status], prio.get(c.priority, 3)))

    return {
        "version": REPORT_VERSION,
        "generated_at": utcnow().isoformat(),
        "website": facts.final_url,
        "duration_s": facts.duration_s,
        "overall": {"score": total, "grade": checks_mod.grade(total)},
        "categories": category_scores,
        "summary": {
            "checks": len(found),
            "passed": sum(1 for c in found if c.status == checks_mod.PASS),
            "warnings": sum(1 for c in found if c.status == checks_mod.WARN),
            "failed": sum(1 for c in found if c.status == checks_mod.FAIL),
            "pages_read": len(pages),
            "links_checked": facts.links_checked,
        },
        "checks": [c.as_dict() for c in found],
        "measured": {
            "platform": facts.platform,
            "https": facts.https,
            "sitemap_url": facts.sitemap_url,
            "sitemap_pages": facts.sitemap_url_count,
            "robots_txt": facts.robots_txt,
            "llms_txt": facts.llms_txt,
            "trackers": facts.trackers,
            "social_links": facts.social_links,
            "phones": facts.phones,
            "emails": facts.emails,
            "local_business_schema": facts.local_business_schema,
            "business_listing": facts.business,
            "pagespeed": facts.pagespeed,
            "broken_links": facts.broken_links,
            "pages": [
                {
                    "url": p.url, "status": p.status, "title": p.title, "meta_description": p.meta_description,
                    "h1": p.h1[:1], "words": p.word_count, "response_ms": p.response_ms, "html_kb": p.html_kb,
                    "images": p.images, "images_without_alt": p.images_without_alt,
                    "schema": p.schema_types, "noindex": p.noindex,
                }
                for p in pages
            ],
        },
        "ai": {"model": model, "strategy": strategy, "creative": creative, "usage": usage},
        "automation": automation(found),
        "notes": [
            "Measured: every check and score comes from reading the site and Google PageSpeed today.",
            f"Pages read: {len(pages)} (home page plus a spread from the sitemap and navigation). "
            + ("The SEO agents audit every page of the sites they are set to work on."
               if in_portal else "Connecting the site in Synaprank.ai audits every page."),
            "AI suggestions (business summary, strategy, keywords, ads, posts, emails, competitors) are "
            f"written by {model or 'the AI model'} from the site's own text — review them before use.",
            "Not measured here: search rankings, traffic, backlinks and Google Maps position — they need "
            "your Search Console, Analytics and listing connected.",
        ],
    }


def automation(found: list[checks_mod.Check]) -> list[dict[str, Any]]:
    """Which agents work on the problems found, in the agents' own words."""
    from app.agents.base import registry
    from app.agents.base.plain import guide_for

    by_agent: dict[str, list[checks_mod.Check]] = {}
    for c in found:
        if c.agent and c.status in (checks_mod.FAIL, checks_mod.WARN, checks_mod.INFO):
            by_agent.setdefault(c.agent, []).append(c)
    out = []
    for slug, items in by_agent.items():
        spec = getattr(registry.get_agent(slug), "spec", None)
        guide = guide_for(slug)
        out.append({
            "agent": slug,
            "name": getattr(spec, "name", slug.replace("_", " ").title()),
            "what_it_does": getattr(guide, "summary", "") if guide else "",
            "addresses": [c.title for c in items],
        })
    out.sort(key=lambda a: -len(a["addresses"]))
    return out


#: How long a signed download link in a report response stays valid.
DOWNLOAD_LINK_SECONDS = 7 * 24 * 3600


def download_signature(analysis_id: str, fmt: str, expires: int) -> str:
    return keyed_hash(f"download:{analysis_id}:{fmt}:{expires}")[:40]


def verify_download(analysis_id: str, fmt: str, expires: int, sig: str) -> bool:
    if expires < int(time.time()):
        return False
    return hmac.compare_digest(download_signature(analysis_id, fmt, expires), sig or "")


def download_links(row: DemoAnalysis) -> dict[str, dict[str, Any]]:
    """Ready-to-use links for both formats, so the website can use plain
    ``<a href>`` links (which cannot carry the device header) at any time."""
    from app.services.demo_analysis import export

    expires = int(time.time()) + DOWNLOAD_LINK_SECONDS
    base = (settings.public_api_base_url or settings.public_base_url).rstrip("/")
    out = {}
    for fmt, (label, media, _ext) in export.FORMATS.items():
        sig = download_signature(row.id, fmt, expires)
        out[fmt] = {
            "label": label,
            "media_type": media.split(";")[0],
            "filename": export.filename(row.report or {"website": row.website_url}, fmt),
            "url": f"{base}/api/v1/public/analysis/{row.id}/download?format={fmt}&expires={expires}&sig={sig}",
            "expires_at": expires,
        }
    return out


def public_view(row: DemoAnalysis, *, token: str | None = None) -> dict[str, Any]:
    done = [s for s in row.progress if s.get("state") in ("done", "skipped")]
    return {
        "id": row.id,
        "status": row.status,
        "website": row.website_url,
        "business_url": row.business_url,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "progress": row.progress,
        "percent": 100 if row.status == DemoStatus.DONE else round(100 * len(done) / max(len(row.progress), 1)),
        "error": row.error or None,
        "report": row.report if row.status == DemoStatus.DONE else None,
        "downloads": download_links(row) if row.status == DemoStatus.DONE and row.report else None,
        **({"access_token": token} if token else {}),
    }
