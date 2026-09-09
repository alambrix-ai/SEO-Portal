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
from app.agents.technical_seo_auditor import checks
from app.connectors.base.connector import Capability
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.seo import SeoIssue, SeoPage
from app.services.encryption import Ctx

log = get_logger(__name__)


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
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths, e.g. ``/blog/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        pages = self._snapshots(ctx)
        if not pages:
            return AgentResult.skip("No pages crawled yet — run On-Page SEO Sync first")

        measured = self._add_vitals(ctx, pages)
        findings = checks.audit(
            pages,
            domain=ctx.org.primary_domain,
            thin_words=int(ctx.setting("thin_words", settings.seo_thin_content_words)),
        )
        opened, resolved = self._reconcile(ctx, findings)

        high = sum(1 for f in findings if f.severity is checks.Severity.HIGH)
        summary = (
            f"{len(findings)} open issues across {len(pages)} pages "
            f"({high} high){'' if measured else ', speed unmeasured'}"
        )
        return AgentResult(
            summary=summary,
            # No actions: this agent proposes nothing to apply. Its output is
            # the findings, which a person works through.
            actions=[],
            metric_label=f"{len(findings)} open issues, {high} high",
            metrics={"seo_issues_open": len(findings), "seo_issues_fixed": resolved},
            detail={
                "pages_audited": len(pages),
                "opened": opened,
                "resolved": resolved,
                "vitals_measured": measured,
                "by_kind": _counts(findings),
            },
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _snapshots(self, ctx: AgentContext) -> list[checks.PageSnapshot]:
        """The crawled pages, decrypted, as the checks want them."""
        ctx.progress("Loading the crawled pages")
        stmt = select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id)
        # Matched properly rather than by string prefix: /blog used to pull in
        # /blog-archive, /blog/* used to exclude /blog itself, and a
        # comma-separated scope matched nothing at all.
        in_scope = scope_rules.sql_filter(ctx.scope or "", SeoPage.url)
        if in_scope is not None:
            stmt = stmt.where(in_scope)
        rows = ctx.db.execute(stmt).scalars()

        pages: list[checks.PageSnapshot] = []
        for row in rows:
            pages.append(
                checks.PageSnapshot(
                    id=row.id,
                    url=row.url,
                    title=row.title,
                    body=ctx.cipher.decrypt(row.body_encrypted, context=Ctx.PAGE_BODY),
                    word_count=row.word_count,
                    schema_types=list(row.schema_types or []),
                )
            )
        return pages

    def _add_vitals(self, ctx: AgentContext, pages: list[checks.PageSnapshot]) -> int:
        """Attach field measurements, where a source is connected.

        Returns how many pages were actually measured. Without a source this
        does nothing and the speed checks produce nothing — which is the
        honest outcome, and the summary says so rather than implying the site
        is fast.
        """
        source = ctx.with_capability(Capability.READ_PAGE_SPEED)
        if source is None:
            return 0

        domain = ctx.org.primary_domain
        measured = 0
        # A quota-limited call per page, so the worst offenders first and a
        # bounded number of them per run.
        candidates = sorted(pages, key=lambda p: -p.word_count)[
            : settings.seo_vitals_pages_per_run
        ]
        for page in candidates:
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
                measured += 1
        return measured

    # ── Findings ───────────────────────────────────────────────────────────
    def _reconcile(
        self, ctx: AgentContext, findings: list[checks.Finding]
    ) -> tuple[int, int]:
        """Bring the stored issues into line with this run. ``(opened, resolved)``.

        The reconciliation is the point. A finding seen again keeps its
        first-seen date, so "open for three weeks" is true. A finding that has
        gone is marked fixed rather than deleted, so the work is countable. A
        finding somebody ignored stays ignored — reopening it every run would
        make the Ignore button useless.
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

        resolved = 0
        for key, issue in stored.items():
            if key in seen or issue.status != "open":
                continue
            issue.status = "fixed"
            issue.resolved_at = now
            resolved += 1

        ctx.db.flush()
        return opened, resolved


def _counts(findings: list[checks.Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind.value] = counts.get(finding.kind.value, 0) + 1
    return counts


AGENT = TechnicalSeoAuditorAgent()
