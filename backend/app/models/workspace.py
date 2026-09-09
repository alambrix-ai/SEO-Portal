"""Organisation, user, and onboarding models.

An *organisation* is the tenant. Anyone can create one from the sign-up screen;
the creator becomes its Super Admin and invites the rest of the team.

Personal data (names, email addresses) is stored under tier-1 encrypted
columns. Email is additionally carried as a blind index so login and
uniqueness still work without the plaintext ever being written to disk.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.rbac import Role
from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id
from app.db.types import BlindIndex, EncryptedString


class PlanTier(StrEnum):
    TRIAL = "trial"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class Organization(Base, TimestampMixin):
    """A customer workspace — the tenant every other row is scoped to."""

    __tablename__ = "organizations"
    __table_args__ = (UniqueConstraint("slug", name="uq_org_slug"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # URL-safe handle, unique across the platform.
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    primary_domain: Mapped[str] = mapped_column(String(255), default="")

    # ── Encryption ─────────────────────────────────────────────────────────
    # This organisation's data-encryption key, stored only wrapped under the
    # master KEK. Nothing else in the row is secret; this column is what makes
    # the tenant's own payloads unreadable without the master key.
    wrapped_dek: Mapped[str] = mapped_column(Text, nullable=False)
    dek_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    dek_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Autonomy ───────────────────────────────────────────────────────────
    # Behind the header's Autonomous / Human review control; flipping it
    # cascades to every agent in the workspace.
    global_autonomy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Billing (Admin screen) ─────────────────────────────────────────────
    plan_name: Mapped[str] = mapped_column(String(80), default="Enterprise Plan")
    plan_tier: Mapped[str] = mapped_column(String(24), default=PlanTier.TRIAL.value)
    seats_total: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    renews_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    data_region: Mapped[str] = mapped_column(String(24), default="eu-central")
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)


class User(Base, TimestampMixin, TenantScopedMixin):
    """A member of one organisation.

    ``email_index`` is the HMAC blind index used for lookups; ``email`` is the
    encrypted column the application reads. Uniqueness is enforced on the
    index, because the ciphertext differs on every write.

    There is no password column, and that is the design: sign-in is a one-time
    code mailed to this address (see :class:`app.models.identity.LoginCode`).
    The address is therefore not merely a username — it is the credential, so
    it is the thing the platform protects.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email_index", name="uq_users_email_index"),
        Index("ix_users_tenant_role", "tenant_id", "role"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)

    name: Mapped[str] = mapped_column(EncryptedString("users.name"), nullable=False)
    email: Mapped[str] = mapped_column(EncryptedString("users.email"), nullable=False)
    email_index: Mapped[str] = mapped_column(BlindIndex, nullable=False, index=True)

    role: Mapped[str] = mapped_column(String(32), default=Role.CLIENT.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The creator of the organisation. Cannot be removed or demoted by peers.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set when the account arrived through an identity provider instead of a
    # mailed code: google | microsoft | okta.
    sso_provider: Mapped[str] = mapped_column(String(64), default="")
    sso_subject: Mapped[str] = mapped_column(String(255), default="")

    # Reserved for a second factor on top of the mailed code (an authenticator
    # app). Unused in this build; no flow reads it yet.
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(
        EncryptedString("users.mfa_secret"), nullable=True
    )

    # When this person last opened the notification panel. What makes the
    # bell's dot mean "there is something you have not seen" rather than
    # "there is activity", which would be true forever and therefore useless.
    # Server-side rather than in the browser, because having read something on
    # a laptop should not leave it unread on a phone.
    notifications_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Brute-force protection ─────────────────────────────────────────────
    # Counts wrong codes, not wrong passwords, and locks the account the same
    # way — otherwise a six-digit space could be walked one request at a time
    # across many freshly issued codes.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(
        EncryptedString("users.last_login_ip"), nullable=True
    )


class RefreshToken(Base, TimestampMixin, TenantScopedMixin):
    """A rotating refresh token, stored only as an HMAC.

    Rotation is single-use: redeeming a token issues a replacement and records
    it in ``replaced_by``. If a token is presented twice, the whole family is
    revoked — that is how a stolen refresh token gets caught.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_user_active", "user_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        FK, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    family_id: Mapped[str] = mapped_column(FK, nullable=False, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(64), default="")
    replaced_by: Mapped[str | None] = mapped_column(FK, nullable=True)

    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str | None] = mapped_column(
        EncryptedString("refresh_tokens.ip_address"), nullable=True
    )


class Invitation(Base, TimestampMixin, TenantScopedMixin):
    """An invitation for a teammate to join with a chosen role.

    Accepting it needs no code of its own: the token arrived in the invitee's
    mailbox, which proves the same thing a sign-in code proves.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitations_tenant_email", "tenant_id", "email_index"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(EncryptedString("invitations.email"), nullable=False)
    email_index: Mapped[str] = mapped_column(BlindIndex, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    invited_by: Mapped[str | None] = mapped_column(
        FK, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingState(Base, TimestampMixin, TenantScopedMixin):
    """Persisted state of the four-step onboarding wizard.

    Connect website -> connect ad accounts -> set guardrails -> review & launch.
    """

    __tablename__ = "onboarding_state"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_onboarding_tenant"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), default="")
    cms: Mapped[str] = mapped_column(String(64), default="WordPress")
    ad_accounts: Mapped[dict] = mapped_column(
        JSONB, default=lambda: {"google": True, "meta": False, "linkedin": False}
    )
    # full | hybrid | human
    guardrail: Mapped[str] = mapped_column(String(16), default="hybrid", nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLogEntry(Base, TenantScopedMixin):
    """Append-only record of every consequential action, human or agent."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_at", "tenant_id", "at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    # user | agent | system
    actor_type: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str] = mapped_column(String(32), default="")
    # Request provenance, encrypted: an audit trail is itself personal data.
    ip_address: Mapped[str | None] = mapped_column(
        EncryptedString("audit_log.ip_address"), nullable=True
    )
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
