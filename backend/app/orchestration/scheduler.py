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
import itertools
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.db.session import SessionLocal, run_per_organization, session_scope
from app.models.agent import AgentRecord, AgentStatus
from app.models.workspace import Organization

log = get_logger(__name__)

#: Days the rollup re-reads on an ordinary day, so late-finalising sources
#: are picked up. Search Console lags about three days, so it gets more.
_RECENT_SESSION_DAYS = 3
_RECENT_SEARCH_DAYS = 7
#: The metric whose presence in ``metric_points`` says a source has been
#: rolled up before (and so needs no backfill).
_SESSIONS_MARKER = "sessions"
_SEARCH_MARKER = "clicks"
_AD_METRICS = ("spend", "clicks", "impressions", "conversions")
#: Minutes between in-process sweeps for stuck Creative Studio jobs (Celery
#: beat uses the same period: tasks.CREATIVE_SWEEP_EVERY_SECONDS).
_CREATIVE_SWEEP_MINUTES = 5
#: Minutes after start-up before the first in-process social metrics run.
_SOCIAL_METRICS_FIRST_DELAY_MINUTES = 10
#: Minutes after start-up before the first in-process retention prune.
_RETENTION_FIRST_DELAY_MINUTES = 15


class AgentScheduler:
    """Runs due agents on a fixed interval."""

    def __init__(self, *, tick_seconds: int | None = None) -> None:
        self.tick_seconds = tick_seconds or settings.scheduler_tick_seconds
        # None until the first tick, so startup is not spent probing vendors.
        self._next_health_sweep: datetime | None = None
        # The UTC date the daily housekeeping last ran for, in this process.
        self._housekeeping_day: date | None = None
        self._next_publish_check: datetime | None = None
        # None until the first tick; see _maybe_collect_social_metrics and
        # _maybe_prune_retention.
        self._next_social_metrics: datetime | None = None
        self._next_retention: datetime | None = None
        self._next_creative_sweep: datetime | None = None
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
        self._maybe_daily_housekeeping()
        self._maybe_prune_retention()
        self._maybe_collect_social_metrics()
        self._maybe_sweep_creative_jobs()
        if self._emergency_stop():
            # Nothing is claimed, run or published while it is on. Health
            # probes, housekeeping and metric collection above only read and
            # tidy.
            log.info("Emergency stop is on: the scheduler is skipping every agent")
            return 0
        self._maybe_publish_scheduled()
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

    @staticmethod
    def _emergency_stop() -> bool:
        """The platform's emergency stop, read fresh on every tick."""
        from app.services import agent_safety

        db = SessionLocal()
        try:
            return agent_safety.platform_switch(db, agent_safety.EMERGENCY_STOP)
        finally:
            db.close()

    def _maybe_publish_scheduled(self) -> None:
        """Every few minutes, publish approved content whose date has arrived."""
        now = utcnow()
        if self._next_publish_check is not None and now < self._next_publish_check:
            return
        self._next_publish_check = now + timedelta(minutes=5)
        try:
            publish_scheduled_assets()
        except Exception:  # noqa: BLE001 - must never stop the fleet
            log.exception("Scheduled publishing failed")

    def _maybe_daily_housekeeping(self) -> None:
        """Once a day: roll up recent metrics and expire stale approvals.

        Celery beat runs both when Celery is enabled. Without it they used to
        never run at all: approvals never expired and the Reports screen's
        sessions and spend stayed empty. Both are idempotent, so running them
        again after a restart changes nothing.
        """
        if settings.celery_enabled:
            return
        today = utcnow().date()
        if self._housekeeping_day == today:
            return
        self._housekeeping_day = today
        for job in (rollup_daily_metrics, expire_stale_approvals):
            try:
                job()
            except Exception:  # noqa: BLE001 - housekeeping must never stop the fleet
                log.exception("%s failed", job.__name__)

    def _maybe_prune_retention(self) -> None:
        """Once a day: delete expired codes, tokens, demo requests and orphans.

        Celery beat runs it when Celery is enabled. Like the metrics
        collection, the first run waits a few minutes after start-up: deleting
        is never the first thing a process does.
        """
        if settings.celery_enabled:
            return
        now = utcnow()
        if self._next_retention is None:
            self._next_retention = now + timedelta(minutes=_RETENTION_FIRST_DELAY_MINUTES)
            return
        if now < self._next_retention:
            return
        self._next_retention = now + timedelta(days=1)
        try:
            prune_retention()
        except Exception as exc:  # noqa: BLE001 - must never stop the fleet
            log_failure(log, "Retention pruning failed", exc)

    def _maybe_collect_social_metrics(self) -> None:
        """Every ``social_metrics_interval_hours``: read posted social figures.

        Celery beat runs it when Celery is enabled. The first collection waits
        a few minutes after start-up rather than a full interval, so a
        frequently restarted process still collects; it is not run at once,
        because some platforms (X) bill every read.
        """
        if settings.celery_enabled:
            return
        now = utcnow()
        if self._next_social_metrics is None:
            self._next_social_metrics = now + timedelta(minutes=_SOCIAL_METRICS_FIRST_DELAY_MINUTES)
            return
        if now < self._next_social_metrics:
            return
        self._next_social_metrics = now + timedelta(hours=settings.social_metrics_interval_hours)
        try:
            collect_social_metrics()
        except Exception as exc:  # noqa: BLE001 - must never stop the fleet
            log_failure(log, "Social metrics collection failed", exc)

    def _maybe_sweep_creative_jobs(self) -> None:
        """Every five minutes: fail Creative Studio jobs left queued or running.

        Only when Celery is off. When Celery is on, beat runs
        ``ops.creative_sweep`` instead, so the sweep never runs twice. Without
        Celery a job runs in a thread of this process, and a restart leaves
        its row stuck. The first tick sweeps too: jobs younger than the render
        time limit are never touched.
        """
        if settings.celery_enabled:
            return
        now = utcnow()
        if self._next_creative_sweep is not None and now < self._next_creative_sweep:
            return
        self._next_creative_sweep = now + timedelta(minutes=_CREATIVE_SWEEP_MINUTES)
        try:
            sweep_stale_creative_jobs()
        except Exception as exc:  # noqa: BLE001 - must never stop the fleet
            log_failure(log, "Creative job sweep failed", exc)

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
            from app.services import agent_safety

            org = db.get(Organization, tenant_id)
            if org is not None and agent_safety.workspace_stopped(org.settings):
                # "Stop all agents" pauses them all, so this finds nothing
                # anyway; checked so a row resumed by hand still waits.
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


