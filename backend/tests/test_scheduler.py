"""Scheduler tests.

The scheduler is the one cross-tenant process in the system, which makes it
the one place row-level security and autonomous execution can quietly conflict:
a query with no organisation pinned correctly returns nothing, so the fleet
silently never runs. These tests pin that down.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.base import utcnow
from app.db.session import bind_tenant
from app.models.agent import AgentRecord, AgentRun, AgentStatus
from tests.conftest import requires_db

pytestmark = requires_db


def _make_due(db, tenant_id: str, slugs: list[str]) -> None:  # noqa: ANN001
    bind_tenant(db, tenant_id)
    now = utcnow()
    for slug in slugs:
        record = db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == tenant_id, AgentRecord.slug == slug
            )
        ).scalar_one()
        record.status = AgentStatus.RUNNING.value
        record.next_run_at = now - timedelta(minutes=1)
    db.commit()


def test_scheduler_claims_across_tenants_under_rls(clean_db, org):  # noqa: ANN001
    """A tick must find due agents even though `agents` is RLS-protected.

    This is the regression: the scheduler cannot select every due agent
    globally, because with no organisation pinned RLS returns an empty set —
    and the fleet would never run at all.
    """
    from app.orchestration.scheduler import AgentScheduler

    _make_due(clean_db, org.id, ["referral_spam_guard", "click_fraud_controller"])

    claimed = AgentScheduler()._claim_due()
    claimed_slugs = {slug for _tenant, slug in claimed}

    assert "referral_spam_guard" in claimed_slugs
    assert "click_fraud_controller" in claimed_slugs
    assert all(tenant == org.id for tenant, _slug in claimed)


def test_claiming_pushes_the_next_run_forward(clean_db, org):  # noqa: ANN001
    """A claimed agent must not be claimed again by a second scheduler."""
    from app.orchestration.scheduler import AgentScheduler

    _make_due(clean_db, org.id, ["referral_spam_guard"])
    scheduler = AgentScheduler()

    first = scheduler._claim_due()
    assert any(slug == "referral_spam_guard" for _t, slug in first)

    # A second claim, before anything has run, finds nothing: the lease holds.
    second = scheduler._claim_due()
    assert not any(slug == "referral_spam_guard" for _t, slug in second)


def test_paused_agents_are_never_claimed(clean_db, org):  # noqa: ANN001
    from app.orchestration.scheduler import AgentScheduler

    bind_tenant(clean_db, org.id)
    record = clean_db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "referral_spam_guard"
        )
    ).scalar_one()
    record.status = AgentStatus.PAUSED.value
    record.next_run_at = utcnow() - timedelta(minutes=5)
    clean_db.commit()

    claimed = AgentScheduler()._claim_due()
    assert not any(slug == "referral_spam_guard" for _t, slug in claimed)


def test_tick_runs_agents_and_records_them(clean_db, org):  # noqa: ANN001
    """A tick must produce real, recorded runs — not just claims."""
    from app.orchestration.scheduler import AgentScheduler

    _make_due(clean_db, org.id, ["referral_spam_guard", "click_fraud_controller"])

    ran = AgentScheduler().tick()
    assert ran >= 1

    bind_tenant(clean_db, org.id)
    recorded = clean_db.execute(
        select(func.count()).select_from(AgentRun).where(AgentRun.tenant_id == org.id)
    ).scalar_one()
    assert recorded >= 1

    # And every run reached a terminal state rather than being left running.
    statuses = set(
        clean_db.execute(
            select(AgentRun.status).where(AgentRun.tenant_id == org.id)
        ).scalars()
    )
    assert statuses
    assert "running" not in statuses


def test_per_tenant_budget_is_respected(clean_db, org):  # noqa: ANN001
    """One busy workspace must not consume an entire tick."""
    from app.orchestration import scheduler as scheduler_module
    from app.orchestration.scheduler import AgentScheduler

    bind_tenant(clean_db, org.id)
    now = utcnow()
    for record in clean_db.execute(
        select(AgentRecord).where(AgentRecord.tenant_id == org.id)
    ).scalars():
        record.status = AgentStatus.RUNNING.value
        record.next_run_at = now - timedelta(minutes=1)
    clean_db.commit()

    claimed = AgentScheduler()._claim_due()
    from app.core.config import settings

    assert len(claimed) <= settings.scheduler_max_per_tenant_per_tick


def test_claims_persist_for_every_tenant_not_just_the_last(clean_db, org, second_org):  # noqa: ANN001
    """The bug this exists for: claims must be committed per organisation.

    A loop that pins each tenant and defers to one commit at the end flushes
    every tenant's dirty rows under whichever pin was set last. Row-level
    security then rejects all the others: earlier tenants' claims silently
    vanish and the flush fails on the row-count mismatch. With one workspace
    it looks fine, which is exactly why it shipped.
    """
    from app.orchestration.scheduler import AgentScheduler

    _make_due(clean_db, org.id, ["referral_spam_guard", "click_fraud_controller"])
    _make_due(clean_db, second_org.id, ["referral_spam_guard", "click_fraud_controller"])

    claimed = AgentScheduler()._claim_due()

    # Both workspaces are represented, not just the last one pinned.
    assert {tenant for tenant, _slug in claimed} == {org.id, second_org.id}

    # And every claim was actually written: re-reading each tenant's rows must
    # show the lease pushed into the future, or a second scheduler would run
    # the same agents again.
    now = utcnow()
    for tenant_id in (org.id, second_org.id):
        bind_tenant(clean_db, tenant_id)
        clean_db.expire_all()
        for slug in ("referral_spam_guard", "click_fraud_controller"):
            record = clean_db.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == tenant_id, AgentRecord.slug == slug
                )
            ).scalar_one()
            assert record.next_run_at > now, f"{tenant_id}/{slug} was not claimed"


def test_one_failing_tenant_does_not_block_the_others(clean_db, org, second_org):  # noqa: ANN001
    """A workspace that errors must be isolated, not fatal to the tick."""
    from app.db.session import run_per_organization

    # Captured up front: the helper clears the identity map between tenants,
    # so ORM instances held across the call are detached afterwards.
    first_id, second_id = org.id, second_org.id

    _make_due(clean_db, first_id, ["referral_spam_guard"])
    _make_due(clean_db, second_id, ["referral_spam_guard"])

    seen: list[str] = []

    def handler(db, tenant_id: str) -> str:  # noqa: ANN001
        seen.append(tenant_id)
        if tenant_id == first_id:
            raise RuntimeError("simulated failure in one workspace")
        return tenant_id

    results = run_per_organization(clean_db, handler, label="test")

    # Both were attempted; only the healthy one produced a result.
    assert set(seen) == {first_id, second_id}
    assert results == [second_id]


def test_rollup_runs_for_every_tenant(clean_db, org, second_org):  # noqa: ANN001
    """The rollup shares the same cross-tenant pattern, so it gets the same check."""
    from app.orchestration.scheduler import rollup_daily_metrics

    # No connectors are configured, so this does no work — the point is that
    # it completes for both workspaces without an RLS or stale-data failure.
    rollup_daily_metrics()


def test_catalogue_sync_inserts_under_each_tenants_pin(clean_db, org, second_org):  # noqa: ANN001
    """Back-filling the catalogue writes into RLS tables, so it must be pinned.

    With no organisation pinned the policy's WITH CHECK rejects the insert
    outright, which made `manage.py sync-catalog` fail on any workspace.
    """
    from app.models.agent import AgentRecord as Record
    from app.services.provisioning import sync_all_organizations

    tenant_ids = (org.id, second_org.id)

    # Remove an agent from each workspace so the sync has something to add.
    for tenant_id in tenant_ids:
        bind_tenant(clean_db, tenant_id)
        record = clean_db.execute(
            select(Record).where(
                Record.tenant_id == tenant_id, Record.slug == "click_fraud_controller"
            )
        ).scalar_one()
        clean_db.delete(record)
        clean_db.commit()

    totals = sync_all_organizations(clean_db)
    assert totals["agents_added"] == 2, totals

    for tenant_id in tenant_ids:
        bind_tenant(clean_db, tenant_id)
        restored = clean_db.execute(
            select(Record).where(
                Record.tenant_id == tenant_id, Record.slug == "click_fraud_controller"
            )
        ).scalar_one_or_none()
        assert restored is not None, f"{tenant_id} was not back-filled"


def test_schedule_intervals_cover_every_offered_choice():
    """Every schedule the Configure dialog offers must map to an interval.

    A missing entry would silently fall back to the agent's default, so an
    operator's choice would appear saved but do nothing.
    """
    from app.orchestration.runner import SCHEDULE_INTERVALS
    from app.services.views import AGENT_SCHEDULES

    for label in AGENT_SCHEDULES:
        assert label in SCHEDULE_INTERVALS, label
        assert SCHEDULE_INTERVALS[label] > timedelta(0)


# ── Run now, and what a refresh cannot interrupt ───────────────────────────
@requires_db
def test_run_now_returns_before_the_pass_finishes(client, clean_db):  # noqa: ANN001
    """It reports that the run started, not what it found.

    The pass used to happen inside the request. Analysing six pages is six
    model calls, so the request could be held open for minutes — long enough
    that refreshing the tab looked like a cancellation and a proxy timeout
    looked like a failure, for a run that was completing on the server
    regardless.
    """
    from tests.test_api_flows import auth, configure, register

    token = register(client)["tokens"]["access_token"]
    configure(client, token, "on_page_seo_sync")

    response = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert response.status_code == 200, response.text
    message = response.json()["toast"]["message"]
    assert "started" in message.lower()
    # It says where the answer will be, because the response cannot carry it.
    assert "server" in message.lower()

    # And the pass genuinely happened: the run row is there with its reason.
    runs = client.get("/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)).json()
    assert runs
    assert runs[0]["summary"]


@requires_db
def test_a_second_run_now_does_not_start_a_second_pass(client, clean_db):  # noqa: ANN001
    """``force=True`` skips the scheduler's claim, which was the only thing
    stopping an agent running twice at once.

    Two clicks meant two passes: two lots of model spend, and under full
    autonomy every action applied twice.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.base import utcnow
    from app.db.session import bind_tenant
    from app.models.agent import AgentRun, RunStatus
    from tests.test_api_flows import auth, configure, register

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    configure(client, token, "on_page_seo_sync")
    bind_tenant(clean_db, org_id)

    # A pass that is still in flight, as one holding a model call would be.
    clean_db.add(
        AgentRun(
            tenant_id=org_id,
            agent_slug="on_page_seo_sync",
            trigger="manual",
            status=RunStatus.RUNNING.value,
            started_at=utcnow() - timedelta(seconds=5),
        )
    )
    clean_db.commit()

    response = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert response.status_code == 200
    assert "already running" in response.json()["toast"]["message"].lower()

    # Nothing new was started.
    rows = clean_db.execute(
        select(AgentRun).where(
            AgentRun.tenant_id == org_id, AgentRun.agent_slug == "on_page_seo_sync"
        )
    ).scalars()
    assert len([r for r in rows]) == 1


