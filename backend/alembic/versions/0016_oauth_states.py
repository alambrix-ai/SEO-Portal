"""Pending platform sign-ins ("Sign in with Google", WordPress approval).

One row per sign-in in flight: makes each signed state single-use across
workers and keeps the PKCE verifier server-side (encrypted). Outside
row-level security, like login_codes — it is read by the public OAuth
callback before any session exists.

Revision ID: 0016_oauth_states
Revises: 0015_audit_history
Create Date: 2026-10-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_oauth_states"
down_revision: str | None = "0015_audit_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "oauth_states" in existing:
        return

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("verifier_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("context", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_oauth_states_tenant_id", "oauth_states", ["tenant_id"])
    op.create_index("ix_oauth_states_expiry", "oauth_states", ["expires_at"])


def downgrade() -> None:
    op.drop_table("oauth_states")
