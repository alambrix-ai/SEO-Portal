"""Local business: Maps search positions and Google review replies.

Two additive tables for the Local Search Tracker and the Review Responder.
Nothing existing is altered.

Revision ID: 0012_local_business
Revises: 0011_workspace_memberships
Create Date: 2026-10-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_local_business"
down_revision: str | None = "0011_workspace_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())
    created: list[str] = []

    if "local_rank_checks" not in existing:
        op.create_table(
            "local_rank_checks",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("phrase", sa.String(length=160), nullable=False),
            sa.Column("position", sa.Integer(), nullable=True),
            sa.Column("results_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("radius_m", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("place_id", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("business_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("rating", sa.Float(), nullable=True),
            sa.Column("review_count", sa.Integer(), nullable=True),
            sa.Column(
                "top_results", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_local_rank_checks_tenant_id", "local_rank_checks", ["tenant_id"])
        op.create_index("ix_local_rank_checks_checked_at", "local_rank_checks", ["checked_at"])
        op.create_index("ix_local_rank_tenant_checked", "local_rank_checks", ["tenant_id", "checked_at"])
        created.append("local_rank_checks")

    if "review_replies" not in existing:
        op.create_table(
            "review_replies",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("review_name", sa.String(length=512), nullable=False),
            sa.Column("reviewer", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("stars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("review_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("reply_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="drafted"),
            sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "review_name", name="uq_review_reply_review"),
        )
        op.create_index("ix_review_replies_tenant_id", "review_replies", ["tenant_id"])
        created.append("review_replies")

    if created:
        # Tenant-scoped like every business table.
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    for table in ("review_replies", "local_rank_checks"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
