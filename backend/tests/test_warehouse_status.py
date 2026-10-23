"""Warehouse fake provider + public status payload hygiene."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.warehouse import WarehouseExportJob
from app.models.workspace import AuditLogEntry, Organization, User
from app.services import warehouse as warehouse_service
from app.services.encryption import Ctx
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def wh_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        User.__table__,
        AuditLogEntry.__table__,
        WarehouseExportJob.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-wh",
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
        user = User(
            tenant_id=org.id,
            name="Admin",
            email="admin@example.com",
            email_index="idx",
            role="admin",
            is_active=True,
            is_owner=True,
            email_verified=True,
        )
        session.add_all([org, user])
        session.flush()
        yield session, org, user
    engine.dispose()


def test_rls_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    assert "warehouse_export_jobs" in TENANT_SCOPED_TABLES


def test_fake_export_round_trip(wh_db):  # noqa: ANN001
    db, org, user = wh_db
    cipher = PlainCipher()
    job = warehouse_service.enqueue_export(
        db,
        org,
        cipher,
        provider="fake",
        dataset="metrics",
        destination="fake://metrics-sink",
        actor=user,
    )
    done = warehouse_service.run_export(db, org, cipher, job.id)
    assert done.status == "succeeded"
    assert done.row_count == 1


def test_real_https_destination_fail_closed_without_allowlist(wh_db):  # noqa: ANN001
    db, org, user = wh_db
    with pytest.raises(Exception, match="allow-list"):
        warehouse_service.enqueue_export(
            db,
            org,
            PlainCipher(),
            provider="fake",
            dataset="metrics",
            destination="https://evil.example/path",
            actor=user,
        )


def test_non_fake_provider_refused() -> None:
    with pytest.raises(Exception, match="not enabled"):
        warehouse_service.get_provider("bigquery")


def test_status_payload_has_no_tenant_keys() -> None:
    from app.api.v1.status_page import public_status

    payload = public_status()
    assert "status" in payload
    assert "components" in payload
    blob = str(payload).lower()
    for forbidden in ("tenant", "organization", "email", "user_id", "workspace"):
        assert forbidden not in blob


def test_secret_encrypted_with_context(wh_db):  # noqa: ANN001
    db, org, user = wh_db
    cipher = PlainCipher()
    job = warehouse_service.enqueue_export(
        db,
        org,
        cipher,
        provider="fake",
        dataset="metrics",
        destination="fake://x",
        actor=user,
        secret="not-a-real-key",
    )
    assert cipher.decrypt(job.secret_encrypted, context=Ctx.WAREHOUSE_SECRET) == "not-a-real-key"