def sweep_stale_creative_jobs() -> int:
    """Fail stuck Creative Studio jobs, in every workspace. Returns how many.

    ``pipeline.sweep_stale_jobs`` already walks each workspace under its own
    pin, so this calls it once. Wrapping that walk in ``run_per_organization``
    would visit every workspace once per workspace. Counts only are logged.
    """
    from app.services.creative import pipeline

    db = SessionLocal()
    try:
        swept = pipeline.sweep_stale_jobs(db)
        total = swept if isinstance(swept, int) and not isinstance(swept, bool) else 0
    finally:
        db.close()
    if total:
        log.info("Failed %d stuck creative job(s)", total)
    return total


def _mark_failing(db: Session, tenant_id: str, slug: str, exc: Exception) -> None:
    """Record a connector's failure where the customer can see it.

    A revoked or expired sign-in (OAuth ``invalid_grant``) cannot fix itself,
    so it is reported as needing reconnection rather than as a raw error.
    """
    from app.models.connector import ConnectorHealth, ConnectorRecord
    from app.services.connectors import public_connector_error

    # The raw text is only inspected, never stored or logged: it can carry
    # vendor bodies or keys.
    if "invalid_grant" in str(exc):
        reason = "Sign-in expired or was revoked — reconnect this integration"
    else:
        reason = public_connector_error(
            exc, fallback="This integration could not be read. Check the connection and try again."
        )
    record = db.execute(
        select(ConnectorRecord).where(ConnectorRecord.tenant_id == tenant_id, ConnectorRecord.slug == slug)
    ).scalar_one_or_none()
    if record is not None and (record.health != ConnectorHealth.FAILING.value or record.last_error != reason):
        log.warning(
            "Daily rollup: %s failed for workspace %s (%s): %s",
            slug, tenant_id, type(exc).__name__, reason,
        )
        record.health = ConnectorHealth.FAILING.value
        record.last_error = reason


