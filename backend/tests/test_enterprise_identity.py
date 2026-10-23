"""Enterprise SSO, SCIM, custom roles, and IP allowlist unit tests."""
from __future__ import annotations

import base64
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.core.rbac import Access, Module, merge_access, validate_permission_map
from app.db.base import Base, new_id
from app.models.enterprise_identity import (
    CustomRole,
    IpAllowlistEntry,
    ScimToken,
    SsoConnection,
)
from app.models.membership import WorkspaceMembership
from app.models.workspace import Organization, User
from app.services import custom_roles, enterprise_sso, ip_allowlist, scim
from app.services.encryption import Ctx
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def _b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_oidc_alg_none_rejected_even_when_enabled(monkeypatch):  # noqa: ANN001
    """Legacy unsigned tokens must never verify — covered fully in test_sso_crypto."""
    monkeypatch.setattr(enterprise_sso.settings, "sso_login_enabled", True)
    now = int(time.time())
    token = ".".join(
        [
            _b64url({"alg": "none", "typ": "JWT"}),
            _b64url(
                {
                    "iss": "https://idp.example.com",
                    "aud": "synaprank",
                    "sub": "user-1",
                    "email": "a@example.com",
                    "exp": now + 600,
                }
            ),
            "",
        ]
    )
    with pytest.raises(ValueError):
        enterprise_sso.verify_oidc_id_token(
            id_token=token,
            expected_issuer="https://idp.example.com",
            expected_audience="synaprank",
            public_key_pem="-----BEGIN PUBLIC KEY-----\nMFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAExample\n-----END PUBLIC KEY-----\n",
        )


def test_oidc_fail_closed_by_default() -> None:
    with pytest.raises(ValueError, match="disabled"):
        enterprise_sso.verify_oidc_id_token(
            id_token="a.b.c",
            expected_issuer="https://idp.example.com",
            expected_audience="synaprank",
            public_key_pem="x",
        )


def test_saml_fail_closed_by_default() -> None:
    with pytest.raises(ValueError, match="disabled"):
        enterprise_sso.verify_saml_assertion_xml(
            xml_bytes=b"<Assertion/>",
            idp_cert_pem="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        )


def test_custom_role_merge_widens_only() -> None:
    overlay = validate_permission_map({"seo": "full", "admin": "view"})
    merged = merge_access("client", overlay)
    # Client base has SEO none — overlay can widen to full
    assert merged[Module.SEO] is Access.FULL
    # Client admin is none; view is wider
    assert merged[Module.ADMIN] is Access.VIEW


def test_metadata_url_rejects_private() -> None:
    with pytest.raises(Exception):
        enterprise_sso.validate_connection_input(
            slug="okta",
            name="Okta",
            protocol="oidc",
            metadata_url="http://127.0.0.1/meta",
        )


def test_rls_tables_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for name in (
        "sso_connections",
        "scim_tokens",
        "custom_roles",
        "ip_allowlist_entries",
    ):
        assert name in TENANT_SCOPED_TABLES


@pytest.fixture
def ent_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        User.__table__,
        WorkspaceMembership.__table__,
        SsoConnection.__table__,
        ScimToken.__table__,
        CustomRole.__table__,
        IpAllowlistEntry.__table__,
    ]
    # Audit table may be needed by audit.record — skip audit in these tests by
    # monkeypatching if needed. Prefer creating audit_log if present.
    from app.models.workspace import AuditLogEntry

    tables.append(AuditLogEntry.__table__)
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme",
            wrapped_dek="test",
            dek_version=1,
            plan_name="Enterprise",
            plan_tier="enterprise",
            seats_total=25,
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


def test_scim_mint_and_auth(ent_db, monkeypatch):  # noqa: ANN001
    db, org = ent_db
    cipher = PlainCipher()
    minted = scim.mint_token(
        db, org, label="IdP", created_by_id=None, actor_email="admin@example.com"
    )
    assert minted.raw_token.startswith("scim_")
    found = scim.authenticate_bearer(db, minted.raw_token)
    assert found is not None
    assert found[0].id == org.id


def test_ip_allowlist_fail_closed(ent_db):  # noqa: ANN001
    db, org = ent_db
    org.ip_allowlist_enabled = True
    db.flush()
    assert ip_allowlist.is_allowed(db, org, "203.0.113.10") is False
    ip_allowlist.add_entry(
        db, org, cidr="203.0.113.0/24", label="office", actor_email="a@example.com"
    )
    assert ip_allowlist.is_allowed(db, org, "203.0.113.10") is True
    assert ip_allowlist.is_allowed(db, org, "198.51.100.1") is False


def test_ip_allowlist_ipv6_cidr(ent_db):  # noqa: ANN001
    db, org = ent_db
    org.ip_allowlist_enabled = True
    ip_allowlist.add_entry(
        db, org, cidr="2001:db8::/32", label="v6", actor_email="a@example.com"
    )
    assert ip_allowlist.is_allowed(db, org, "2001:db8::1") is True
    assert ip_allowlist.is_allowed(db, org, "2001:db9::1") is False


def test_ip_allowlist_lockout_safety(ent_db):  # noqa: ANN001
    db, org = ent_db
    ip_allowlist.add_entry(
        db, org, cidr="203.0.113.0/24", label="office", actor_email="a@example.com"
    )
    with pytest.raises(Exception, match="locked out|current IP"):
        ip_allowlist.set_enabled(
            db,
            org,
            enabled=True,
            actor_email="a@example.com",
            actor_ip="198.51.100.9",
        )
    ip_allowlist.set_enabled(
        db,
        org,
        enabled=True,
        actor_email="a@example.com",
        actor_ip="203.0.113.50",
    )
    assert org.ip_allowlist_enabled is True


def test_group_role_mapping() -> None:
    assert scim.map_group_to_role(["seo"]) == "seo"
    assert scim.map_group_to_role(["unknown"]) == "client"
