"""MFA enrolment, login challenge completion, and org policy."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.api.v1.auth import _auth_response
from app.core import crypto
from app.core.exceptions import AuthError, InvalidInputError
from app.core.rbac import Module
from app.models.mfa import MfaPendingChallenge
from app.models.workspace import Organization
from app.schemas.auth import AuthResponse, MfaTotpConfirmRequest, MfaVerifyRequest
from app.services import auth as auth_service
from app.services import mfa as mfa_service
from app.services.encryption import OrgCipher

router = APIRouter(prefix="/mfa", tags=["mfa"])


class OrgMfaIn(BaseModel):
    required: bool


class WebAuthnRegisterIn(BaseModel):
    ceremony_token: str
    credential: dict
    nickname: str = ""


class WebAuthnAuthBeginIn(BaseModel):
    mfa_token: str = Field(min_length=8, max_length=200)


@router.post("/totp/begin")
def totp_begin(current: CurrentUserDep) -> dict:
    enrollment = mfa_service.begin_totp_enrollment(current.user)
    return {
        "secret": enrollment.secret,
        "otpauth_url": enrollment.otpauth_url,
        "recovery_codes": list(enrollment.recovery_codes),
    }


@router.post("/totp/confirm")
def totp_confirm(
    body: MfaTotpConfirmRequest,
    current: CurrentUserDep,
    db: DbSession,
) -> dict:
    mfa_service.confirm_totp_enrollment(
        db,
        current.organization,
        current.user,
        current.cipher,
        secret=body.secret,
        code=body.code,
        recovery_codes=body.recovery_codes,
        actor_email=current.user.email,
    )
    db.commit()
    return {"mfa_enabled": True}


@router.post("/disable")
def disable_own(
    current: CurrentUserDep,
    db: DbSession,
) -> dict:
    mfa_service.disable_mfa(
        db, current.organization, current.user, actor_email=current.user.email
    )
    db.commit()
    return {"mfa_enabled": False}


@router.post("/org/require")
def org_require(
    body: OrgMfaIn,
    current: CurrentUserDep,
    db: DbSession,
) -> dict:
    current.require_write(Module.ADMIN)
    mfa_service.require_step_up(current.claims, current.organization, current.user)
    mfa_service.set_org_mfa_required(
        db, current.organization, required=body.required, actor_email=current.user.email
    )
    db.commit()
    return {"mfa_required": current.organization.mfa_required}


@router.post("/verify", response_model=AuthResponse)
def verify_login_mfa(
    body: MfaVerifyRequest,
    request: Request,
    db: DbSession,
    ip: ClientIp,
) -> AuthResponse:
    if body.totp_code:
        user, org = mfa_service.verify_totp_for_challenge(
            db, mfa_token=body.mfa_token, code=body.totp_code
        )
    elif body.recovery_code:
        user, org = mfa_service.verify_recovery_for_challenge(
            db, mfa_token=body.mfa_token, recovery_code=body.recovery_code
        )
    elif body.webauthn_credential:
        row = db.execute(
            select(MfaPendingChallenge).where(
                MfaPendingChallenge.token_hash == crypto.hash_token(body.mfa_token),
            )
        ).scalar_one_or_none()
        if row is None:
            raise AuthError("MFA challenge expired or invalid")
        org_row = db.get(Organization, row.tenant_id)
        if org_row is None:
            raise AuthError("MFA challenge expired or invalid")
        user, org = mfa_service.finish_webauthn_authentication(
            db,
            OrgCipher.for_org(org_row),
            mfa_token=body.mfa_token,
            credential=body.webauthn_credential,
        )
    else:
        raise InvalidInputError("Provide a TOTP, recovery code, or passkey response")
    result = auth_service.complete_mfa_login(
        db,
        user=user,
        organization=org,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip or None,
    )
    db.commit()
    return _auth_response(db, result)


@router.post("/webauthn/register/begin")
def webauthn_register_begin(
    current: CurrentUserDep,
    db: DbSession,
) -> dict:
    payload = mfa_service.begin_webauthn_registration(
        db, current.organization, current.cipher, current.user
    )
    db.commit()
    return payload


@router.post("/webauthn/register/finish")
def webauthn_register_finish(
    body: WebAuthnRegisterIn,
    current: CurrentUserDep,
    db: DbSession,
) -> dict:
    row = mfa_service.finish_webauthn_registration(
        db,
        current.organization,
        current.cipher,
        current.user,
        ceremony_token=body.ceremony_token,
        credential=body.credential,
        nickname=body.nickname,
        actor_email=current.user.email,
    )
    db.commit()
    return {"id": row.id, "nickname": row.nickname}


@router.post("/webauthn/authenticate/begin")
def webauthn_auth_begin(
    body: WebAuthnAuthBeginIn,
    db: DbSession,
) -> dict:
    row = db.execute(
        select(MfaPendingChallenge).where(
            MfaPendingChallenge.token_hash == crypto.hash_token(body.mfa_token),
            MfaPendingChallenge.consumed_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        raise AuthError("MFA challenge expired or invalid")
    org = db.get(Organization, row.tenant_id)
    if org is None:
        raise AuthError("MFA challenge expired or invalid")
    payload = mfa_service.begin_webauthn_authentication(
        db, org, OrgCipher.for_org(org), mfa_token=body.mfa_token
    )
    db.commit()
    return payload
