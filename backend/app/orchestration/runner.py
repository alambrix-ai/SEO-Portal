"""Runs one agent, once.

This is the seam the whole platform turns on. An agent only *proposes*
actions; the runner decides — from the workspace guardrail, the agent's own
autonomy switch and each action's impact — whether to apply it now or put it
in the approvals queue, then records the run, the audit trail and the metrics.

Because the same ``apply`` callable is used in both paths, an approved action
executes exactly what an autonomous one would have, with no second code path
to drift.
"""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.contracts import AgentAction, AgentResult, BaseAgent
from app.agents.base.policy import Decision, Guardrail, decide, resolve_guardrail
from app.agents.base.registry import get_agent
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.llm.base import LLMError
from app.models.agent import AgentRecord, AgentRun, AgentStatus, RunStatus
from app.models.workspace import OnboardingState, Organization
from app.services import audit, metrics
from app.services.approvals import queue_action
from app.connectors.base.connector import Capability
from app.services.connectors import build_connector_bundle
from app.services.encryption import OrgCipher

log = get_logger(__name__)




class RunOutcome:
    """Lightweight result holder returned to callers (API, scheduler)."""

    def __init__(self, run: AgentRun, result: AgentResult | None) -> None:
        self.run = run
        self.result = result

    @property
    def status(self) -> str:
        return self.run.status

    @property
    def summary(self) -> str:
        return self.run.summary


def build_context(
    db: Session, record: AgentRecord, org: Organization, *, trigger: str = "schedule"
) -> AgentContext:
    from app.llm import get_provider

    bundle = build_connector_bundle(db, org)
    return AgentContext(
        db=db,
        org=org,
        record=record,
        cipher=OrgCipher.for_org(org),
        llm=get_provider(),
        connectors=bundle.instances,
        connector_names=bundle.names,
        connected_slugs=bundle.connected,
        trigger=trigger,
    )


def _guardrail_for(db: Session, tenant_id: str) -> Guardrail:
    state = db.execute(
        select(OnboardingState).where(OnboardingState.tenant_id == tenant_id)
    ).scalar_one_or_none()
    return resolve_guardrail(state.guardrail if state else None)


def _reset_daily_counter(record: AgentRecord) -> None:
    today = utcnow().date().isoformat()
    if record.actions_today_date != today:
        record.actions_today_date = today
        record.actions_today = 0


def _schedule_next(record: AgentRecord, agent: BaseAgent) -> None:
    """Set ``next_run_at`` from the configured schedule."""
    interval = SCHEDULE_INTERVALS.get(record.schedule) or agent.spec.default_interval
    # SCHEDULER_SPEED shortens every cadence in development so a run is
    # observable without waiting an hour.
    speed = max(settings.scheduler_speed, 0.01)
    record.next_run_at = utcnow() + timedelta(seconds=interval.total_seconds() / speed)


SCHEDULE_INTERVALS: dict[str, timedelta] = {
    "Hourly": timedelta(hours=1),
    "Every 6 hours": timedelta(hours=6),
    "Daily": timedelta(days=1),
    "Weekly": timedelta(days=7),
}


