"""Referral Spam Guard — blocks junk referrers before they pollute the data.

Ghost referrals, adult-spam domains and scraper farms inflate sessions, wreck
bounce rate, and quietly corrupt every decision made downstream — including
the ones this platform's own agents make from analytics. So this runs
continuously and independently of the rest of the SEO pipeline.

Classification is deliberately cheap-first: a large static ruleset and
behavioural heuristics catch the overwhelming majority for free, and only
genuinely ambiguous domains reach the model. Blocking is low impact and
reversible, so it proceeds autonomously under any guardrail short of
full human review.
"""
from __future__ import annotations

import uuid
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
from app.agents.referral_spam_guard import prompts, rules
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AnalyticsConnector, TrafficSample
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem
from app.models.seo import ReferralSpamEvent
from app.models.workspace import Organization
from app.services.approvals import register_applier

log = get_logger(__name__)

# Most ambiguous domains sent to the model in one pass. The actual number
# follows the remaining daily allowance (see llm_check_budget); each one is an
# LLM call, so this bound keeps a pass's cost predictable.
MIN_LLM_CHECKS = 5
MAX_LLM_CHECKS = 10


class ReferralSpamGuardAgent(BaseAgent):
    spec = AgentSpec(
        slug="referral_spam_guard",
        name="Referral Spam Guard",
        category=AgentCategory.SEO_AEO,
        description=(
            "Detects and blocks ghost, bot, and adult/spam referral traffic "
            "before it pollutes analytics and SEO signals."
        ),
        default_interval=timedelta(minutes=30),
        default_schedule="Hourly",
        required_capabilities=(Capability.READ_REFERRERS,),
        max_impact=Impact.LOW,
        default_max_actions_per_day=500,
        scope_placeholder="Extra domains to always block, comma separated",
        continuous=True,
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.READ_REFERRERS,
                UseMode.ONE,
                question=(
                    "Which analytics account should this agent check for spam visits?"
                ),
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Extra domains to always block."""
        return scope_rules.domains(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        analytics = self._analytics(ctx)
        if analytics is None:
            return AgentResult.skip("Waiting on an analytics connector")

        samples = analytics.read_referrers(days=1, limit=200)
        if not samples:
            return AgentResult.skip("No new referral traffic to assess")

        already_blocked = self._recently_blocked(ctx)
        extra_blocklist = rules.parse_scope(ctx.scope)

        verdicts: list[tuple[TrafficSample, str, float]] = []
        ambiguous: list[TrafficSample] = []
        seen: set[str] = set()

        for sample in samples:
            domain = rules.normalise_domain(sample.referrer)
            if not domain or domain in already_blocked or domain in seen:
                continue
            if rules.is_own_domain(domain, ctx.org.primary_domain):
                continue
            # "www.x.com/a" and "x.com/b" are one referrer, and one proposal.
            seen.add(domain)

            category, confidence = rules.classify(sample, extra_blocklist=extra_blocklist)
            if category is rules.Verdict.UNKNOWN:
                ambiguous.append(sample)
            elif category is not rules.Verdict.CLEAN:
                verdicts.append((sample, category.value, confidence))

        rule_matches = len(verdicts)

        # Only the genuinely uncertain ones cost a model call.
        to_check = ambiguous[: llm_check_budget(ctx.remaining_actions, rule_matches)]
        model_flagged = 0
        for sample in to_check:
            resolved = self._ask_model(ctx, sample)
            if resolved is not None:
                verdicts.append(resolved)
                model_flagged += 1

        actions = [
            self._block_action(ctx, sample, category, confidence)
            for sample, category, confidence in verdicts
        ]

        # Only blocks that were actually applied are in the event log. This
        # pass's proposals are reported separately: under review they are
        # waiting for a person, and counting them as blocked was untrue.
        blocked_today = ctx.db.execute(
            select(func.count())
            .select_from(ReferralSpamEvent)
            .where(
                ReferralSpamEvent.tenant_id == ctx.tenant_id,
                ReferralSpamEvent.at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
            )
        ).scalar_one()
        label = f"{blocked_today} spam referrers blocked today"
        if actions:
            label += f", {len(actions)} more proposed"

        return AgentResult(
            summary=f"assessed {len(samples)} referrers, proposing {len(actions)} blocks",
            actions=actions,
            metric_label=label,
            detail={
                "assessed": len(samples),
                "rule_matches": rule_matches,
                "model_checks": len(to_check),
                "model_flagged": model_flagged,
                "proposed": len(actions),
                "blocked_today": blocked_today,
            },
        )

    # ── Helpers ────────────────────────────────────────────────────────────
    def _analytics(self, ctx: AgentContext) -> AnalyticsConnector | None:
        return ctx.with_capability(Capability.READ_REFERRERS)  # type: ignore[return-value]

    def _recently_blocked(self, ctx: AgentContext) -> set[str]:
        """Domains already blocked in the last week, so nothing is re-logged."""
        cutoff = utcnow() - timedelta(days=7)
        rows = ctx.db.execute(
            select(ReferralSpamEvent.referrer).where(
                ReferralSpamEvent.tenant_id == ctx.tenant_id,
                ReferralSpamEvent.at >= cutoff,
            )
        )
        return {rules.normalise_domain(referrer) for (referrer,) in rows}

    def _ask_model(
        self, ctx: AgentContext, sample: TrafficSample
    ) -> tuple[TrafficSample, str, float] | None:
        try:
            data = ctx.ask_json(
                prompts.classify_referrer(
                    referrer=sample.referrer,
                    sessions=sample.sessions,
                    bounce_rate=sample.bounce_rate,
                    avg_seconds=sample.avg_session_seconds,
                ),
                system=prompts.SYSTEM,
                schema_hint=prompts.SCHEMA_HINT,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.log_warning("Referrer classification failed for %s: %s", sample.referrer, exc)
            return None

        if not data.get("is_spam"):
            return None
        confidence = float(data.get("confidence") or 0.0)
        # A model guess below this is left alone: wrongly excluding real
        # traffic is a worse error than letting one spam referrer through.
        if confidence < 0.75:
            return None
        return sample, str(data.get("category") or "Spam / link-drop bot"), confidence

    def _block_action(
        self, ctx: AgentContext, sample: TrafficSample, category: str, confidence: float
    ) -> AgentAction:
        domain = rules.normalise_domain(sample.referrer)
        return AgentAction(
            kind="block_referrer",
            title=f"Block referral spam from {domain}",
            impact=Impact.LOW,
            approval_type="Traffic Block",
            target_kind=TARGET_KIND,
            # One pending item per domain. Without a target id every proposal
            # had target_id NULL, so under review they all collapsed into one
            # approval card that kept being overwritten by the next domain.
            target_id=target_id_for(domain),
            payload={
                "referrer": domain,
                "category": category,
                "confidence": confidence,
                "sessions": sample.sessions,
            },
            apply=_make_blocker(domain, category, confidence, sample.sessions),
            audit=f"blocked {sample.sessions} sessions of {category.lower()} from {domain}",
            metrics={"referral_spam_blocked": 1},
        )


TARGET_KIND = "referral_spam"


def target_id_for(domain: str) -> str:
    """A stable id for one referrer domain.

    ApprovalItem.target_id is a UUID column, so the domain itself cannot go
    there; a name-based UUID of it is stable across runs, which is what
    keeps one pending proposal per domain.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TARGET_KIND}:{domain}"))


