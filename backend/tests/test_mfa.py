"""TOTP MFA enrolment, challenge, recovery codes, and step-up."""
from __future__ import annotations

import pyotp
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.mfa import MfaPendingChallenge, MfaRecoveryCode, WebAuthnCredential
from app.models.workspace import AuditLogEntry, Organization, User
from app.services import mfa as mfa_service
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture(autouse=True)
def _reset_mfa_limits():  # noqa: ANN201
    mfa_service.reset_verify_failures()
    yield
    mfa_service.reset_verify_failures()


@pytest.fixture
def mfa_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        User.__table__,
        AuditLogEntry.__table__,
        MfaRecoveryCode.__table__,
        WebAuthnCredential.__table__,
        MfaPendingChallenge.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-mfa",
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
            mfa_enabled=False,
        )
        session.add_all([org, user])
        session.flush()
        yield session, org, user
    engine.dispose()


def test_rls_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for name in ("mfa_recovery_codes", "webauthn_credentials", "mfa_pending_challenges"):
        assert name in TENANT_SCOPED_TABLES


def test_totp_enroll_and_login_challenge(mfa_db):  # noqa: ANN001
    db, org, user = mfa_db
    cipher = PlainCipher()
    enrollment = mfa_service.begin_totp_enrollment(user)
    code = pyotp.TOTP(enrollment.secret).now()
    mfa_service.confirm_totp_enrollment(
        db,
        org,
        user,
        cipher,
        secret=enrollment.secret,
        code=code,
        recovery_codes=list(enrollment.recovery_codes),
        actor_email=user.email,
    )
    assert user.mfa_enabled is True
    assert mfa_service.needs_login_mfa(user, org) is True
    _ch, raw = mfa_service.create_login_challenge(db, org, user)
    verified_user, verified_org = mfa_service.verify_totp_for_challenge(
        db,
        cipher,
        mfa_token=raw,
        code=pyotp.TOTP(enrollment.secret).now(),
    )
    assert verified_user.id == user.id
    assert verified_org.id == org.id


def test_totp_replay_same_step_rejected(mfa_db):  # noqa: ANN001
    db, org, user = mfa_db
    cipher = PlainCipher()
    enrollment = mfa_service.begin_totp_enrollment(user)
    mfa_service.confirm_totp_enrollment(
        db,
        org,
        user,
        cipher,
        secret=enrollment.secret,
        code=pyotp.TOTP(enrollment.secret).now(),
        recovery_codes=list(enrollment.recovery_codes),
        actor_email=user.email,
    )
    _ch, raw = mfa_service.create_login_challenge(db, org, user)
    code = pyotp.TOTP(enrollment.secret).now()
    # Force last_totp_step to current step before verify by verifying once on a
    # separate challenge then reusing code on same challenge object mid-flight:
    challenge = mfa_service._load_challenge(db, raw)
    import time

    challenge.last_totp_step = int(time.time()) // 30
    db.flush()
    with pytest.raises(Exception, match="already used"):
        mfa_service.verify_totp_for_challenge(db, cipher, mfa_token=raw, code=code)


def test_recovery_code_single_use(mfa_db):  # noqa: ANN001
    db, org, user = mfa_db
    cipher = PlainCipher()
    enrollment = mfa_service.begin_totp_enrollment(user)
    mfa_service.confirm_totp_enrollment(
        db,
        org,
        user,
        cipher,
        secret=enrollment.secret,
        code=pyotp.TOTP(enrollment.secret).now(),
        recovery_codes=list(enrollment.recovery_codes),
        actor_email=user.email,
    )
    _ch, raw = mfa_service.create_login_challenge(db, org, user)
    mfa_service.verify_recovery_for_challenge(
        db, mfa_token=raw, recovery_code=enrollment.recovery_codes[0]
    )
    _ch2, raw2 = mfa_service.create_login_challenge(db, org, user)
    with pytest.raises(Exception):
        mfa_service.verify_recovery_for_challenge(
            db, mfa_token=raw2, recovery_code=enrollment.recovery_codes[0]
        )


def test_step_up_required(mfa_db):  # noqa: ANN001
    db, org, user = mfa_db
    org.mfa_required = True
    user.mfa_enabled = True
    with pytest.raises(Exception, match="authenticator"):
        mfa_service.require_step_up({}, org, user)
    mfa_service.require_step_up({"mfa": True}, org, user)


def test_org_require_audited(mfa_db):  # noqa: ANN001
    db, org, user = mfa_db
    mfa_service.set_org_mfa_required(db, org, required=True, actor_email=user.email)
    assert org.mfa_required is True
