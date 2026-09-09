"""Authentication and account lifecycle for a public, multi-tenant product.

**There are no passwords.** Identity here is control of a mailbox: the server
mails a one-time code and the caller sends it back. That is the entire
credential, for signing up and for signing in.

Why it is built this way, since it is the platform's central security choice:

* Nothing is stored that a breach could turn into a credential — no hashes to
  crack offline, and a stolen ``login_codes`` row holds an HMAC of a number
  that expired minutes after it was issued.
* Nothing can be reused. Most account takeovers are a password from another
  site's breach, and there is no password here to reuse.
* There is no reset flow, which is usually the weakest path into an account,
  and no "forgot" mail to phish — the sign-in mail *is* the reset.
* Every sign-in is visible to the account holder, in their inbox, as it
  happens.

The trade is explicit and worth stating: the mailbox becomes the single point
of compromise, and mail delivery becomes a hard dependency of logging in.
So the guardrails refuse to start production without SMTP, and the code
itself is treated as a live credential — minutes to live, single use, capped
attempts, superseded on reissue, HMAC at rest. Those rules live in
:func:`issue_code` and :func:`consume_code` below and in
:class:`app.models.identity.LoginCode`.

The rest of the lifecycle:

* **Register** — prove the address with a code, then an organisation is
  created and its creator becomes Super Admin, with a data-encryption key
  provisioned and the workspace seeded with agents and connectors. No code,
  no organisation: an unproven address can never own a workspace.
* **Sign in** — a code, then rotating refresh tokens, with lockout after
  repeated wrong codes.
* **Invite** teammates with a role; the invite link proves their address, so
  accepting needs no code of its own.

Email addresses are only ever matched through their blind index — the
plaintext is encrypted at rest and never appears in a WHERE clause.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import (
    email_index,
    hash_token,
    new_url_token,
    normalize_email,
)
from app.core.exceptions import (
    AuthError,
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
)
from app.core.logging import get_logger
from app.core.rbac import ROLE_LABELS, Role
from app.core.security import (
    codes_match,
    create_access_token,
    new_login_code,
    normalize_code,
)
from app.db.base import new_id, utcnow
from app.db.session import bind_tenant
from app.models.identity import AuthIdentity, CodePurpose, LoginCode
from app.models.workspace import (
    Invitation,
    Organization,
    RefreshToken,
    User,
)
from app.services import audit, email as email_service
from app.services.encryption import OrgCipher

log = get_logger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "live.com", "aol.com", "icloud.com", "proton.me", "protonmail.com",
        "mail.com", "gmx.com", "yandex.com", "zoho.com",
    }
)


# ── Results ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"


@dataclass(slots=True)
class AuthResult:
    user: User
    organization: Organization
    tokens: TokenPair


@dataclass(slots=True)
class CodeChallenge:
    """What the caller is told after asking for a code.

    Deliberately thin. It never carries the code, never says whether the
    address is registered, and looks identical whether a code was just mailed
    or suppressed because one is still live — otherwise the response itself
    becomes an oracle for which addresses have accounts.
    """

    # Lifetime of a code, in seconds. A constant, so it discloses nothing.
    expires_in: int
    # Seconds until another code may be requested for this address.
    resend_in: int


# ── Helpers ────────────────────────────────────────────────────────────────
def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug[:64] or "workspace"


def unique_slug(db: Session, desired: str) -> str:
    """Append a counter until the handle is free across the platform."""
    base = slugify(desired)
    candidate = base
    n = 1
    while db.execute(
        select(func.count()).select_from(Organization).where(Organization.slug == candidate)
    ).scalar_one():
        n += 1
        candidate = f"{base}-{n}"[:64]
    return candidate


def find_identity(db: Session, address: str) -> AuthIdentity | None:
    """Resolve an address to its organisation, before any session exists.

    Matched on the blind index; the plaintext address never appears in a
    WHERE clause. See ``app.models.identity`` for why this lookup has to sit
    outside row-level security.
    """
    return db.get(AuthIdentity, email_index(address))


def find_user_by_email(db: Session, address: str) -> User | None:
    """Load the user behind an address, pinning their organisation first.

    The pin is what puts every subsequent query in this transaction inside
    that organisation's row-level-security boundary.
    """
    identity = find_identity(db, address)
    if identity is None:
        return None
    bind_tenant(db, identity.tenant_id)
    return db.get(User, identity.user_id)


def register_identity(db: Session, user: User) -> AuthIdentity:
    """Add a user to the global directory. Called once per account created."""
    identity = AuthIdentity(
        email_index=user.email_index,
        user_id=user.id,
        tenant_id=user.tenant_id,
        is_active=user.is_active,
        sso_provider=user.sso_provider,
    )
    db.add(identity)
    db.flush()
    return identity


def get_organization(db: Session, tenant_id: str) -> Organization:
    org = db.get(Organization, tenant_id)
    if org is None or not org.is_active:
        raise NotFoundError("Workspace not found")
    return org


def _reject_freemail(address: str) -> None:
    if not settings.block_public_email_domains:
        return
    domain = normalize_email(address).split("@")[-1]
    if domain in _FREEMAIL_DOMAINS:
        raise InvalidInputError(
            "Use your work email address to create an organisation"
        )


def _seats_used(db: Session, tenant_id: str) -> int:
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant_id, User.is_active.is_(True))
    ).scalar_one()


def assert_seat_available(db: Session, org: Organization) -> None:
    if _seats_used(db, org.id) >= org.seats_total:
        raise ConflictError(
            f"All {org.seats_total} seats on the {org.plan_name} are in use — "
            "free a seat or upgrade the plan"
        )


# ── Token issuing ──────────────────────────────────────────────────────────
def _issue_tokens(
    db: Session,
    user: User,
    *,
    family_id: str | None = None,
    user_agent: str = "",
    ip_address: str | None = None,
) -> TokenPair:
    """Mint an access token and persist a fresh single-use refresh token."""
    session_id = new_id()
    raw_refresh = new_url_token(32)
    record = RefreshToken(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        family_id=family_id or session_id,
        expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        user_agent=user_agent[:255],
        ip_address=ip_address,
    )
    db.add(record)
    db.flush()

    access, expires_in = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        session_id=record.id,
    )
    return TokenPair(access_token=access, refresh_token=raw_refresh, expires_in=expires_in)


def _revoke_family(db: Session, family_id: str, reason: str) -> int:
    """Revoke every live token in one lineage. Used on logout and on reuse."""
    tokens = db.execute(
        select(RefreshToken).where(
            RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
        )
    ).scalars()
    count = 0
    for token in tokens:
        token.revoked_at = utcnow()
        token.revoked_reason = reason
        count += 1
    return count


def revoke_all_sessions(db: Session, user: User, reason: str) -> int:
    tokens = db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ).scalars()
    count = 0
    for token in tokens:
        token.revoked_at = utcnow()
        token.revoked_reason = reason
        count += 1
    return count



# ── One-time codes ─────────────────────────────────────────────────────────
# This is the credential system. Everything that makes a six-digit number safe
# to log in with lives in these functions, so the rules are in one place and
# cannot be half-applied by a caller.
def _code_ttl() -> timedelta:
    return timedelta(minutes=settings.login_code_ttl_minutes)


def _last_request(db: Session, index: str, purpose: CodePurpose) -> LoginCode | None:
    """The most recent code row for this address and purpose, in any state.

    "In any state" matters: the resend throttle has to count codes that were
    already spent or burned as well as live ones, or it could be walked by
    consuming a code and immediately asking for another.
    """
    return db.execute(
        select(LoginCode)
        .where(LoginCode.email_index == index, LoginCode.purpose == purpose.value)
        .order_by(LoginCode.created_at.desc())
        .limit(1)
    ).scalars().first()


def issue_code(
    db: Session,
    *,
    address: str,
    purpose: CodePurpose,
    name: str = "",
    deliver: bool = True,
    ip_address: str | None = None,
    user_agent: str = "",
) -> CodeChallenge:
    """Mail a fresh code, unless one was just sent.

    ``deliver=False`` writes the row and sends nothing. That is not a quirk:
    it is how a request for an unregistered address is handled. The row keeps
    the throttle arithmetic identical for addresses that exist and addresses
    that do not, so a caller reading the ``resend_in`` they get back cannot use
    it to enumerate accounts. Such a row is born burned and can never be
    redeemed.
    """
    index = email_index(address)
    now = utcnow()
    previous = _last_request(db, index, purpose)

    if previous is not None:
        elapsed = (now - previous.created_at).total_seconds()
        remaining = int(settings.login_code_resend_seconds - elapsed)
        if remaining > 0:
            # Silently decline to send another. An error here would both annoy
            # a user who simply double-clicked and disclose that the previous
            # request reached a real account.
            log.info("Suppressed a duplicate code request (%ds remaining)", remaining)
            return CodeChallenge(
                expires_in=int(_code_ttl().total_seconds()), resend_in=remaining
            )
        # A new code retires the old one, so only one code per address and
        # purpose is ever live. Without this, every unused code would widen the
        # space an attacker may guess against.
        if previous.is_live:
            previous.burned_at = now

    code = new_login_code()
    record = LoginCode(
        email_index=index,
        purpose=purpose.value,
        code_hash=hash_token(code),
        expires_at=now + _code_ttl(),
        burned_at=None if deliver else now,
        ip_address=ip_address,
        user_agent=user_agent[:255],
    )
    db.add(record)
    db.flush()

    if deliver:
        minutes = settings.login_code_ttl_minutes
        if purpose is CodePurpose.SIGN_IN:
            email_service.send_login_code(to=address, name=name, code=code, minutes=minutes)
        else:
            email_service.send_signup_code(to=address, code=code, minutes=minutes)

    return CodeChallenge(
        expires_in=int(_code_ttl().total_seconds()),
        resend_in=settings.login_code_resend_seconds,
    )


def consume_code(db: Session, *, address: str, purpose: CodePurpose, submitted: str) -> None:
    """Redeem a code, or raise :class:`AuthError` saying why not.

    Only the newest code for the address and purpose is considered — the same
    rule as issuing — and it is spent on success, burned once the attempt cap
    is reached.

    The caller **must commit** after this raises. The attempt counter is the
    only thing standing between a six-digit code and an exhaustive search, and
    a counter rolled back with the failed request does not count. Every call
    site therefore commits before re-raising; see :func:`authenticate`.
    """
    code = normalize_code(submitted)
    record = _last_request(db, email_index(address), purpose)
    stale = AuthError("That code is no longer valid — ask for a new one")

    if record is None or not record.is_live or record.expires_at <= utcnow():
        raise stale

    record.attempts += 1
    if not codes_match(hash_token(code), record.code_hash):
        remaining = settings.login_code_max_attempts - record.attempts
        if remaining <= 0:
            record.burned_at = utcnow()
            db.flush()
            raise AuthError("Too many incorrect codes — ask for a new one")
        db.flush()
        raise AuthError(
            f"That code is not correct — {remaining} "
            f"{'attempt' if remaining == 1 else 'attempts'} left"
        )

    record.consumed_at = utcnow()
    db.flush()


def _note_failed_attempt(db: Session, user: User | None) -> None:
    """Count a rejected code against the account, locking it after enough.

    A per-code attempt cap alone is not sufficient: an attacker can request a
    new code — which they cannot read — and guess a few times against each,
    indefinitely. The account-level counter is what makes that terminate.
    """
    if user is None:
        return
    user.failed_login_count += 1
    if user.failed_login_count >= settings.max_failed_logins:
        user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_login_count = 0
        log.warning("Locked account %s after repeated incorrect codes", user.id)


def _assert_not_locked(user: User) -> None:
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60))
        raise AuthError(f"Too many failed attempts — try again in {minutes} minutes")


def request_sign_in_code(
    db: Session, *, email: str, ip_address: str | None = None, user_agent: str = ""
) -> CodeChallenge:
    """Start a sign-in. Reveals nothing about whether the address is known.

    An unknown or deactivated address takes the same path as a real one, minus
    the mail — see ``deliver`` in :func:`issue_code`.
    """
    address = normalize_email(email)
    if "@" not in address:
        raise InvalidInputError("Enter a valid email address")

    user = find_user_by_email(db, address)
    known = user is not None and user.is_active
    if not known:
        # Outside production, name the address. The endpoint deliberately
        # tells the *caller* nothing, which makes a typo indistinguishable
        # from a real account and impossible to debug from the console alone.
        # In production this line would be a record of who is signing in, so
        # it stays anonymous there.
        if settings.is_production:
            log.info("Sign-in code requested for an unknown or inactive address")
        else:
            log.info(
                "Sign-in code requested for %s — no active account, so nothing was sent",
                address,
            )
    return issue_code(
        db,
        address=address,
        purpose=CodePurpose.SIGN_IN,
        name=user.name if known and user is not None else "",
        deliver=known,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def request_sign_up_code(
    db: Session, *, email: str, ip_address: str | None = None, user_agent: str = ""
) -> CodeChallenge:
    """Start a sign-up by proving the address.

    Unlike sign-in, this says plainly when an address is already registered:
    somebody trying to create a workspace needs to know to sign in instead,
    and the sign-up endpoint itself already makes the same disclosure. The
    rate limiter is what keeps it from being a bulk enumeration tool.
    """
    if not settings.allow_public_signup:
        raise ForbiddenError("Sign-up is closed on this deployment")

    address = normalize_email(email)
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise InvalidInputError("Enter a valid email address")
    _reject_freemail(address)

    if find_identity(db, address) is not None:
        raise ConflictError("An account already exists for that email — sign in instead")

    return issue_code(
        db,
        address=address,
        purpose=CodePurpose.SIGN_UP,
        deliver=True,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# ── Registration ───────────────────────────────────────────────────────────
def register_organization(
    db: Session,
    *,
    organization_name: str,
    full_name: str,
    email: str,
    code: str,
    primary_domain: str = "",
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Create an organisation and its first (owner) user.

    The code is the second half of a two-step sign-up: :func:`request_sign_up_code`
    mailed it to this address, and presenting it back is what proves the
    address belongs to whoever is signing up. Nothing is created until it
    checks out, so an unproven address can never end up owning a workspace —
    which is also why the account is created already confirmed.

    The organisation's data key is generated here and stored only in wrapped
    form.
    """
    if not settings.allow_public_signup:
        raise ForbiddenError("Sign-up is closed on this deployment")

    address = normalize_email(email)
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise InvalidInputError("Enter a valid email address")
    _reject_freemail(address)

    if find_identity(db, address) is not None:
        # Deliberately explicit: a would-be signer-up needs to know to log in
        # instead. Enumeration is a real trade-off, and it is mitigated by the
        # rate limiter on this endpoint.
        raise ConflictError("An account already exists for that email — sign in instead")

    try:
        consume_code(db, address=address, purpose=CodePurpose.SIGN_UP, submitted=code)
    except AuthError:
        # The attempt this just recorded has to outlive the failed request —
        # see the note on consume_code.
        db.commit()
        raise

    org_id = new_id()
    org = Organization(
        id=org_id,
        name=organization_name.strip() or "My workspace",
        slug=unique_slug(db, organization_name),
        primary_domain=primary_domain.strip().lower(),
        wrapped_dek=OrgCipher.provision(org_id),
        dek_version=1,
        global_autonomy=True,
        plan_name=settings.default_plan_name,
        seats_total=settings.default_seats,
        renews_on=(utcnow() + timedelta(days=settings.default_plan_days)).date(),
    )
    db.add(org)
    db.flush()
    # Pin the new organisation before inserting anything scoped to it —
    # row-level security rejects a write that no session claims.
    bind_tenant(db, org.id)

    user = User(
        tenant_id=org.id,
        name=full_name.strip(),
        email=address,
        email_index=email_index(address),
        role=Role.ADMIN.value,
        is_owner=True,
        # The code proved the address a moment ago. There is nothing left to
        # confirm, and no state in which a workspace exists behind an
        # unverified address.
        email_verified=True,
        email_verified_at=utcnow(),
    )
    db.add(user)

    try:
        db.flush()
        register_identity(db, user)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("An account already exists for that email") from exc

    # Give the workspace its agents, connectors, budgets and onboarding row.
    from app.services.provisioning import provision_organization

    provision_organization(db, org)

    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action=f"created the workspace {org.name}",
        module="admin",
        ip_address=ip_address,
    )

    tokens = _issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)
    log.info("Registered organisation %s (%s)", org.name, org.slug)
    return AuthResult(user=user, organization=org, tokens=tokens)


