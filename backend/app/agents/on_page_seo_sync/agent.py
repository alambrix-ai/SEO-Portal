"""On-Page SEO Sync — crawls the CMS, finds semantic gaps, rewrites live copy.

One pass does three things:

1. **Sync** — pull content nodes from whichever CMS is connected and upsert
   them as ``SeoPage`` rows, with bodies encrypted under the organisation's key.
2. **Analyse** — for pages that are new or stale, ask the model where the page
   is topically incomplete and get a rewrite back. The gap score and proposed
   body are stored; nothing is published yet.
3. **Propose** — offer each rewrite as a high-impact action. Under full
   autonomy it is written straight back to the CMS; under the hybrid or
   human-in-the-loop guardrails it queues for approval, and the stored payload
   is what gets published when someone says yes.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base import pages
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
from app.agents.on_page_seo_sync import prompts
from app.connectors.base.connector import Capability
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.seo import PageStatus, SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

log = get_logger(__name__)

# How long a page's analysis stays fresh before it is looked at again.
ANALYSIS_TTL = timedelta(days=7)
# A page is only worth rewriting above this gap score.
REWRITE_THRESHOLD = 45
# Pages analysed per run. Each one is an LLM call, so the cap bounds both
# latency and spend per run rather than relying on the daily action limit.
ANALYSE_PER_RUN = 6


class OnPageSeoSyncAgent(BaseAgent):
    spec = AgentSpec(
        slug="on_page_seo_sync",
        name="On-Page SEO Sync",
        category=AgentCategory.SEO_AEO,
        description="Crawls CMS nodes, maps semantic gaps, rewrites blocks live.",
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        required_capabilities=(Capability.LIST_PAGES, Capability.WRITE_PAGE),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=20,
        scope_placeholder="/blog/* or leave blank for the whole site",
        requires_llm=True,
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths, e.g. ``/blog/*`` or ``/products/*, /guides/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        # Every capable source is tried, the scope is applied here rather than
        # inside a connector, and a run with no pages says which step
        # discarded them. See app/agents/base/pages.py.
        ctx.progress("Looking for pages to work on")
        found = pages.harvest(ctx, limit=200, need_usable=True)
        if not found.ok:
            return AgentResult.skip(found.reason)

        ctx.progress(f"Syncing {len(found.usable_pages)} pages", total=len(found.pages))
        synced = self._sync_pages(ctx, found)
        read = self._read_bodies(ctx, found)

        total = ctx.db.execute(
            select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id)
        ).scalars()
        page_count = len(list(total))

        analysed = self._analyse_pages(ctx)
        actions = self._propose_rewrites(ctx)

        # Pages whose rewrite is already with a person. Counted because a
        # pass that finds nothing new is the *normal* state once the work is
        # done, and reporting only the deltas made that look like a failure.
        awaiting = ctx.db.execute(
            select(func.count())
            .select_from(SeoPage)
            .where(
                SeoPage.tenant_id == ctx.tenant_id,
                SeoPage.status == PageStatus.QUEUED.value,
            )
        ).scalar_one()

        return AgentResult(
            summary=self._summary(
                source=ctx.connector_name(found.source_slug),
                synced=synced,
                read=read,
                analysed=analysed,
                proposed=len(actions),
                awaiting=awaiting,
            ),
            actions=actions,
            metric_label=f"{page_count} pages synced",
            metrics={"pages_optimised": len(actions)},
            detail={
                "source": found.source_slug,
                "listed": found.listed,
                "in_scope": len(found.pages),
                "synced": synced,
                "bodies_read": read,
                "analysed": analysed,
                "proposed": len(actions),
            },
        )

    @staticmethod
    def _summary(
        *,
        source: str,
        synced: int,
        read: int,
        analysed: int,
        proposed: int,
        awaiting: int,
    ) -> str:
        """What happened, and where things stand if nothing did.

        The distinction matters because "analysed 0, 0 rewrites proposed" is
        both what a broken agent reports and what a finished one reports, and
        those need to read differently.
        """
        did = []
        if synced:
            did.append(f"synced {synced} pages from {source}")
        if read:
            did.append(f"read {read}")
        if analysed:
            did.append(f"analysed {analysed}")
        if proposed:
            did.append(f"proposed {proposed} rewrite{'s' if proposed != 1 else ''}")

        if did:
            sentence = ", ".join(did)
            # Still worth adding: a pass that proposed two more while six sit
            # unapproved is a different situation from one that proposed two.
            if awaiting > proposed:
                sentence += f" — {awaiting} now awaiting approval"
            return sentence

        # Nothing new this pass. Which of the two reasons it was matters.
        if awaiting:
            return (
                f"nothing new to analyse — {awaiting} "
                f"rewrite{'s' if awaiting != 1 else ''} already awaiting "
                f"approval"
            )
        return (
            "no pages were due for analysis; every synced page has been "
            "looked at recently"
        )

    # ── Steps ──────────────────────────────────────────────────────────────
    def _sync_pages(self, ctx: AgentContext, found: pages.PageHarvest) -> int:
        """Upsert the pages in scope. Bodies are encrypted on write.

        Only the pages whose body is content, not component source: listing a
        framework page file is useful for knowing the URL exists, but storing
        its source as the page body would put an import block through gap
        analysis and get a confident answer about it.
        """
        remote = found.usable_pages
        if not remote:
            return 0

        existing = {
            page.url: page
            for page in ctx.db.execute(
                select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id)
            ).scalars()
        }
        now = utcnow()
        touched = 0
        source_slug = found.source_slug

        for node in remote:
            page = existing.get(node.url)
            body_cipher = ctx.cipher.encrypt(node.body, context=Ctx.PAGE_BODY)

            if page is None:
                page = SeoPage(
                    tenant_id=ctx.tenant_id,
                    url=node.url,
                    title=node.title or node.url,
                    cms_id=node.remote_id,
                    cms_slug=source_slug,
                    body_encrypted=body_cipher,
                    word_count=node.word_count,
                    schema_types=node.schema_types or [],
                    status=PageStatus.LIVE.value,
                    last_crawled_at=now,
                    # Left null deliberately: crawled is not analysed, and a
                    # new page must be eligible for gap analysis immediately.
                    last_synced_at=None,
                )
                ctx.db.add(page)
            else:
                page.title = node.title or page.title
                page.cms_id = node.remote_id or page.cms_id
                page.cms_slug = source_slug
                page.word_count = node.word_count or page.word_count
                if node.schema_types:
                    page.schema_types = node.schema_types
                # Only overwrite the stored body when the CMS actually
                # returned one, so a listing endpoint that omits content does
                # not wipe what a previous full read captured.
                if node.body:
                    page.body_encrypted = body_cipher
                page.last_crawled_at = now
            touched += 1

        ctx.db.flush()
        return touched

    def _read_bodies(self, ctx: AgentContext, found: pages.PageHarvest) -> int:
        """Fetch the page bodies the analysis step needs.

        This step did not exist, and its absence was the reason this agent
        had never once proposed a rewrite. ``list_pages`` deliberately omits
        bodies — a listing of two hundred pages would otherwise be two
        hundred reads — and the intent was always that whatever wanted a body
        would read it. Nothing did. So every synced page had an empty body,
        ``_analyse_pages`` skipped all of them as "nothing to analyse yet",
        and the run reported "analysed 0" as a success, indefinitely.

        Worse for the auditor downstream, which reads ``word_count`` from
        these same rows: every page looked like nought words, which is to say
        every page on every site looked like thin content.

        Bodies are read newest-need-first and bounded per run, so a large
        site fills in over several runs instead of being re-read in full
        every six hours.
        """
        source = found.source
        if source is None or not hasattr(source, "read_page"):
            return 0

        by_url = {page.url: page for page in found.usable_pages}
        if not by_url:
            return 0
        rows = list(
            ctx.db.execute(
                select(SeoPage).where(
                    SeoPage.tenant_id == ctx.tenant_id,
                    SeoPage.url.in_(list(by_url)),
                )
            ).scalars()
        )
        # Pages with nothing stored come first: an empty body is the case that
        # blocks analysis outright, while a stale one merely ages.
        rows.sort(
            key=lambda row: (
                bool(row.body_encrypted),
                row.last_crawled_at or utcnow(),
            )
        )

        read = 0
        batch = rows[: settings.seo_pages_read_per_run]
        for index, row in enumerate(batch, start=1):
            ctx.progress(
                f"Reading page content ({index} of {len(batch)})",
                done=index,
                total=len(batch),
            )
            node = by_url.get(row.url)
            if node is None:
                continue
            try:
                full = source.read_page(node.remote_id)
            except Exception as exc:  # noqa: BLE001 - one page must not end the run
                ctx.log_warning("Could not read %s: %s", row.url, exc)
                continue

            if not full.body:
                continue
            row.body_encrypted = ctx.cipher.encrypt(full.body, context=Ctx.PAGE_BODY)
            # From the read, not from the listing, which had neither.
            row.word_count = full.word_count or len(full.body.split())
            if full.schema_types:
                row.schema_types = list(full.schema_types)
            if full.title:
                row.title = full.title
            row.last_crawled_at = utcnow()
            read += 1

        ctx.db.flush()
        return read

    def _analyse_pages(self, ctx: AgentContext) -> int:
        """Score the semantic gap on the pages most in need of a look."""
        cutoff = utcnow() - ANALYSIS_TTL
        candidates = list(
            ctx.db.execute(
                select(SeoPage)
                .where(
                    SeoPage.tenant_id == ctx.tenant_id,
                    SeoPage.status.in_([PageStatus.LIVE.value, PageStatus.FLAGGED.value]),
                )
                .order_by(SeoPage.last_synced_at.asc().nulls_first())
                .limit(ANALYSE_PER_RUN * 3)
            ).scalars()
        )
        stale = [
            page
            for page in candidates
            if page.last_synced_at is None or page.last_synced_at < cutoff
        ][:ANALYSE_PER_RUN]

        industry = ctx.industry
        analysed = 0

        for index, page in enumerate(stale, start=1):
            # One model call each, and the slowest thing this agent does.
            ctx.progress(
                f"Analysing {page.url} ({index} of {len(stale)})",
                done=index,
                total=len(stale),
            )
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            if not body:
                # Nothing to analyse yet; a later full read will supply it.
                page.last_synced_at = utcnow()
                continue

            try:
                data = ctx.ask_json(
                    prompts.semantic_gap(
                        title=page.title,
                        url=page.url,
                        body=body,
                        keywords=list(page.target_keywords or []),
                        industry=industry,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001 - one page must not end the run
                ctx.log_warning("Gap analysis failed for %s: %s", page.url, exc)
                continue

            gap = int(data.get("gap_score") or 0)
            page.gap_score = max(0, min(100, gap))
            page.missing_topics = list(data.get("missing_topics") or [])
            page.target_keywords = list(data.get("target_keywords") or page.target_keywords or [])
            # Cleaned on the way in, so what a reviewer reads is exactly
            # what would publish.
            page.proposed_body_encrypted = ctx.cipher.encrypt(
                humanise(data.get("proposed_body") or ""), context=Ctx.PAGE_PROPOSED
            )
            page.rewrite_rationale_encrypted = ctx.cipher.encrypt(
                humanise(data.get("rationale") or ""), context=Ctx.PAGE_RATIONALE
            )
            page.last_synced_at = utcnow()

            # Only a page with both a real gap and a rewrite to apply is
            # flagged; otherwise it stays live and is left alone.
            if page.gap_score >= REWRITE_THRESHOLD and data.get("proposed_body"):
                page.status = PageStatus.FLAGGED.value
            elif page.status == PageStatus.FLAGGED.value:
                page.status = PageStatus.LIVE.value
            analysed += 1

        ctx.db.flush()
        return analysed

    def _propose_rewrites(self, ctx: AgentContext) -> list[AgentAction]:
        """Turn every flagged page into one high-impact action."""
        flagged = list(
            ctx.db.execute(
                select(SeoPage)
                .where(
                    SeoPage.tenant_id == ctx.tenant_id,
                    SeoPage.status == PageStatus.FLAGGED.value,
                )
                .order_by(SeoPage.gap_score.desc())
                .limit(max(ctx.remaining_actions, 1) * 2)
            ).scalars()
        )

        ctx.progress("Preparing the rewrites it wants to propose")
        actions: list[AgentAction] = []
        for page in flagged:
            proposed = ctx.cipher.decrypt(
                page.proposed_body_encrypted, context=Ctx.PAGE_PROPOSED
            )
            if not proposed:
                continue

            actions.append(
                AgentAction(
                    kind="publish_rewrite",
                    title=f'Rewrite: "{page.title}" page copy',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.CONTENT_REWRITE.value,
                    target_kind="seo_page",
                    target_id=page.id,
                    payload={
                        "page_id": page.id,
                        "url": page.url,
                        "title": page.title,
                        "gap_score": page.gap_score,
                        "missing_topics": list(page.missing_topics or []),
                        # The rewrite travels with the approval so publishing
                        # later needs no second model call.
                        "proposed_body": proposed,
                    },
                    apply=_make_publisher(page.id),
                    audit=f"published rewrite on {page.url}",
                )
            )
            # Queued items are marked so the SEO table shows "Awaiting
            # approval" rather than re-offering the same rewrite next run.
            page.status = PageStatus.QUEUED.value

        ctx.db.flush()
        return actions


# ── Applying a rewrite ─────────────────────────────────────────────────────
def _make_publisher(page_id: str):  # noqa: ANN202 - returns a closure
    def publish(ctx: AgentContext) -> None:
        page = ctx.db.get(SeoPage, page_id)
        if page is None:
            return
        body = ctx.cipher.decrypt(page.proposed_body_encrypted, context=Ctx.PAGE_PROPOSED)
        if not body:
            return

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
                f"this page came from, and it is not connected, so this rewrite "
                f"cannot be published. Reconnect it and approve this again."
            )

        cms.write_page(page.cms_id, body=body, title=page.title)  # type: ignore[union-attr]
        page.body_encrypted = ctx.cipher.encrypt(body, context=Ctx.PAGE_BODY)
        page.proposed_body_encrypted = ""
        page.status = PageStatus.REWRITTEN.value
        page.gap_score = min(page.gap_score, REWRITE_THRESHOLD - 1)
        page.missing_topics = []
        page.last_synced_at = utcnow()
        ctx.db.flush()

    return publish


@register_applier("seo_page")
def apply_page_rewrite(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    """Publish an approved rewrite.

    Runs the same publish path an autonomous action would, rebuilt from the
    approval's stored payload rather than from a fresh model call.
    """
    from app.orchestration.runner import build_context

    from app.models.agent import AgentRecord

    page_id = payload.get("page_id") or item.target_id
    page = db.get(SeoPage, page_id) if page_id else None
    if page is None:
        raise LookupError("The page this rewrite targets no longer exists")

    body = payload.get("proposed_body") or ""
    if not body:
        raise ValueError("The approved rewrite has no content stored")

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "on_page_seo_sync"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("On-Page SEO Sync is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    cipher = OrgCipher.for_org(org)
    page.proposed_body_encrypted = cipher.encrypt(body, context=Ctx.PAGE_PROPOSED)
    db.flush()
    _make_publisher(page.id)(ctx)


@register_rejecter("seo_page")
def revert_page_rewrite(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    """Put a rejected page back to live and drop the proposal."""
    page = db.get(SeoPage, payload.get("page_id") or item.target_id or "")
    if page is None:
        return
    page.status = PageStatus.LIVE.value
    page.proposed_body_encrypted = ""
    db.flush()


AGENT = OnPageSeoSyncAgent()