@requires_db
def test_a_run_abandoned_by_a_crash_does_not_wedge_the_button(client, clean_db):  # noqa: ANN001
    """The guard is bounded by the scheduler's own lease.

    A process killed mid-pass leaves a row saying RUNNING for ever. If that
    blocked Run now permanently, a crash would take the agent out of service
    until somebody edited the database.
    """
    from datetime import timedelta

    from app.db.base import utcnow
    from app.db.session import bind_tenant
    from app.models.agent import AgentRun, RunStatus
    from tests.test_api_flows import auth, configure, register

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    configure(client, token, "on_page_seo_sync")
    bind_tenant(clean_db, org_id)

    from app.core.config import settings

    clean_db.add(
        AgentRun(
            tenant_id=org_id,
            agent_slug="on_page_seo_sync",
            trigger="schedule",
            status=RunStatus.RUNNING.value,
            started_at=utcnow()
            - timedelta(minutes=settings.scheduler_claim_lease_minutes + 1),
        )
    )
    clean_db.commit()

    response = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert "started" in response.json()["toast"]["message"].lower()


@requires_db
def test_the_scheduler_needs_no_signed_in_user(clean_db, org):  # noqa: ANN001
    """What makes agents run while everybody is signed out.

    The scheduler claims due agents straight from the database inside the
    app's own lifespan task. There is no session, no token and no request
    anywhere in the path, which is why closing the browser changes nothing.
    """
    import inspect

    from app.orchestration import scheduler as scheduler_module

    source = inspect.getsource(scheduler_module.AgentScheduler.tick)
    for forbidden in ("request", "current_user", "token", "CurrentUser"):
        assert forbidden not in source, f"tick() references {forbidden}"