# ── Login ──────────────────────────────────────────────────────────────────
def authenticate(
    db: Session,
    *,
    email: str,
    code: str,
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Finish a sign-in by redeeming the code that was mailed out.

    The pairing of address and code is the whole check. Note the ordering: the
    lockout is tested before the code is, so an account under attack stays
    shut even if the attacker happens to guess right, and the code is consumed
    before any session is minted.
    """
    address = normalize_email(email)
    user = find_user_by_email(db, address)

    if user is None:
        # An unknown address has no code row either, so this is the same
        # message consume_code would have produced for a stale code — the
        # response does not distinguish the two cases.
        raise AuthError("That code is no longer valid — ask for a new one")
    if not user.is_active:
        raise AuthError("This account has been deactivated")
    _assert_not_locked(user)

    try:
        consume_code(db, address=address, purpose=CodePurpose.SIGN_IN, submitted=code)
    except AuthError:
        _note_failed_attempt(db, user)
        # Committed before re-raising: the caller is about to roll this
        # request back, and a rolled-back attempt counter would leave the code
        # space effectively unlimited. See consume_code.
        db.commit()
        raise

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_login_ip = ip_address

    org = get_organization(db, user.tenant_id)
    tokens = _issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)
    db.flush()
    return AuthResult(user=user, organization=org, tokens=tokens)


def refresh_session(
    db: Session, *, refresh_token: str, user_agent: str = "", ip_address: str | None = None
) -> AuthResult:
    """Rotate a refresh token, detecting reuse.

    Tokens are single-use. Presenting one that was already redeemed means a
    copy is circulating, so the entire lineage is revoked and the holder has to
    sign in again.
    """
    presented = hash_token(refresh_token)
    record = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == presented)
    ).scalar_one_or_none()

    if record is None:
        raise AuthError("Invalid session — sign in again")

    if record.revoked_at is not None or record.replaced_by is not None:
        revoked = _revoke_family(db, record.family_id, "token_reuse_detected")
        # Committed before raising: the request is about to fail, and the
        # caller's rollback would otherwise undo the revocation — leaving the
        # stolen token's replacement still valid, which is the exact outcome
        # reuse detection exists to prevent.
        db.commit()
        log.warning(
            "Refresh token reuse on family %s — revoked %d tokens", record.family_id, revoked
        )
        raise AuthError("This session was already used — sign in again")

    if record.expires_at <= utcnow():
        record.revoked_at = utcnow()
        record.revoked_reason = "expired"
        db.commit()
        raise AuthError("Session expired — sign in again")

    bind_tenant(db, record.tenant_id)
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthError("This account is no longer active")

    tokens = _issue_tokens(
        db, user, family_id=record.family_id, user_agent=user_agent, ip_address=ip_address
    )
    record.revoked_at = utcnow()
    record.revoked_reason = "rotated"
    # The newest token in the family is the one just issued.
    newest = db.execute(
        select(RefreshToken)
        .where(RefreshToken.family_id == record.family_id, RefreshToken.revoked_at.is_(None))
        .order_by(RefreshToken.created_at.desc())
    ).scalars().first()
    if newest is not None:
        record.replaced_by = newest.id
    db.flush()

    org = get_organization(db, user.tenant_id)
    return AuthResult(user=user, organization=org, tokens=tokens)


def logout(db: Session, *, refresh_token: str) -> None:
    record = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
    ).scalar_one_or_none()
    if record is not None:
        _revoke_family(db, record.family_id, "logout")
        db.flush()


# ── Invitations ────────────────────────────────────────────────────────────
def invite_member(
    db: Session,
    *,
    org: Organization,
    inviter: User,
    email: str,
    role: Role | str,
) -> Invitation:
    address = normalize_email(email)
    role_value = Role(role).value

    if find_identity(db, address) is not None:
        raise ConflictError("That person already has an account")
    assert_seat_available(db, org)

    index = email_index(address)
    existing = db.execute(
        select(Invitation).where(
            Invitation.tenant_id == org.id,
            Invitation.email_index == index,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-inviting refreshes the link rather than stacking invitations.
        existing.revoked_at = utcnow()

    raw = new_url_token(32)
    invitation = Invitation(
        tenant_id=org.id,
        email=address,
        email_index=index,
        role=role_value,
        token_hash=hash_token(raw),
        invited_by=inviter.id,
        expires_at=utcnow() + timedelta(days=settings.invitation_ttl_days),
    )
    db.add(invitation)
    db.flush()

    email_service.send_invitation(
        to=address,
        org_name=org.name,
        inviter=inviter.name,
        role_label=ROLE_LABELS[Role(role_value)],
        token=raw,
    )
    audit.record_user_action(
        db,
        user=inviter,
        action=f"invited a {ROLE_LABELS[Role(role_value)]} to the workspace",
        module="admin",
        context={"role": role_value},
    )
    return invitation


def peek_invitation(db: Session, *, token: str) -> tuple[Invitation, Organization]:
    """Resolve an invite token so the accept screen can show who invited whom."""
    invitation = db.execute(
        select(Invitation).where(Invitation.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if invitation is None or invitation.revoked_at is not None:
        raise NotFoundError("This invitation is no longer valid")
    if invitation.accepted_at is not None:
        raise ConflictError("This invitation has already been used — sign in instead")
    if invitation.expires_at <= utcnow():
        raise AuthError("This invitation has expired — ask for a new one")
    bind_tenant(db, invitation.tenant_id)
    return invitation, get_organization(db, invitation.tenant_id)


def accept_invitation(
    db: Session,
    *,
    token: str,
    full_name: str,
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Join an existing organisation.

    No code is asked for here, and no password is set: the invite token came
    out of the invitee's own mailbox, which proves exactly what a mailed code
    proves. Making them wait for a second email to say the same thing would be
    ceremony, not security. From here on they sign in with a code like
    everybody else.
    """
    invitation, org = peek_invitation(db, token=token)
    assert_seat_available(db, org)

    user = User(
        tenant_id=org.id,
        name=full_name.strip(),
        email=invitation.email,
        email_index=invitation.email_index,
        role=invitation.role,
        # The address is proven by the fact the invite arrived there.
        email_verified=True,
        email_verified_at=utcnow(),
    )
    db.add(user)
    try:
        db.flush()
        register_identity(db, user)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("An account already exists for that email") from exc

    invitation.accepted_at = utcnow()
    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action=f"joined the workspace as {ROLE_LABELS[Role(user.role)]}",
        module="admin",
        ip_address=ip_address,
    )
    tokens = _issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)
    db.flush()
    return AuthResult(user=user, organization=org, tokens=tokens)