#: Most daily rows taken from one source in one rollup (the backfill is at
#: most 366 days; this only guards against a misbehaving connector).
_MAX_DAILY_ROWS = 400


class SearchDailyReader(Protocol):
    """A search source that can report per-day totals (step 4.6 adds it).

    Each item has ``day`` (a date, or an ISO ``YYYY-MM-DD`` string),
    ``clicks``, ``impressions`` and ``position`` (average; may be ``None``),
    as attributes or mapping keys.
    """

    slug: str

    def read_search_daily(self, *, days: int) -> Iterable[Any]: ...


def _days_to_read(db: Session, *, tenant_id: str, source: str, marker: str, recent: int) -> int:
    """``recent``, or the configured backfill the first time ``source`` appears.

    A source has appeared once ``metric_points`` holds its ``marker`` metric
    for this workspace. A backfill that fails writes nothing, so it is simply
    tried again the next day.
    """
    from app.models.social import MetricPoint

    if settings.metrics_backfill_days <= recent:
        return recent
    seen = db.scalar(
        select(MetricPoint.id)
        .where(
            MetricPoint.tenant_id == tenant_id,
            MetricPoint.source == source,
            MetricPoint.metric == marker,
        )
        .limit(1)
    )
    return recent if seen is not None else settings.metrics_backfill_days


