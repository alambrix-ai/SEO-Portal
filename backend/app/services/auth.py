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
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core import client_ip
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
from app.core.logging import get_logger, log_failure
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
from app.models.membership import WorkspaceMembership
from app.models.workspace import (
    Invitation,
    Organization,
    RefreshToken,
    User,
)
from app.services import audit, email as email_service
from app.services.encryption import OrgCipher

log = get_logger(__name__)

#: Audit ``module`` for sign-in security events. Not an RBAC module on
#: purpose: these rows belong in the Admin audit log, not in every admin's
#: activity feed and notification badge (both filter to RBAC modules).
AUTH_AUDIT_MODULE = "auth"

#: Hands a mail send to the caller to run later, e.g. FastAPI's
#: ``BackgroundTasks.add_task``. Called as ``schedule(func, **kwargs)``.
MailScheduler = Callable[..., None]

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
    tokens: TokenPair | None = None
    #: When set, email OTP succeeded but MFA is still required before tokens.
    mfa_token: str | None = None
    mfa_methods: tuple[str, ...] = ()


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


#: The one answer to a code that did not get anyone in, whatever the reason:
#: wrong, expired, superseded, already used, burned after too many tries, an
#: unknown or closed address, or a sign-in pause. Separate messages told a
#: caller how many guesses were left and whether an address was locked, which
#: is exactly what someone guessing at an account wants to know.
CODE_REJECTED_MESSAGE = (
    "That code didn't work. Check you typed the newest code we emailed you, "
    "or ask for a new one."
)


class CodeRejected(AuthError):
    """A sign-in or sign-up code was refused. Always :data:`CODE_REJECTED_MESSAGE`.

    ``counted`` is True only when the refusal used up an attempt on a live,
    issued code. Only those count towards the account's sign-in pause: a
    stale or superseded code costs an attacker nothing to send, so counting
    it would let anyone pause anyone's account.
    """

    def __init__(self, *, counted: bool = False) -> None:
        super().__init__(CODE_REJECTED_MESSAGE)
        self.counted = counted


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
    """Resolve an address to its login directory row, before any session exists.

    Matched on the blind index; the plaintext address never appears in a
    WHERE clause. See ``app.models.identity`` for why this lookup has to sit
    outside row-level security.
    """
    return db.get(AuthIdentity, email_index(address))


def list_memberships(db: Session, *, email_index_value: str) -> list[WorkspaceMembership]:
    """Every workspace seat for an address, newest first."""
    return list(
        db.execute(
            select(WorkspaceMembership)
            .where(WorkspaceMembership.email_index == email_index_value)
            .order_by(WorkspaceMembership.created_at.desc())
        ).scalars()
    )


def find_membership(
    db: Session, *, email_index_value: str, tenant_id: str
) -> WorkspaceMembership | None:
    return db.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.email_index == email_index_value,
            WorkspaceMembership.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()


def _membership_org_alive(db: Session, membership: WorkspaceMembership) -> bool:
    """True when the seat and its organisation are both still available."""
    if not membership.is_active:
        return False
    org = db.get(Organization, membership.tenant_id)
    return org is not None and org.is_active


def find_user_by_email(db: Session, address: str) -> User | None:
    """Load the last-active user behind an address, pinning their organisation.

    The pin is what puts every subsequent query in this transaction inside
    that organisation's row-level-security boundary. Soft-deleted workspaces
    (``Organization.is_active = False``) are skipped so login lands on a
    live seat.
    """
    identity = find_identity(db, address)
    if identity is None:
        return None
    # Prefer the last-active membership when it is still live; otherwise the
    # newest active seat for this address.
    membership = find_membership(
        db, email_index_value=identity.email_index, tenant_id=identity.tenant_id
    )
    if membership is None or not _membership_org_alive(db, membership):
        memberships = [
            m
            for m in list_memberships(db, email_index_value=identity.email_index)
            if _membership_org_alive(db, m)
        ]
        if not memberships:
            return None
        membership = memberships[0]
        identity.user_id = membership.user_id
        identity.tenant_id = membership.tenant_id
    bind_tenant(db, membership.tenant_id)
    user = db.get(User, membership.user_id)
    if user is None or not user.is_active:
        return None
    return user


def set_last_active(db: Session, user: User) -> AuthIdentity:
    """Point the login directory at this workspace as the default after OTP."""
    identity = db.get(AuthIdentity, user.email_index)
    if identity is None:
        identity = AuthIdentity(
            email_index=user.email_index,
            user_id=user.id,
            tenant_id=user.tenant_id,
            is_active=True,
            sso_provider=user.sso_provider,
        )
        db.add(identity)
    else:
        identity.user_id = user.id
        identity.tenant_id = user.tenant_id
        identity.is_active = True
        if user.sso_provider:
            identity.sso_provider = user.sso_provider
    db.flush()
    return identity


