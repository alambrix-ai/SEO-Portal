"""SEO & AEO workspace — the page table and the traffic-quality guard."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import case, desc, func, or_, select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.connectors.base.connector import Capability
from app.core.exceptions import ConflictError, NotFoundError
from app.core.rbac import Module
from app.db.base import utcnow
from app.models.agent import AgentRun
from app.models.approval import ApprovalType
from app.models.seo import PageStatus, ReferralSpamEvent, SeoIssue, SeoPage
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import (
    IgnoreIssueRequest,
    SeoAuditOut,
    SeoPageDetailOut,
    SeoWorkspaceOut,
)
from app.services import approvals, audit as audit_service, views
from app.services import connectors as connector_service
from app.services.encryption import Ctx

router = APIRouter(prefix="/seo", tags=["seo"])


@router.get("", response_model=SeoWorkspaceOut)
def workspace(
    current: CurrentUserDep,
    db: DbSession,
    search: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
) -> SeoWorkspaceOut:
    """Everything the SEO screen renders in one call."""
    current.require_view(Module.SEO)
    can_write = current.can_write(Module.SEO)

    stmt = select(SeoPage).where(SeoPage.tenant_id == current.tenant_id)
    if search:
        needle = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(SeoPage.title).like(needle),
                func.lower(SeoPage.url).like(needle),
            )
        )

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    pages = db.execute(
        # Worst gaps first: that is the order someone reviewing this works in.
        stmt.order_by(desc(SeoPage.gap_score), SeoPage.url).limit(limit)
    ).scalars()

    spam = db.execute(
        select(ReferralSpamEvent)
        .where(ReferralSpamEvent.tenant_id == current.tenant_id)
        .order_by(desc(ReferralSpamEvent.at))
        .limit(25)
    ).scalars()

    return SeoWorkspaceOut(
        pages=[views.seo_page_out(page, can_write=can_write) for page in pages],
        total=total,
        referral_spam=[views.referral_spam_out(event) for event in spam],
        read_only=not can_write,
    )


@router.get("/pages/{page_id}", response_model=SeoPageDetailOut)
def page_detail(page_id: str, current: CurrentUserDep, db: DbSession) -> SeoPageDetailOut:
    """One page with its content decrypted — what a reviewer reads."""
    current.require_view(Module.SEO)
    page = db.get(SeoPage, page_id)
    if page is None or page.tenant_id != current.tenant_id:
        raise NotFoundError("Page not found")

    cipher = current.cipher
    base = views.seo_page_out(page, can_write=current.can_write(Module.SEO))
    return SeoPageDetailOut(
        **base.model_dump(),
        body=cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY),
        proposed_body=cipher.decrypt(page.proposed_body_encrypted, context=Ctx.PAGE_PROPOSED),
        rewrite_rationale=cipher.decrypt(
            page.rewrite_rationale_encrypted, context=Ctx.PAGE_RATIONALE
        ),
        target_keywords=list(page.target_keywords or []),
        qa_pairs=views.qa_pairs_for(db, page=page, cipher=cipher),
        schema_patches=views.schema_patches_for(db, page=page, cipher=cipher),
    )


@router.post("/pages/{page_id}/request-approval", response_model=ActionResult)
def request_approval(
    page_id: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Send a flagged page's rewrite for review.

    This is the "Approve rewrite" button in the page table: a person asking
    for the agent's proposal to be put in front of an approver.
    """
    current.require_write(Module.SEO)
    page = db.get(SeoPage, page_id)
    if page is None or page.tenant_id != current.tenant_id:
        raise NotFoundError("Page not found")
    if page.status != PageStatus.FLAGGED.value:
        raise ConflictError("This page has no rewrite waiting to be sent")

    proposed = current.cipher.decrypt(
        page.proposed_body_encrypted, context=Ctx.PAGE_PROPOSED
    )
    if not proposed:
        raise ConflictError(
            "The rewrite for this page is no longer available — let the agent run again"
        )

    approvals.queue_manual(
        db,
        org=current.organization,
        type_=ApprovalType.CONTENT_REWRITE.value,
        title=f'Rewrite: "{page.title}" page copy',
        agent_slug="on_page_seo_sync",
        agent_name="On-Page SEO Sync",
        target_kind="seo_page",
        target_id=page.id,
        payload={
            "page_id": page.id,
            "url": page.url,
            "title": page.title,
            "gap_score": page.gap_score,
            "missing_topics": list(page.missing_topics or []),
            "proposed_body": proposed,
        },
    )
    page.status = PageStatus.QUEUED.value

    audit_service.record_user_action(
        db,
        user=current.user,
        action=f"sent the rewrite for {page.url} for approval",
        module="seo",
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Sent for approval"))


# ── Technical audit ────────────────────────────────────────────────────────
@router.get("/audit", response_model=SeoAuditOut)
def audit(
    current: CurrentUserDep,
    db: DbSession,
    severity: str = Query(default="", pattern="^(high|medium|low|)$"),
    kind: str = Query(default="", max_length=48),
    status: str = Query(default="open", pattern="^(open|fixed|ignored|all)$"),
) -> SeoAuditOut:
    """Everything the Technical SEO screen shows.

    Defaults to open issues, because that is the working list. "Fixed" is
    available deliberately — it is the evidence the work happened, and the
    number a customer asks about at the end of the month.
    """
    current.require_view(Module.SEO)

    stmt = select(SeoIssue).where(SeoIssue.tenant_id == current.tenant_id)
    if status != "all":
        stmt = stmt.where(SeoIssue.status == status)
    if severity:
        stmt = stmt.where(SeoIssue.severity == severity)
    if kind:
        stmt = stmt.where(SeoIssue.kind == kind)

    # Worst first, then oldest first: a high-severity issue open for a month
    # is the top of the list.
    rank = case({"high": 0, "medium": 1, "low": 2}, value=SeoIssue.severity, else_=3)
    issues = list(
        db.execute(stmt.order_by(rank, SeoIssue.first_seen_at)).scalars()
    )

    open_rows = list(
        db.execute(
            select(SeoIssue).where(
                SeoIssue.tenant_id == current.tenant_id, SeoIssue.status == "open"
            )
        ).scalars()
    )
    month_ago = utcnow() - timedelta(days=30)
    fixed = db.execute(
        select(func.count())
        .select_from(SeoIssue)
        .where(
            SeoIssue.tenant_id == current.tenant_id,
            SeoIssue.status == "fixed",
            SeoIssue.resolved_at >= month_ago,
        )
    ).scalar_one()

    counts: dict[str, int] = {}
    for row in open_rows:
        counts[row.kind] = counts.get(row.kind, 0) + 1

    pages_audited = db.execute(
        select(func.count()).select_from(SeoPage).where(SeoPage.tenant_id == current.tenant_id)
    ).scalar_one()

    # "No speed issues" and "speed was never measured" must not look the same.
    bundle = connector_service.build_connector_bundle(db, current.organization)
    has_speed = any(
        instance.supports(Capability.READ_PAGE_SPEED) for instance in bundle.instances.values()
    )

    last_run = db.execute(
        select(AgentRun)
        .where(
            AgentRun.tenant_id == current.tenant_id,
            AgentRun.agent_slug == "technical_seo_auditor",
            AgentRun.status == "succeeded",
        )
        .order_by(desc(AgentRun.finished_at))
        .limit(1)
    ).scalars().first()

    return SeoAuditOut(
        issues=[views.issue_out(issue) for issue in issues],
        open_count=len(open_rows),
        high_count=sum(1 for row in open_rows if row.severity == "high"),
        fixed_this_month=fixed,
        pages_audited=pages_audited,
        by_kind=[
            {
                "kind": kind_value,
                "label": views.ISSUE_LABELS.get(kind_value, kind_value),
                "count": count,
            }
            for kind_value, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
        vitals_unavailable=(
            ""
            if has_speed
            else (
                "Mobile speed is not measured. Core Web Vitals are field data "
                "from real devices, so they cannot be inferred from the page — "
                "connect PageSpeed Insights to include them."
            )
        ),
        last_audit_at=last_run.finished_at if last_run else None,
        read_only=not current.can_write(Module.SEO),
    )


@router.post("/audit/{issue_id}/ignore", response_model=ActionResult)
def ignore_issue(
    issue_id: str,
    payload: IgnoreIssueRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    """Accept an issue as it stands, with a reason.

    The note is required. An ignored finding with no reason becomes an
    argument again in a month, and the auditor deliberately does not reopen
    what somebody chose to live with — so the reason is the only record of
    why.
    """
    current.require_write(Module.SEO)
    issue = _issue(db, current.tenant_id, issue_id)

    issue.status = "ignored"
    issue.ignore_note = payload.note.strip()
    issue.resolved_at = None
    audit_service.record_user_action(
        db,
        user=current.user,
        action=f"ignored the {views.ISSUE_LABELS.get(issue.kind, issue.kind)} on {issue.url}",
        module="seo",
        ip_address=ip,
        context={"issue": issue.kind, "note": issue.ignore_note},
    )
    db.commit()
    return ActionResult(toast=Toast(message="Issue ignored", kind="info"))


@router.post("/audit/{issue_id}/reopen", response_model=ActionResult)
def reopen_issue(
    issue_id: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Put an ignored issue back on the working list."""
    current.require_write(Module.SEO)
    issue = _issue(db, current.tenant_id, issue_id)

    issue.status = "open"
    issue.ignore_note = ""
    issue.resolved_at = None
    audit_service.record_user_action(
        db,
        user=current.user,
        action=f"reopened the {views.ISSUE_LABELS.get(issue.kind, issue.kind)} on {issue.url}",
        module="seo",
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Issue reopened", kind="info"))


def _issue(db: DbSession, tenant_id: str, issue_id: str) -> SeoIssue:
    issue = db.execute(
        select(SeoIssue).where(
            SeoIssue.tenant_id == tenant_id, SeoIssue.id == issue_id
        )
    ).scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue not found")
    return issue