def run_agent(
    db: Session,
    *,
    record: AgentRecord,
    org: Organization,
    trigger: str = "schedule",
    triggered_by: str = "",
    force: bool = False,
) -> RunOutcome:
    """Execute one pass of one agent and persist everything it produced.

    ``force`` bypasses the paused check, which is what a manual "Run now" from
    the console does.
    """
    agent = get_agent(record.slug)
    started = utcnow()
    clock = time.perf_counter()

    run = AgentRun(
        tenant_id=org.id,
        agent_slug=record.slug,
        trigger=trigger,
        triggered_by=triggered_by,
        status=RunStatus.RUNNING.value,
        started_at=started,
    )
    db.add(run)
    db.flush()

    def finish(
        status: RunStatus,
        *,
        summary: str = "",
        error: str = "",
        taken: int = 0,
        queued: int = 0,
        detail: dict | None = None,
        usage=None,  # noqa: ANN001 - LLMUsage or None
    ) -> RunOutcome:
        run.status = status.value
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        run.summary = summary
        run.error = error
        run.actions_taken = taken
        run.actions_queued = queued
        run.detail = detail or {}
        if usage is not None:
            run.tokens_in = usage.input_tokens
            run.tokens_out = usage.output_tokens
            run.cost = usage.cost
        record.last_run_at = run.finished_at
        return RunOutcome(run, None)

    if agent is None:
        log.error("No implementation registered for agent %s", record.slug)
        record.status = AgentStatus.ERROR.value
        record.last_error = "Agent implementation not found"
        return finish(RunStatus.FAILED, error=record.last_error)

    if not force and not record.is_running:
        return finish(RunStatus.SKIPPED, summary="Agent is paused")

    _reset_daily_counter(record)

    try:
        ctx = build_context(db, record, org, trigger=trigger)
    except LLMError as exc:
        # No model credential is a setup gap, not a defect — the same class of
        # thing as a missing connector. Report it as a skip an operator can
        # act on, rather than an error that looks like a bug.
        reason = str(exc)
        record.metric_label = "Waiting on a model credential"
        _schedule_next(record, agent)
        return finish(RunStatus.SKIPPED, summary=reason)

    # Handed over so the pass can report where it has got to, and
    # committed so the progress writer — which uses its own connection — can
    # see the row. Committing the run row early also makes an in-flight pass
    # visible to the "is it already running" guard behind Run now.
    ctx.run_id = run.id
    db.commit()

    # ── Preflight ──────────────────────────────────────────────────────────
    try:
        ctx.progress("Checking its connectors and scope")
        skip_reason = agent.preflight(ctx)
    except Exception as exc:  # noqa: BLE001
        log.exception("Preflight failed for %s", record.slug)
        from app.core.user_messages import public_error_message

        public = public_error_message(exc, fallback="This agent could not start its run.")
        record.last_error = public
        _schedule_next(record, agent)
        return finish(RunStatus.FAILED, error=public)

    if skip_reason:
        _schedule_next(record, agent)
        record.metric_label = skip_reason
        return finish(RunStatus.SKIPPED, summary=skip_reason)

    # ── Run ────────────────────────────────────────────────────────────────
    try:
        result = agent.run(ctx)
    except Exception as exc:  # noqa: BLE001 - one agent must not stop the fleet
        log.exception("Agent %s failed", record.slug)
        from app.core.user_messages import public_error_message

        public = public_error_message(
            exc, fallback="This agent could not finish its run. Try again later."
        )
        record.consecutive_failures += 1
        record.last_error = public
        if record.consecutive_failures == 1:
            _notify(
                ctx,
                record,
                subject=f"{record.name} could not finish its run",
                message=f"{public}\n\nWorkspace: {org.name}",
            )
            # Once, at the start of the streak. Every tick would be noise, and
            # only recording it at the pause threshold meant the first four
            # failures of a broken integration went unmentioned.
            audit.record_agent_action(
                db,
                tenant_id=org.id,
                agent_name=record.name,
                action=f"could not finish its run — {public}",
                module=_module_for(record.category),
                context={"error": type(exc).__name__},
            )
        if record.consecutive_failures >= settings.agent_max_consecutive_failures:
            record.status = AgentStatus.ERROR.value
            record.metric_label = "Paused after repeated failures"
            audit.record_agent_action(
                db,
                tenant_id=org.id,
                agent_name=record.name,
                action=(
                    f"was paused automatically after {record.consecutive_failures} "
                    "consecutive failures"
                ),
                module=_module_for(record.category),
            )
        _schedule_next(record, agent)
        return finish(RunStatus.FAILED, error=record.last_error, usage=ctx.usage)

    record.consecutive_failures = 0
    record.last_error = ""

    if result.skipped:
        _schedule_next(record, agent)
        if result.metric_label:
            record.metric_label = result.metric_label
        return finish(
            RunStatus.SKIPPED,
            summary=result.skip_reason or result.summary,
            usage=ctx.usage,
        )

    # ── Apply or queue each action ─────────────────────────────────────────
    guardrail = _guardrail_for(db, org.id)
    module = _module_for(record.category)
    applied = queued = blocked = 0

    total_actions = len(result.actions)
    for index, action in enumerate(result.actions, start=1):
        ctx.progress(
            f"Applying what it found ({index} of {total_actions})",
            done=index,
            total=total_actions,
        )
        decision, reason = decide(
            action,
            guardrail=guardrail,
            agent_autonomous=record.autonomy,
            remaining_actions=ctx.remaining_actions,
        )

        if decision is Decision.APPLY:
            try:
                _apply(action, ctx)
            except Exception as exc:  # noqa: BLE001 - keep going for the rest
                log.exception("Applying %s from %s failed", action.kind, record.slug)
                record.last_error = f"{action.kind}: {exc}"
                continue
            applied += 1
            record.actions_today += 1
            audit.record_agent_action(
                db,
                tenant_id=org.id,
                agent_name=record.name,
                action=action.audit or action.title,
                module=module,
                context={"kind": action.kind, "target_id": action.target_id},
            )

        elif decision is Decision.QUEUE:
            _, created = queue_action(
                db, org=org, record=record, action=action, cipher=ctx.cipher
            )
            # A proposal that was already pending is not newly queued. Counting
            # it would report work to the operator on every run of an agent
            # that has simply not changed its mind.
            if created:
                queued += 1
                # Recorded, because this is the outcome that needs a person and
                # it used to be recorded nowhere. Every agent ships paused and
                # under human review, so queueing is the *normal* result — and
                # with nothing in the audit log the notification panel had
                # nothing an agent had done to show, on any workspace, ever.
                audit.record_agent_action(
                    db,
                    tenant_id=org.id,
                    agent_name=record.name,
                    action=f"proposed {action.title} for approval",
                    module=module,
                    context={
                        "kind": action.kind,
                        "target_id": action.target_id,
                        "impact": action.impact.value,
                        "awaiting_approval": True,
                    },
                )

        else:
            blocked += 1
            log.info("%s: action blocked — %s", record.slug, reason)

    # ── Bookkeeping ────────────────────────────────────────────────────────
    if result.metric_label:
        record.metric_label = result.metric_label
    if result.metrics:
        metrics.accumulate(db, tenant_id=org.id, values=result.metrics)
    metrics.accumulate(
        db,
        tenant_id=org.id,
        values={
            "agent_runs": 1,
            "actions_autonomous": applied,
        },
    )
    _schedule_next(record, agent)

    summary = result.summary or f"{applied} applied, {queued} queued"
    if blocked:
        summary = f"{summary} ({blocked} held by the daily cap)"

    # Only when a person would want to know. A pass that looked and found
    # nothing to do is the normal case and says nothing.
    if applied or queued:
        _notify(
            ctx,
            record,
            subject=f"{record.name}: {applied} applied, {queued} awaiting approval",
            message=(
                f"{summary}\n\n"
                f"Workspace: {org.name}\n"
                f"Agent: {record.name} ({record.slug})\n"
                + (
                    "There are proposals waiting for approval.\n"
                    if queued
                    else ""
                )
            ),
        )

    log.info(
        "%s/%s: %s in %dms", org.slug, record.slug, summary, int((time.perf_counter() - clock) * 1000)
    )
    outcome = finish(
        RunStatus.SUCCEEDED,
        summary=summary,
        taken=applied,
        queued=queued,
        detail=result.detail,
        usage=ctx.usage,
    )
    outcome.result = result
    return outcome


