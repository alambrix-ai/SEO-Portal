"""TOTP + WebAuthn MFA enrolment, verification, recovery codes, step-up."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import pyotp
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.core.exceptions import AuthError, ForbiddenError, InvalidInputError
from app.db.base import utcnow
from app.models.mfa import MfaPendingChallenge, MfaRecoveryCode, WebAuthnCredential
from app.models.workspace import Organization, User
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

TOTP_ISSUER = "Synaprank"
RECOVERY_COUNT = 10
CHALLENGE_TTL_SECONDS = 300
#: In-process rate limit: (user_id, window) → failures.
_VERIFY_FAILURES: dict[str, list[float]] = {}
_VERIFY_LOCK = threading.Lock()
_MAX_VERIFY_PER_MINUTE = 8


def _rate_limit_verify(user_id: str) -> None:
    now = time.monotonic()
    with _VERIFY_LOCK:
        bucket = [t for t in _VERIFY_FAILURES.get(user_id, []) if now - t < 60]
        if len(bucket) >= _MAX_VERIFY_PER_MINUTE:
            raise AuthError("Too many MFA attempts. Wait a minute and try again.")
        _VERIFY_FAILURES[user_id] = bucket


def _note_verify_failure(user_id: str) -> None:
    now = time.monotonic()
    with _VERIFY_LOCK:
        bucket = [t for t in _VERIFY_FAILURES.get(user_id, []) if now - t < 60]
        bucket.append(now)
        _VERIFY_FAILURES[user_id] = bucket


def reset_verify_failures() -> None:
    with _VERIFY_LOCK:
        _VERIFY_FAILURES.clear()


@dataclass(frozen=True, slots=True)
class TotpEnrollment:
    secret: str
    otpauth_url: str
    recovery_codes: tuple[str, ...]


def begin_totp_enrollment(user: User) -> TotpEnrollment:
    """Return a fresh TOTP secret + recovery codes (not yet persisted)."""
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    url = totp.provisioning_uri(name=user.email or user.id, issuer_name=TOTP_ISSUER)
    codes = tuple(secrets.token_hex(4) for _ in range(RECOVERY_COUNT))
    return TotpEnrollment(secret=secret, otpauth_url=url, recovery_codes=codes)


def confirm_totp_enrollment(
    db: Session,
    org: Organization,
    user: User,
    cipher: OrgCipher,
    *,
    secret: str,
    code: str,
    recovery_codes: list[str],
    actor_email: str,
) -> User:
    secret = (secret or "").strip().replace(" ", "")
    if len(secret) < 16:
        raise InvalidInputError("Invalid authenticator secret")
    totp = pyotp.TOTP(secret)
    if not totp.verify(code.strip(), valid_window=1):
        raise InvalidInputError("That authenticator code is not valid")
    # Per-workspace DEK — never store the raw TOTP seed.
    user.mfa_secret = cipher.encrypt(secret, context=Ctx.MFA_TOTP_SECRET)
    user.mfa_enabled = True
    # Replace recovery codes.
    for old in db.execute(
        select(MfaRecoveryCode).where(
            MfaRecoveryCode.tenant_id == org.id, MfaRecoveryCode.user_id == user.id
        )
    ).scalars():
        db.delete(old)
    for raw in recovery_codes:
        raw = (raw or "").strip().lower()
        if len(raw) < 8:
            continue
        db.add(
            MfaRecoveryCode(
                tenant_id=org.id,
                user_id=user.id,
                code_hash=crypto.hash_token(raw),
            )
        )
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="mfa.totp.enroll",
        module="admin",
        context={"user_fp": crypto.fingerprint(user.id)},
    )
    return user


def disable_mfa(
    db: Session, org: Organization, user: User, *, actor_email: str, admin_reset: bool = False
) -> User:
    user.mfa_enabled = False
    user.mfa_secret = None
    for row in db.execute(
        select(MfaRecoveryCode).where(
            MfaRecoveryCode.tenant_id == org.id, MfaRecoveryCode.user_id == user.id
        )
    ).scalars():
        db.delete(row)
    for row in db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.tenant_id == org.id, WebAuthnCredential.user_id == user.id
        )
    ).scalars():
        db.delete(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="mfa.reset" if admin_reset else "mfa.disable",
        module="admin",
        context={"user_fp": crypto.fingerprint(user.id), "admin_reset": admin_reset},
    )
    return user


def set_org_mfa_required(
    db: Session, org: Organization, *, required: bool, actor_email: str
) -> Organization:
    org.mfa_required = bool(required)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="mfa.org.require",
        module="admin",
        context={"required": org.mfa_required},
    )
    return org


def create_login_challenge(
    db: Session, org: Organization, user: User
) -> tuple[MfaPendingChallenge, str]:
    raw = f"mfa_{crypto.new_url_token(32)}"
    row = MfaPendingChallenge(
        tenant_id=org.id,
        user_id=user.id,
        token_hash=crypto.hash_token(raw),
        expires_at=utcnow() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    )
    db.add(row)
    db.flush()
    return row, raw


def _load_challenge(db: Session, raw_token: str) -> MfaPendingChallenge:
    if not raw_token or not raw_token.startswith("mfa_"):
        raise AuthError("MFA challenge expired or invalid")
    row = db.execute(
        select(MfaPendingChallenge).where(
            MfaPendingChallenge.token_hash == crypto.hash_token(raw_token),
            MfaPendingChallenge.consumed_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at < utcnow():
        raise AuthError("MFA challenge expired or invalid")
    return row


def _totp_secret(cipher: OrgCipher, user: User) -> str:
    if not user.mfa_secret:
        raise AuthError("MFA challenge expired or invalid")
    return cipher.decrypt(user.mfa_secret, context=Ctx.MFA_TOTP_SECRET)


def verify_totp_for_challenge(
    db: Session,
    cipher: OrgCipher | None = None,
    *,
    mfa_token: str,
    code: str,
) -> tuple[User, Organization]:
    challenge = _load_challenge(db, mfa_token)
    user = db.get(User, challenge.user_id)
    org = db.get(Organization, challenge.tenant_id)
    if user is None or org is None or not user.mfa_enabled or not user.mfa_secret:
        raise AuthError("MFA challenge expired or invalid")
    org_cipher = cipher or OrgCipher.for_org(org)
    _rate_limit_verify(user.id)
    totp = pyotp.TOTP(_totp_secret(org_cipher, user))
    # Replay protection: refuse the same time-step twice on this challenge.
    step = int(time.time()) // 30
    if challenge.last_totp_step and challenge.last_totp_step == step:
        _note_verify_failure(user.id)
        raise AuthError("That authenticator code was already used")
    if not totp.verify(code.strip(), valid_window=1):
        _note_verify_failure(user.id)
        raise AuthError("That authenticator code is not valid")
    challenge.last_totp_step = step
    challenge.consumed_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action="mfa.totp.verify",
        module="auth",
        context={"event": "mfa_ok"},
    )
    return user, org


def verify_recovery_for_challenge(
    db: Session, *, mfa_token: str, recovery_code: str
) -> tuple[User, Organization]:
    challenge = _load_challenge(db, mfa_token)
    user = db.get(User, challenge.user_id)
    org = db.get(Organization, challenge.tenant_id)
    if user is None or org is None:
        raise AuthError("MFA challenge expired or invalid")
    _rate_limit_verify(user.id)
    digest = crypto.hash_token((recovery_code or "").strip().lower())
    row = db.execute(
        select(MfaRecoveryCode).where(
            MfaRecoveryCode.tenant_id == org.id,
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.code_hash == digest,
            MfaRecoveryCode.used_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        _note_verify_failure(user.id)
        raise AuthError("That recovery code is not valid")
    row.used_at = utcnow()
    challenge.consumed_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action="mfa.recovery.verify",
        module="auth",
        context={"event": "mfa_ok"},
    )
    return user, org


def available_methods(db: Session, user: User) -> list[str]:
    methods: list[str] = []
    if user.mfa_enabled and user.mfa_secret:
        methods.append("totp")
    has_keys = db.execute(
        select(WebAuthnCredential.id).where(
            WebAuthnCredential.tenant_id == user.tenant_id,
            WebAuthnCredential.user_id == user.id,
        ).limit(1)
    ).scalar_one_or_none()
    if has_keys:
        methods.append("webauthn")
    has_recovery = db.execute(
        select(MfaRecoveryCode.id).where(
            MfaRecoveryCode.tenant_id == user.tenant_id,
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.used_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if has_recovery:
        methods.append("recovery")
    return methods


def needs_login_mfa(user: User, org: Organization) -> bool:
    """True when the caller must complete MFA before receiving session tokens."""
    if user.mfa_enabled and user.mfa_secret:
        return True
    # Org requires MFA but user has not enrolled — still issue a limited session;
    # step-up blocks sensitive admin until enrolled.
    return False


def require_step_up(claims: dict, org: Organization, user: User) -> None:
    """Raise if org requires MFA and this access token is not step-up graded."""
    if not org.mfa_required:
        return
    if claims.get("mfa") is True or claims.get("amr") == "mfa":
        return
    if not user.mfa_enabled:
        raise ForbiddenError(
            "Multi-factor authentication is required for this workspace. "
            "Enrol an authenticator before continuing."
        )
    raise ForbiddenError(
        "Confirm your authenticator to continue with this admin action."
    )


# ── WebAuthn (py_webauthn) ─────────────────────────────────────────────────

def _rp_id() -> str:
    # Prefer configured public origin host; fall back to localhost for tests.
    origin = (settings.cors_origins or ["http://localhost:5173"])[0]
    from urllib.parse import urlsplit

    host = urlsplit(origin).hostname or "localhost"
    return host


def begin_webauthn_registration(
    db: Session, org: Organization, cipher: OrgCipher, user: User
) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import AuthenticatorSelectionCriteria, ResidentKeyRequirement

    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name="Synaprank",
        user_id=user.id.encode(),
        user_name=user.email or user.id,
        user_display_name=user.name or user.email or user.id,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED
        ),
    )
    # Stash challenge on a short-lived pending row keyed by user.
    raw = f"mfa_{crypto.new_url_token(24)}"
    row = MfaPendingChallenge(
        tenant_id=org.id,
        user_id=user.id,
        token_hash=crypto.hash_token(raw),
        expires_at=utcnow() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        webauthn_challenge_encrypted=cipher.encrypt(
            options.challenge.hex() if isinstance(options.challenge, bytes) else str(options.challenge),
            context=Ctx.MFA_CHALLENGE,
        ),
    )
    # Prefer storing JSON options challenge as hex from bytes.
    from webauthn.helpers import bytes_to_base64url

    row.webauthn_challenge_encrypted = cipher.encrypt(
        bytes_to_base64url(options.challenge), context=Ctx.MFA_CHALLENGE
    )
    db.add(row)
    db.flush()
    return {"options": options_to_json(options), "ceremony_token": raw}


def finish_webauthn_registration(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    user: User,
    *,
    ceremony_token: str,
    credential: dict,
    nickname: str = "",
    actor_email: str,
) -> WebAuthnCredential:
    from webauthn import verify_registration_response
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    from webauthn.helpers.exceptions import InvalidRegistrationResponse

    challenge = _load_challenge(db, ceremony_token)
    if challenge.user_id != user.id:
        raise AuthError("MFA challenge expired or invalid")
    expected = cipher.decrypt(challenge.webauthn_challenge_encrypted, context=Ctx.MFA_CHALLENGE)
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origins(),
        )
    except InvalidRegistrationResponse as exc:
        raise InvalidInputError("Passkey registration failed") from exc
    cred_id = bytes_to_base64url(verification.credential_id)
    row = WebAuthnCredential(
        tenant_id=org.id,
        user_id=user.id,
        credential_id=cred_id,
        public_key_encrypted=cipher.encrypt(
            bytes_to_base64url(verification.credential_public_key),
            context=Ctx.WEBAUTHN_PUBKEY,
        ),
        sign_count=int(verification.sign_count or 0),
        nickname=(nickname or "Passkey")[:80],
        backed_up=bool(getattr(verification, "credential_device_type", None)),
    )
    db.add(row)
    user.mfa_enabled = True
    challenge.consumed_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="mfa.webauthn.enroll",
        module="admin",
        context={"user_fp": crypto.fingerprint(user.id)},
    )
    return row


def _expected_origins() -> list[str] | str:
    origins = [o for o in (settings.cors_origins or []) if o]
    if not origins:
        return "http://localhost:5173"
    return origins if len(origins) > 1 else origins[0]


def begin_webauthn_authentication(
    db: Session, org: Organization, cipher: OrgCipher, *, mfa_token: str
) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor

    challenge = _load_challenge(db, mfa_token)
    creds = list(
        db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.tenant_id == org.id,
                WebAuthnCredential.user_id == challenge.user_id,
            )
        ).scalars()
    )
    if not creds:
        raise InvalidInputError("No passkeys enrolled")
    options = generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in creds
        ],
    )
    from webauthn.helpers import bytes_to_base64url

    challenge.webauthn_challenge_encrypted = cipher.encrypt(
        bytes_to_base64url(options.challenge), context=Ctx.MFA_CHALLENGE
    )
    db.flush()
    return {"options": options_to_json(options)}


def finish_webauthn_authentication(
    db: Session,
    cipher: OrgCipher,
    *,
    mfa_token: str,
    credential: dict,
) -> tuple[User, Organization]:
    from webauthn import verify_authentication_response
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    challenge = _load_challenge(db, mfa_token)
    user = db.get(User, challenge.user_id)
    org = db.get(Organization, challenge.tenant_id)
    if user is None or org is None:
        raise AuthError("MFA challenge expired or invalid")
    _rate_limit_verify(user.id)
    cred_id = credential.get("id") or ""
    stored = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.tenant_id == org.id,
            WebAuthnCredential.user_id == user.id,
            WebAuthnCredential.credential_id == cred_id,
        )
    ).scalar_one_or_none()
    if stored is None:
        _note_verify_failure(user.id)
        raise AuthError("Passkey not recognised")
    expected = cipher.decrypt(challenge.webauthn_challenge_encrypted, context=Ctx.MFA_CHALLENGE)
    public_key = base64url_to_bytes(
        cipher.decrypt(stored.public_key_encrypted, context=Ctx.WEBAUTHN_PUBKEY)
    )
    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origins(),
            credential_public_key=public_key,
            credential_current_sign_count=stored.sign_count,
        )
    except InvalidAuthenticationResponse as exc:
        _note_verify_failure(user.id)
        raise AuthError("Passkey verification failed") from exc
    if verification.new_sign_count <= stored.sign_count and stored.sign_count != 0:
        _note_verify_failure(user.id)
        raise AuthError("Passkey verification failed")
    stored.sign_count = int(verification.new_sign_count)
    stored.last_used_at = utcnow()
    challenge.consumed_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action="mfa.webauthn.verify",
        module="auth",
        context={"event": "mfa_ok"},
    )
    return user, org