def add_membership(db: Session, user: User) -> WorkspaceMembership:
    """Record a seat for this user in their organisation."""
    existing = find_membership(
        db, email_index_value=user.email_index, tenant_id=user.tenant_id
    )
    if existing is not None:
        existing.user_id = user.id
        existing.is_active = user.is_active
        db.flush()
        return existing
    membership = WorkspaceMembership(
        email_index=user.email_index,
        tenant_id=user.tenant_id,
        user_id=user.id,
        is_active=user.is_active,
    )
    db.add(membership)
    db.flush()
    return membership


def register_identity(db: Session, user: User) -> AuthIdentity:
    """Add or refresh the global directory entry and membership for a user."""
    identity = set_last_active(db, user)
    add_membership(db, user)
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
    mfa_verified: bool = False,
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

    extra: dict = {}
    if mfa_verified:
        extra["mfa"] = True
        extra["amr"] = "mfa"
    access, expires_in = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        session_id=record.id,
        extra=extra or None,
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


def revoke_sessions_everywhere(db: Session, user: User, reason: str) -> int:
    """Revoke every live session this person has, in every workspace.

    One mailbox can hold seats in several workspaces, each its own ``users``
    row. "Sign out everywhere" means the person, not the seat, so every seat
    behind the address is covered — including deactivated ones, which should
    have nothing live but are swept anyway. ``refresh_tokens`` and
    ``workspace_memberships`` sit outside row-level security (see
    ``app.db.rls``), so no tenant needs pinning here.
    """
    seat_ids = {
        m.user_id for m in list_memberships(db, email_index_value=user.email_index)
    }
    seat_ids.add(user.id)
    tokens = db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id.in_(seat_ids), RefreshToken.revoked_at.is_(None)
        )
    ).scalars()
    now = utcnow()
    count = 0
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = reason
        count += 1
    db.flush()
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


def _deliver_code_quietly(
    send: Callable[..., bool], purpose: str, mail: dict[str, object]
) -> None:
    """Run a deferred code mail, after the response has gone.

    Nobody is left to tell by then, so a failure is logged (type and hash,
    never the address or the code) rather than raised into the server's
    background-task handler, which would print the raw message.
    """
    try:
        send(**mail)
    except Exception as exc:  # noqa: BLE001 - logged; the user can ask again
        log_failure(log, "Could not send a code email", exc, purpose=purpose)


