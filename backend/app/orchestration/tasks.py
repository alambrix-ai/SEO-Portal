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

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.core.config import settings
from app.core.logging import get_logger, log_failure

log = get_logger(__name__)

#: Redis key the beat container's healthcheck reads: the unix time of the
#: last heartbeat. It expires on its own, so a dead beat shows as a missing key.
BEAT_HEARTBEAT_KEY = "beat:hb"
BEAT_HEARTBEAT_TTL_SECONDS = 120
BEAT_HEARTBEAT_EVERY_SECONDS = 30.0
_REDIS_TIMEOUT_SECONDS = 5.0

#: The queue Creative Studio jobs and redraws run on. A dedicated worker
#: serves it (``celery worker -Q render -c $CELERY_RENDER_CONCURRENCY``), so a
#: browser never runs beside the general queues or in the API process.
RENDER_QUEUE = "render"

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
        # A task whose worker died is not re-run blindly; creative jobs guard
        # redelivery with their own row claim (pipeline.claim_job).
        task_reject_on_worker_lost=False,
        task_routes={"creative.*": {"queue": RENDER_QUEUE}},
        # Worker sizing from config. Recycling bounds a leak or a browser
        # that grew (the render worker overrides concurrency on its command).
        worker_concurrency=settings.celery_worker_concurrency,
        worker_max_tasks_per_child=settings.celery_max_tasks_per_child,
        worker_max_memory_per_child=settings.celery_max_memory_per_child_kb,
        broker_transport_options={"visibility_timeout": 3600},
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
        # Redacted, no traceback: exception text can carry vendor bodies or keys.
        log_failure(log, "agents.task_failed", exc, level=logging.ERROR, tenant=tenant_id, agent=slug)
        if CELERY_AVAILABLE and self is not None:
            # Back off and retry twice; a transient connector outage is the
            # common cause and it usually clears.
            raise self.retry(exc=exc, countdown=60) from exc
        from app.core.user_messages import public_error_message

        return {
            "status": "failed",
            "error": public_error_message(exc, fallback="This agent could not finish its run."),
        }


# ── Creative Studio ────────────────────────────────────────────────────────
#: Hard and soft limits from config; acks late, guarded by the job claim.
_RENDER_TASK_OPTIONS = {
    "queue": RENDER_QUEUE,
    "soft_time_limit": settings.creative_render_soft_time_limit,
    "time_limit": settings.creative_render_time_limit,
    "acks_late": True,
    "reject_on_worker_lost": False,
    # Model and image calls are billed: a failed job is never retried here.
    "max_retries": 0,
}


def _is_time_limit(exc: BaseException) -> bool:
    return type(exc).__name__ == "SoftTimeLimitExceeded"


