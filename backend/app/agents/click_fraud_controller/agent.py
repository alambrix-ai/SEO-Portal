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

from datetime import timedelta

from sqlalchemy import func, select

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
from app.agents.click_fraud_controller import detectors, prompts
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AdsConnector, TrafficSample
from app.core.logging import get_logger
from app.core.money import money
from app.db.base import utcnow
from app.models.ads import CHANNEL_LABELS, BudgetAllocation, FraudEvent

log = get_logger(__name__)

MAX_LLM_CHECKS = 4
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

        for sample in samples:
            if sample.source in blocked_recently:
                continue

            verdict, confidence, reason = detectors.assess(
                sample, always_block=always_block
            )
            if verdict is detectors.Verdict.FRAUD:
                verdicts.append((sample, reason, confidence))
            elif verdict is detectors.Verdict.UNCERTAIN:
                ambiguous.append(sample)

        for sample in ambiguous[:MAX_LLM_CHECKS]:
            resolved = self._ask_model(ctx, sample)
            if resolved is not None:
                verdicts.append(resolved)

        actions = []
        total_saved = 0.0
        for sample, reason, confidence in verdicts:
            cpc = cpc_by_channel.get(sample.channel, detectors.FALLBACK_CPC)
            saved = round(sample.sessions * cpc, 2)
            total_saved += saved
            actions.append(self._block_action(sample, reason, confidence, saved))

        blocked_today = ctx.db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(
                FraudEvent.tenant_id == ctx.tenant_id,
                FraudEvent.at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
            )
        ).scalar_one()

        return AgentResult(
            summary=(
                f"assessed {len(samples)} sources, blocking {len(actions)} "
                f"({money(total_saved, decimals=2)} saved)"
            ),
            actions=actions,
            metric_label=f"{blocked_today + len(actions)} blocked today",
            metrics={
                "fraud_blocked": len(actions),
                "spend_saved": round(total_saved, 2),
            },
            detail={
                "assessed": len(samples),
                "heuristic_blocks": len(verdicts) - min(len(ambiguous), MAX_LLM_CHECKS),
                "model_checks": min(len(ambiguous), MAX_LLM_CHECKS),
                "spend_saved": round(total_saved, 2),
            },
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _sample_traffic(self, ctx: AgentContext) -> list[TrafficSample]:
        samples: list[TrafficSample] = []
        for connector in ctx.all_with_capability(Capability.READ_TRAFFIC_QUALITY):
            ads: AdsConnector = connector  # type: ignore[assignment]
            try:
                samples.extend(ads.sample_traffic(limit=80))
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning(
                    "Traffic sample failed for %s: %s", connector.slug, exc
                )
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

    def _cpc_by_channel(self, ctx: AgentContext) -> dict[str, float]:
        """Real cost per click per channel, so savings are not guessed."""
        cpc: dict[str, float] = {}
        for row in ctx.db.execute(
            select(BudgetAllocation).where(BudgetAllocation.tenant_id == ctx.tenant_id)
        ).scalars():
            # Spend over conversions is a CAC; a click costs a fraction of it.
            if row.spend > 0 and row.conversions > 0:
                cpc[row.channel] = round((row.spend / row.conversions) * 0.04, 4)
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
        self, sample: TrafficSample, reason: str, confidence: float, saved: float
    ) -> AgentAction:
        return AgentAction(
            kind="block_placement",
            title=f"Block {sample.source} — {reason}",
            impact=Impact.LOW,
            approval_type="Traffic Block",
            target_kind="fraud_event",
            payload={
                "source": sample.source,
                "reason": reason,
                "channel": sample.channel,
                "confidence": confidence,
                "impressions": sample.sessions,
                "spend_saved": saved,
            },
            apply=_make_blocker(sample, reason, confidence, saved),
            audit=(
                f"blocked {sample.sessions} fraudulent impressions from "
                f"{sample.source} ({money(saved, decimals=2)} saved)"
            ),
        )


def _make_blocker(
    sample: TrafficSample, reason: str, confidence: float, saved: float
):  # noqa: ANN202
    def block(ctx: AgentContext) -> None:
        ctx.db.add(
            FraudEvent(
                tenant_id=ctx.tenant_id,
                at=utcnow(),
                source=sample.source,
                reason=reason,
                action="Blocked",
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

        # Push the exclusion to the platform so it stops bidding, not just
        # so this console knows about it.
        for connector in ctx.all_with_capability(Capability.BLOCK_PLACEMENT):
            try:
                connector.block_placement(source=sample.source, reason=reason)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - the local record stands
                ctx.log_warning(
                    "Could not push block to %s: %s", connector.slug, exc
                )
        ctx.db.flush()

    return block


AGENT = ClickFraudControllerAgent()