def issue_code(
    db: Session,
    *,
    address: str,
    purpose: CodePurpose,
    name: str = "",
    deliver: bool = True,
    ip_address: str | None = None,
    user_agent: str = "",
    schedule_mail: MailScheduler | None = None,
) -> CodeChallenge:
    """Mail a fresh code, unless one was just sent.

    ``deliver=False`` writes the row and sends nothing. That is not a quirk:
    it is how a request for an unregistered address is handled. The row keeps
    the throttle arithmetic identical for addresses that exist and addresses
    that do not, so a caller reading the ``resend_in`` they get back cannot use
    it to enumerate accounts. Such a row is born burned and can never be
    redeemed.

    ``schedule_mail`` defers the send until after the response. Mail delivery
    takes seconds and is skipped for unknown addresses, so sending inline
    would let response *timing* reveal what the response body does not.
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
            send: Callable[..., bool] = email_service.send_login_code
            mail: dict[str, object] = {
                "to": address, "name": name, "code": code, "minutes": minutes
            }
        else:
            send = email_service.send_signup_code
            mail = {"to": address, "code": code, "minutes": minutes}
        if schedule_mail is None:
            send(**mail)
        else:
            schedule_mail(_deliver_code_quietly, send, purpose.value, mail)

    return CodeChallenge(
        expires_in=int(_code_ttl().total_seconds()),
        resend_in=settings.login_code_resend_seconds,
    )


def _claim_attempt(db: Session, record: LoginCode, *, cap: int, now: datetime) -> int | None:
    """Use up one attempt on a live code, atomically. Returns the new count.

    One ``UPDATE … SET attempts = attempts + 1 … RETURNING`` with the
    liveness and the cap in its WHERE clause. Reading the counter in Python
    and writing it back let parallel guesses each see the same count and all
    pass the cap; here PostgreSQL serialises them on the row, so at most
    ``cap`` attempts are ever granted. ``None`` means none was granted: the
    code was spent, burned, expired or capped, possibly a moment ago by a
    concurrent request.
    """
    attempts = db.execute(
        update(LoginCode)
        .where(
            LoginCode.id == record.id,
            LoginCode.consumed_at.is_(None),
            LoginCode.burned_at.is_(None),
            LoginCode.expires_at > now,
            LoginCode.attempts < cap,
        )
        .values(attempts=LoginCode.attempts + 1)
        .returning(LoginCode.attempts)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if attempts is None:
        return None
    set_committed_value(record, "attempts", int(attempts))
    return int(attempts)


def _close_code(db: Session, record: LoginCode, *, column: str, now: datetime) -> bool:
    """Set ``consumed_at`` or ``burned_at`` once, atomically. False if already closed."""
    target = getattr(LoginCode, column)
    closed = db.execute(
        update(LoginCode)
        .where(
            LoginCode.id == record.id,
            LoginCode.consumed_at.is_(None),
            LoginCode.burned_at.is_(None),
        )
        .values({target: now})
        .returning(LoginCode.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if closed is None:
        return False
    set_committed_value(record, column, now)
    return True


def consume_code(
    db: Session,
    *,
    address: str,
    purpose: CodePurpose,
    submitted: str,
    issued_after: datetime | None = None,
) -> None:
    """Redeem a code, or raise :class:`CodeRejected`.

    Only the newest code for the address and purpose is considered — the same
    rule as issuing — and it is spent on success, burned once the attempt cap
    is reached. Both the attempt and the spend are single atomic UPDATEs (see
    :func:`_claim_attempt`), so parallel requests cannot beat the cap or
    redeem one code twice.

    ``issued_after`` is the sign-in pause's exception: only a code mailed
    after that moment is considered, and it gets exactly one try.

    The caller **must commit** after this raises. The attempt counter is the
    only thing standing between a six-digit code and an exhaustive search, and
    a counter rolled back with the failed request does not count. Every call
    site therefore commits before re-raising; see :func:`authenticate`.
    """
    code = normalize_code(submitted)
    record = _last_request(db, email_index(address), purpose)
    now = utcnow()

    if record is None or not record.is_live or record.expires_at <= now:
        raise CodeRejected()
    if issued_after is not None and record.created_at < issued_after:
        raise CodeRejected()

    cap = 1 if issued_after is not None else settings.login_code_max_attempts
    attempts = _claim_attempt(db, record, cap=cap, now=now)
    if attempts is None:
        raise CodeRejected()

    if not codes_match(hash_token(code), record.code_hash):
        if attempts >= cap:
            _close_code(db, record, column="burned_at", now=now)
        raise CodeRejected(counted=True)

    if not _close_code(db, record, column="consumed_at", now=now):
        raise CodeRejected()


def _set_address_lock(
    db: Session, user: User, until: datetime | None, *, ip_address: str | None = None
) -> int:
    """Apply (``until`` set) or lift (``None``) the sign-in lock on every seat.

    The lock belongs to the address, not the seat. Sign-in resolves an
    address to one seat, and which one can change (switching workspace,
    a seat being deactivated); a per-seat lock would let guessing carry on
    against the next seat. Each seat sits in its own tenant under row-level
    security, so each is pinned before it is touched and flushed while
    pinned, and the caller's pin is restored afterwards. Returns the number
    of seats changed. A new lock is written to each workspace's audit log.

    ``user`` itself is always covered, even when no membership row points at
    it, so the seat that tripped the lock can never be the one left open.
    """
    changed = 0
    seats: list[tuple[str, str]] = [
        (m.tenant_id, m.user_id)
        for m in list_memberships(db, email_index_value=user.email_index)
    ]
    if not any(user_id == user.id for _, user_id in seats):
        seats.append((user.tenant_id, user.id))
    try:
        for tenant_id, user_id in seats:
            bind_tenant(db, tenant_id)
            seat = user if user_id == user.id else db.get(User, user_id)
            if seat is None:
                continue
            seat.locked_until = until
            seat.failed_login_count = 0
            if until is not None:
                audit.record(
                    db,
                    tenant_id=seat.tenant_id,
                    actor="System",
                    actor_type="system",
                    action=(
                        f"paused sign-in for {seat.name} for "
                        f"{settings.lockout_minutes} minutes after repeated incorrect codes"
                    ),
                    module=AUTH_AUDIT_MODULE,
                    ip_address=ip_address,
                    context={"member_id": seat.id, "event": "sign_in_locked"},
                    flush=False,
                )
            db.flush()
            changed += 1
    finally:
        bind_tenant(db, user.tenant_id)
    return changed


def _note_failed_attempt(
    db: Session, user: User | None, *, ip_address: str | None = None
) -> None:
    """Count a rejected code against the account, locking it after enough.

    A per-code attempt cap alone is not sufficient: an attacker can request a
    new code — which they cannot read — and guess a few times against each,
    indefinitely. The account-level counter is what makes that terminate, and
    the lock it sets covers every seat behind the address (see
    :func:`_set_address_lock`). Per-IP throttling is the auth rate limiter's
    job, in the middleware.

    Only called for a wrong guess at a live, issued code (see
    :class:`CodeRejected`). The increment is a single atomic
    ``UPDATE … RETURNING`` and the cap is checked against the value it
    returns, so parallel wrong guesses cannot overwrite each other's count.
    """
    if user is None:
        return
    failed = int(
        db.execute(
            update(User)
            .where(User.id == user.id)
            .values(failed_login_count=User.failed_login_count + 1)
            .returning(User.failed_login_count)
            .execution_options(synchronize_session=False)
        ).scalar_one()
    )
    set_committed_value(user, "failed_login_count", failed)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        actor=user.name,
        actor_id=user.id,
        action="tried to sign in with an incorrect code",
        module=AUTH_AUDIT_MODULE,
        ip_address=ip_address,
        context={"event": "sign_in_failed", "failed_attempts": failed},
    )
    if failed >= settings.max_failed_logins:
        until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        seats = _set_address_lock(db, user, until, ip_address=ip_address)
        log.warning(
            "Locked sign-in for account %s (%d seat(s)) after repeated incorrect codes",
            user.id,
            seats,
        )


def _address_lock_started(user: User) -> datetime | None:
    """When the address's current sign-in pause began, or None if it is not paused."""
    now = utcnow()
    if user.locked_until is None or user.locked_until <= now:
        return None
    return user.locked_until - timedelta(minutes=settings.lockout_minutes)