def _number(value: Any) -> float | None:  # noqa: ANN401
    """``value`` as a finite non-negative number, or ``None``. Connector data is untrusted."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _field(item: Any, name: str) -> Any:  # noqa: ANN401
    return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)


def _as_day(value: Any) -> date | None:  # noqa: ANN401
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _rollup_search(db: Session, tenant_id: str, bundle: Any) -> None:  # noqa: ANN401
    """Search Console daily clicks, impressions and position, as metric points.

    Kept apart from sessions on purpose: a click from search is not a visit
    measured by analytics, and counting it as one would inflate Reports.
    """
    from app.connectors.base.connector import Capability
    from app.services import social_metrics
    from app.services.social_metrics import Point

    for source in bundle.with_capability(Capability.READ_SEARCH_PERFORMANCE):
        reader = getattr(source, "read_search_daily", None)
        if not callable(reader):
            continue
        try:
            days = _days_to_read(
                db, tenant_id=tenant_id, source=source.slug,
                marker=_SEARCH_MARKER, recent=_RECENT_SEARCH_DAYS,
            )
            points: list[Point] = []
            for item in itertools.islice(reader(days=days) or (), _MAX_DAILY_ROWS):
                day = _as_day(_field(item, "day"))
                if day is None:
                    continue
                for metric in ("clicks", "impressions", "position"):
                    value = _number(_field(item, metric))
                    if value is not None:
                        points.append(Point(day=day, source=source.slug, metric=metric, value=value))
            social_metrics.record_points(db, tenant_id=tenant_id, points=points)
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rollup
            _mark_failing(db, tenant_id, source.slug, exc)


def _first_website_analytics(bundle: Any) -> Any:  # noqa: ANN401 - a ConnectorBundle
    """The first connected READ_SESSIONS source that measures website visits."""
    from app.connectors.base.connector import Capability
    from app.connectors.base.registry import connector_slugs
    from app.services.metrics import NOT_WEBSITE_VISITS

    for slug in connector_slugs():
        if slug in NOT_WEBSITE_VISITS:
            continue
        connector = bundle.instances.get(slug)
        if connector is not None and connector.supports(Capability.READ_SESSIONS):
            return connector
    return None


def _rollup_local(db: Session, tenant_id: str, bundle: Any) -> None:  # noqa: ANN401
    """Business Profile's local discovery impressions, as metric points.

    Its ``read_sessions`` series is impressions on Google Search and Maps, not
    visits to the website, so it is recorded as ``local_impressions`` and
    never as sessions or ``daily_metrics.organic_sessions``.
    """
    from app.connectors.base.connector import Capability
    from app.services import social_metrics
    from app.services.metrics import LOCAL_IMPRESSIONS, NOT_WEBSITE_VISITS
    from app.services.social_metrics import Point

    for source in bundle.with_capability(Capability.READ_SESSIONS):
        if source.slug not in NOT_WEBSITE_VISITS:
            continue
        try:
            days = _days_to_read(
                db, tenant_id=tenant_id, source=source.slug,
                marker=LOCAL_IMPRESSIONS, recent=_RECENT_SESSION_DAYS,
            )
            points: list[Point] = []
            for row in itertools.islice(source.read_sessions(days=days) or (), _MAX_DAILY_ROWS):
                day = _as_day(getattr(row, "day", None))
                value = _number(getattr(row, "sessions", None))
                if day is not None and value is not None:
                    points.append(Point(day, source.slug, LOCAL_IMPRESSIONS, value))
            social_metrics.record_points(db, tenant_id=tenant_id, points=points)
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rollup
            _mark_failing(db, tenant_id, source.slug, exc)


def rollup_daily_metrics() -> None:
    """Record measured sessions, conversions, ad, search and local figures.

    These come from connectors rather than agents. Each day's figure is set
    (see ``metrics.record_snapshot`` and ``social_metrics.record_points``), so
    the rollup is idempotent and a late correction from the source simply
    overwrites the earlier number.

    An ordinary day re-reads the last few days. The first time a source
    appears it reads ``metrics_backfill_days`` instead, so a newly connected
    workspace's 30- and 90-day ranges are not empty for months.

    Ad spend is the platforms' own figure for yesterday. It used to be the sum
    of the budget engine's trailing 7-day spend, added again every day, which
    is about seven times the real amount.
    """
    from app.connectors.base.connector import Capability
    from app.services import metrics, social_metrics
    from app.services.connectors import build_connector_bundle
    from app.services.social_metrics import Point

    def rollup(db: Session, tenant_id: str) -> None:
        org = db.get(Organization, tenant_id)
        if org is None:
            return
        bundle = build_connector_bundle(db, org)
        try:
            # Website visits come from the first analytics source in catalogue
            # order. Business Profile also reads "sessions", but they are local
            # impressions, so it is never picked here (see _rollup_local).
            analytics = _first_website_analytics(bundle)
            if analytics is not None:
                try:
                    days = _days_to_read(
                        db, tenant_id=tenant_id, source=analytics.slug,
                        marker=_SESSIONS_MARKER, recent=_RECENT_SESSION_DAYS,
                    )
                    points: list[Point] = []
                    rows = analytics.read_sessions(days=days)  # type: ignore[union-attr]
                    for row in itertools.islice(rows, _MAX_DAILY_ROWS):
                        day = _as_day(_field(row, "day"))
                        if day is None:
                            continue
                        sessions = _field(row, "sessions")
                        conversions = _field(row, "conversions")
                        metrics.record_snapshot(
                            db,
                            tenant_id=tenant_id,
                            day=day,
                            values={"organic_sessions": sessions, "conversions": conversions},
                        )
                        points.append(Point(day, analytics.slug, _SESSIONS_MARKER, sessions))
                        points.append(Point(day, analytics.slug, "conversions", conversions))
                    social_metrics.record_points(db, tenant_id=tenant_id, points=points)
                except Exception as exc:  # noqa: BLE001 - analytics must not stop the spend rollup
                    _mark_failing(db, tenant_id, analytics.slug, exc)

            _rollup_local(db, tenant_id, bundle)
            _rollup_search(db, tenant_id, bundle)

            yesterday = utcnow().date() - timedelta(days=1)
            spend = 0.0
            measured = False
            ad_points: list[Point] = []
            for platform in bundle.with_capability(Capability.READ_AD_PERFORMANCE):
                try:
                    performance = platform.read_performance(days=1)  # type: ignore[union-attr]
                    spend += performance.spend
                    measured = True
                    for metric in _AD_METRICS:
                        value = _number(getattr(performance, metric, None))
                        if value is not None:
                            ad_points.append(Point(yesterday, platform.slug, metric, value))
                except Exception as exc:  # noqa: BLE001 - one platform must not hide the rest
                    _mark_failing(db, tenant_id, platform.slug, exc)
            if measured:
                metrics.record_snapshot(
                    db,
                    tenant_id=tenant_id,
                    day=yesterday,
                    values={"ad_spend": round(spend, 2)},
                )
            social_metrics.record_points(db, tenant_id=tenant_id, points=ad_points)
        finally:
            bundle.close()

    db = SessionLocal()
    try:
        done = run_per_organization(db, rollup, label="Daily rollup")
        log.info("Daily metrics rolled up for %d workspace(s)", len(done))
    finally:
        db.close()


def expire_stale_approvals(older_than_days: int = 30) -> None:
    """Close out approval items nobody has decided, in every workspace."""
    from app.services.approvals import expire_stale

    def expire(db: Session, tenant_id: str) -> int:
        return expire_stale(db, tenant_id=tenant_id, older_than_days=older_than_days)

    db = SessionLocal()
    try:
        counts = run_per_organization(db, expire, label="Expiring approvals")
    finally:
        db.close()
    expired = sum(counts)
    if expired:
        log.info("Expired %d stale approval(s)", expired)


def publish_scheduled_assets() -> None:
    """Publish scheduled content that is due, in every workspace."""
    from app.services import agent_safety, assets

    def publish(db: Session, tenant_id: str) -> int:
        org = db.get(Organization, tenant_id)
        if org is None or agent_safety.workspace_stopped(org.settings):
            # Held while "Stop all agents" is on; published after Resume.
            return 0
        return assets.publish_due(db, org)

    db = SessionLocal()
    try:
        counts = run_per_organization(db, publish, label="Scheduled publishing")
    finally:
        db.close()
    published = sum(counts)
    if published:
        log.info("Published %d scheduled item(s)", published)


def collect_social_metrics() -> int:
    """Read published social posts' figures, in every workspace.

    Returns how many post ids were sent to the platforms (what X bills for).
    """
    from app.services import social_metrics

    def collect(db: Session, tenant_id: str) -> int:
        org = db.get(Organization, tenant_id)
        if org is None:
            return 0
        return social_metrics.collect(db, org).posts_requested

    db = SessionLocal()
    try:
        counts = run_per_organization(db, collect, label="Social metrics")
    finally:
        db.close()
    total = sum(counts)
    if total:
        log.info("Read figures for %d social post(s)", total)
    return total


def prune_retention() -> dict[str, int]:
    """Delete expired sign-in codes, tokens, demo requests and unused uploads.

    The first three tables are not tenant-scoped, so they are pruned once,
    unpinned. Uploads are pruned per workspace under its own pin. Counts only
    are logged.
    """
    from app.services import retention

    with session_scope() as db:
        counts = retention.prune_expired(db)

    def prune(db: Session, tenant_id: str) -> tuple[int, int]:
        result = retention.prune_orphan_uploads(db, tenant_id=tenant_id)
        return result.files, result.failed

    db = SessionLocal()
    try:
        uploads = run_per_organization(db, prune, label="Pruning unused uploads")
    finally:
        db.close()
    counts["unused_uploads"] = sum(files for files, _ in uploads)
    failed = sum(failures for _, failures in uploads)
    log.info(
        "Retention pruned %s",
        ", ".join(f"{table}={count}" for table, count in counts.items()),
    )
    if failed:
        log.warning("Retention could not remove %d unused upload(s)", failed)
    return counts
