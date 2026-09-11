"""Digital PR Outreach — writes and sends sentiment-tuned pitches.

This is the one agent that contacts real people who did not ask to be
contacted, so it is the most conservative in the fleet:

* sending is always **high impact** — even under full autonomy it respects a
  hard daily cap and a per-domain cooldown, because a burst of near-identical
  pitches to the same publication is how a brand gets blacklisted;
* every pitch is tuned to the target's own tone, and the variant chosen is
  recorded so reply rates can be compared later;
* a domain is only pitched once, then left alone until the cooldown lapses.

Drafting is separate from sending: a draft costs nothing and is reviewable.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base import scope as scope_rules
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.backlink_node_discovery.scoring import priority
from app.agents.base.style import humanise
from app.agents.digital_pr_outreach import prompts
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import NotificationConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.offpage import BacklinkStatus, BacklinkTarget, OutreachPitch
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx

log = get_logger(__name__)

# A domain contacted once is not contacted again for this long.
DOMAIN_COOLDOWN = timedelta(days=45)
# Hard ceiling per run regardless of the configured daily cap.
MAX_PITCHES_PER_RUN = 3


class DigitalPrOutreachAgent(BaseAgent):
    spec = AgentSpec(
        slug="digital_pr_outreach",
        name="Digital PR Outreach",
        category=AgentCategory.OFF_PAGE,
        description="Auto-generates sentiment-tuned pitches to webmasters.",
        default_interval=timedelta(hours=12),
        default_schedule="Daily",
        required_capabilities=(Capability.SEND_NOTIFICATION,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=5,
        scope_placeholder="Story angle — the claim the pitch is built on",
        requires_llm=True,
    )

    def validate_scope(self, scope: str) -> str | None:
        """A story angle — one line of free text."""
        return scope_rules.free_text(scope, max_items=3, label="angles")

    def run(self, ctx: AgentContext) -> AgentResult:
        # The angle is the claim the pitch is built on, and this agent emails
        # people who did not ask to be contacted. With no angle configured it
        # used to fall back to "an original data study from our own sales
        # data" — a statement about the customer's business that may simply
        # be untrue, offered to a publisher under the customer's name. There
        # is no honest default for this, so there is no default.
        if not self._angle(ctx):
            return AgentResult.skip(
                "Set the story angle on this agent before it pitches anyone. "
                "It is the claim every pitch is built on, and inventing one "
                "would offer a publisher something you may not have."
            )

        targets = self._pitchable_targets(ctx)
        if not targets:
            return AgentResult.skip("No qualified targets ready to pitch")

        actions: list[AgentAction] = []
        drafted = 0

        for target in targets[:MAX_PITCHES_PER_RUN]:
            pitch = self._draft(ctx, target)
            if pitch is None:
                continue
            drafted += 1
            actions.append(
                AgentAction(
                    kind="send_pitch",
                    title=f"{target.placement_type} pitch to {target.domain}",
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.PR_PITCH.value,
                    target_kind="outreach_pitch",
                    target_id=pitch.id,
                    payload={
                        "pitch_id": pitch.id,
                        "target_id": target.id,
                        "domain": target.domain,
                        "tone": pitch.tone,
                        "variant": pitch.variant,
                        # Approving this sends an email to a stranger under
                        # the customer's name. It carried the domain, a tone
                        # and a variant letter, and not one word of what
                        # would actually be sent.
                        "subject": ctx.cipher.decrypt(
                            pitch.subject_encrypted, context=Ctx.PITCH_SUBJECT
                        ),
                        "body": ctx.cipher.decrypt(
                            pitch.body_encrypted, context=Ctx.PITCH_BODY
                        ),
                    },
                    apply=_make_sender(pitch.id),
                    audit=f"sent outreach pitch to {target.domain}",
                )
            )

        total_drafted = ctx.db.execute(
            select(func.count())
            .select_from(OutreachPitch)
            .where(
                OutreachPitch.tenant_id == ctx.tenant_id,
                OutreachPitch.status.in_(["drafted", "queued"]),
            )
        ).scalar_one()

        return AgentResult(
            summary=f"drafted {drafted} pitches, {len(actions)} ready to send",
            actions=actions,
            metric_label=f"{total_drafted} pitches drafted",
            detail={"drafted": drafted, "candidates": len(targets)},
        )

    @staticmethod
    def _angle(ctx: AgentContext) -> str:
        """The story this agent pitches, or empty if nobody has chosen one."""
        return (ctx.scope or ctx.setting("angle", "") or "").strip()

    # ── Selection ──────────────────────────────────────────────────────────
    def _pitchable_targets(self, ctx: AgentContext) -> list[BacklinkTarget]:
        """Discovered targets with no recent contact, best fit first."""
        cutoff = utcnow() - DOMAIN_COOLDOWN
        recently_contacted = {
            domain
            for (domain,) in ctx.db.execute(
                select(OutreachPitch.target_domain).where(
                    OutreachPitch.tenant_id == ctx.tenant_id,
                    OutreachPitch.sent_at.is_not(None),
                    OutreachPitch.sent_at >= cutoff,
                )
            )
        }
        already_drafted = {
            target_id
            for (target_id,) in ctx.db.execute(
                select(OutreachPitch.target_id).where(
                    OutreachPitch.tenant_id == ctx.tenant_id,
                    OutreachPitch.status.in_(["drafted", "queued"]),
                )
            )
        }

        candidates = list(
            ctx.db.execute(
                select(BacklinkTarget).where(
                    BacklinkTarget.tenant_id == ctx.tenant_id,
                    BacklinkTarget.status == BacklinkStatus.DISCOVERED.value,
                )
            ).scalars()
        )
        eligible = [
            t
            for t in candidates
            if t.domain not in recently_contacted and t.id not in already_drafted
        ]
        eligible.sort(key=lambda t: priority(t.authority, t.relevance), reverse=True)
        return eligible

    # ── Drafting ───────────────────────────────────────────────────────────
    def _draft(self, ctx: AgentContext, target: BacklinkTarget) -> OutreachPitch | None:
        angle = self._angle(ctx)
        try:
            data = ctx.ask_json(
                prompts.write_pitch(
                    domain=target.domain,
                    placement_type=target.placement_type,
                    brand=ctx.org.name,
                    angle=angle,
                    authority=target.authority,
                    relevance=target.relevance,
                ),
                system=prompts.SYSTEM,
                schema_hint=prompts.SCHEMA_HINT,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.log_warning("Pitch drafting failed for %s: %s", target.domain, exc)
            return None

        subject = humanise((data.get("subject") or "").strip())
        body = humanise((data.get("body") or "").strip())
        if not subject or not body:
            return None

        problem = prompts.check_pitch(subject=subject, body=body, brand=ctx.org.name)
        if problem:
            # A pitch that reads as spam damages the brand more than a missing
            # link costs it, so a failed check is dropped rather than sent.
            ctx.log_warning("Rejected pitch to %s: %s", target.domain, problem)
            return None

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
        )
        ctx.db.add(pitch)
        ctx.db.flush()
        return pitch


# ── Sending ────────────────────────────────────────────────────────────────
def _make_sender(pitch_id: str):  # noqa: ANN202
    def send(ctx: AgentContext) -> None:
        pitch = ctx.db.get(OutreachPitch, pitch_id)
        if pitch is None or pitch.status == "sent":
            return
        target = ctx.db.get(BacklinkTarget, pitch.target_id)

        subject = ctx.cipher.decrypt(pitch.subject_encrypted, context=Ctx.PITCH_SUBJECT)
        body = ctx.cipher.decrypt(pitch.body_encrypted, context=Ctx.PITCH_BODY)
        recipient = (target.contact_email if target else "") or ""

        # Prefer real email; fall back to any notification channel so a
        # drafted pitch is never silently dropped.
        channel = ctx.with_capability(Capability.SEND_EMAIL) or ctx.with_capability(
            Capability.SEND_NOTIFICATION
        )
        if channel is None:
            from app.core.exceptions import ConnectorError

            raise ConnectorError("No email or messaging connector available to send this pitch")

        notifier: NotificationConnector = channel  # type: ignore[assignment]
        notifier.notify(
            subject=subject,
            message=prompts.render_email(
                body=body,
                recipient=recipient,
                brand=ctx.org.name,
                domain=(target.domain if target else ""),
            ),
            channel=recipient,
        )

        pitch.status = "sent"
        pitch.sent_at = utcnow()
        if target is not None:
            target.status = BacklinkStatus.PITCHED.value
        ctx.db.flush()

    return send


@register_applier("outreach_pitch")
def apply_pitch(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "digital_pr_outreach"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Digital PR Outreach is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    _make_sender(payload.get("pitch_id") or item.target_id or "")(ctx)


@register_rejecter("outreach_pitch")
def discard_pitch(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    """A rejected pitch is discarded and its target released for a rethink."""
    pitch = db.get(OutreachPitch, payload.get("pitch_id") or item.target_id or "")
    if pitch is None:
        return
    pitch.status = "rejected"
    target = db.get(BacklinkTarget, pitch.target_id)
    if target is not None and target.status == BacklinkStatus.PITCHED.value:
        target.status = BacklinkStatus.DISCOVERED.value
    db.flush()


AGENT = DigitalPrOutreachAgent()