#: Most (address, network) pairs remembered at once. Past this the least
#: recently seen pair is forgotten, so the table cannot grow without bound.
SOURCE_FAILURES_MAX_ENTRIES = 10_000


class _SourceFailures:
    """Wrong codes counted per (address, client network), in this process.

    The address-wide pause (:func:`_set_address_lock`) is what bounds
    guessing, but on its own it hands a denial of service to anyone: send
    enough wrong codes and the real owner is shut out too. So the pause has
    one exception — a code mailed after it began may be redeemed once (see
    :func:`authenticate`) — and this table is what keeps the exception from
    the source that caused the pause. A network here is the client's IPv4
    address or its IPv6 /64 (:func:`client_ip.rate_limit_key`).

    Bounded twice: entries expire after ``lockout_minutes``, and at most
    :data:`SOURCE_FAILURES_MAX_ENTRIES` are kept, least recently seen
    evicted first. It is per process by design: losing it (a restart, another
    worker) only loses the per-source refusal, never the attempt caps, which
    are enforced in the database.
    """

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        # key -> (wrong codes, paused until (monotonic) or 0.0, last seen (monotonic))
        self._entries: OrderedDict[tuple[str, str], tuple[int, float, float]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _window() -> float:
        return float(settings.lockout_minutes * 60)

    def _live(self, key: tuple[str, str], now: float) -> tuple[int, float, float] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        _count, paused_until, last_seen = entry
        if paused_until <= now and now - last_seen > self._window():
            del self._entries[key]
            return None
        return entry

    def is_paused(self, key: tuple[str, str]) -> bool:
        now = time.monotonic()
        with self._lock:
            entry = self._live(key, now)
            return entry is not None and entry[1] > now

    def note_failure(self, key: tuple[str, str]) -> bool:
        """Count one wrong code from this source. True when it is now paused."""
        now = time.monotonic()
        with self._lock:
            entry = self._live(key, now)
            count = (entry[0] if entry is not None else 0) + 1
            paused_until = entry[1] if entry is not None else 0.0
            if count >= settings.max_failed_logins:
                paused_until = now + self._window()
                count = 0
            self._entries[key] = (count, paused_until, now)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return paused_until > now

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def reset(self) -> None:
        """Forget everything. For tests."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_SOURCE_FAILURES = _SourceFailures(SOURCE_FAILURES_MAX_ENTRIES)


def _source_key(index: str, ip_address: str | None) -> tuple[str, str]:
    return index, client_ip.rate_limit_key(ip_address or "")


def reset_source_failures() -> None:
    """Clear the per-source wrong-code table. Test isolation only."""
    _SOURCE_FAILURES.reset()


def request_sign_in_code(
    db: Session,
    *,
    email: str,
    ip_address: str | None = None,
    user_agent: str = "",
    schedule_mail: MailScheduler | None = None,
) -> CodeChallenge:
    """Start a sign-in. The answer is the same whether or not the address has an account.

    Unknown and deactivated addresses get a burned code row and no mail
    (``deliver=False``), so the response body, the resend throttle and —
    with ``schedule_mail`` — the response time all match a real account's.
    This reverses an earlier choice to answer "no account found": that
    answer turned the sign-in form into a lookup tool for which addresses
    are customers. The screen now says a code was sent *if* the address has
    an account.
    """
    address = normalize_email(email)
    if "@" not in address:
        raise InvalidInputError("Enter a valid email address")

    user = find_user_by_email(db, address)
    seat = user if user is not None and user.is_active else None

    return issue_code(
        db,
        address=address,
        purpose=CodePurpose.SIGN_IN,
        name=(seat.name or "") if seat is not None else "",
        deliver=seat is not None,
        ip_address=ip_address,
        user_agent=user_agent,
        schedule_mail=schedule_mail,
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

    The pairing of address and code is the whole check, and every refusal
    looks the same (:data:`CODE_REJECTED_MESSAGE`) — wrong, stale, unknown
    address, closed account or paused — so the answer gives nothing away.

    While the address is paused after repeated wrong codes, a code mailed
    *after* the pause began may still be redeemed, with one try only, unless
    this caller's own network is the one that caused the pause. That keeps
    the real owner able to get in (ask for a new code, type it) without
    giving a guesser more than one try per mailed code, and the resend
    throttle and rate limiter bound how often a code can be mailed. The code
    is consumed before any session is minted.
    """
    address = normalize_email(email)
    user = find_user_by_email(db, address)

    if user is None or not user.is_active:
        # An unknown or closed address has no live code either, so this is
        # the same refusal as a wrong code, and nothing is counted.
        raise CodeRejected()

    source = _source_key(user.email_index, ip_address)
    if _SOURCE_FAILURES.is_paused(source):
        log.info("Refused a sign-in from a network paused for repeated incorrect codes")
        raise CodeRejected()

    try:
        consume_code(
            db,
            address=address,
            purpose=CodePurpose.SIGN_IN,
            submitted=code,
            issued_after=_address_lock_started(user),
        )
    except CodeRejected as exc:
        if exc.counted:
            _SOURCE_FAILURES.note_failure(source)
            _note_failed_attempt(db, user, ip_address=ip_address)
        # Committed before re-raising: the caller is about to roll this
        # request back, and a rolled-back attempt counter would leave the code
        # space effectively unlimited. See consume_code. The audit rows ride
        # on the same commit.
        db.commit()
        raise

    _SOURCE_FAILURES.clear(source)

    if user.locked_until is not None:
        # A lock was set (and has expired); lift it on every seat, not just
        # this one, so a later switch of workspace does not hit a stale lock.
        _set_address_lock(db, user, None)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_login_ip = ip_address

    org = get_organization(db, user.tenant_id)
    from app.services import mfa as mfa_service

    if mfa_service.needs_login_mfa(user, org):
        _challenge, raw = mfa_service.create_login_challenge(db, org, user)
        methods = tuple(mfa_service.available_methods(db, user))
        audit.record(
            db,
            tenant_id=user.tenant_id,
            actor=user.name,
            actor_id=user.id,
            action="signed in (mfa pending)",
            module=AUTH_AUDIT_MODULE,
            ip_address=ip_address,
            context={"event": "mfa_challenge_issued"},
        )
        db.flush()
        return AuthResult(
            user=user, organization=org, tokens=None, mfa_token=raw, mfa_methods=methods
        )

    tokens = _issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        actor=user.name,
        actor_id=user.id,
        action="signed in",
        module=AUTH_AUDIT_MODULE,
        ip_address=ip_address,
        context={"event": "sign_in_succeeded"},
    )
    db.flush()
    return AuthResult(user=user, organization=org, tokens=tokens)


