"""AI Visibility Tracker — measure brand and competitor citations in AI answers.

Asks connected answer engines (ChatGPT / OpenAI, Perplexity, Gemini, Claude)
the workspace's target questions, records every cited host, and attributes
brand vs configured competitors. Read-only: it never writes to a CMS or opens
a PR. Observations are still produced inside an agent run so the audit log and
daily caps apply.

Google AI Overviews are intentionally out of scope for this first slice — they
need a guarded SERP path; see docs/roadmap/PLAN.md Phase 1.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.contracts import ConnectorUse, UseMode
from app.connectors.base.aeo_base import MAX_VISIBILITY_PROBES
from app.connectors.base.connector import Capability
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.models.visibility import VisibilityQuery
from app.services import visibility as visibility_service
from app.services.encryption import Ctx


class AiVisibilityTrackerAgent(BaseAgent):
    spec = AgentSpec(
        slug="ai_visibility_tracker",
        name="AI Visibility Tracker",
        category=AgentCategory.SEO_AEO,
        description=(
            "Asks ChatGPT, Perplexity, Gemini and Claude your target questions, "
            "records whether they cite your brand, and which competitors they cite."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.CHECK_CITATIONS,),
        max_impact=Impact.LOW,
        default_max_actions_per_day=1,
        scope_placeholder="",
        connector_use=(
            ConnectorUse(
                Capability.CHECK_CITATIONS,
                UseMode.ALL,
                question=(
                    "Which AI answer engines should this agent ask about your brand?"
                ),
                group="AI services",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        domain = (ctx.org.primary_domain or "").strip()
        if not domain:
            return AgentResult.skip(
                "Set the workspace primary domain so citations can be matched to your brand."
            )

        engines = ctx.all_with_capability(Capability.CHECK_CITATIONS)
        if not engines:
            return AgentResult.skip(
                "Connect an AI answer engine (OpenAI, Perplexity, Gemini or Claude) "
                "that can check citations."
            )

        settings = visibility_service.get_or_create_settings(ctx.db, ctx.tenant_id)
        competitors = visibility_service.normalise_competitor_list(
            list(settings.competitor_domains or [])
        )

        queries = list(
            ctx.db.execute(
                select(VisibilityQuery)
                .where(
                    VisibilityQuery.tenant_id == ctx.tenant_id,
                    VisibilityQuery.active.is_(True),
                )
                .order_by(VisibilityQuery.created_at.asc())
                .limit(MAX_VISIBILITY_PROBES)
            ).scalars()
        )
        if not queries:
            return AgentResult.skip(
                "Add target questions on the AI Visibility page before running this agent."
            )

        asked = 0
        brand_hits = 0
        competitor_hits = 0
        failures = 0
        total = len(queries) * len(engines)
        done = 0

        for query_row in queries:
            question = ctx.cipher.decrypt(
                query_row.question_encrypted, context=Ctx.VISIBILITY_QUESTION
            )
            if not question.strip():
                continue
            for engine in engines:
                done += 1
                ctx.progress(
                    f"Asking {engine.name} ({done} of {total})",
                    done=done - 1,
                    total=total,
                )
                try:
                    probe = engine.probe_visibility(query=question)  # type: ignore[attr-defined]
                except ConnectorConfigError:
                    raise
                except (ConnectorError, AttributeError, NotImplementedError) as exc:
                    ctx.log_warning(
                        "Visibility probe via %s failed: %s", engine.slug, exc
                    )
                    failures += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    ctx.log_warning(
                        "Visibility probe via %s failed: %s", engine.slug, exc
                    )
                    failures += 1
                    continue

                obs = visibility_service.record_observation(
                    ctx.db,
                    tenant_id=ctx.tenant_id,
                    cipher=ctx.cipher,
                    query=query_row,
                    question=question,
                    probe=probe,
                    brand_domain=domain,
                    competitors=competitors,
                    run_id=ctx.run_id or "",
                )
                asked += 1
                if obs.brand_cited:
                    brand_hits += 1
                if obs.competitors_cited:
                    competitor_hits += 1

        ctx.db.flush()

        if asked == 0 and failures:
            return AgentResult.skip(
                "Every answer engine refused the visibility check — see the run log."
            )

        share = f"{brand_hits}/{asked} cited your brand" if asked else "No probes completed"
        return AgentResult(
            summary=(
                f"Probed {asked} engine×question pairs; {share}; "
                f"{competitor_hits} saw a tracked competitor"
                + (f"; {failures} failed" if failures else "")
            ),
            metric_label=share,
            metrics={
                "visibility_probes": asked,
                "visibility_brand_citations": brand_hits,
                "visibility_competitor_sightings": competitor_hits,
            },
            detail={
                "probes": asked,
                "brand_citations": brand_hits,
                "competitor_sightings": competitor_hits,
                "failures": failures,
                "engines": [e.slug for e in engines],
                "queries": len(queries),
            },
        )


AGENT = AiVisibilityTrackerAgent()