def revoke_invitation(db: Session, *, org: Organization, invitation_id: str, actor: User) -> None:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.tenant_id != org.id:
        raise NotFoundError("Invitation not found")
    invitation.revoked_at = utcnow()
    db.flush()
    audit.record_user_action(db, user=actor, action="revoked an invitation", module="admin")


# ── Member management ──────────────────────────────────────────────────────
def change_member_role(
    db: Session, *, org: Organization, actor: User, member_id: str, role: Role | str
) -> User:
    member = db.get(User, member_id)
    if member is None or member.tenant_id != org.id:
        raise NotFoundError("Team member not found")

    role_value = Role(role).value
    if member.is_owner and role_value != Role.ADMIN.value:
        raise ForbiddenError("The workspace owner must remain a Super Admin")
    if member.id == actor.id and role_value != Role.ADMIN.value:
        # Prevents an admin locking themselves — and possibly everyone — out.
        raise ForbiddenError("You cannot remove your own admin access")

    previous = member.role
    member.role = role_value
    # A role change alters what every live token is allowed to do, so the
    # member's sessions are ended and re-minted on their next request.
    revoke_all_sessions(db, member, "role_changed")
    db.flush()

    audit.record_user_action(
        db,
        user=actor,
        action=f"changed {member.name} to {ROLE_LABELS[Role(role_value)]}",
        module="admin",
        context={"member_id": member.id, "from": previous, "to": role_value},
    )
    return member