def complete_mfa_login(
    db: Session,
    *,
    user: User,
    organization: Organization,
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Issue a full session after a successful MFA challenge."""
    tokens = _issue_tokens(
        db,
        user,
        user_agent=user_agent,
        ip_address=ip_address,
        mfa_verified=True,
    )
    audit.record(
        db,
        tenant_id=user.tenant_id,
        actor=user.name,
        actor_id=user.id,
        action="signed in",
        module=AUTH_AUDIT_MODULE,
        ip_address=ip_address,
        context={"event": "sign_in_mfa_succeeded"},
    )
    db.flush()
    return AuthResult(user=user, organization=organization, tokens=tokens)


def refresh_session(
    db: Session, *, refresh_token: str, user_agent: str = "", ip_address: str | None = None
) -> AuthResult:
    """Rotate a refresh token, detecting reuse.

    Tokens are single-use. Presenting one that was already redeemed means a
    copy is circulating, so the entire lineage is revoked and the holder has to
    sign in again.
    """
    presented = hash_token(refresh_token)
    # Row-locked for the rest of the transaction: two requests presenting the
    # same token at once are serialised, so the second sees the first's
    # rotation and is treated as reuse instead of minting a second lineage.
    record = db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == presented)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if record is None:
        log.info("Refused a refresh with an unknown session token")
        raise AuthError("Invalid session — sign in again")

    if record.revoked_at is not None or record.replaced_by is not None:
        revoked = _revoke_family(db, record.family_id, "token_reuse_detected")
        # The workspace's admins should see this: it means a copy of a
        # session was in someone else's hands. Pinned first, because the
        # audit log is under row-level security and nothing is pinned yet.
        bind_tenant(db, record.tenant_id)
        holder = db.get(User, record.user_id)
        audit.record(
            db,
            tenant_id=record.tenant_id,
            actor="System",
            actor_type="system",
            action=(
                f"signed {holder.name if holder is not None else 'a member'} out of a "
                "session that was used twice, which can mean it was copied"
            ),
            module=AUTH_AUDIT_MODULE,
            ip_address=ip_address,
            context={
                "event": "refresh_token_reuse",
                "member_id": record.user_id,
                "family_id": record.family_id,
                "revoked": revoked,
            },
        )
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
        _audit_session_event(
            db,
            record,
            action="could not renew a session because it had expired",
            event="refresh_failed",
            ip_address=ip_address,
            reason="expired",
        )
        db.commit()
        raise AuthError("Session expired — sign in again")

    bind_tenant(db, record.tenant_id)
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        _audit_session_event(
            db,
            record,
            action="could not renew a session because the account is no longer active",
            event="refresh_failed",
            ip_address=ip_address,
            reason="inactive",
        )
        db.commit()
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


def _audit_session_event(
    db: Session,
    record: RefreshToken,
    *,
    action: str,
    event: str,
    ip_address: str | None,
    **context: object,
) -> None:
    """Write a session event to the owning workspace's audit log.

    Pins the token's tenant first (the audit log is under row-level
    security). Records ids only — never the token or its hash.
    """
    bind_tenant(db, record.tenant_id)
    holder = db.get(User, record.user_id)
    audit.record(
        db,
        tenant_id=record.tenant_id,
        actor=holder.name if holder is not None else "A member",
        actor_id=record.user_id,
        action=action,
        module=AUTH_AUDIT_MODULE,
        ip_address=ip_address,
        context={"event": event, "family_id": record.family_id, **context},
    )


def logout(db: Session, *, refresh_token: str, ip_address: str | None = None) -> None:
    record = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
    ).scalar_one_or_none()
    if record is None:
        return
    revoked = _revoke_family(db, record.family_id, "logout")
    db.flush()
    if revoked:
        _audit_session_event(
            db,
            record,
            action="signed out",
            event="signed_out",
            ip_address=ip_address,
            revoked=revoked,
        )


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
    index = email_index(address)

    # Already seated in *this* workspace — not "has any account".
    existing_member = find_membership(db, email_index_value=index, tenant_id=org.id)
    if existing_member is not None and existing_member.is_active:
        raise ConflictError("That person is already a member of this workspace")

    assert_seat_available(db, org)

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


def seat_for_invitation(
    previous: User | None,
    *,
    invitation: Invitation,
    typed_name: str,
    fallback_name: str,
) -> tuple[User, bool]:
    """The ``users`` row an accepted invitation should use, and whether it is new.

    * No row for this address in the workspace: a new seat.
    * A deactivated row: that same row, switched back on with the invited
      role. Its old sessions stay revoked, its failed-code counter and lock
      are cleared, and it never keeps ownership — an invitation grants only
      the role it names.
    * An active row: refused, because they are already a member.
    """
    name = typed_name.strip()
    if previous is not None:
        if previous.is_active:
            raise ConflictError("You are already a member of this workspace — sign in instead")
        previous.is_active = True
        previous.role = invitation.role
        previous.is_owner = False
        if name:
            previous.name = name
        previous.email_verified = True
        previous.email_verified_at = utcnow()
        previous.failed_login_count = 0
        previous.locked_until = None
        return previous, False

    user = User(
        tenant_id=invitation.tenant_id,
        name=name or fallback_name or invitation.email.split("@")[0],
        email=invitation.email,
        email_index=invitation.email_index,
        role=invitation.role,
        # The address is proven by the fact the invite arrived there.
        email_verified=True,
        email_verified_at=utcnow(),
    )
    return user, True


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

    If the address already owns other workspaces, this adds another seat —
    it does not replace them. A member deactivated here and invited back gets
    their old seat back (see :func:`seat_for_invitation`).
    """
    invitation, org = peek_invitation(db, token=token)
    assert_seat_available(db, org)

    existing = find_membership(
        db, email_index_value=invitation.email_index, tenant_id=org.id
    )
    if existing is not None and existing.is_active:
        raise ConflictError("You are already a member of this workspace — sign in instead")

    # Reuse the name from another seat when the invitee did not type one.
    prior_name = full_name.strip()
    if not prior_name:
        identity = db.get(AuthIdentity, invitation.email_index)
        if identity is not None:
            bind_tenant(db, identity.tenant_id)
            prior = db.get(User, identity.user_id)
            if prior is not None:
                prior_name = prior.name
        bind_tenant(db, org.id)

    # A member who was deactivated and is now invited back still has their
    # users row: (tenant, email) is unique, so inserting a second one fails.
    # Bring the old seat back instead.
    previous_seat = db.execute(
        select(User).where(
            User.tenant_id == org.id, User.email_index == invitation.email_index
        )
    ).scalar_one_or_none()
    user, is_new = seat_for_invitation(
        previous_seat, invitation=invitation, typed_name=full_name, fallback_name=prior_name
    )
    if is_new:
        db.add(user)
    try:
        db.flush()
        register_identity(db, user)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("Could not join this workspace") from exc

    invitation.accepted_at = utcnow()
    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action=(
            f"{'joined' if is_new else 'rejoined'} the workspace as "
            f"{ROLE_LABELS[Role(user.role)]}"
        ),
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
    membership = find_membership(
        db, email_index_value=member.email_index, tenant_id=org.id
    )
    if membership is not None:
        membership.is_active = False

    # Only disable the login directory when *no* active seats remain.
    still_active = [
        m
        for m in list_memberships(db, email_index_value=member.email_index)
        if m.is_active
    ]
    identity = db.get(AuthIdentity, member.email_index)
    if identity is not None:
        if still_active:
            identity.user_id = still_active[0].user_id
            identity.tenant_id = still_active[0].tenant_id
            identity.is_active = True
        else:
            identity.is_active = False

    revoke_all_sessions(db, member, "deactivated")
    db.flush()
    audit.record_user_action(
        db, user=actor, action=f"deactivated {member.name}", module="admin"
    )
    return member


