"""Content refresh candidates.

Revision ID: 0024_content_refresh
Revises: 0023_audit_truncate_guard
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_content_refresh"
down_revision: str | None = "0023_audit_truncate_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "content_refresh_candidates" in set(insp.get_table_names()):
        return

    op.create_table(
        "content_refresh_candidates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("page_id", UUID, nullable=True),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column(
            "reason_codes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("prior_clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recent_clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_position", sa.Float(), nullable=True),
        sa.Column("recent_position", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("rationale_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["seo_pages.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_content_refresh_candidates_tenant_id",
        "content_refresh_candidates",
        ["tenant_id"],
    )
    op.create_index(
        "ix_refresh_candidates_tenant_status",
        "content_refresh_candidates",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_refresh_candidates_tenant_url",
        "content_refresh_candidates",
        ["tenant_id", "url"],
    )

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=("content_refresh_candidates",))


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON content_refresh_candidates")
    op.drop_table("content_refresh_candidates")