def deactivate_member(db: Session, *, org: Organization, actor: User, member_id: str) -> User:
    member = db.get(User, member_id)
    if member is None or member.tenant_id != org.id:
        raise NotFoundError("Team member not found")
    if member.is_owner:
        raise ForbiddenError("The workspace owner cannot be removed")
    if member.id == actor.id:
        raise ForbiddenError("You cannot deactivate your own account")

    member.is_active = False
    # Mirror it into the directory so the account is rejected at login,
    # before any organisation is pinned.
    identity = db.get(AuthIdentity, member.email_index)
    if identity is not None:
        identity.is_active = False
    revoke_all_sessions(db, member, "deactivated")
    db.flush()
    audit.record_user_action(
        db, user=actor, action=f"deactivated {member.name}", module="admin"
    )
    return member


# ── Invitations ────────────────────────────────────────────────────────────
def outstanding_invitation_clauses() -> tuple:
    """The one definition of "an invitation still waiting to be accepted".

    Exists because there were two. The Admin screen listed invitations that
    are neither accepted nor revoked; the navigation badge counted those with
    no ``accepted_at``, and so counted a revoked one. The badge said 1 and
    the screen showed none, which reads as the screen being broken.

    A predicate rather than a query, so a caller can select rows or count
    them without the rule being written down twice.

    An **expired** invitation is deliberately still outstanding. It is still
    there, nobody has accepted it, and the thing to do about it — resend or
    revoke — is exactly what the Admin screen offers. Hiding it would make it
    unfixable.
    """
    from app.models.workspace import Invitation

    return (
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
    )
