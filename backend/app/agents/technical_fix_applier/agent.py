"""Propose reversible CMS / git fixes for open technical audit findings.

The Technical SEO Auditor stays read-only. This agent owns the ``apply``
callables and always goes through the runner → approval gate. Dangerous
kinds (Core Web Vitals, thin content, orphans) are left for humans.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import pages as page_rules
from app.agents.base.context import AgentContext
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
from app.db.base import utcnow
from app.models.agent import AgentRecord
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.seo import SeoIssue, SeoPage
from app.models.seo_fix import SeoIssueFixProposal
from app.models.workspace import Organization
from app.services import approvals
from app.services import technical_fixes as fixes
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "seo_issue_fix"
ISSUES_PER_RUN = 8


class TechnicalFixApplierAgent(BaseAgent):
    spec = AgentSpec(
        slug="technical_fix_applier",
        name="Technical Fix Applier",
        category=AgentCategory.SEO_AEO,
        description=(
            "Turns open technical-audit findings into proposed CMS or git "
            "fixes — titles, meta descriptions, broken links, canonicals, "
            "and missing H1s — always through the approval gate."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.WRITE_PAGE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=20,
        scope_placeholder="Leave blank to consider every open auto-fixable finding",
        requires_llm=False,
        connector_use=(
            ConnectorUse(
                Capability.WRITE_PAGE,
                UseMode.PAGE_SOURCE,
                question="Which websites may receive technical fixes?",
                multi=True,
                selected="applies fixes on pages from {names}",
                everything="applies fixes on every website it can write to",
                note="Each fix is written back to the website its page was collected from.",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        issues = self._open_fixable(ctx)
        if not issues:
            return AgentResult(
                summary="No open auto-fixable findings",
                metric_label="No fixes needed",
                skipped=True,
                skip_reason="No open title, meta, link, canonical or H1 findings",
            )

        pages = self._pages_by_id(ctx, {issue.page_id for issue in issues})
        actions: list[AgentAction] = []
        proposed = 0
        skipped = 0

        for index, issue in enumerate(issues, start=1):
            if len(actions) >= min(ISSUES_PER_RUN, max(ctx.remaining_actions, 1)):
                break
            if self._pending_for_issue(ctx, issue.id):
                skipped += 1
                continue
            page = pages.get(issue.page_id)
            if page is None:
                skipped += 1
                continue

            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            try:
                plan = fixes.apply_fix(
                    kind=issue.kind,
                    title=page.title,
                    body=body or "",
                    url=page.url,
                    target=str((issue.evidence or {}).get("target") or issue.target or ""),
                )
            except ValueError:
                skipped += 1
                continue
            if plan.body is None and plan.title is None and plan.meta_description is None:
                skipped += 1
                continue

            ctx.progress(
                f"Proposing fix for {issue.kind} on {page.url}",
                done=index - 1,
                total=len(issues),
            )
            proposal = self._ensure_proposal(ctx, issue, page, plan.fix_kind)
            payload = {
                "fix_kind": plan.fix_kind,
                "issue_id": issue.id,
                "page_id": page.id,
                "url": page.url,
                "target": str((issue.evidence or {}).get("target") or issue.target or ""),
                "title": page.title,
                "proposed_title": plan.title,
                "proposed_meta": plan.meta_description,
                "proposed_body": plan.body,
                "summary": plan.summary,
            }
            actions.append(
                AgentAction(
                    kind="apply_technical_fix",
                    title=f'Fix: {issue.kind.replace("_", " ")} on "{page.title}"',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.TECHNICAL_FIX.value,
                    target_kind=TARGET_KIND,
                    target_id=issue.id,
                    payload=payload,
                    apply=_make_applier(issue.id),
                    audit=f"applied technical fix ({plan.fix_kind}) on {page.url}",
                    metrics={"technical_fixes": 1},
                )
            )
            proposal.status = "proposed"
            proposed += 1

        ctx.db.flush()
        if not actions:
            return AgentResult.skip(
                "Open findings were reviewed, but none produced a safe write payload."
            )
        return AgentResult(
            summary=(
                f"Proposed {proposed} technical fix{'es' if proposed != 1 else ''}"
                + (f" ({skipped} skipped)" if skipped else "")
            ),
            actions=actions,
            metric_label=f"{proposed} fix proposed" if proposed else "No fix proposed",
            metrics={"technical_fixes_proposed": proposed},
            detail={"proposed": proposed, "skipped": skipped},
        )

    def _open_fixable(self, ctx: AgentContext) -> list[SeoIssue]:
        stmt = (
            select(SeoIssue)
            .where(
                SeoIssue.tenant_id == ctx.tenant_id,
                SeoIssue.status == "open",
                SeoIssue.kind.in_(sorted(fixes.AUTO_FIXABLE_KINDS)),
            )
            .order_by(SeoIssue.first_seen_at.asc())
            .limit(80)
        )
        rows = list(ctx.db.execute(stmt).scalars())
        # Prefer writable pages only.
        writable_ids = set(self._pages_by_id(ctx, {r.page_id for r in rows}))
        return [row for row in rows if row.page_id in writable_ids]

    def _pages_by_id(self, ctx: AgentContext, ids: set[str]) -> dict[str, SeoPage]:
        if not ids:
            return {}
        stmt = select(SeoPage).where(
            SeoPage.tenant_id == ctx.tenant_id,
            SeoPage.id.in_(list(ids)),
        )
        writable = page_rules.writable_rows(SeoPage.cms_slug)
        if writable is not None:
            stmt = stmt.where(writable)
        chosen = page_rules.source_filter(ctx, SeoPage.cms_slug, Capability.WRITE_PAGE)
        if chosen is not None:
            stmt = stmt.where(chosen)
        return {page.id: page for page in ctx.db.execute(stmt).scalars()}

    def _pending_for_issue(self, ctx: AgentContext, issue_id: str) -> bool:
        # Only an open approval blocks a new proposal. A proposal row without a
        # queued approval (blocked by policy, or a failed run) must not strand
        # the finding forever.
        pending = ctx.db.execute(
            select(ApprovalItem.id).where(
                ApprovalItem.tenant_id == ctx.tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
                ApprovalItem.target_kind == TARGET_KIND,
                ApprovalItem.target_id == issue_id,
            ).limit(1)
        ).scalar_one_or_none()
        return pending is not None

    def _ensure_proposal(
        self,
        ctx: AgentContext,
        issue: SeoIssue,
        page: SeoPage,
        fix_kind: str,
    ) -> SeoIssueFixProposal:
        existing = ctx.db.execute(
            select(SeoIssueFixProposal).where(
                SeoIssueFixProposal.tenant_id == ctx.tenant_id,
                SeoIssueFixProposal.issue_id == issue.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status in {"rejected", "superseded", "applied"}:
                existing.status = "proposed"
                existing.proposed_at = utcnow()
                existing.decided_at = None
                existing.approval_item_id = None
            existing.fix_kind = fix_kind
            existing.page_id = page.id
            return existing
        row = SeoIssueFixProposal(
            tenant_id=ctx.tenant_id,
            issue_id=issue.id,
            page_id=page.id,
            fix_kind=fix_kind,
            status="proposed",
            proposed_at=utcnow(),
        )
        ctx.db.add(row)
        ctx.db.flush()
        return row


def build_action_for_issue(ctx: AgentContext, issue: SeoIssue) -> AgentAction | None:
    """Build one HIGH action for an API-driven Propose-fix request."""
    if not fixes.is_auto_fixable(issue.kind) or issue.status != "open":
        return None
    page = ctx.db.get(SeoPage, issue.page_id)
    if page is None or page.tenant_id != ctx.tenant_id:
        return None
    body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
    plan = fixes.apply_fix(
        kind=issue.kind,
        title=page.title,
        body=body or "",
        url=page.url,
        target=str((issue.evidence or {}).get("target") or issue.target or ""),
    )
    if plan.body is None and plan.title is None and plan.meta_description is None:
        return None
    AGENT._ensure_proposal(ctx, issue, page, plan.fix_kind)
    return AgentAction(
        kind="apply_technical_fix",
        title=f'Fix: {issue.kind.replace("_", " ")} on "{page.title}"',
        impact=Impact.HIGH,
        approval_type=ApprovalType.TECHNICAL_FIX.value,
        target_kind=TARGET_KIND,
        target_id=issue.id,
        payload={
            "fix_kind": plan.fix_kind,
            "issue_id": issue.id,
            "page_id": page.id,
            "url": page.url,
            "target": str((issue.evidence or {}).get("target") or issue.target or ""),
            "title": page.title,
            "proposed_title": plan.title,
            "proposed_meta": plan.meta_description,
            "proposed_body": plan.body,
            "summary": plan.summary,
        },
        apply=_make_applier(issue.id),
        audit=f"applied technical fix ({plan.fix_kind}) on {page.url}",
        metrics={"technical_fixes": 1},
    )


def _make_applier(issue_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _apply_issue_fix(ctx.db, ctx.org, issue_id, cipher=ctx.cipher, ctx=ctx)

    return apply


def _apply_issue_fix(
    db: Session,
    org: Organization,
    issue_id: str,
    *,
    cipher: OrgCipher,
    ctx: AgentContext | None = None,
    payload: dict | None = None,
) -> None:
    from app.orchestration.runner import build_context

    issue = db.get(SeoIssue, issue_id)
    if issue is None or issue.tenant_id != org.id:
        raise LookupError("The finding this fix targets no longer exists")
    page = db.get(SeoPage, issue.page_id)
    if page is None:
        raise LookupError("The page this fix targets no longer exists")

    data = fixes.validate_payload(payload) if payload else None
    body = cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY) or ""
    plan = fixes.apply_fix(
        kind=data["fix_kind"] if data else issue.kind,
        title=page.title,
        body=body,
        url=page.url,
        target=(data or {}).get("target")
        or str((issue.evidence or {}).get("target") or issue.target or ""),
        proposed_title=(data or {}).get("proposed_title"),
        proposed_meta=(data or {}).get("proposed_meta"),
    )
    # Prefer the approved body snapshot when present (idempotent re-apply).
    if data and data.get("proposed_body"):
        new_body = data["proposed_body"]
    else:
        new_body = plan.body if plan.body is not None else body
    new_title = plan.title if plan.title is not None else page.title
    meta = plan.meta_description

    if ctx is None:
        record = db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == org.id,
                AgentRecord.slug == "technical_fix_applier",
            )
        ).scalar_one_or_none()
        if record is None:
            raise LookupError("Technical Fix Applier is not installed in this workspace")
        ctx = build_context(db, record, org, trigger="approval")

    cms = ctx.optional_connector(page.cms_slug)
    if cms is None:
        from app.core.exceptions import ConnectorError

        raise ConnectorError(
            f"{ctx.connector_name(page.cms_slug)} is the system this page came "
            "from, and it is not connected, so this fix cannot be published. "
            "Reconnect it and approve this again."
        )

    _write_page(
        cms,
        page.cms_id,
        body=new_body,
        title=new_title if new_title != page.title or plan.title else None,
        meta_description=meta,
    )
    page.body_encrypted = cipher.encrypt(new_body, context=Ctx.PAGE_BODY)
    if plan.title:
        page.title = new_title
    page.last_synced_at = utcnow()

    issue.status = "fixed"
    issue.resolved_at = utcnow()
    proposal = db.execute(
        select(SeoIssueFixProposal).where(
            SeoIssueFixProposal.tenant_id == org.id,
            SeoIssueFixProposal.issue_id == issue.id,
        )
    ).scalar_one_or_none()
    if proposal is not None:
        proposal.status = "applied"
        proposal.decided_at = utcnow()
    db.flush()


def _write_page(cms, remote_id: str, *, body: str, title: str | None, meta_description: str | None) -> None:  # noqa: ANN001
    """Call ``write_page``, passing meta when the connector accepts it."""
    try:
        cms.write_page(  # type: ignore[union-attr]
            remote_id, body=body, title=title, meta_description=meta_description
        )
    except TypeError:
        cms.write_page(remote_id, body=body, title=title)  # type: ignore[union-attr]
        if meta_description and hasattr(cms, "update_seo_fields"):
            cms.update_seo_fields(remote_id, meta_description=meta_description)


@register_applier(TARGET_KIND)
def apply_technical_fix(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    data = fixes.validate_payload(payload)
    cipher = OrgCipher.for_org(org)
    _apply_issue_fix(
        db,
        org,
        data["issue_id"] or item.target_id or "",
        cipher=cipher,
        payload=data,
    )
    proposal = db.execute(
        select(SeoIssueFixProposal).where(
            SeoIssueFixProposal.tenant_id == org.id,
            SeoIssueFixProposal.issue_id == data["issue_id"],
        )
    ).scalar_one_or_none()
    if proposal is not None:
        proposal.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_technical_fix(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    issue_id = str((payload or {}).get("issue_id") or item.target_id or "")
    if not issue_id:
        return
    proposal = db.execute(
        select(SeoIssueFixProposal).where(
            SeoIssueFixProposal.tenant_id == org.id,
            SeoIssueFixProposal.issue_id == issue_id,
        )
    ).scalar_one_or_none()
    if proposal is not None:
        proposal.status = "rejected"
        proposal.decided_at = utcnow()
        proposal.approval_item_id = item.id
        db.flush()


AGENT = TechnicalFixApplierAgent()

# Touch approvals so register_rejecter is always importable alongside this module.
_ = approvals
