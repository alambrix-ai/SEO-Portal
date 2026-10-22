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

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext, LLMOverride
from app.agents.base.contracts import AgentAction, AgentResult, BaseAgent
from app.agents.base.policy import Decision, Guardrail, decide, resolve_guardrail
from app.agents.base.registry import get_agent
from app.core.config import settings
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.llm.base import LLMError
from app.models.agent import AgentRecord, AgentRun, AgentStatus, RunStatus
from app.models.workspace import OnboardingState, Organization
from app.agents.base import safety
from app.services import agent_safety, audit, metrics
from app.services.approvals import queue_action
from app.connectors.base.connector import Capability
from app.services.connectors import build_connector_bundle, public_connector_error
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


#: Agent setting that pins an agent to its chosen model: when that model is
#: not connected the run is skipped instead of using another one.
STRICT_MODEL_SETTING = "strict_model"


def _strict_unavailable(choice: str, names: dict[str, str]) -> LLMError:
    """The skip reason when a pinned model cannot be used.

    Only a known connector's display name is quoted, never the raw choice: a
    job's choice is caller input.
    """
    if not choice:
        return LLMError("Choose which AI model this agent should use, then try again.")
    pretty = names.get(choice) or "The chosen AI model"
    return LLMError(
        f"{pretty} is not connected or is not available on your plan. This run is "
        "set to use only that model, so nothing was written with another one. "
        "Connect it under Connectors, or choose a different model."
    )


