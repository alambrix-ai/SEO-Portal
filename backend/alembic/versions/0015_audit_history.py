"""Audit history: dated snapshots of the Technical SEO audit, SEO & AEO page
scores and brand profile versions.

Revision ID: 0015_audit_history
Revises: 0014_demo_analysis
Create Date: 2026-10-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_audit_history"
down_revision: str | None = "0014_demo_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "audit_snapshots" in existing:
        return

    op.create_table(
        "audit_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
        sa.Column("created_by_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("items", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_audit_snapshots_tenant_id", "audit_snapshots", ["tenant_id"])
    op.create_index(
        "ix_audit_snapshots_tenant_kind_created", "audit_snapshots", ["tenant_id", "kind", "created_at"]
    )

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=("audit_snapshots",))


def downgrade() -> None:
    op.drop_table("audit_snapshots")
