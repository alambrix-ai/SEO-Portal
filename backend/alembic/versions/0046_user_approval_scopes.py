"""Per-user brand/region/channel approval scopes.

Revision ID: 0046_user_approval_scopes
Revises: 0045_mfa_factors
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046_user_approval_scopes"
down_revision: str | None = "0045_mfa_factors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLE = "user_approval_scopes"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        op.create_table(
            TABLE,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("user_id", UUID, nullable=False),
            sa.Column("brands", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("regions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("channels", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "user_id", name="uq_user_approval_scope"),
        )
        op.create_index("ix_user_approval_scopes_tenant_id", TABLE, ["tenant_id"])
        op.create_index("ix_user_approval_scopes_user_id", TABLE, ["user_id"])
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
        op.drop_table(TABLE)