def build_context(
    db: Session,
    record: AgentRecord,
    org: Organization,
    *,
    trigger: str = "schedule",
    llm_override: LLMOverride | None = None,
    job: Mapping[str, Any] | None = None,
) -> AgentContext:
    """Everything one pass of ``record`` is handed.

    ``llm_override`` is the model a job named for this run and ``job`` that
    job's payload (on-demand agents). A named model is strict, as is an agent
    whose ``strict_model`` setting is on: the chosen model is used or the run
    stops with :class:`LLMError` — it never falls back to another connected
    model. Otherwise the saved choice falls back as it always has.
    """
    from app.agents.base.registry import get_agent
    from app.llm.from_connector import UnusedLLMProvider, effective_llm, provider_from_connector

    from app.connectors.base.connector import Capability
    from app.services import entitlements

    if job is not None and not isinstance(job, Mapping):
        raise TypeError("job must be a mapping")

    bundle = build_connector_bundle(db, org)
    agent = get_agent(record.slug)
    requires_llm = bool(agent and agent.spec.requires_llm) or llm_override is not None
    strict = llm_override is not None or (record.settings or {}).get(STRICT_MODEL_SETTING) is True
    # What the plan lets this agent use; the context applies it to every
    # connector lookup, and the model is picked from the allowed ones.
    policies = entitlements.lookup(db, org)

    llm_connector = ""
    llm_model = ""
    if not requires_llm:
        llm = UnusedLLMProvider()
    else:
        # Configure-time on_configure may run before a model is chosen; do not
        # fail the save path. Real runs still raise and become a skip.
        try:
            model_policy = policies(record.slug, Capability.COMPLETE)
            choice = (
                (llm_override.connector if llm_override else "") or record.llm_connector or ""
            ).strip()
            slug, _stale = effective_llm(
                choice,
                [s for s in bundle.instances if model_policy.permits(s)],
                allow_fallback=not strict,
            )
            if not slug:
                if strict:
                    raise _strict_unavailable(choice, bundle.names)
                raise LLMError(
                    "Connect an AI model (OpenAI, Anthropic Claude, Google Gemini or "
                    "Perplexity) under Integrations first — this agent writes with it."
                )
            llm = provider_from_connector(
                slug,
                bundle.instances,
                names=bundle.names,
                model_override=llm_override.model if llm_override else None,
            )
            llm_connector = slug
            llm_model = str(getattr(llm, "model", "") or "")
        except LLMError:
            if trigger == "config":
                llm = UnusedLLMProvider()
            else:
                raise

    return AgentContext(
        db=db,
        org=org,
        record=record,
        cipher=OrgCipher.for_org(org),
        llm=llm,
        connectors=bundle.instances,
        connector_names=bundle.names,
        connected_slugs=bundle.connected,
        trigger=trigger,
        policies=policies,
        # A read-only copy: the agent cannot change the caller's job.
        job=MappingProxyType(dict(job)) if job is not None else None,
        llm_override=llm_override,
        strict_model=strict,
        llm_connector=llm_connector,
        llm_model=llm_model,
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
    llm_override: LLMOverride | None = None,
    job: Mapping[str, Any] | None = None,
) -> RunOutcome:
    """Execute one pass of one agent and persist everything it produced.

    ``force`` bypasses the paused check, which is what a manual "Run now" from
    the console does. ``job`` and ``llm_override`` are for on-demand runs
    (see :func:`run_on_demand` and :func:`build_context`).
    """
    agent = get_agent(record.slug)
    # Set once the context is built; ``finish`` records the model from it.
    ctx: AgentContext | None = None
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
        # The progress writer commits the timeline on its own connection, so
        # this session's copy of ``detail`` is stale: read the stored value
        # and merge, rather than overwrite what the pass recorded.
        from app.models.agent import close_timeline

        stored = db.execute(
            select(AgentRun.detail).where(AgentRun.id == run.id)
        ).scalar_one_or_none()
        run.status = status.value
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        run.summary = summary
        run.error = error
        run.actions_taken = taken
        run.actions_queued = queued
        merged = {**(stored or {}), **(detail or {})}
        if ctx is not None and ctx.tokens_unknown:
            # A model call came back without token counts: the numbers below
            # are placeholders, and usage reports must say "unknown", not 0.
            from app.services.llm_usage import TOKENS_UNKNOWN_DETAIL_KEY

            merged[TOKENS_UNKNOWN_DETAIL_KEY] = True
        run.detail = close_timeline(merged)
        if usage is not None:
            run.tokens_in = usage.input_tokens
            run.tokens_out = usage.output_tokens
            run.cost = usage.cost
        # The model this run was set up to write with, kept on the run because
        # the agent's choice can change later and usage is priced per model.
        if ctx is not None and ctx.llm_connector:
            run.llm_connector = ctx.llm_connector[:64]
            run.llm_model = ctx.llm_model[:128]
        record.last_run_at = run.finished_at
        return RunOutcome(run, None)

    if agent is None:
        log.error("No implementation registered for agent %s", record.slug)
        record.status = AgentStatus.ERROR.value
        record.last_error = "Agent implementation not found"
        return finish(RunStatus.FAILED, error=record.last_error)

    if getattr(agent.spec, "on_demand", False) and job is None:
        # Never on a schedule: an on-demand agent has nothing to do without
        # the job it was started for.
        return finish(RunStatus.SKIPPED, summary="This agent runs only when a job starts it")

    if not force and not record.is_running:
        return finish(RunStatus.SKIPPED, summary="Agent is paused")

    # The stop switches win over everything, "Run now" included: the
    # platform's emergency stop and the workspace's "Stop all agents".
    halted = agent_safety.halt_reason(db, org)
    if halted:
        return finish(RunStatus.SKIPPED, summary=halted)

    _reset_daily_counter(record)

    try:
        ctx = build_context(
            db, record, org, trigger=trigger, llm_override=llm_override, job=job
        )
    except LLMError as exc:
        # No model connector is a setup gap, not a defect — the same class of
        # thing as a missing CMS. Report it as a skip an operator can act on.
        from app.core.user_messages import public_error_message

        reason = public_error_message(
            exc,
            fallback="Choose which AI model this agent should use, then try again.",
        )
        record.metric_label = "Waiting on an AI model"
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
        # Redacted and bounded, no traceback: exception text can carry vendor
        # bodies or keys.
        log_failure(
            log, "agent.preflight_failed", exc, level=logging.ERROR, agent=record.slug, run_id=run.id
        )
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
        log_failure(
            log, "agent.run_failed", exc, level=logging.ERROR, agent=record.slug, run_id=run.id
        )
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
                context={"error": type(exc).__name__, "run_id": run.id},
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

    # ── Apply, queue or practise each action ───────────────────────────────
    guardrail = _guardrail_for(db, org.id)
    gate = agent_safety.gate_for(db, org, record, connector_name=ctx.connector_name)
    tally = _process_actions(
        db,
        ctx=ctx,
        org=org,
        record=record,
        run_id=run.id,
        actions=result.actions,
        guardrail=guardrail,
        gate=gate,
    )
    applied, queued, blocked, practised = (
        tally.applied,
        tally.queued,
        tally.blocked,
        tally.practised,
    )

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
    if gate.practice:
        noun = "change" if practised == 1 else "changes"
        again = (
            f" ({tally.practised_again} the same as earlier today)"
            if tally.practised_again
            else ""
        )
        summary = (
            f"{summary}. Practice mode: {practised} {noun} planned{again}, "
            "nothing sent"
        )
    run_detail = dict(result.detail or {})
    if gate.practice or practised:
        run_detail["practice"] = gate.practice
        run_detail["practised"] = practised
    if tally.stopped_mid_run:
        run_detail["stopped_mid_run"] = True
        summary = f"{summary}. Agents were stopped during the run; the rest wait for approval"

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
        detail=run_detail,
        usage=ctx.usage,
    )
    outcome.result = result
    _record_history(db, org, record.slug, run)
    return outcome


