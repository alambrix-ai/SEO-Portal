"""Site analyses run from inside the portal, kept as a history per workspace.

The same measurement and report as the public demo, with three differences:

* the writing is done by the **workspace's own connected AI model** (chosen
  on the run, or the first connected one) — never the platform's key;
* a PageSpeed Insights connector, when connected, supplies its own key;
* each run records what the workspace had connected and which agents were
  on, and is compared with the previous run of the same site, so the history
  shows what changed and why.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.demo import DemoStatus, SiteAnalysis
from app.models.workspace import Organization
from app.services.demo_analysis import collect, service

log = get_logger(__name__)

FEATURE = "site_analysis"
ACTIVE = (DemoStatus.QUEUED, DemoStatus.RUNNING)
BAD = ("fail", "warn")


# ── Inputs ──────────────────────────────────────────────────────────────────
def default_website(org: Organization) -> str:
    from app.services import brand_profile

    profile = brand_profile.get(org) or {}
    if profile.get("source_url"):
        return str(profile["source_url"])
    return f"https://{org.primary_domain}" if org.primary_domain else ""


def model_options(db: Session, org: Organization) -> list[dict[str, Any]]:
    """Connected AI models, every one equal; the first is the default."""
    from app.connectors.base import registry
    from app.llm.from_connector import supports_agent_llm
    from app.services.connectors import build_connector_bundle

    bundle = build_connector_bundle(db, org)
    try:
        return [
            {"slug": slug, "name": bundle.names.get(slug, slug)}
            for slug in registry.connector_slugs()
            if slug in bundle.instances and supports_agent_llm(slug)
        ]
    finally:
        bundle.close()


def snapshot(db: Session, org: Organization) -> dict[str, Any]:
    """What the workspace had connected and switched on, right now."""
    from app.connectors.base import registry
    from app.models.agent import AgentRecord
    from app.services.connectors import list_records

    connectors = []
    for record in list_records(db, tenant_id=org.id):
        if not record.connected:
            continue
        spec = registry.get_spec(record.slug)
        connectors.append({
            "slug": record.slug,
            "name": spec.name if spec else record.name,
            "category": str(getattr(spec, "category", "") or record.category),
            "health": record.health,
            "last_error": (record.last_error or "")[:200],
            "connected_at": record.connected_at.isoformat() if record.connected_at else None,
        })
    agents = [
        {
            "slug": a.slug,
            "name": a.name,
            "status": a.status,
            "configured": a.configured,
            "last_run_at": a.last_run_at.isoformat() if a.last_run_at else None,
        }
        for a in db.scalars(select(AgentRecord).where(AgentRecord.tenant_id == org.id).order_by(AgentRecord.name))
    ]
    return {"connectors": connectors, "agents": agents, "taken_at": utcnow().isoformat()}


# ── Start ───────────────────────────────────────────────────────────────────
def start(
    db: Session,
    org: Organization,
    *,
    user_id: str,
    user_name: str,
    website: str = "",
    business_url: str = "",
    llm_connector: str = "",
) -> SiteAnalysis:
    from app.services import entitlements

    policy = entitlements.feature_policy(db, org, FEATURE)
    if not policy.allowed:
        raise AppError(policy.reason or "Site analysis is not included in your plan.")
    site = collect.normalise(website or default_website(org))
    collect.check_public_url(site)

    models = model_options(db, org)
    if not models:
        raise ConflictError(
            "Connect an AI model (OpenAI, Anthropic Claude, Google Gemini or Perplexity) under "
            "Integrations first — it writes the strategy and marketing in the report."
        )
    chosen = (llm_connector or "").strip()
    if chosen and chosen not in {m["slug"] for m in models}:
        raise InvalidInputError("That AI model is not connected in this workspace.")
    model = next((m for m in models if m["slug"] == chosen), models[0])

    _expire_stale(db, org.id)
    if db.scalar(select(func.count()).select_from(SiteAnalysis).where(
        SiteAnalysis.tenant_id == org.id, SiteAnalysis.status.in_(ACTIVE)
    )):
        raise ConflictError("An analysis is already running for this workspace. Wait for it to finish.")
    limit = policy.max_per_day if policy.max_per_day is not None else settings.site_analysis_max_per_day
    today = db.scalar(select(func.count()).select_from(SiteAnalysis).where(
        SiteAnalysis.tenant_id == org.id,
        SiteAnalysis.status != DemoStatus.FAILED,
        SiteAnalysis.created_at >= utcnow() - timedelta(days=1),
    )) or 0
    if today >= limit:
        raise ConflictError(f"This workspace has run {today} analyses in the last 24 hours, the most allowed. Try again later.")

    context = snapshot(db, org)
    context["llm"] = model
    row = SiteAnalysis(
        tenant_id=org.id,
        requested_by=user_id,
        requested_by_name=user_name[:160],
        website_url=site,
        business_url=(business_url or "").strip()[:1000],
        status=DemoStatus.QUEUED,
        progress=[{"key": k, "label": label, "state": "pending", "detail": ""} for k, label in service.STEPS],
        context=context,
        model=model["name"][:80],
    )
    db.add(row)
    db.commit()
    service._executor().submit(run, row.id, org.id)
    return row


def _expire_stale(db: Session, tenant_id: str) -> None:
    cutoff = utcnow() - service.STALE_AFTER
    for row in db.scalars(select(SiteAnalysis).where(
        SiteAnalysis.tenant_id == tenant_id, SiteAnalysis.status.in_(ACTIVE), SiteAnalysis.created_at < cutoff
    )):
        row.status = DemoStatus.FAILED
        row.error = "The analysis was interrupted. Please run it again."


# ── The job ─────────────────────────────────────────────────────────────────
def run(analysis_id: str, tenant_id: str) -> None:
    from app.db.session import session_scope
    from app.llm.from_connector import provider_from_connector
    from app.services.connectors import build_connector_bundle

    with session_scope(tenant_id) as db:
        row = db.get(SiteAnalysis, analysis_id)
        org = db.get(Organization, tenant_id)
        if row is None or org is None:
            return
        row.status, row.started_at = DemoStatus.RUNNING, utcnow()
        db.commit()

        def step(key: str, state: str, detail: str = "") -> None:
            progress = [dict(s) for s in row.progress]
            for item in progress:
                if item["key"] == key:
                    item["state"], item["detail"] = state, detail[:200]
            row.progress = progress
            db.commit()

        bundle = build_connector_bundle(db, org)
        try:
            slug = (row.context.get("llm") or {}).get("slug", "")
            llm = provider_from_connector(slug, bundle.instances, names=bundle.names)
            speed = bundle.instances.get("pagespeed_insights")
            speed_key = None
            if speed is not None:
                try:
                    speed_key = speed.credentials.require("apiKey")
                except Exception:  # noqa: BLE001 - fall back to the platform key
                    speed_key = None
            row.context = {**row.context, "pagespeed": "Your PageSpeed Insights connection" if speed_key else "Platform"}
            row.report = service.build_report(
                row.website_url, row.business_url, step=step, llm=llm,
                model_label=row.model, pagespeed_key=speed_key, in_portal=True,
            )
            row.status = DemoStatus.DONE
        except AppError as exc:
            row.status, row.error = DemoStatus.FAILED, exc.message
        except Exception as exc:  # noqa: BLE001
            # Exception text can carry keys or vendor bodies; it is never stored.
            log.error("Site analysis %s failed (%s)", analysis_id, type(exc).__name__)
            row.status = DemoStatus.FAILED
            row.error = service.ANALYSIS_FAILED_MESSAGE
        finally:
            bundle.close()
        if row.status == DemoStatus.FAILED:
            row.progress = [
                {**s, "state": "failed"} if s.get("state") == "running" else dict(s) for s in row.progress
            ]
        row.finished_at = utcnow()
        if row.status == DemoStatus.DONE:
            from app.services.history import store

            store.guarded(db, "site_analysis", record_version, db, org, row)
        db.commit()


def record_version(db: Session, org: Organization, row: SiteAnalysis) -> None:
    """Keep a finished analysis as the next version of its website's history.

    Credited to the person who asked for it while they are still in the
    workspace, otherwise to the system. Read-only: analyses are not restorable.
    """
    from app.models.history import SnapshotKind
    from app.models.workspace import User
    from app.services import history
    from app.services.history import site_analysis

    try:
        requester = str(uuid.UUID(row.requested_by)) if row.requested_by else ""
    except ValueError:
        requester = ""  # not a person's id: credit the system
    actor = None
    if requester:
        actor = db.scalar(select(User).where(User.id == requester, User.tenant_id == org.id))
    history.record_version(
        db,
        org,
        SnapshotKind.SITE_ANALYSIS,
        site_analysis.subject_for(row.website_url),
        {
            "analysis_id": row.id,
            "website_url": row.website_url,
            "business_url": row.business_url,
            "report": row.report,
        },
        actor=actor,
        model=row.model or None,
    )


# ── Reading ─────────────────────────────────────────────────────────────────
def _site_key(url: str) -> str:
    return url.split("://", 1)[-1].lower().removeprefix("www.").rstrip("/")


def _score(row: SiteAnalysis) -> int | None:
    return ((row.report or {}).get("overall") or {}).get("score")


def history(db: Session, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    _expire_stale(db, tenant_id)
    rows = list(db.scalars(
        select(SiteAnalysis).where(SiteAnalysis.tenant_id == tenant_id)
        .order_by(SiteAnalysis.created_at.desc()).limit(limit)
    ))
    out = []
    for i, row in enumerate(rows):
        previous = next(
            (r for r in rows[i + 1:] if r.status == DemoStatus.DONE and _site_key(r.website_url) == _site_key(row.website_url)),
            None,
        )
        out.append(summary(row, previous))
    return out


def summary(row: SiteAnalysis, previous: SiteAnalysis | None = None) -> dict[str, Any]:
    report = row.report or {}
    score = _score(row)
    before = _score(previous) if previous is not None else None
    return {
        "id": row.id,
        "website": row.website_url,
        "business_url": row.business_url,
        "status": row.status,
        "error": row.error or None,
        "score": score,
        "grade": (report.get("overall") or {}).get("grade"),
        "change": score - before if score is not None and before is not None else None,
        "issues": ((report.get("summary") or {}).get("failed", 0) + (report.get("summary") or {}).get("warnings", 0)) if report else None,
        "model": row.model,
        "requested_by": row.requested_by_name,
        "connectors": len(row.context.get("connectors") or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "percent": 100 if row.status == DemoStatus.DONE else round(
            100 * sum(1 for s in row.progress if s.get("state") in ("done", "skipped")) / max(len(row.progress), 1)
        ),
    }


def get(db: Session, tenant_id: str, analysis_id: str) -> SiteAnalysis:
    row = db.get(SiteAnalysis, analysis_id)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError("Analysis not found")
    return row


def previous_of(db: Session, row: SiteAnalysis) -> SiteAnalysis | None:
    for candidate in db.scalars(
        select(SiteAnalysis).where(
            SiteAnalysis.tenant_id == row.tenant_id,
            SiteAnalysis.status == DemoStatus.DONE,
            SiteAnalysis.created_at < row.created_at,
            SiteAnalysis.id != row.id,
        ).order_by(SiteAnalysis.created_at.desc()).limit(50)
    ):
        if _site_key(candidate.website_url) == _site_key(row.website_url):
            return candidate
    return None


def compare(current: SiteAnalysis, previous: SiteAnalysis | None) -> dict[str, Any] | None:
    """What changed since the previous completed run of the same site."""
    if previous is None or not current.report or not previous.report:
        return None
    now, before = current.report, previous.report
    categories = {}
    for key, cat in (now.get("categories") or {}).items():
        old = ((before.get("categories") or {}).get(key) or {}).get("score")
        new = cat.get("score")
        categories[key] = {
            "label": cat.get("label", key),
            "before": old,
            "after": new,
            "change": new - old if new is not None and old is not None else None,
        }
    old_checks = {c["id"]: c for c in before.get("checks") or []}
    fixed, new_issues, still = [], [], []
    for check in now.get("checks") or []:
        prior = old_checks.get(check["id"])
        if check["status"] == "pass" and prior and prior["status"] in BAD:
            fixed.append({"title": check["title"], "before": prior["evidence"], "after": check["evidence"]})
        elif check["status"] in BAD and (prior is None or prior["status"] == "pass"):
            new_issues.append({"title": check["title"], "evidence": check["evidence"], "status": check["status"]})
        elif check["status"] in BAD and prior and prior["status"] in BAD:
            still.append(check["title"])
    old_conn = {c["slug"]: c["name"] for c in (previous.context.get("connectors") or [])}
    new_conn = {c["slug"]: c["name"] for c in (current.context.get("connectors") or [])}
    score_now, score_before = _score(current), _score(previous)
    return {
        "previous_id": previous.id,
        "previous_at": previous.created_at.isoformat() if previous.created_at else None,
        "overall": {
            "before": score_before,
            "after": score_now,
            "change": score_now - score_before if score_now is not None and score_before is not None else None,
        },
        "categories": categories,
        "fixed": fixed,
        "new_issues": new_issues,
        "still_open": still,
        "connectors_added": [new_conn[s] for s in new_conn if s not in old_conn],
        "connectors_removed": [old_conn[s] for s in old_conn if s not in new_conn],
    }


def detail(db: Session, row: SiteAnalysis) -> dict[str, Any]:
    previous = previous_of(db, row) if row.status == DemoStatus.DONE else None
    return {
        **summary(row, previous),
        "progress": row.progress,
        "report": row.report if row.status == DemoStatus.DONE else None,
        "context": row.context,
        "comparison": compare(row, previous),
    }
