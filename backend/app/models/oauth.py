"""Pending sign-ins: one row per "Sign in with <Vendor>" in flight.

The browser carries a signed, expiring ``state`` to the vendor and back. The
signature proves the state was issued here and has not been altered; this
row is what makes it **single-use** across every worker. The callback deletes
the row as it redeems it (``DELETE … RETURNING``), so a replayed state finds
nothing — whichever worker it lands on.

The row also holds the PKCE code verifier, encrypted under the organisation's
key, so the verifier never travels through the browser.

Like ``login_codes`` it sits outside row-level security: it is read by the
public callback, before any session exists. Authorisation is possession of a
state signed with the platform's secret key, and the row id is a SHA-256 of
the state's random nonce, so the table on its own yields nothing redeemable.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, Base


class OAuthState(Base):
    __tablename__ = "oauth_states"
    __table_args__ = (Index("ix_oauth_states_expiry", "expires_at"),)

    #: SHA-256 (hex) of the state's nonce.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        FK, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: PKCE verifier, encrypted under the organisation's data key.
    verifier_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Non-secret flow context (the WordPress site address being approved).
    context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