@dataclass(slots=True)
class _Tally:
    applied: int = 0
    queued: int = 0
    blocked: int = 0
    #: Practice mode: changes planned and recorded instead of sent.
    practised: int = 0
    #: …of which the same change had already been practised in the last day.
    practised_again: int = 0
    stopped_mid_run: bool = False


def _process_actions(
    db: Session,
    *,
    ctx: AgentContext,
    org: Organization,
    record: AgentRecord,
    run_id: str,
    actions: list[AgentAction],
    guardrail: Guardrail,
    gate: safety.SafetyGate,
) -> _Tally:
    """Apply, queue or practise each action a run produced.

    For a spending agent (``gate.spending``), in this order:

    * the safety holds — a write through a connector not yet verified, or a
      change over the workspace's limits — are worked out first
      (``safety.assess``) and passed to the policy, which then never applies
      the action automatically;
    * in **practice mode** nothing is applied or queued: the action is
      recorded as "Practised — not sent", with its payload and what would
      have happened, and its ``apply`` is never called — so no write reaches
      a vendor. The reads the agent did to plan it have already happened;
    * otherwise, before every apply the stop switches are read again, so a
      stop pressed during a run turns the rest of its changes into proposals
      instead of sending them.
    """
    tally = _Tally()
    module = _module_for(record.category)
    total = len(actions)
    # Practice runs do not use the daily allowance (nothing is applied), but
    # the plan should still respect it, so it is counted here.
    practice_used = 0

    for index, action in enumerate(actions, start=1):
        ctx.progress(
            f"Applying what it found ({index} of {total})",
            done=index,
            total=total,
        )
        route = safety.assess(action, gate)
        decision, reason = decide(
            action,
            guardrail=guardrail,
            agent_autonomous=record.autonomy,
            remaining_actions=ctx.remaining_actions - practice_used,
            holds=route.notes,
        )

        if gate.practice:
            if decision is Decision.BLOCK:
                tally.blocked += 1
                continue
            would = "apply" if decision is Decision.APPLY else "queue"
            if decision is Decision.APPLY:
                gate.practice_ledger.add(route.measure)
                practice_used += 1
            item, created = agent_safety.record_practice(
                db,
                org=org,
                record=record,
                action=action,
                cipher=ctx.cipher,
                would=would,
                reason=reason,
                notes=route.notes,
                measure=route.measure.to_dict(),
                source=gate.practice,
            )
            tally.practised += 1
            if not created:
                # The same change as one practised earlier today: counted in
                # the run's summary, not recorded again.
                tally.practised_again += 1
                continue
            outcome = (
                "would have gone ahead automatically"
                if would == "apply"
                else "would have waited for approval"
            )
            audit.record_agent_action(
                db,
                tenant_id=org.id,
                agent_name=record.name,
                action=f"practised {action.title} — not sent ({outcome})",
                module=module,
                context={
                    "kind": action.kind,
                    "target_id": action.target_id,
                    "run_id": run_id,
                    "agent": record.slug,
                    "practice": True,
                    "would": would,
                    "notes": route.notes,
                    "safety": route.measure.to_dict(),
                    "approval_id": getattr(item, "id", None),
                },
            )
            continue

        notes = list(route.notes)
        if decision is Decision.APPLY:
            halted = gate.halted()
            if halted:
                # Stopped while this run was going: nothing more is sent, and
                # what it found waits for a person rather than being lost.
                tally.stopped_mid_run = True
                decision = Decision.QUEUE
                notes.append(agent_safety.STOPPED_MID_RUN)

        if decision is Decision.APPLY:
            try:
                _apply(action, ctx)
            except Exception as exc:  # noqa: BLE001 - keep going for the rest
                # Type only: exception text can carry vendor bodies or keys.
                log.error(
                    "Applying %s from %s failed (%s)", action.kind, record.slug, type(exc).__name__
                )
                record.last_error = public_connector_error(
                    exc,
                    fallback="One of this agent's changes could not be made. "
                    "Check its connections and try again.",
                )
                continue
            tally.applied += 1
            record.actions_today += 1
            if action.metrics:
                metrics.accumulate(db, tenant_id=org.id, values=action.metrics)
            context = {"kind": action.kind, "target_id": action.target_id, "run_id": run_id}
            if gate.spending:
                gate.ledger.add(route.measure)
                # Read back by today's limits (agent_safety.today_ledger).
                context.update(auto_applied=True, safety=route.measure.to_dict())
            audit.record_agent_action(
                db,
                tenant_id=org.id,
                agent_name=record.name,
                action=action.audit or action.title,
                module=module,
                context=context,
            )

        elif decision is Decision.QUEUE:
            _, created = queue_action(
                db, org=org, record=record, action=action, cipher=ctx.cipher, why=notes
            )
            # A proposal that was already pending is not newly queued. Counting
            # it would report work to the operator on every run of an agent
            # that has simply not changed its mind.
            if created:
                tally.queued += 1
                # Recorded, because this is the outcome that needs a person and
                # it used to be recorded nowhere. Every agent ships paused and
                # under human review, so queueing is the *normal* result — and
                # with nothing in the audit log the notification panel had
                # nothing an agent had done to show, on any workspace, ever.
                context = {
                    "kind": action.kind,
                    "target_id": action.target_id,
                    "impact": action.impact.value,
                    "awaiting_approval": True,
                    "run_id": run_id,
                }
                if notes:
                    context["held"] = notes
                audit.record_agent_action(
                    db,
                    tenant_id=org.id,
                    agent_name=record.name,
                    action=f"proposed {action.title} for approval",
                    module=module,
                    context=context,
                )

        else:
            tally.blocked += 1
            log.info("%s: action blocked — %s", record.slug, reason)

    return tally


