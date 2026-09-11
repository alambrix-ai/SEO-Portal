"""AEO Q&A Injector — turns page copy into retrievable conversational blocks.

Answer engines retrieve passages, not pages. This agent splits body copy into
question/answer pairs phrased the way a person actually asks, tags each pair
with the engine whose phrasing it suits, and injects them back into the page.

Injection is low impact (additive, reversible, and it does not touch existing
copy), so under the hybrid guardrail it proceeds autonomously while a full
rewrite still waits for a human. Citations are then measured back through
whichever answer-engine connectors are wired up, which is what makes the
"AEO Citations" KPI a measurement rather than a guess.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.aeo_qa_injector import prompts
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
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AeoMonitorConnector, CmsConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.seo import AeoQaPair, SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier
from app.services.encryption import Ctx

log = get_logger(__name__)

# Pages to generate pairs for per run — one model call each.
PAGES_PER_RUN = 5
# Below this, a page has too few pairs to be reliably retrievable.
MIN_PAIRS_PER_PAGE = 4


class AeoQaInjectorAgent(BaseAgent):
    spec = AgentSpec(
        slug="aeo_qa_injector",
        name="AEO Q&A Injector",
        category=AgentCategory.SEO_AEO,
        description="Splits body copy into conversational Q&A for LLM crawlers.",
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        required_capabilities=(Capability.WRITE_PAGE,),
        max_impact=Impact.LOW,
        default_max_actions_per_day=40,
        scope_placeholder="/pricing/* or leave blank for every synced page",
        requires_llm=True,
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths, e.g. ``/service/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        generated, actions = self._generate(ctx)
        citations = self._measure_citations(ctx)

        pairs_today = ctx.db.execute(
            select(func.count())
            .select_from(AeoQaPair)
            .where(
                AeoQaPair.tenant_id == ctx.tenant_id,
                AeoQaPair.created_at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
            )
        ).scalar_one()

        return AgentResult(
            summary=(
                f"generated {generated} Q&A pairs, {len(actions)} injections proposed, "
                f"{citations} citations found"
            ),
            actions=actions,
            metric_label=f"{pairs_today} Q&A pairs today",
            metrics={"aeo_citations": citations},
            detail={"generated": generated, "citations": citations},
        )

    # ── Generation ─────────────────────────────────────────────────────────
    def _generate(self, ctx: AgentContext) -> tuple[int, list[AgentAction]]:
        pages = self._pages_needing_pairs(ctx)
        industry = ctx.industry
        generated = 0
        actions: list[AgentAction] = []

        for page in pages:
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            if not body:
                continue

            existing = [
                ctx.cipher.decrypt(row.question_encrypted, context=Ctx.QA_QUESTION)
                for row in ctx.db.execute(
                    select(AeoQaPair).where(
                        AeoQaPair.tenant_id == ctx.tenant_id, AeoQaPair.page_id == page.id
                    )
                ).scalars()
            ]

            try:
                data = ctx.ask_json(
                    prompts.qa_pairs(
                        title=page.title,
                        url=page.url,
                        body=body,
                        industry=industry,
                        existing_questions=[q for q in existing if q],
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Q&A generation failed for %s: %s", page.url, exc)
                continue

            fresh: list[AeoQaPair] = []
            seen = {q.strip().lower() for q in existing if q}
            for entry in data.get("pairs") or []:
                question = humanise((entry.get("question") or "").strip())
                answer = humanise((entry.get("answer") or "").strip())
                if not question or not answer or question.lower() in seen:
                    continue
                seen.add(question.lower())
                pair = AeoQaPair(
                    tenant_id=ctx.tenant_id,
                    page_id=page.id,
                    question_encrypted=ctx.cipher.encrypt(question, context=Ctx.QA_QUESTION),
                    answer_encrypted=ctx.cipher.encrypt(answer, context=Ctx.QA_ANSWER),
                    target_engine=(entry.get("target_engine") or "all").lower(),
                )
                ctx.db.add(pair)
                fresh.append(pair)

            if not fresh:
                continue

            ctx.db.flush()
            generated += len(fresh)
            page.aeo_pairs = (page.aeo_pairs or 0) + len(fresh)

            actions.append(
                AgentAction(
                    kind="inject_qa",
                    title=f'Inject {len(fresh)} Q&A blocks into "{page.title}"',
                    impact=Impact.LOW,
                    approval_type=ApprovalType.AEO_INJECTION.value,
                    target_kind="aeo_pair",
                    target_id=page.id,
                    payload={
                        "page_id": page.id,
                        "url": page.url,
                        "pair_ids": [p.id for p in fresh],
                        # The text itself. Without this the reviewer saw a
                        # URL and a count, and was approving copy for their
                        # own website that they had no way to read.
                        "pairs": [
                            {
                                "question": ctx.cipher.decrypt(
                                    pair.question_encrypted, context=Ctx.QA_QUESTION
                                ),
                                "answer": ctx.cipher.decrypt(
                                    pair.answer_encrypted, context=Ctx.QA_ANSWER
                                ),
                            }
                            for pair in fresh
                        ],
                    },
                    apply=_make_injector(page.id, [p.id for p in fresh]),
                    audit=f"injected {len(fresh)} Q&A blocks into {page.url}",
                )
            )

        ctx.db.flush()
        return generated, actions

    def _pages_needing_pairs(self, ctx: AgentContext) -> list[SeoPage]:
        """Pages with too few pairs, weakest coverage first."""
        # No filter on SeoPage.status. That column is On-Page SEO Sync's
        # rewrite lifecycle, and a page whose *copy rewrite* is waiting for
        # approval is still a perfectly good subject for Q&A pairs — the two
        # are unrelated concerns. Excluding "queued" meant that on a
        # workspace under human review, where queued is the normal state,
        # this agent had nothing to look at and reported success with zeroes.
        #
        # Re-proposing is handled where it belongs: queue_action keeps one
        # pending item per agent per page, so this cannot pile up duplicates.
        stmt = select(SeoPage).where(
            SeoPage.tenant_id == ctx.tenant_id,
            SeoPage.aeo_pairs < MIN_PAIRS_PER_PAGE + 3,
        )
        in_scope = scope_rules.sql_filter(ctx.scope or "", SeoPage.url)
        if in_scope is not None:
            stmt = stmt.where(in_scope)
        return list(
            ctx.db.execute(
                stmt.order_by(SeoPage.aeo_pairs.asc()).limit(PAGES_PER_RUN)
            ).scalars()
        )

    # ── Measurement ────────────────────────────────────────────────────────
    def _measure_citations(self, ctx: AgentContext) -> int:
        """Ask each connected answer engine whether it is citing this site."""
        domain = ctx.org.primary_domain
        if not domain:
            return 0

        engines = ctx.all_with_capability(Capability.CHECK_CITATIONS)
        if not engines:
            return 0

        # Query with the questions actually injected, since those are the
        # passages a retrieval engine would match.
        recent = list(
            ctx.db.execute(
                select(AeoQaPair)
                .where(
                    AeoQaPair.tenant_id == ctx.tenant_id, AeoQaPair.injected.is_(True)
                )
                .order_by(AeoQaPair.injected_at.desc())
                .limit(10)
            ).scalars()
        )
        queries = [
            ctx.cipher.decrypt(pair.question_encrypted, context=Ctx.QA_QUESTION)
            for pair in recent
        ]
        queries = [q for q in queries if q]
        if not queries:
            return 0

        by_question = {
            ctx.cipher.decrypt(pair.question_encrypted, context=Ctx.QA_QUESTION): pair
            for pair in recent
        }
        found = 0

        for engine in engines:
            monitor: AeoMonitorConnector = engine  # type: ignore[assignment]
            try:
                hits = monitor.check_citations(queries=queries, domain=domain)
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Citation check via %s failed: %s", engine.slug, exc)
                continue
            for hit in hits:
                found += 1
                pair = by_question.get(hit.query)
                if pair is not None:
                    pair.citation_count += 1

        ctx.db.flush()
        return found


# ── Applying an injection ──────────────────────────────────────────────────
def _make_injector(page_id: str, pair_ids: list[str]):  # noqa: ANN202
    def inject(ctx: AgentContext) -> None:
        page = ctx.db.get(SeoPage, page_id)
        if page is None:
            return

        pairs = [p for p in (ctx.db.get(AeoQaPair, pid) for pid in pair_ids) if p]
        if not pairs:
            return

        blocks = []
        for pair in pairs:
            question = ctx.cipher.decrypt(pair.question_encrypted, context=Ctx.QA_QUESTION)
            answer = ctx.cipher.decrypt(pair.answer_encrypted, context=Ctx.QA_ANSWER)
            blocks.append(prompts.render_qa_block(question, answer))

        # Only the system this page came from. There is no safe
        # guess: with_capability would hand back the first writer in
        # catalogue order, which in a workspace with two content
        # systems means publishing to the wrong website and
        # reporting that it worked.
        cms = ctx.optional_connector(page.cms_slug)
        if cms is None:
            from app.core.exceptions import ConnectorError

            raise ConnectorError(
                f"{ctx.connector_name(page.cms_slug)} is the system "
                f"this page came from, and it is not connected, so these Q&A blocks "
                f"cannot be published. Reconnect it and approve this again."
            )

        body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
        merged = prompts.merge_qa_section(body, blocks)
        writer: CmsConnector = cms  # type: ignore[assignment]
        writer.write_page(page.cms_id, body=merged, title=page.title)

        page.body_encrypted = ctx.cipher.encrypt(merged, context=Ctx.PAGE_BODY)
        now = utcnow()
        for pair in pairs:
            pair.injected = True
            pair.injected_at = now
        ctx.db.flush()

    return inject


@register_applier("aeo_pair")
def apply_qa_injection(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "aeo_qa_injector"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("AEO Q&A Injector is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    page_id = payload.get("page_id") or item.target_id or ""
    _make_injector(page_id, list(payload.get("pair_ids") or []))(ctx)


AGENT = AeoQaInjectorAgent()
