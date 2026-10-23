"""Billing accounts, fake provider, metering, and entitlements."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.billing import BillingAccount, PlanEntitlement, UsageMeter
from app.models.workspace import AuditLogEntry, Organization
from app.services import billing as billing_svc
from app.services import entitlements
from app.services.billing_provider import FakeBillingProvider, get_provider, plan_catalog
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def bill_db():  # noqa: ANN201
    get_provider("fake").reset()  # type: ignore[attr-defined]
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        AuditLogEntry.__table__,
        BillingAccount.__table__,
        UsageMeter.__table__,
        PlanEntitlement.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-bill",
            wrapped_dek="test",
            dek_version=1,
            plan_name="Trial",
            plan_tier="trial",
            seats_total=3,
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


def test_rls_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for name in ("billing_accounts", "usage_meters", "plan_entitlements"):
        assert name in TENANT_SCOPED_TABLES


def test_default_provider_is_fake() -> None:
    assert get_provider().name == "fake"
    assert isinstance(get_provider("fake"), FakeBillingProvider)


def test_unknown_provider_refused() -> None:
    with pytest.raises(ValueError, match="not enabled"):
        get_provider("stripe")


def test_catalog_has_no_hardcoded_requirement() -> None:
    catalog = plan_catalog()
    assert "trial" in catalog
    assert "price_monthly" in catalog["trial"]


def test_ensure_and_change_plan(bill_db):  # noqa: ANN001
    db, org = bill_db
    cipher = PlainCipher()
    row = billing_svc.ensure_billing_account(
        db, org, cipher, plan_slug="trial", actor_email="a@example.com"
    )
    assert row.provider == "fake"
    assert row.external_customer_id.startswith("fake_")
    assert row.secret_encrypted
    row = billing_svc.change_plan(
        db, org, cipher, plan_slug="growth", seats=8, actor_email="a@example.com"
    )
    assert row.plan_slug == "growth"
    assert row.seats_purchased == 8
    ent = db.execute(
        select(PlanEntitlement).where(PlanEntitlement.tenant_id == org.id)
    ).scalar_one()
    assert ent.plan_slug == "growth"
    assert "features" in ent.entitlements


def test_usage_metering_and_caps(bill_db):  # noqa: ANN001
    db, org = bill_db
    cipher = PlainCipher()
    billing_svc.ensure_billing_account(
        db, org, cipher, plan_slug="trial", actor_email="a@example.com"
    )
    billing_svc.record_usage(
        db, org, metric="agent_runs", quantity=50, actor_email="a@example.com"
    )
    assert billing_svc.usage_within_limit(db, org, "agent_runs") is True
    billing_svc.record_usage(
        db, org, metric="agent_runs", quantity=100, actor_email="a@example.com"
    )
    # trial default cap is 100 — after 150 we are over
    assert billing_svc.usage_within_limit(db, org, "agent_runs") is False


def test_feature_policy_reads_entitlements(bill_db):  # noqa: ANN001
    db, org = bill_db
    cipher = PlainCipher()
    billing_svc.ensure_billing_account(
        db, org, cipher, plan_slug="trial", actor_email="a@example.com"
    )
    policy = entitlements.feature_policy(db, org, "site_analysis")
    assert policy.allowed is True
    assert policy.max_per_day == 3


def test_rejects_unknown_plan(bill_db):  # noqa: ANN001
    db, org = bill_db
    with pytest.raises(ValueError, match="unknown plan"):
        billing_svc.ensure_billing_account(
            db, org, PlainCipher(), plan_slug="platinum", actor_email="a@example.com"
        )