# ── Multi-workspace ────────────────────────────────────────────────────────
@dataclass(slots=True)
class WorkspaceCard:
    id: str
    name: str
    slug: str
    primary_domain: str
    role: str
    role_label: str
    onboarding_complete: bool
    is_current: bool
    is_owner: bool


def list_workspace_cards(
    db: Session, *, email_index_value: str, current_tenant_id: str
) -> list[WorkspaceCard]:
    """Cards for every seat this address holds."""
    from app.models.workspace import OnboardingState

    cards: list[WorkspaceCard] = []
    for membership in list_memberships(db, email_index_value=email_index_value):
        if not membership.is_active:
            continue
        bind_tenant(db, membership.tenant_id)
        org = db.get(Organization, membership.tenant_id)
        user = db.get(User, membership.user_id)
        if org is None or not org.is_active or user is None or not user.is_active:
            continue
        state = db.execute(
            select(OnboardingState).where(OnboardingState.tenant_id == org.id)
        ).scalar_one_or_none()
        role = Role(user.role)
        cards.append(
            WorkspaceCard(
                id=org.id,
                name=org.name,
                slug=org.slug,
                primary_domain=org.primary_domain or "",
                role=role.value,
                role_label=ROLE_LABELS[role],
                onboarding_complete=bool(state and state.completed),
                is_current=org.id == current_tenant_id,
                is_owner=user.is_owner,
            )
        )
    # Current workspace first, then alphabetical.
    cards.sort(key=lambda c: (not c.is_current, c.name.lower()))
    # The loop above leaves the session pinned to whichever seat was visited
    # last. Callers keep writing under the current workspace, so put that pin
    # back before returning.
    bind_tenant(db, current_tenant_id)
    return cards