def llm_check_budget(remaining_actions: int, already_proposed: int) -> int:
    """How many ambiguous referrers to send to the model this pass.

    Each check is one LLM call that can produce at most one action, so the
    count follows the daily actions still available after the rule matches
    (which cost nothing), never fewer than MIN_LLM_CHECKS (the old fixed
    number: proposals queued for review do not use up the allowance) and
    never more than MAX_LLM_CHECKS, which bounds a pass's model cost.
    """
    room = max(0, remaining_actions - already_proposed)
    return max(MIN_LLM_CHECKS, min(MAX_LLM_CHECKS, room))


def _make_blocker(domain: str, category: str, confidence: float, sessions: int):  # noqa: ANN202
    def block(ctx: AgentContext) -> None:
        ctx.db.add(
            ReferralSpamEvent(
                tenant_id=ctx.tenant_id,
                at=utcnow(),
                referrer=domain,
                category=category,
                action="Blocked",
                sessions_affected=sessions,
                confidence=confidence,
            )
        )
        # Push the exclusion upstream so every analytics property stops
        # counting it at source, not just in this console.
        for connector in ctx.all_with_capability(Capability.READ_REFERRERS):
            try:
                connector.exclude_referrer(domain)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - local block still stands
                ctx.log_warning(
                    "Could not push exclusion to %s: %s", connector.slug, exc
                )
        ctx.db.flush()

    return block


@register_applier(TARGET_KIND)
def apply_block(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    """Approving a queued block does exactly what the autonomous path does."""
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    domain = rules.normalise_domain(str(payload.get("referrer") or ""))
    if not domain:
        raise ValueError("The approved block has no referrer stored")

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "referral_spam_guard"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Referral Spam Guard is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    _make_blocker(
        domain,
        str(payload.get("category") or rules.Verdict.LINK_DROP.value),
        float(payload.get("confidence") or 0.0),
        int(payload.get("sessions") or 0),
    )(ctx)


AGENT = ReferralSpamGuardAgent()
