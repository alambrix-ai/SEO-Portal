"""Technical SEO Auditor — the site's health, as a list of things to fix.

What this agent is for, in business terms: an SEO retainer's first month is
usually spent finding out what is broken. This does that continuously, and it
does it from the customer's own pages rather than from a crawl of somebody's
opinion — thin pages, missing structured data, internal links into nothing,
pages nothing links to, and where a page-speed source is connected, the mobile
Core Web Vitals that Google actually ranks on.

Three deliberate choices:

**It reports, it does not rewrite.** Every finding carries a recommendation
and no ``apply`` callable. The agents that change things — On-Page SEO Sync,
Knowledge Graph & Schema — are separate and gated by the approvals queue. An
auditor that silently fixed what it found would leave nobody able to say what
the site looked like before.

**Findings persist and resolve.** A finding is a row with a first-seen date,
so it can be worked through, ignored with a reason, and — the number that
matters commercially — counted as fixed when the next run no longer sees it.

**Nothing measured is invented.** The speed checks produce nothing at all
without a connected page-speed source, because Core Web Vitals are field
measurements and cannot be derived from HTML. An audit that estimated them
would be making up numbers about somebody's site.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.agents.base import scope as scope_rules
from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.technical_seo_auditor import checks
from app.connectors.base.connector import Capability
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.seo import SeoIssue, SeoPage
from app.services.encryption import Ctx

log = get_logger(__name__)

#: Agent setting holding when each page was last sent for a speed reading,
#: so the quota-limited calls rotate through the site.
MEASURED_KEY = "_vitals_measured_at"
#: Findings only a speed reading can confirm or clear.
VITALS_KINDS = frozenset(
    {
        checks.IssueKind.SLOW_LCP_MOBILE.value,
        checks.IssueKind.POOR_CLS_MOBILE.value,
        checks.IssueKind.SLOW_INP_MOBILE.value,
    }
)


class TechnicalSeoAuditorAgent(BaseAgent):
    spec = AgentSpec(
        slug="technical_seo_auditor",
        name="Technical SEO Auditor",
        category=AgentCategory.SEO_AEO,
        description=(
            "Audits crawled pages for thin content, missing schema, broken "
            "internal links, orphan pages and mobile Core Web Vitals."
        ),
        default_interval=timedelta(hours=12),
        default_schedule="Daily",
        # It reads the pages the CMS sync stored, so it needs the same source
        # to exist — an audit of an empty page table is not an audit.
        required_capabilities=(Capability.LIST_PAGES,),
        # It only ever writes findings, never the customer's site.
        max_impact=Impact.LOW,
        default_max_actions_per_day=200,
        scope_placeholder="/blog/* to audit part of the site, or blank for all",
        connector_use=(
            ConnectorUse(
                Capability.LIST_PAGES,
                UseMode.PAGE_SOURCE,
                question="Which websites should this agent audit?",
                multi=True,
                selected="audits the pages collected from {names}",
                everything="audits every page On-Page SEO Sync has collected",
                note=(
                    "It does not read your website itself; it audits the pages On-Page "
                    "SEO Sync collects."
                ),
            ),
            ConnectorUse(
                Capability.READ_PAGE_SPEED,
                UseMode.ONE,
                question="Which service should measure page speed?",
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths, e.g. ``/blog/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        pages, rest = self._snapshots(ctx)
        if not pages:
            if rest:
                return AgentResult.skip(
                    f"None of the {len(rest)} collected pages is in this agent's scope and "
                    "from the websites chosen in its settings."
                )
            return AgentResult.skip("No pages crawled yet — run On-Page SEO Sync first")

        measured_ids = self._add_vitals(ctx, pages)
        measured = len(measured_ids)
        findings = checks.audit(
            pages,
            domain=ctx.org.primary_domain,
            thin_words=int(ctx.setting("thin_words", settings.seo_thin_content_words)),
            rest_of_site=rest,
        )
        opened, resolved, carried = self._reconcile(
            ctx,
            findings,
            audited={page.id for page in pages},
            measured=measured_ids,
        )

        high = sum(1 for f in findings if f.severity is checks.Severity.HIGH)
        open_total = len(findings) + carried
        summary = (
            f"{open_total} open issues across {len(pages)} pages "
            f"({high} high){'' if measured else ', speed unmeasured'}"
        )
        return AgentResult(
            summary=summary,
            # No actions: this agent proposes nothing to apply. Its output is
            # the findings, which a person works through.
            actions=[],
            metric_label=f"{open_total} open issues, {high} high",
            metrics={"seo_issues_open": open_total, "seo_issues_fixed": resolved},
            detail={
                "pages_audited": len(pages),
                "opened": opened,
                "resolved": resolved,
                "vitals_measured": measured,
                "by_kind": _counts(findings),
            },
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _snapshots(
        self, ctx: AgentContext
    ) -> tuple[list[checks.PageSnapshot], list[checks.PageSnapshot]]:
        """The crawled pages, decrypted: ``(in scope, the rest of the site)``.

        The rest are loaded too because whether a link is broken, or a page
        orphaned, depends on the whole site, not on the part being audited.
        """
        ctx.progress("Loading the crawled pages")
        # Matched properly rather than by string prefix: /blog used to pull in
        # /blog-archive, /blog/* used to exclude /blog itself, and a
        # comma-separated scope matched nothing at all.
        in_scope = scope_rules.path_matcher(ctx.scope or "")
        rows = ctx.db.execute(
            select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id)
        ).scalars()

        # Only pages from the websites chosen in this agent's settings are
        # audited; the others still count for links and orphans.
        finder = getattr(ctx, "page_sources", None)
        sources = finder(Capability.LIST_PAGES) if callable(finder) else None

        pages: list[checks.PageSnapshot] = []
        rest: list[checks.PageSnapshot] = []
        for row in rows:
            audited = in_scope(row.url) and (sources is None or row.cms_slug in sources)
            (pages if audited else rest).append(
                checks.PageSnapshot(
                    id=row.id,
                    url=row.url,
                    title=row.title,
                    body=ctx.cipher.decrypt(row.body_encrypted, context=Ctx.PAGE_BODY),
                    word_count=row.word_count,
                    schema_types=list(row.schema_types or []),
                )
            )
        return pages, rest

    def _add_vitals(self, ctx: AgentContext, pages: list[checks.PageSnapshot]) -> set[str]:
        """Attach field measurements, where a source is connected.

        Returns the ids of the pages actually measured. Without a source this
        does nothing and the speed checks produce nothing — which is the
        honest outcome, and the summary says so rather than implying the site
        is fast.

        Each reading is a quota-limited call, so a bounded number run per
        pass, rotating through the site: pages never measured first, then
        the longest since their last reading. (This used to take the longest
        pages every time, so most of a site was never measured at all.)
        """
        source = ctx.with_capability(Capability.READ_PAGE_SPEED)
        if source is None:
            return set()

        domain = ctx.org.primary_domain
        measured: set[str] = set()
        history = dict(ctx.setting(MEASURED_KEY) or {})
        candidates = sorted(pages, key=lambda p: history.get(p.id, ""))[
            : settings.seo_vitals_pages_per_run
        ]
        stamp = utcnow().isoformat()
        for page in candidates:
            # Recorded as attempted whatever the outcome, so a URL that never
            # has field data does not hold the front of the queue forever.
            history[page.id] = stamp
            absolute = page.url if page.url.startswith("http") else f"https://{domain}{page.url}"
            try:
                vitals = source.read_vitals(absolute)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - one URL must not stop the audit
                ctx.log_warning("Vitals unavailable for %s: %s", page.url, exc)
                continue
            if vitals.origin_level_only:
                # Real numbers, but about the whole site rather than this
                # page. Attributing them here would be a made-up per-page
                # measurement.
                continue
            page.lcp_ms, page.cls, page.inp_ms = vitals.lcp_ms, vitals.cls, vitals.inp_ms
            if any(v is not None for v in (vitals.lcp_ms, vitals.cls, vitals.inp_ms)):
                measured.add(page.id)

        alive = {page.id for page in pages}
        ctx.set_setting(MEASURED_KEY, {k: v for k, v in history.items() if k in alive})
        return measured

    # ── Findings ───────────────────────────────────────────────────────────
    def _reconcile(
        self,
        ctx: AgentContext,
        findings: list[checks.Finding],
        *,
        audited: set[str] | None = None,
        measured: set[str] | None = None,
    ) -> tuple[int, int, int]:
        """Bring the stored issues into line with this run.

        Returns ``(opened, resolved, carried)``, where ``carried`` counts the
        open speed issues on pages this pass did not re-measure, which are
        still open and so still part of the total.

        The reconciliation is the point. A finding seen again keeps its
        first-seen date, so "open for three weeks" is true. A finding that has
        gone is marked fixed rather than deleted, so the work is countable. A
        finding somebody ignored stays ignored — reopening it every run would
        make the Ignore button useless.

        Only what this run actually looked at can be found fixed. A page
        outside the scope was not audited, so its issues are not gone — they
        were marked fixed, and the "fixed this month" figure counted pages
        nobody touched. Likewise a speed issue is only cleared by a new
        reading of that page, not by the page being skipped this pass.
        ``audited=None`` means every stored page was audited.
        """
        now = utcnow()
        stored = {
            (issue.page_id, issue.kind, issue.target): issue
            for issue in ctx.db.execute(
                select(SeoIssue).where(SeoIssue.tenant_id == ctx.tenant_id)
            ).scalars()
        }
        seen: set[tuple[str, str, str]] = set()
        opened = 0

        for finding in findings:
            target = str(finding.evidence.get("target", ""))[:512]
            key = (finding.page_id, finding.kind.value, target)
            seen.add(key)
            existing = stored.get(key)

            if existing is None:
                ctx.db.add(
                    SeoIssue(
                        tenant_id=ctx.tenant_id,
                        page_id=finding.page_id,
                        url=finding.url,
                        kind=finding.kind.value,
                        severity=finding.severity.value,
                        target=target,
                        summary=finding.summary,
                        recommendation=finding.recommendation,
                        evidence=finding.evidence,
                        status="open",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                opened += 1
                continue

            if existing.status == "ignored":
                # Deliberately left alone. Still touched, so the console can
                # show it is current rather than stale.
                existing.last_seen_at = now
                continue

            existing.last_seen_at = now
            existing.severity = finding.severity.value
            existing.summary = finding.summary
            existing.evidence = finding.evidence
            if existing.status == "fixed":
                # It came back. Same issue, so the same row, reopened with the
                # original first-seen date intact.
                existing.status = "open"
                existing.resolved_at = None
                opened += 1

        resolved = carried = 0
        measured = measured or set()
        for key, issue in stored.items():
            if key in seen or issue.status != "open":
                continue
            if audited is not None and issue.page_id not in audited:
                continue
            if issue.kind in VITALS_KINDS and issue.page_id not in measured:
                carried += 1
                continue
            issue.status = "fixed"
            issue.resolved_at = now
            resolved += 1

        ctx.db.flush()
        return opened, resolved, carried


def _counts(findings: list[checks.Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind.value] = counts.get(finding.kind.value, 0) + 1
    return counts


AGENT = TechnicalSeoAuditorAgent()