def create_workspace(
    db: Session,
    *,
    actor: User,
    name: str,
    primary_domain: str = "",
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Provision another organisation for an existing account holder."""
    address = normalize_email(actor.email)
    index = actor.email_index
    org_name = name.strip() or "My workspace"
    domain = primary_domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        domain = domain.removeprefix(prefix)
    domain = domain.rstrip("/")

    org_id = new_id()
    org = Organization(
        id=org_id,
        name=org_name,
        slug=unique_slug(db, org_name),
        primary_domain=domain,
        wrapped_dek=OrgCipher.provision(org_id),
        dek_version=1,
        global_autonomy=True,
        plan_name=settings.default_plan_name,
        seats_total=settings.default_seats,
        renews_on=(utcnow() + timedelta(days=settings.default_plan_days)).date(),
    )
    db.add(org)
    db.flush()
    bind_tenant(db, org.id)

    user = User(
        tenant_id=org.id,
        name=actor.name,
        email=address,
        email_index=index,
        role=Role.ADMIN.value,
        is_owner=True,
        email_verified=True,
        email_verified_at=utcnow(),
    )
    db.add(user)
    try:
        db.flush()
        register_identity(db, user)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("Could not create that workspace") from exc

    from app.services.provisioning import provision_organization

    provision_organization(db, org)

    audit.record(
        db,
        tenant_id=org.id,
        actor=user.name,
        actor_id=user.id,
        action="created a new workspace",
        module="admin",
        ip_address=ip_address,
    )
    tokens = _issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)
    db.flush()
    return AuthResult(user=user, organization=org, tokens=tokens)


def switch_workspace(
    db: Session,
    *,
    actor: User,
    organization_id: str,
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Re-issue tokens for another workspace the caller is a member of."""
    membership = find_membership(
        db, email_index_value=actor.email_index, tenant_id=organization_id
    )
    if membership is None or not membership.is_active:
        raise ForbiddenError("You are not a member of that workspace")

    bind_tenant(db, organization_id)
    user = db.get(User, membership.user_id)
    if user is None or not user.is_active:
        raise ForbiddenError("That workspace seat is no longer active")
    org = get_organization(db, organization_id)

    set_last_active(db, user)
    tokens = _issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        actor=user.name,
        actor_id=user.id,
        action="switched into this workspace and was issued a new session",
        module=AUTH_AUDIT_MODULE,
        ip_address=ip_address,
        # No source workspace id: it belongs to another tenant, and this
        # workspace's admins read this log.
        context={"event": "workspace_switched"},
    )
    db.flush()
    return AuthResult(user=user, organization=org, tokens=tokens)


