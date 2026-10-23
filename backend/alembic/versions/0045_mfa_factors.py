"""MFA recovery codes, WebAuthn credentials, pending challenges.

Revision ID: 0045_mfa_factors
Revises: 0044_commercial_billing
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045_mfa_factors"
down_revision: str | None = "0044_commercial_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
TABLES = ("mfa_recovery_codes", "webauthn_credentials", "mfa_pending_challenges")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "mfa_recovery_codes" not in existing:
        op.create_table(
            "mfa_recovery_codes",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("user_id", UUID, nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "code_hash", name="uq_mfa_recovery_hash"),
        )
        op.create_index("ix_mfa_recovery_codes_tenant_id", "mfa_recovery_codes", ["tenant_id"])
        op.create_index("ix_mfa_recovery_codes_user_id", "mfa_recovery_codes", ["user_id"])
        op.create_index("ix_mfa_recovery_user", "mfa_recovery_codes", ["tenant_id", "user_id"])
        created.append("mfa_recovery_codes")

    if "webauthn_credentials" not in existing:
        op.create_table(
            "webauthn_credentials",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("user_id", UUID, nullable=False),
            sa.Column("credential_id", sa.String(512), nullable=False),
            sa.Column("public_key_encrypted", sa.Text(), nullable=False),
            sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("transports", sa.String(255), nullable=False, server_default=""),
            sa.Column("nickname", sa.String(80), nullable=False, server_default=""),
            sa.Column("backed_up", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "credential_id", name="uq_webauthn_cred"),
        )
        op.create_index("ix_webauthn_credentials_tenant_id", "webauthn_credentials", ["tenant_id"])
        op.create_index("ix_webauthn_credentials_user_id", "webauthn_credentials", ["user_id"])
        op.create_index("ix_webauthn_user", "webauthn_credentials", ["tenant_id", "user_id"])
        created.append("webauthn_credentials")

    if "mfa_pending_challenges" not in existing:
        op.create_table(
            "mfa_pending_challenges",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("user_id", UUID, nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_totp_step", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("webauthn_challenge_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "token_hash", name="uq_mfa_challenge_hash"),
        )
        op.create_index(
            "ix_mfa_pending_challenges_tenant_id", "mfa_pending_challenges", ["tenant_id"]
        )
        op.create_index("ix_mfa_pending_challenges_user_id", "mfa_pending_challenges", ["user_id"])
        created.append("mfa_pending_challenges")

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
