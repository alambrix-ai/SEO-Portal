"""In-process agent scheduler.

Every tick it asks one question — *which agents are due?* — and runs them.
Deliberately simple and dependency-free, because the interesting behaviour
lives in the agents and the guardrail policy, not in the timer.

Two properties matter:

* **Claim-before-run.** An agent's ``next_run_at`` is pushed forward inside
  the claiming transaction, so two workers (or a worker and a manual "Run
  now") cannot execute the same agent concurrently.
* **Isolation.** Each agent runs in its own transaction. One failing agent
  rolls back only its own work and never stops the fleet.

For horizontal scaling, set ``CELERY_ENABLED=true`` and run the Celery worker
in ``app.orchestration.tasks`` instead; this scheduler then only dispatches.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import SessionLocal, run_per_organization, session_scope
from app.models.agent import AgentRecord, AgentStatus
from app.models.workspace import Organization

log = get_logger(__name__)

# Agents claimed per tick in total, and per workspace, so one busy
# organisation cannot monopolise a tick.

# How long a claim is assumed to last; a crashed run becomes due again after.



class AgentScheduler:
    """Runs due agents on a fixed interval."""

    def __init__(self, *, tick_seconds: int | None = None) -> None:
        self.tick_seconds = tick_seconds or settings.scheduler_tick_seconds
        # None until the first tick, so startup is not spent probing vendors.
        self._next_health_sweep: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="agent-scheduler")
        log.info("Agent scheduler started (tick %ds)", self.tick_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("Agent scheduler stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Loop ───────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        # A short initial delay lets the app finish starting before the first
        # batch of database work.
        await asyncio.sleep(2)
        while not self._stopping.is_set():
            try:
                # The run path is synchronous (SQLAlchemy + httpx), so it goes
                # to a worker thread rather than blocking the event loop.
                count = await asyncio.to_thread(self.tick)
                if count:
                    log.info("Scheduler tick ran %d agent(s)", count)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("Scheduler tick failed")

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)

    # ── One tick ───────────────────────────────────────────────────────────
    def tick(self) -> int:
        """Claim and run every due agent. Returns how many ran."""
        self._maybe_sweep_health()
        claims = self._claim_due()
        if not claims:
            return 0

        if settings.celery_enabled:
            return self._dispatch_to_celery(claims)

        ran = 0
        for tenant_id, slug in claims:
            if self._run_one(tenant_id, slug):
                ran += 1
        return ran

    def _maybe_sweep_health(self) -> None:
        """Re-probe connectors on their own slower clock.

        Not every tick: a probe is a network call per connected integration
        per workspace, and a token does not get revoked every thirty seconds.
        On its own interval so the Connectors screen is current without a
        human pressing anything — which is what lets that screen have no Test
        button.
        """
        now = utcnow()
        if self._next_health_sweep is not None and now < self._next_health_sweep:
            return
        first_pass = self._next_health_sweep is None
        self._next_health_sweep = now + timedelta(
            minutes=settings.connector_health_sweep_minutes
        )
        if first_pass:
            # Skipped on startup: the process has just come up and the first
            # tick has agents to run.
            return
        try:
            sweep_connector_health()
        except Exception:  # noqa: BLE001 - a sweep must never stop the fleet
            log.exception("Connector health sweep failed")

    def _claim_due(self) -> list[tuple[str, str]]:
        """Atomically claim due agents by pushing their next run forward.

        The scheduler is the one cross-tenant process in the system, and
        ``agents`` is under row-level security — so it cannot simply select
        every due agent: with no organisation pinned, that query correctly
        returns nothing. Instead it walks the active organisations (the
        ``organizations`` table is the tenant itself and not scoped), pins
        each one, and claims that organisation's due agents inside its own
        boundary. RLS therefore stays fully enforced, with no bypass role and
        no exception table.

        ``SKIP LOCKED`` means several schedulers can claim disjoint sets
        without blocking each other, and the per-organisation budget stops one
        busy workspace from consuming a whole tick.

        :func:`run_per_organization` owns the part that is easy to get wrong:
        each organisation's claim is committed while its own pin is still
        active, because a deferred flush would write one tenant's rows under
        another's pin and row-level security would reject them.
        """
        now = utcnow()
        # Counted as each batch is built rather than after the fact, so the
        # total cap and the per-tenant budget both see the real running total.
        # A tenant whose commit then fails leaves this slightly high, which
        # only makes the tick claim less — never more.
        counter = {"claimed": 0}

        def claim(db: Session, tenant_id: str) -> list[tuple[str, str]]:
            budget = min(
                settings.scheduler_max_per_tick - counter["claimed"],
                settings.scheduler_max_per_tenant_per_tick,
            )
            if budget <= 0:
                return []
            rows = db.execute(
                select(AgentRecord)
                .where(
                    AgentRecord.tenant_id == tenant_id,
                    AgentRecord.status == AgentStatus.RUNNING.value,
                    AgentRecord.next_run_at.is_not(None),
                    AgentRecord.next_run_at <= now,
                )
                .order_by(AgentRecord.next_run_at)
                .limit(budget)
                .with_for_update(skip_locked=True)
            ).scalars()

            batch: list[tuple[str, str]] = []
            from app.models.portal import FeatureKind
            from app.services import portal_features

            for record in rows:
                if not portal_features.is_enabled(db, FeatureKind.AGENT, record.slug):
                    continue
                # Hold the lease so a crash mid-run does not leave the agent
                # permanently due and looping.
                record.next_run_at = now + timedelta(minutes=settings.scheduler_claim_lease_minutes)
                batch.append((record.tenant_id, record.slug))
            counter["claimed"] += len(batch)
            return batch

        db = SessionLocal()
        try:
            batches = run_per_organization(
                db,
                claim,
                should_stop=lambda: counter["claimed"] >= settings.scheduler_max_per_tick,
                label="Claiming due agents",
            )
        finally:
            db.close()

        # Only committed batches count: a tenant that failed is rolled back
        # and its agents stay due for the next tick.
        claimed = [entry for batch in batches for entry in batch]
        if claimed:
            log.debug("Claimed %d due agent(s)", len(claimed))
        return claimed

    def _run_one(self, tenant_id: str, slug: str) -> bool:
        """Run one agent in its own transaction, pinned to its organisation."""
        from app.orchestration.runner import run_agent

        try:
            with session_scope(tenant_id) as db:
                record = db.execute(
                    select(AgentRecord).where(
                        AgentRecord.tenant_id == tenant_id, AgentRecord.slug == slug
                    )
                ).scalar_one_or_none()
                org = db.get(Organization, tenant_id)
                if record is None or org is None or not org.is_active:
                    return False
                run_agent(db, record=record, org=org, trigger="schedule")
            return True
        except Exception:  # noqa: BLE001 - one agent must not stop the fleet
            log.exception("Scheduled run of %s/%s failed", tenant_id, slug)
            return False

    def _dispatch_to_celery(self, claims: list[tuple[str, str]]) -> int:
        from app.orchestration.tasks import run_agent_task

        for tenant_id, slug in claims:
            run_agent_task.delay(tenant_id, slug)
        log.info("Dispatched %d agent run(s) to Celery", len(claims))
        return len(claims)


scheduler = AgentScheduler()


def sweep_connector_health() -> None:
    """Re-probe every connected integration, across every workspace.

    The reason the Connectors screen has no Test button: a health state that
    only updates when somebody remembers to press something is a health state
    nobody trusts. Credentials are verified when they are submitted, and kept
    honest from here — a token revoked at the vendor shows up as "needs
    attention" on its own, which is when it matters.

    One workspace's unreachable vendor must not stop the sweep, which is what
    run_per_organization already guarantees per tenant; within a tenant each
    connector is probed independently for the same reason.
    """
    from app.services import connectors as connector_service

    def sweep(db: Session, tenant_id: str) -> int:
        checked = 0
        org = db.get(Organization, tenant_id)
        if org is None:
            return 0
        for record in connector_service.list_records(db, tenant_id=tenant_id):
            if not record.connected:
                continue
            try:
                connector_service.check_health(db, org=org, slug=record.slug)
                checked += 1
            except Exception:  # noqa: BLE001 - one vendor must not stop the rest
                log.exception("Health probe for %s failed", record.slug)
        return checked

    db = SessionLocal()
    try:
        counts = run_per_organization(db, sweep, label="Connector health sweep")
    finally:
        db.close()
    total = sum(counts)
    if total:
        log.info("Probed %d connected integration(s)", total)


def rollup_daily_metrics() -> None:
    """Fold yesterday's live counters into the metric table.

    Session and spend totals come from connectors rather than agents, so they
    are pulled once a day instead of on every agent run.
    """
    from app.connectors.base.connector import Capability
    from app.models.ads import BudgetAllocation
    from app.services import metrics
    from app.services.connectors import build_connector_bundle

    def rollup(db: Session, tenant_id: str) -> None:
        org = db.get(Organization, tenant_id)
        if org is None:
            return
        bundle = build_connector_bundle(db, org)
        try:
            analytics = bundle.first_with_capability(Capability.READ_SESSIONS)
            if analytics is not None:
                for row in analytics.read_sessions(days=1):  # type: ignore[union-attr]
                    metrics.accumulate(
                        db,
                        tenant_id=tenant_id,
                        day=row.day,
                        values={
                            "organic_sessions": row.sessions,
                            "conversions": row.conversions,
                        },
                    )

            spend = sum(
                r.spend
                for r in db.execute(
                    select(BudgetAllocation).where(
                        BudgetAllocation.tenant_id == tenant_id
                    )
                ).scalars()
            )
            if spend:
                metrics.accumulate(
                    db, tenant_id=tenant_id, values={"ad_spend": round(spend, 2)}
                )
        finally:
            bundle.close()

    db = SessionLocal()
    try:
        done = run_per_organization(db, rollup, label="Daily rollup")
        log.info("Daily metrics rolled up for %d workspace(s)", len(done))
    finally:
        db.close()
