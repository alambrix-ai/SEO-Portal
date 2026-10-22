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

A pitch reaches the publisher only when the target has a contact address
(entered by a person — see ``PUT /offpage/targets/{id}/contact``) and an email
connector is set up. Otherwise it is delivered to the team's own channel as a
draft and recorded as ``team_draft``: it used to be recorded as "sent", with
the target marked Pitched, when nobody at the publication had received a word.
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
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.backlink_node_discovery.scoring import priority
from app.agents.base.style import humanise
from app.agents.digital_pr_outreach import prompts
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import NotificationConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.offpage import BacklinkStatus, BacklinkTarget, OutreachPitch
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx

log = get_logger(__name__)

# A domain contacted once is not contacted again for this long.
DOMAIN_COOLDOWN = timedelta(days=45)
# Hard ceiling per run regardless of the configured daily cap.
MAX_PITCHES_PER_RUN = 3

# ── Pitch statuses ─────────────────────────────────────────────────────────
DRAFTED = "drafted"
#: Delivered to the publisher by email.
SENT = "sent"
#: Delivered to the team's own channel as a draft, because the target has no
#: contact address (or no mail connector is set up). Nobody outside the
#: workspace received it, so the target is *not* marked Pitched.
TEAM_DRAFT = "team_draft"
#: Drafted, but its approval expired (or it was never queued) — released so
#: the target can be pitched again instead of sitting in "drafted" forever.
EXPIRED = "expired"
REJECTED = "rejected"
OPEN_STATUSES = (DRAFTED, "queued")

# ── Where an approved pitch goes ───────────────────────────────────────────
DELIVERY_PUBLISHER = "publisher"
DELIVERY_TEAM = "team"
PITCH_TARGET_KINDS = ("outreach_pitch", "counter_pitch")


def plan_delivery(ctx: AgentContext, target: BacklinkTarget | None) -> tuple[str, str]:
    """``(delivery, recipient)`` for a pitch to ``target``, as things stand.

    Only a target with a contact address *and* a connected mail connector is
    emailed. Anything else goes to the team as a draft — and is recorded as
    exactly that, not as "sent".
    """
    address = ((target.contact_email if target is not None else "") or "").strip()
    if address and ctx.with_capability(Capability.SEND_EMAIL) is not None:
        return DELIVERY_PUBLISHER, address
    return DELIVERY_TEAM, ""


def describe_delivery(delivery: str, recipient: str, domain: str) -> str:
    """One line for the approval card: what approving this will actually do."""
    if delivery == DELIVERY_PUBLISHER:
        return f"Approving emails this pitch to {prompts.mask_email(recipient)} at {domain}."
    return (
        f"Approving sends this pitch to your team as a draft. {domain} has no "
        "contact address (or no email connector is set up), so nobody at the "
        "publication receives it. Add the editor's address to send it to them."
    )


def pending_pitch_ids(db: Session, tenant_id: str) -> set[str]:
    """Pitches whose approval is still waiting for a person."""
    return {
        target_id
        for (target_id,) in db.execute(
            select(ApprovalItem.target_id).where(
                ApprovalItem.tenant_id == tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
                ApprovalItem.target_kind.in_(PITCH_TARGET_KINDS),
            )
        )
        if target_id
    }


