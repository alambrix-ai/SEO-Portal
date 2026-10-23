"""Cross-channel budget proposals with daily shift caps.

Revision ID: 0034_cross_channel_budget
Revises: 0033_creative_video_scripts
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_cross_channel_budget"
down_revision: str | None = "0033_creative_video_scripts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLE = "cross_channel_budget_proposals"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("snapshot_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "current_shares", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "proposed_shares", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("shifts", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("blended_roas_before", sa.String(32), nullable=False, server_default=""),
        sa.Column("blended_roas_after", sa.String(32), nullable=False, server_default=""),
        sa.Column("moved_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("approval_item_id", UUID, nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_cc_budget_tenant_status", TABLE, ["tenant_id", "status"])

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        return
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