# ── What a card can say about a pass ───────────────────────────────────────
@requires_db
def test_a_running_pass_reports_the_step_it_is_on(clean_db, org):  # noqa: ANN001
    """Runs are started and not awaited, so "running" on its own is not an
    answer for the minute or two a pass takes. The step is written on its own
    connection precisely so it is readable while the pass is still open."""
    from sqlalchemy import select

    from app.db.base import utcnow
    from app.models.agent import AgentRecord, AgentRun, RunStatus
    from app.orchestration.runner import build_context
    from app.services import views

    record = clean_db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "on_page_seo_sync"
        )
    ).scalar_one()
    run = AgentRun(
        tenant_id=org.id,
        agent_slug=record.slug,
        trigger="manual",
        status=RunStatus.RUNNING.value,
        started_at=utcnow(),
    )
    clean_db.add(run)
    clean_db.commit()

    ctx = build_context(clean_db, record, org, trigger="manual")
    ctx.run_id = run.id
    ctx.progress("Analysing /pricing (3 of 6)", done=3, total=6)

    clean_db.expire_all()
    out = views.agent_out(record, can_write=True, run=clean_db.get(AgentRun, run.id))
    assert out.busy is True
    assert out.step == "Analysing /pricing (3 of 6)"
    assert out.step_percent == 50
    assert out.busy_for.startswith("running for")


