"""Video script jobs for Creative Studio short-form expansion.

Revision ID: 0033_creative_video_scripts
Revises: 0032_content_repurpose
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_creative_video_scripts"
down_revision: str | None = "0032_content_repurpose"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLE = "video_script_jobs"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "thumbnail_formats", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("asset_id", UUID, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("approval_item_id", UUID, nullable=True),
        sa.Column("product_id", UUID, nullable=True),
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
    op.create_index("ix_video_script_tenant_status", TABLE, ["tenant_id", "status"])

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        return
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
