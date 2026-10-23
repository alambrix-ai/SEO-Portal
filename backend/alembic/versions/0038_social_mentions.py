"""Social mentions for listening → reply routing.

Revision ID: 0038_social_mentions
Revises: 0037_locations
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_social_mentions"
down_revision: str | None = "0037_locations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLE = "social_mentions"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("network", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("author", sa.String(200), nullable=False, server_default=""),
        sa.Column("text_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("sentiment", sa.String(16), nullable=False, server_default="neutral"),
        sa.Column("sentiment_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_item_id", UUID, nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "network", "external_id", name="uq_social_mention_external"
        ),
    )
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_social_mention_tenant_status", TABLE, ["tenant_id", "status"])

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        return
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
