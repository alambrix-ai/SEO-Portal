"""Weekly autopilot report sections and recipient allowlisting."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.metrics import DailyMetric
from app.models.reports import ReportArtifact, ScheduledReport
from app.models.seo import SeoIssue, SeoPage, PageStatus
from app.models.workspace import User
from app.services import weekly_report
from app.services.encryption import Ctx
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_sections_are_stable_for_fixture() -> None:
    engine = create_engine("sqlite://")
    tables = [
        DailyMetric.__table__,
        ApprovalItem.__table__,
        SeoIssue.__table__,
        SeoPage.__table__,
        ScheduledReport.__table__,
        ReportArtifact.__table__,
        User.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as db:
        page = SeoPage(
            id=new_id(),
            tenant_id=TENANT,
            url="https://acme.test/",
            title="Home",
            cms_id="1",
            cms_slug="wordpress",
            body_encrypted="x",
            status=PageStatus.LIVE.value,
            schema_types=[],
            missing_topics=[],
            target_keywords=[],
        )
        db.add(page)
        db.flush()
        db.add_all(
            [
                DailyMetric(
                    id=new_id(),
                    tenant_id=TENANT,
                    day=date(2026, 10, 20),
                    organic_sessions=10,
                    ad_spend=5.0,
                    conversions=1,
                    agent_runs=2,
                ),
                ApprovalItem(
                    id=new_id(),
                    tenant_id=TENANT,
                    type="t",
                    title="t",
                    status=ApprovalStatus.PENDING.value,
                    submitted_at=utcnow(),
                ),
                SeoIssue(
                    id=new_id(),
                    tenant_id=TENANT,
                    page_id=page.id,
                    url=page.url,
                    kind="missing_h1",
                    severity="medium",
                    target="",
                    summary="no h1",
                    recommendation="add one",
                    evidence={},
                    status="open",
                    first_seen_at=utcnow(),
                    last_seen_at=utcnow(),
                ),
            ]
        )
        db.flush()
        org = SimpleNamespace(id=TENANT, name="Acme")
        a = weekly_report.build_sections(db, org=org)  # type: ignore[arg-type]
        b = weekly_report.build_sections(db, org=org)  # type: ignore[arg-type]
        assert a.body == b.body
        assert a.sections == ("traffic", "approvals", "technical")
        assert "Organic sessions: 10" in a.body
        assert "Pending approvals: 1" in a.body
        assert "@" not in a.subject
        artifact = weekly_report.persist_artifact(
            db,
            org=org,  # type: ignore[arg-type]
            schedule=None,
            sections=a,
            cipher=PlainCipher(),  # type: ignore[arg-type]
        )
        assert artifact.subject == a.subject
        assert PlainCipher().decrypt(artifact.body_encrypted, context=Ctx.REPORT_BODY) == a.body
        rows = list(db.execute(select(ReportArtifact)).scalars())
        assert len(rows) == 1
    engine.dispose()


def test_recipients_must_be_workspace_members() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__])
    with Session(engine) as db:
        member = User(
            id=new_id(),
            tenant_id=TENANT,
            email="member@acme.test",
            email_index="x",
            name="Member",
            role="admin",
            is_active=True,
        )
        db.add(member)
        db.flush()
        allowed = weekly_report.member_recipient_ids(
            db, tenant_id=TENANT, requested=[member.id, "99999999-9999-4999-8999-999999999999"]
        )
        assert allowed == [member.id]
    engine.dispose()


def test_report_tables_under_rls() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    assert "scheduled_reports" in TENANT_SCOPED_TABLES
    assert "report_artifacts" in TENANT_SCOPED_TABLES
