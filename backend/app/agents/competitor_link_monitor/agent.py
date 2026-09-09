"""Competitor Link Monitor — watches rivals' backlinks and counter-pitches.

When a competitor earns a link from a domain, that domain has just proven two
things: it publishes on this subject, and it is currently willing to link out.
That is the best moment to approach it — so this agent detects the event and
drafts a *structurally superior* counter-pitch to the same publisher.

The counter-pitch is deliberately not a copy of the competitor's angle;
pitching the same story a week later gets ignored. The prompt asks for
something that beats it on substance, and sending stays high impact.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.style import humanise
from app.agents.base import scope as scope_rules
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.competitor_link_monitor import prompts
from app.agents.digital_pr_outreach import prompts as pitch_prompts
from app.connectors.base.connector import Capability
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert, OutreachPitch
from app.models.workspace import Organization
from app.services.approvals import register_applier
from app.services.encryption import Ctx

log = get_logger(__name__)

MAX_COUNTERS_PER_RUN = 2
# The same publisher is not counter-pitched twice inside this window.
PUBLISHER_COOLDOWN = timedelta(days=30)


class CompetitorLinkMonitorAgent(BaseAgent):
    spec = AgentSpec(
        slug="competitor_link_monitor",
        name="Competitor Link Monitor",
        category=AgentCategory.OFF_PAGE,
        description="Watches competitor backlinks, triggers counter-pitches.",
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        max_impact=Impact.HIGH,
        default_max_actions_per_day=6,
        # Declared, not only checked inside the run. The internal guard below
        # already refuses to invent competitor activity without a data source,
        # but an agent that runs and reports "0 new alerts" reads as though it
        # looked and found nothing. Preflight says the truth instead: it is
        # waiting on a connector.
        required_capabilities=(Capability.READ_BACKLINKS,),
        scope_placeholder="Competitor names or domains, comma separated",
    )

    def validate_scope(self, scope: str) -> str | None:
        """Competitor domains or names."""
        return scope_rules.domains(scope) or scope_rules.free_text(
            scope, max_items=10, label="competitors"
        )

    def run(self, ctx: AgentContext) -> AgentResult:
        competitors = self._competitors(ctx)
        if not competitors:
            return AgentResult.skip("No competitors configured — set them in Configure")

        new_alerts = self._detect(ctx, competitors)
        actions = self._counter_pitch(ctx)

        recent = list(
            ctx.db.execute(
                select(CompetitorAlert).where(
                    CompetitorAlert.tenant_id == ctx.tenant_id,
                    CompetitorAlert.detected_at >= utcnow() - timedelta(days=7),
                )
            ).scalars()
        )

        return AgentResult(
            summary=f"{new_alerts} new competitor links, {len(actions)} counter-pitches drafted",
            actions=actions,
            metric_label=f"{len(recent)} links poached this week",
            detail={"new_alerts": new_alerts, "competitors": competitors},
        )

    # ── Configuration ──────────────────────────────────────────────────────
    def _competitors(self, ctx: AgentContext) -> list[str]:
        if ctx.scope:
            return [c.strip() for c in ctx.scope.split(",") if c.strip()][:10]
        configured = ctx.setting("competitors") or []
        return [str(c) for c in configured][:10]

    # ── Detection ──────────────────────────────────────────────────────────
    def _detect(self, ctx: AgentContext, competitors: list[str]) -> int:
        """Compare each competitor's live backlink set against what is known."""
        source = ctx.with_capability(Capability.READ_BACKLINKS)
        if source is None:
            # Without a backlink data source there is nothing factual to
            # report, and inventing competitor activity would put fabricated
            # alerts in front of an operator. So the run stays quiet.
            ctx.log_info("No backlink data source connected; detection skipped")
            return 0

        known = {
            (alert.competitor, alert.source_domain)
            for alert in ctx.db.execute(
                select(CompetitorAlert).where(CompetitorAlert.tenant_id == ctx.tenant_id)
            ).scalars()
        }
        discovered = 0

        for competitor in competitors:
            try:
                rows = source.read_backlinks(limit=50)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Backlink source unavailable: %s", exc)
                break

            for row in rows:
                domain = str(row.get("domain") or "").strip().lower()
                if not domain or (competitor, domain) in known:
                    continue
                known.add((competitor, domain))
                authority = int(row.get("authority") or 0)
                ctx.db.add(
                    CompetitorAlert(
                        tenant_id=ctx.tenant_id,
                        competitor=competitor,
                        source_domain=domain,
                        authority=authority,
                        text=(
                            f'Competitor "{competitor}" earned a backlink from '
                            f"{domain} (authority {authority})."
                        ),
                        detected_at=utcnow(),
                        detail={
                            "anchor": row.get("anchor") or "",
                            "url": row.get("url") or "",
                        },
                    )
                )
                discovered += 1

        if discovered:
            ctx.db.flush()
        return discovered

    # ── Counter-pitching ───────────────────────────────────────────────────
    def _counter_pitch(self, ctx: AgentContext) -> list[AgentAction]:
        alerts = self._alerts_to_answer(ctx)
        angle = ctx.scope and "" or ctx.setting("counter_angle", "")
        actions: list[AgentAction] = []

        for alert in alerts[:MAX_COUNTERS_PER_RUN]:
            try:
                data = ctx.ask_json(
                    prompts.counter_pitch(
                        publisher=alert.source_domain,
                        competitor=alert.competitor,
                        competitor_anchor=str((alert.detail or {}).get("anchor") or ""),
                        brand=ctx.org.name,
                        authority=alert.authority,
                        preferred_angle=angle,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Counter-pitch drafting failed for %s: %s", alert.source_domain, exc)
                continue

            subject = humanise((data.get("subject") or "").strip())
            body = humanise((data.get("body") or "").strip())
            if not subject or not body:
                continue

            # Held to exactly the same quality bar as ordinary outreach.
            problem = pitch_prompts.check_pitch(subject=subject, body=body, brand=ctx.org.name)
            if problem:
                ctx.log_warning("Rejected counter-pitch to %s: %s", alert.source_domain, problem)
                continue

            target = self._ensure_target(ctx, alert)
            pitch = OutreachPitch(
                tenant_id=ctx.tenant_id,
                target_id=target.id,
                target_domain=target.domain,
                subject_encrypted=ctx.cipher.encrypt(subject, context=Ctx.PITCH_SUBJECT),
                body_encrypted=ctx.cipher.encrypt(body, context=Ctx.PITCH_BODY),
                tone=str(data.get("tone") or "professional"),
                sentiment_score=float(data.get("sentiment_score") or 0.0),
                variant=str(data.get("variant") or "A")[:8],
                status="drafted",
                is_counter_pitch=True,
            )
            ctx.db.add(pitch)
            ctx.db.flush()

            actions.append(
                AgentAction(
                    kind="send_counter_pitch",
                    title=f"Counter-pitch to {alert.source_domain} (vs {alert.competitor})",
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.PR_PITCH.value,
                    target_kind="counter_pitch",
                    target_id=pitch.id,
                    payload={
                        "pitch_id": pitch.id,
                        "alert_id": alert.id,
                        "publisher": alert.source_domain,
                        "competitor": alert.competitor,
                    },
                    apply=_make_counter_sender(pitch.id, alert.id),
                    audit=(
                        f"launched a counter-pitch to {alert.source_domain} after "
                        f"{alert.competitor} earned a link there"
                    ),
                )
            )

        return actions

    def _alerts_to_answer(self, ctx: AgentContext) -> list[CompetitorAlert]:
        """Unanswered alerts, highest-authority publisher first."""
        cutoff = utcnow() - PUBLISHER_COOLDOWN
        recently_pitched = {
            domain
            for (domain,) in ctx.db.execute(
                select(OutreachPitch.target_domain).where(
                    OutreachPitch.tenant_id == ctx.tenant_id,
                    OutreachPitch.created_at >= cutoff,
                )
            )
        }
        alerts = list(
            ctx.db.execute(
                select(CompetitorAlert)
                .where(
                    CompetitorAlert.tenant_id == ctx.tenant_id,
                    CompetitorAlert.counter_launched.is_(False),
                )
                .order_by(CompetitorAlert.authority.desc())
            ).scalars()
        )
        return [a for a in alerts if a.source_domain not in recently_pitched]

    def _ensure_target(self, ctx: AgentContext, alert: CompetitorAlert) -> BacklinkTarget:
        """A publisher we are pitching becomes a tracked target."""
        target = ctx.db.execute(
            select(BacklinkTarget).where(
                BacklinkTarget.tenant_id == ctx.tenant_id,
                BacklinkTarget.domain == alert.source_domain,
            )
        ).scalar_one_or_none()
        if target is not None:
            return target

        target = BacklinkTarget(
            tenant_id=ctx.tenant_id,
            domain=alert.source_domain,
            authority=alert.authority,
            placement_type="Guest Post",
            status=BacklinkStatus.DISCOVERED.value,
            # A publisher that just linked to a direct competitor is by
            # definition on-topic for this organisation.
            relevance=0.9,
            discovered_via="competitor gap",
            notes=f"Linked to {alert.competitor}",
        )
        ctx.db.add(target)
        ctx.db.flush()
        return target


# ── Sending ────────────────────────────────────────────────────────────────
def _make_counter_sender(pitch_id: str, alert_id: str):  # noqa: ANN202
    def send(ctx: AgentContext) -> None:
        from app.agents.digital_pr_outreach.agent import _make_sender

        # Reuses the outreach agent's send path, so a counter-pitch and an
        # ordinary pitch go out through the same channel and bookkeeping.
        _make_sender(pitch_id)(ctx)

        alert = ctx.db.get(CompetitorAlert, alert_id)
        if alert is not None:
            alert.counter_launched = True
            alert.counter_pitch_id = pitch_id
        ctx.db.flush()

    return send


@register_applier("counter_pitch")
def apply_counter_pitch(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "competitor_link_monitor"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Competitor Link Monitor is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    _make_counter_sender(
        payload.get("pitch_id") or item.target_id or "", payload.get("alert_id") or ""
    )(ctx)


AGENT = CompetitorLinkMonitorAgent()
