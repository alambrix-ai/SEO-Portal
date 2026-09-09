"""Passwordless sign-in: one-time emailed codes replace passwords.

What changes, and why it is safe to run against a live database:

* ``login_codes`` is created. It holds the one-time passcode that is now the
  only credential — an HMAC of it, a blind index of the address, and the
  rules' bookkeeping (expiry, attempts, single use). Like ``auth_identities``
  it is deliberately **not** tenant-scoped and gets no RLS policy: a code is
  requested before any session exists, and for a sign-up before the
  organisation does.

* ``users.password_hash`` and ``users.password_changed_at`` are dropped. This
  is the destructive part and it is intentional: leaving a column of Argon2
  hashes behind that nothing reads is a breach waiting to be exploited for no
  benefit. Existing accounts are unaffected — they sign in with a mailed code
  from the next attempt onwards, which needs nothing stored.

* ``verification_tokens`` is dropped. Email verification links no longer
  exist: an account cannot be created without proving the address first, by
  code at sign-up or by opening an invitation, so there is nothing left to
  verify after the fact. Existing rows are single-use links that have no
  endpoint to be redeemed at, so keeping them would be misleading.

* Every existing user is marked ``email_verified``. They arrived through the
  old flow; from here on the address is proven at creation, and the column
  should not read as "unproven" for accounts that predate the change.

The downgrade puts the tables and columns back, but it **cannot** restore any
password hash — nothing here has one to restore. After a downgrade every
account is left with an empty ``password_hash``, which verifies against
nothing, so a rollback has to be followed by giving people a way back in.
That asymmetry is inherent to deleting credentials and is called out here
rather than discovered later.

Revision ID: 0002_passwordless_sign_in
Revises: 0001_initial_schema
Create Date: 2026-09-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_passwordless_sign_in"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "login_codes" in tables:
        # Fresh install: 0001 create_all already created the current schema.
        return

    op.create_table(
        "login_codes",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        # The blind index of the address: an HMAC, not the address itself.
        sa.Column("email_index", sa.String(length=44), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("burned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The lookup the issue/consume path makes on every request.
    op.create_index("ix_login_codes_lookup", "login_codes", ["email_index", "purpose"])
    # Supports pruning expired rows without a sequential scan.
    op.create_index("ix_login_codes_expiry", "login_codes", ["expires_at"])
    op.create_index("ix_login_codes_code_hash", "login_codes", ["code_hash"])

    # Accounts created under the old flow keep a proven address.
    op.execute("UPDATE users SET email_verified = true WHERE email_verified = false")
    op.execute(
        "UPDATE users SET email_verified_at = COALESCE(email_verified_at, created_at)"
    )

    op.drop_table("verification_tokens")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "password_changed_at")


def downgrade() -> None:
    # Restored empty: see the note above. No stored hash survives the upgrade,
    # so a rolled-back deployment has no working passwords to fall back on.
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "verification_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_verification_tokens_tenant_id", "verification_tokens", ["tenant_id"])
    op.create_index("ix_verification_tokens_user_id", "verification_tokens", ["user_id"])
    op.create_index("ix_verification_tokens_purpose", "verification_tokens", ["purpose"])
    op.create_index("ix_verification_tokens_token_hash", "verification_tokens", ["token_hash"])

    op.drop_index("ix_login_codes_code_hash", table_name="login_codes")
    op.drop_index("ix_login_codes_expiry", table_name="login_codes")
    op.drop_index("ix_login_codes_lookup", table_name="login_codes")
    op.drop_table("login_codes")
