"""Website analyses: public (marketing-site demo) and per-workspace history.

Revision ID: 0014_demo_analysis
Revises: 0013_content_assets
Create Date: 2026-10-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_demo_analysis"
down_revision: str | None = "0013_content_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB


def _job_columns() -> list[sa.Column]:
    return [
        sa.Column("website_url", sa.String(500), nullable=False),
        sa.Column("business_url", sa.String(1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("progress", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("report", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(80), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "demo_analyses" not in existing:
        # Public: a visitor has no workspace, so this table is not tenant scoped.
        op.create_table(
            "demo_analyses",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("device_hash", sa.String(64), nullable=False),
            sa.Column("fingerprint_hash", sa.String(64), nullable=False),
            sa.Column("ip_hash", sa.String(64), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            *_job_columns(),
        )
        op.create_index("ix_demo_device", "demo_analyses", ["device_hash"])
        op.create_index("ix_demo_fingerprint", "demo_analyses", ["fingerprint_hash"])
        op.create_index("ix_demo_ip_created", "demo_analyses", ["ip_hash", "created_at"])

    if "site_analyses" not in existing:
        op.create_table(
            "site_analyses",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("requested_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("requested_by_name", sa.String(160), nullable=False, server_default=""),
            sa.Column("context", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            *_job_columns(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_site_analyses_tenant_id", "site_analyses", ["tenant_id"])
        op.create_index("ix_site_analyses_tenant_created", "site_analyses", ["tenant_id", "created_at"])

        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=("site_analyses",))


def downgrade() -> None:
    op.drop_table("site_analyses")
    op.drop_table("demo_analyses")
