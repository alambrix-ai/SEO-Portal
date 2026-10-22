"""Competitor Link Monitor — watches rivals' backlinks and counter-pitches.

When a competitor earns a link from a domain, that domain has just proven two
things: it publishes on this subject, and it is currently willing to link out.
That is the best moment to approach it — so this agent detects the event and
drafts a *structurally superior* counter-pitch to the same publisher.

What counts as an opportunity is the **gap**: a site that links to the
competitor but not to the customer. A site that already links to both is not
news. So each run reads the backlinks of every competitor *and* of the
customer's own domain, from a source that can look up any domain (a
``READ_BACKLINKS`` connector such as DataForSEO). It used to call
``read_backlinks(limit=50)`` with no domain at all, so whatever the source
returned about the customer's own site was filed under every competitor.

Competitors are therefore configured as domains. A company name cannot be
looked up in a backlink index.

The counter-pitch is deliberately not a copy of the competitor's angle;
pitching the same story a week later gets ignored. The prompt asks for
something that beats it on substance, and sending stays high impact.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.backlink_node_discovery.scoring import normalise_domain
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
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.competitor_link_monitor import prompts
from app.agents.digital_pr_outreach import agent as outreach
from app.agents.digital_pr_outreach import prompts as pitch_prompts
from app.connectors.base.connector import Capability
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert, OutreachPitch
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx

log = get_logger(__name__)

MAX_COMPETITORS = 10
MAX_COUNTERS_PER_RUN = 2
#: Linking domains read per competitor.
COMPETITOR_LINKS_LIMIT = 100
#: Linking domains read for the customer's own site — the set a competitor's
#: link is compared against. The source returns its strongest first, so a
#: site with more referring domains than this can see an occasional false
#: "gap" from a weak site that does in fact link to it.
OWN_LINKS_LIMIT = 1000
# The same publisher is not counter-pitched twice inside this window.
PUBLISHER_COOLDOWN = timedelta(days=30)


def _related(domain: str, other: str) -> bool:
    """The same site, or a subdomain of it."""
    return bool(other) and (domain == other or domain.endswith("." + other))


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
        scope_placeholder="Competitor domains, comma separated (e.g. rival.com, other.co.uk)",
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.READ_BACKLINKS,
                UseMode.ONE,
                question=(
                    "Which backlink index should this agent check competitors with?"
                ),
            ),
            ConnectorUse(
                Capability.SEND_NOTIFICATION,
                UseMode.ONE,
                question=(
                    "Which channel should draft counter-pitches be sent to your team "
                    "through?"
                ),
            ),
            ConnectorUse(
                Capability.SEND_EMAIL,
                UseMode.ONE,
                question=(
                    "Which email account should counter-pitches to publishers be sent "
                    "from?"
                ),
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Competitor domains. Names are refused: they cannot be looked up."""
        problem = scope_rules.domains(scope)
        if problem:
            return problem
        entries = scope_rules.items(scope)
        for entry in entries:
            if "." not in entry:
                return (
                    f"{entry!r} looks like a company name. Enter each competitor's "
                    "website domain instead, such as rival.com — backlinks can only "
                    "be looked up by domain."
                )
        if len(entries) > MAX_COMPETITORS:
            return (
                f"That is {len(entries)} competitors; keep it to {MAX_COMPETITORS} "
                "or fewer."
            )
        return None

    def run(self, ctx: AgentContext) -> AgentResult:
        competitors, names = self._competitors(ctx)
        if not competitors:
            if names:
                return AgentResult.skip(
                    "Competitors are set as names ("
                    + ", ".join(names[:3])
                    + "), which cannot be looked up. Enter their website domains "
                    "instead, such as rival.com, in Configure."
                )
            return AgentResult.skip(
                "No competitor domains configured — add them in Configure, such as rival.com"
            )

        own = normalise_domain(ctx.org.primary_domain or "")
        if not own:
            return AgentResult.skip(
                "Set your website's domain on the workspace first. A new competitor "
                "link is a site that links to them but not to you, so your own "
                "links have to be checked too."
            )

        new_alerts, problem = self._detect(ctx, competitors, own)
        outreach.release_stale_drafts(ctx, counter=True)
        actions = self._counter_pitch(ctx)

        recent = self._recent_alert_count(ctx)
        summary = (
            f"{new_alerts} new competitor links (sites linking to them but not to you), "
            f"{len(actions)} counter-pitches drafted"
        )
        if problem:
            summary = f"{summary}. {problem}"
        if names:
            summary += f". Ignored {len(names)} competitor name(s) without a domain"

        return AgentResult(
            summary=summary,
            actions=actions,
            metric_label=f"{recent} competitor-only links found this week",
            detail={
                "new_alerts": new_alerts,
                "competitors": competitors,
                "ignored_names": names,
                "own_domain": own,
            },
        )

    # ── Configuration ──────────────────────────────────────────────────────
    def _competitors(self, ctx: AgentContext) -> tuple[list[str], list[str]]:
        """``(domains, names)``: what can be looked up, and what cannot."""
        if ctx.scope:
            raw = scope_rules.items(ctx.scope)
        else:
            raw = [str(c) for c in (ctx.setting("competitors") or [])]
        own = normalise_domain(ctx.org.primary_domain or "")
        domains: list[str] = []
        names: list[str] = []
        for entry in raw:
            domain = normalise_domain(entry)
            if "." not in domain:
                if entry.strip():
                    names.append(entry.strip())
                continue
            if domain not in domains and not _related(domain, own):
                domains.append(domain)
        return domains[:MAX_COMPETITORS], names

    # ── Detection ──────────────────────────────────────────────────────────
    def _detect(
        self, ctx: AgentContext, competitors: list[str], own: str
    ) -> tuple[int, str]:
        """Record sites linking to a competitor but not to ``own``.

        Returns ``(new alerts, problem)``; the problem is a sentence for the
        summary when detection could not run, so a failure does not read as
        "nothing new".
        """
        source = ctx.with_capability(Capability.READ_BACKLINKS)
        if source is None:
            # Without a backlink data source there is nothing factual to
            # report, and inventing competitor activity would put fabricated
            # alerts in front of an operator. So the run stays quiet.
            ctx.log_info("No backlink data source connected; detection skipped")
            return 0, "No backlink data source is connected, so nothing was checked"

        try:
            own_rows = source.read_backlinks(domain=own, limit=OWN_LINKS_LIMIT)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            # Without the customer's own links every competitor link would look
            # like a gap, so nothing is recorded.
            ctx.log_warning("Could not read backlinks for %s: %s", own, exc)
            return 0, f"Could not read your own backlinks, so no comparison was made ({exc})"
        own_linkers = {normalise_domain(str(row.get("domain") or "")) for row in own_rows}

        known = self._known_pairs(ctx)
        discovered = 0
        problem = ""

        for competitor in competitors:
            try:
                rows = source.read_backlinks(domain=competitor, limit=COMPETITOR_LINKS_LIMIT)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Backlink source unavailable for %s: %s", competitor, exc)
                problem = f"Stopped at {competitor}: the backlink source failed ({exc})"
                break

            for row in rows:
                domain = normalise_domain(str(row.get("domain") or ""))
                if (
                    not domain
                    or domain in own_linkers
                    or _related(domain, own)
                    or _related(domain, competitor)
                    or (competitor, domain) in known
                ):
                    continue
                known.add((competitor, domain))
                authority = int(row.get("authority") or 0)
                strength = f" (authority {authority})" if authority else ""
                ctx.db.add(
                    CompetitorAlert(
                        tenant_id=ctx.tenant_id,
                        competitor=competitor,
                        source_domain=domain,
                        authority=authority,
                        text=f"{domain} links to {competitor} but not to you{strength}.",
                        detected_at=utcnow(),
                        detail={
                            "anchor": row.get("anchor") or "",
                            "url": row.get("url") or "",
                            "url_to": row.get("url_to") or "",
                            "dofollow": row.get("dofollow"),
                            "first_seen": row.get("first_seen") or "",
                            "authority_measured": bool(authority),
                        },
                    )
                )
                discovered += 1

        if discovered:
            ctx.db.flush()
        return discovered, problem

    @staticmethod
    def _known_pairs(ctx: AgentContext) -> set[tuple[str, str]]:
        return {
            (competitor, domain)
            for competitor, domain in ctx.db.execute(
                select(CompetitorAlert.competitor, CompetitorAlert.source_domain).where(
                    CompetitorAlert.tenant_id == ctx.tenant_id
                )
            )
        }

    @staticmethod
    def _recent_alert_count(ctx: AgentContext) -> int:
        return len(
            list(
                ctx.db.execute(
                    select(CompetitorAlert.id).where(
                        CompetitorAlert.tenant_id == ctx.tenant_id,
                        CompetitorAlert.detected_at >= utcnow() - timedelta(days=7),
                    )
                )
            )
        )

    # ── Counter-pitching ───────────────────────────────────────────────────
    def _counter_pitch(self, ctx: AgentContext) -> list[AgentAction]:
        alerts = self._alerts_to_answer(ctx)
        # Scope holds the competitor list for this agent, so the angle only
        # ever comes from settings. The old `ctx.scope and "" or setting(...)`
        # evaluated to the setting either way; it now says so.
        angle = str(ctx.setting("counter_angle", "") or "").strip()
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
                status=outreach.DRAFTED,
                is_counter_pitch=True,
            )
            ctx.db.add(pitch)
            ctx.db.flush()

            delivery, recipient = outreach.plan_delivery(ctx, target)
            if delivery == outreach.DELIVERY_PUBLISHER:
                title = (
                    f"Counter-pitch email to {pitch_prompts.mask_email(recipient)} at "
                    f"{alert.source_domain} (vs {alert.competitor})"
                )
            else:
                title = (
                    f"Draft counter-pitch for {alert.source_domain} (vs {alert.competitor}), "
                    "to your team, no publisher address"
                )

            actions.append(
                AgentAction(
                    kind="send_counter_pitch",
                    title=title,
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.PR_PITCH.value,
                    target_kind="counter_pitch",
                    target_id=pitch.id,
                    payload={
                        "pitch_id": pitch.id,
                        "alert_id": alert.id,
                        "target_id": target.id,
                        "publisher": alert.source_domain,
                        "competitor": alert.competitor,
                        "delivery": delivery,
                        "recipient": recipient,
                        "delivery_note": outreach.describe_delivery(
                            delivery, recipient, alert.source_domain
                        ),
                        # What would actually be sent, as for ordinary pitches.
                        "subject": subject,
                        "body": body,
                    },
                    apply=_make_counter_sender(
                        pitch.id, alert.id, delivery=delivery, recipient=recipient
                    ),
                    audit=(
                        f"launched a counter-pitch to {alert.source_domain} after "
                        f"{alert.competitor} earned a link there"
                        if delivery == outreach.DELIVERY_PUBLISHER
                        else f"sent a draft counter-pitch for {alert.source_domain} to the "
                        "team (no publisher address)"
                    ),
                )
            )

        return actions

    def _alerts_to_answer(self, ctx: AgentContext) -> list[CompetitorAlert]:
        """Unanswered alerts, highest-authority publisher first.

        Excluded: publishers pitched inside the cooldown, publishers with a
        pitch still awaiting approval (however old — the cooldown lapsing used
        to queue a second approval beside the first), alerts already answered
        with a team draft, and alerts whose counter-pitch was rejected.
        """
        cutoff = utcnow() - PUBLISHER_COOLDOWN
        blocked = {
            domain
            for domain, created_at, status in ctx.db.execute(
                select(
                    OutreachPitch.target_domain,
                    OutreachPitch.created_at,
                    OutreachPitch.status,
                ).where(OutreachPitch.tenant_id == ctx.tenant_id)
            )
            if (created_at is not None and created_at >= cutoff)
            or status in outreach.OPEN_STATUSES
        }
        alerts = list(
            ctx.db.execute(
                select(CompetitorAlert)
                .where(
                    CompetitorAlert.tenant_id == ctx.tenant_id,
                    CompetitorAlert.counter_launched.is_(False),
                    CompetitorAlert.counter_pitch_id.is_(None),
                )
                .order_by(CompetitorAlert.authority.desc())
            ).scalars()
        )
        return [
            a
            for a in alerts
            if a.source_domain not in blocked and not (a.detail or {}).get("counter_rejected")
        ]

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
def _make_counter_sender(
    pitch_id: str, alert_id: str, *, delivery: str | None = None, recipient: str | None = None
):  # noqa: ANN202
    def send(ctx: AgentContext) -> None:
        # Reuses the outreach agent's send path, so a counter-pitch and an
        # ordinary pitch go out through the same channel and bookkeeping —
        # including the rule that a pitch with no publisher address goes to
        # the team as a draft and is not recorded as sent.
        delivered = outreach._make_sender(pitch_id, delivery=delivery, recipient=recipient)(ctx)

        alert = ctx.db.get(CompetitorAlert, alert_id)
        if alert is not None and delivered:
            alert.counter_pitch_id = pitch_id
            # "Launched" means the publisher received it.
            alert.counter_launched = delivered == outreach.DELIVERY_PUBLISHER
            alert.detail = {**(alert.detail or {}), "counter_delivery": delivered}
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
        payload.get("pitch_id") or item.target_id or "",
        payload.get("alert_id") or "",
        delivery=payload.get("delivery"),
        recipient=payload.get("recipient"),
    )(ctx)


@register_rejecter("counter_pitch")
def discard_counter_pitch(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    """Discard the pitch and stop answering that alert.

    Without this a rejected counter-pitch stayed "drafted" and its alert stayed
    open, so the same publisher was drafted again once the cooldown lapsed.
    """
    outreach.discard_pitch(db, org, item, payload)
    alert = db.get(CompetitorAlert, payload.get("alert_id") or "")
    if alert is not None:
        alert.detail = {**(alert.detail or {}), "counter_rejected": True}
    db.flush()


AGENT = CompetitorLinkMonitorAgent()
