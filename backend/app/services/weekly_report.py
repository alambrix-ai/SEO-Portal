"""Assemble and optionally notify the weekly autopilot digest."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.metrics import DailyMetric
from app.models.reports import ReportArtifact, ScheduledReport
from app.models.seo import SeoIssue
from app.models.workspace import Organization, User
from app.services.encryption import Ctx, OrgCipher

KIND = "weekly_autopilot"


@dataclass(frozen=True, slots=True)
class ReportSections:
    subject: str
    body: str
    sections: tuple[str, ...]


def build_sections(
    db: Session,
    *,
    org: Organization,
) -> ReportSections:
    """Stable, deterministic sections from tenant-scoped tables only."""
    tid = org.id
    week_ago = utcnow() - timedelta(days=7)
    metrics = list(
        db.execute(
            select(DailyMetric)
            .where(DailyMetric.tenant_id == tid, DailyMetric.day >= week_ago.date())
            .order_by(DailyMetric.day.asc())
        ).scalars()
    )
    sessions = sum(int(m.organic_sessions or 0) for m in metrics)
    spend = sum(float(m.ad_spend or 0) for m in metrics)
    conversions = sum(int(m.conversions or 0) for m in metrics)
    runs = sum(int(m.agent_runs or 0) for m in metrics)

    pending = db.execute(
        select(func.count())
        .select_from(ApprovalItem)
        .where(
            ApprovalItem.tenant_id == tid,
            ApprovalItem.status == ApprovalStatus.PENDING.value,
        )
    ).scalar_one()
    open_issues = db.execute(
        select(func.count())
        .select_from(SeoIssue)
        .where(SeoIssue.tenant_id == tid, SeoIssue.status == "open")
    ).scalar_one()

    name = (org.name or "Workspace").strip() or "Workspace"
    subject = f"Weekly autopilot digest — {name}"
    # Subject must not carry personal emails / free-form PII.
    if "@" in subject:
        subject = "Weekly autopilot digest"

    lines = [
        f"# Weekly autopilot — {name}",
        "",
        "## Traffic & paid (last 7 days)",
        f"- Organic sessions: {sessions}",
        f"- Ad spend: {spend:.2f}",
        f"- Conversions: {conversions}",
        f"- Agent runs: {runs}",
        "",
        "## Approvals & technical SEO",
        f"- Pending approvals: {int(pending or 0)}",
        f"- Open technical issues: {int(open_issues or 0)}",
        "",
        "_Generated automatically. Open the console for detail._",
    ]
    body = "\n".join(lines)
    return ReportSections(
        subject=subject[:200],
        body=body,
        sections=("traffic", "approvals", "technical"),
    )


def member_recipient_ids(db: Session, *, tenant_id: str, requested: list[str]) -> list[str]:
    """Intersect requested ids with active workspace members only."""
    if not requested:
        # Default: every active member.
        rows = db.execute(
            select(User.id).where(User.tenant_id == tenant_id, User.is_active.is_(True))
        ).scalars()
        return [str(r) for r in rows]
    allowed = set(
        db.execute(
            select(User.id).where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.id.in_(list(requested)),
            )
        ).scalars()
    )
    return [uid for uid in requested if uid in allowed]


def persist_artifact(
    db: Session,
    *,
    org: Organization,
    schedule: ScheduledReport | None,
    sections: ReportSections,
    cipher: OrgCipher,
) -> ReportArtifact:
    artifact = ReportArtifact(
        tenant_id=org.id,
        schedule_id=schedule.id if schedule else None,
        kind=KIND,
        subject=sections.subject,
        body_encrypted=cipher.encrypt(sections.body, context=Ctx.REPORT_BODY),
        generated_at=utcnow(),
    )
    db.add(artifact)
    if schedule is not None:
        schedule.last_sent_at = utcnow()
    db.flush()
    return artifact


def notify_digest(
    *,
    connectors: dict,
    subject: str,
    body: str,
) -> int:
    """Send via every connected NotificationConnector. Returns send count."""
    sent = 0
    for connector in connectors.values():
        if not connector.supports(Capability.SEND_NOTIFICATION):
            continue
        try:
            if connector.notify(subject=subject, message=body):  # type: ignore[union-attr]
                sent += 1
        except Exception:  # noqa: BLE001
            continue
    return sent


def run_for_org(
    db: Session,
    *,
    org: Organization,
    connectors: dict | None = None,
) -> ReportArtifact:
    cipher = OrgCipher.for_org(org)
    schedule = db.execute(
        select(ScheduledReport).where(
            ScheduledReport.tenant_id == org.id,
            ScheduledReport.kind == KIND,
            ScheduledReport.status == "on",
        )
    ).scalar_one_or_none()
    sections = build_sections(db, org=org)
    artifact = persist_artifact(
        db, org=org, schedule=schedule, sections=sections, cipher=cipher
    )
    if connectors:
        notify_digest(connectors=connectors, subject=sections.subject, body=sections.body)
    return artifact
