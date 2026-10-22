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
from app.agents.base import pages as page_rules
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
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AeoMonitorConnector, CmsConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.seo import AeoQaPair, SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx

log = get_logger(__name__)

# The fewest pages worked on per run — one model call each. The batch grows
# with the daily allowance up to pages.MAX_ITEMS_PER_RUN; see
# pages.batch_size for the rule.
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
        connector_use=(
            ConnectorUse(
                Capability.WRITE_PAGE,
                UseMode.PAGE_SOURCE,
                question="Which websites should this agent add Q&A sections to?",
                multi=True,
                selected="adds Q&A sections to collected pages from {names}",
                everything=(
                    "adds Q&A sections to collected pages on every website it can write "
                    "to"
                ),
                note=(
                    "Each section is saved to the website its page was collected from."
                ),
            ),
            ConnectorUse(
                Capability.CHECK_CITATIONS,
                UseMode.ALL,
                question=(
                    "Which AI services should this agent ask whether they mention your "
                    "site?"
                ),
                group="AI services",
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths, e.g. ``/service/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        generated, actions = self._generate(ctx)
        citations, newly_cited = self._measure_citations(ctx)

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
            # The increase since the last check, not the total: the counter
            # is summed by day, so adding the whole total on every run
            # counted the same citation four times a day.
            metrics={"aeo_citations": newly_cited},
            detail={
                "generated": generated,
                "citations": citations,
                "new_citations": newly_cited,
            },
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

            rows = list(
                ctx.db.execute(
                    select(AeoQaPair).where(
                        AeoQaPair.tenant_id == ctx.tenant_id, AeoQaPair.page_id == page.id
                    )
                ).scalars()
            )
            # Pairs written on an earlier run that never reached the page —
            # the daily cap held the injection back, or publishing failed.
            # They are offered again rather than generating another batch
            # beside them, which left the first batch orphaned and counted.
            leftover = [row for row in rows if not row.injected]
            if leftover:
                actions.append(self._action(ctx, page, leftover))
                continue

            existing = [
                ctx.cipher.decrypt(row.question_encrypted, context=Ctx.QA_QUESTION)
                for row in rows
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
            # page.aeo_pairs is not touched here. It counts pairs on the
            # page, and these are not on it until the injection is applied.
            actions.append(self._action(ctx, page, fresh))

        ctx.db.flush()
        return generated, actions

    @staticmethod
    def _action(ctx: AgentContext, page: SeoPage, pairs: list[AeoQaPair]) -> AgentAction:
        return AgentAction(
            kind="inject_qa",
            title=f'Inject {len(pairs)} Q&A blocks into "{page.title}"',
            impact=Impact.LOW,
            approval_type=ApprovalType.AEO_INJECTION.value,
            target_kind="aeo_pair",
            target_id=page.id,
            payload={
                "page_id": page.id,
                "url": page.url,
                "pair_ids": [p.id for p in pairs],
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
                    for pair in pairs
                ],
            },
            apply=_make_injector(page.id, [p.id for p in pairs]),
            audit=f"injected {len(pairs)} Q&A blocks into {page.url}",
        )

    def _pages_needing_pairs(self, ctx: AgentContext) -> list[SeoPage]:
        """Pages with too few pairs, weakest coverage first."""
        # No filter on SeoPage.status. That column is On-Page SEO Sync's
        # rewrite lifecycle, and a page whose *copy rewrite* is waiting for
        # approval is still a perfectly good subject for Q&A pairs — the two
        # are unrelated concerns. Excluding "queued" meant that on a
        # workspace under human review, where queued is the normal state,
        # this agent had nothing to look at and reported success with zeroes.
        #
        # A page whose injection is already waiting for a person is left out
        # here, not merely deduplicated by queue_action: a fresh batch would
        # replace the pending payload, and the pairs the reviewer had been
        # reading would be orphaned — never injected, never removed.
        pending = (
            select(ApprovalItem.id)
            .where(
                ApprovalItem.tenant_id == ctx.tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
                ApprovalItem.target_kind == "aeo_pair",
                ApprovalItem.target_id == SeoPage.id,
            )
            .exists()
        )
        stmt = select(SeoPage).where(
            SeoPage.tenant_id == ctx.tenant_id,
            SeoPage.aeo_pairs < MIN_PAIRS_PER_PAGE + 3,
            ~pending,
        )
        in_scope = scope_rules.sql_filter(ctx.scope or "", SeoPage.url)
        if in_scope is not None:
            stmt = stmt.where(in_scope)
        # Pages synced from a source that cannot write (the Site Crawler)
        # would get blocks no approval could ever publish.
        writable = page_rules.writable_rows(SeoPage.cms_slug)
        if writable is not None:
            stmt = stmt.where(writable)
        # Only pages from the websites chosen in this agent's settings.
        chosen = page_rules.source_filter(ctx, SeoPage.cms_slug, Capability.WRITE_PAGE)
        if chosen is not None:
            stmt = stmt.where(chosen)
        return list(
            ctx.db.execute(
                stmt.order_by(
                    # Pages with no body yet last: they are skipped without a
                    # model call, and must not take the run's places.
                    (SeoPage.body_encrypted == "").asc(),
                    SeoPage.aeo_pairs.asc(),
                ).limit(page_rules.batch_size(ctx, floor=PAGES_PER_RUN))
            ).scalars()
        )

    # ── Measurement ────────────────────────────────────────────────────────
    def _measure_citations(self, ctx: AgentContext) -> tuple[int, int]:
        """Ask each connected answer engine whether it is citing this site.

        Returns ``(found, new)``: the citations this check saw, and how many
        more than last time the checked pairs have. A pair's
        ``citation_count`` is *set* from the latest check, not added to —
        it used to grow by one every run while the same answer engine kept
        citing the same passage, so a single citation became dozens.
        """
        domain = ctx.org.primary_domain
        if not domain:
            return 0, 0

        engines = ctx.all_with_capability(Capability.CHECK_CITATIONS)
        if not engines:
            return 0, 0

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
        by_question = {
            ctx.cipher.decrypt(pair.question_encrypted, context=Ctx.QA_QUESTION): pair
            for pair in recent
        }
        by_question.pop("", None)
        queries = list(by_question)
        if not queries:
            return 0, 0

        found = 0
        failed = False
        latest: dict[str, int] = {}

        for engine in engines:
            monitor: AeoMonitorConnector = engine  # type: ignore[assignment]
            try:
                hits = monitor.check_citations(queries=queries, domain=domain)
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Citation check via %s failed: %s", engine.slug, exc)
                failed = True
                continue
            for hit in hits:
                found += 1
                pair = by_question.get(hit.query)
                if pair is not None:
                    latest[pair.id] = latest.get(pair.id, 0) + 1

        if failed:
            # A partial check is not a measurement of the whole: storing it
            # would drop the failed engine's citations now and count them as
            # new when it answers again.
            return found, 0

        new = 0
        for pair in by_question.values():
            count = latest.get(pair.id, 0)
            new += max(0, count - (pair.citation_count or 0))
            pair.citation_count = count

        ctx.db.flush()
        return found, new


# ── Applying an injection ──────────────────────────────────────────────────
def _make_injector(page_id: str, pair_ids: list[str]):  # noqa: ANN202
    def inject(ctx: AgentContext) -> None:
        page = ctx.db.get(SeoPage, page_id)
        if page is None:
            return

        pairs = [p for p in (ctx.db.get(AeoQaPair, pid) for pid in pair_ids) if p]
        if not pairs:
            return

        # Every pair already on the page as well as the new ones. The section
        # is replaced whole, so rendering only this batch deleted the last
        # one from the page while the database still counted it as there.
        ids = {pair.id for pair in pairs}
        already = [
            row
            for row in ctx.db.execute(
                select(AeoQaPair)
                .where(
                    AeoQaPair.tenant_id == page.tenant_id,
                    AeoQaPair.page_id == page.id,
                    AeoQaPair.injected.is_(True),
                )
                .order_by(AeoQaPair.injected_at.asc())
            ).scalars()
            if row.id not in ids
        ]

        blocks = []
        for pair in [*already, *pairs]:
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

        # Into the page as it is now, not as the last sync saw it.
        body = page_rules.current_body(ctx, cms, page)
        merged = prompts.merge_qa_section(body, blocks)
        writer: CmsConnector = cms  # type: ignore[assignment]
        writer.write_page(page.cms_id, body=merged, title=page.title)

        page.body_encrypted = ctx.cipher.encrypt(merged, context=Ctx.PAGE_BODY)
        now = utcnow()
        for pair in pairs:
            pair.injected = True
            pair.injected_at = now
        # Counted here, where they reach the page.
        page.aeo_pairs = len(already) + len(pairs)
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


@register_rejecter("aeo_pair")
def discard_qa_pairs(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    """Remove the pairs a reviewer turned down.

    Left in place they would be offered again on the next run as pairs
    that never reached the page, which is the opposite of what rejecting
    them meant. Pairs already on the page are never touched.
    """
    for pair_id in payload.get("pair_ids") or []:
        pair = db.get(AeoQaPair, pair_id)
        if pair is not None and pair.tenant_id == org.id and not pair.injected:
            db.delete(pair)
    db.flush()


AGENT = AeoQaInjectorAgent()
