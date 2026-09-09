"""The global authentication tables.

Two tables live here, and they have one thing in common: both are read
*before* a session exists, so neither can be scoped by one. Everything else in
the schema is tenant-scoped and protected by row-level security.

Row-level security means an authenticated session can only see its own
organisation's rows — which is exactly right, and creates one problem: at the
moment somebody types their email into the login form, there is no session and
no organisation yet. The lookup that resolves *which* workspace an address
belongs to therefore cannot itself be tenant-scoped.

This table is that lookup, and it is deliberately the smallest thing that can
do the job:

* the only identifying value it holds is the **blind index** of the address —
  an HMAC, not the address, and not reversible;
* it carries no name, no credential, no personal data of any kind;
* it is the platform-wide uniqueness constraint on an email address.

The flow is: index the submitted address, find the row, pin that
organisation, and load the user *under* row-level security from there. So the
directory reveals nothing on its own, and everything after it is scoped.

:class:`LoginCode` is the second: the one-time passcode that *is* the
credential on this platform, since there are no passwords. It is keyed on the
same blind index and holds no plaintext either.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TimestampMixin, new_id
from app.db.types import BlindIndex, EncryptedString


class AuthIdentity(Base, TimestampMixin):
    __tablename__ = "auth_identities"
    __table_args__ = (Index("ix_identity_tenant", "tenant_id"),)

    # The blind index is the primary key: it is unique platform-wide, which is
    # what stops the same address registering twice.
    email_index: Mapped[str] = mapped_column(BlindIndex, primary_key=True)

    user_id: Mapped[str] = mapped_column(
        FK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    tenant_id: Mapped[str] = mapped_column(
        FK, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    # Mirrored from the user so a disabled account can be rejected before the
    # tenant is pinned and the full row loaded.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # google | microsoft | okta | "" for one-time-code accounts.
    sso_provider: Mapped[str] = mapped_column(String(64), default="")


class CodePurpose(StrEnum):
    """What a one-time code is allowed to do.

    Bound into the stored row so a code mailed out to prove ownership of a new
    address cannot be replayed to sign in to an existing account, or the other
    way round.
    """

    SIGN_IN = "sign_in"
    SIGN_UP = "sign_up"


class LoginCode(Base, TimestampMixin):
    """A one-time passcode mailed to an address, stored only as an HMAC.

    This is the whole credential: the platform has no passwords, so possession
    of a live mailbox *is* the authentication. That puts the burden on this
    table, and the rules it enforces are the ones that make an emailed six-digit
    number safe to log in with:

    * **short-lived** — minutes, not hours, so a code sitting in a mailbox is
      not a standing key;
    * **single-use** — ``consumed_at`` is set the moment it succeeds;
    * **attempt-capped** — ``attempts`` is incremented on every wrong guess and
      the row is dead once it passes the limit, which is what makes a
      six-digit space (a million) impossible to walk;
    * **superseded on reissue** — asking for a new code kills the old one, so
      only one code per address and purpose is ever live;
    * **stored as an HMAC** — a database leak yields no usable code, exactly as
      with refresh and invitation tokens.

    Like :class:`AuthIdentity`, it sits outside row-level security: a code is
    requested before any session exists, and for sign-up before the
    organisation exists at all. It holds no plaintext address — only the blind
    index — so the table on its own says nothing about who is signing in.
    """

    __tablename__ = "login_codes"
    __table_args__ = (
        Index("ix_login_codes_lookup", "email_index", "purpose"),
        Index("ix_login_codes_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)

    email_index: Mapped[str] = mapped_column(BlindIndex, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set when the attempt cap is hit, so the reason a code stopped working can
    # be reported accurately instead of as a generic "invalid".
    burned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Requested-from metadata, for the audit trail and for spotting a single
    # source spraying addresses. The address itself is encrypted, not indexed.
    ip_address: Mapped[str | None] = mapped_column(
        EncryptedString("login_codes.ip_address"), nullable=True
    )
    user_agent: Mapped[str] = mapped_column(String(255), default="")

    @property
    def is_live(self) -> bool:
        return self.consumed_at is None and self.burned_at is None