def _record_history(db: Session, org: Organization, slug: str, run: AgentRun) -> None:
    """Snapshot the audit a successful run just produced, for its history.

    Only for the agents that have one (``history.AGENT_KINDS``). The run and
    its work are committed first, so they stand on their own; the snapshot is
    then taken in a savepoint, and any failure there is logged and dropped —
    a run never fails because its history could not be written.
    """
    from app.services import history
    from app.services.history import store

    if slug not in history.AGENT_KINDS:
        return
    db.commit()
    if store.guarded(db, f"{slug} run {run.id}", history.record_after_run, db, org, slug, run) is None:
        return
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - the run is already committed
        log.exception("Could not save the history snapshot for %s", slug)
        db.rollback()


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
        "Local business": "seo",
        "Content & Social": "content",
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


#: Triggers an on-demand run may be recorded with.
ON_DEMAND_TRIGGERS: frozenset[str] = frozenset({"manual", "job"})


def _on_demand_record(agent: BaseAgent, org: Organization) -> AgentRecord:
    """A stand-in configuration row for one on-demand run.

    On-demand agents are not installed in a workspace, so there is no stored
    row. This one is never added to the session, so nothing about it is
    saved; every field the runner reads is set explicitly, because column
    defaults only apply on insert. It is autonomy-off: what an on-demand
    agent proposes always waits for a person.
    """
    spec = agent.spec
    return AgentRecord(
        tenant_id=org.id,
        slug=spec.slug,
        name=spec.name,
        category=spec.category.value,
        description=spec.description,
        status=AgentStatus.RUNNING.value,
        autonomy=False,
        schedule=spec.default_schedule,
        scope="",
        notify_channel="None",
        llm_connector="",
        max_actions_per_day=spec.default_max_actions_per_day,
        configured=True,
        metric_label="",
        actions_today=0,
        actions_today_date="",
        consecutive_failures=0,
        last_error="",
        settings={},
    )


def run_on_demand(
    db: Session,
    *,
    org: Organization,
    slug: str,
    job: Mapping[str, Any],
    llm_override: LLMOverride | None = None,
    trigger: str = "job",
    triggered_by: str = "",
) -> RunOutcome:
    """Run an on-demand agent once for ``job`` and record the run.

    ``llm_override`` is the model the job named; it is strict, so a model
    that cannot be used skips the run rather than falling back. Only agents
    declared ``on_demand`` can be run this way; fleet agents keep their own
    schedule and "Run now".
    """
    from app.core.exceptions import InvalidInputError, NotFoundError

    if trigger not in ON_DEMAND_TRIGGERS:
        raise InvalidInputError("Unknown trigger for an on-demand run")
    if not isinstance(job, Mapping):
        raise InvalidInputError("An on-demand run needs its job")
    agent = get_agent(slug)
    if agent is None or not agent.spec.on_demand:
        raise NotFoundError("That on-demand agent does not exist")
    return run_agent(
        db,
        record=_on_demand_record(agent, org),
        org=org,
        trigger=trigger,
        triggered_by=triggered_by,
        force=True,
        llm_override=llm_override,
        job=job,
    )
