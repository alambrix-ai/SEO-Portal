"""Compliance retention, subject requests, signed audit exports, AI governance."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, utcnow
from app.models.compliance import (
    AiGovernancePolicy,
    AuditExportJob,
    RetentionPolicy,
    SubjectRequest,
)
from app.models.workspace import AuditLogEntry, Organization
from app.services import compliance
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_retention_rejects_unknown_class(comp_db):  # noqa: ANN001
    db, org = comp_db
    with pytest.raises(ValueError, match="unsupported"):
        compliance.upsert_retention(
            db, org, data_class="not_a_class", ttl_days=30, actor_email="a@example.com"
        )


def test_rls_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for name in (
        "retention_policies",
        "subject_requests",
        "audit_export_jobs",
        "ai_governance_policies",
    ):
        assert name in TENANT_SCOPED_TABLES


@pytest.fixture
def comp_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        AuditLogEntry.__table__,
        RetentionPolicy.__table__,
        SubjectRequest.__table__,
        AuditExportJob.__table__,
        AiGovernancePolicy.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-comp",
            wrapped_dek="test",
            dek_version=1,
            plan_name="Enterprise",
            plan_tier="enterprise",
            seats_total=10,
            is_active=True,
            data_region="eu-central",
            settings={},
            ip_allowlist_enabled=False,
            mfa_required=False,
        )
        session.add(org)
        session.flush()
        yield session, org
    engine.dispose()


def test_retention_upsert(comp_db):  # noqa: ANN001
    db, org = comp_db
    row = compliance.upsert_retention(
        db, org, data_class="agent_runs", ttl_days=90, actor_email="a@example.com"
    )
    assert row.ttl_days == 90


def test_subject_export_roundtrip(comp_db):  # noqa: ANN001
    db, org = comp_db
    cipher = PlainCipher()
    req = compliance.open_subject_request(
        db,
        org,
        cipher,
        kind="export",
        email="subject@example.com",
        requested_by_id=None,
        actor_email="a@example.com",
    )
    compliance.complete_subject_export(
        db,
        org,
        cipher,
        req,
        payload={"modules": ["profile"], "note": "redacted"},
        actor_email="a@example.com",
    )
    assert req.status == "completed"
    assert req.result_encrypted


def test_signed_audit_export(comp_db):  # noqa: ANN001
    db, org = comp_db
    cipher = PlainCipher()
    now = utcnow()
    db.add(
        AuditLogEntry(
            tenant_id=org.id,
            at=now,
            actor="a@example.com",
            actor_type="user",
            action="test.action",
            module="admin",
            context={},
        )
    )
    db.flush()
    job = compliance.build_signed_audit_export(
        db,
        org,
        cipher,
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(hours=1),
        created_by_id=None,
        actor_email="a@example.com",
        signing_key=b"test-key",
    )
    assert job.row_count == 1
    assert len(job.signature) == 64


def test_ai_governance_allowlist(comp_db):  # noqa: ANN001
    db, org = comp_db
    compliance.upsert_ai_governance(
        db,
        org,
        model_allowlist=["openai_gpt"],
        agent_cost_caps={"seo_article_writer": 100000},
        actor_email="a@example.com",
    )
    assert compliance.model_allowed(db, org.id, "openai_gpt") is True
    assert compliance.model_allowed(db, org.id, "other") is False


def test_siem_url_rejects_private(comp_db):  # noqa: ANN001
    db, org = comp_db
    cipher = PlainCipher()
    now = utcnow()
    with pytest.raises(Exception):
        compliance.build_signed_audit_export(
            db,
            org,
            cipher,
            window_start=now,
            window_end=now,
            siem_url="http://127.0.0.1/siem",
            created_by_id=None,
            actor_email="a@example.com",
        )
