"""Celery tasks, for running the fleet outside the API process.

The PRD calls for Celery + Redis to carry the crawling and outreach work, and
that is the right shape once agents outgrow one box. The in-process scheduler
stays the default so the platform runs with nothing but Postgres; setting
``CELERY_ENABLED=true`` turns the scheduler into a dispatcher and these tasks
do the work.

Import is safe without Celery installed: the decorators degrade to no-ops so
the API can start regardless.
"""
from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

try:
    from celery import Celery

    celery_app = Celery(
        "automarket",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
    )
    celery_app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        # An agent run is long and non-idempotent to repeat blindly: acking
        # late plus one-at-a-time prefetch means a crashed worker's task is
        # redelivered rather than lost, and no worker hoards a queue.
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=600,
        task_time_limit=900,
        broker_connection_retry_on_startup=True,
    )
    CELERY_AVAILABLE = True
except ImportError:  # pragma: no cover
    celery_app = None  # type: ignore[assignment]
    CELERY_AVAILABLE = False


def _task(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    """``celery_app.task`` when Celery is present, otherwise a no-op."""
    if CELERY_AVAILABLE:
        return celery_app.task(*args, **kwargs)

    def decorator(fn):  # noqa: ANN001, ANN202
        fn.delay = lambda *a, **k: fn(*a, **k)  # type: ignore[attr-defined]
        return fn

    return decorator


@_task(name="agents.run", bind=CELERY_AVAILABLE, max_retries=2)
def run_agent_task(self, tenant_id: str, slug: str) -> dict:  # noqa: ANN001
    """Run one agent for one organisation."""
    from sqlalchemy import select

    from app.db.session import session_scope
    from app.models.agent import AgentRecord
    from app.models.workspace import Organization
    from app.orchestration.runner import run_agent

    try:
        with session_scope(tenant_id) as db:
            record = db.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == tenant_id, AgentRecord.slug == slug
                )
            ).scalar_one_or_none()
            org = db.get(Organization, tenant_id)
            if record is None or org is None:
                return {"status": "skipped", "reason": "agent or workspace missing"}

            outcome = run_agent(db, record=record, org=org, trigger="schedule")
            return {
                "status": outcome.status,
                "summary": outcome.summary,
                "actions_taken": outcome.run.actions_taken,
                "actions_queued": outcome.run.actions_queued,
            }
    except Exception as exc:  # noqa: BLE001
        log.exception("Celery agent run failed for %s/%s", tenant_id, slug)
        if CELERY_AVAILABLE and self is not None:
            # Back off and retry twice; a transient connector outage is the
            # common cause and it usually clears.
            raise self.retry(exc=exc, countdown=60) from exc
        return {"status": "failed", "error": str(exc)}


@_task(name="agents.tick")
def tick_task() -> dict:
    """Claim and dispatch due agents — Celery beat's entry point."""
    from app.orchestration.scheduler import scheduler

    count = scheduler.tick()
    return {"dispatched": count}


@_task(name="metrics.rollup")
def rollup_task() -> dict:
    """Daily metric rollup across every organisation."""
    from app.orchestration.scheduler import rollup_daily_metrics

    rollup_daily_metrics()
    return {"status": "ok"}


@_task(name="approvals.expire")
def expire_approvals_task(older_than_days: int = 30) -> dict:
    """Close out approval items nobody has decided."""
    from sqlalchemy.orm import Session

    from app.db.session import SessionLocal, run_per_organization
    from app.services.approvals import expire_stale

    def expire(db: Session, tenant_id: str) -> int:
        return expire_stale(db, tenant_id=tenant_id, older_than_days=older_than_days)

    db = SessionLocal()
    try:
        # Each workspace is committed under its own tenant pin — see
        # `run_per_organization` for why a single commit at the end would
        # silently lose all but the last one's updates.
        counts = run_per_organization(db, expire, label="Expiring approvals")
    finally:
        db.close()
    return {"expired": sum(counts)}


if CELERY_AVAILABLE:
    # Beat schedule, used when the worker is started with `-B`.
    celery_app.conf.beat_schedule = {
        "agent-tick": {
            "task": "agents.tick",
            "schedule": float(settings.scheduler_tick_seconds),
        },
        "daily-rollup": {
            "task": "metrics.rollup",
            # 00:20 UTC, after the previous day has closed everywhere useful.
            "schedule": 86_400.0,
        },
        "expire-approvals": {
            "task": "approvals.expire",
            "schedule": 86_400.0,
        },
    }