def release_stale_drafts(ctx: AgentContext, *, counter: bool | None = None) -> int:
    """Expire drafted pitches that nothing is waiting on.

    A draft stays "drafted" until its approval is decided. When the approval
    expired instead — or the action was held by the daily cap and never
    queued — the draft used to block its target for good. Called at the start
    of a run, before anything new is drafted, so a pitch from this run is
    never mistaken for an orphan.
    """
    pending = pending_pitch_ids(ctx.db, ctx.tenant_id)
    stmt = select(OutreachPitch).where(
        OutreachPitch.tenant_id == ctx.tenant_id,
        OutreachPitch.status.in_(OPEN_STATUSES),
    )
    if counter is not None:
        stmt = stmt.where(OutreachPitch.is_counter_pitch.is_(counter))
    released = 0
    for pitch in ctx.db.execute(stmt).scalars():
        if pitch.id not in pending:
            pitch.status = EXPIRED
            released += 1
    if released:
        ctx.db.flush()
    return released


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
        scope_placeholder="Story angles — the claim each pitch is built on, comma separated",
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.SEND_NOTIFICATION,
                UseMode.ONE,
                question=(
                    "Which channel should draft pitches be sent to your team through?"
                ),
            ),
            ConnectorUse(
                Capability.SEND_EMAIL,
                UseMode.ONE,
                question=(
                    "Which email account should pitches to publishers be sent from?"
                ),
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Up to three story angles; each pitch uses one of them."""
        return scope_rules.free_text(scope, max_items=3, label="angles")

    def run(self, ctx: AgentContext) -> AgentResult:
        # The angle is the claim the pitch is built on, and this agent emails
        # people who did not ask to be contacted. With no angle configured it
        # used to fall back to "an original data study from our own sales
        # data" — a statement about the customer's business that may simply
        # be untrue, offered to a publisher under the customer's name. There
        # is no honest default for this, so there is no default.
        angles = self._angles(ctx)
        if not angles:
            return AgentResult.skip(
                "Set the story angle on this agent before it pitches anyone. "
                "It is the claim every pitch is built on, and inventing one "
                "would offer a publisher something you may not have."
            )

        released = release_stale_drafts(ctx, counter=False)
        targets = self._pitchable_targets(ctx)
        if not targets:
            return AgentResult.skip("No qualified targets ready to pitch")

        actions: list[AgentAction] = []
        to_publisher = to_team = 0
        # Several angles rotate across pitches — one claim per email, rather
        # than the whole comma-separated list pasted into every prompt.
        offset = self._angle_offset(ctx)

        for target in targets[:MAX_PITCHES_PER_RUN]:
            angle = angles[(offset + len(actions)) % len(angles)]
            pitch = self._draft(ctx, target, angle=angle)
            if pitch is None:
                continue
            delivery, recipient = plan_delivery(ctx, target)
            placement = target.placement_type.lower()
            if delivery == DELIVERY_PUBLISHER:
                to_publisher += 1
                title = (
                    f"Email {placement} pitch to {prompts.mask_email(recipient)} "
                    f"at {target.domain}"
                )
                audit_line = f"emailed an outreach pitch to {target.domain}"
            else:
                to_team += 1
                title = (
                    f"Draft {placement} pitch for {target.domain} "
                    "(to your team, no publisher address)"
                )
                audit_line = (
                    f"sent a draft pitch for {target.domain} to the team "
                    "(no publisher address)"
                )
            actions.append(
                AgentAction(
                    kind="send_pitch",
                    title=title,
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
                        "angle": angle,
                        # Who receives it if this is approved. The sender never
                        # goes further than this: an approval that said "to
                        # your team" cannot become an email to a stranger
                        # because an address was added afterwards.
                        "delivery": delivery,
                        "recipient": recipient,
                        "delivery_note": describe_delivery(delivery, recipient, target.domain),
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
                    apply=_make_sender(pitch.id, delivery=delivery, recipient=recipient),
                    audit=audit_line,
                )
            )

        summary = f"drafted {len(actions)} pitches: {to_publisher} to publishers"
        if to_team:
            summary += f", {to_team} as drafts for your team (no contact address)"

        return AgentResult(
            summary=summary,
            actions=actions,
            metric_label=f"{self._open_draft_count(ctx)} pitches drafted",
            detail={
                "drafted": len(actions),
                "to_publisher": to_publisher,
                "to_team": to_team,
                "released_expired_drafts": released,
                "candidates": len(targets),
            },
        )

    @staticmethod
    def _angles(ctx: AgentContext) -> list[str]:
        """The stories this agent pitches, or empty if nobody has chosen one."""
        return scope_rules.items((ctx.scope or ctx.setting("angle", "") or "").strip())

    @staticmethod
    def _angle(ctx: AgentContext) -> str:
        """The first configured angle, or empty."""
        angles = DigitalPrOutreachAgent._angles(ctx)
        return angles[0] if angles else ""

    @staticmethod
    def _angle_offset(ctx: AgentContext) -> int:
        """Where the angle rotation starts: one step per pitch ever drafted."""
        return int(
            ctx.db.execute(
                select(func.count())
                .select_from(OutreachPitch)
                .where(
                    OutreachPitch.tenant_id == ctx.tenant_id,
                    OutreachPitch.is_counter_pitch.is_(False),
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    def _open_draft_count(ctx: AgentContext) -> int:
        return ctx.db.execute(
            select(func.count())
            .select_from(OutreachPitch)
            .where(
                OutreachPitch.tenant_id == ctx.tenant_id,
                OutreachPitch.status.in_(OPEN_STATUSES),
            )
        ).scalar_one()

    # ── Selection ──────────────────────────────────────────────────────────
    def _pitchable_targets(self, ctx: AgentContext) -> list[BacklinkTarget]:
        """Discovered targets with no recent contact, best fit first.

        A target whose pitch already went to the team as a draft is left
        alone — re-drafting it every run would repeat the same message — until
        somebody adds its contact address, when it can be pitched for real.
        """
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
        already_drafted: set[str] = set()
        team_drafted: set[str] = set()
        for target_id, status in ctx.db.execute(
            select(OutreachPitch.target_id, OutreachPitch.status).where(
                OutreachPitch.tenant_id == ctx.tenant_id,
                OutreachPitch.status.in_((*OPEN_STATUSES, TEAM_DRAFT)),
            )
        ):
            (team_drafted if status == TEAM_DRAFT else already_drafted).add(target_id)

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
            if t.domain not in recently_contacted
            and t.id not in already_drafted
            and (t.id not in team_drafted or plan_delivery(ctx, t)[0] == DELIVERY_PUBLISHER)
        ]
        eligible.sort(key=lambda t: priority(t.authority, t.relevance), reverse=True)
        return eligible

    # ── Drafting ───────────────────────────────────────────────────────────
    def _draft(
        self, ctx: AgentContext, target: BacklinkTarget, *, angle: str
    ) -> OutreachPitch | None:
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
            status=DRAFTED,
        )
        ctx.db.add(pitch)
        ctx.db.flush()
        return pitch


# ── Sending ────────────────────────────────────────────────────────────────
def _make_sender(
    pitch_id: str, *, delivery: str | None = None, recipient: str | None = None
):  # noqa: ANN202
    """Deliver a pitch the way its approval said it would be delivered.

    ``delivery``/``recipient`` are what the reviewer was shown. The pitch only
    goes to a publisher when that is what was approved, the target still has
    that same address, and a mail connector is connected; in every other case
    it goes to the team as a draft. An approval with no recorded delivery
    (queued before this existed) is a team draft, because nobody approved
    emailing a stranger.

    The returned callable reports the delivery that happened ("" for none).
    """

    def send(ctx: AgentContext) -> str:
        pitch = ctx.db.get(OutreachPitch, pitch_id)
        if pitch is None or pitch.status in (SENT, TEAM_DRAFT):
            return ""
        target = ctx.db.get(BacklinkTarget, pitch.target_id)
        domain = target.domain if target is not None else pitch.target_domain

        subject = ctx.cipher.decrypt(pitch.subject_encrypted, context=Ctx.PITCH_SUBJECT)
        body = ctx.cipher.decrypt(pitch.body_encrypted, context=Ctx.PITCH_BODY)

        mode, address = plan_delivery(ctx, target)
        if delivery != DELIVERY_PUBLISHER or address != (recipient or ""):
            mode, address = DELIVERY_TEAM, ""

        if mode == DELIVERY_PUBLISHER:
            mailer: NotificationConnector = ctx.with_capability(Capability.SEND_EMAIL)  # type: ignore[assignment]
            mailer.notify(
                subject=subject,
                message=prompts.render_email(
                    body=body, recipient=address, brand=ctx.org.name, domain=domain
                ),
                channel=address,
            )
            pitch.status = SENT
            pitch.sent_at = utcnow()
            if target is not None:
                target.status = BacklinkStatus.PITCHED.value
            ctx.db.flush()
            return DELIVERY_PUBLISHER

        channel = ctx.with_capability(Capability.SEND_NOTIFICATION) or ctx.with_capability(
            Capability.SEND_EMAIL
        )
        if channel is None:
            from app.core.exceptions import ConnectorError

            raise ConnectorError("No email or messaging connector available to deliver this pitch")
        team: NotificationConnector = channel  # type: ignore[assignment]
        # No channel argument: this is the team's own channel (the SMTP
        # connector's "Send notifications to" address, or Slack's default).
        team.notify(
            subject=f"Draft pitch for {domain} (not sent): {subject}",
            message=prompts.render_email(body=body, recipient="", brand=ctx.org.name, domain=domain),
        )
        # Recorded as what it is. This used to be "sent", with the target
        # marked Pitched, when nobody at the publication had received it.
        pitch.status = TEAM_DRAFT
        ctx.db.flush()
        return DELIVERY_TEAM

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
    _make_sender(
        payload.get("pitch_id") or item.target_id or "",
        delivery=payload.get("delivery"),
        recipient=payload.get("recipient"),
    )(ctx)


@register_rejecter("outreach_pitch")
def discard_pitch(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    """A rejected pitch is discarded and its target released for a rethink."""
    pitch = db.get(OutreachPitch, payload.get("pitch_id") or item.target_id or "")
    if pitch is None:
        return
    pitch.status = REJECTED
    target = db.get(BacklinkTarget, pitch.target_id)
    if target is not None and target.status == BacklinkStatus.PITCHED.value:
        target.status = BacklinkStatus.DISCOVERED.value
    db.flush()


AGENT = DigitalPrOutreachAgent()
