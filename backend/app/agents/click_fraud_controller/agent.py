"""Click-Fraud Controller — drops non-human traffic in real time.

Runs continuously across every connected ad platform and exchange. Detection
is heuristic-first (see ``detectors.py``): datacentre origins, impossible
engagement timings, duplicate fingerprints and click-flooding are decidable
from the signals themselves, cheaply and explainably, and only genuinely
ambiguous sources cost a model call.

Blocking a placement is low impact — it stops spend rather than commits it, and
it is reversible — so it proceeds autonomously under every guardrail short of
full human review. The saving is recorded per event, which is what makes the
"Fraud Blocked" KPI and the spend-saved figure real numbers.
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
from app.agents.click_fraud_controller import detectors, prompts
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AdsConnector, TrafficSample
from app.core.logging import get_logger
from app.core.money import money
from app.db.base import utcnow
from app.models.ads import CHANNEL_LABELS, BudgetAllocation, FraudEvent
from app.models.approval import ApprovalItem
from app.models.workspace import Organization
from app.services.approvals import register_applier

log = get_logger(__name__)

# Most ambiguous sources sent to the model in one pass; the actual number
# follows the remaining daily allowance (see llm_check_budget).
MIN_LLM_CHECKS = 4
MAX_LLM_CHECKS = 8

TARGET_KIND = "fraud_event"
# How a saving's cost per click was arrived at. Only "measured" is a real
# figure; the other two make the saving an estimate, and it is labelled so.
CPC_MEASURED = "measured"  # platform spend / clicks
CPC_ESTIMATED = "estimated_from_cac"  # CAC x CPC_FROM_CAC_RATIO
CPC_PLACEHOLDER = "placeholder"  # FALLBACK_CPC
# A source blocked once is not re-assessed for this long.
BLOCK_MEMORY = timedelta(days=14)


class ClickFraudControllerAgent(BaseAgent):
    spec = AgentSpec(
        slug="click_fraud_controller",
        name="Click-Fraud Controller",
        category=AgentCategory.ADS,
        description="Flags and drops non-human traffic in real time.",
        default_interval=timedelta(minutes=20),
        default_schedule="Hourly",
        required_capabilities=(Capability.READ_TRAFFIC_QUALITY,),
        max_impact=Impact.LOW,
        default_max_actions_per_day=500,
        scope_placeholder="Sources to always block, comma separated",
        continuous=True,
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.READ_TRAFFIC_QUALITY,
                UseMode.ALL,
                question="Which ad platforms should this agent check for fake clicks?",
                group="ad platforms",
            ),
            ConnectorUse(
                Capability.READ_AD_PERFORMANCE,
                UseMode.ALL,
                group="ad platforms",
                follows=Capability.READ_TRAFFIC_QUALITY,
            ),
            ConnectorUse(
                Capability.BLOCK_PLACEMENT,
                UseMode.ALL,
                group="ad platforms that can block",
                follows=Capability.READ_TRAFFIC_QUALITY,
                selected="blocks sources on {names}",
                note=(
                    "A source is blocked only on the ad platform its clicks came from."
                ),
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Traffic sources to always block."""
        return scope_rules.free_text(scope, max_items=40, label="sources")

    def run(self, ctx: AgentContext) -> AgentResult:
        samples = self._sample_traffic(ctx)
        if not samples:
            return AgentResult.skip("No new ad traffic to assess")

        blocked_recently = self._recently_blocked(ctx)
        always_block = detectors.parse_scope(ctx.scope)
        cpc_by_channel = self._cpc_by_channel(ctx)

        verdicts: list[tuple[TrafficSample, str, float]] = []
        ambiguous: list[TrafficSample] = []
        seen: set[tuple[str, str]] = set()

        for sample in samples:
            if sample.source in blocked_recently:
                continue
            key = (sample.channel, sample.source)
            if key in seen:
                continue
            seen.add(key)

            verdict, confidence, reason = detectors.assess(
                sample, always_block=always_block
            )
            if verdict is detectors.Verdict.FRAUD:
                verdicts.append((sample, reason, confidence))
            elif verdict is detectors.Verdict.UNCERTAIN:
                ambiguous.append(sample)

        heuristic_blocks = len(verdicts)
        to_check = ambiguous[: llm_check_budget(ctx.remaining_actions, heuristic_blocks)]
        model_flagged = 0
        for sample in to_check:
            resolved = self._ask_model(ctx, sample)
            if resolved is not None:
                verdicts.append(resolved)
                model_flagged += 1

        actions = []
        total_saved = 0.0
        estimated = False
        for sample, reason, confidence in verdicts:
            cpc, basis = cpc_by_channel.get(
                sample.channel, (detectors.FALLBACK_CPC, CPC_PLACEHOLDER)
            )
            estimated = estimated or basis != CPC_MEASURED
            saved = round(sample.sessions * cpc, 2)
            total_saved += saved
            action = self._block_action(sample, reason, confidence, saved, basis)
            # The platform the block would be pushed to, for the
            # unverified-connector rule.
            action.writes_via = tuple(c.slug for c in _block_targets(ctx, sample.channel))
            actions.append(action)

        # Applied blocks only; this pass's proposals are reported separately
        # because under review they are still waiting for a person.
        blocked_today = ctx.db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(
                FraudEvent.tenant_id == ctx.tenant_id,
                FraudEvent.at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
            )
        ).scalar_one()
        label = f"{blocked_today} blocked today"
        if actions:
            label += f", {len(actions)} more proposed"
        saved_text = money(total_saved, decimals=2)
        saved_text = f"~{saved_text} est. saved" if estimated else f"{saved_text} saved"

        return AgentResult(
            summary=(
                f"assessed {len(samples)} sources, proposing {len(actions)} blocks "
                f"({saved_text})"
            ),
            actions=actions,
            metric_label=label,
            detail={
                "assessed": len(samples),
                "heuristic_blocks": heuristic_blocks,
                "model_checks": len(to_check),
                "model_flagged": model_flagged,
                "proposed": len(actions),
                "blocked_today": blocked_today,
                "spend_saved": round(total_saved, 2),
                "spend_saved_is_estimate": estimated,
            },
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _sample_traffic(self, ctx: AgentContext) -> list[TrafficSample]:
        samples: list[TrafficSample] = []
        for connector in ctx.all_with_capability(Capability.READ_TRAFFIC_QUALITY):
            ads: AdsConnector = connector  # type: ignore[assignment]
            try:
                batch = ads.sample_traffic(limit=80)
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning(
                    "Traffic sample failed for %s: %s", connector.slug, exc
                )
                continue
            for sample in batch:
                # The block goes back to the platform the traffic came from,
                # so every sample must know its channel.
                if not sample.channel:
                    sample.channel = getattr(ads, "channel", "") or ""
            samples.extend(batch)
        return samples

    def _recently_blocked(self, ctx: AgentContext) -> set[str]:
        cutoff = utcnow() - BLOCK_MEMORY
        return {
            source
            for (source,) in ctx.db.execute(
                select(FraudEvent.source).where(
                    FraudEvent.tenant_id == ctx.tenant_id, FraudEvent.at >= cutoff
                )
            )
        }

    def _cpc_by_channel(self, ctx: AgentContext) -> dict[str, tuple[float, str]]:
        """Cost per click per channel, with how it was arrived at.

        Preferred: the platform's own spend / clicks for the last week, which
        is a real CPC. Failing that, a rough estimate from the channel's CAC
        (``CPC_FROM_CAC_RATIO`` of it), which is a guess and is labelled as
        one. Channels with neither fall back to ``FALLBACK_CPC`` in run().
        """
        cpc: dict[str, tuple[float, str]] = {}
        for row in ctx.db.execute(
            select(BudgetAllocation).where(BudgetAllocation.tenant_id == ctx.tenant_id)
        ).scalars():
            if row.spend > 0 and row.conversions > 0:
                estimate = (row.spend / row.conversions) * detectors.CPC_FROM_CAC_RATIO
                cpc[row.channel] = (round(estimate, 4), CPC_ESTIMATED)

        for connector in ctx.all_with_capability(Capability.READ_AD_PERFORMANCE):
            channel = getattr(connector, "channel", "") or ""
            if not channel:
                continue
            try:
                performance = connector.read_performance(days=7)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - an estimate still stands
                ctx.log_warning("CPC read failed for %s: %s", connector.slug, exc)
                continue
            if performance.clicks > 0 and performance.spend > 0:
                cpc[channel] = (round(performance.spend / performance.clicks, 4), CPC_MEASURED)
        return cpc

    # ── Escalation ─────────────────────────────────────────────────────────
    def _ask_model(
        self, ctx: AgentContext, sample: TrafficSample
    ) -> tuple[TrafficSample, str, float] | None:
        try:
            data = ctx.ask_json(
                prompts.assess_traffic(
                    source=sample.source,
                    channel=CHANNEL_LABELS.get(sample.channel, sample.channel),
                    sessions=sample.sessions,
                    bounce_rate=sample.bounce_rate,
                    avg_seconds=sample.avg_session_seconds,
                    signals=sample.signals,
                ),
                system=prompts.SYSTEM,
                schema_hint=prompts.SCHEMA_HINT,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.log_warning("Fraud assessment failed for %s: %s", sample.source, exc)
            return None

        if str(data.get("verdict") or "").lower() != "fraudulent":
            return None
        confidence = float(data.get("confidence") or 0.0)
        # Blocking real traffic costs real customers, so the bar is high.
        if confidence < 0.8:
            return None
        return sample, str(data.get("reason") or "Non-human click pattern"), confidence

    # ── Blocking ───────────────────────────────────────────────────────────
    def _block_action(
        self,
        sample: TrafficSample,
        reason: str,
        confidence: float,
        saved: float,
        basis: str = CPC_PLACEHOLDER,
    ) -> AgentAction:
        saved_text = money(saved, decimals=2)
        saved_text = f"{saved_text} saved" if basis == CPC_MEASURED else f"~{saved_text} est. saved"
        channel_name = CHANNEL_LABELS.get(sample.channel, sample.channel or "its platform")
        return AgentAction(
            kind="block_placement",
            title=f"Block {sample.source} on {channel_name} — {reason}",
            impact=Impact.LOW,
            approval_type="Traffic Block",
            target_kind=TARGET_KIND,
            # One pending item per source and channel. Without it every
            # proposal had target_id NULL and they collapsed into one card.
            target_id=target_id_for(sample.channel, sample.source),
            payload={
                "source": sample.source,
                "reason": reason,
                "channel": sample.channel,
                "confidence": confidence,
                "impressions": sample.sessions,
                "bounce_rate": sample.bounce_rate,
                "avg_session_seconds": sample.avg_session_seconds,
                "signals": {
                    k: v
                    for k, v in (sample.signals or {}).items()
                    if k in detectors.SAFE_SIGNAL_KEYS
                },
                "spend_saved": saved,
                "spend_saved_basis": basis,
            },
            apply=_make_blocker(sample, reason, confidence, saved),
            audit=(
                f"blocked {sample.sessions} fraudulent impressions from "
                f"{sample.source} on {channel_name} ({saved_text})"
            ),
            metrics={"fraud_blocked": 1, "spend_saved": saved},
        )


def target_id_for(channel: str, source: str) -> str:
    """Stable UUID for one source on one channel (target_id is a UUID column)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TARGET_KIND}:{channel}:{source}"))


def llm_check_budget(remaining_actions: int, already_proposed: int) -> int:
    """Ambiguous sources sent to the model this pass.

    Each check is one LLM call that can yield at most one block, so the count
    follows the daily actions left after the free heuristic blocks, never
    fewer than MIN_LLM_CHECKS (the old fixed number; queued proposals do not
    use up the allowance) and never more than MAX_LLM_CHECKS.
    """
    room = max(0, remaining_actions - already_proposed)
    return max(MIN_LLM_CHECKS, min(MAX_LLM_CHECKS, room))


def _block_targets(ctx: AgentContext, channel: str) -> list:
    """Connectors that can block this channel's traffic: the one it came from.

    A placement on Google is meaningless to Meta; pushing every source to
    every platform added nonsense exclusions everywhere else.
    """
    if not channel:
        return []
    return [
        connector
        for connector in ctx.all_with_capability(Capability.BLOCK_PLACEMENT)
        if (getattr(connector, "channel", "") or "") == channel
    ]


def _make_blocker(
    sample: TrafficSample, reason: str, confidence: float, saved: float
):  # noqa: ANN202
    def block(ctx: AgentContext) -> None:
        # Push first, so the record says what actually happened. Every push
        # failing fails the change (it is retried, and not counted as a block);
        # a source no connected platform can block is recorded as such.
        targets = _block_targets(ctx, sample.channel)
        failures: list[str] = []
        for connector in targets:
            try:
                connector.block_placement(source=sample.source, reason=reason)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - reported below
                ctx.log_warning("Could not push block to %s: %s", connector.slug, exc)
                failures.append(f"{connector.name}: {exc}")
        if targets and len(failures) == len(targets):
            from app.core.exceptions import ConnectorError

            raise ConnectorError(
                f"{sample.source} could not be blocked — " + "; ".join(failures)
            )
        if not targets:
            ctx.log_warning(
                "No connected platform can block %s traffic; %s recorded locally only",
                sample.channel or "unattributed",
                sample.source,
            )
        ctx.db.add(
            FraudEvent(
                tenant_id=ctx.tenant_id,
                at=utcnow(),
                source=sample.source,
                reason=reason,
                action="Blocked" if targets else "Recorded only",
                channel=sample.channel,
                impressions_blocked=sample.sessions,
                spend_saved=saved,
                # Detection features only — never a raw IP or device id.
                signal={
                    "bounce_rate": sample.bounce_rate,
                    "avg_session_seconds": sample.avg_session_seconds,
                    "confidence": confidence,
                    **{
                        k: v
                        for k, v in (sample.signals or {}).items()
                        if k in detectors.SAFE_SIGNAL_KEYS
                    },
                },
            )
        )
        ctx.db.flush()

    return block


@register_applier(TARGET_KIND)
def apply_block(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    """Approving a queued block does exactly what the autonomous path does."""
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    source = str(payload.get("source") or "").strip()
    if not source:
        raise ValueError("The approved block has no source stored")

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "click_fraud_controller"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Click-Fraud Controller is not installed in this workspace")

    sample = TrafficSample(
        source=source,
        sessions=int(payload.get("impressions") or 0),
        bounce_rate=float(payload.get("bounce_rate") or 0.0),
        avg_session_seconds=float(payload.get("avg_session_seconds") or 0.0),
        channel=str(payload.get("channel") or ""),
        signals=dict(payload.get("signals") or {}),
    )
    ctx = build_context(db, record, org, trigger="approval")
    _make_blocker(
        sample,
        str(payload.get("reason") or detectors.Reason.NON_HUMAN),
        float(payload.get("confidence") or 0.0),
        float(payload.get("spend_saved") or 0.0),
    )(ctx)


AGENT = ClickFraudControllerAgent()
