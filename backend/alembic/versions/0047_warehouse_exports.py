"""Warehouse export jobs (BigQuery/Snowflake/S3 abstraction).

Revision ID: 0047_warehouse_exports
Revises: 0046_user_approval_scopes
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_warehouse_exports"
down_revision: str | None = "0046_user_approval_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
TABLE = "warehouse_export_jobs"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        op.create_table(
            TABLE,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("dataset", sa.String(80), nullable=False, server_default="metrics"),
            sa.Column("destination", sa.String(512), nullable=False, server_default=""),
            sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
            sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_warehouse_export_jobs_tenant_id", TABLE, ["tenant_id"])
        op.create_index(
            "ix_warehouse_export_tenant_status", TABLE, ["tenant_id", "status"]
        )
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
        op.drop_table(TABLE)