def delete_workspace(
    db: Session,
    *,
    actor: User,
    organization_id: str,
    user_agent: str = "",
    ip_address: str | None = None,
) -> AuthResult:
    """Remove a workspace from the caller's account without wiping rows.

    Flips ``is_active`` on the organisation, every membership seat, and users
    in that tenant. The console treats it as a normal delete; data stays for
    recovery / audit. Returns a session on a remaining workspace (switching
    automatically when the deleted one was current).
    """
    membership = find_membership(
        db, email_index_value=actor.email_index, tenant_id=organization_id
    )
    if membership is None or not membership.is_active:
        raise NotFoundError("Workspace not found")

    bind_tenant(db, organization_id)
    user = db.get(User, membership.user_id)
    if user is None or not user.is_active:
        raise NotFoundError("Workspace not found")
    if not user.is_owner:
        raise ForbiddenError("Only the workspace owner can delete it")

    org = db.get(Organization, organization_id)
    if org is None or not org.is_active:
        raise NotFoundError("Workspace not found")

    live_cards = list_workspace_cards(
        db,
        email_index_value=actor.email_index,
        current_tenant_id=actor.tenant_id,
    )
    # Card listing re-pins to the actor's current seat; the soft-delete and
    # its audit row must run under the workspace being removed.
    bind_tenant(db, organization_id)
    if len(live_cards) <= 1:
        raise ConflictError("You need at least one workspace. Create another before deleting this one.")

    was_current = organization_id == actor.tenant_id
    fallback = next((c for c in live_cards if c.id != organization_id), None)
    if fallback is None:
        raise ConflictError("You need at least one workspace. Create another before deleting this one.")

    # Soft-delete: hide from switchers / login without dropping tenant data.
    org.is_active = False
    for seat in list(
        db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.tenant_id == organization_id
            )
        ).scalars()
    ):
        seat.is_active = False

    for member in list(
        db.execute(select(User).where(User.tenant_id == organization_id)).scalars()
    ):
        member.is_active = False

    audit.record(
        db,
        tenant_id=organization_id,
        actor=user.name,
        actor_id=user.id,
        action="deleted a workspace",
        module="admin",
        ip_address=ip_address,
    )
    db.flush()

    if was_current:
        return switch_workspace(
            db,
            actor=actor,
            organization_id=fallback.id,
            user_agent=user_agent,
            ip_address=ip_address,
        )

    # Stay on the current workspace; refresh tokens so the session card list
    # drops the deleted org.
    bind_tenant(db, actor.tenant_id)
    current_org = get_organization(db, actor.tenant_id)
    current_user = db.get(User, actor.id)
    if current_user is None or not current_user.is_active:
        return switch_workspace(
            db,
            actor=actor,
            organization_id=fallback.id,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    tokens = _issue_tokens(
        db, current_user, user_agent=user_agent, ip_address=ip_address
    )
    db.flush()
    return AuthResult(user=current_user, organization=current_org, tokens=tokens)


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
