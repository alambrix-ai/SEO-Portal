"""Agency parent accounts, client links, and white-label unit tests."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id
from app.models.agency import Agency, AgencyClientLink, AgencyMembership, WhiteLabelSettings
from app.models.workspace import AuditLogEntry, Organization
from app.services import agency as agency_svc
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def agency_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        AuditLogEntry.__table__,
        Agency.__table__,
        AgencyMembership.__table__,
        AgencyClientLink.__table__,
        WhiteLabelSettings.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-agency",
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
        client = Organization(
            id=new_id(),
            name="Client Co",
            slug="client-co",
            wrapped_dek="test",
            dek_version=1,
            plan_name="Growth",
            plan_tier="growth",
            seats_total=5,
            is_active=True,
            data_region="eu-central",
            settings={},
            ip_allowlist_enabled=False,
            mfa_required=False,
        )
        session.add_all([org, client])
        session.flush()
        yield session, org, client
    engine.dispose()


def test_agency_tables_have_no_tenant_id() -> None:
    """Agency access is membership-based, not shared tenant_id (PLAN Phase 11)."""
    for model in (Agency, AgencyMembership, AgencyClientLink, WhiteLabelSettings):
        assert "tenant_id" not in model.__table__.c


def test_create_and_link_client(agency_db):  # noqa: ANN001
    db, org, client = agency_db
    agency = agency_svc.create_agency(
        db,
        name="Partner Agency",
        slug="partner-agency",
        owner_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    link = agency_svc.link_client(
        db,
        agency,
        client,
        access_level="manage",
        actor_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    assert link.client_tenant_id == client.id
    ids = agency_svc.list_client_tenant_ids(db, agency, email="owner@agency.example")
    assert client.id in ids


def test_member_cannot_link_without_admin(agency_db):  # noqa: ANN001
    db, org, client = agency_db
    agency = agency_svc.create_agency(
        db,
        name="Partner",
        slug="partner-2",
        owner_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    agency_svc.add_member(
        db,
        agency,
        email="member@agency.example",
        role="member",
        actor_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    with pytest.raises(ValueError, match="agency admin"):
        agency_svc.link_client(
            db,
            agency,
            client,
            actor_email="member@agency.example",
            audit_tenant_id=org.id,
        )


def test_white_label_encrypts_branding(agency_db):  # noqa: ANN001
    db, org, _client = agency_db
    cipher = PlainCipher()
    agency = agency_svc.create_agency(
        db,
        name="Brand Co",
        slug="brand-co",
        owner_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    row = agency_svc.upsert_white_label(
        db,
        agency,
        cipher,
        custom_domain="portal.brand.example",
        email_from_name="Brand Portal",
        branding={"primary": "#0a0a0a", "logo_url": "https://cdn.example/logo.png"},
        brand_safety={"blocked_terms": ["competitor"]},
        enabled=True,
        actor_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    assert row.custom_domain == "portal.brand.example"
    assert row.branding_encrypted  # stored via cipher.encrypt_json (Ctx.WHITE_LABEL)
    assert agency_svc.decrypt_branding(cipher, row)["primary"] == "#0a0a0a"


def test_white_label_rejects_bad_domain(agency_db):  # noqa: ANN001
    db, org, _client = agency_db
    cipher = PlainCipher()
    agency = agency_svc.create_agency(
        db,
        name="Bad Dom",
        slug="bad-dom",
        owner_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    with pytest.raises(ValueError, match="custom_domain"):
        agency_svc.upsert_white_label(
            db,
            agency,
            cipher,
            custom_domain="not a host!!",
            actor_email="owner@agency.example",
            audit_tenant_id=org.id,
        )


def test_non_member_cannot_list_clients(agency_db):  # noqa: ANN001
    db, org, client = agency_db
    agency = agency_svc.create_agency(
        db,
        name="Closed",
        slug="closed-ag",
        owner_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    agency_svc.link_client(
        db,
        agency,
        client,
        actor_email="owner@agency.example",
        audit_tenant_id=org.id,
    )
    with pytest.raises(ValueError, match="membership"):
        agency_svc.list_client_tenant_ids(db, agency, email="stranger@example.com")
