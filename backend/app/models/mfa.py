"""MFA recovery codes, WebAuthn credentials, and pending login challenges."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class MfaRecoveryCode(Base, TimestampMixin, TenantScopedMixin):
    """Hashed one-time recovery code — plaintext shown once at enrolment."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code_hash", name="uq_mfa_recovery_hash"),
        Index("ix_mfa_recovery_user", "tenant_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        FK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebAuthnCredential(Base, TimestampMixin, TenantScopedMixin):
    """Passkey / security-key credential (py_webauthn)."""

    __tablename__ = "webauthn_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "credential_id", name="uq_webauthn_cred"),
        Index("ix_webauthn_user", "tenant_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        FK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Base64url credential id.
    credential_id: Mapped[str] = mapped_column(String(512), nullable=False)
    #: COSE public key (base64url) — not a workspace secret of high value but
    #: still encrypted under the org DEK via Ctx.WEBAUTHN_PUBKEY.
    public_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    sign_count: Mapped[int] = mapped_column(default=0, nullable=False)
    transports: Mapped[str] = mapped_column(String(255), default="")
    nickname: Mapped[str] = mapped_column(String(80), default="")
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MfaPendingChallenge(Base, TimestampMixin, TenantScopedMixin):
    """Short-lived MFA challenge after email OTP (hashed token at rest)."""

    __tablename__ = "mfa_pending_challenges"
    __table_args__ = (
        UniqueConstraint("tenant_id", "token_hash", name="uq_mfa_challenge_hash"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        FK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Last TOTP time-step accepted (replay protection within the window).
    last_totp_step: Mapped[int] = mapped_column(default=0, nullable=False)
    #: WebAuthn ceremony challenge (encrypted JSON options).
    webauthn_challenge_encrypted: Mapped[str] = mapped_column(Text, default="")