#: The channel an operator picks -> the capability that can serve it.
#: "Slack" is the channel's name, not necessarily the Slack connector: a
#: webhook or an SMTP relay can carry a notification too, and an operator who
#: has wired one of those should not be told to install Slack.
NOTIFY_CAPABILITIES: dict[str, Capability] = {
    "Slack": Capability.SEND_NOTIFICATION,
    "Email": Capability.SEND_EMAIL,
}


def _notify(ctx: AgentContext, record: AgentRecord, *, subject: str, message: str) -> None:
    """Tell the configured channel what just happened.

    Silent when the channel is None, which is the default and means exactly
    that. Never allowed to fail a run: the work is done and recorded by this
    point, and a Slack outage is not a reason to mark a successful pass as
    failed.
    """
    capability = NOTIFY_CAPABILITIES.get(record.notify_channel)
    if capability is None:
        return

    connector = ctx.with_capability(capability)
    if connector is None:
        # Configure refuses this combination now, so reaching here means the
        # connector was disconnected after the agent was set up.
        log.info(
            "%s: notify channel %s has no connector; nothing sent",
            record.slug,
            record.notify_channel,
        )
        return
    try:
        connector.notify(subject=subject, message=message)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - the run itself succeeded
        log.warning("%s: could not send a %s notification: %s", record.slug, record.notify_channel, exc)


def _apply(action: AgentAction, ctx: AgentContext) -> None:
    if action.apply is None:
        raise RuntimeError(f"Action {action.kind!r} declared no apply callable")
    action.apply(ctx)


def _module_for(category: str) -> str:
    return {
        "SEO & AEO": "seo",
        "Off-Page": "offpage",
        "Ads": "ads",
    }.get(category, "agents")


def run_agent_by_slug(
    db: Session,
    *,
    tenant_id: str,
    slug: str,
    trigger: str = "manual",
    triggered_by: str = "",
    force: bool = True,
) -> RunOutcome:
    """Convenience entry point for the API's "Run now" and for Celery tasks."""
    from app.core.exceptions import NotFoundError

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == tenant_id, AgentRecord.slug == slug
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError(f"Agent {slug!r} is not installed in this workspace")
    org = db.get(Organization, tenant_id)
    if org is None:
        raise NotFoundError("Workspace not found")
    return run_agent(
        db, record=record, org=org, trigger=trigger, triggered_by=triggered_by, force=force
    )