@_task(name="creative.run_job", **_RENDER_TASK_OPTIONS)
def run_creative_job_task(tenant_id: str, job_id: str) -> dict:
    """Run one Creative Studio job (claimed idempotently by the pipeline)."""
    from app.services.creative import pipeline

    try:
        return pipeline.run_job(tenant_id, job_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the job, never re-raised raw
        code = "render_timeout" if _is_time_limit(exc) else "failed"
        log_failure(log, "creative.job_task_failed", exc, level=logging.ERROR, tenant=tenant_id, job=job_id)
        pipeline.fail_job(tenant_id, job_id, code)
        return {"status": "failed", "error_code": code}


@_task(name="creative.rerender", **_RENDER_TASK_OPTIONS)
def rerender_creative_task(tenant_id: str, asset_id: str) -> dict:
    """Redraw one creative from its saved text. No model call."""
    from app.services.creative import pipeline

    try:
        return pipeline.rerender_asset(tenant_id, asset_id)
    except Exception as exc:  # noqa: BLE001 - logged; the old preview stays
        log_failure(log, "creative.rerender_task_failed", exc, level=logging.ERROR, tenant=tenant_id, asset=asset_id)
        return {"status": "failed"}


#: How often stuck Creative Studio jobs are looked for.
CREATIVE_SWEEP_EVERY_SECONDS = 300.0


# Not "creative.*": that routes to the render queue, and this is a short
# database sweep that must not wait behind renders.
@_task(name="ops.creative_sweep", ignore_result=True)
def sweep_stale_creative_jobs_task() -> dict:
    """Fail Creative Studio jobs stuck queued or running, in every workspace.

    A worker that died mid-job leaves its row QUEUED or RUNNING for ever; the
    pipeline's sweep marks such jobs failed ("timed_out") and undoes their
    revision state. Each workspace runs under its own tenant pin. Counts only
    are logged. With Celery off the in-process scheduler runs the same sweep.
    """
    from app.orchestration.scheduler import sweep_stale_creative_jobs

    return {"swept": sweep_stale_creative_jobs()}


_local_lock = threading.Lock()
_local_executor: ThreadPoolExecutor | None = None


def _local() -> ThreadPoolExecutor:
    """Worker threads for creative work when Celery is off: never the request thread."""
    global _local_executor
    with _local_lock:
        if _local_executor is None:
            _local_executor = ThreadPoolExecutor(
                max_workers=settings.creative_job_max_concurrent, thread_name_prefix="creative-job"
            )
        return _local_executor


def _submit(task: object, fn: Callable[[str, str], dict], tenant_id: str, item_id: str) -> None:
    if CELERY_AVAILABLE and settings.celery_enabled:
        task.apply_async(args=[tenant_id, item_id], queue=RENDER_QUEUE)  # type: ignore[attr-defined]
        return
    _local().submit(fn, tenant_id, item_id)


def submit_creative_job(tenant_id: str, job_id: str) -> None:
    """Start a committed creative job on the render queue, or a worker thread."""
    _submit(run_creative_job_task, run_creative_job_task, tenant_id, job_id)


def submit_creative_rerender(tenant_id: str, asset_id: str) -> None:
    """Redraw a creative on the render queue, or a worker thread."""
    _submit(rerender_creative_task, rerender_creative_task, tenant_id, asset_id)


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


@_task(name="reports.weekly_autopilot")
def weekly_autopilot_report_task() -> dict:
    """Build and store the weekly digest for every organisation."""
    from sqlalchemy.orm import Session

    from app.db.session import SessionLocal, run_per_organization
    from app.models.workspace import Organization
    from app.services import weekly_report

    def build(db: Session, tenant_id: str) -> int:
        org = db.get(Organization, tenant_id)
        if org is None:
            return 0
        weekly_report.run_for_org(db, org=org, connectors=None)
        return 1

    db = SessionLocal()
    try:
        counts = run_per_organization(db, build, label="Weekly autopilot report")
    finally:
        db.close()
    return {"reports": sum(counts)}


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


@_task(name="retention.prune")
def prune_retention_task() -> dict:
    """Delete expired sign-in codes, tokens, demo requests and unused uploads."""
    from app.orchestration.scheduler import prune_retention

    return {"pruned": prune_retention()}


@_task(name="social.metrics")
def social_metrics_task() -> dict:
    """Read published social posts' latest figures, in every workspace."""
    from app.orchestration.scheduler import collect_social_metrics

    return {"posts_requested": collect_social_metrics()}


def _redis_url() -> str:
    """``REDIS_URL`` (``settings.redis_url``), else the Celery broker if it
    is Redis; "" when neither."""
    url = settings.redis_url.strip() or settings.celery_broker_url.strip()
    return url if url.startswith(("redis://", "rediss://")) else ""


@_task(name="ops.beat_heartbeat", ignore_result=True)
def beat_heartbeat_task() -> dict:
    """Prove beat is alive: SET beat:hb <unix time> EX 120.

    Beat only schedules; a worker runs this, so a stale key means beat *or*
    every worker is down. Skipped quietly when Celery or Redis is not set up.
    The URL can hold a password, so it is never logged.
    """
    if not (CELERY_AVAILABLE and settings.celery_enabled):
        return {"status": "skipped"}
    url = _redis_url()
    if not url:
        return {"status": "skipped"}
    try:
        import redis
    except ImportError:  # pragma: no cover - installed with Celery's Redis extra
        return {"status": "skipped"}

    try:
        client = redis.Redis.from_url(
            url,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        )
        try:
            client.set(BEAT_HEARTBEAT_KEY, str(int(time.time())), ex=BEAT_HEARTBEAT_TTL_SECONDS)
        finally:
            client.close()
    except (redis.RedisError, ValueError) as exc:
        # Type only: Redis errors can quote the URL, password included, and
        # log_failure's redaction does not mask URL credentials.
        log.warning("Beat heartbeat could not be written: %s", type(exc).__name__)
        return {"status": "failed"}
    return {"status": "ok"}


def _every_hours(hours: int) -> object:
    """A clock-anchored schedule every ``hours``.

    Under a day: every ``hours`` hours at :10. A day or more: daily at 00:10,
    or every N days (``hours`` rounded up to whole days) on the days of the
    month divisible from the 1st. Never a bare interval, which first fires a
    full period after every beat restart.
    """
    from celery.schedules import crontab

    if hours < 1:
        hours = 1
    if hours <= 23:
        return crontab(minute=10, hour=f"*/{hours}")
    days = -(-hours // 24)
    if days == 1:
        return crontab(minute=10, hour=0)
    return crontab(minute=10, hour=0, day_of_month=f"*/{min(days, 31)}")


if CELERY_AVAILABLE:
    from celery.schedules import crontab
    from celery.signals import worker_process_shutdown

    @worker_process_shutdown.connect
    def _close_renderer(**_kwargs: object) -> None:
        """Close this worker process's browser before it exits."""
        from app.services.creative import render

        render.shutdown()

    # Beat schedule, used by the beat service (or a worker started with `-B`).
    # Daily jobs are anchored to the clock: a bare 86,400-second interval
    # first fires a full day after every beat restart.
    celery_app.conf.beat_schedule = {
        "agent-tick": {
            "task": "agents.tick",
            "schedule": float(settings.scheduler_tick_seconds),
        },
        "daily-rollup": {
            "task": "metrics.rollup",
            # 00:20 UTC, after the previous day has closed everywhere useful.
            "schedule": crontab(minute=20, hour=0),
        },
        "weekly-autopilot-report": {
            "task": "reports.weekly_autopilot",
            # Monday 08:00 UTC.
            "schedule": crontab(minute=0, hour=8, day_of_week=1),
        },
        "expire-approvals": {
            "task": "approvals.expire",
            "schedule": crontab(minute=30, hour=0),
        },
        "retention-prune": {
            "task": "retention.prune",
            "schedule": crontab(minute=40, hour=3),
        },
        "social-metrics": {
            "task": "social.metrics",
            "schedule": _every_hours(settings.social_metrics_interval_hours),
        },
        "creative-sweep-stale": {
            "task": "ops.creative_sweep",
            "schedule": CREATIVE_SWEEP_EVERY_SECONDS,
            # A missed sweep is simply done by the next one; never queue a backlog.
            "options": {"expires": CREATIVE_SWEEP_EVERY_SECONDS},
        },
        "beat-heartbeat": {
            "task": "ops.beat_heartbeat",
            "schedule": BEAT_HEARTBEAT_EVERY_SECONDS,
            # A heartbeat nobody ran in time is worthless; never queue a backlog.
            "options": {"expires": BEAT_HEARTBEAT_EVERY_SECONDS},
        },
    }
