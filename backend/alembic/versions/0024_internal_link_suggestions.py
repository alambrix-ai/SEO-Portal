"""Internal link suggestions for the Internal Linking agent.

Revision ID: 0024_internal_link_suggestions
Revises: 0023_audit_truncate_guard
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_internal_link_suggestions"
down_revision: str | None = "0023_audit_truncate_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
TABLE = "internal_link_suggestions"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        return

    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("source_page_id", UUID, nullable=False),
        sa.Column("target_page_id", UUID, nullable=False),
        sa.Column("source_url", sa.String(512), nullable=False),
        sa.Column("target_url", sa.String(512), nullable=False),
        sa.Column("anchor", sa.String(80), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_page_id"], ["seo_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_page_id"], ["seo_pages.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_page_id",
            "target_page_id",
            "anchor",
            name="uq_internal_link_identity",
        ),
    )
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])
    op.create_index(f"ix_{TABLE}_source_page_id", TABLE, ["source_page_id"])
    op.create_index(f"ix_{TABLE}_target_page_id", TABLE, ["target_page_id"])
    op.create_index("ix_internal_link_tenant_status", TABLE, ["tenant_id", "status"])

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        return
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