@requires_db
def test_a_step_with_no_countable_total_offers_no_percentage(clean_db, org):  # noqa: ANN001
    """A progress bar that invents its own position is worse than none. Some
    steps genuinely have no denominator — "reading the sitemap"."""
    from sqlalchemy import select

    from app.db.base import utcnow
    from app.models.agent import AgentRecord, AgentRun, RunStatus
    from app.services import views

    record = clean_db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "on_page_seo_sync"
        )
    ).scalar_one()
    run = AgentRun(
        tenant_id=org.id,
        agent_slug=record.slug,
        trigger="manual",
        status=RunStatus.RUNNING.value,
        started_at=utcnow(),
        step="Looking for pages to work on",
    )
    clean_db.add(run)
    clean_db.flush()

    out = views.agent_out(record, can_write=True, run=run)
    assert out.busy is True
    assert out.step_percent is None


@requires_db
def test_a_finished_pass_reports_its_outcome_not_its_step(clean_db, org):  # noqa: ANN001
    """Once it is over, what it *did* is the useful thing. A status with no
    outcome is what let an agent skip six times and still look healthy."""
    from sqlalchemy import select

    from app.db.base import utcnow
    from app.models.agent import AgentRecord, AgentRun, RunStatus
    from app.services import views

    record = clean_db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "on_page_seo_sync"
        )
    ).scalar_one()
    run = AgentRun(
        tenant_id=org.id,
        agent_slug=record.slug,
        trigger="schedule",
        status=RunStatus.SKIPPED.value,
        started_at=utcnow(),
        finished_at=utcnow(),
        step="Analysing /pricing (3 of 6)",
        step_done=3,
        step_total=6,
        summary="Waiting on a connector that can list pages",
    )
    clean_db.add(run)
    clean_db.flush()

    out = views.agent_out(record, can_write=True, run=run)
    assert out.busy is False
    # The stale step is not shown as though it were still happening.
    assert out.step == ""
    assert out.step_percent is None
    assert out.last_run_status == "skipped"
    assert "Waiting on a connector" in out.last_run_summary


@requires_db
def test_every_agent_is_filed_under_a_section_the_console_knows(clean_db, org):  # noqa: ANN001
    """The group is derived from the same rule that sorts the list, so the
    headings and the order cannot disagree — and a row with no group would
    simply vanish from a sectioned screen."""
    from app.services import views

    known = set(views.AGENT_GROUPS.values())
    rows = views.list_agents(clean_db, tenant_id=org.id, can_write=True)
    assert rows
    for row in rows:
        assert row.group in known, (row.slug, row.group)


@requires_db
def test_every_connector_is_filed_under_a_section_too(clean_db, org):  # noqa: ANN001
    from app.services import connectors as connector_service
    from app.services import views

    known = set(views.CONNECTOR_GROUPS.values())
    records = connector_service.list_records(clean_db, tenant_id=org.id)
    assert records
    for record in records:
        assert views.connector_out(record, can_write=True).group in known
