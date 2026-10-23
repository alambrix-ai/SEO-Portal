"""Ask Willy grounding traces for AI governance.

Revision ID: 0027_willy_grounding
Revises: 0026_topic_clusters
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_willy_grounding"
down_revision: str | None = "0026_topic_clusters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLE = "willy_grounding_traces"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("tool_calls", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("cost_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_willy_trace_tenant_at", TABLE, ["tenant_id", "observed_at"])

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        return
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
